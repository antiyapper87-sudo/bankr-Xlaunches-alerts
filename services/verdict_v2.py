from __future__ import annotations

import html
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database import create_verdict_v2, get_latest_token_research, list_spoof_signals


VERSION = "verdict-v2.1"

WEIGHTS = {
    "market": 32,
    "social": 28,
    "risk": 20,
    "deployer": 12,
    "narrative": 8,
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def fmt_usd_short(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def label_for_score(score: float, hard_risks: list[str]) -> str:
    if any("prior $" in risk.lower() or "ticker collision" in risk.lower() for risk in hard_risks):
        return "SKIP" if score < 72 else "WAIT"
    if score >= 72:
        return "WATCH"
    if score >= 56:
        return "WAIT"
    return "SKIP"


def score_market(market: dict[str, Any], source: str) -> tuple[float, list[str], list[str]]:
    mcap = num(market.get("mcap"))
    volume = num(market.get("volume_24h"))
    liquidity = num(market.get("liquidity"))
    age = market.get("age_minutes")
    age = num(age, -1) if age is not None else None
    volume_liq = volume / liquidity if liquidity > 0 else 0
    mcap_liq = mcap / liquidity if liquidity > 0 else 0
    txns_h1 = int(num(market.get("txns_h1_buys")) + num(market.get("txns_h1_sells")))
    safe_source = source in {"bankr", "clanker", "virtuals"}

    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []

    if mcap >= 250_000:
        score += 8
        reasons.append(f"strong early cap {fmt_usd_short(mcap)}")
    elif mcap >= 100_000:
        score += 7
        reasons.append(f"healthy early cap {fmt_usd_short(mcap)}")
    elif mcap >= 50_000:
        score += 5
        reasons.append(f"mcap passed filter {fmt_usd_short(mcap)}")
    else:
        score += 1
        risks.append("mcap below launch filter")

    if volume >= 250_000:
        score += 8
        reasons.append(f"strong volume {fmt_usd_short(volume)}")
    elif volume >= 75_000:
        score += 7
        reasons.append(f"volume confirms attention {fmt_usd_short(volume)}")
    elif volume >= 30_000:
        score += 5
        reasons.append(f"volume passed filter {fmt_usd_short(volume)}")
    else:
        risks.append("volume is not confirmed")

    if liquidity >= 100_000:
        score += 7
        reasons.append(f"deep early liquidity {fmt_usd_short(liquidity)}")
    elif liquidity >= 50_000:
        score += 6
        reasons.append(f"usable liquidity {fmt_usd_short(liquidity)}")
    elif liquidity >= 30_000:
        score += 4
        reasons.append(f"liquidity passed minimum {fmt_usd_short(liquidity)}")
    elif safe_source:
        score += 4
        reasons.append("structured launchpad source reduces liquidity weight")
    elif liquidity > 0:
        score += 1
        risks.append("liquidity is thin")
    else:
        risks.append("liquidity is missing")

    if age is not None:
        if age <= 20:
            score += 3
            reasons.append("very fresh pair")
        elif age <= 240:
            score += 2
            reasons.append("inside fresh-launch window")
        else:
            risks.append("pair age is outside early-launch window")

    if txns_h1 >= 40:
        score += 3
        reasons.append(f"active 1h flow ({txns_h1} txns)")
    elif txns_h1 >= 15:
        score += 2

    if volume_liq >= 8:
        score -= 5
        risks.append("volume/liquidity ratio is extreme")
    elif volume_liq >= 3:
        score -= 2
        risks.append("volume is high versus liquidity")
    if mcap_liq >= 15:
        score -= 5
        risks.append("market cap is stretched versus liquidity")
    elif mcap_liq >= 8:
        score -= 2
        risks.append("liquidity is thin relative to mcap")

    return clamp(score, 0, WEIGHTS["market"]), reasons, risks


def score_social(research: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    social = research.get("social") or {}
    smart_money = research.get("smart_money") or {}
    reasons: list[str] = []
    risks: list[str] = []
    score = 0.0

    qualified = int(social.get("qualified_tweets") or 0)
    total_followers = int(social.get("total_followers") or 0)
    total_likes = int(social.get("total_likes") or 0)
    max_score = int(social.get("max_score") or 0)
    avg_quality = num(social.get("avg_thesis_quality"))
    top_authors = social.get("top_authors") or []
    evidence_count = int(social.get("evidence_count") or 0)
    project_value_score = int(social.get("project_value_score") or 0)
    hermes_social_score = int(social.get("social_score") or 0)

    if social.get("ca_verified"):
        score += 8
        reasons.append(f"{qualified} CA-verified X posts")
    else:
        risks.append("no CA-verified social proof")

    if qualified >= 10:
        score += 6
        reasons.append("broad early X coverage")
    elif qualified >= 5:
        score += 5
        reasons.append("qualified X threshold passed")
    elif qualified > 0:
        score += 2
        risks.append("social proof is below preferred count")

    if total_followers >= 250_000:
        score += 5
        reasons.append("high-follower social reach")
    elif total_followers >= 50_000:
        score += 4
        reasons.append("meaningful social reach")
    elif total_followers >= 10_000:
        score += 2

    if max_score >= 16 or avg_quality >= 6:
        score += 4
        reasons.append("tweet quality contains thesis/metrics")
    elif max_score >= 8:
        score += 2

    if total_likes >= 100:
        score += 3
    elif total_likes >= 25:
        score += 1

    if any(int(author.get("tier") or 3) <= 2 for author in top_authors):
        score += 2
        reasons.append("at least one higher-tier author")

    if evidence_count >= 5:
        score += 2
        reasons.append("Hermes evidence set is populated")
    if project_value_score >= 14:
        score += 2
        reasons.append("social evidence supports project value")
    if hermes_social_score >= 70:
        score += 4
        reasons.append(f"Hermes social score is strong ({hermes_social_score}/100)")
    elif hermes_social_score >= 50:
        score += 2
        reasons.append(f"Hermes social score is watchable ({hermes_social_score}/100)")
    elif hermes_social_score and hermes_social_score < 35:
        risks.append(f"Hermes social score is weak ({hermes_social_score}/100)")

    inflow_wallets = int(smart_money.get("inflow_wallets") or 0)
    outflow_wallets = int(smart_money.get("outflow_wallets") or 0)
    if inflow_wallets >= 3:
        score += 5
        reasons.append(f"tracked wallet convergence: {inflow_wallets}")
    elif inflow_wallets > 0:
        score += 2
        reasons.append(f"tracked wallet inflow: {inflow_wallets}")
    if outflow_wallets > 0 and inflow_wallets == 0:
        risks.append(f"tracked wallet outflow: {outflow_wallets}")

    if not social.get("x_username") and not social.get("ca_verified"):
        risks.append("creator X metadata is unresolved")

    return clamp(score, 0, WEIGHTS["social"]), reasons, risks


def score_risk(spoof_signals: list[dict[str, Any]], research: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    flags = set(research.get("flags") or [])
    ranked = sorted(spoof_signals, key=lambda signal: num(signal.get("score_impact")), reverse=True)
    impact = sum(num(signal.get("score_impact")) for signal in ranked)
    score = WEIGHTS["risk"] - min(WEIGHTS["risk"], impact * 0.65)
    risks = [signal.get("title", "spoof risk") for signal in ranked[:4]]
    reasons: list[str] = []

    if "paid_attention" in flags:
        score -= 2
        risks.append("paid attention source")
    if "thin_liquidity_vs_mcap" in flags:
        score -= 2
    if "volume_above_liquidity" in flags:
        score -= 2
    if not risks:
        reasons.append("no deterministic spoof signal detected")

    return clamp(score, 0, WEIGHTS["risk"]), reasons, risks


def score_deployer(research: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    source = research.get("source") or {}
    deployer = research.get("deployer") or {}
    reasons: list[str] = []
    risks: list[str] = []
    score = 0.0

    if source.get("x_username"):
        score += 4
        reasons.append(f"creator metadata links to @{source['x_username']}")
    elif source.get("deployer_wallet"):
        score += 2
        reasons.append("deployer wallet is captured")
    else:
        risks.append("deployer identity is unresolved")

    if source.get("source") in {"bankr", "clanker", "virtuals"}:
        score += 3
        reasons.append("structured launch source")
    elif source.get("source") in {"dexscreener", "coingecko"}:
        score += 1

    previous = int(deployer.get("previous_launches") or 0)
    signaled = int(deployer.get("signaled_launches") or 0)
    dead_ratio = num(deployer.get("dead_ratio"))
    best_previous = num(deployer.get("best_previous_mcap"))
    if previous == 0:
        score += 3
        reasons.append("no negative local deployer history")
    else:
        score += min(4, signaled * 1.5)
        reasons.append(f"deployer history: {previous} previous")
        if best_previous >= 250_000:
            score += 2
            reasons.append("previous deployer launch reached traction")
        if dead_ratio >= 0.5:
            score -= 4
            risks.append("many previous launches expired/dead")

    return clamp(score, 0, WEIGHTS["deployer"]), reasons, risks


def score_narrative(research: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    token_type = research.get("token_type") or ""
    product_note = research.get("product_note") or ""
    project_narrative = research.get("project_narrative") or {}
    social = research.get("social") or {}
    project_value = social.get("project_value") or ""
    project_value_score = int(social.get("project_value_score") or 0)
    source = research.get("source") or {}
    reasons: list[str] = []
    risks: list[str] = []
    score = 1.0
    narrative_confidence = str(project_narrative.get("confidence") or "").upper()

    weak_product = not product_note or any(
        phrase in product_note.lower()
        for phrase in ("not proven", "not confirmed", "differentiation is not")
    )
    if project_value == "Utility / Tech":
        score += 4
        reasons.append("Hermes classifies social thesis as utility/tech")
    elif project_value == "Narrative / Community":
        score += 2
        reasons.append("Hermes sees narrative/community traction")
    elif project_value == "Memecoin / Low-priority":
        score -= 1
        risks.append("social thesis is mostly meme-driven")

    if project_value_score >= 16:
        score += 2
    elif project_value_score >= 10:
        score += 1

    if token_type and token_type != "Memecoin / Experimental":
        score += 2
        reasons.append(f"clearer narrative: {token_type}")
    if narrative_confidence == "HIGH":
        score += 3
        reasons.append("project narrative is confirmed by multiple sources")
    elif narrative_confidence == "MEDIUM":
        score += 2
        reasons.append("project narrative has usable confirmation")
    elif project_narrative:
        risks.append("project narrative confidence is low")
    if project_narrative.get("is_ticker_only_evidence"):
        score -= 3
        risks.append("project narrative is ticker-only, not contract-confirmed")
    if project_narrative.get("same_ticker_collision"):
        score -= 4
        risks.append("ticker collision weakens narrative attribution")
    if product_note and not weak_product:
        score += 3
        reasons.append("product/narrative metadata is present")
    elif source.get("description"):
        score += 1
    else:
        risks.append("narrative differentiation is weak")
    if source.get("website_url"):
        score += 1

    return clamp(score, 0, WEIGHTS["narrative"]), reasons, risks


def confidence_for(research: dict[str, Any], spoof_signals: list[dict[str, Any]]) -> str:
    social = research.get("social") or {}
    market = research.get("market") or {}
    if social.get("ca_verified") and market.get("pair_created_at") and spoof_signals:
        return "high"
    if social.get("ca_verified") and market:
        return "medium"
    return "low"


def top_items(items: list[str], limit: int = 2) -> str:
    return "; ".join(items[:limit]) if items else "none"


def fmt_compact_int(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return str(value)


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def format_evidence_refs(research: dict[str, Any], *, limit: int = 3) -> str:
    social = research.get("social") or {}
    tweets = social.get("evidence_tweets") or []
    if not tweets:
        return "none"
    parts: list[str] = []
    for item in tweets[:limit]:
        ref = int(item.get("ref") or 0)
        username = item.get("username") or "unknown"
        views = fmt_compact_int(int(item.get("views") or 0))
        likes = fmt_compact_int(int(item.get("likes") or 0))
        url = item.get("url") or ""
        retweets = fmt_compact_int(int(item.get("retweets") or 0))
        label = f"[{ref}] @{username} ❤️ {likes} · 👁 {views} · 🔄 {retweets}"
        if url:
            parts.append(f"<a href='{url}'>{label}</a>")
        else:
            parts.append(label)
    return " · ".join(parts)


def build_product_line(research: dict[str, Any], reasons: list[str]) -> str:
    project_narrative = research.get("project_narrative") or {}
    product_note = project_narrative.get("product") or research.get("product_note") or "Product differentiation is not proven yet."
    product_note = " ".join(str(product_note).split())
    if len(product_note) > 145:
        product_note = product_note[:142] + "..."
    signal = top_items(reasons, 1)
    if signal and signal != "none":
        return f"{product_note} Signal: {signal}."
    return product_note


def build_human_readable(
    *,
    label: str,
    score: float,
    reasons: list[str],
    risks: list[str],
    research: dict[str, Any],
    confidence: str,
) -> str:
    token_type = research.get("token_type") or "Unknown"
    social = research.get("social") or {}
    project_narrative = research.get("project_narrative") or {}
    thesis = social.get("evidence_thesis") or ""
    value_assessment = social.get("value_assessment") or ""
    social_score = int(social.get("social_score") or 0)
    breakdown = social.get("score_breakdown") or {}
    project_value = social.get("project_value") or token_type
    product_line = build_product_line(research, reasons)
    why_value = project_narrative.get("why_it_matters") or value_assessment or product_line
    if len(why_value) > 220:
        why_value = why_value[:217] + "..."
    thesis_line = thesis or product_line
    if len(thesis_line) > 220:
        thesis_line = thesis_line[:217] + "..."
    evidence_line = format_evidence_refs(research)
    risk_text = top_items(risks, 2) or "no major deterministic risk detected"
    value_line = value_assessment or why_value
    if len(value_line) > 220:
        value_line = value_line[:217] + "..."
    split = ""
    if breakdown:
        split = (
            f"\n<i>Split: Narrative {int(breakdown.get('narrative') or 0)}/40 · "
            f"Creator {int(breakdown.get('creator') or 0)}/30 · "
            f"Utility/Tech {int(breakdown.get('utility_tech') or 0)}/20 · "
            f"Shill Risk -{int(breakdown.get('shill_risk') or 0)}/10</i>"
        )
    return (
        f"🧠 <b>AI brief</b> • Score <b>{score / 10:.1f}/10</b> · <b>{label}</b>\n\n"
        f"• <b>Type:</b> {esc(project_value)}\n"
        f"• <b>Product:</b> {esc(product_line)}\n"
        f"• <b>Thesis:</b> {esc(thesis_line)}\n"
        f"• <b>Why value:</b> {esc(why_value)}\n"
        f"• <b>Evidence:</b> {evidence_line}\n"
        f"• <b>Risks:</b> {esc(risk_text)}\n"
        f"• <b>Confidence:</b> {esc(confidence)}"
        + (f"\n• <b>Social Score:</b> {social_score}/100" if social_score else "")
        + split
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

    market_score, market_reasons, market_risks = score_market(market, source)
    social_score, social_reasons, social_risks = score_social(research)
    risk_score, risk_reasons, risk_risks = score_risk(spoof, research)
    deployer_score, deployer_reasons, deployer_risks = score_deployer(research)
    narrative_score, narrative_reasons, narrative_risks = score_narrative(research)

    categories = {
        "market": round(market_score, 1),
        "social": round(social_score, 1),
        "risk": round(risk_score, 1),
        "deployer": round(deployer_score, 1),
        "narrative": round(narrative_score, 1),
    }
    reasons = market_reasons + social_reasons + risk_reasons + deployer_reasons + narrative_reasons
    risks = risk_risks + market_risks + social_risks + deployer_risks + narrative_risks
    raw_score = sum(categories.values())
    score = round(clamp(raw_score, 0, 100), 1)
    label = label_for_score(score, risks)
    confidence = confidence_for(research, spoof)
    symbol = (launch.get("symbol") or research.get("symbol") or "").lstrip("$")
    token_name = launch.get("name") or symbol or ca
    human = build_human_readable(
        label=label,
        score=score,
        reasons=reasons,
        risks=risks,
        research=research,
        confidence=confidence,
    )
    verdict = {
        "version": VERSION,
        "token": {
            "ca": ca.lower(),
            "symbol": symbol,
            "name": token_name,
            "source": launch.get("source", ""),
        },
        "score": score,
        "label": label,
        "confidence": confidence,
        "categories": categories,
        "reasons": reasons[:8],
        "risks": risks[:8],
        "spoof_signals": spoof,
        "research": research,
        "research_id": research_row.id if research_row else None,
        "ai": {
            "provider": "deterministic",
            "used": False,
            "model": VERSION,
        },
    }
    row = await create_verdict_v2(
        db,
        ca=ca,
        research_id=research_row.id if research_row else None,
        score=score,
        label=label,
        score_json={"categories": categories, "weights": WEIGHTS, "confidence": confidence},
        verdict_json=verdict,
        human_readable=human,
        version=VERSION,
    )
    verdict["id"] = row.id
    verdict["human_readable"] = human
    return verdict
