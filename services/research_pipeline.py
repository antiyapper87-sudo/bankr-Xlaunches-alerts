from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    Launch,
    complete_token_research,
    fail_token_research,
    get_latest_block_scan,
    get_deployer_history,
    get_launch,
    list_recent_wallet_events_for_ca,
    start_token_research,
    upsert_chain_token_identity,
    upsert_historical_launch,
    utc_now,
)
from services.lore_extraction import extract_and_store_project_lore
from services.project_narrative import extract_project_narrative, narrative_token_type


def fmt_usd_short(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def age_minutes_from_pair_created(pair_created_at: Any) -> float | None:
    if not pair_created_at:
        return None
    try:
        value = float(pair_created_at)
    except (TypeError, ValueError):
        return None
    if value > 10_000_000_000:
        value = value / 1000
    return max(0.0, (datetime.now(timezone.utc).timestamp() - value) / 60)


def classify_token_type(launch: Launch, dex: dict[str, Any] | None) -> str:
    name = (launch.name or "").lower()
    ticker = (launch.ticker or "").lower()
    source = (launch.source or "").lower()
    raw = launch.raw_json or {}
    description = (raw.get("description") or "").lower()
    if "agent" in name or "ai" in name or source == "virtuals":
        return "AI agent / Utility"
    if any(word in name or word in ticker or word in description for word in ("bot", "terminal", "index", "forge", "scanner")):
        return "Utility / Tooling"
    if raw.get("website_url") or raw.get("description"):
        return "Narrative / Community"
    return "Memecoin / Experimental"


def infer_owner_note(launch: Launch) -> str:
    raw = launch.raw_json or {}
    handle = raw.get("x_username") or raw.get("creator_x") or raw.get("deployer_x")
    wallet = raw.get("deployer_wallet") or raw.get("msg_sender")
    if handle:
        return f"Base deployer has X metadata: @{str(handle).lstrip('@')}."
    if wallet:
        return f"Deployer wallet is known ({str(wallet)[:10]}...), but social identity is not resolved."
    return "Owner identity is not resolved yet; treat deployer quality as unknown."


def infer_product_note(launch: Launch, dex: dict[str, Any] | None) -> str:
    dex = dex or {}
    raw = launch.raw_json or {}
    source = (launch.source or "unknown").upper()
    volume = float(dex.get("volume_24h") or 0)
    mcap = float(dex.get("mcap") or 0)
    description = (raw.get("description") or "").strip()
    if description:
        clean = " ".join(description.split())
        return clean[:180] + ("..." if len(clean) > 180 else "")
    if volume >= mcap and volume > 0:
        return f"{source} launch with volume already near or above market cap, so attention is real but may be unstable."
    if "agent" in (launch.name or "").lower():
        return "The name positions it as an agent token; product proof is not confirmed from available data."
    return f"{source} launch with early market traction; differentiation is not proven yet."


def build_market_snapshot(dex: dict[str, Any] | None) -> dict[str, Any]:
    dex = dex or {}
    mcap = float(dex.get("mcap") or 0)
    volume = float(dex.get("volume_24h") or 0)
    liquidity = float(dex.get("liquidity") or 0)
    age_minutes = age_minutes_from_pair_created(dex.get("pair_created_at"))
    return {
        "mcap": mcap,
        "volume_24h": volume,
        "liquidity": liquidity,
        "price_usd": dex.get("price_usd") or "0",
        "price_change_1h": float(dex.get("price_change_1h") or 0),
        "price_change_24h": float(dex.get("price_change_24h") or 0),
        "volume_liquidity_ratio": round(volume / liquidity, 2) if liquidity > 0 else None,
        "mcap_liquidity_ratio": round(mcap / liquidity, 2) if liquidity > 0 else None,
        "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
        "pair_created_at": dex.get("pair_created_at") or 0,
        "pair_url": dex.get("pair_url") or "",
        "pair_address": dex.get("pair_address") or "",
        "dex_id": dex.get("dex_id") or "",
        "quote_token_symbol": dex.get("quote_token_symbol") or "",
        "txns_h1_buys": int(dex.get("txns_h1_buys") or 0),
        "txns_h1_sells": int(dex.get("txns_h1_sells") or 0),
        "txns_h24_buys": int(dex.get("txns_h24_buys") or 0),
        "txns_h24_sells": int(dex.get("txns_h24_sells") or 0),
        "boosts_active": int(dex.get("boosts_active") or 0),
        "source": dex.get("_source") or "",
    }


def build_source_snapshot(launch: Launch) -> dict[str, Any]:
    raw = launch.raw_json or {}
    return {
        "source": launch.source,
        "source_method": raw.get("source_method") or "",
        "status": raw.get("status") or "",
        "deployer_wallet": raw.get("deployer_wallet") or raw.get("msg_sender") or "",
        "x_username": raw.get("x_username") or raw.get("creator_x") or "",
        "tweet_url": raw.get("tweet_url") or "",
        "website_url": raw.get("website_url") or "",
        "pair_url": raw.get("pair_url") or "",
        "description": raw.get("description") or "",
        "image_uri": raw.get("image_uri") or "",
    }


def summarize_social_confirmation(launch: Launch) -> dict[str, Any]:
    raw = launch.raw_json or {}
    evidence = raw.get("social_confirmation") or {}
    social_evidence = evidence.get("social_evidence") or {}
    top_authors = evidence.get("top_authors") or []
    top_tweets = social_evidence.get("top_tweets") or []
    return {
        "ca_verified": bool(evidence.get("verified")),
        "qualified_tweets": int(evidence.get("qualified_tweets") or 0),
        "min_required": int(evidence.get("min_required") or 0),
        "total_followers": int(evidence.get("total_followers") or 0),
        "total_likes": int(evidence.get("total_likes") or 0),
        "total_retweets": int(evidence.get("total_retweets") or 0),
        "max_score": int(evidence.get("max_score") or 0),
        "avg_thesis_quality": float(evidence.get("avg_thesis_quality") or 0),
        "top_authors": top_authors[:5],
        "evidence_thesis": social_evidence.get("thesis") or "",
        "value_assessment": social_evidence.get("value_assessment") or "",
        "social_score": int(social_evidence.get("social_score") or 0),
        "score_breakdown": social_evidence.get("score_breakdown") or {},
        "project_value": social_evidence.get("project_value") or "",
        "project_value_score": int(social_evidence.get("project_value_score") or 0),
        "evidence_count": int(social_evidence.get("evidence_count") or len(top_tweets)),
        "evidence_tweets": top_tweets[:24],
        "source_provenance": social_evidence.get("source_provenance") or {},
        "hermes_agent": social_evidence.get("agent") or {},
    }


def deployer_key_from_source(source_info: dict[str, Any]) -> str:
    return (
        source_info.get("x_username")
        or source_info.get("deployer_wallet")
        or ""
    ).strip()


def summarize_deployer_history(rows: list[Any]) -> dict[str, Any]:
    dead_statuses = {"expired", "skipped", "failed"}
    previous_count = len(rows)
    dead_count = sum(1 for row in rows if (row.final_status or "").lower() in dead_statuses)
    signaled_count = sum(1 for row in rows if (row.final_status or "").lower() == "signaled")
    max_mcap = max((float(row.max_mcap or 0) for row in rows), default=0.0)
    recent = [
        {
            "ca": row.ca,
            "ticker": row.ticker,
            "status": row.final_status,
            "max_mcap": float(row.max_mcap or 0),
        }
        for row in rows[:5]
    ]
    return {
        "previous_launches": previous_count,
        "dead_launches": dead_count,
        "signaled_launches": signaled_count,
        "dead_ratio": round(dead_count / previous_count, 2) if previous_count else 0.0,
        "best_previous_mcap": max_mcap,
        "recent": recent,
    }


def summarize_wallet_events(rows: list[Any]) -> dict[str, Any]:
    inflow = [row for row in rows if (row.direction or "").lower() == "in"]
    outflow = [row for row in rows if (row.direction or "").lower() == "out"]
    inflow_wallets = sorted({row.wallet_address for row in inflow})
    outflow_wallets = sorted({row.wallet_address for row in outflow})
    return {
        "inflow_events": len(inflow),
        "outflow_events": len(outflow),
        "inflow_wallets": len(inflow_wallets),
        "outflow_wallets": len(outflow_wallets),
        "recent_inflow_wallets": inflow_wallets[:5],
        "recent_outflow_wallets": outflow_wallets[:5],
    }


def build_research_flags(launch: Launch, market: dict[str, Any], source_info: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    source = (launch.source or "").lower()
    if source == "dexscreener":
        flags.append("dex_discovery")
    if source_info.get("source_method") == "boosts" or int(market.get("boosts_active") or 0) > 0:
        flags.append("paid_attention")
    if not source_info.get("x_username"):
        flags.append("anonymous_or_unresolved_owner")
    if market.get("age_minutes") is not None and float(market["age_minutes"]) <= 30:
        flags.append("very_fresh_pair")
    if market.get("volume_liquidity_ratio") and float(market["volume_liquidity_ratio"]) >= 3:
        flags.append("volume_above_liquidity")
    if market.get("mcap_liquidity_ratio") and float(market["mcap_liquidity_ratio"]) >= 8:
        flags.append("thin_liquidity_vs_mcap")
    if float(market.get("mcap") or 0) >= 50_000 and float(market.get("volume_24h") or 0) >= 30_000:
        flags.append("passes_primary_market_filters")
    return flags


async def run_research_pipeline(
    db: AsyncSession,
    *,
    ca: str,
    dex: dict[str, Any] | None = None,
    requested_by: str = "pipeline",
) -> dict[str, Any]:
    launch = await get_launch(db, ca)
    if not launch:
        raise ValueError(f"Launch not found: {ca}")

    research, _ = await start_token_research(
        db,
        ca=launch.ca,
        source=launch.source,
        requested_by=requested_by,
    )
    try:
        dex = dex or launch.market_json or {}
        market = build_market_snapshot(dex)
        source_info = build_source_snapshot(launch)
        await upsert_chain_token_identity(
            db,
            chain="base",
            token_id=launch.ca,
            ticker=launch.ticker or "",
            name=launch.name or "",
            launch_source=launch.source,
            source_method=source_info.get("source_method") or "",
            deployer_wallet=source_info.get("deployer_wallet") or "",
            creator_handle=source_info.get("x_username") or "",
            pair_address=(dex or {}).get("pair_address") or "",
            first_seen_at=launch.first_seen_at,
            source_created_at=launch.launched_at,
            reliable_created_at=bool(launch.launched_at),
            source_confidence="medium" if launch.launched_at else "low",
            raw_refs_json={"launch_ca": launch.ca, "source": launch.source},
        )
        deployer_key = deployer_key_from_source(source_info)
        await upsert_historical_launch(db, launch=launch, deployer=deployer_key or None)
        deployer_history_rows = await get_deployer_history(
            db,
            deployer=deployer_key,
            since=utc_now() - timedelta(days=365),
            exclude_ca=launch.ca,
            limit=50,
        ) if deployer_key else []
        deployer_history = summarize_deployer_history(deployer_history_rows)
        wallet_event_rows = await list_recent_wallet_events_for_ca(
            db,
            ca=launch.ca,
            since=utc_now() - timedelta(minutes=60),
            limit=50,
        )
        smart_money = summarize_wallet_events(wallet_event_rows)
        social_confirmation = summarize_social_confirmation(launch)
        flags = build_research_flags(launch, market, source_info)
        if social_confirmation["ca_verified"]:
            flags.append("ca_verified_social")
        if social_confirmation["qualified_tweets"] >= 5:
            flags.append("qualified_social_confirmation")
        if smart_money["inflow_wallets"] >= 1:
            flags.append("tracked_wallet_inflow")
        if smart_money["inflow_wallets"] >= 3:
            flags.append("smart_wallet_convergence")
        project_narrative = extract_project_narrative(
            ca=launch.ca,
            ticker=launch.ticker or "",
            name=launch.name or "",
            launch={**(launch.raw_json or {}), "source": launch.source},
            dex=dex,
            social_evidence={
                "qualified_tweets": social_confirmation.get("qualified_tweets"),
                "thesis": social_confirmation.get("evidence_thesis"),
                "value_assessment": social_confirmation.get("value_assessment"),
                "top_tweets": social_confirmation.get("evidence_tweets") or [],
            },
            flags=flags,
        )
        project_lore = await extract_and_store_project_lore(
            db,
            chain="base",
            token_id=launch.ca,
            ticker=launch.ticker or "",
            name=launch.name or "",
            launch={**(launch.raw_json or {}), "source": launch.source},
            dex=dex,
            social_evidence={
                "qualified_tweets": social_confirmation.get("qualified_tweets"),
                "thesis": social_confirmation.get("evidence_thesis"),
                "value_assessment": social_confirmation.get("value_assessment"),
                "top_tweets": social_confirmation.get("evidence_tweets") or [],
                "primary_tweets": (launch.raw_json or {}).get("social_confirmation", {}).get("social_evidence", {}).get("primary_tweets") or [],
                "ticker_context_tweets": (launch.raw_json or {}).get("social_confirmation", {}).get("social_evidence", {}).get("ticker_context_tweets") or [],
            },
        )
        block_scan = await get_latest_block_scan(db, chain="base", token_id=launch.ca)
        onchain = (
            {
                **(block_scan.summary_json or {}),
                "provider": block_scan.provider,
                "scan_status": block_scan.status,
                "confidence": block_scan.confidence,
                "pair_address": block_scan.pair_address or "",
                "from_block": block_scan.from_block,
                "to_block": block_scan.to_block,
            }
            if block_scan and block_scan.status == "completed"
            else {
                "provider": "stub",
                "scan_status": "pending",
                "wallet_profile": "pending",
                "bundle": {},
                "holder_distribution": "pending",
            }
        )
        processed = {
            "schema": "token-research-v2.1",
            "symbol": (launch.ticker or "").lstrip("$"),
            "name": launch.name or "",
            "source": source_info,
            "project_narrative": project_narrative.to_dict(),
            "project_lore": project_lore,
            "token_type": narrative_token_type(project_narrative, classify_token_type(launch, dex)),
            "owner_note": infer_owner_note(launch),
            "product_note": project_narrative.product or infer_product_note(launch, dex),
            "market": market,
            "social": {
                "x_username": source_info.get("x_username"),
                "tweet_url": source_info.get("tweet_url"),
                "website_url": source_info.get("website_url"),
                **social_confirmation,
            },
            "deployer": {
                "key": deployer_key,
                **deployer_history,
            },
            "onchain": onchain,
            "smart_money": smart_money,
            "flags": flags,
            "brief_inputs": {
                "market_line": (
                    f"MC {fmt_usd_short(market['mcap'])} · "
                    f"Vol {fmt_usd_short(market['volume_24h'])} · "
                    f"Liq {fmt_usd_short(market['liquidity'])}"
                ),
                "risk_line": ", ".join(flags[:4]) or "no deterministic flags",
            },
        }
        raw_data = {
            "launch": launch.raw_json or {},
            "market": dex,
        }
        completed = await complete_token_research(
            db,
            research_id=research.id,
            raw_data=raw_data,
            processed_data=processed,
        )
        return {
            "id": completed.id if completed else research.id,
            "ca": launch.ca,
            "status": "completed",
            "raw_data": raw_data,
            "processed_data": processed,
        }
    except Exception as exc:
        await fail_token_research(db, research_id=research.id, error=str(exc))
        raise
