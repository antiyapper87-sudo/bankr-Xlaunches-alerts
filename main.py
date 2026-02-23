"""
Whale Alert Bot — Bankr + Clanker
==================================
Monitors TWO sources for new token launches on Base:
  1. Bankr API  — https://api.bankr.bot/token-launches
  2. Clanker API — https://www.clanker.world/api/tokens

When a token is launched by an X account with 10K+ followers → alerts to
Telegram + WhatsApp.

Deduplicates by contract address so Bankr tokens (which also appear in
Clanker) aren't double-alerted.

Deploy: GitHub + Railway
"""

import asyncio
import aiohttp
import logging
import os
import re
import json
from datetime import datetime, timezone

# ─── Config from environment ──────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN", "")
WHATSAPP_GROUP_ID = os.getenv("WHATSAPP_GROUP_ID", "")
MIN_FOLLOWERS = int(os.getenv("MIN_FOLLOWERS", "10000"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))  # seconds

BANKR_API_URL = "https://api.bankr.bot/token-launches"
CLANKER_API_URL = "https://www.clanker.world/api/tokens"

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("whale-alert")

# ─── State ────────────────────────────────────────────────────────────────────

seen_tokens: set[str] = set()          # contract addresses (lowercase)
follower_cache: dict[str, int | None] = {}  # xUsername -> follower count
blocked_accounts: set[str] = set()     # blocked X usernames (lowercase)
BLOCKLIST_FILE = "/data/blocklist.json" if os.path.exists("/data") else "blocklist.json"


# ─── Blocklist persistence ────────────────────────────────────────────────────

def load_blocklist():
    global blocked_accounts
    try:
        with open(BLOCKLIST_FILE, "r") as f:
            blocked_accounts = set(json.load(f))
        log.info(f"📋 Loaded {len(blocked_accounts)} blocked accounts")
    except FileNotFoundError:
        blocked_accounts = set()
    except Exception as e:
        log.error(f"Error loading blocklist: {e}")
        blocked_accounts = set()


def save_blocklist():
    try:
        with open(BLOCKLIST_FILE, "w") as f:
            json.dump(list(blocked_accounts), f)
    except Exception as e:
        log.error(f"Error saving blocklist: {e}")


# ─── Telegram ─────────────────────────────────────────────────────────────────

async def send_telegram(session: aiohttp.ClientSession, text: str) -> bool:
    """Send a message to Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
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

async def send_alert(session: aiohttp.ClientSession, tg_text: str, wa_text: str):
    """Send alert to both Telegram and WhatsApp."""
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        await send_telegram(session, tg_text)
    if WHAPI_TOKEN and WHATSAPP_GROUP_ID:
        await send_whatsapp(session, wa_text)


# ─── Follower Count Lookup ────────────────────────────────────────────────────

async def get_follower_count(session: aiohttp.ClientSession, username: str) -> int | None:
    """Look up X follower count. Uses cache to avoid repeated lookups."""
    username = username.lstrip("@").strip()
    if not username:
        return None

    if username in follower_cache:
        return follower_cache[username]

    count = None

    # Method 1: Twitter syndication API (works sometimes)
    try:
        url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                text = await resp.text()
                # Look for follower count patterns
                patterns = [
                    r'"followers_count":(\d+)',
                    r'"followersCount":(\d+)',
                    r'followers_count&quot;:(\d+)',
                ]
                for pattern in patterns:
                    match = re.search(pattern, text)
                    if match:
                        count = int(match.group(1))
                        break
    except Exception:
        pass

    # Method 2: Try nitter instances
    if count is None:
        nitter_instances = [
            "https://nitter.privacydev.net",
            "https://nitter.poast.org",
        ]
        for instance in nitter_instances:
            try:
                url = f"{instance}/{username}"
                async with session.get(url, timeout=8, allow_redirects=True) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        match = re.search(r'class="profile-stat-num"[^>]*>([\d,]+)', text)
                        if match:
                            # The followers count is typically the 2nd stat
                            stats = re.findall(r'class="profile-stat-num"[^>]*>([\d,]+)', text)
                            if len(stats) >= 3:
                                count = int(stats[2].replace(",", ""))
                                break
            except Exception:
                continue

    if count is None:
        log.warning(f"@{username} → could not determine follower count")

    follower_cache[username] = count
    return count


# ─── Extract X username from various formats ──────────────────────────────────

def extract_x_username(urls: list[str] | None) -> str | None:
    """Extract X/Twitter username from a list of social media URLs."""
    if not urls:
        return None
    for url in urls:
        if not isinstance(url, str):
            continue
        # Match x.com/username or twitter.com/username
        match = re.search(r'(?:x\.com|twitter\.com)/(@?[\w]+)/?', url, re.IGNORECASE)
        if match:
            username = match.group(1).lstrip("@")
            # Skip generic pages
            if username.lower() not in ("home", "explore", "search", "settings", "i", "intent"):
                return username
    return None


# ─── Fetch Bankr launches ────────────────────────────────────────────────────

async def fetch_bankr(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch recent Bankr token launches and normalize them."""
    try:
        async with session.get(BANKR_API_URL, timeout=15) as resp:
            if resp.status != 200:
                log.error(f"Bankr API error: {resp.status}")
                return []
            data = await resp.json()

        launches = data if isinstance(data, list) else data.get("launches", data.get("tokens", []))
        normalized = []

        for launch in launches:
            address = launch.get("tokenAddress", launch.get("contractAddress", ""))
            if not address:
                continue

            deployer = launch.get("deployer", {})
            x_username = deployer.get("xUsername", "")

            normalized.append({
                "source": "bankr",
                "address": address.lower(),
                "name": launch.get("tokenName", "Unknown"),
                "symbol": launch.get("tokenSymbol", "?"),
                "x_username": x_username,
                "tweet_url": launch.get("tweetUrl", ""),
                "website": launch.get("websiteUrl", ""),
                "raw": launch,
            })

        return normalized

    except Exception as e:
        log.error(f"Bankr fetch error: {e}")
        return []


