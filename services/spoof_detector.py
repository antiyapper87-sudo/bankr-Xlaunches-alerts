from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import aiohttp

from sqlalchemy.ext.asyncio import AsyncSession

from database import Launch, get_recent_launches_by_ticker, upsert_spoof_signal, utc_now


MIN_MCAP = int(os.getenv("MIN_MCAP", "50000"))
MIN_VOLUME_24H = int(os.getenv("MIN_VOLUME_24H", "30000"))
MIN_LIQUIDITY = int(os.getenv("MIN_LIQUIDITY", "30000"))
MAX_TOKEN_AGE = int(os.getenv("MAX_TOKEN_AGE", str(4 * 3600)))
SAFE_LAUNCHPADS = {"bankr", "clanker", "virtuals"}
SAME_TICKER_LOOKBACK = timedelta(seconds=MAX_TOKEN_AGE)
SAME_TICKER_PRIOR_LOOKBACK = timedelta(hours=int(os.getenv("SAME_TICKER_PRIOR_LOOKBACK_HOURS", "48")))
SAME_TICKER_EXTERNAL_ENABLED = os.getenv("SAME_TICKER_EXTERNAL_ENABLED", "true").lower() == "true"
SAME_TICKER_EXTERNAL_TIMEOUT_SEC = int(os.getenv("SAME_TICKER_EXTERNAL_TIMEOUT_SEC", "8"))
GECKOTERMINAL_API_URL = "https://api.geckoterminal.com/api/v2"


def severity_from_impact(impact: float) -> str:
    if impact >= 18:
        return "high"
    if impact >= 9:
        return "medium"
    return "low"


def age_seconds_from_pair_created(pair_created_at: Any) -> float | None:
    if not pair_created_at:
        return None
    if isinstance(pair_created_at, str) and not pair_created_at.isdigit():
        try:
            dt = datetime.fromisoformat(pair_created_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, utc_now().timestamp() - dt.timestamp())
        except ValueError:
            return None
    try:
        value = float(pair_created_at)
        if value > 10_000_000_000:
            value = value / 1000
        return max(0.0, utc_now().timestamp() - value)
    except (TypeError, ValueError):
        return None


def age_seconds_from_launch(launch: Launch, market: dict[str, Any]) -> float | None:
    pair_created_at = market.get("pair_created_at") if market else 0
    age_seconds = age_seconds_from_pair_created(pair_created_at)
    if age_seconds is not None:
        return age_seconds

    started_at = launch.launched_at or launch.first_seen_at
    if not started_at:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=utc_now().tzinfo)
    return max(0.0, (utc_now() - started_at).total_seconds())


def passes_market_thresholds(market: dict[str, Any], source: str = "dexscreener") -> bool:
    mcap = float(market.get("mcap") or 0)
    volume = float(market.get("volume_24h") or 0)
    liquidity = float(market.get("liquidity") or 0)
    source = (source or "").lower()
    if mcap < MIN_MCAP or volume < MIN_VOLUME_24H:
        return False
    if source not in SAFE_LAUNCHPADS and liquidity < MIN_LIQUIDITY:
        return False
    return True


def passes_same_ticker_filters(
    launch: Launch,
    *,
    min_age_seconds: float = 0,
    max_age_seconds: float = MAX_TOKEN_AGE,
) -> bool:
    market = launch.market_json or {}
    age_seconds = age_seconds_from_launch(launch, market)
    if age_seconds is None or age_seconds < min_age_seconds or age_seconds > max_age_seconds:
        return False
    return passes_market_thresholds(market, launch.source or "")


def passes_same_ticker_candidate_filters(
    candidate: dict[str, Any],
    *,
    min_age_seconds: float = 0,
    max_age_seconds: float = MAX_TOKEN_AGE,
) -> bool:
    age_seconds = candidate.get("age_seconds")
    if age_seconds is None or float(age_seconds) < min_age_seconds or float(age_seconds) > max_age_seconds:
        return False
    return passes_market_thresholds(candidate, candidate.get("source") or "dexscreener")


