from __future__ import annotations

import asyncio
import html
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp


AUTO_VERDICT_ENABLED = os.getenv("AUTO_VERDICT_ENABLED", "true").lower() == "true"
AUTO_VERDICT_TIMEOUT_SEC = float(os.getenv("AUTO_VERDICT_TIMEOUT_SEC", "12"))
AUTO_VERDICT_MAX_CONCURRENT = int(os.getenv("AUTO_VERDICT_MAX_CONCURRENT", "2"))


SearchMentionsFn = Callable[[aiohttp.ClientSession, str, str, str], Awaitable[list[dict]]]
SearchInfluencersFn = Callable[[aiohttp.ClientSession, str, str], Awaitable[list[dict]]]
ResolveDeployerFn = Callable[[aiohttp.ClientSession, str], Awaitable[dict]]
FmtUsdFn = Callable[[float], str]

_verdict_semaphore = asyncio.Semaphore(max(1, AUTO_VERDICT_MAX_CONCURRENT))
_verdict_cache: dict[str, tuple[float, dict]] = {}
_VERDICT_CACHE_TTL = 900


@dataclass(slots=True)
class ResearchDeps:
    search_mentions: SearchMentionsFn
    search_influencers: SearchInfluencersFn
    resolve_deployer: ResolveDeployerFn
    fmt_usd: FmtUsdFn


def _compact_num(n: int | float) -> str:
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return f"{int(n)}"


def _market_score(dex: dict | None) -> tuple[float, list[str], list[str]]:
    if not dex:
        return 0, [], ["no market data"]

    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []

    mcap = float(dex.get("mcap") or 0)
    volume = float(dex.get("volume_24h") or 0)
    liquidity = float(dex.get("liquidity") or 0)
    change_1h = float(dex.get("price_change_1h") or 0)
    created_at = float(dex.get("pair_created_at") or 0)

    if mcap >= 250_000:
        score += 1.5
        reasons.append("market cap established")
    elif mcap >= 50_000:
        score += 1.0
        reasons.append("market cap passed filter")
    elif mcap > 0:
        risks.append("low market cap")

    if volume >= 100_000:
        score += 1.5
        reasons.append("strong 24h volume")
    elif volume >= 30_000:
        score += 1.0
        reasons.append("volume passed filter")
    elif volume > 0:
        risks.append("low volume")

    if liquidity >= 75_000:
        score += 1.0
        reasons.append("decent liquidity")
    elif liquidity and liquidity < 30_000:
        risks.append("thin liquidity")

    if change_1h >= 100:
        score += 0.8
        reasons.append("strong 1h momentum")
    elif change_1h <= -40:
        risks.append("sharp 1h drawdown")

    if created_at:
        age_seconds = time.time() - created_at / 1000
        if age_seconds <= 3600:
            score += 0.5
            reasons.append("fresh launch")
        elif age_seconds > 4 * 3600:
            risks.append("older than launch window")

    return score, reasons, risks


def _social_score(mentions: list[dict], influencer_mentions: list[dict]) -> tuple[float, list[str], list[str]]:
    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []

    if mentions:
        top = mentions[0]
        top_followers = int(top.get("followers") or 0)
        top_score = int(top.get("score") or 0)
        if top_followers >= 100_000:
            score += 2.0
            reasons.append(f"top mention @{top.get('username')} {_compact_num(top_followers)} followers")
        elif top_followers >= 10_000:
            score += 1.4
            reasons.append(f"notable mention @{top.get('username')}")
        elif top_followers >= 1_000:
            score += 0.8
            reasons.append("some X traction")
        if top_score >= 12:
            score += 1.0
            reasons.append("high-signal tweet language")
        elif top_score >= 8:
            score += 0.6
    else:
        risks.append("no notable X mentions")

    if len(mentions) >= 3:
        score += 1.0
        reasons.append(f"{len(mentions)} notable X mentions")

    if influencer_mentions:
        score += min(2.0, 0.8 + 0.4 * len(influencer_mentions))
        first = influencer_mentions[0]
        reasons.append(f"watched influencer @{first.get('username')} mentioned it")

    if not mentions and not influencer_mentions:
        risks.append("no watched influencer coverage")

    return score, reasons, risks


