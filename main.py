"""
Bankr Whale Alert Bot
======================
Monitors https://api.bankr.bot/token-launches for new token launches.
When a token is launched by an X account with 10K+ followers → Telegram alert.

Deploy: GitHub + Railway
"""

import asyncio
import aiohttp
import logging
import os
import time
import re
from datetime import datetime, timezone

# ─── Config from environment ──────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_FOLLOWERS = int(os.getenv("MIN_FOLLOWERS", "10000"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))  # seconds
BANKR_API_URL = "https://api.bankr.bot/token-launches"

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bankr-whale")

# ─── State ────────────────────────────────────────────────────────────────────

seen_tokens: set[str] = set()
follower_cache: dict[str, int | None] = {}  # xUsername -> follower count


# ─── Telegram ─────────────────────────────────────────────────────────────────

async def send_telegram(session: aiohttp.ClientSession, text: str) -> bool:
    """Send a message to Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            if resp.status == 200:
                log.info("✅ Telegram alert sent")
                return True
            else:
                body = await resp.text()
                log.error(f"❌ Telegram error {resp.status}: {body}")
                return False
    except Exception as e:
        log.error(f"❌ Telegram send failed: {e}")
        return False


# ─── X/Twitter Follower Lookup ────────────────────────────────────────────────

async def get_follower_count(session: aiohttp.ClientSession, username: str) -> int | None:
    """
    Get follower count for an X/Twitter username.
    Uses multiple free methods as fallbacks:
    1. Twitter syndication API (unofficial but reliable)
    2. Nitter instances
    3. Direct profile scrape
    Returns follower count or None if all methods fail.
    """
    if not username:
        return None

    username = username.strip().lstrip("@")

    # Check cache first
    if username.lower() in follower_cache:
        cached = follower_cache[username.lower()]
        log.debug(f"Cache hit for @{username}: {cached}")
        return cached

    count = None

    # Method 1: Twitter syndication/embed API
    count = await _try_syndication(session, username)

    # Method 2: Try scraping the X profile page
    if count is None:
        count = await _try_profile_scrape(session, username)

    # Method 3: Try Nitter
    if count is None:
        count = await _try_nitter(session, username)

    # Cache result (even None, to avoid hammering)
    follower_cache[username.lower()] = count

    if count is not None:
        log.info(f"@{username} → {count:,} followers")
    else:
        log.warning(f"@{username} → could not determine follower count")

    return count


async def _try_syndication(session: aiohttp.ClientSession, username: str) -> int | None:
    """Try Twitter's syndication/user endpoint."""
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
            # Look for follower count in the HTML
            # Pattern: "followers_count":12345 or similar
            match = re.search(r'"followers_count"\s*:\s*(\d+)', text)
            if match:
                return int(match.group(1))
            # Also try: "1.2M Followers" or "12.5K Followers" pattern
            match = re.search(r'([\d,.]+)\s*[KkMm]?\s*[Ff]ollowers', text)
            if match:
                return _parse_follower_string(match.group(0))
    except Exception:
        pass
    return None


async def _try_profile_scrape(session: aiohttp.ClientSession, username: str) -> int | None:
    """Try scraping follower count from X profile page."""
    url = f"https://x.com/{username}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with session.get(url, headers=headers, timeout=10, allow_redirects=True) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
            # Look for "followers_count" in embedded data
            match = re.search(r'"followers_count"\s*:\s*(\d+)', text)
            if match:
                return int(match.group(1))
    except Exception:
        pass
    return None


async def _try_nitter(session: aiohttp.ClientSession, username: str) -> int | None:
    """Try Nitter instances for follower count."""
    nitter_instances = [
        f"https://nitter.privacydev.net/{username}",
        f"https://nitter.poast.org/{username}",
    ]
    for url in nitter_instances:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(url, headers=headers, timeout=8) as resp:
                if resp.status != 200:
                    continue
                text = await resp.text()
                # Nitter shows followers as "123,456 Followers"
                match = re.search(r'class="profile-stat-num"[^>]*>([\d,]+)</span>\s*<span[^>]*>Followers', text)
                if match:
                    return int(match.group(1).replace(",", ""))
                # Alternative pattern
                match = re.search(r'([\d,]+)\s*Followers', text)
                if match:
                    return int(match.group(1).replace(",", ""))
        except Exception:
            continue
    return None


def _parse_follower_string(s: str) -> int | None:
    """Parse strings like '12.5K Followers', '1.2M Followers'."""
    match = re.search(r'([\d,.]+)\s*([KkMm]?)', s)
    if not match:
        return None
    num = float(match.group(1).replace(",", ""))
    suffix = match.group(2).upper()
    if suffix == "K":
        return int(num * 1_000)
    elif suffix == "M":
        return int(num * 1_000_000)
    return int(num)


# ─── Bankr API ────────────────────────────────────────────────────────────────

async def fetch_launches(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch latest token launches from Bankr API."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "application/json",
        "Referer": "https://bankr.bot/launches",
        "Origin": "https://bankr.bot",
    }
    try:
        async with session.get(BANKR_API_URL, headers=headers, timeout=15) as resp:
            if resp.status != 200:
                log.warning(f"Bankr API returned {resp.status}")
                return []
            data = await resp.json()
            launches = data.get("launches", data if isinstance(data, list) else [])
            return launches
    except Exception as e:
        log.error(f"Bankr API error: {e}")
        return []


# ─── Alert Formatting ─────────────────────────────────────────────────────────

