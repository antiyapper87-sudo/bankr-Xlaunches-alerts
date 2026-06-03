from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database import get_recent_launches_by_ticker, get_ticker_history, upsert_spoof_signal, utc_now


def severity_from_impact(impact: float) -> str:
    if impact >= 18:
        return "high"
    if impact >= 9:
        return "medium"
    return "low"


async def detect_spoof_signals(
    db: AsyncSession,
    *,
    ca: str,
    ticker: str,
    dex: dict[str, Any] | None,
    research_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    dex = dex or {}
    research_data = research_data or {}
    signals: list[dict[str, Any]] = []

    mcap = float(dex.get("mcap") or 0)
    volume = float(dex.get("volume_24h") or 0)
    liquidity = float(dex.get("liquidity") or 0)
    change_1h = float(dex.get("price_change_1h") or 0)

    if liquidity > 0 and volume / liquidity >= 3:
        signals.append(
            {
                "signal_type": "fake_volume_risk",
                "score_impact": 14.0,
                "title": "Volume is high relative to liquidity",
                "details": "24h volume is more than 3x liquidity; this can be organic on fresh launches, but it is a common fake-volume pattern.",
                "evidence_json": {"volume_24h": volume, "liquidity": liquidity, "ratio": round(volume / liquidity, 2)},
            }
        )

    if liquidity > 0 and mcap / liquidity >= 8:
        signals.append(
            {
                "signal_type": "thin_liquidity_mcap_risk",
                "score_impact": 9.0,
                "title": "Market cap is stretched versus liquidity",
                "details": "The token can move sharply because liquidity is thin relative to market cap.",
                "evidence_json": {"mcap": mcap, "liquidity": liquidity, "ratio": round(mcap / liquidity, 2)},
            }
        )

    if liquidity > 0 and abs(change_1h) >= 80 and volume < 75_000:
        signals.append(
            {
                "signal_type": "momentum_without_depth",
                "score_impact": 8.0,
                "title": "Sharp 1h move without deep volume",
                "details": "Large short-term move with limited volume/depth can reverse quickly.",
                "evidence_json": {"price_change_1h": change_1h, "volume_24h": volume, "liquidity": liquidity},
            }
        )

    history = await get_ticker_history(
        db,
        ticker=ticker,
        since=utc_now() - timedelta(days=60),
        limit=25,
    )
    recent_launches = await get_recent_launches_by_ticker(
        db,
        ticker=ticker,
        since=utc_now() - timedelta(days=60),
        limit=25,
    )
    prior_contracts = {row.ca for row in history if row.ca != ca.lower()}
    prior_contracts.update(row.ca for row in recent_launches if row.ca != ca.lower())
    if len(prior_contracts) >= 2:
        signals.append(
            {
                "signal_type": "ticker_reuse",
                "score_impact": min(22.0, 8.0 + len(prior_contracts) * 3.0),
                "title": "Ticker has recent reuse history",
                "details": f"Ticker appeared {len(prior_contracts)} other times in the last 60 days.",
                "evidence_json": {
                    "ticker": ticker,
                    "prior_count_60d": len(prior_contracts),
                    "prior_contracts": list(sorted(prior_contracts))[:10],
                },
            }
        )

    bundle = research_data.get("bundle") or {}
    if int(bundle.get("small_related_wallet_count") or 0) >= 5:
        count = int(bundle.get("small_related_wallet_count") or 0)
        signals.append(
            {
                "signal_type": "bundle_spoof_stub",
                "score_impact": 18.0,
                "title": "Possible bundled early wallet cluster",
                "details": "Bundle analysis is currently heuristic/stubbed; this flags clustered early wallets once data is supplied.",
                "evidence_json": {"small_related_wallet_count": count},
            }
        )

    persisted: list[dict[str, Any]] = []
    for signal in signals:
        impact = float(signal["score_impact"])
        row = await upsert_spoof_signal(
            db,
            ca=ca,
            signal_type=signal["signal_type"],
            severity=severity_from_impact(impact),
            score_impact=impact,
            title=signal["title"],
            details=signal["details"],
            evidence_json=signal["evidence_json"],
        )
        persisted.append(
            {
                "id": row.id,
                "type": row.signal_type,
                "severity": row.severity,
                "score_impact": row.score_impact,
                "title": row.title,
                "details": row.details,
                "evidence": row.evidence_json,
            }
        )
    return persisted
