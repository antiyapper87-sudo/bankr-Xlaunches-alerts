from __future__ import annotations

import re
import html
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


Confidence = Literal["HIGH", "MEDIUM", "LOW"]


CONTRACT_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+\-/]{1,}")
GENERIC_PHRASES = (
    "early market traction",
    "differentiation is not proven",
    "product proof",
    "not confirmed",
    "no verified project description",
    "base launch",
)

NARRATIVE_GROUPS: dict[str, dict[str, Any]] = {
    "Privacy / Base infrastructure": {
        "terms": {
            "privacy", "private", "shielded", "anonymous", "anonymity", "confidential",
            "mev", "front-running", "frontrunning", "stealth", "zk", "zero knowledge",
            "mixer", "veil", "cash",
        },
        "why": "The strongest claim is privacy or shielded trading infrastructure, which is a real utility angle if the token-specific evidence holds.",
    },
    "AI / Agent": {
        "terms": {
            "ai", "agent", "autonomous", "automation", "model", "inference", "workflow",
            "llm", "compute", "robot", "assistant",
        },
        "why": "The narrative points to AI or agent utility; it needs proof beyond ticker hype and generic automation language.",
    },
    "Trading / Analytics Tool": {
        "terms": {
            "terminal", "scanner", "analytics", "signals", "trading", "research", "dashboard",
            "alerts", "data", "scraping", "portfolio",
        },
        "why": "The value case is tooling for traders or data workflows, which can matter if users and product proof are visible.",
    },
    "DeFi / Protocol": {
        "terms": {
            "defi", "protocol", "swap", "liquidity", "yield", "lending", "perps",
            "vault", "staking", "revenue", "fees",
        },
        "why": "The token is being framed as DeFi/protocol infrastructure, but execution and liquidity quality matter more than the label.",
    },
    "Launchpad / Creator Token": {
        "terms": {
            "launchpad", "creator", "socialfi", "community token", "fair launch",
            "creator token", "mint", "factory",
        },
        "why": "The value case depends on creator/community distribution, not just the presence of a fresh pair.",
    },
    "Game / Social App": {
        "terms": {
            "game", "gaming", "social app", "consumer", "app", "mobile", "quest",
            "play", "media",
        },
        "why": "This reads as a consumer narrative; it needs visible product or community proof to be more than a launch label.",
    },
    "Memecoin / Community": {
        "terms": {
            "meme", "memecoin", "community", "cult", "mascot", "character", "viral",
            "funny", "cto",
        },
        "why": "The value case is community attention, which is lower priority unless the hook and holder base become unusually strong.",
    },
}


