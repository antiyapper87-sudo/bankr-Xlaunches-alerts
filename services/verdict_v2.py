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


def fmt_usd_short(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def label_for_score(score: float) -> str:
    if score >= 74:
        return "WATCH"
    if score >= 54:
        return "WAIT"
    return "SKIP"


def score_liquidity(market: dict[str, Any], source: str) -> tuple[float, list[str], list[str]]:
    mcap = float(market.get("mcap") or 0)
    volume = float(market.get("volume_24h") or 0)
    liquidity = float(market.get("liquidity") or 0)
    age_minutes = market.get("age_minutes")
    is_safe_source = source in {"bankr", "clanker", "virtuals"}
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
    if liquidity >= 75_000:
        score += 6
        reasons.append("liquidity is strong for early stage")
    elif liquidity >= 30_000:
        score += 6
        reasons.append("liquidity is usable for early stage")
    elif is_safe_source:
        score += 4
        reasons.append("launchpad source; liquidity check is secondary")
    elif liquidity > 0:
        score += 2
        risks.append("liquidity is thin")
    else:
        risks.append("liquidity is missing")
    if age_minutes is not None and float(age_minutes) <= 240:
        score += 1
        reasons.append("fresh pair window")
    return min(20, score), reasons, risks


def score_community(research: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    social = research.get("social") or {}
    source = research.get("source") or {}
    reasons: list[str] = []
    risks: list[str] = []
    score = 4.0
    if social.get("x_username"):
        score += 8
        reasons.append(f"creator metadata links to @{social['x_username']}")
    if social.get("website_url"):
        score += 3
        reasons.append("website/profile link is present")
    if social.get("tweet_url"):
        score += 3
        reasons.append("launch tweet is present")
    if source.get("description"):
        score += 2
        reasons.append("token has a public description")
    if not social.get("x_username"):
        risks.append("no creator X metadata available")
    return min(20.0, score), reasons, risks


def score_dev_wallet(research: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    source = research.get("source") or {}
    reasons: list[str] = []
    risks: list[str] = []
    score = 5.0
    if source.get("deployer_wallet"):
        score += 4
        reasons.append("deployer wallet is captured")
    if source.get("x_username"):
        score += 4
        reasons.append("deployer/social identity is linked")
    else:
        risks.append("deployer/social identity is unresolved")
    if source.get("source") in {"bankr", "clanker", "virtuals"}:
        score += 2
        reasons.append("launch source is structured")
    return min(15.0, score), reasons, risks


def score_funding_quality(research: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    source = research.get("source") or {}
    flags = set(research.get("flags") or [])
    reasons: list[str] = []
    risks: list[str] = []
    score = 5.0
    if source.get("source") in {"bankr", "clanker", "virtuals"}:
        score += 2
        reasons.append("launchpad context is known")
    if "paid_attention" in flags:
        score -= 2
        risks.append("paid attention before deeper validation")
    if "passes_primary_market_filters" in flags:
        score += 3
        reasons.append("primary market filters are met")
    return max(0.0, min(10.0, score)), reasons, risks


def score_bundle_strength(research: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    onchain = research.get("onchain") or {}
    bundle = onchain.get("bundle") or {}
    reasons: list[str] = []
    risks: list[str] = []
    related_wallets = int(bundle.get("small_related_wallet_count") or 0)
    if related_wallets >= 5:
        risks.append("possible related early wallet cluster")
        return 4.0, reasons, risks
    risks.append("bundle analysis is not connected yet")
    return 8.0, reasons, risks


def score_spoof(spoof_signals: list[dict[str, Any]]) -> tuple[float, list[str], list[str]]:
    impact = sum(float(signal.get("score_impact") or 0) for signal in spoof_signals)
    score = max(0.0, 20.0 - impact)
    risks = [signal.get("title", "spoof risk") for signal in spoof_signals[:4]]
    reasons = ["no major spoof signal detected"] if not risks else []
    return score, reasons, risks


def top_items(items: list[str], limit: int = 2) -> str:
    return "; ".join(items[:limit]) if items else "none"


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
    market = research.get("market") or {}
    score_10 = score / 10
    market_text = (
        f"MC {fmt_usd_short(float(market.get('mcap') or 0))} · "
        f"Vol {fmt_usd_short(float(market.get('volume_24h') or 0))} · "
        f"Liq {fmt_usd_short(float(market.get('liquidity') or 0))}"
    )
    age = market.get("age_minutes")
    if age is not None:
        market_text += f" · Age {float(age):.0f}m"
    reason_text = top_items(reasons, 2) or "limited positive evidence"
    risk_text = top_items(risks, 3) or "no major deterministic risk detected"
    return (
        f"🧠 <b>AI brief</b> • Score <b>{score_10:.1f}/10</b> · <b>{label}</b>\n\n"
        f"• <b>Type:</b> {token_type}\n"
        f"• <b>Owner:</b> {owner_note[:180]}\n"
        f"• <b>Market:</b> {market_text}\n"
        f"• <b>Product:</b> {product_note[:220]}\n"
        f"• <b>Focus:</b> {reason_text}\n"
        f"• <b>Risks:</b> {risk_text}\n\n"
        f"<i>Split: LQ {categories['liquidity_health']:.0f}/20 · "
        f"Social {categories['community_signals']:.0f}/20 · Spoof {categories['spoof_risk']:.0f}/20 · "
        f"Dev {categories['dev_wallet_behavior']:.0f}/15</i>"
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
    research_market = (research.get("market") or {}) or {}
    market = {**research_market, **(dex or {})}
    source_info = research.get("source") or {}
    source = (launch.get("source") or source_info.get("source") or "").lower()
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

    liquidity_score, liquidity_reasons, liquidity_risks = score_liquidity(market, source)
    community_score, community_reasons, community_risks = score_community(research)
    dev_score, dev_reasons, dev_risks = score_dev_wallet(research)
    funding_score, funding_reasons, funding_risks = score_funding_quality(research)
    bundle_score, bundle_reasons, bundle_risks = score_bundle_strength(research)
    spoof_score, spoof_reasons, spoof_risks = score_spoof(spoof)

    categories = {
        "bundle_strength": bundle_score,
        "dev_wallet_behavior": dev_score,
        "funding_quality": funding_score,
        "liquidity_health": liquidity_score,
        "community_signals": community_score,
        "spoof_risk": spoof_score,
    }
    score = round(sum(categories.values()), 1)
    label = label_for_score(score)
    reasons = liquidity_reasons + community_reasons + dev_reasons + funding_reasons + bundle_reasons + spoof_reasons
    risks = liquidity_risks + community_risks + dev_risks + funding_risks + bundle_risks + spoof_risks
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
