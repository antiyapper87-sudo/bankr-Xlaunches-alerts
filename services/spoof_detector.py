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
    txns_h1_buys = int(dex.get("txns_h1_buys") or 0)
    txns_h1_sells = int(dex.get("txns_h1_sells") or 0)
    txns_h24_buys = int(dex.get("txns_h24_buys") or 0)
    txns_h24_sells = int(dex.get("txns_h24_sells") or 0)
    pair_created_at = dex.get("pair_created_at") or 0
    market = research_data.get("market") or {}
    source_info = research_data.get("source") or {}
    flags = set(research_data.get("flags") or [])
    age_minutes = market.get("age_minutes")

    if (mcap > 0 or volume > 0) and liquidity <= 0:
        signals.append(
            {
                "signal_type": "missing_or_zero_liquidity",
                "score_impact": 16.0,
                "title": "Market data has cap/volume but no usable liquidity",
                "details": "This can be an indexing gap, but it is unsafe for DEX-discovered tokens until liquidity is confirmed.",
                "evidence_json": {"mcap": mcap, "volume_24h": volume, "liquidity": liquidity},
            }
        )

    if liquidity > 0 and volume / liquidity >= 3:
        signals.append(
            {
                "signal_type": "fake_volume_risk",
                "score_impact": 18.0 if volume / liquidity >= 8 else 14.0,
                "title": "Volume is high relative to liquidity",
                "details": "24h volume is more than 3x liquidity; this can be organic on fresh launches, but it is a common fake-volume pattern.",
                "evidence_json": {"volume_24h": volume, "liquidity": liquidity, "ratio": round(volume / liquidity, 2)},
            }
        )

    if liquidity > 0 and mcap / liquidity >= 8:
        signals.append(
            {
                "signal_type": "thin_liquidity_mcap_risk",
                "score_impact": 14.0 if mcap / liquidity >= 15 else 9.0,
                "title": "Market cap is stretched versus liquidity",
                "details": "The token can move sharply because liquidity is thin relative to market cap.",
                "evidence_json": {"mcap": mcap, "liquidity": liquidity, "ratio": round(mcap / liquidity, 2)},
            }
        )

    if not pair_created_at and (mcap >= 75_000 or volume >= 50_000):
        signals.append(
            {
                "signal_type": "missing_pair_age",
                "score_impact": 7.0,
                "title": "Pair age is missing on a non-trivial market",
                "details": "Without pairCreatedAt the scanner cannot reliably prove this is a fresh launch.",
                "evidence_json": {"mcap": mcap, "volume_24h": volume},
            }
        )

    if age_minutes is not None and float(age_minutes) <= 20 and volume >= 75_000 and liquidity < 50_000:
        signals.append(
            {
                "signal_type": "instant_volume_thin_depth",
                "score_impact": 13.0,
                "title": "Very fresh pair has fast volume but thin depth",
                "details": "Large early volume inside the first 20 minutes with limited liquidity is often unstable.",
                "evidence_json": {"age_minutes": age_minutes, "volume_24h": volume, "liquidity": liquidity},
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

    total_h1 = txns_h1_buys + txns_h1_sells
    if total_h1 >= 20:
        buy_share = txns_h1_buys / total_h1
        if buy_share >= 0.88 or buy_share <= 0.12:
            signals.append(
                {
                    "signal_type": "one_sided_h1_flow",
                    "score_impact": 9.0,
                    "title": "1h transaction flow is unusually one-sided",
                    "details": "Extreme buy/sell imbalance can be organic, but it is a useful early spoof/wash-trading flag.",
                    "evidence_json": {
                        "txns_h1_buys": txns_h1_buys,
                        "txns_h1_sells": txns_h1_sells,
                        "buy_share": round(buy_share, 2),
                    },
                }
            )

    total_h24 = txns_h24_buys + txns_h24_sells
    if total_h24 >= 40 and liquidity > 0 and volume / max(total_h24, 1) < 25 and volume / liquidity >= 2:
        signals.append(
            {
                "signal_type": "many_low_value_trades",
                "score_impact": 8.0,
                "title": "Many low-value trades versus liquidity",
                "details": "A high transaction count with low average volume and elevated volume/liquidity ratio can indicate wash-like activity.",
                "evidence_json": {
                    "txns_h24": total_h24,
                    "avg_volume_per_txn": round(volume / max(total_h24, 1), 2),
                    "volume_liquidity_ratio": round(volume / liquidity, 2),
                },
            }
        )

    if source_info.get("source") == "dexscreener" and (
        source_info.get("source_method") == "boosts" or "paid_attention" in flags
    ) and liquidity < 75_000:
        signals.append(
            {
                "signal_type": "paid_attention_thin_liquidity",
                "score_impact": 10.0,
                "title": "Paid DexScreener attention with limited liquidity",
                "details": "Boost/profile discovery is useful, but paid attention before strong liquidity should be treated cautiously.",
                "evidence_json": {
                    "source_method": source_info.get("source_method"),
                    "liquidity": liquidity,
                    "boosts_active": dex.get("boosts_active") or 0,
                },
            }
        )

    if not source_info.get("x_username") and source_info.get("source") in {"dexscreener", "bankr", "clanker"}:
        signals.append(
            {
                "signal_type": "unresolved_owner_identity",
                "score_impact": 5.0,
                "title": "Owner/social identity is unresolved",
                "details": "No X identity was resolved from launch metadata; this is not fatal, but it lowers confidence.",
                "evidence_json": {"source": source_info.get("source"), "deployer_wallet": source_info.get("deployer_wallet")},
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
