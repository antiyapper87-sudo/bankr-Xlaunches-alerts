from __future__ import annotations

import re
from typing import Any


EVM_CA_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
EVM_CA_IN_TEXT_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
CASHTAG_RE = re.compile(r"\$([A-Za-z0-9]{2,10})\b")


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


def annotate_tweet_source(tweet: dict[str, Any], *, ticker: str = "", address: str = "", provider: str = "") -> dict[str, Any]:
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
    if provider:
        annotated["source_provider"] = provider
    return annotated


def ca_first_sort_key(tweet: dict[str, Any]) -> tuple[int, int]:
    if tweet.get("ca_confirmed") or tweet.get("source_match") == "ca":
        return (2, 0)
    if tweet.get("ticker_confirmed") or tweet.get("source_match") == "cashtag":
        return (1, -1)
    return (0, -2)
