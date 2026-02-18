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
import json
from datetime import datetime, timezone
from pathlib import Path

# ─── Config from environment ──────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN", "")
WHATSAPP_GROUP_ID = os.getenv("WHATSAPP_GROUP_ID", "")
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

# ─── Blocklist (persists to file) ─────────────────────────────────────────────

BLOCKLIST_FILE = Path("/data/blocklist.json") if Path("/data").exists() else Path("blocklist.json")

def load_blocklist() -> set[str]:
    """Load blocked X usernames from file."""
    try:
        if BLOCKLIST_FILE.exists():
            with open(BLOCKLIST_FILE) as f:
                data = json.load(f)
                blocked = {u.lower().strip().lstrip("@") for u in data}
                log.info(f"🚫 Loaded {len(blocked)} blocked accounts")
                return blocked
    except Exception as e:
        log.error(f"Error loading blocklist: {e}")
    return set()

def save_blocklist(blocked: set[str]):
    """Save blocked X usernames to file."""
    try:
        BLOCKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(BLOCKLIST_FILE, "w") as f:
            json.dump(sorted(blocked), f, indent=2)
    except Exception as e:
        log.error(f"Error saving blocklist: {e}")

blocked_accounts: set[str] = load_blocklist()


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


# ─── WhatsApp via Whapi ───────────────────────────────────────────────────────

async def send_whatsapp(session: aiohttp.ClientSession, text: str) -> bool:
    """Send a message to WhatsApp group via Whapi."""
    if not WHAPI_TOKEN or not WHATSAPP_GROUP_ID:
        return False
    url = "https://gate.whapi.cloud/messages/text"
    headers = {
        "Authorization": f"Bearer {WHAPI_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": WHATSAPP_GROUP_ID,
        "body": text,
    }
    try:
        async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
            if resp.status in (200, 201):
                log.info("✅ WhatsApp alert sent")
                return True
            else:
                body = await resp.text()
                log.error(f"❌ WhatsApp error {resp.status}: {body}")
                return False
    except Exception as e:
        log.error(f"❌ WhatsApp send failed: {e}")
        return False


# ─── Send to all channels ────────────────────────────────────────────────────

async def send_alert(session: aiohttp.ClientSession, telegram_text: str, whatsapp_text: str):
    """Send alert to both Telegram and WhatsApp."""
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        await send_telegram(session, telegram_text)
    if WHAPI_TOKEN and WHATSAPP_GROUP_ID:
        await send_whatsapp(session, whatsapp_text)


# ─── Telegram Command Handler ─────────────────────────────────────────────────

last_update_id: int = 0

async def check_telegram_commands(session: aiohttp.ClientSession):
    """Poll Telegram for /block and /unblock commands."""
    global last_update_id, blocked_accounts

    if not TELEGRAM_BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 0, "limit": 10}

    try:
        async with session.get(url, params=params, timeout=5) as resp:
            if resp.status != 200:
                return
            data = await resp.json()
            if not data.get("ok"):
                return

            for update in data.get("result", []):
                last_update_id = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                # Only accept commands from our configured chat
                if chat_id != TELEGRAM_CHAT_ID:
                    continue

                # /block @username or /block username
                if text.lower().startswith("/block"):
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2:
                        await send_telegram(session, "Usage: /block @username")
                        continue
                    username = parts[1].strip().lstrip("@").lower()
                    blocked_accounts.add(username)
                    save_blocklist(blocked_accounts)
                    # Also clear from follower cache so we don't waste lookups
                    follower_cache.pop(username, None)
                    log.info(f"🚫 Blocked @{username}")
                    await send_telegram(session, f"🚫 Blocked <b>@{username}</b> — their launches will be ignored.\n\n{len(blocked_accounts)} accounts blocked total.")

                # /unblock @username
                elif text.lower().startswith("/unblock"):
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2:
                        await send_telegram(session, "Usage: /unblock @username")
                        continue
                    username = parts[1].strip().lstrip("@").lower()
                    blocked_accounts.discard(username)
                    save_blocklist(blocked_accounts)
                    log.info(f"✅ Unblocked @{username}")
                    await send_telegram(session, f"✅ Unblocked <b>@{username}</b>")

                # /blocklist — show all blocked accounts
                elif text.lower().startswith("/blocklist"):
                    if blocked_accounts:
                        names = "\n".join(f"• @{u}" for u in sorted(blocked_accounts))
                        await send_telegram(session, f"🚫 <b>Blocked accounts ({len(blocked_accounts)}):</b>\n{names}")
                    else:
                        await send_telegram(session, "No accounts blocked.")

                # /status
                elif text.lower().startswith("/status"):
                    await send_telegram(
                        session,
                        f"🐋 <b>Bankr Whale Alert Bot</b>\n"
                        f"• Tracking: {len(seen_tokens)} tokens seen\n"
                        f"• Blocked: {len(blocked_accounts)} accounts\n"
                        f"• Cached: {len(follower_cache)} follower lookups\n"
                        f"• Min followers: {MIN_FOLLOWERS:,}\n"
                        f"• Poll interval: {POLL_INTERVAL}s",
                    )

    except Exception as e:
        log.debug(f"Telegram command check error: {e}")


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

