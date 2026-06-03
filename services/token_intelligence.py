from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database import audit_event, get_launch
from services.ai_summary import get_or_create_ai_summary
from services.research_pipeline import run_research_pipeline
from services.spoof_detector import detect_spoof_signals
from services.verdict_v2 import build_verdict_v2


async def analyze_token_intelligence(
    db: AsyncSession,
    *,
    ca: str,
    dex: dict[str, Any] | None = None,
    requested_by: str = "pipeline",
    include_summary: bool = True,
    language: str = "en",
) -> dict[str, Any]:
    launch = await get_launch(db, ca)
    if not launch:
        raise ValueError(f"Launch not found: {ca}")

    research = await run_research_pipeline(
        db,
        ca=launch.ca,
        dex=dex,
        requested_by=requested_by,
    )
    spoof = await detect_spoof_signals(
        db,
        ca=launch.ca,
        ticker=launch.ticker or "",
        dex=dex or launch.market_json,
        research_data=research.get("processed_data"),
    )
    verdict = await build_verdict_v2(
        db,
        ca=launch.ca,
        launch=launch.raw_json or {},
        dex=dex or launch.market_json,
    )
    summary = None
    if include_summary:
        summary = await get_or_create_ai_summary(
            db,
            ca=launch.ca,
            verdict=verdict,
            language=language,
        )
    await audit_event(
        db,
        event_type="token_intelligence_completed",
        payload={
            "ca": launch.ca,
            "requested_by": requested_by,
            "verdict_id": verdict.get("id"),
            "score": verdict.get("score"),
            "label": verdict.get("label"),
            "spoof_signals": len(spoof),
        },
    )
    return {
        "launch": {
            "ca": launch.ca,
            "symbol": launch.ticker,
            "name": launch.name,
            "source": launch.source,
        },
        "research": research,
        "spoof_signals": spoof,
        "verdict": verdict,
        "summary": summary,
    }
