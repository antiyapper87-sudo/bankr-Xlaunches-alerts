from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from hermes_skills.social_intelligence import (
    is_english_tweet,
    passes_social_intelligence_filters,
    tweet_tier,
    tweet_tier_rank,
    tweet_tier_score,
)
from services.hermes_context import load_hermes_context
from services.tweet_provenance import annotate_tweet_source, ca_first_sort_key


VERSION = "hermes-social-v1"

UTILITY_TERMS = {
    "agent", "api", "app", "automation", "bot", "cloud", "data", "devtool",
    "framework", "inference", "infra", "mainnet", "protocol", "research",
    "robot", "scanner", "sdk", "terminal", "tool", "utility",
}
TECH_TERMS = {
    "ai", "compute", "model", "onchain", "platform", "scraping", "teleoperation",
    "workflow", "x402", "zk", "zero knowledge",
}
MARKET_TERMS = {
    "accumulation", "catalyst", "fees", "holders", "liquidity", "mcap",
    "revenue", "traction", "tvl", "undervalued", "volume", "whale",
}
MEME_TERMS = {"meme", "memecoin", "community", "cult", "mascot", "vibes"}
CONTRACT_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
ADDRESS_LABEL_RE = re.compile(
    r"\b(?:ca|contract|contract address|token address|address)\s*(?:[:：⋮|]+)\s*",
    re.IGNORECASE,
)
SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")
ENGLISH_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def _num(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _created_at(tweet: dict[str, Any]) -> datetime | None:
    value = tweet.get("created_at")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def is_recent_tweet(tweet: dict[str, Any], *, max_age_hours: int = 24) -> bool:
    created_at = _created_at(tweet)
    if not created_at:
        return True
    age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
    return 0 <= age_hours <= max_age_hours


def hide_contract_mentions(text: str, address: str = "") -> str:
    clean = str(text or "")
    if address:
        clean = re.sub(re.escape(address), "", clean, flags=re.IGNORECASE)
    clean = CONTRACT_RE.sub("", clean)
    clean = ADDRESS_LABEL_RE.sub("", clean)
    clean = re.sub(r"https?://t\.co/\S+", "", clean)
    clean = " ".join(clean.split())
    return clean


def strip_non_english_content(text: str) -> str:
    clean = str(text or "")
    chunks = re.split(r"(?<=[.!?])\s+|\n+", clean)
    kept: list[str] = []
    for chunk in chunks:
        ascii_chunk = "".join(char if char.isascii() else " " for char in chunk)
        ascii_chunk = " ".join(ascii_chunk.split())
        if len(ENGLISH_WORD_RE.findall(ascii_chunk)) >= 3:
            kept.append(ascii_chunk)
    return " ".join(kept)


def is_likely_english_text(text: str) -> bool:
    clean = strip_non_english_content(hide_contract_mentions(text))
    if not clean:
        return False
    return is_english_tweet(clean)


def compact_excerpt(text: str, limit: int = 260, address: str = "", min_sentences: int = 2) -> str:
    clean = strip_non_english_content(hide_contract_mentions(text, address))
    sentences = [sentence.strip() for sentence in SENTENCE_RE.findall(clean) if sentence.strip()]
    if len(sentences) >= min_sentences:
        clean = " ".join(sentences[:min_sentences])
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _term_hits(text_lower: str, terms: set[str]) -> int:
    return sum(1 for term in terms if term in text_lower)


def rank_tweet(tweet: dict[str, Any]) -> dict[str, Any]:
    text = str(tweet.get("text") or "")
    lower = text.lower()
    followers = _num(tweet.get("followers"))
    views = _num(tweet.get("views"))
    likes = _num(tweet.get("likes"))
    retweets = _num(tweet.get("retweets"))
    replies = _num(tweet.get("replies"))
    base_score = _num(tweet.get("score"))
    thesis_quality = float(tweet.get("thesis_quality") or 0)

    account_score = 0
    if followers >= 250_000:
        account_score = 20
    elif followers >= 100_000:
        account_score = 16
    elif followers >= 50_000:
        account_score = 13
    elif followers >= 10_000:
        account_score = 9
    elif followers >= 1_000:
        account_score = 5

    engagement_score = 0
    if views >= 100_000:
        engagement_score += 16
    elif views >= 25_000:
        engagement_score += 12
    elif views >= 5_000:
        engagement_score += 8
    elif views >= 1_000:
        engagement_score += 4
    engagement_score += 12 if likes >= 200 else 8 if likes >= 50 else 4 if likes >= 10 else 0
    engagement_score += 6 if retweets >= 50 else 3 if retweets >= 10 else 1 if retweets >= 3 else 0
    engagement_score += 3 if replies >= 20 else 1 if replies >= 5 else 0

    utility_hits = _term_hits(lower, UTILITY_TERMS)
    tech_hits = _term_hits(lower, TECH_TERMS)
    market_hits = _term_hits(lower, MARKET_TERMS)
    meme_hits = _term_hits(lower, MEME_TERMS)
    content_score = min(26, int(thesis_quality * 2) + utility_hits * 4 + tech_hits * 3 + market_hits * 2)
    if len(text.split()) >= 35:
        content_score += 4
    elif len(text.split()) >= 18:
        content_score += 2

    value_score = account_score + engagement_score + content_score + min(base_score, 20)
    if tweet.get("watched_influencer") or tweet.get("high_priority"):
        value_score += 8
    if meme_hits and not (utility_hits or tech_hits):
        value_score -= 5

    reasons: list[str] = []
    if utility_hits or tech_hits:
        reasons.append("utility/tech context")
    if market_hits:
        reasons.append("market/traction context")
    if followers >= 10_000:
        reasons.append("strong account")
    if views >= 1_000 or likes >= 10:
        reasons.append("engagement")
    if thesis_quality >= 5:
        reasons.append("thesis-rich")
    if not reasons:
        reasons.append("low-context mention")

    ranked = dict(tweet)
    tier_score = tweet_tier_score(ranked)
    tier = tweet_tier(tier_score)
    ranked["hermes_score"] = max(0, value_score)
    ranked["hermes_reason"] = ", ".join(reasons[:3])
    ranked["tweet_tier_score"] = tier_score
    ranked["tweet_tier"] = tier
    ranked["tweet_tier_rank"] = tweet_tier_rank(tier)
    ranked["signal_terms"] = {
        "utility": utility_hits,
        "tech": tech_hits,
        "market": market_hits,
        "meme": meme_hits,
    }
    return ranked


def infer_project_value(ranked: list[dict[str, Any]]) -> tuple[str, int, dict[str, int]]:
    totals = {"utility": 0, "tech": 0, "market": 0, "meme": 0}
    for item in ranked:
        terms = item.get("signal_terms") or {}
        for key in totals:
            totals[key] += int(terms.get(key) or 0)

    utility_score = totals["utility"] * 3 + totals["tech"] * 3 + totals["market"]
    meme_score = totals["meme"] * 2
    if utility_score >= 8:
        return "Utility / Tech", min(20, 10 + utility_score), totals
    if utility_score >= 4:
        return "Narrative / Community", min(14, 7 + utility_score), totals
    if meme_score > 0:
        return "Memecoin / Low-priority", max(2, min(8, 5 + meme_score - utility_score)), totals
    return "Unclear / Experimental", 5, totals


def build_thesis(project_value: str, ranked: list[dict[str, Any]], totals: dict[str, int]) -> str:
    if not ranked:
        return "No qualified CA-linked social evidence after spam and engagement filters."
    lead = ranked[0]
    author = lead.get("username") or "unknown"
    if project_value == "Utility / Tech":
        return (
            f"This has a real utility/tech angle, not just ticker noise. "
            f"The strongest CA-linked evidence is led by @{author} after English-only and spam filters."
        )
    if project_value == "Narrative / Community":
        return (
            f"There is early narrative/community traction, but the product edge is still unproven. "
            f"The evidence passed CA, engagement, English-only, and spam filters."
        )
    if project_value == "Memecoin / Low-priority":
        return (
            f"This still reads like a meme-first launch with limited durable substance. "
            f"Social proof exists, but it needs stronger creator, utility, or community evidence before priority improves."
        )
    return (
        f"Social evidence is real but thin on why this token should matter. "
        f"The filtered tweets exist, but the thesis is not strong yet."
    )


def build_value_assessment(project_value: str, ranked: list[dict[str, Any]], totals: dict[str, int]) -> str:
    if not ranked:
        return "No value case: Hermes found no qualified English tweets after CA, engagement, account, and shill filters."
    if project_value == "Utility / Tech":
        return "Value comes from utility/tech context in the strongest tweets. That deserves a closer look, but only if on-chain quality confirms it."
    if project_value == "Narrative / Community":
        return "Value comes from early attention around the narrative/community. It is watchable, but weak if creator proof and product context stay missing."
    if project_value == "Memecoin / Low-priority":
        return "Value is mostly speculative meme attention. That is low priority unless community evidence becomes unusually strong."
    return "Value is unclear. Mentions exist, but they do not explain a strong reason for the token to matter yet."


def build_social_score_breakdown(
    project_value: str,
    ranked: list[dict[str, Any]],
    project_value_score: int,
) -> dict[str, int]:
    top = ranked[:6]
    followers = max((_num(item.get("followers")) for item in top), default=0)
    replies = sum(_num(item.get("replies")) for item in top)
    shill_risk = 0
    if len(top) < 5:
        shill_risk += 5
    if project_value == "Memecoin / Low-priority":
        shill_risk += 2

    narrative = min(40, project_value_score * 2)
    creator = 0
    if followers >= 100_000:
        creator = 24
    elif followers >= 25_000:
        creator = 18
    elif followers >= 5_000:
        creator = 12
    elif followers >= 1_000:
        creator = 7

    utility = 0
    if project_value == "Utility / Tech":
        utility = 18
    elif project_value == "Narrative / Community":
        utility = 8
    elif project_value == "Memecoin / Low-priority":
        utility = 2
    else:
        utility = 4

    if replies >= 10:
        creator = min(30, creator + 4)
    elif replies >= 3:
        creator = min(30, creator + 2)

    return {
        "narrative": max(0, min(40, narrative)),
        "creator": max(0, min(30, creator)),
        "utility_tech": max(0, min(20, utility)),
        "shill_risk": max(0, min(10, shill_risk)),
    }


def social_score_from_breakdown(breakdown: dict[str, int]) -> int:
    return max(
        0,
        min(
            100,
            int(breakdown.get("narrative", 0))
            + int(breakdown.get("creator", 0))
            + int(breakdown.get("utility_tech", 0))
            - int(breakdown.get("shill_risk", 0)),
        ),
    )


def evidence_importance(item: dict[str, Any]) -> str:
    reason = str(item.get("hermes_reason") or "")
    views = _num(item.get("views"))
    likes = _num(item.get("likes"))
    followers = _num(item.get("followers"))
    if "utility/tech" in reason:
        return "utility context, not just hype"
    if "market/traction" in reason:
        return "market traction context"
    if followers >= 10_000 and views >= 1_000:
        return "credible account with real reach"
    if likes >= 25:
        return "engagement is above the minimum filter"
    return "qualified mention after spam filters"


def build_social_evidence(
    tweets: list[dict[str, Any]],
    *,
    ticker: str,
    address: str,
    min_count: int = 5,
    max_tweets: int = 24,
    max_age_hours: int = 24,
) -> dict[str, Any]:
    hermes_context = load_hermes_context()
    seen_urls: set[str] = set()
    ranked: list[dict[str, Any]] = []
    for tweet in tweets:
        url = str(tweet.get("url") or "")
        if not tweet or url in seen_urls or not is_recent_tweet(tweet, max_age_hours=max_age_hours):
            continue
        tweet = annotate_tweet_source(tweet, ticker=ticker, address=address, provider=tweet.get("source_provider") or tweet.get("source") or "")
        if address and not tweet.get("ca_confirmed"):
            continue
        if not passes_social_intelligence_filters(tweet):
            continue
        seen_urls.add(url)
        ranked.append(rank_tweet(tweet))

    ranked.sort(
        key=lambda item: (
            ca_first_sort_key(item)[0],
            -int(item.get("tweet_tier_rank") or 3),
            int(item.get("tweet_tier_score") or 0),
            int(item.get("hermes_score") or 0),
            int(item.get("views") or 0),
            int(item.get("likes") or 0),
            int(item.get("followers") or 0),
        ),
        reverse=True,
    )
    top = ranked[:max_tweets]
    project_value, project_value_score, totals = infer_project_value(top)
    thesis = build_thesis(project_value, top, totals)
    value_assessment = build_value_assessment(project_value, top, totals)
    score_breakdown = build_social_score_breakdown(project_value, top, project_value_score)
    social_score = social_score_from_breakdown(score_breakdown)
    evidence = []
    for idx, item in enumerate(top, 1):
        excerpt = compact_excerpt(item.get("text", ""), address=address)
        evidence.append(
            {
                "ref": idx,
                "url": item.get("url", ""),
                "username": item.get("username", ""),
                "followers": _num(item.get("followers")),
                "views": _num(item.get("views")),
                "likes": _num(item.get("likes")),
                "retweets": _num(item.get("retweets")),
                "score": _num(item.get("hermes_score")),
                "tweet_tier_score": _num(item.get("tweet_tier_score")),
                "tweet_tier": item.get("tweet_tier", "C"),
                "reason": item.get("hermes_reason", ""),
                "importance": evidence_importance(item),
                "excerpt": excerpt,
                "language": "en" if is_likely_english_text(excerpt) else "other",
                "source_match": item.get("source_match") or "",
                "source_matches": item.get("source_matches") or [],
                "source_provider": item.get("source_provider") or item.get("source") or "",
                "ca_confirmed": bool(item.get("ca_confirmed")),
                "ticker_confirmed": bool(item.get("ticker_confirmed")),
            }
        )
    qualified_count = sum(1 for item in top if _num(item.get("views")) >= 50 and _num(item.get("likes")) >= 5)

    return {
        "schema": "social-evidence-v1",
        "agent": {
            "name": "Hermes Agent",
            "mode": "deterministic_rules",
            "rules_version": VERSION,
            "rules_loaded": bool(hermes_context["loaded"]),
            "rules_digest": hermes_context["digest"],
            "rule_files": list(hermes_context["files"].keys()),
            "missing_rule_files": hermes_context["missing"],
        },
        "ticker": (ticker or "").lstrip("$").upper(),
        "address": (address or "").lower(),
        "qualified": qualified_count >= min_count,
        "qualified_tweets": qualified_count,
        "evidence_count": len(top),
        "min_required": min_count,
        "max_age_hours": max_age_hours,
        "project_value": project_value,
        "project_value_score": project_value_score,
        "value_assessment": value_assessment,
        "social_score": social_score,
        "score_breakdown": score_breakdown,
        "signal_terms": totals,
        "thesis": thesis,
        "top_tweets": evidence,
        "source_provenance": {
            "ca_confirmed": sum(1 for item in evidence if item.get("ca_confirmed")),
            "ticker_confirmed": sum(1 for item in evidence if item.get("ticker_confirmed")),
            "providers": sorted({str(item.get("source_provider") or "") for item in evidence if item.get("source_provider")}),
        },
    }