def _deployer_score(deployer: dict | None) -> tuple[float, list[str], list[str]]:
    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []
    if not deployer:
        risks.append("deployer identity unresolved")
        return score, reasons, risks

    handle = deployer.get("x_username") or ""
    followers = deployer.get("follower_count")
    if handle:
        reasons.append(f"deployer @{handle}")
    if followers is None:
        risks.append("deployer follower count unknown")
    elif followers >= 100_000:
        score += 2.0
        reasons.append(f"deployer {_compact_num(followers)} followers")
    elif followers >= 10_000:
        score += 1.5
        reasons.append(f"deployer {_compact_num(followers)} followers")
    elif followers >= 5_000:
        score += 1.0
        reasons.append(f"deployer {_compact_num(followers)} followers")
    elif followers >= 1_000:
        score += 0.5
    else:
        risks.append("small deployer account")

    return score, reasons, risks


def _label(score: float) -> tuple[str, str]:
    if score >= 7:
        return "SOLID", "🟢"
    if score >= 4:
        return "MID", "🟡"
    return "WEAK", "🔴"


async def build_signal_verdict(
    session: aiohttp.ClientSession,
    launch: dict,
    dex: dict | None,
    deps: ResearchDeps,
) -> dict:
    address = (launch.get("address") or "").lower()
    if address in _verdict_cache:
        cached_at, cached = _verdict_cache[address]
        if time.time() - cached_at < _VERDICT_CACHE_TTL:
            return cached

    async with _verdict_semaphore:
        symbol = (launch.get("symbol") or "").lstrip("$")
        token_name = launch.get("name", "") or symbol
        deployer = None
        if address:
            deployer = await deps.resolve_deployer(session, address)

        mentions, influencer_mentions = await asyncio.gather(
            deps.search_mentions(session, symbol, token_name, address),
            deps.search_influencers(session, symbol, address),
        )
        mention_urls = {m.get("url") for m in mentions}
        influencer_mentions = [m for m in influencer_mentions if m.get("url") not in mention_urls]

        market_points, market_reasons, market_risks = _market_score(dex)
        deployer_points, deployer_reasons, deployer_risks = _deployer_score(deployer)
        social_points, social_reasons, social_risks = _social_score(mentions, influencer_mentions)

        score = round(min(10.0, market_points + deployer_points + social_points), 1)
        label, emoji = _label(score)
        reasons = (market_reasons + deployer_reasons + social_reasons)[:5]
        risks = (market_risks + deployer_risks + social_risks)[:4]

        verdict = {
            "token": {
                "address": address,
                "symbol": symbol,
                "name": token_name,
                "source": launch.get("source", ""),
            },
            "market": dex or {},
            "deployer": deployer or {},
            "social": {
                "notable_mentions": mentions,
                "watched_influencer_mentions": influencer_mentions,
            },
            "score": {
                "value": score,
                "label": label,
                "emoji": emoji,
                "reasons": reasons,
                "risk_flags": risks,
            },
            "llm": {
                "used": False,
                "provider": "stub",
                "summary": "",
            },
        }
        if address:
            _verdict_cache[address] = (time.time(), verdict)
        return verdict


async def build_signal_verdict_with_timeout(
    session: aiohttp.ClientSession,
    launch: dict,
    dex: dict | None,
    deps: ResearchDeps,
) -> dict | None:
    if not AUTO_VERDICT_ENABLED:
        return None
    try:
        return await asyncio.wait_for(
            build_signal_verdict(session, launch, dex, deps),
            timeout=AUTO_VERDICT_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None


def format_verdict_block(verdict: dict) -> str:
    score = verdict.get("score", {})
    value = score.get("value", 0)
    label = score.get("label", "WEAK")
    emoji = score.get("emoji", "🔴")
    lines = [f"🧠 <b>VERDICT: {emoji} {html.escape(str(label))}</b> ({value}/10)"]

    reasons = score.get("reasons") or []
    risks = score.get("risk_flags") or []
    if reasons:
        lines.append("├ ✅ " + "; ".join(html.escape(str(r)) for r in reasons[:3]))
    if risks:
        lines.append("├ ⚠️ " + "; ".join(html.escape(str(r)) for r in risks[:3]))

    social = verdict.get("social") or {}
    mentions = social.get("notable_mentions") or []
    watched = social.get("watched_influencer_mentions") or []
    if mentions:
        top = mentions[0]
        lines.append(
            f"├ 🐦 Top X: @{html.escape(str(top.get('username', '')))} "
            f"({_compact_num(top.get('followers') or 0)})"
        )
    if watched:
        top_watched = watched[0]
        lines.append(f"├ 👀 Watched: @{html.escape(str(top_watched.get('username', '')))}")

    llm = verdict.get("llm") or {}
    if llm.get("used"):
        lines.append("└ 🤖 AI summary enabled")
    else:
        lines.append("└ 🤖 AI: stub")

    return "\n".join(lines)
