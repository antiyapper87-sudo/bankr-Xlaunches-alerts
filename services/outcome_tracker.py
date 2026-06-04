from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database import create_or_update_token_outcome, pct_change, utc_now
from services.agent_memory import update_memory_from_outcome


OUTCOME_WINDOWS = ("1h", "4h", "24h", "7d")


def num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def market_snapshot(dex: dict[str, Any] | None) -> dict[str, Any]:
    dex = dex or {}
    return {
        "observed_at": utc_now().isoformat(),
        "mcap": num(dex.get("mcap")),
        "volume_24h": num(dex.get("volume_24h")),
        "liquidity": num(dex.get("liquidity")),
        "price_usd": str(dex.get("price_usd") or "0"),
        "pair_address": dex.get("pair_address") or "",
        "pair_created_at": dex.get("pair_created_at") or 0,
        "source": dex.get("_source") or "",
    }


def classify_outcome(initial: dict[str, Any], latest: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    initial_mcap = num(initial.get("mcap"))
    latest_mcap = num(latest.get("mcap"))
    latest_liq = num(latest.get("liquidity"))
    initial_liq = num(initial.get("liquidity"))
    mcap_change = pct_change(initial_mcap, latest_mcap)
    liquidity_removed_pct = 0.0
    if initial_liq > 0 and latest_liq >= 0:
        liquidity_removed_pct = max(0.0, ((initial_liq - latest_liq) / initial_liq) * 100)
    if liquidity_removed_pct >= 70:
        label = "rug_or_liquidity_removed"
    elif mcap_change is not None and mcap_change <= -80:
        label = "dumped"
    elif initial_mcap > 0 and latest_mcap >= initial_mcap * 5:
        label = "winner_24h"
    elif initial_mcap > 0 and latest_mcap >= initial_mcap * 3:
        label = "winner_4h"
    elif initial_mcap > 0 and latest_mcap >= initial_mcap * 2:
        label = "winner_1h"
    elif num(latest.get("volume_24h")) < 10_000 and (mcap_change is not None and mcap_change <= -50):
        label = "dead"
    else:
        label = "flat"
    return label, {
        "mcap_change_pct": mcap_change,
        "liquidity_removed_pct": liquidity_removed_pct,
        "initial_mcap": initial_mcap,
        "latest_mcap": latest_mcap,
    }


async def schedule_initial_outcome(
    db: AsyncSession,
    *,
    chain: str = "base",
    token_id: str,
    launch: dict[str, Any],
    dex: dict[str, Any] | None,
    signal_id: int | None = None,
    initial_score: float | None = None,
    initial_label: str = "",
) -> None:
    snapshot = market_snapshot(dex)
    first_seen_at = utc_now()
    raw_first_seen = launch.get("first_seen_at")
    if isinstance(raw_first_seen, datetime):
        first_seen_at = raw_first_seen
    await create_or_update_token_outcome(
        db,
        chain=chain,
        token_id=token_id,
        launch_source=launch.get("source") or "",
        ticker=launch.get("symbol") or launch.get("ticker") or "",
        deployer_wallet=launch.get("deployer_wallet") or launch.get("msg_sender") or "",
        pair_address=snapshot.get("pair_address") or "",
        first_seen_at=first_seen_at,
        signal_id=signal_id,
        initial_verdict_version="signal-initial",
        initial_score=initial_score,
        initial_label=initial_label,
        next_check_at=utc_now() + timedelta(hours=1),
        snapshot_key="1h",
        snapshot_json={"initial": snapshot},
        evidence_json={"initial_market": snapshot},
        status="tracking",
    )


async def record_outcome_snapshot(
    db: AsyncSession,
    *,
    chain: str = "base",
    token_id: str,
    window: str,
    dex: dict[str, Any] | None,
    initial_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest = market_snapshot(dex)
    label, metrics = classify_outcome(initial_snapshot or {}, latest)
    next_check_at = None
    if window == "1h":
        next_check_at = utc_now() + timedelta(hours=3)
    elif window == "4h":
        next_check_at = utc_now() + timedelta(hours=20)
    elif window == "24h":
        next_check_at = utc_now() + timedelta(days=6)
    outcome = await create_or_update_token_outcome(
        db,
        chain=chain,
        token_id=token_id,
        next_check_at=next_check_at,
        snapshot_key=window,
        snapshot_json={"market": latest, "metrics": metrics},
        final_outcome_label=label,
        evidence_json={f"snapshot_{window}": latest, "metrics": metrics},
        status="completed" if window == "7d" else "tracking",
    )
    if outcome.status == "completed":
        await update_memory_from_outcome(db, outcome)
    return {"window": window, "label": label, "metrics": metrics, "market": latest}


def next_due_window(outcome) -> str:
    if not outcome.snapshot_1h_json or "market" not in (outcome.snapshot_1h_json or {}):
        return "1h"
    if not outcome.snapshot_4h_json:
        return "4h"
    if not outcome.snapshot_24h_json:
        return "24h"
    return "7d"


def initial_snapshot_from_outcome(outcome) -> dict[str, Any]:
    snap = outcome.snapshot_1h_json or {}
    return snap.get("initial") or snap.get("market") or {}
