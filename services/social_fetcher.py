from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from hermes_skills.social_intelligence import tweet_tier, tweet_tier_score


TweetListFetcher = Callable[..., Awaitable[list[dict]]]


@dataclass(frozen=True)
class SmartFetchResult:
    latest: list[dict]
    top: list[dict]
    alpha_found: bool
    socialdata_called: bool
    alpha_reason: str


class NitterFetcher:
    def __init__(self, fetch_latest: TweetListFetcher):
        self._fetch_latest = fetch_latest

    async def latest(self, *, ticker: str, address: str = "", limit: int = 12, max_age_hours: int = 24) -> list[dict]:
        return await self._fetch_latest(
            ticker=ticker,
            address=address,
            limit=limit,
            max_age_hours=max_age_hours,
        )


class SocialDataFetcher:
    def __init__(self, fetch_top: TweetListFetcher):
        self._fetch_top = fetch_top

    async def top(
        self,
        *,
        ticker: str,
        address: str = "",
        limit: int = 24,
        max_age_hours: int = 24,
        min_count: int = 0,
    ) -> list[dict]:
        return await self._fetch_top(
            ticker=ticker,
            address=address,
            limit=limit,
            max_age_hours=max_age_hours,
            allow_tier3=True,
            min_count=min_count,
        )


class AlphaDetector:
    def __init__(self, *, min_followers: int = 5_000, min_thesis_quality: float = 5.0):
        self.min_followers = min_followers
        self.min_thesis_quality = min_thesis_quality

    def detect(self, tweets: list[dict]) -> tuple[bool, str]:
        for tweet in tweets:
            tier = tweet_tier(tweet_tier_score(tweet))
            if tier in {"S", "A"}:
                return True, f"tier_{tier.lower()}_tweet"
            followers = int(tweet.get("followers") or 0)
            thesis_quality = float(tweet.get("thesis_quality") or 0)
            if followers >= self.min_followers and thesis_quality >= self.min_thesis_quality:
                return True, "strong_account_thesis"
            text = str(tweet.get("text") or "").lower()
            if any(term in text for term in ("utility", "thesis", "workflow", "product", "traction", "narrative")):
                if int(tweet.get("likes") or 0) >= 5 or int(tweet.get("views") or 0) >= 500:
                    return True, "narrative_or_utility_signal"
        return False, "no_alpha_in_latest"


class SmartFetchOrchestrator:
    def __init__(
        self,
        *,
        nitter: NitterFetcher,
        socialdata: SocialDataFetcher,
        alpha_detector: AlphaDetector | None = None,
    ):
        self.nitter = nitter
        self.socialdata = socialdata
        self.alpha_detector = alpha_detector or AlphaDetector()

    async def fetch(
        self,
        *,
        ticker: str,
        address: str = "",
        latest_limit: int = 12,
        top_limit: int = 24,
        max_age_hours: int = 24,
        force_top: bool = False,
        top_min_count: int = 0,
    ) -> SmartFetchResult:
        latest = await self.nitter.latest(
            ticker=ticker,
            address=address,
            limit=latest_limit,
            max_age_hours=max_age_hours,
        )
        alpha_found, alpha_reason = self.alpha_detector.detect(latest)
        if not alpha_found and not force_top:
            return SmartFetchResult(
                latest=latest,
                top=[],
                alpha_found=False,
                socialdata_called=False,
                alpha_reason=alpha_reason,
            )

        top = await self.socialdata.top(
            ticker=ticker,
            address=address,
            limit=top_limit,
            max_age_hours=max_age_hours,
            min_count=top_min_count,
        )
        return SmartFetchResult(
            latest=latest,
            top=top,
            alpha_found=alpha_found,
            socialdata_called=True,
            alpha_reason=alpha_reason,
        )

