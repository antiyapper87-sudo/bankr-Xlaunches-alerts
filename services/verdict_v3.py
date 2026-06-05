from __future__ import annotations

import hashlib
import html
import json
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database import create_verdict_v3, get_latest_block_scan, get_latest_token_research, list_bundle_signals, utc_now
from services.agent_memory import build_memory_context


VERSION = "3.0"
MODEL = "verdict-v3-shadow"

WEIGHTS_V3 = {
    "market": 25,
    "social_primary": 20,
    "lore": 15,
    "onchain_bundle": 15,
    "memory": 10,
    "deployer": 10,
    "risk_penalty": 5,
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def stable_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def primary_social_count(social: dict[str, Any]) -> int:
    provenance = social.get("source_provenance") or {}
    trust = social.get("trust_summary") or {}
    return (
        int(provenance.get("ca_confirmed") or trust.get("ca_confirmed") or 0)
        + int(provenance.get("pair_confirmed") or trust.get("pair_confirmed") or 0)
        + int(provenance.get("project_confirmed") or trust.get("project_confirmed") or 0)
    )


def ticker_context_count(social: dict[str, Any]) -> int:
    provenance = social.get("source_provenance") or {}
    trust = social.get("trust_summary") or {}
    return int(provenance.get("ticker_strong") or trust.get("ticker_strong") or 0) + int(provenance.get("ticker_only") or trust.get("ticker_only") or 0)


def score_market(market: dict[str, Any]) -> tuple[float, list[str]]:
    mcap = num(market.get("mcap"))
    volume = num(market.get("volume_24h"))
    liquidity = num(market.get("liquidity"))
    score = 0.0
    reasons: list[str] = []
    if mcap >= 250_000:
        score += 8
        reasons.append("strong early market cap")
    elif mcap >= 50_000:
        score += 6
        reasons.append("market cap passed launch filter")
    if volume >= 150_000:
        score += 8
        reasons.append("volume confirms strong attention")
    elif volume >= 30_000:
        score += 6
        reasons.append("volume passed launch filter")
    if liquidity >= 100_000:
        score += 7
        reasons.append("liquidity is healthier than minimum")
    elif liquidity >= 30_000:
        score += 5
        reasons.append("liquidity passed launch filter")
    return clamp(score, 0, WEIGHTS_V3["market"]), reasons


def score_social(social: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    primary = primary_social_count(social)
    ticker_context = ticker_context_count(social)
    social_score = int(social.get("social_score") or 0)
    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []
    if primary >= 5:
        score += 12
        reasons.append(f"{primary} primary social evidence items")
    elif primary >= 2:
        score += 8
        reasons.append(f"{primary} primary social evidence items")
    elif primary == 1:
        score += 3
        risks.append("only one primary social evidence item")
    else:
        risks.append("no primary social proof")
    if primary and social_score >= 70:
        score += 6
        reasons.append("Hermes primary social score is strong")
    elif primary and social_score >= 45:
        score += 3
    if ticker_context and not primary:
        risks.append("ticker context is not contract proof")
    return clamp(score, 0, WEIGHTS_V3["social_primary"]), reasons, risks


def score_lore(lore: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    confidence = str(lore.get("ca_attribution_confidence") or lore.get("attribution_confidence") or "LOW").upper()
    category = lore.get("project_category") or lore.get("category") or "Unknown"
    summary = str(lore.get("narrative_summary") or "")
    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []
    if confidence == "HIGH":
        score += 10
        reasons.append("project lore has high attribution confidence")
    elif confidence == "MEDIUM":
        score += 7
        reasons.append("project lore has usable attribution")
    elif summary:
        score += 3
        risks.append("project lore attribution is low")
    if category not in {"Unknown", "Unclear / Experimental", "Meme / Community"}:
        score += 4
        reasons.append(f"specific category detected: {category}")
    elif category == "Meme / Community":
        score += 1
    if not summary:
        risks.append("product narrative missing")
    return clamp(score, 0, WEIGHTS_V3["lore"]), reasons, risks


def score_onchain(onchain: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    bundle_risk = num(onchain.get("bundle_risk") or onchain.get("bundle_risk_score"))
    sniper_risk = num(onchain.get("sniper_score"))
    prebuy_risk = num(onchain.get("prebuy_risk"))
    dev_risk = num(onchain.get("dev_dump_risk") or onchain.get("dev_risk_score"))
    liquidity_risk = num(onchain.get("liquidity_risk") or onchain.get("liquidity_risk_score"))
    holder_risk = num(onchain.get("holder_concentration_risk") or onchain.get("holder_concentration_score"))
    risk = max(bundle_risk, sniper_risk, prebuy_risk, dev_risk, liquidity_risk, holder_risk, num(onchain.get("overall_risk_score")))
    reasons: list[str] = []
    risks: list[str] = []
    if risk >= 80:
        risks.append("high on-chain manipulation risk")
        return 0.0, reasons, risks
    if risk >= 50:
        risks.append("medium on-chain risk")
        return 5.0, reasons, risks
    if onchain.get("provider") == "stub" or not onchain:
        risks.append("on-chain bundle scan pending")
        return 6.0, reasons, risks
    if bundle_risk >= 25:
        wallets = int(onchain.get("suspected_bundle_wallets_count") or 0)
        risks.append(f"suspected early wallet cluster: {wallets} wallets")
    if sniper_risk >= 25:
        risks.append("sniper-like first-block launch pattern detected")
    if dev_risk >= 25:
        risks.append("deployer-linked behavior detected")
    if liquidity_risk >= 25:
        risks.append("liquidity removal/anomaly detected")
    if risks:
        return 8.0, reasons, risks
    reasons.append("no severe on-chain bundle risk")
    return 12.0, reasons, risks


def memory_adjustment(memory: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    confidence = num(memory.get("confidence"))
    if confidence < 0.65:
        return 0.0, [], ["memory confidence too low"]
    adjustment = clamp(num(memory.get("score_adjustment")), -12.0, 8.0)
    reasons = [str(item) for item in memory.get("positive_notes") or []][:2]
    risks = [str(item) for item in memory.get("risk_notes") or []][:2]
    return adjustment, reasons, risks


def label_for(score: float, *, primary: int, collision: bool, bundle_risk: float, confidence: str) -> str:
    if collision and primary < 3:
        return "SKIP"
    if bundle_risk >= 80:
        return "HIGH RISK"
    if score >= 75 and confidence != "LOW":
        return "WATCH"
    if score >= 58:
        return "WAIT"
    return "SKIP"


def confidence_for(lore: dict[str, Any], social: dict[str, Any], onchain: dict[str, Any]) -> str:
    lore_conf = str(lore.get("ca_attribution_confidence") or lore.get("attribution_confidence") or "LOW").upper()
    primary = primary_social_count(social)
    if lore_conf == "HIGH" and primary >= 5 and onchain.get("provider") != "stub":
        return "HIGH"
    if lore_conf in {"HIGH", "MEDIUM"} and primary >= 2:
        return "MEDIUM"
    return "LOW"


def build_verdict_input(*, ca: str, launch: dict[str, Any], research: dict[str, Any], memory: dict[str, Any] | None = None) -> dict[str, Any]:
    market = research.get("market") or {}
    social = research.get("social") or {}
    lore = research.get("project_lore") or research.get("project_narrative") or {}
    onchain = research.get("onchain") or {}
    collision = {
        "same_ticker_collision": bool((research.get("project_narrative") or {}).get("same_ticker_collision")),
        "risk": "high" if (research.get("project_narrative") or {}).get("same_ticker_collision") else "none",
    }
    return {
        "schema": "verdict-v3-input",
        "version": VERSION,
        "token": {
            "chain": "base",
            "token_id": ca.lower(),
            "ticker": (launch.get("symbol") or research.get("symbol") or "").lstrip("$"),
            "name": launch.get("name") or research.get("name") or "",
            "launch_source": launch.get("source") or (research.get("source") or {}).get("source") or "",
            "first_seen_at": str(launch.get("first_seen_at") or ""),
        },
        "market": market,
        "social": {
            "primary_evidence_count": primary_social_count(social),
            "ca_confirmed": int((social.get("source_provenance") or {}).get("ca_confirmed") or 0),
            "project_confirmed": int((social.get("source_provenance") or {}).get("project_confirmed") or 0),
            "pair_confirmed": int((social.get("source_provenance") or {}).get("pair_confirmed") or 0),
            "ticker_context_count": ticker_context_count(social),
            "social_score_primary": int(social.get("social_score") or 0),
            "evidence_refs": (social.get("evidence_tweets") or [])[:6],
        },
        "lore": lore,
        "onchain": {
            "provider": onchain.get("provider") or "stub",
            "bundle_risk": num((onchain.get("bundle") or {}).get("risk") or onchain.get("bundle_risk")),
            "prebuy_risk": num(onchain.get("prebuy_risk")),
            "dev_dump_risk": num(onchain.get("dev_dump_risk")),
            "holder_concentration_risk": num(onchain.get("holder_concentration_risk")),
            "funding_quality": onchain.get("funding_quality") or "unknown",
            "signals": onchain.get("signals") or [],
        },
        "memory": memory or {"score_adjustment": 0, "confidence": 0, "matches": []},
        "collision": collision,
    }


def build_output(verdict_input: dict[str, Any]) -> dict[str, Any]:
    market_score, market_reasons = score_market(verdict_input["market"])
    social_score, social_reasons, social_risks = score_social(verdict_input["social"])
    lore_score, lore_reasons, lore_risks = score_lore(verdict_input["lore"])
    onchain_score, onchain_reasons, onchain_risks = score_onchain(verdict_input["onchain"])
    memory_score, memory_reasons, memory_risks = memory_adjustment(verdict_input["memory"])
    deployer_score = 4.0
    risk_penalty = 0.0
    risks = social_risks + lore_risks + onchain_risks + memory_risks
    if verdict_input["collision"].get("same_ticker_collision"):
        risk_penalty -= 8
        risks.insert(0, "same-ticker collision")
    raw_score = market_score + social_score + lore_score + onchain_score + memory_score + deployer_score + risk_penalty
    score = round(clamp(raw_score, 0, 100), 1)
    confidence = confidence_for(verdict_input["lore"], verdict_input["social"], verdict_input["onchain"])
    label = label_for(
        score,
        primary=int(verdict_input["social"].get("primary_evidence_count") or 0),
        collision=bool(verdict_input["collision"].get("same_ticker_collision")),
        bundle_risk=num(verdict_input["onchain"].get("bundle_risk")),
        confidence=confidence,
    )
    lore = verdict_input["lore"]
    product = lore.get("narrative_summary") or lore.get("product") or "Product narrative is not confirmed."
    why = lore.get("why_it_matters") or "No reliable value case confirmed yet."
    thesis = product if label != "SKIP" else f"{product} Conviction remains capped by attribution/risk."
    return {
        "schema": "verdict-v3-output",
        "version": VERSION,
        "score": score,
        "label": label,
        "confidence": confidence,
        "category_scores": {
            "market": round(market_score, 1),
            "social_primary": round(social_score, 1),
            "lore": round(lore_score, 1),
            "onchain_bundle": round(onchain_score, 1),
            "memory": round(memory_score, 1),
            "deployer": round(deployer_score, 1),
            "risk_penalty": round(risk_penalty, 1),
        },
        "product": product,
        "why_it_matters": why,
        "thesis": thesis,
        "social_proof": {
            "summary": f"Primary {verdict_input['social'].get('primary_evidence_count', 0)} · Ticker context {verdict_input['social'].get('ticker_context_count', 0)}",
            "attribution": confidence,
            "ca_confirmed": verdict_input["social"].get("ca_confirmed", 0),
            "ticker_context": verdict_input["social"].get("ticker_context_count", 0),
        },
        "onchain_risk": {
            "summary": "; ".join(onchain_risks[:2]) or "No severe on-chain risk available yet.",
            "bundle_risk": verdict_input["onchain"].get("bundle_risk", 0),
            "prebuy_risk": verdict_input["onchain"].get("prebuy_risk", 0),
        },
        "memory_match": {
            "summary": "; ".join(memory_reasons + memory_risks) or "No high-confidence memory match yet.",
            "adjustment": round(memory_score, 1),
        },
        "collision_risk": {"summary": verdict_input["collision"].get("risk", "none")},
        "final_takeaway": "; ".join((market_reasons + social_reasons + lore_reasons + onchain_reasons + risks)[:3]) or "Insufficient conviction.",
        "risks": risks[:8],
        "evidence_refs": (lore.get("evidence") or lore.get("evidence_refs") or [])[:6],
    }


def format_verdict_v3(output: dict[str, Any]) -> str:
    return (
        f"🧠 <b>Verdict 3.0</b> · <b>{output['score'] / 10:.1f}/10</b> · <b>{esc(output['label'])}</b>\n\n"
        f"<b>Product</b>\n{esc(output.get('product'))}\n\n"
        f"<b>Why it matters</b>\n{esc(output.get('why_it_matters'))}\n\n"
        f"<b>Thesis</b>\n{esc(output.get('thesis'))}\n\n"
        f"<b>Social Proof</b>\n{esc((output.get('social_proof') or {}).get('summary'))} · Attribution {esc((output.get('social_proof') or {}).get('attribution'))}\n\n"
        f"<b>On-chain / Bundle Risk</b>\n{esc((output.get('onchain_risk') or {}).get('summary'))}\n\n"
        f"<b>Memory Match</b>\n{esc((output.get('memory_match') or {}).get('summary'))}\n\n"
        f"<b>Collision Risk</b>\n{esc((output.get('collision_risk') or {}).get('summary'))}\n\n"
        f"<b>Final Takeaway</b>\n{esc(output.get('final_takeaway'))}"
    )


async def build_verdict_v3(db: AsyncSession, *, ca: str, launch: dict[str, Any]) -> dict[str, Any]:
    research_row = await get_latest_token_research(db, ca)
    research = (research_row.processed_data if research_row else {}) or {}
    source = research.get("source") or {}
    deployer_key = source.get("deployer_wallet") or source.get("x_username") or ""
    memory_context = await build_memory_context(
        db,
        chain="base",
        deployer_wallet=deployer_key,
        ticker=(launch.get("symbol") or research.get("symbol") or "").lstrip("$"),
        launch_source=launch.get("source") or source.get("source") or "",
    )
    bundle_signals = await list_bundle_signals(db, chain="base", token_id=ca)
    block_scan = await get_latest_block_scan(db, chain="base", token_id=ca)
    if block_scan and block_scan.status == "completed":
        research["onchain"] = {
            **(research.get("onchain") or {}),
            **(block_scan.summary_json or {}),
            "provider": block_scan.provider,
            "scan_status": block_scan.status,
            "confidence": block_scan.confidence,
        }
    if bundle_signals:
        max_risk = max(float(item.risk_score or 0) for item in bundle_signals)
        research["onchain"] = {
            **(research.get("onchain") or {}),
            "provider": "block-reader",
            "bundle_risk": max_risk,
            "signals": [
                {
                    "type": item.signal_type,
                    "severity": item.severity,
                    "risk_score": item.risk_score,
                    "title": item.title,
                }
                for item in bundle_signals[:6]
            ],
        }
    verdict_input = build_verdict_input(ca=ca, launch=launch, research=research, memory=memory_context)
    output = build_output(verdict_input)
    human = format_verdict_v3(output)
    input_hash = stable_hash(verdict_input)
    row = await create_verdict_v3(
        db,
        chain="base",
        token_id=ca,
        input_hash=input_hash,
        score=output["score"],
        label=output["label"],
        confidence=output["confidence"],
        output_json={"input": verdict_input, "output": output},
        human_readable=human,
        expires_at=(utc_now() + timedelta(minutes=30)).replace(microsecond=0),
    )
    output["id"] = row.id
    output["human_readable"] = human
    return output
