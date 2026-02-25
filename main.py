"""
Whale Alert Bot — Bankr + Clanker
==================================
Monitors TWO sources for new token launches on Base:
  1. Bankr API  — https://api.bankr.bot/token-launches
  2. Clanker API — https://www.clanker.world/api/tokens

When a token is launched by an X account with 10K+ followers → alerts to Telegram.
Uses SocialData.tools API for reliable follower count lookups.
Uses DexScreener for market data filtering (MCap, Volume, Liquidity).

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
SOCIALDATA_API_KEY = os.getenv("SOCIALDATA_API_KEY", "")
MIN_FOLLOWERS = int(os.getenv("MIN_FOLLOWERS", "10000"))
MIN_MCAP = int(os.getenv("MIN_MCAP", "50000"))
MIN_VOLUME_24H = int(os.getenv("MIN_VOLUME_24H", "50000"))
MIN_LIQUIDITY = int(os.getenv("MIN_LIQUIDITY", "30000"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

BANKR_API_URL = "https://api.bankr.bot/token-launches"
CLANKER_API_URL = "https://www.clanker.world/api/tokens"
DEXSCREENER_API_URL = "https://api.dexscreener.com/latest/dex/tokens"
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
                # Clear from follower cache too
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
                await send_telegram(
                    session,
                    f"🐋 <b>Whale Alert Bot</b>\n\n"
                    f"• Sources: Bankr + Clanker\n"
                    f"• Tokens seen: {len(seen_tokens)}\n"
                    f"• Alerts sent: {alert_count}\n"
                    f"• Blocked: {len(blocked_accounts)} accounts\n"
                    f"• Cached followers: {len(follower_cache)}\n"
                    f"• Min followers: {MIN_FOLLOWERS:,}\n"
                    f"• Min MCap: ${MIN_MCAP:,}\n"
                    f"• Min Volume: ${MIN_VOLUME_24H:,}\n"
                    f"• Min Liquidity: ${MIN_LIQUIDITY:,}\n"
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
                    # Try alternate response structure
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
                # Don't cache rate limits
                return None
            else:
                body = await resp.text()
                log.warning(f"@{username} → SocialData API {resp.status}: {body[:100]}")
    except Exception as e:
        log.warning(f"@{username} → SocialData lookup error: {e}")
        # Don't cache errors
        return None

    follower_cache[username] = count
    return count


# ─── DexScreener Market Data ─────────────────────────────────────────────────

async def fetch_dexscreener(session: aiohttp.ClientSession, token_address: str) -> dict | None:
    """Fetch market data from DexScreener for a Base token."""
    try:
        url = f"{DEXSCREENER_API_URL}/{token_address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        pairs = data.get("pairs", [])
        if not pairs:
            return None

        # Use the highest-liquidity pair
        pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        return {
            "mcap": float(pair.get("marketCap", 0) or pair.get("fdv", 0) or 0),
            "volume_24h": float(pair.get("volume", {}).get("h24", 0) or 0),
            "liquidity": float(pair.get("liquidity", {}).get("usd", 0) or 0),
            "price_usd": pair.get("priceUsd", "0"),
            "price_change_1h": float(pair.get("priceChange", {}).get("h1", 0) or 0),
            "price_change_24h": float(pair.get("priceChange", {}).get("h24", 0) or 0),
            "pair_url": pair.get("url", ""),
        }
    except Exception as e:
        log.debug(f"DexScreener error for {token_address[:10]}...: {e}")
        return None


def passes_market_filters(dex: dict | None) -> tuple[bool, str]:
    """Check if token passes MCap/Volume/Liquidity filters."""
    if dex is None:
        return False, "no DexScreener data"

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
        "User-Agent": "Mozilla/5.0",
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

            # Extract X username from socialMediaUrls
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

            # Also check description for @mentions if no social URL found
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


# ─── Alert Formatting ─────────────────────────────────────────────────────────

def fmt_usd(val) -> str:
    val = float(val or 0)
    if val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    elif val >= 1_000:
        return f"${val / 1_000:.1f}K"
    return f"${val:.0f}"


def format_alert(launch: dict, follower_count: int, dex: dict | None) -> str:
    """Format a Telegram alert message."""
    source = launch["source"].upper()
    name = launch["name"]
    symbol = launch["symbol"]
    address = launch["address"]
    x_username = launch["x_username"]
    tweet_url = launch.get("tweet_url", "")

    source_emoji = "🏦" if launch["source"] == "bankr" else "⚙️"

    # Market data section
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

    # Links
    links = [
        f"├ <a href='https://dexscreener.com/base/{address}'>DexScreener</a>",
        f"├ <a href='https://www.clanker.world/clanker/{address}'>Clanker</a>",
        f"├ <a href='https://basescan.org/token/{address}'>BaseScan</a>",
        f"├ <a href='https://gmgn.ai/base/token/{address}'>GMGN</a>",
    ]
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
        f"{market_lines}\n"
        f"🔗 <b>Links:</b>\n" + "\n".join(links) +
        f"\n\n<code>{address}</code>"
    )

    return msg


# ─── Seeding ──────────────────────────────────────────────────────────────────

async def seed_existing(session: aiohttp.ClientSession):
    """Fetch existing tokens on startup so we don't alert on old ones."""
    log.info("📋 Seeding existing tokens...")
    bankr = await fetch_bankr(session)
    clanker = await fetch_clanker(session)

    for launch in bankr + clanker:
        seen_tokens.add(launch["address"])

    log.info(f"📋 Seeded {len(seen_tokens)} tokens (Bankr: {len(bankr)}, Clanker: {len(clanker)})")


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
    log.info("  🐋 Whale Alert Bot (Bankr + Clanker)")
    log.info(f"  Min followers : {MIN_FOLLOWERS:,}")
    log.info(f"  Min MCap      : ${MIN_MCAP:,}")
    log.info(f"  Min Volume 24h: ${MIN_VOLUME_24H:,}")
    log.info(f"  Min Liquidity : ${MIN_LIQUIDITY:,}")
    log.info(f"  Poll interval : {POLL_INTERVAL}s")
    log.info(f"  Telegram : {'✅ configured' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else '❌ not set'}")
    log.info(f"  SocialData: {'✅ configured' if SOCIALDATA_API_KEY else '❌ not set'}")
    log.info("=" * 60)

    async with aiohttp.ClientSession() as session:

        # Seed existing tokens
        await seed_existing(session)

        # Startup message
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            await send_telegram(
                session,
                f"🐋 <b>Whale Alert Bot started</b>\n\n"
                f"Sources: Bankr + Clanker\n"
                f"Follower lookup: SocialData.tools\n"
                f"Min followers: {MIN_FOLLOWERS:,}\n"
                f"Min MCap: ${MIN_MCAP:,} · Vol: ${MIN_VOLUME_24H:,} · Liq: ${MIN_LIQUIDITY:,}\n"
                f"Blocked: {len(blocked_accounts)} accounts\n"
                f"Polling every {POLL_INTERVAL}s\n\n"
                f"Commands: /block @user · /unblock @user · /blocklist · /status",
            )

        # Poll loop
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

                    if address in seen_tokens:
                        continue
                    seen_tokens.add(address)
                    new_count += 1

                    x_username = launch.get("x_username", "")
                    symbol = launch.get("symbol", "?")
                    source = launch.get("source", "?")

                    # No X account → skip
                    if not x_username:
                        log.info(f"  [{source}] ${symbol} — no deployer X, skip")
                        continue

                    # Blocked → skip
                    if x_username.lower() in blocked_accounts:
                        log.info(f"  [{source}] ${symbol} @{x_username} — BLOCKED, skip")
                        continue

                    # Check follower count via SocialData
                    followers = await get_follower_count(session, x_username)

                    if followers is None:
                        log.info(f"  [{source}] ${symbol} @{x_username} — followers unknown, skip")
                        continue

                    if followers < MIN_FOLLOWERS:
                        log.info(f"  [{source}] ${symbol} @{x_username} — {followers:,} followers < {MIN_FOLLOWERS:,}, skip")
                        continue

                    # 🐋 Whale detected! Now check market data
                    log.info(f"  🐋 [{source}] ${symbol} @{x_username} — {followers:,} followers! Checking market data...")

                    dex = await fetch_dexscreener(session, address)
                    passes, reason = passes_market_filters(dex)

                    if not passes:
                        log.info(f"  [{source}] ${symbol} — whale but {reason}, skip")
                        continue

                    # 🚀 All filters passed — send alert!
                    whale_count += 1
                    alert_count += 1
                    alert_text = format_alert(launch, followers, dex)
                    log.info(f"  🚀 ALERT: [{source}] ${symbol} @{x_username} — {followers:,} followers, MCap {fmt_usd(dex['mcap'])}")
                    await send_telegram(session, alert_text)

                log.info(f"🔍 {new_count} new launches processed, {whale_count} alerts sent")

            except Exception as e:
                log.error(f"Poll loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
