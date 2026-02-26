"""
Whale Alert Bot — Bankr + Clanker + Virtuals
========================================================
Monitors FOUR sources for new token launches on Base:
  1. Bankr API   — https://api.bankr.bot/token-launches
  2. Clanker API  — https://www.clanker.world/api/tokens
  3. Virtuals API — https://api2.virtuals.io/api/virtuals  (AI agent launches)
  4. GeckoTerminal — market data (MCap, Volume, Liquidity) — faster than DexScreener

When a token is launched by an X account with 10K+ followers → alerts to Telegram + WhatsApp.
Uses SocialData.tools API for reliable follower count lookups.
Uses GeckoTerminal for market data filtering (MCap, Volume, Liquidity).

Deploy: GitHub + Railway
"""

import asyncio
import aiohttp
import logging
import os
import re
import json
from pathlib import Path
from datetime import datetime, timezone

# ─── Config from environment ──────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN", "")
WHATSAPP_GROUP_ID = os.getenv("WHATSAPP_GROUP_ID", "")
SOCIALDATA_API_KEY = os.getenv("SOCIALDATA_API_KEY", "")
MIN_FOLLOWERS = int(os.getenv("MIN_FOLLOWERS", "5000"))
MIN_MCAP = int(os.getenv("MIN_MCAP", "50000"))
MIN_VOLUME_24H = int(os.getenv("MIN_VOLUME_24H", "50000"))
MIN_LIQUIDITY = int(os.getenv("MIN_LIQUIDITY", "30000"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

BANKR_API_URL = "https://api.bankr.bot/token-launches"
CLANKER_API_URL = "https://www.clanker.world/api/tokens"
VIRTUALS_API_URL = "https://api2.virtuals.io/api/virtuals"
GECKOTERMINAL_API_URL = "https://api.geckoterminal.com/api/v2"
SOCIALDATA_API_URL = "https://api.socialdata.tools/twitter/user"

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("whale-alert")

# ─── State ────────────────────────────────────────────────────────────────────

seen_tokens: set[str] = set()
follower_cache: dict[str, int | None] = {}
last_update_id: int = 0
alert_count: int = 0

# ─── Blocklist (persists to file) ─────────────────────────────────────────────

BLOCKLIST_FILE = Path("/data/blocklist.json") if Path("/data").exists() else Path("blocklist.json")


def load_blocklist() -> set[str]:
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
    try:
        BLOCKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(BLOCKLIST_FILE, "w") as f:
            json.dump(sorted(blocked), f, indent=2)
    except Exception as e:
        log.error(f"Error saving blocklist: {e}")


blocked_accounts: set[str] = load_blocklist()


# ─── Telegram ─────────────────────────────────────────────────────────────────

async def send_telegram(session: aiohttp.ClientSession, text: str) -> bool:
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
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return True
            else:
                body = await resp.text()
                log.error(f"Telegram error {resp.status}: {body[:200]}")
                return False
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


# ─── WhatsApp via Whapi ───────────────────────────────────────────────────────

async def send_whatsapp(session: aiohttp.ClientSession, text: str) -> bool:
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
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status in (200, 201):
                log.info("✅ WhatsApp alert sent")
                return True
            else:
                body = await resp.text()
                log.error(f"WhatsApp error {resp.status}: {body[:200]}")
                return False
    except Exception as e:
        log.error(f"WhatsApp send failed: {e}")
        return False


# ─── Send to all channels ────────────────────────────────────────────────────

async def send_alert_all(session: aiohttp.ClientSession, tg_text: str, wa_text: str):
    """Send alert to both Telegram and WhatsApp."""
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        await send_telegram(session, tg_text)
    if WHAPI_TOKEN and WHATSAPP_GROUP_ID:
        await send_whatsapp(session, wa_text)


# ─── Telegram Command Handler ─────────────────────────────────────────────────

async def handle_telegram_commands(session: aiohttp.ClientSession):
    global last_update_id, blocked_accounts
    if not TELEGRAM_BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 0, "limit": 10}

    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return
            data = await resp.json()

        for update in data.get("result", []):
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            if not text.startswith("/"):
                continue

            # /block @username
            if text.lower().startswith("/block") and not text.lower().startswith("/blocklist"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await send_telegram(session, "Usage: /block @username")
                    continue
                username = parts[1].strip().lstrip("@").lower()
                blocked_accounts.add(username)
                save_blocklist(blocked_accounts)
                follower_cache.pop(username, None)
                log.info(f"🚫 Blocked @{username}")
                await send_telegram(session, f"🚫 Blocked <b>@{username}</b> — future launches ignored")

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

            # /blocklist
            elif text.lower().startswith("/blocklist"):
                if blocked_accounts:
                    names = "\n".join(f"• @{u}" for u in sorted(blocked_accounts))
                    await send_telegram(session, f"🚫 <b>Blocked ({len(blocked_accounts)}):</b>\n{names}")
                else:
                    await send_telegram(session, "No accounts blocked.")

            # /status
            elif text.lower().startswith("/status"):
                wa_status = "✅" if WHAPI_TOKEN and WHATSAPP_GROUP_ID else "❌"
                await send_telegram(
                    session,
                    f"🐋 <b>Whale Alert Bot</b>\n\n"
                    f"• Sources: Bankr + Clanker + Virtuals\n"
                    f"• Tokens seen: {len(seen_tokens)}\n"
                    f"• Alerts sent: {alert_count}\n"
                    f"• Blocked: {len(blocked_accounts)} accounts\n"
                    f"• Cached followers: {len(follower_cache)}\n"
                    f"• Min followers: {MIN_FOLLOWERS:,}\n"
                    f"• Min MCap: ${MIN_MCAP:,}\n"
                    f"• Min Volume: ${MIN_VOLUME_24H:,}\n"
                    f"• Min Liquidity: ${MIN_LIQUIDITY:,}\n"
                    f"• WhatsApp: {wa_status}\n"
                    f"• Poll interval: {POLL_INTERVAL}s",
                )

    except Exception as e:
        log.debug(f"Telegram command check error: {e}")


# ─── SocialData.tools Follower Lookup ─────────────────────────────────────────

async def get_follower_count(session: aiohttp.ClientSession, username: str) -> int | None:
    """Look up X follower count via SocialData.tools API. Uses cache."""
    username = username.lstrip("@").strip().lower()
    if not username:
        return None

    if username in follower_cache:
        return follower_cache[username]

    if not SOCIALDATA_API_KEY:
        log.warning(f"@{username} → SOCIALDATA_API_KEY not set!")
        follower_cache[username] = None
        return None

    count = None

    try:
        url = f"{SOCIALDATA_API_URL}/{username}"
        headers = {
            "Authorization": f"Bearer {SOCIALDATA_API_KEY}",
            "Accept": "application/json",
        }
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                count = data.get("public_metrics", {}).get("followers_count")
                if count is None:
                    count = data.get("followers_count")
                if count is not None:
                    count = int(count)
                    log.info(f"@{username} → {count:,} followers")
                else:
                    log.warning(f"@{username} → no followers_count in response")
            elif resp.status == 404:
                log.info(f"@{username} → account not found (404)")
            elif resp.status == 429:
                log.warning(f"@{username} → rate limited (429), will retry later")
                return None
            else:
                body = await resp.text()
                log.warning(f"@{username} → SocialData API {resp.status}: {body[:100]}")
    except Exception as e:
        log.warning(f"@{username} → SocialData lookup error: {e}")
        return None

    follower_cache[username] = count
    return count


# ─── GeckoTerminal Market Data ────────────────────────────────────────────────

async def fetch_geckoterminal(session: aiohttp.ClientSession, token_address: str) -> dict | None:
    """Fetch market data from GeckoTerminal for a Base token. Much faster indexing than DexScreener."""
    try:
        # Include top_pools to get liquidity (reserve_in_usd) in the included array
        url = f"{GECKOTERMINAL_API_URL}/networks/base/tokens/{token_address}?include=top_pools"
        headers = {"Accept": "application/json;version=20230302"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 404:
                return None
            if resp.status == 429:
                log.warning("GeckoTerminal rate limited, backing off...")
                await asyncio.sleep(5)
                return None
            if resp.status != 200:
                log.debug(f"GeckoTerminal {resp.status} for {token_address[:10]}...")
                return None
            data = await resp.json()

        attrs = data.get("data", {}).get("attributes", {})
        if not attrs:
            return None

        fdv = float(attrs.get("fdv_usd") or 0)
        mcap = float(attrs.get("market_cap_usd") or 0) or fdv
        vol_24h = float(attrs.get("volume_usd", {}).get("h24") or 0)

        # Get liquidity from included top pool data (saves a 2nd API call)
        liquidity = 0.0
        included = data.get("included", [])
        for item in included:
            if item.get("type") == "pool":
                pool_attrs = item.get("attributes", {})
                reserve = float(pool_attrs.get("reserve_in_usd") or 0)
                if reserve > liquidity:
                    liquidity = reserve

        price_change_1h = float(attrs.get("price_change_percentage", {}).get("h1") or 0)
        price_change_24h = float(attrs.get("price_change_percentage", {}).get("h24") or 0)

        return {
            "mcap": mcap,
            "volume_24h": vol_24h,
            "liquidity": liquidity,
            "price_usd": attrs.get("price_usd", "0"),
            "price_change_1h": price_change_1h,
            "price_change_24h": price_change_24h,
            "pair_url": f"https://www.geckoterminal.com/base/tokens/{token_address}",
        }
    except Exception as e:
        log.debug(f"GeckoTerminal error for {token_address[:10]}...: {e}")
        return None


def passes_market_filters(dex: dict | None) -> tuple[bool, str]:
    """Check if token passes MCap/Volume/Liquidity filters."""
    if dex is None:
        return False, "no market data"

    mcap = dex.get("mcap", 0)
    vol = dex.get("volume_24h", 0)
    liq = dex.get("liquidity", 0)

    if mcap < MIN_MCAP:
        return False, f"mcap ${mcap:,.0f} < ${MIN_MCAP:,}"
    if vol < MIN_VOLUME_24H:
        return False, f"vol ${vol:,.0f} < ${MIN_VOLUME_24H:,}"
    if liq < MIN_LIQUIDITY:
        return False, f"liq ${liq:,.0f} < ${MIN_LIQUIDITY:,}"

    return True, ""


# ─── Bankr API ────────────────────────────────────────────────────────────────

async def fetch_bankr(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch & normalize launches from Bankr API."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://bankr.bot/launches",
        "Origin": "https://bankr.bot",
    }
    normalized = []
    try:
        async with session.get(BANKR_API_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                log.warning(f"Bankr API returned {resp.status}")
                return []
            data = await resp.json()

        launches = data.get("launches", data if isinstance(data, list) else [])
        log.info(f"Bankr: {len(launches)} launches fetched")

        for launch in launches:
            address = (launch.get("tokenAddress") or "").lower()
            if not address:
                continue

            deployer = launch.get("deployer", {}) or {}
            x_username = deployer.get("xUsername", "")

            normalized.append({
                "source": "bankr",
                "address": address,
                "name": launch.get("tokenName", "Unknown"),
                "symbol": launch.get("tokenSymbol", "?"),
                "x_username": x_username or "",
                "tweet_url": launch.get("tweetUrl", ""),
                "image_uri": launch.get("imageUri", ""),
            })

    except Exception as e:
        log.error(f"Bankr fetch error: {e}")

    return normalized


# ─── Clanker API ──────────────────────────────────────────────────────────────

async def fetch_clanker(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch & normalize launches from Clanker API."""
    normalized = []
    try:
        params = {"sort": "desc", "page": 1, "pageSize": 20}
        async with session.get(CLANKER_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                log.warning(f"Clanker API returned {resp.status}")
                return []
            data = await resp.json()

        tokens = data.get("data", data if isinstance(data, list) else [])
        log.info(f"Clanker: {len(tokens)} launches fetched")

        for token in tokens:
            address = (token.get("contract_address") or token.get("address") or "").lower()
            if not address:
                continue

            x_username = ""
            social_urls = token.get("socialMediaUrls", []) or []
            if isinstance(social_urls, list):
                for url in social_urls:
                    if isinstance(url, str) and ("twitter.com/" in url or "x.com/" in url):
                        match = re.search(r'(?:twitter\.com|x\.com)/(@?\w+)', url)
                        if match:
                            candidate = match.group(1).lstrip("@")
                            if candidate.lower() not in ("home", "explore", "search", "settings"):
                                x_username = candidate
                                break

            if not x_username:
                desc = token.get("description", "") or ""
                desc_match = re.search(r'@(\w{1,15})', desc)
                if desc_match:
                    candidate = desc_match.group(1)
                    if candidate.lower() not in ("clanker", "bankr", "bankrbot", "base"):
                        x_username = candidate

            normalized.append({
                "source": "clanker",
                "address": address,
                "name": token.get("name", "Unknown"),
                "symbol": token.get("symbol", token.get("ticker", "?")),
                "x_username": x_username or "",
                "tweet_url": "",
                "image_uri": "",
            })

    except Exception as e:
        log.error(f"Clanker fetch error: {e}")

    return normalized


# ─── Virtuals API ─────────────────────────────────────────────────────────────

async def fetch_virtuals(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch & normalize AI agent launches from Virtuals Protocol API."""
    normalized = []
    try:
        # Fetch newest sentient agents (status=5) and prototypes (status=3)
        for status in [5, 3]:
            params = {
                "filters[status]": status,
                "filters[factory][0]": "VIBES_BONDING_V2",
                "sort": "createdAt:desc",
                "populate[0]": "image",
                "pagination[page]": 1,
                "pagination[pageSize]": 20,
            }
            async with session.get(
                VIRTUALS_API_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    log.warning(f"Virtuals API (status={status}) returned {resp.status}")
                    continue
                data = await resp.json()

            agents = data.get("data", [])
            status_label = "sentient" if status == 5 else "prototype"
            log.info(f"Virtuals ({status_label}): {len(agents)} agents fetched")

            for agent in agents:
                address = (agent.get("tokenAddress") or "").lower()
                if not address:
                    continue

                # Extract X username — try both agent socials and creator socials
                x_username = ""

                # 1) Agent's own verified X
                agent_socials = agent.get("socials", {}) or {}
                verified_usernames = agent_socials.get("VERIFIED_USERNAMES", {}) or {}
                x_username = verified_usernames.get("TWITTER", "")

                # 2) Creator's verified X (fallback)
                creator_x = ""
                creator = agent.get("creator", {}) or {}
                creator_socials = creator.get("socials", {}) or {}
                creator_verified = creator_socials.get("VERIFIED_USERNAMES", {}) or {}
                creator_x = creator_verified.get("TWITTER", "")

                # Use agent X if available, else creator X
                if not x_username:
                    x_username = creator_x

                # Build tweet URL from video pitch if available
                tweet_url = ""
                video_pitch = agent_socials.get("VIDEO_PITCH", {}) or {}
                tweet_url = video_pitch.get("TWEET_URL", "")

                # Get image
                image = agent.get("image", {}) or {}
                image_uri = image.get("url", "")

                normalized.append({
                    "source": "virtuals",
                    "address": address,
                    "name": agent.get("name", "Unknown"),
                    "symbol": agent.get("symbol", "?"),
                    "x_username": x_username or "",
                    "creator_x": creator_x or "",
                    "tweet_url": tweet_url,
                    "image_uri": image_uri,
                    "virtuals_id": agent.get("id", ""),
                    "holder_count": agent.get("holderCount", 0),
                    "fdv_virtual": agent.get("fdvInVirtual", 0),
                    "liquidity_usd": agent.get("liquidityUsd", 0),
                })

    except Exception as e:
        log.error(f"Virtuals fetch error: {e}")

    return normalized


# ─── Alert Formatting ─────────────────────────────────────────────────────────

def fmt_usd(val) -> str:
    val = float(val or 0)
    if val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    elif val >= 1_000:
        return f"${val / 1_000:.1f}K"
    return f"${val:.0f}"


SOURCE_EMOJIS = {
    "bankr": "🏦",
    "clanker": "⚙️",
    "virtuals": "🤖",
}


def format_alert_telegram(launch: dict, follower_count: int, dex: dict | None) -> str:
    """Format a Telegram alert message (HTML)."""
    source = launch["source"].upper()
    name = launch["name"]
    symbol = launch["symbol"]
    address = launch["address"]
    x_username = launch["x_username"]
    tweet_url = launch.get("tweet_url", "")

    source_emoji = SOURCE_EMOJIS.get(launch["source"], "📡")

    # Extra info for Virtuals
    virtuals_line = ""
    if launch["source"] == "virtuals":
        creator_x = launch.get("creator_x", "")
        if creator_x and creator_x != x_username:
            virtuals_line = f"👷 Creator: <a href='https://x.com/{creator_x}'>@{creator_x}</a>\n"
        holders = launch.get("holder_count", 0)
        if holders:
            virtuals_line += f"👥 Holders: {holders:,}\n"

    market_lines = ""
    if dex:
        change_1h = dex.get("price_change_1h", 0)
        change_emoji = "🟢" if change_1h >= 0 else "🔴"
        market_lines = (
            f"\n📊 <b>Market Data:</b>\n"
            f"├ 💰 MCap: {fmt_usd(dex['mcap'])}\n"
            f"├ 💧 Liq: {fmt_usd(dex['liquidity'])}\n"
            f"├ 📈 Vol 24h: {fmt_usd(dex['volume_24h'])}\n"
            f"└ {change_emoji} 1h: {change_1h:+.1f}%\n"
        )

    links = [
        f"├ <a href='https://www.geckoterminal.com/base/tokens/{address}'>GeckoTerminal</a>",
        f"├ <a href='https://basescan.org/token/{address}'>BaseScan</a>",
        f"├ <a href='https://gmgn.ai/base/token/{address}'>GMGN</a>",
    ]

    # Source-specific links
    if launch["source"] == "virtuals":
        vid = launch.get("virtuals_id", "")
        if vid:
            links.append(f"├ <a href='https://app.virtuals.io/virtuals/{vid}'>Virtuals</a>")
    elif launch["source"] == "clanker":
        links.append(f"├ <a href='https://www.clanker.world/clanker/{address}'>Clanker</a>")

    if x_username:
        links.append(f"├ <a href='https://x.com/{x_username}'>𝕏 @{x_username}</a>")
    if tweet_url:
        links.append(f"├ <a href='{tweet_url}'>📝 Launch Tweet</a>")
    links.append(f"└ <a href='https://app.uniswap.org/swap?chain=base&amp;outputCurrency={address}'>Uniswap</a>")

    msg = (
        f"🐋 <b>WHALE LAUNCH ALERT</b>\n\n"
        f"<b>{name}</b> (${symbol})\n"
        f"{source_emoji} Via: <b>{source}</b>\n"
        f"👤 <a href='https://x.com/{x_username}'>@{x_username}</a> — <b>{follower_count:,}</b> followers\n"
        f"{virtuals_line}"
        f"{market_lines}\n"
        f"🔗 <b>Links:</b>\n" + "\n".join(links) +
        f"\n\n<code>{address}</code>"
    )

    return msg


def format_alert_whatsapp(launch: dict, follower_count: int, dex: dict | None) -> str:
    """Format a WhatsApp alert message (plain text with emojis)."""
    source = launch["source"].upper()
    name = launch["name"]
    symbol = launch["symbol"]
    address = launch["address"]
    x_username = launch["x_username"]
    tweet_url = launch.get("tweet_url", "")

    source_emoji = SOURCE_EMOJIS.get(launch["source"], "📡")

    if follower_count >= 1_000_000:
        f_str = f"{follower_count / 1_000_000:.1f}M"
    elif follower_count >= 1_000:
        f_str = f"{follower_count / 1_000:.1f}K"
    else:
        f_str = str(follower_count)

    lines = [
        f"🐋 *WHALE LAUNCH ALERT*",
        f"",
        f"*{name}* (${symbol})",
        f"{source_emoji} Via: *{source}*",
        f"👤 @{x_username} — *{f_str} followers*",
    ]

    # Extra info for Virtuals
    if launch["source"] == "virtuals":
        creator_x = launch.get("creator_x", "")
        if creator_x and creator_x != x_username:
            lines.append(f"👷 Creator: @{creator_x}")
        holders = launch.get("holder_count", 0)
        if holders:
            lines.append(f"👥 Holders: {holders:,}")

    if dex:
        change_1h = dex.get("price_change_1h", 0)
        change_emoji = "🟢" if change_1h >= 0 else "🔴"
        lines.extend([
            f"",
            f"📊 *Market Data:*",
            f"├ 💰 MCap: {fmt_usd(dex['mcap'])}",
            f"├ 💧 Liq: {fmt_usd(dex['liquidity'])}",
            f"├ 📈 Vol 24h: {fmt_usd(dex['volume_24h'])}",
            f"└ {change_emoji} 1h: {change_1h:+.1f}%",
        ])

    lines.extend([
        f"",
        f"🔗 *Links:*",
        f"├ GeckoTerminal: https://www.geckoterminal.com/base/tokens/{address}",
        f"├ GMGN: https://gmgn.ai/base/token/{address}",
    ])

    if launch["source"] == "virtuals":
        vid = launch.get("virtuals_id", "")
        if vid:
            lines.append(f"├ Virtuals: https://app.virtuals.io/virtuals/{vid}")
    elif launch["source"] == "clanker":
        lines.append(f"├ Clanker: https://www.clanker.world/clanker/{address}")

    lines.append(f"├ X: https://x.com/{x_username}")

    if tweet_url:
        lines.append(f"├ Tweet: {tweet_url}")

    lines.extend([
        f"└ Uniswap: https://app.uniswap.org/swap?chain=base&outputCurrency={address}",
        f"",
        f"{address}",
    ])

    return "\n".join(lines)


# ─── Seeding ──────────────────────────────────────────────────────────────────

async def seed_existing(session: aiohttp.ClientSession):
    """Fetch existing tokens on startup so we don't alert on old ones."""
    log.info("📋 Seeding existing tokens...")
    bankr = await fetch_bankr(session)
    clanker = await fetch_clanker(session)
    virtuals = await fetch_virtuals(session)

    for launch in bankr + clanker + virtuals:
        seen_tokens.add(launch["address"])

    log.info(
        f"📋 Seeded {len(seen_tokens)} tokens "
        f"(Bankr: {len(bankr)}, Clanker: {len(clanker)}, "
        f"Virtuals: {len(virtuals)})"
    )


# ─── Main Loop ────────────────────────────────────────────────────────────────

async def main():
    global alert_count

    # Validate config
    if not TELEGRAM_BOT_TOKEN:
        log.error("❌ TELEGRAM_BOT_TOKEN not set!")
    if not TELEGRAM_CHAT_ID:
        log.error("❌ TELEGRAM_CHAT_ID not set!")
    if not SOCIALDATA_API_KEY:
        log.error("❌ SOCIALDATA_API_KEY not set!")

    log.info("=" * 60)
    log.info("  🐋 Whale Alert Bot (Bankr + Clanker + Virtuals)")
    log.info(f"  Min followers : {MIN_FOLLOWERS:,}")
    log.info(f"  Min MCap      : ${MIN_MCAP:,}")
    log.info(f"  Min Volume 24h: ${MIN_VOLUME_24H:,}")
    log.info(f"  Min Liquidity : ${MIN_LIQUIDITY:,}")
    log.info(f"  Poll interval : {POLL_INTERVAL}s")
    log.info(f"  Telegram : {'✅' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else '❌'}")
    log.info(f"  WhatsApp : {'✅' if WHAPI_TOKEN and WHATSAPP_GROUP_ID else '❌'}")
    log.info(f"  SocialData: {'✅' if SOCIALDATA_API_KEY else '❌'}")
    log.info("=" * 60)

    async with aiohttp.ClientSession() as session:

        # Seed existing tokens
        await seed_existing(session)

        # Startup message
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            await send_telegram(
                session,
                f"🐋 <b>Whale Alert Bot started</b>\n\n"
                f"Sources: Bankr + Clanker + Virtuals\n"
                f"Market data: GeckoTerminal\n"
                f"Follower lookup: SocialData.tools\n"
                f"Min followers: {MIN_FOLLOWERS:,}\n"
                f"Min MCap: ${MIN_MCAP:,} · Vol: ${MIN_VOLUME_24H:,} · Liq: ${MIN_LIQUIDITY:,}\n"
                f"Blocked: {len(blocked_accounts)} accounts\n"
                f"WhatsApp: {'✅' if WHAPI_TOKEN else '❌'}\n"
                f"Polling every {POLL_INTERVAL}s\n\n"
                f"Commands: /block @user · /unblock @user · /blocklist · /status",
            )

        # Poll loop
        while True:
            try:
                # Check for Telegram commands
                await handle_telegram_commands(session)

                # Fetch from ALL sources
                bankr_launches = await fetch_bankr(session)
                clanker_launches = await fetch_clanker(session)
                virtuals_launches = await fetch_virtuals(session)

                all_launches = bankr_launches + clanker_launches + virtuals_launches
                new_count = 0
                whale_count = 0

                for launch in all_launches:
                    address = launch["address"]

                    if address in seen_tokens:
                        continue
                    seen_tokens.add(address)
                    new_count += 1

                    symbol = launch.get("symbol", "?")
                    source = launch.get("source", "?")

                    # ── STEP 1: Does the DEPLOYER have an X account? ──
                    # For Virtuals: use creator X only (not the agent/project X)
                    # For Bankr/Clanker: use deployer X as before

                    if source == "virtuals":
                        deployer_x = launch.get("creator_x", "")
                    else:
                        deployer_x = launch.get("x_username", "")

                    if not deployer_x:
                        log.info(f"  [{source}] ${symbol} — no deployer X, skip")
                        continue

                    # Blocked → skip
                    if deployer_x.lower() in blocked_accounts:
                        log.info(f"  [{source}] ${symbol} @{deployer_x} — BLOCKED, skip")
                        continue

                    # ── STEP 2: Market data filter (MCap/Volume/Liquidity) ──
                    dex = await fetch_geckoterminal(session, address)
                    passes, reason = passes_market_filters(dex)

                    if not passes:
                        log.info(f"  [{source}] ${symbol} @{deployer_x} — {reason}, skip")
                        continue

                    # ── STEP 3: Deployer follower count ──
                    followers = await get_follower_count(session, deployer_x)

                    if followers is None:
                        log.info(f"  [{source}] ${symbol} @{deployer_x} — followers unknown, skip")
                        continue

                    if followers < MIN_FOLLOWERS:
                        log.info(f"  [{source}] ${symbol} @{deployer_x} — {followers:,} followers < {MIN_FOLLOWERS:,}, skip")
                        continue

                    # ── STEP 4: All passed → send alert! ──
                    launch["x_username"] = deployer_x
                    whale_count += 1
                    alert_count += 1
                    tg_text = format_alert_telegram(launch, followers, dex)
                    wa_text = format_alert_whatsapp(launch, followers, dex)
                    log.info(f"  🚀 ALERT: [{source}] ${symbol} @{deployer_x} — {followers:,} followers, MCap {fmt_usd(dex['mcap'])}")
                    await send_alert_all(session, tg_text, wa_text)

                log.info(f"🔍 {new_count} new launches processed, {whale_count} alerts sent")

            except Exception as e:
                log.error(f"Poll loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
