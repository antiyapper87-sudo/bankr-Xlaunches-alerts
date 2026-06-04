from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database import upsert_project_lore
from services.project_narrative import clean_text
from services.social_evidence import hide_contract_mentions, is_likely_english_text


LORE_VERSION = "lore-v1"

CATEGORY_KEYWORDS = {
    "AI / Agent": ("ai", "agent", "inference", "model", "automation", "workflow", "intelligence", "llm"),
    "Privacy / Infrastructure": ("privacy", "private", "shield", "zk", "anonymous", "confidential", "encrypted"),
    "Trading / Tooling": ("terminal", "scanner", "trading", "analytics", "dashboard", "bot", "alerts", "tools"),
    "DeFi / Protocol": ("swap", "liquidity", "yield", "vault", "lending", "staking", "dex", "protocol"),
    "Game / Consumer": ("game", "gaming", "consumer", "app", "social", "mobile"),
    "Meme / Community": ("meme", "community", "cult", "mascot", "character", "lore"),
}

UTILITY_TERMS = (
    "platform",
    "protocol",
    "marketplace",
    "infrastructure",
    "terminal",
    "framework",
    "automation",
    "privacy",
    "inference",
    "analytics",
    "tool",
    "app",
)


def identity_key(chain: str, token_id: str) -> str:
    return f"{str(chain or 'base').lower()}:{str(token_id or '').lower()}"


def compact(value: str, *, limit: int = 360) -> str:
    text = clean_text(str(value or ""))
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def excerpt_hash(text: str) -> str:
    return hashlib.sha256(clean_text(text).lower().encode("utf-8")).hexdigest()[:24]


def tweet_text(item: dict[str, Any]) -> str:
    return clean_text(str(item.get("excerpt") or item.get("text") or ""))


def evidence_type(item: dict[str, Any]) -> str:
    return str(item.get("evidence_type") or "").lower()


def is_primary_evidence(item: dict[str, Any]) -> bool:
    return evidence_type(item) in {"ca_confirmed", "pair_confirmed", "project_confirmed"} or bool(item.get("ai_verdict_eligible"))


def is_ticker_context(item: dict[str, Any]) -> bool:
    return evidence_type(item) in {"ticker_strong", "ticker_only"} or bool(item.get("ticker_confirmed"))


def classify_category(text: str) -> str:
    lower = text.lower()
    best = ("Unclear / Experimental", 0)
    for category, terms in CATEGORY_KEYWORDS.items():
        score = sum(1 for term in terms if term in lower)
        if score > best[1]:
            best = (category, score)
    return best[0]


def first_sentence_with_terms(texts: list[str], terms: tuple[str, ...], *, fallback: str = "") -> str:
    for text in texts:
        for sentence in re.split(r"(?<=[.!?])\s+", clean_text(text)):
            sentence_lower = sentence.lower()
            if 35 <= len(sentence) <= 320 and any(term in sentence_lower for term in terms):
                return sentence
    return fallback


def evidence_refs(tweets: list[dict[str, Any]], *, address: str, limit: int = 6) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(tweets, 1):
        text = hide_contract_mentions(tweet_text(item), address)
        if not text or not is_likely_english_text(text):
            continue
        url = str(item.get("url") or "")
        key = url or excerpt_hash(text)
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            {
                "ref": len(refs) + 1,
                "source": item.get("source_provider") or item.get("source") or "x",
                "evidence_type": evidence_type(item) or "unknown",
                "username": item.get("username") or "",
                "url": url,
                "short_excerpt": compact(text, limit=220),
                "excerpt_hash": excerpt_hash(text),
                "score": float(item.get("tweet_tier_score") or item.get("score") or 0),
            }
        )
        if len(refs) >= limit:
            break
    return refs


def source_description(launch: dict[str, Any], dex: dict[str, Any] | None) -> str:
    dex = dex or {}
    raw = launch or {}
    candidates = [
        raw.get("description"),
        raw.get("profile", {}).get("description") if isinstance(raw.get("profile"), dict) else "",
        dex.get("description"),
        dex.get("info", {}).get("description") if isinstance(dex.get("info"), dict) else "",
    ]
    for item in candidates:
        text = compact(str(item or ""), limit=520)
        if text:
            return text
    return ""


def attribution(primary_count: int, ticker_count: int, has_description: bool) -> tuple[str, str]:
    if primary_count >= 3 and has_description:
        return "CA-confirmed", "HIGH"
    if primary_count >= 2:
        return "CA-confirmed", "MEDIUM"
    if primary_count == 1 and has_description:
        return "mixed", "MEDIUM"
    if primary_count == 1:
        return "CA-confirmed", "LOW"
    if ticker_count:
        return "ticker-context", "LOW"
    if has_description:
        return "screener-description", "MEDIUM"
    return "unknown", "LOW"