def format_alert(launch: dict, follower_count: int) -> tuple[str, str]:
    """Format alert messages for Telegram (HTML) and WhatsApp (plain text)."""
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

    # ── Telegram (HTML) ──
    tg_lines = [
        f"🐋 <b>WHALE LAUNCH DETECTED</b>",
        f"",
        f"🪙 <b>{name}</b> (${symbol})",
        f"👤 <a href='https://x.com/{x_username}'>@{x_username}</a> — <b>{followers_str} followers</b>",
        f"",
        f"📍 Chain: Base",
        f"📋 CA: <code>{address}</code>",
    ]

    if tweet_url:
        tg_lines.append(f"🐦 <a href='{tweet_url}'>Original Tweet</a>")

    if website:
        tg_lines.append(f"🌐 <a href='{website}'>Website</a>")

    if address:
        tg_lines.append(f"")
        tg_lines.append(f"📊 <a href='https://dexscreener.com/base/{address}'>DexScreener</a> | <a href='https://www.dextools.io/app/en/base/pair-explorer/{address}'>DexTools</a>")
        tg_lines.append(f"💰 <a href='https://app.uniswap.org/swap?outputCurrency={address}&chain=base'>Buy on Uniswap</a>")

    # ── WhatsApp (plain text) ──
    wa_lines = [
        f"🐋 *WHALE LAUNCH DETECTED*",
        f"",
        f"🪙 *{name}* (${symbol})",
        f"👤 @{x_username} — *{followers_str} followers*",
        f"https://x.com/{x_username}",
        f"",
        f"📍 Chain: Base",
        f"📋 CA: {address}",
    ]

    if tweet_url:
        wa_lines.append(f"🐦 Tweet: {tweet_url}")

    if website:
        wa_lines.append(f"🌐 {website}")

    if address:
        wa_lines.append(f"")
        wa_lines.append(f"📊 https://dexscreener.com/base/{address}")
        wa_lines.append(f"💰 https://app.uniswap.org/swap?outputCurrency={address}&chain=base")

    return "\n".join(tg_lines), "\n".join(wa_lines)


# ─── Main Loop ────────────────────────────────────────────────────────────────

async def poll_loop():
    """Main polling loop."""
    log.info("=" * 60)
    log.info("  🐋 Bankr Whale Alert Bot")
    log.info(f"  Min followers: {MIN_FOLLOWERS:,}")
    log.info(f"  Poll interval: {POLL_INTERVAL}s")
    log.info(f"  Telegram: {'✅ configured' if TELEGRAM_BOT_TOKEN else '❌ not configured'}")
    log.info(f"  WhatsApp: {'✅ configured' if WHAPI_TOKEN else '❌ not configured'}")
    log.info("=" * 60)

    async with aiohttp.ClientSession() as session:
        # Send startup message
        startup_tg = (
            f"🐋 <b>Bankr Whale Alert Bot started</b>\n"
            f"Monitoring for launches by accounts with {MIN_FOLLOWERS:,}+ followers\n"
            f"Polling every {POLL_INTERVAL}s"
        )
        startup_wa = (
            f"🐋 *Bankr Whale Alert Bot started*\n"
            f"Monitoring for launches by accounts with {MIN_FOLLOWERS:,}+ followers\n"
            f"Polling every {POLL_INTERVAL}s"
        )
        await send_alert(session, startup_tg, startup_wa)

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

                    # Check blocklist
                    if x_username.lower() in blocked_accounts:
                        log.info(f"  ${launch.get('tokenSymbol', '?')} by @{x_username} — BLOCKED, skipping")
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
                    tg_text, wa_text = format_alert(launch, follower_count)
                    await send_alert(session, tg_text, wa_text)

                    # Small delay between alerts to avoid Telegram rate limits
                    await asyncio.sleep(1)

                if new_count > 0:
                    log.info(f"🔍 Processed {new_count} new launches, {whale_count} whale alerts sent")
                else:
                    log.debug("No new launches this cycle")

            except Exception as e:
                log.error(f"Poll loop error: {e}", exc_info=True)

            # Check for Telegram commands (/block, /unblock, etc.)
            await check_telegram_commands(session)

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