def format_alert(launch: dict, follower_count: int) -> str:
    """Format a Telegram alert message."""
    name = launch.get("tokenName", "Unknown")
    symbol = launch.get("tokenSymbol", "Unknown")
    address = launch.get("tokenAddress", "")
    deployer = launch.get("deployer", {})
    x_username = deployer.get("xUsername", "")
    tweet_url = launch.get("tweetUrl", "")
    website = launch.get("websiteUrl", "")

    # Format follower count nicely
    if follower_count >= 1_000_000:
        followers_str = f"{follower_count / 1_000_000:.1f}M"
    elif follower_count >= 1_000:
        followers_str = f"{follower_count / 1_000:.1f}K"
    else:
        followers_str = str(follower_count)

    lines = [
        f"🐋 <b>WHALE LAUNCH DETECTED</b>",
        f"",
        f"🪙 <b>{name}</b> (${symbol})",
        f"👤 <a href='https://x.com/{x_username}'>@{x_username}</a> — <b>{followers_str} followers</b>",
        f"",
        f"📍 Chain: Base",
        f"📋 CA: <code>{address}</code>",
    ]

    if tweet_url:
        lines.append(f"🐦 <a href='{tweet_url}'>Original Tweet</a>")

    if website:
        lines.append(f"🌐 <a href='{website}'>Website</a>")

    # Trading links
    if address:
        lines.append(f"")
        lines.append(f"📊 <a href='https://dexscreener.com/base/{address}'>DexScreener</a> | <a href='https://www.dextools.io/app/en/base/pair-explorer/{address}'>DexTools</a>")
        lines.append(f"💰 <a href='https://app.uniswap.org/swap?outputCurrency={address}&chain=base'>Buy on Uniswap</a>")

    return "\n".join(lines)


# ─── Main Loop ────────────────────────────────────────────────────────────────

async def poll_loop():
    """Main polling loop."""
    log.info("=" * 60)
    log.info("  🐋 Bankr Whale Alert Bot")
    log.info(f"  Min followers: {MIN_FOLLOWERS:,}")
    log.info(f"  Poll interval: {POLL_INTERVAL}s")
    log.info(f"  Telegram: {'✅ configured' if TELEGRAM_BOT_TOKEN else '❌ not configured'}")
    log.info("=" * 60)

    async with aiohttp.ClientSession() as session:
        # Send startup message
        if TELEGRAM_BOT_TOKEN:
            await send_telegram(
                session,
                f"🐋 <b>Bankr Whale Alert Bot started</b>\n"
                f"Monitoring for launches by accounts with {MIN_FOLLOWERS:,}+ followers\n"
                f"Polling every {POLL_INTERVAL}s",
            )

        # Initial fetch to populate seen_tokens (don't alert on existing ones)
        log.info("📋 Initial fetch to seed seen tokens...")
        initial = await fetch_launches(session)
        for launch in initial:
            addr = launch.get("tokenAddress", "")
            if addr:
                seen_tokens.add(addr.lower())
        log.info(f"📋 Seeded {len(seen_tokens)} existing tokens")

        # Poll loop
        while True:
            try:
                launches = await fetch_launches(session)

                if not launches:
                    log.info("No launches returned, retrying...")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                new_count = 0
                whale_count = 0

                for launch in launches:
                    address = launch.get("tokenAddress", "")
                    if not address or address.lower() in seen_tokens:
                        continue

                    seen_tokens.add(address.lower())
                    new_count += 1

                    # Check if deployer has X account
                    deployer = launch.get("deployer", {})
                    x_username = deployer.get("xUsername")

                    if not x_username:
                        log.debug(f"  {launch.get('tokenSymbol', '?')} — no X account, skipping")
                        continue

                    # Check follower count
                    follower_count = await get_follower_count(session, x_username)

                    if follower_count is None:
                        log.info(f"  ${launch.get('tokenSymbol', '?')} by @{x_username} — followers unknown, skipping")
                        continue

                    if follower_count < MIN_FOLLOWERS:
                        log.info(f"  ${launch.get('tokenSymbol', '?')} by @{x_username} — {follower_count:,} followers (below {MIN_FOLLOWERS:,})")
                        continue

                    # 🐋 WHALE DETECTED!
                    whale_count += 1
                    log.info(f"  🐋 ${launch.get('tokenSymbol', '?')} by @{x_username} — {follower_count:,} followers — ALERT!")
                    alert_text = format_alert(launch, follower_count)
                    await send_telegram(session, alert_text)

                    # Small delay between alerts to avoid Telegram rate limits
                    await asyncio.sleep(1)

                if new_count > 0:
                    log.info(f"🔍 Processed {new_count} new launches, {whale_count} whale alerts sent")
                else:
                    log.debug("No new launches this cycle")

            except Exception as e:
                log.error(f"Poll loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL)


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    # Validate config
    if not TELEGRAM_BOT_TOKEN:
        log.error("❌ TELEGRAM_BOT_TOKEN not set!")
        log.error("   Create a bot via @BotFather on Telegram and set the token")
    if not TELEGRAM_CHAT_ID:
        log.error("❌ TELEGRAM_CHAT_ID not set!")
        log.error("   Send /start to your bot, then get chat ID via https://api.telegram.org/bot<TOKEN>/getUpdates")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("⚠️  Bot will run but cannot send alerts. Set env vars and restart.")

    asyncio.run(poll_loop())


if __name__ == "__main__":
    main()