def format_same_ticker_candidate(launch: Launch) -> dict[str, Any]:
    market = launch.market_json or {}
    age_seconds = age_seconds_from_launch(launch, market)
    return {
        "ca": launch.ca,
        "source": launch.source,
        "name": launch.name or "",
        "ticker": launch.ticker or "",
        "age_minutes": round((age_seconds or 0) / 60, 1),
        "age_seconds": round(age_seconds or 0, 1),
        "mcap": float(market.get("mcap") or 0),
        "volume_24h": float(market.get("volume_24h") or 0),
        "liquidity": float(market.get("liquidity") or 0),
    }


def format_age_short(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "unknown age"
    if age_seconds < 3600:
        return f"{int(age_seconds / 60)}m"
    if age_seconds < 86400:
        return f"{age_seconds / 3600:.1f}h"
    return f"{age_seconds / 86400:.1f}d"


async def find_same_ticker_passed_launches(
    db: AsyncSession,
    *,
    ca: str,
    ticker: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    clean_ticker = (ticker or "").strip().lstrip("$").upper()
    if not clean_ticker:
        return []

    recent = await get_recent_launches_by_ticker(
        db,
        ticker=clean_ticker,
        since=utc_now() - SAME_TICKER_LOOKBACK,
        limit=50,
    )
    current_ca = ca.lower()
    candidates = [
        launch
        for launch in recent
        if launch.ca.lower() != current_ca and passes_same_ticker_filters(launch)
    ]
    candidates.sort(
        key=lambda launch: float((launch.market_json or {}).get("mcap") or 0),
        reverse=True,
    )
    return [format_same_ticker_candidate(launch) for launch in candidates[:limit]]


async def find_same_ticker_prior_passed_launches(
    db: AsyncSession,
    *,
    ca: str,
    ticker: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    clean_ticker = (ticker or "").strip().lstrip("$").upper()
    if not clean_ticker:
        return []

    recent = await get_recent_launches_by_ticker(
        db,
        ticker=clean_ticker,
        since=utc_now() - SAME_TICKER_PRIOR_LOOKBACK,
        limit=100,
    )
    current_ca = ca.lower()
    max_age = SAME_TICKER_PRIOR_LOOKBACK.total_seconds()
    candidates = [
        launch
        for launch in recent
        if launch.ca.lower() != current_ca
        and passes_same_ticker_filters(
            launch,
            min_age_seconds=MAX_TOKEN_AGE,
            max_age_seconds=max_age,
        )
    ]
    candidates.sort(
        key=lambda launch: float((launch.market_json or {}).get("mcap") or 0),
        reverse=True,
    )
    return [format_same_ticker_candidate(launch) for launch in candidates[:limit]]


def parse_geckoterminal_same_ticker_candidates(
    data: dict[str, Any],
    *,
    ticker: str,
    current_ca: str,
) -> list[dict[str, Any]]:
    clean_ticker = (ticker or "").strip().lstrip("$").upper()
    current_ca = (current_ca or "").lower()
    if not clean_ticker:
        return []

    candidates: list[dict[str, Any]] = []
    for pool in data.get("data", []) or []:
        attrs = pool.get("attributes") or {}
        relationships = pool.get("relationships") or {}
        base_token_id = ((relationships.get("base_token") or {}).get("data") or {}).get("id") or ""
        base_ca = str(base_token_id).replace("base_", "").lower()
        if not base_ca or base_ca == current_ca:
            continue

        pool_name = str(attrs.get("name") or "")
        base_symbol = pool_name.split(" / ")[0].strip().upper() if " / " in pool_name else ""
        if base_symbol != clean_ticker:
            continue

        age_seconds = age_seconds_from_pair_created(attrs.get("pool_created_at"))
        volume = attrs.get("volume_usd") or {}
        price_change = attrs.get("price_change_percentage") or {}
        transactions = attrs.get("transactions") or {}
        h1_txns = transactions.get("h1") or {}
        h24_txns = transactions.get("h24") or {}
        pool_address = attrs.get("address") or ""
        candidate = {
            "ca": base_ca,
            "source": "geckoterminal",
            "dex_id": (((relationships.get("dex") or {}).get("data") or {}).get("id") or ""),
            "name": base_symbol.title(),
            "ticker": clean_ticker,
            "age_minutes": round((age_seconds or 0) / 60, 1),
            "age_seconds": round(age_seconds or 0, 1) if age_seconds is not None else None,
            "mcap": float(attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0),
            "volume_24h": float(volume.get("h24") or 0),
            "liquidity": float(attrs.get("reserve_in_usd") or 0),
            "price_change_1h": float(price_change.get("h1") or 0),
            "txns_h1_buys": int(h1_txns.get("buys") or 0),
            "txns_h1_sells": int(h1_txns.get("sells") or 0),
            "txns_h24_buys": int(h24_txns.get("buys") or 0),
            "txns_h24_sells": int(h24_txns.get("sells") or 0),
            "pool_address": pool_address,
            "pair_url": f"https://www.geckoterminal.com/base/pools/{pool_address}" if pool_address else "",
        }
        candidates.append(candidate)
    return candidates


async def fetch_external_same_ticker_candidates(
    *,
    ticker: str,
    current_ca: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    clean_ticker = (ticker or "").strip().lstrip("$").upper()
    if not clean_ticker or not SAME_TICKER_EXTERNAL_ENABLED:
        return []
    url = f"{GECKOTERMINAL_API_URL}/search/pools?query={quote(clean_ticker)}&network=base&page=1"
    headers = {"Accept": "application/json;version=20230302"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=SAME_TICKER_EXTERNAL_TIMEOUT_SEC)) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
    except Exception:
        return []
    candidates = parse_geckoterminal_same_ticker_candidates(data, ticker=clean_ticker, current_ca=current_ca)
    candidates.sort(key=lambda item: float(item.get("mcap") or 0), reverse=True)
    return candidates[:limit]


def merge_same_ticker_candidates(*groups: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            key = (item.get("ca") or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    merged.sort(key=lambda item: float(item.get("mcap") or 0), reverse=True)
    return merged[:limit]


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

    same_ticker_matches = await find_same_ticker_passed_launches(
        db,
        ca=ca,
        ticker=ticker,
        limit=25,
    )
    if same_ticker_matches:
        match_count = len(same_ticker_matches)
        pair_word = "pair" if match_count == 1 else "pairs"
        clean_ticker = (ticker or "").strip().lstrip("$").upper()
        signals.append(
            {
                "signal_type": "same_ticker_fresh_passed_filters",
                "score_impact": min(22.0, 10.0 + match_count * 4.0),
                "title": f"ticker collision: {match_count} other fresh ${clean_ticker} {pair_word} passed filters",
                "details": (
                    f"{match_count} other fresh Base {pair_word} with ticker ${clean_ticker} "
                    "also passed the scanner filters. This can be organic reuse, but it can also indicate spoofing around an anticipated launch."
                ),
                "evidence_json": {
                    "ticker": clean_ticker,
                    "lookback_seconds": MAX_TOKEN_AGE,
                    "matched_count": match_count,
                    "matches": same_ticker_matches[:3],
                },
            }
        )

    prior_local = await find_same_ticker_prior_passed_launches(
        db,
        ca=ca,
        ticker=ticker,
        limit=25,
    )
    prior_external = await fetch_external_same_ticker_candidates(
        ticker=ticker,
        current_ca=ca,
        limit=25,
    )
    max_prior_age = SAME_TICKER_PRIOR_LOOKBACK.total_seconds()
    prior_external = [
        item
        for item in prior_external
        if passes_same_ticker_candidate_filters(
            item,
            min_age_seconds=MAX_TOKEN_AGE,
            max_age_seconds=max_prior_age,
        )
    ]
    prior_matches = merge_same_ticker_candidates(prior_local, prior_external, limit=5)
    if prior_matches:
        match_count = len(prior_matches)
        pair_word = "market" if match_count == 1 else "markets"
        clean_ticker = (ticker or "").strip().lstrip("$").upper()
        top = prior_matches[0]
        top_age = format_age_short(float(top.get("age_seconds") or 0))
        signals.append(
            {
                "signal_type": "same_ticker_prior_passed_filters",
                "score_impact": min(24.0, 14.0 + match_count * 4.0),
                "title": f"prior ${clean_ticker} {pair_word} passed filters ({top_age} old)",
                "details": (
                    f"{match_count} older Base {pair_word} with ticker ${clean_ticker} "
                    "passed the scanner filters before this launch. Treat the new pair as weak until it proves independent demand."
                ),
                "evidence_json": {
                    "ticker": clean_ticker,
                    "lookback_seconds": int(max_prior_age),
                    "matched_count": match_count,
                    "matches": prior_matches[:3],
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
