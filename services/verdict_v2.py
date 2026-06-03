from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database import create_verdict_v2, get_latest_token_research, list_spoof_signals


WEIGHTS = {
    "bundle_strength": 15,
    "dev_wallet_behavior": 15,
    "funding_quality": 10,
    "liquidity_health": 20,
    "community_signals": 20,
    "spoof_risk": 20,
}


def label_for_score(score: float) -> str:
    if score >= 72:
        return "WATCH"
    if score >= 50:
        return "WAIT"
    return "SKIP"


def score_liquidity(market: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    mcap = float(market.get("mcap") or 0)
    volume = float(market.get("volume_24h") or 0)
    liquidity = float(market.get("liquidity") or 0)
    reasons: list[str] = []
    risks: list[str] = []
    score = 0.0
    if mcap >= 50_000:
        score += 7
        reasons.append("mcap passed launch filter")
    else:
        risks.append("mcap below preferred range")
    if volume >= 30_000:
        score += 7
        reasons.append("volume confirms early attention")
    else:
        risks.append("volume is not confirmed")
    if liquidity >= 30_000:
        score += 6
        reasons.append("liquidity is usable for early stage")
    elif liquidity > 0:
        score += 2
        risks.append("liquidity is thin")
    return min(20, score), reasons, risks


def score_community(research: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    social = research.get("social") or {}
    reasons: list[str] = []
    risks: list[str] = []
    if social.get("x_username"):
        return 12.0, [f"creator metadata links to @{social['x_username']}"], []
    risks.append("no creator X metadata available")
    return 4.0, [], risks


def score_spoof(spoof_signals: list[dict[str, Any]]) -> tuple[float, list[str], list[str]]:
    impact = sum(float(signal.get("score_impact") or 0) for signal in spoof_signals)
    score = max(0.0, 20.0 - impact)
    risks = [signal.get("title", "spoof risk") for signal in spoof_signals[:4]]
    reasons = ["no major spoof signal detected"] if not risks else []
    return score, reasons, risks


def build_human_readable(
    *,
    symbol: str,
    token_name: str,
    label: str,
    score: float,
    categories: dict[str, float],
    reasons: list[str],
    risks: list[str],
    research: dict[str, Any],
) -> str:
    token_type = research.get("token_type") or "Unknown"
    owner_note = research.get("owner_note") or "Owner identity is unresolved."
    product_note = research.get("product_note") or "No product differentiation confirmed yet."
    reason_text = "; ".join(reasons[:3]) or "limited positive evidence"
    risk_text = "; ".join(risks[:3]) or "no major deterministic risk detected"
    return (
        f"🧠 <b>AI brief</b> • <b>{label}</b> ({score:.0f}/100)\n\n"
        f"• <b>Type:</b> {token_type}\n"
        f"• <b>Owner:</b> {owner_note}\n"
        f"• <b>Why it matters:</b> {product_note}\n"
        f"• <b>Positive:</b> {reason_text}\n"
        f"• <b>Risk:</b> {risk_text}\n"
        f"• <b>Score split:</b> LQ {categories['liquidity_health']:.0f}/20 · "
        f"Social {categories['community_signals']:.0f}/20 · Spoof {categories['spoof_risk']:.0f}/20"
    )


async def build_verdict_v2(
    db: AsyncSession,
    *,
    ca: str,
    launch: dict[str, Any],
    dex: dict[str, Any] | None = None,
) -> dict[str, Any]:
    research_row = await get_latest_token_research(db, ca)
    research = (research_row.processed_data if research_row else {}) or {}
    market = (dex or research.get("market") or {}) or {}
    spoof_rows = await list_spoof_signals(db, ca)
    spoof = [
        {
            "type": row.signal_type,
            "severity": row.severity,
            "score_impact": row.score_impact,
            "title": row.title,
            "details": row.details,
            "evidence": row.evidence_json,
        }
        for row in spoof_rows
    ]

    liquidity_score, liquidity_reasons, liquidity_risks = score_liquidity(market)
    community_score, community_reasons, community_risks = score_community(research)
    spoof_score, spoof_reasons, spoof_risks = score_spoof(spoof)

    categories = {
        "bundle_strength": 8.0,
        "dev_wallet_behavior": 7.0,
        "funding_quality": 6.0,
        "liquidity_health": liquidity_score,
        "community_signals": community_score,
        "spoof_risk": spoof_score,
    }
    score = round(sum(categories.values()), 1)
    label = label_for_score(score)
    reasons = liquidity_reasons + community_reasons + spoof_reasons
    risks = liquidity_risks + community_risks + spoof_risks
    symbol = (launch.get("symbol") or research.get("symbol") or "").lstrip("$")
    token_name = launch.get("name") or symbol or ca
    human = build_human_readable(
        symbol=symbol,
        token_name=token_name,
        label=label,
        score=score,
        categories=categories,
        reasons=reasons,
        risks=risks,
        research=research,
    )
    verdict = {
        "version": "verdict-v2.0",
        "token": {
            "ca": ca.lower(),
            "symbol": symbol,
            "name": token_name,
            "source": launch.get("source", ""),
        },
        "score": score,
        "label": label,
        "categories": categories,
        "reasons": reasons[:6],
        "risks": risks[:6],
        "spoof_signals": spoof,
        "research": research,
        "research_id": research_row.id if research_row else None,
        "ai": {
            "provider": "stub",
            "used": False,
        },
    }
    row = await create_verdict_v2(
        db,
        ca=ca,
        research_id=research_row.id if research_row else None,
        score=score,
        label=label,
        score_json={"categories": categories, "weights": WEIGHTS},
        verdict_json=verdict,
        human_readable=human,
    )
    verdict["id"] = row.id
    verdict["human_readable"] = human
    return verdict