def build_lore_payload(
    *,
    chain: str = "base",
    token_id: str,
    ticker: str,
    name: str,
    launch: dict[str, Any] | None = None,
    dex: dict[str, Any] | None = None,
    social_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    launch = launch or {}
    dex = dex or {}
    social_evidence = social_evidence or {}
    address = token_id
    tweets = list(social_evidence.get("primary_tweets") or [])
    if not tweets:
        tweets = [
            item for item in social_evidence.get("top_tweets") or []
            if is_primary_evidence(item)
        ]
    ticker_tweets = list(social_evidence.get("ticker_context_tweets") or [])
    if not ticker_tweets:
        ticker_tweets = [
            item for item in social_evidence.get("top_tweets") or []
            if is_ticker_context(item)
        ]
    desc = source_description(launch, dex)
    primary_texts = [hide_contract_mentions(tweet_text(item), address) for item in tweets]
    ticker_texts = [hide_contract_mentions(tweet_text(item), address) for item in ticker_tweets]
    trusted_texts = ([desc] if desc else []) + primary_texts
    context_texts = trusted_texts + ticker_texts[:5] + [name, ticker]
    combined = " ".join(context_texts)
    category = classify_category(combined)
    primary_count = len(tweets)
    ticker_count = len(ticker_tweets)
    attr_type, confidence = attribution(primary_count, ticker_count, bool(desc))

    product_sentence = first_sentence_with_terms(
        trusted_texts,
        UTILITY_TERMS,
        fallback=desc or first_sentence_with_terms(primary_texts, UTILITY_TERMS),
    )
    if not product_sentence and ticker_count and not primary_count:
        product_sentence = f"{name or ticker} has ticker-context only; contract attribution is not confirmed."
    elif not product_sentence:
        product_sentence = f"{name or ticker} has no contract-confirmed product mechanics yet."

    why_sentence = first_sentence_with_terms(
        trusted_texts,
        ("solves", "enables", "allows", "market", "users", "traders", "builders", "infrastructure", "privacy", "automation"),
        fallback="No durable value case is confirmed from primary evidence yet.",
    )
    if attr_type == "ticker-context":
        why_sentence = "Ticker context may describe a real narrative, but it is not safe to attribute to this contract yet."

    lore_bullets = []
    for text in primary_texts[:3]:
        if text:
            lore_bullets.append(compact(text, limit=180))
    if not lore_bullets and desc:
        lore_bullets.append(compact(desc, limit=180))

    primary_refs = evidence_refs(tweets, address=address, limit=4)
    ticker_refs = evidence_refs(ticker_tweets, address=address, limit=2)
    refs = primary_refs + ticker_refs

    return {
        "schema": "project-lore-v1",
        "chain": chain,
        "token_id": token_id.lower() if chain in {"base", "ethereum", "bnb"} else token_id,
        "identity_key": identity_key(chain, token_id),
        "project_category": category,
        "utility": compact(product_sentence, limit=420),
        "product_mechanics": compact(product_sentence, limit=420),
        "target_users": "Unclear from primary evidence." if confidence == "LOW" else "Early traders/builders referenced by available evidence.",
        "meme_lore_hook": "" if category != "Meme / Community" else compact(product_sentence, limit=240),
        "founder_or_dev_identity": launch.get("x_username") or launch.get("creator_x") or "Creator identity unresolved.",
        "community_quality": "Primary social evidence present." if primary_count else "No primary social evidence.",
        "official_vs_unofficial": attr_type,
        "ticker_collision_risk": "high" if social_evidence.get("same_ticker_collision") else "unknown",
        "ca_attribution_confidence": confidence,
        "narrative_summary": compact(product_sentence, limit=520),
        "why_it_matters": compact(why_sentence, limit=360),
        "lore_bullets": lore_bullets[:4],
        "ca_confirmed_evidence_count": sum(1 for item in tweets if evidence_type(item) == "ca_confirmed" or item.get("ca_confirmed")),
        "project_confirmed_evidence_count": sum(1 for item in tweets if evidence_type(item) in {"pair_confirmed", "project_confirmed"}),
        "ticker_context_evidence_count": ticker_count,
        "evidence": refs,
        "extraction_version": LORE_VERSION,
    }


async def extract_and_store_project_lore(
    db: AsyncSession,
    *,
    chain: str = "base",
    token_id: str,
    ticker: str,
    name: str,
    launch: dict[str, Any] | None = None,
    dex: dict[str, Any] | None = None,
    social_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = build_lore_payload(
        chain=chain,
        token_id=token_id,
        ticker=ticker,
        name=name,
        launch=launch,
        dex=dex,
        social_evidence=social_evidence,
    )
    row = await upsert_project_lore(db, chain=chain, token_id=token_id, lore=payload, extraction_version=LORE_VERSION)
    payload["id"] = row.id
    return payload
