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
    reasons = verdict.get("reasons") or []
    risks = verdict.get("risks") or []
    reason = reasons[0] if reasons else "evidence is limited"
    risk = risks[0] if risks else "no major deterministic risk was detected"
    if language == "ru":
        return (
            f"${symbol}: {label} ({score:.0f}/100). "
            f"Главный плюс: {reason}. "
            f"Главный риск: {risk}. "
            "AI-модель пока не подключена; это честная deterministic summary по собранным данным."
        )
    return (
        f"${symbol}: {label} ({score:.0f}/100). "
        f"Main positive: {reason}. "
        f"Main risk: {risk}. "
        "The AI model is not connected yet; this is a deterministic summary from collected evidence."
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
