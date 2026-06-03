from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    Launch,
    complete_token_research,
    fail_token_research,
    get_launch,
    start_token_research,
    upsert_historical_launch,
)


def classify_token_type(launch: Launch, dex: dict[str, Any] | None) -> str:
    name = (launch.name or "").lower()
    ticker = (launch.ticker or "").lower()
    source = (launch.source or "").lower()
    if "agent" in name or "ai" in name or source == "virtuals":
        return "AI agent / Utility"
    if any(word in name or word in ticker for word in ("bot", "terminal", "index", "forge")):
        return "Utility / Tooling"
    return "Memecoin / Experimental"


def infer_owner_note(launch: Launch) -> str:
    raw = launch.raw_json or {}
    handle = raw.get("x_username") or raw.get("creator_x") or raw.get("deployer_x")
    if handle:
        return f"Base deployer has X metadata: @{str(handle).lstrip('@')}."
    return "Owner identity is not resolved yet; treat deployer quality as unknown."


def infer_product_note(launch: Launch, dex: dict[str, Any] | None) -> str:
    dex = dex or {}
    source = (launch.source or "unknown").upper()
    volume = float(dex.get("volume_24h") or 0)
    mcap = float(dex.get("mcap") or 0)
    if volume >= mcap and volume > 0:
        return f"{source} launch with volume already near or above market cap, so attention is real but may be unstable."
    if "agent" in (launch.name or "").lower():
        return "The name positions it as an agent token; product proof is not confirmed from available data."
    return f"{source} launch with early market traction; differentiation is not proven yet."


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
        await upsert_historical_launch(db, launch=launch)
        processed = {
            "token_type": classify_token_type(launch, dex),
            "owner_note": infer_owner_note(launch),
            "product_note": infer_product_note(launch, dex),
            "market": {
                "mcap": float(dex.get("mcap") or 0),
                "volume_24h": float(dex.get("volume_24h") or 0),
                "liquidity": float(dex.get("liquidity") or 0),
                "price_change_1h": float(dex.get("price_change_1h") or 0),
            },
            "social": {
                "x_username": (launch.raw_json or {}).get("x_username") or (launch.raw_json or {}).get("creator_x"),
                "tweet_url": (launch.raw_json or {}).get("tweet_url"),
            },
            "onchain": {
                "provider": "stub",
                "wallet_profile": "pending",
                "bundle": {},
                "holder_distribution": "pending",
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
