from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database import get_cached_ai_summary, upsert_ai_summary, utc_now


def build_stub_summary(verdict: dict[str, Any], *, language: str) -> str:
    token = verdict.get("token") or {}
    symbol = token.get("symbol") or token.get("ca") or "token"
    label = verdict.get("label", "WAIT")
    score = float(verdict.get("score") or 0)
    research = verdict.get("research") or {}
    token_type = research.get("token_type") or "Unknown"
    market = research.get("market") or {}
    social = research.get("social") or {}
    project_narrative = research.get("project_narrative") or {}
    reasons = verdict.get("reasons") or []
    risks = verdict.get("risks") or []
    product = project_narrative.get("product") or research.get("product_note") or "Project description is not confirmed."
    lore = project_narrative.get("key_lore_context") or ""
    thesis = social.get("evidence_thesis") or product
    value = (
        project_narrative.get("why_it_matters")
        or social.get("value_assessment")
        or (reasons[0] if reasons else "No strong social value case is confirmed yet")
    )
    risk = risks[0] if risks else "no major deterministic risk was detected"
    market_line = (
        f"MC ${float(market.get('mcap') or 0):,.0f}, "
        f"Vol ${float(market.get('volume_24h') or 0):,.0f}, "
        f"Liq ${float(market.get('liquidity') or 0):,.0f}"
    )
    if language == "ru":
        return (
            f"${symbol}: {label}, {score / 10:.1f}/10. "
            f"Тип: {token_type}. Проект: {product}. Рынок: {market_line}. "
            f"Тезис: {thesis} Оценка ценности: {value}. Главный риск: {risk}. "
            + (f"Контекст: {lore}. " if lore else "")
            + "Это deterministic AI-brief stub без внешней модели."
        )
    return (
        f"${symbol}: {label}, {score / 10:.1f}/10. "
        f"Type: {token_type}. Project: {product}. Market: {market_line}. "
        f"Thesis: {thesis} Value read: {value}. Main risk: {risk}. "
        + (f"Context: {lore}. " if lore else "")
        + "This is a deterministic AI-brief stub without an external model."
    )


async def get_or_create_ai_summary(
    db: AsyncSession,
    *,
    ca: str,
    verdict: dict[str, Any],
    language: str = "en",
) -> dict[str, Any]:
    cached = await get_cached_ai_summary(db, ca=ca, language=language)
    if cached:
        return {
            "id": cached.id,
            "language": cached.language,
            "provider": cached.provider,
            "model": cached.model,
            "summary_text": cached.summary_text,
            "cached": True,
        }
    summary_text = build_stub_summary(verdict, language=language)
    row = await upsert_ai_summary(
        db,
        ca=ca,
        language=language,
        summary_text=summary_text,
        summary_json={"source": "deterministic_stub", "verdict_id": verdict.get("id")},
        verdict_v2_id=verdict.get("id"),
        expires_at=utc_now() + timedelta(minutes=30),
    )
    return {
        "id": row.id,
        "language": row.language,
        "provider": row.provider,
        "model": row.model,
        "summary_text": row.summary_text,
        "cached": False,
    }
