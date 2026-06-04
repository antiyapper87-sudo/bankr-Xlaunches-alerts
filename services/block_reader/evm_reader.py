from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    insert_holder_snapshot,
    list_bundle_signals,
    upsert_block_scan,
    upsert_bundle_signal,
    upsert_prebuy_signal,
    upsert_token_transaction,
)
from services.block_reader.bundle_detector import detect_bundle_like_patterns, severity_for
from services.chains.evm import BaseEvmAdapter


PAIR_WINDOW_BEFORE_BLOCKS = 20
PAIR_WINDOW_AFTER_BLOCKS = 120
MAX_TX_TO_PARSE = 300


def estimate_pair_block_from_age(latest_block: int, pair_created_at: int | None) -> int | None:
    if not latest_block or not pair_created_at:
        return None
    age_seconds = max(0, datetime.now(timezone.utc).timestamp() - (pair_created_at / 1000 if pair_created_at > 10_000_000_000 else pair_created_at))
    return max(0, latest_block - int(age_seconds / 2.0))


async def scan_base_token_blocks(
    db: AsyncSession,
    session: aiohttp.ClientSession,
    *,
    rpc_url: str,
    token_id: str,
    dex: dict[str, Any] | None = None,
    launch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dex = dex or {}
    launch = launch or {}
    adapter = BaseEvmAdapter(session, rpc_url)
    latest = await adapter.get_latest_block()
    if latest is None:
        await upsert_block_scan(db, chain="base", token_id=token_id, status="pending", confidence="LOW", summary_json={"reason": "rpc unavailable"})
        return {"provider": "alchemy", "confidence": "LOW", "signals": [], "bundle_risk": 0, "prebuy_risk": 0, "status": "pending"}

    pair_block = dex.get("pair_block") or dex.get("created_block")
    if pair_block is None:
        pair_block = estimate_pair_block_from_age(latest, dex.get("pair_created_at"))
    if pair_block is None:
        await upsert_block_scan(db, chain="base", token_id=token_id, status="pending", confidence="LOW", summary_json={"reason": "pair block missing", "latest_block": latest})
        return {"provider": "alchemy", "confidence": "LOW", "signals": [], "bundle_risk": 0, "prebuy_risk": 0, "status": "pending"}

    from_block = max(0, int(pair_block) - PAIR_WINDOW_BEFORE_BLOCKS)
    to_block = min(latest, int(pair_block) + PAIR_WINDOW_AFTER_BLOCKS)
    transfers = (await adapter.get_token_transfers(token_id, from_block, to_block))[:MAX_TX_TO_PARSE]
    for tx in transfers:
        await upsert_token_transaction(
            db,
            chain="base",
            token_id=token_id,
            tx_hash=tx.tx_hash,
            event_type=tx.event_type,
            wallet_address=tx.wallet_address or "",
            counterparty_address=tx.from_address or "",
            pair_address=tx.pair_address or dex.get("pair_address") or "",
            block_number=tx.block_number,
            tx_index=tx.tx_index,
            amount_token=tx.amount_token,
            amount_native=tx.amount_native,
            raw_json=tx.raw,
        )

    summary = detect_bundle_like_patterns(transfers, dev_wallet=launch.get("deployer_wallet") or launch.get("msg_sender") or "")
    for signal in summary.signals:
        await upsert_bundle_signal(
            db,
            chain="base",
            token_id=token_id,
            signal_type=signal["type"],
            severity=signal.get("severity") or severity_for(float(signal.get("risk_score") or 0)),
            risk_score=float(signal.get("risk_score") or 0),
            score_impact=-min(15, float(signal.get("risk_score") or 0) / 4),
            title=signal.get("title") or signal["type"],
            details=signal.get("details") or "",
            evidence_json={"from_block": from_block, "to_block": to_block, "sample_size": len(transfers)},
        )
    if summary.prebuy_risk:
        await upsert_prebuy_signal(db, chain="base", token_id=token_id, severity=severity_for(summary.prebuy_risk), risk_score=summary.prebuy_risk, evidence_json=summary.to_dict())
    await insert_holder_snapshot(
        db,
        chain="base",
        token_id=token_id,
        provider="alchemy-transfer-sample",
        holder_count=len({tx.to_address for tx in transfers if tx.to_address}) or None,
        snapshot_json={"sampled_transfer_count": len(transfers), "window": [from_block, to_block]},
    )
    await upsert_block_scan(
        db,
        chain="base",
        token_id=token_id,
        pair_address=dex.get("pair_address") or "",
        from_block=from_block,
        to_block=to_block,
        confidence=summary.confidence,
        status="completed",
        provider="alchemy",
        summary_json=summary.to_dict(),
    )
    existing = await list_bundle_signals(db, chain="base", token_id=token_id)
    result = summary.to_dict()
    result["provider"] = "alchemy"
    result["status"] = "completed"
    result["persisted_signals"] = len(existing)
    return result
