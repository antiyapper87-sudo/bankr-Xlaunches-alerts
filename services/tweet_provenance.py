from __future__ import annotations

import re
from typing import Any


EVM_CA_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
EVM_CA_IN_TEXT_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
CASHTAG_RE = re.compile(r"\$([A-Za-z0-9]{2,10})\b")
AI_VERDICT_EVIDENCE_TYPES = {"ca_confirmed", "project_confirmed", "pair_confirmed"}
PAIR_LINK_TERMS = (
    "dexscreener.com/base",
    "geckoterminal.com/base",
    "gmgn.ai/base/token",
    "app.uniswap.org",
    "basescan.org/token",
)


def normalize_ca(value: str) -> str:
    value = str(value or "").strip().lower()
    return value if EVM_CA_RE.fullmatch(value) else ""


def normalize_ticker(value: str) -> str:
    return str(value or "").strip().lstrip("$").upper()


def strict_ca_query(address: str) -> str:
    ca = normalize_ca(address)
    return f'"{ca}"' if ca else ""


def strict_ticker_query(ticker: str) -> str:
    clean = normalize_ticker(ticker)
    return f"${clean}" if clean else ""


def tweet_source_matches(tweet: dict[str, Any], *, ticker: str = "", address: str = "") -> dict[str, Any]:
    text = str(tweet.get("text") or tweet.get("excerpt") or "")
    lower = text.lower()
    clean_ticker = normalize_ticker(ticker)
    clean_ca = normalize_ca(address)
    cashtags = {item.upper() for item in CASHTAG_RE.findall(text)}
    ca_mentions = {item.lower() for item in EVM_CA_IN_TEXT_RE.findall(text)}

    matches: list[str] = []
    if clean_ca and clean_ca in ca_mentions:
        matches.append("ca")
    if clean_ticker and clean_ticker in cashtags:
        matches.append("cashtag")

    return {
        "source_matches": matches,
        "source_match": matches[0] if matches else "",
        "ca_confirmed": "ca" in matches,
        "ticker_confirmed": "cashtag" in matches,
        "matched_cashtags": sorted(cashtags),
        "matched_contracts": sorted(ca_mentions),
    }


def classify_tweet_evidence(tweet: dict[str, Any], *, official_handles: set[str] | None = None) -> tuple[str, int]:
    official_handles = official_handles or set()
    username = str(tweet.get("username") or "").strip().lstrip("@").lower()
    text = str(tweet.get("text") or tweet.get("excerpt") or "").lower()
    if tweet.get("ca_confirmed"):
        return "ca_confirmed", 100
    if tweet.get("ticker_confirmed") and any(term in text for term in PAIR_LINK_TERMS):
        return "pair_confirmed", 80
    if username and username in official_handles and tweet.get("ticker_confirmed"):
        return "project_confirmed", 70
    if tweet.get("ticker_confirmed"):
        views = int(tweet.get("views") or 0)
        likes = int(tweet.get("likes") or 0)
        followers = int(tweet.get("followers") or 0)
        score = int(tweet.get("score") or tweet.get("tweet_tier_score") or 0)
        if views >= 1000 or likes >= 25 or followers >= 5000 or score >= 8:
            return "ticker_strong", 40
        return "ticker_only", 10
    return "unmatched", 0


def classify_evidence_type(tweet: dict[str, Any], *, official_handles: set[str] | None = None) -> tuple[str, int]:
    return classify_tweet_evidence(tweet, official_handles=official_handles)


def annotate_tweet_source(
    tweet: dict[str, Any],
    *,
    ticker: str = "",
    address: str = "",
    provider: str = "",
    official_handles: set[str] | None = None,
) -> dict[str, Any]:
    annotated = dict(tweet)
    provenance = tweet_source_matches(annotated, ticker=ticker, address=address)
    existing = list(annotated.get("source_matches") or [])
    for match in provenance["source_matches"]:
        if match not in existing:
            existing.append(match)
    annotated.update(provenance)
    annotated["source_matches"] = existing
    annotated["source_match"] = "ca" if "ca" in existing else "cashtag" if "cashtag" in existing else provenance["source_match"]
    annotated["ca_confirmed"] = "ca" in existing
    annotated["ticker_confirmed"] = "cashtag" in existing
    evidence_type, confidence_score = classify_evidence_type(annotated, official_handles=official_handles)
    annotated["evidence_type"] = evidence_type
    annotated["confidence_score"] = confidence_score
    annotated["ai_verdict_eligible"] = evidence_type in AI_VERDICT_EVIDENCE_TYPES
    if provider:
        annotated["source_provider"] = provider
    return annotated


def ca_first_sort_key(tweet: dict[str, Any]) -> tuple[int, int]:
    if tweet.get("ca_confirmed") or tweet.get("source_match") == "ca":
        return (2, 0)
    if tweet.get("ticker_confirmed") or tweet.get("source_match") == "cashtag":
        return (1, -1)
    return (0, -2)