@dataclass(slots=True)
class ProjectNarrative:
    ca: str
    ticker: str
    name: str = ""
    product: str = "No verified project description found."
    why_it_matters: str = "No reliable value case is confirmed from current sources."
    confidence: Confidence = "LOW"
    evidence_sources: list[str] = field(default_factory=list)
    raw_description: str = ""
    key_keywords: list[str] = field(default_factory=list)
    same_ticker_collision: bool = False
    is_ticker_only_evidence: bool = False
    ca_confirmed_mentions: int = 0
    qualified_mentions: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def clean_text(value: Any, *, limit: int = 360) -> str:
    text = CONTRACT_RE.sub("", str(value or ""))
    text = re.sub(r"https?://\S+", "", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def is_generic_description(text: str) -> bool:
    clean = clean_text(text).lower()
    if len(clean) < 30:
        return True
    if any(phrase in clean for phrase in GENERIC_PHRASES):
        return True
    words = WORD_RE.findall(clean)
    return len(words) < 6


def first_present(*values: Any) -> str:
    for value in values:
        clean = clean_text(value)
        if clean and not is_generic_description(clean):
            return clean
    return ""


def source_description(launch: dict[str, Any] | None, dex: dict[str, Any] | None) -> tuple[str, list[str]]:
    launch = launch or {}
    dex = dex or {}
    raw = launch.get("raw_json") if isinstance(launch.get("raw_json"), dict) else {}
    candidates = [
        (launch.get("description"), "DexScreener"),
        (raw.get("description"), "DexScreener"),
        (dex.get("description"), "DexScreener"),
        (dex.get("token_description"), "GeckoTerminal"),
        (dex.get("gecko_description"), "GeckoTerminal"),
    ]
    for value, source in candidates:
        clean = clean_text(value)
        if clean and not is_generic_description(clean):
            return clean, [source]
    return "", []


def tweet_text(tweet: dict[str, Any]) -> str:
    return clean_text(tweet.get("excerpt") or tweet.get("text") or "", limit=420)


def narrative_hits(text: str) -> dict[str, int]:
    lower = f" {text.lower()} "
    hits: dict[str, int] = {}
    for group, config in NARRATIVE_GROUPS.items():
        count = 0
        for term in config["terms"]:
            if f" {term} " in lower or term in lower:
                count += 1
        if count:
            hits[group] = count
    return hits


def best_narrative_group(text: str, tweets: list[dict[str, Any]]) -> tuple[str, list[str], int]:
    totals = narrative_hits(text)
    tweet_author_by_group: dict[str, set[str]] = {}
    for tweet in tweets:
        hits = narrative_hits(tweet_text(tweet))
        author = str(tweet.get("username") or "").lower()
        for group, count in hits.items():
            totals[group] = totals.get(group, 0) + count
            if author:
                tweet_author_by_group.setdefault(group, set()).add(author)
    if not totals:
        return "Unclear / Experimental", [], 0
    group = max(totals, key=lambda key: (totals[key], len(tweet_author_by_group.get(key, set()))))
    terms = sorted(NARRATIVE_GROUPS[group]["terms"], key=lambda term: (-text.lower().count(term), term))
    found = [term for term in terms if term in text.lower()]
    for tweet in tweets:
        lower = tweet_text(tweet).lower()
        for term in terms:
            if term in lower and term not in found:
                found.append(term)
    return group, found[:6], len(tweet_author_by_group.get(group, set()))


def ca_confirmed_tweets(tweets: list[dict[str, Any]], ca: str) -> int:
    if not ca:
        return 0
    needle = ca.lower()
    return sum(
        1 for tweet in tweets
        if tweet.get("ca_confirmed")
        or needle in str(tweet.get("text") or tweet.get("excerpt") or "").lower()
    )


def qualified_tweets(social_evidence: dict[str, Any] | None, tweets: list[dict[str, Any]]) -> int:
    if social_evidence and social_evidence.get("qualified_tweets") is not None:
        try:
            return int(social_evidence.get("qualified_tweets") or 0)
        except (TypeError, ValueError):
            return 0
    return len(tweets)


def has_same_ticker_collision(social_evidence: dict[str, Any] | None, flags: list[str] | None = None) -> bool:
    flags = flags or []
    text = " ".join(str(item) for item in flags).lower()
    if "ticker_collision" in text or "same_ticker" in text:
        return True
    if not social_evidence:
        return False
    risk_text = " ".join(
        str(social_evidence.get(key) or "")
        for key in ("thesis", "value_assessment", "risk", "risks", "alpha_reason")
    ).lower()
    return "ticker collision" in risk_text or "same ticker" in risk_text


def product_line(group: str, name: str, desc: str, keywords: list[str], *, source_backed: bool) -> str:
    if desc:
        return clean_text(desc, limit=220)
    display = clean_text(name, limit=80) or "This token"
    if group == "Unclear / Experimental" or not source_backed:
        return "No verified project description found from screeners or qualified X evidence."
    keyword_part = ", ".join(keywords[:3])
    qualifier = "appears positioned as" if source_backed else "has weak ticker-only signs of"
    return f"{display} {qualifier} {group.lower()}" + (f" ({keyword_part})." if keyword_part else ".")


def confidence_for(
    *,
    has_description: bool,
    unique_authors_for_group: int,
    qualified_count: int,
    ca_mentions: int,
    ticker_only: bool,
    collision: bool,
) -> Confidence:
    if collision:
        return "LOW"
    if has_description and unique_authors_for_group >= 2:
        return "HIGH"
    if has_description and not ticker_only:
        return "MEDIUM"
    if unique_authors_for_group >= 3 and qualified_count >= 3 and not ticker_only:
        return "MEDIUM"
    if unique_authors_for_group >= 3 and qualified_count >= 5 and ca_mentions > 0:
        return "MEDIUM"
    return "LOW"


def extract_project_narrative(
    *,
    ca: str,
    ticker: str,
    name: str = "",
    launch: dict[str, Any] | None = None,
    dex: dict[str, Any] | None = None,
    social_evidence: dict[str, Any] | None = None,
    tweets: list[dict[str, Any]] | None = None,
    flags: list[str] | None = None,
) -> ProjectNarrative:
    tweets = tweets or list((social_evidence or {}).get("top_tweets") or [])
    desc, sources = source_description(launch, dex)
    text_pool = " ".join([desc, name, ticker] + [tweet_text(tweet) for tweet in tweets[:12]])
    group, keywords, unique_authors = best_narrative_group(text_pool, tweets)
    ca_mentions = ca_confirmed_tweets(tweets, ca)
    qualified_count = qualified_tweets(social_evidence, tweets)
    collision = has_same_ticker_collision(social_evidence, flags)
    ticker_only = bool(tweets and ca_mentions == 0 and not desc)
    has_description = bool(desc)
    confidence = confidence_for(
        has_description=has_description,
        unique_authors_for_group=unique_authors,
        qualified_count=qualified_count,
        ca_mentions=ca_mentions,
        ticker_only=ticker_only,
        collision=collision,
    )
    if tweets:
        sources.append("X")
    sources = sorted(set(sources), key=sources.index)

    if collision and ticker_only:
        product = "Possible same-ticker collision; project description is not safe to attribute to this contract."
        why = "Ticker-only evidence can belong to another token, so the current CA needs contract-specific proof."
    else:
        source_backed = has_description or unique_authors >= 2 or ca_mentions > 0
        product = product_line(group, name or ticker, desc, keywords, source_backed=source_backed)
        why = str(NARRATIVE_GROUPS.get(group, {}).get("why") or "Current sources do not explain a durable reason for the token to matter yet.")
        if ticker_only:
            why = "X evidence is ticker-only and not CA-confirmed; treat this narrative as weak until contract-specific proof appears."

    return ProjectNarrative(
        ca=ca.lower(),
        ticker=ticker.lstrip("$").upper(),
        name=name,
        product=product,
        why_it_matters=why,
        confidence=confidence,
        evidence_sources=sources,
        raw_description=desc,
        key_keywords=keywords,
        same_ticker_collision=collision,
        is_ticker_only_evidence=ticker_only,
        ca_confirmed_mentions=ca_mentions,
        qualified_mentions=qualified_count,
    )


def narrative_token_type(narrative: dict[str, Any] | ProjectNarrative | None, fallback: str = "Unclear / Experimental") -> str:
    data = narrative.to_dict() if isinstance(narrative, ProjectNarrative) else (narrative or {})
    product = str(data.get("product") or "").lower()
    keywords = " ".join(data.get("key_keywords") or []).lower()
    text = f"{product} {keywords}"
    if any(term in text for term in ("privacy", "private", "shielded", "mev", "zero knowledge", "zk")):
        return "Privacy / Base infrastructure"
    if any(term in text for term in ("ai", "agent", "automation", "model", "inference")):
        return "AI / Agent"
    if any(term in text for term in ("terminal", "scanner", "analytics", "trading", "tool")):
        return "Trading / Analytics Tool"
    if any(term in text for term in ("defi", "protocol", "swap", "liquidity", "yield")):
        return "DeFi / Protocol"
    if any(term in text for term in ("meme", "memecoin", "community", "cult")):
        return "Memecoin / Community"
    return fallback


def format_project_narrative_block(narrative: dict[str, Any] | ProjectNarrative | None) -> str:
    data = narrative.to_dict() if isinstance(narrative, ProjectNarrative) else (narrative or {})
    if not data:
        return ""
    sources = data.get("evidence_sources") or []
    evidence = " + ".join(sources[:3]) if sources else "not confirmed"
    if data.get("is_ticker_only_evidence"):
        evidence += " (ticker-only)"
    return (
        f"• <b>Product:</b> {html.escape(clean_text(data.get('product'), limit=220))}\n"
        f"• <b>Why value:</b> {html.escape(clean_text(data.get('why_it_matters'), limit=220))}\n"
        f"• <b>Confidence:</b> {html.escape(str(data.get('confidence') or 'LOW'))} · {html.escape(evidence)}"
    )