# ─── Fetch Clanker launches ──────────────────────────────────────────────────

async def fetch_clanker(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch recent Clanker token launches and normalize them."""
    try:
        params = {
            "sort": "desc",
            "page": "1",
            "pageSize": "50",
        }
        headers = {
            "User-Agent": "WhaleAlertBot/1.0",
            "Accept": "application/json",
        }
        async with session.get(CLANKER_API_URL, params=params, headers=headers, timeout=15) as resp:
            if resp.status != 200:
                log.error(f"Clanker API error: {resp.status}")
                return []
            data = await resp.json()

        # Clanker API returns either a list or {data: [...], ...}
        tokens = data if isinstance(data, list) else data.get("data", data.get("tokens", []))
        normalized = []

        for token in tokens:
            address = token.get("contract_address", token.get("address", token.get("tokenAddress", "")))
            if not address:
                continue

            # Extract X username from socialMediaUrls
            social_urls = token.get("socialMediaUrls", token.get("social_media_urls", []))
            if isinstance(social_urls, str):
                try:
                    social_urls = json.loads(social_urls)
                except:
                    social_urls = [social_urls]

            x_username = extract_x_username(social_urls)

            # Also check context field for platform info
            context = token.get("context", {})
            if not x_username and context:
                # If deployed via X/Twitter, context might have user info
                platform = context.get("platform", "")
                if platform and "twitter" in platform.lower() or "x.com" in str(platform).lower():
                    user_id = context.get("id", "")
                    if user_id:
                        x_username = str(user_id)

            # Also check if castHash or requestor has X link
            if not x_username:
                # Check description for X links
                desc = token.get("description", "") or ""
                desc_match = re.search(r'(?:x\.com|twitter\.com)/(@?[\w]+)/?', desc, re.IGNORECASE)
                if desc_match:
                    candidate = desc_match.group(1).lstrip("@")
                    if candidate.lower() not in ("home", "explore", "search"):
                        x_username = candidate

            # Determine the interface that deployed it
            deploy_interface = ""
            if context and isinstance(context, dict):
                deploy_interface = context.get("interface", "")

            normalized.append({
                "source": "clanker",
                "address": address.lower(),
                "name": token.get("name", "Unknown"),
                "symbol": token.get("symbol", token.get("ticker", "?")),
                "x_username": x_username or "",
                "tweet_url": "",  # Clanker doesn't have tweet URLs directly
                "website": "",
                "deploy_interface": deploy_interface,
                "clanker_url": f"https://clanker.world/clanker/{address}",
                "raw": token,
            })

        return normalized

    except Exception as e:
        log.error(f"Clanker fetch error: {e}")
        return []


# ─── Format Alert ─────────────────────────────────────────────────────────────

def format_alert(launch: dict, follower_count: int) -> tuple[str, str]:
    """Format alert messages for Telegram (HTML) and WhatsApp (plain text)."""
    name = launch["name"]
    symbol = launch["symbol"]
    address = launch["address"]
    x_username = launch["x_username"]
    source = launch["source"]
    tweet_url = launch.get("tweet_url", "")
    website = launch.get("website", "")
    clanker_url = launch.get("clanker_url", "")

    # Format follower count nicely
    if follower_count >= 1_000_000:
        followers_str = f"{follower_count / 1_000_000:.1f}M"
    elif follower_count >= 1_000:
        followers_str = f"{follower_count / 1_000:.1f}K"
    else:
        followers_str = str(follower_count)

    source_label = "🏦 Bankr" if source == "bankr" else "⚙️ Clanker"

    # ── Telegram (HTML) ──
    tg_lines = [
        f"🐋 <b>WHALE LAUNCH DETECTED</b>",
        f"",
        f"🪙 <b>{name}</b> (${symbol})",
        f"👤 <a href='https://x.com/{x_username}'>@{x_username}</a> — <b>{followers_str} followers</b>",
        f"🚀 Source: {source_label}",
        f"",
        f"📍 Chain: Base",
        f"📋 CA: <code>{address}</code>",
    ]

    if tweet_url:
        tg_lines.append(f"🐦 <a href='{tweet_url}'>Original Tweet</a>")
    if website:
        tg_lines.append(f"🌐 <a href='{website}'>Website</a>")
    if clanker_url:
        tg_lines.append(f"🔗 <a href='{clanker_url}'>Clanker Page</a>")

    if address:
        tg_lines.append(f"")
        tg_lines.append(f"📊 <a href='https://dexscreener.com/base/{address}'>DexScreener</a> | <a href='https://www.dextools.io/app/en/base/pair-explorer/{address}'>DexTools</a>")
        tg_lines.append(f"💰 <a href='https://app.uniswap.org/swap?outputCurrency={address}&chain=base'>Buy on Uniswap</a>")

    tg_text = "\n".join(tg_lines)

    # ── WhatsApp (plain text with emojis) ──
    wa_lines = [
        f"🐋 *WHALE LAUNCH DETECTED*",
        f"",
        f"🪙 *{name}* (${symbol})",
        f"👤 @{x_username} — *{followers_str} followers*",
        f"🚀 Source: {source_label}",
        f"",
        f"📍 Chain: Base",
        f"📋 CA: {address}",
    ]

    if tweet_url:
        wa_lines.append(f"🐦 Tweet: {tweet_url}")
    if clanker_url:
        wa_lines.append(f"🔗 Clanker: {clanker_url}")

    wa_lines.append(f"")
    wa_lines.append(f"📊 DexScreener: https://dexscreener.com/base/{address}")
    wa_lines.append(f"💰 Uniswap: https://app.uniswap.org/swap?outputCurrency={address}&chain=base")

    wa_text = "\n".join(wa_lines)

    return tg_text, wa_text


# ─── Telegram Command Handler ────────────────────────────────────────────────

async def handle_telegram_commands(session: aiohttp.ClientSession):
    """Check for Telegram commands (/block, /unblock, /status, /blocklist)."""
    if not TELEGRAM_BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": handle_telegram_commands.last_update_id + 1, "timeout": 0}

    try:
        async with session.get(url, params=params, timeout=10) as resp:
            if resp.status != 200:
                return
            data = await resp.json()

        for update in data.get("result", []):
            handle_telegram_commands.last_update_id = update["update_id"]
            message = update.get("message", {})
            text = message.get("text", "").strip()
            chat_id = str(message.get("chat", {}).get("id", ""))

            # Only respond to commands from our configured chat
            if chat_id != TELEGRAM_CHAT_ID:
                continue

            if text.startswith("/block "):
                username = text.replace("/block ", "").strip().lstrip("@").lower()
                if username:
                    blocked_accounts.add(username)
                    save_blocklist()
                    # Also clear from follower cache
                    follower_cache.pop(username, None)
                    await send_telegram(session, f"🚫 Blocked @{username} — no more alerts from this account.")

            elif text.startswith("/unblock "):
                username = text.replace("/unblock ", "").strip().lstrip("@").lower()
                if username in blocked_accounts:
                    blocked_accounts.discard(username)
                    save_blocklist()
                    await send_telegram(session, f"✅ Unblocked @{username}")
                else:
                    await send_telegram(session, f"@{username} was not in the blocklist.")

            elif text == "/blocklist":
                if blocked_accounts:
                    blocked_list = "\n".join(f"  • @{u}" for u in sorted(blocked_accounts))
                    await send_telegram(session, f"🚫 <b>Blocked accounts ({len(blocked_accounts)}):</b>\n{blocked_list}")
                else:
                    await send_telegram(session, "No blocked accounts.")

            elif text == "/status":
                await send_telegram(
                    session,
                    f"🐋 <b>Whale Alert Bot Status</b>\n"
                    f"Sources: Bankr + Clanker\n"
                    f"Seen tokens: {len(seen_tokens)}\n"
                    f"Cached followers: {len(follower_cache)}\n"
                    f"Blocked accounts: {len(blocked_accounts)}\n"
                    f"Min followers: {MIN_FOLLOWERS:,}\n"
                    f"Poll interval: {POLL_INTERVAL}s",
                )

    except Exception as e:
        log.debug(f"Command check error: {e}")

handle_telegram_commands.last_update_id = 0


# ─── Main Poll Loop ──────────────────────────────────────────────────────────

async def poll_loop():
    """Main loop: fetch from both sources, check followers, alert."""

    log.info("=" * 60)
    log.info("  🐋 Whale Alert Bot (Bankr + Clanker)")
    log.info(f"  Min followers: {MIN_FOLLOWERS:,}")
    log.info(f"  Poll interval: {POLL_INTERVAL}s")
    log.info(f"  Telegram: {'✅ configured' if TELEGRAM_BOT_TOKEN else '❌ not configured'}")
    log.info(f"  WhatsApp: {'✅ configured' if WHAPI_TOKEN else '❌ not configured'}")
    log.info("=" * 60)

    load_blocklist()

    async with aiohttp.ClientSession() as session:

        # ── Initial seed: fetch existing tokens so we don't alert on old ones ──
        log.info("📋 Initial fetch to seed seen tokens...")
        bankr_launches = await fetch_bankr(session)
        clanker_launches = await fetch_clanker(session)

        for launch in bankr_launches + clanker_launches:
            seen_tokens.add(launch["address"])

        log.info(f"📋 Seeded {len(seen_tokens)} existing tokens (Bankr: {len(bankr_launches)}, Clanker: {len(clanker_launches)})")

        # ── Send startup alert ──
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            await send_telegram(
                session,
                f"🐋 <b>Whale Alert Bot started</b>\n"
                f"Sources: Bankr + Clanker\n"
                f"Monitoring for launches by X accounts with {MIN_FOLLOWERS:,}+ followers\n"
                f"Blocked accounts: {len(blocked_accounts)}\n"
                f"Polling every {POLL_INTERVAL}s\n\n"
                f"Commands: /block @user · /unblock @user · /blocklist · /status",
            )

        # ── Poll loop ──
        while True:
            try:
                # Check for Telegram commands
                await handle_telegram_commands(session)

                # Fetch from both sources
                bankr_launches = await fetch_bankr(session)
                clanker_launches = await fetch_clanker(session)

                all_launches = bankr_launches + clanker_launches
                new_count = 0
                whale_count = 0

                for launch in all_launches:
                    address = launch["address"]

                    # Skip already seen
                    if address in seen_tokens:
                        continue

                    seen_tokens.add(address)
                    new_count += 1

                    x_username = launch["x_username"]
                    source = launch["source"]

                    # Skip if no X username
                    if not x_username:
                        log.info(f"  [{source}] ${launch['symbol']} — no X account linked, skipping")
                        continue

                    # Skip blocked accounts
                    if x_username.lower() in blocked_accounts:
                        log.info(f"  [{source}] ${launch['symbol']} by @{x_username} — BLOCKED, skipping")
                        continue

                    # Check follower count
                    follower_count = await get_follower_count(session, x_username)

                    if follower_count is None:
                        log.info(f"  [{source}] ${launch['symbol']} by @{x_username} — followers unknown, skipping")
                        continue

                    if follower_count < MIN_FOLLOWERS:
                        log.info(f"  [{source}] ${launch['symbol']} by @{x_username} — {follower_count:,} followers (below {MIN_FOLLOWERS:,})")
                        continue

                    # 🐋 WHALE DETECTED!
                    whale_count += 1
                    log.info(f"  🐋 [{source}] ${launch['symbol']} by @{x_username} — {follower_count:,} followers — ALERT!")
                    tg_text, wa_text = format_alert(launch, follower_count)
                    await send_alert(session, tg_text, wa_text)

                    # Small delay between alerts to avoid rate limits
                    await asyncio.sleep(1)

                if new_count > 0:
                    log.info(f"🔍 Processed {new_count} new launches, {whale_count} whale alerts sent")

            except Exception as e:
                log.error(f"Poll loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL)


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        log.error("❌ TELEGRAM_BOT_TOKEN not set!")
    if not TELEGRAM_CHAT_ID:
        log.error("❌ TELEGRAM_CHAT_ID not set!")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("⚠️  Bot will run but cannot send Telegram alerts.")
    if not WHAPI_TOKEN:
        log.info("ℹ️  WhatsApp not configured (optional)")

    asyncio.run(poll_loop())


if __name__ == "__main__":
    main()
