from __future__ import annotations

import re
from typing import Any


BLOCKED_USERNAMES = {
    "bankrbot",
    "binance",
    "bitstamp",
    "bybit_official",
    "coinbase",
    "cz_binance",
    "gate_io",
    "krakenfx",
    "okx",
    "watcherguru",
    "whale_alert",
}

ACCOUNT_TEXT_BLACKLIST = (
    "exchange",
    "listing",
    "cex",
    "dex listing",
)

SHILL_PATTERNS = (
    re.compile(r"\bbuy\s+(?:now\s+)?(?:on|at)\b", re.IGNORECASE),
    re.compile(r"\blisted\s+on\b", re.IGNORECASE),
    re.compile(r"\bnext\s+\d{2,4}x\b", re.IGNORECASE),
    re.compile(r"\bmoon\s+soon\b", re.IGNORECASE),
    re.compile(r"\bto\s+the\s+moon\b", re.IGNORECASE),
    re.compile(r"\b100x\b", re.IGNORECASE),
    re.compile(r"\btop\s+call\b", re.IGNORECASE),
    re.compile(r"\blast\s+chance\s+to\s+grab\b", re.IGNORECASE),
)

NON_ENGLISH_SCRIPT_RE = re.compile(
    r"[\u0400-\u04FF\u3040-\u30FF\u3400-\u9FFF\uAC00-\uD7AF]"
)
ENGLISH_WORD_RE = re.compile(r"[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://\S+")
CONTRACT_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
TIER_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3}


def normalize_username(username: str) -> str:
    return str(username or "").strip().lstrip("@").lower()


def is_blocked_account(tweet: dict[str, Any]) -> bool:
    username = normalize_username(tweet.get("username"))
    if username in BLOCKED_USERNAMES:
        return True

    profile_text = " ".join(
        str(tweet.get(key) or "")
        for key in ("name", "bio", "description", "user_description")
    ).lower()
    return any(term in profile_text for term in ACCOUNT_TEXT_BLACKLIST)


def is_shill_text(text: str) -> bool:
    clean = str(text or "")
    return any(pattern.search(clean) for pattern in SHILL_PATTERNS)


def is_english_tweet(text: str) -> bool:
    clean = CONTRACT_RE.sub(" ", URL_RE.sub(" ", str(text or "")))
    if len(NON_ENGLISH_SCRIPT_RE.findall(clean)) >= 2:
        return False

    letters = [char for char in clean if char.isalpha()]
    if not letters:
        return False
    ascii_letters = sum(1 for char in letters if char.isascii())
    if ascii_letters / max(len(letters), 1) < 0.9:
        return False
    return len(ENGLISH_WORD_RE.findall(clean)) >= 2


def tweet_rejection_reason(tweet: dict[str, Any]) -> str:
    text = str(tweet.get("text") or "")
    if not is_english_tweet(text):
        return "non_english"
    if is_blocked_account(tweet):
        return "blocked_account"
    if is_shill_text(text):
        return "shill_pattern"
    return ""


def passes_social_intelligence_filters(tweet: dict[str, Any]) -> bool:
    return not tweet_rejection_reason(tweet)


def tweet_tier_score(tweet: dict[str, Any]) -> int:
    text_len = len(str(tweet.get("text") or ""))
    likes = int(tweet.get("likes") or 0)
    retweets = int(tweet.get("retweets") or 0)
    replies = int(tweet.get("replies") or 0)
    views = int(tweet.get("views") or 0)
    total_engagements = likes + retweets + replies + (views / 10)
    return int(round((text_len * 0.6) + (total_engagements * 0.4)))


def tweet_tier(score: int) -> str:
    if score >= 850:
        return "S"
    if score >= 500:
        return "A"
    if score >= 250:
        return "B"
    return "C"


def tweet_tier_rank(tier: str) -> int:
    return TIER_ORDER.get(str(tier or "C").upper(), TIER_ORDER["C"])
