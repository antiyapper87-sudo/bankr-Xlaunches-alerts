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
import time
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
MIN_VOLUME_24H = int(os.getenv("MIN_VOLUME_24H", "30000"))
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
signaled_tokens: set[str] = set()  # addresses we've already sent a signal for
follower_cache: dict[str, int | None] = {}
gecko_cache: dict[str, tuple[float, dict | None]] = {}  # address → (timestamp, result)
GECKO_CACHE_TTL_HIT = 120    # cache valid market data for 2 min
GECKO_CACHE_TTL_MISS = 60    # cache "no data" for 1 min (recheck soon for new tokens)
last_update_id: int = 0
alert_count: int = 0

# ─── Recheck queue: tokens that failed market filters on first pass ───────────
# {address: {"launch": launch_dict, "first_seen": timestamp, "checks": count}}
RECHECK_MAX_AGE = 3600       # recheck for up to 60 min after first seen
RECHECK_INTERVAL = 300       # recheck every 5 min
RECHECK_MAX_CHECKS = 12      # max 12 rechecks (60 min / 5 min)
RECHECK_MAX_QUEUE = 200      # max tokens in recheck queue (seeds + new)
recheck_queue: dict[str, dict] = {}

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

async def send_telegram(session: aiohttp.ClientSession, text: str, chat_id: str = "") -> bool:
    """Send a Telegram message. Uses TELEGRAM_CHAT_ID by default, or a specific chat_id."""
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target,
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
                log.error(f"Telegram error {resp.status} (chat {target}): {body[:200]}")
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
                log.warning(f"getUpdates returned {resp.status}")
                return
            data = await resp.json()

        updates = data.get("result", [])
        if updates:
            log.info(f"📬 Received {len(updates)} Telegram updates")

        for update in updates:
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            if not text.startswith("/"):
                continue

            # Log the chat ID for debugging
            log.info(f"📩 Command from chat {chat_id}: {text[:50]}")

            # /block @username
            if text.lower().startswith("/block") and not text.lower().startswith("/blocklist"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await send_telegram(session, "Usage: /block @username", chat_id)
                    continue
                username = parts[1].strip().lstrip("@").lower()
                blocked_accounts.add(username)
                save_blocklist(blocked_accounts)
                follower_cache.pop(username, None)
                log.info(f"🚫 Blocked @{username}")
                await send_telegram(session, f"🚫 Blocked <b>@{username}</b> — future launches ignored", chat_id)

            # /unblock @username
            elif text.lower().startswith("/unblock"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await send_telegram(session, "Usage: /unblock @username", chat_id)
                    continue
                username = parts[1].strip().lstrip("@").lower()
                blocked_accounts.discard(username)
                save_blocklist(blocked_accounts)
                log.info(f"✅ Unblocked @{username}")
                await send_telegram(session, f"✅ Unblocked <b>@{username}</b>", chat_id)

            # /blocklist
            elif text.lower().startswith("/blocklist"):
                if blocked_accounts:
                    names = "\n".join(f"• @{u}" for u in sorted(blocked_accounts))
                    await send_telegram(session, f"🚫 <b>Blocked ({len(blocked_accounts)}):</b>\n{names}", chat_id)
                else:
                    await send_telegram(session, "No accounts blocked.", chat_id)

            # /status
            elif text.lower().startswith("/status"):
                await send_telegram(
                    session,
                    f"📡 <b>Signal Bot</b>\n\n"
                    f"• Sources: Bankr + Clanker + Virtuals\n"
                    f"• Tokens seen: {len(seen_tokens)}\n"
                    f"• Signals sent: {alert_count}\n"
                    f"• Recheck queue: {len(recheck_queue)}\n"
                    f"• Blocked: {len(blocked_accounts)} accounts\n"
                    f"• Min MCap: ${MIN_MCAP:,}\n"
                    f"• Min Volume: ${MIN_VOLUME_24H:,}\n"
                    f"• Min Liquidity: ${MIN_LIQUIDITY:,}\n"
                    f"• Poll interval: {POLL_INTERVAL}s\n"
                    f"• Recheck: every {RECHECK_INTERVAL}s for {RECHECK_MAX_AGE//60}min",
                    chat_id,
                )

            # /research $TICKER or /r $TICKER — token intelligence brief
            elif text.lower().startswith("/research") or text.lower().startswith("/r "):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await send_telegram(session, "Usage: /research $TICKER or /research 0x...", chat_id)
                    continue
                ticker_query = parts[1].strip()
                await send_telegram(session, f"🔍 Researching <b>{ticker_query}</b>...", chat_id)
                try:
                    report = await research_token(session, ticker_query)
                    await send_telegram(session, report, chat_id)
                except Exception as re:
                    log.error(f"Research error for {ticker_query}: {re}")
                    await send_telegram(session, f"❌ Research failed for {ticker_query}: {str(re)[:100]}", chat_id)

    except Exception as e:
        log.warning(f"Telegram command check error: {e}")


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


# ─── DexScreener Market Data ─────────────────────────────────────────────────

DEXSCREENER_API_URL = "https://api.dexscreener.com"

async def fetch_geckoterminal(session: aiohttp.ClientSession, token_address: str) -> dict | None:
    """Fetch market data from DexScreener for a Base token. Cached."""
    token_address = token_address.lower()

    # Check cache first
    if token_address in gecko_cache:
        cached_time, cached_result = gecko_cache[token_address]
        ttl = GECKO_CACHE_TTL_HIT if cached_result else GECKO_CACHE_TTL_MISS
        if time.time() - cached_time < ttl:
            return cached_result

    try:
        url = f"{DEXSCREENER_API_URL}/token-pairs/v1/base/{token_address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 404:
                gecko_cache[token_address] = (time.time(), None)
                return None
            if resp.status == 429:
                log.warning("DexScreener rate limited, backing off...")
                await asyncio.sleep(2)
                gecko_cache[token_address] = (time.time(), None)
                return None
            if resp.status != 200:
                log.debug(f"DexScreener {resp.status} for {token_address[:10]}...")
                return None
            raw = await resp.json()

        # DexScreener can return a list of pairs directly, or a dict with "pairs" key
        if isinstance(raw, list):
            pairs = raw
        elif isinstance(raw, dict):
            pairs = raw.get("pairs") or raw.get("data") or []
            if not pairs and raw.get("schemaVersion"):
                pairs = raw.get("pairs", [])
        else:
            pairs = []

        if not pairs:
            log.debug(f"DexScreener no pairs for {token_address[:10]}... (raw type={type(raw).__name__})")
            gecko_cache[token_address] = (time.time(), None)
            return None

        # Pick the pair with highest liquidity
        best = None
        best_liq = -1
        for pair in pairs:
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            if liq > best_liq:
                best_liq = liq
                best = pair

        if not best:
            gecko_cache[token_address] = (time.time(), None)
            return None

        mcap = float(best.get("marketCap") or best.get("fdv") or 0)
        vol_24h = float((best.get("volume") or {}).get("h24") or 0)
        liquidity = float((best.get("liquidity") or {}).get("usd") or 0)
        price_change = best.get("priceChange") or {}
        price_change_1h = float(price_change.get("h1") or 0)
        price_change_24h = float(price_change.get("h24") or 0)

        result = {
            "mcap": mcap,
            "volume_24h": vol_24h,
            "liquidity": liquidity,
            "price_usd": best.get("priceUsd", "0"),
            "price_change_1h": price_change_1h,
            "price_change_24h": price_change_24h,
            "pair_url": best.get("url", f"https://dexscreener.com/base/{token_address}"),
        }
        gecko_cache[token_address] = (time.time(), result)
        return result
    except Exception as e:
        log.debug(f"DexScreener error for {token_address[:10]}...: {e}")
        gecko_cache[token_address] = (time.time(), None)
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


# ─── Token Research ───────────────────────────────────────────────────────────

async def search_x_mentions(session: aiohttp.ClientSession, ticker: str, token_name: str = "") -> list[dict]:
    """Search X for $TICKER mentions by notable accounts using SocialData search API."""
    if not SOCIALDATA_API_KEY:
        return []

    mentions = []
    try:
        # Search for $TICKER mentions with engagement, sorted by top/popular
        query = f"${ticker} min_faves:10"
        url = "https://api.socialdata.tools/twitter/search"
        headers = {
            "Authorization": f"Bearer {SOCIALDATA_API_KEY}",
            "Accept": "application/json",
        }
        params = {"query": query, "type": "Top"}

        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                log.debug(f"X search for ${ticker}: status {resp.status}")
                return []
            data = await resp.json()

        tweets = data.get("tweets", [])
        for tweet in tweets[:10]:
            user = tweet.get("user", {})
            followers = user.get("followers_count", 0)
            # Only include tweets from accounts with 10K+ followers
            if followers < 10000:
                continue
            mentions.append({
                "username": user.get("screen_name", ""),
                "name": user.get("name", ""),
                "followers": followers,
                "text": (tweet.get("full_text") or tweet.get("text") or "")[:300],
                "likes": tweet.get("favorite_count", 0),
                "retweets": tweet.get("retweet_count", 0),
                "date": tweet.get("tweet_created_at", "")[:10],
                "url": f"https://x.com/{user.get('screen_name', '')}/status/{tweet.get('id_str', '')}",
            })

        # Sort by followers descending
        mentions.sort(key=lambda m: m["followers"], reverse=True)

    except Exception as e:
        log.debug(f"X search error for ${ticker}: {e}")

    return mentions[:5]


async def resolve_deployer_x(session: aiohttp.ClientSession, address: str) -> dict:
    """
    Try to resolve the deployer's X account for a token.
    Returns dict with: x_username, farcaster_username, source_method, follower_count
    """
    result = {
        "x_username": "",
        "farcaster_username": "",
        "farcaster_display": "",
        "source_method": "",
        "follower_count": None,
    }

    # ── Method 1: Check Clanker /search-creator by token address ──
    # Clanker tokens have msg_sender wallet — look up via search-creator
    try:
        # First get the token's page to find the msg_sender
        url = f"https://www.clanker.world/api/tokens?sort=desc&page=1&pageSize=50"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                tokens = data.get("data", data if isinstance(data, list) else [])
                for token in tokens:
                    token_addr = (token.get("contract_address") or token.get("address") or "").lower()
                    if token_addr == address.lower():
                        msg_sender = token.get("msg_sender", "")
                        social_ctx = token.get("social_context", {}) or {}

                        # Extract X from socialMediaUrls
                        social_urls = token.get("socialMediaUrls", []) or []
                        if isinstance(social_urls, list):
                            for surl in social_urls:
                                if isinstance(surl, str) and ("twitter.com/" in surl or "x.com/" in surl):
                                    match = re.search(r'(?:twitter\.com|x\.com)/(@?\w+)', surl)
                                    if match:
                                        candidate = match.group(1).lstrip("@")
                                        if candidate.lower() not in ("home", "explore", "search", "settings"):
                                            result["x_username"] = candidate
                                            result["source_method"] = "clanker socialMediaUrls"
                                            break

                        # If no X from metadata, try wallet lookup via search-creator
                        if not result["x_username"] and msg_sender:
                            try:
                                creator_url = f"https://clanker.world/api/search-creator?q={msg_sender}&limit=1"
                                async with session.get(creator_url, timeout=aiohttp.ClientTimeout(total=10)) as cr:
                                    if cr.status == 200:
                                        creator_data = await cr.json()
                                        user = creator_data.get("user", {})
                                        if user:
                                            result["farcaster_username"] = user.get("username", "")
                                            result["farcaster_display"] = user.get("displayName", "")
                                            # Farcaster verified addresses may include X
                                            verified = user.get("verifiedAddresses", [])
                                            result["source_method"] = f"clanker search-creator (wallet {msg_sender[:10]}...)"
                            except Exception as e:
                                log.debug(f"Clanker search-creator error: {e}")

                        # Extract from description
                        if not result["x_username"]:
                            desc = token.get("description", "") or ""
                            # Check for "automated by @X" / "requested by @X" / "launched by @X"
                            auto_match = re.search(r'(?:automated|requested|launched|created|deployed)\s+by\s+@(\w{1,15})', desc, re.IGNORECASE)
                            if auto_match:
                                result["x_username"] = auto_match.group(1)
                                result["source_method"] = "clanker description (automated by)"
                            else:
                                # Generic @username in description
                                desc_match = re.search(r'@(\w{1,15})', desc)
                                if desc_match:
                                    candidate = desc_match.group(1)
                                    if candidate.lower() not in ("clanker", "bankr", "bankrbot", "base", "everyone"):
                                        result["x_username"] = candidate
                                        result["source_method"] = "clanker description"
                        break
    except Exception as e:
        log.debug(f"Clanker deployer lookup error: {e}")

    # ── Method 2: Check if Farcaster user has X via SocialData ──
    if not result["x_username"] and result["farcaster_username"]:
        # Try searching SocialData for the Farcaster username as an X handle
        try:
            fc_user = result["farcaster_username"]
            followers = await get_follower_count(session, fc_user)
            if followers and followers > 100:
                result["x_username"] = fc_user
                result["follower_count"] = followers
                result["source_method"] += " → X match via farcaster username"
        except Exception:
            pass

    # ── Method 3: Check Bankr tweetUrl for requester X account ──
    # (This runs during research if we stored the launch data)

    # ── Get follower count if we found an X username ──
    if result["x_username"] and result["follower_count"] is None:
        result["follower_count"] = await get_follower_count(session, result["x_username"])

    return result


async def research_token(session: aiohttp.ClientSession, query: str) -> str:
    """
    Research a token by ticker or address.
    Returns a formatted Telegram HTML report.
    """
    query = query.strip().lstrip("$").upper()
    if not query:
        return "Usage: /research $TICKER or /research 0x..."

    is_address = query.lower().startswith("0x") and len(query) == 42
    ticker = query

    # ── Step 1: Find the token on GeckoTerminal ──
    dex = None
    address = ""
    token_name = ""

    if is_address:
        address = query.lower()
        dex = await fetch_geckoterminal(session, address)
        if dex:
            # Try to get name/symbol from GeckoTerminal
            try:
                url = f"{GECKOTERMINAL_API_URL}/networks/base/tokens/{address}"
                headers = {"Accept": "application/json;version=20230302"}
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        attrs = data.get("data", {}).get("attributes", {})
                        token_name = attrs.get("name", "")
                        ticker = attrs.get("symbol", query).upper()
            except Exception:
                pass
    else:
        # Search GeckoTerminal by ticker
        try:
            url = f"{GECKOTERMINAL_API_URL}/search/pools?query={ticker}&network=base&page=1"
            headers = {"Accept": "application/json;version=20230302"}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pools = data.get("data", [])

                    # Score each pool: exact ticker match + highest volume
                    best_pool = None
                    best_score = -1

                    for pool in pools:
                        pool_attrs = pool.get("attributes", {})
                        pool_name = pool_attrs.get("name", "")  # e.g. "VIRTUAL / WETH"
                        vol_raw = pool_attrs.get("volume_usd") or {}
                        vol_24h = float(vol_raw.get("h24") or 0)

                        base_addr = pool.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
                        if base_addr:
                            base_addr = base_addr.replace("base_", "")
                        if not base_addr:
                            continue

                        # Extract base token symbol from pool name (before " / ")
                        base_symbol = pool_name.split(" / ")[0].strip().upper() if " / " in pool_name else ""

                        # Exact ticker match gets a huge bonus
                        score = vol_24h
                        if base_symbol == ticker:
                            score += 1_000_000_000  # exact match priority

                        if score > best_score:
                            best_score = score
                            best_pool = pool

                    if best_pool:
                        pool_attrs = best_pool.get("attributes", {})
                        pool_name = pool_attrs.get("name", "")
                        base_addr = best_pool.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
                        if base_addr:
                            address = base_addr.replace("base_", "")
                        token_name = pool_name.split(" / ")[0].strip() if " / " in pool_name else pool_name

        except Exception as e:
            log.debug(f"GeckoTerminal search error: {e}")

        if address:
            dex = await fetch_geckoterminal(session, address)

    # ── Step 2: Resolve deployer identity ──
    deployer_info = None
    if address:
        deployer_info = await resolve_deployer_x(session, address)

    # ── Step 3: Search X for $TICKER mentions ──
    x_mentions = await search_x_mentions(session, ticker, token_name)

    # ── Step 4: Check if token was seen by our bot ──
    was_alerted = address.lower() in seen_tokens if address else False

    # ── Build the report ──
    if not dex and not x_mentions:
        return (
            f"🔍 <b>No data found for ${ticker}</b>\n\n"
            f"No market data on Base or notable X mentions.\n"
            f"Token might be on another chain (Solana, ETH, etc).\n"
            f"Try the contract address: /research 0x..."
        )

    safe_name = (token_name or ticker).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    lines = [f"🔍 <b>Research: {safe_name}</b> (${ticker})\n"]

    if address:
        lines.append(f"📋 <code>{address}</code>\n")

    # Market data
    if dex:
        change_1h = dex.get("price_change_1h", 0)
        change_24h = dex.get("price_change_24h", 0)
        change_1h_emoji = "🟢" if change_1h >= 0 else "🔴"
        change_24h_emoji = "🟢" if change_24h >= 0 else "🔴"

        lines.extend([
            f"📊 <b>Market Data:</b>",
            f"├ 💰 MCap: {fmt_usd(dex['mcap'])}",
            f"├ 💧 Liquidity: {fmt_usd(dex['liquidity'])}",
            f"├ 📈 Volume 24h: {fmt_usd(dex['volume_24h'])}",
            f"├ 💵 Price: ${float(dex.get('price_usd') or 0):.6f}",
            f"├ {change_1h_emoji} 1h: {change_1h:+.1f}%",
            f"└ {change_24h_emoji} 24h: {change_24h:+.1f}%",
            "",
        ])

        # Quick filter check
        passes, reason = passes_market_filters(dex)
        if passes:
            lines.append("✅ Passes market filters (MCap/Vol/Liq)\n")
        else:
            lines.append(f"❌ Fails filter: {reason}\n")
    else:
        lines.append("⚠️ No market data found on GeckoTerminal\n")

    if was_alerted:
        lines.append("👀 Token seen by bot\n")

    # ── Deployer identity section ──
    if deployer_info:
        x_user = deployer_info.get("x_username", "")
        fc_user = deployer_info.get("farcaster_username", "")
        fc_display = deployer_info.get("farcaster_display", "")
        method = deployer_info.get("source_method", "")
        d_followers = deployer_info.get("follower_count")

        lines.append("👤 <b>Deployer Identity:</b>")

        if x_user:
            f_str = ""
            if d_followers is not None:
                if d_followers >= 1_000_000:
                    f_str = f" — <b>{d_followers/1_000_000:.1f}M followers</b>"
                elif d_followers >= 1_000:
                    f_str = f" — <b>{d_followers/1_000:.0f}K followers</b>"
                else:
                    f_str = f" — {d_followers:,} followers"

                # Flag whales
                if d_followers >= 10_000:
                    f_str += " 🐋"
                elif d_followers >= 5_000:
                    f_str += " 🔥"

            lines.append(f"├ 𝕏 <a href='https://x.com/{x_user}'>@{x_user}</a>{f_str}")
        else:
            lines.append("├ 𝕏 No X account found")

        if fc_user:
            display = f" ({fc_display})" if fc_display and fc_display != fc_user else ""
            lines.append(f"├ 🟣 Farcaster: @{fc_user}{display}")

        if method:
            lines.append(f"└ 🔎 Found via: {method}")
        else:
            lines.append(f"└ 🔎 No deployer info in API metadata")

        lines.append("")
    else:
        lines.append("👤 <b>Deployer:</b> Could not resolve\n")

    # X mentions
    if x_mentions:
        lines.append(f"🐦 <b>Notable X mentions (${ticker}):</b>")
        for m in x_mentions:
            f_count = m['followers']
            if f_count >= 1_000_000:
                f_str = f"{f_count/1_000_000:.1f}M"
            elif f_count >= 1_000:
                f_str = f"{f_count/1_000:.0f}K"
            else:
                f_str = str(f_count)

            # Clean up tweet text — remove t.co links, expand preview
            text_clean = re.sub(r'https?://t\.co/\S+', '', m['text']).strip()
            text_clean = text_clean.replace('\n', ' ').replace('  ', ' ')
            # Escape HTML entities to prevent Telegram parse errors
            text_clean = text_clean.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            if len(text_clean) > 280:
                text_clean = text_clean[:277] + "..."

            lines.extend([
                f"",
                f"├ <a href='{m['url']}'>@{m['username']}</a> ({f_str} followers) · {m['date']}",
                f"│ ❤️ {m['likes']} 🔁 {m['retweets']}",
                f"│ <i>{text_clean}</i>" if text_clean else f"│ <i>[media only]</i>",
            ])
        lines.append("")
    else:
        lines.append(f"\n🐦 No notable X mentions found for ${ticker}\n")

    # Links
    if address:
        lines.extend([
            f"🔗 <b>Links:</b>",
            f"├ <a href='https://www.geckoterminal.com/base/tokens/{address}'>GeckoTerminal</a>",
            f"├ <a href='https://basescan.org/token/{address}'>BaseScan</a>",
            f"├ <a href='https://gmgn.ai/base/token/{address}'>GMGN</a>",
            f"└ <a href='https://app.uniswap.org/swap?chain=base&outputCurrency={address}'>Uniswap</a>",
        ])

    return "\n".join(lines)


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

            # Debug: log deployer keys when X is missing but deployer exists
            if not x_username and deployer:
                dep_keys = {k: v for k, v in deployer.items() if v}
                if dep_keys:
                    sym = launch.get("tokenSymbol", "?")
                    log.debug(f"  [bankr] ${sym} deployer fields (no xUsername): {dep_keys}")

            normalized.append({
                "source": "bankr",
                "address": address,
                "name": launch.get("tokenName", "Unknown"),
                "symbol": launch.get("tokenSymbol", "?"),
                "x_username": x_username or "",
                "tweet_url": launch.get("tweetUrl", ""),
                "image_uri": launch.get("imageUri", ""),
                "created_at": launch.get("createdAt") or launch.get("launchedAt") or launch.get("created_at") or "",
            })

    except Exception as e:
        log.error(f"Bankr fetch error: {e}")

    return normalized


# ─── Clanker API ──────────────────────────────────────────────────────────────

async def fetch_clanker(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch & normalize launches from Clanker API."""
    normalized = []
    try:
        # Fetch 2 pages to catch more tokens (API may cap results)
        all_tokens = []
        for page in [1, 2]:
            params = {"sort": "desc", "page": page, "pageSize": 50}
            async with session.get(CLANKER_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.warning(f"Clanker API returned {resp.status}")
                    break
                data = await resp.json()

            tokens = data.get("data", data if isinstance(data, list) else [])
            all_tokens.extend(tokens)
            if len(tokens) < 10:  # no more pages
                break

        log.info(f"Clanker: {len(all_tokens)} launches fetched")

        for token in all_tokens:
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
                "created_at": token.get("created_at") or token.get("createdAt") or token.get("deployedAt") or "",
            })

    except Exception as e:
        log.error(f"Clanker fetch error: {e}")

    return normalized


# ─── Virtuals API ─────────────────────────────────────────────────────────────

async def fetch_virtuals(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch & normalize AI agent launches from Virtuals Protocol API."""
    normalized = []
    try:
        # Fetch newest sentient agents (status=5), prototypes (status=3), and all others
        for status in [5, 3, 1, 2, 4]:
            params = {
                "filters[status]": status,
                "sort": "createdAt:desc",
                "populate[0]": "image",
                "pagination[page]": 1,
                "pagination[pageSize]": 50,
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
            status_labels = {5: "sentient", 3: "prototype", 1: "init", 2: "pending", 4: "bonding"}
            status_label = status_labels.get(status, f"status-{status}")
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


def format_signal_telegram(launch: dict, dex: dict | None) -> str:
    """Format a compact signal message for Telegram (HTML)."""
    source = launch["source"].upper()
    name = launch["name"]
    symbol = launch["symbol"]
    address = launch["address"]
    x_username = launch.get("x_username", "")
    tweet_url = launch.get("tweet_url", "")

    source_emoji = SOURCE_EMOJIS.get(launch["source"], "📡")

    # Market data
    market_lines = ""
    if dex:
        change_1h = dex.get("price_change_1h", 0)
        change_emoji = "🟢" if change_1h >= 0 else "🔴"
        market_lines = (
            f"├ 💰 MCap: {fmt_usd(dex['mcap'])}\n"
            f"├ 📈 Vol: {fmt_usd(dex['volume_24h'])}\n"
            f"├ 💧 Liq: {fmt_usd(dex['liquidity'])}\n"
            f"└ {change_emoji} 1h: {change_1h:+.1f}%"
        )

    # Deployer line (if known)
    deployer_line = ""
    if x_username:
        deployer_line = f"👤 Deployer: <a href='https://x.com/{x_username}'>@{x_username}</a>\n"

    # Virtuals extra info
    if launch["source"] == "virtuals":
        creator_x = launch.get("creator_x", "")
        if creator_x and creator_x != x_username:
            deployer_line += f"👷 Creator: <a href='https://x.com/{creator_x}'>@{creator_x}</a>\n"
        holders = launch.get("holder_count", 0)
        if holders:
            deployer_line += f"👥 Holders: {holders:,}\n"

    # Links
    links = [
        f"├ <a href='https://www.geckoterminal.com/base/tokens/{address}'>Gecko</a>",
        f"├ <a href='https://gmgn.ai/base/token/{address}'>GMGN</a>",
    ]
    if launch["source"] == "clanker":
        links.append(f"├ <a href='https://www.clanker.world/clanker/{address}'>Clanker</a>")
    elif launch["source"] == "virtuals":
        vid = launch.get("virtuals_id", "")
        if vid:
            links.append(f"├ <a href='https://app.virtuals.io/virtuals/{vid}'>Virtuals</a>")
    if tweet_url:
        links.append(f"├ <a href='{tweet_url}'>📝 Tweet</a>")
    links.append(f"└ <a href='https://app.uniswap.org/swap?chain=base&amp;outputCurrency={address}'>Uniswap</a>")

    msg = (
        f"📡 <b>SIGNAL</b> {source_emoji} {source}\n\n"
        f"<b>{name}</b> (${symbol})\n"
        f"{deployer_line}"
        f"{market_lines}\n\n"
        f"🔗 " + " · ".join(links) + "\n\n"
        f"<code>{address}</code>\n"
        f"💡 /research {address}"
    )

    return msg



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
    """Fetch existing tokens on startup. Add all to seen, queue recent ones for recheck."""
    log.info("📋 Seeding existing tokens...")
    bankr = await fetch_bankr(session)
    clanker = await fetch_clanker(session)
    virtuals = await fetch_virtuals(session)

    all_launches = bankr + clanker + virtuals
    queued = 0

    for launch in all_launches:
        addr = launch["address"]
        seen_tokens.add(addr)

        # Add ALL fetched tokens to recheck queue (covers ~last 4 hours)
        # The recheck cycle will check market data and signal any that pass.
        # This avoids duplicate signals on restart since we don't signal here.
        if addr not in recheck_queue and len(recheck_queue) < RECHECK_MAX_QUEUE:
            recheck_queue[addr] = {
                "launch": launch,
                "first_seen": time.time(),
                "last_check": 0,  # force immediate check on first recheck cycle
                "checks": 0,
            }
            queued += 1

    log.info(
        f"📋 Seeded {len(seen_tokens)} tokens "
        f"(Bankr: {len(bankr)}, Clanker: {len(clanker)}, "
        f"Virtuals: {len(virtuals)}) — {queued} queued for recheck"
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

        # ── DexScreener health check ──
        try:
            test_addr = "0x532f27101965dd16442e59d40670faf5ebb142e4"  # BRETT on Base
            test_url = f"{DEXSCREENER_API_URL}/token-pairs/v1/base/{test_addr}"
            async with session.get(test_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                test_data = await resp.json()
                if isinstance(test_data, list) and len(test_data) > 0:
                    p = test_data[0]
                    log.info(f"✅ DexScreener OK — BRETT: ${float(p.get('priceUsd') or 0):.4f}, mcap ${float(p.get('marketCap') or 0):,.0f}")
                else:
                    log.warning(f"⚠️ DexScreener returned unexpected format: {type(test_data).__name__}, status {resp.status}")
                    if isinstance(test_data, dict):
                        log.warning(f"   Keys: {list(test_data.keys())[:10]}")
        except Exception as e:
            log.error(f"❌ DexScreener health check failed: {e}")

        # Seed existing tokens
        await seed_existing(session)

        # Startup message
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            await send_telegram(
                session,
                f"🐋 <b>Whale Alert Bot started</b>\n\n"
                f"Sources: Bankr + Clanker + Virtuals\n"
                f"Market data: DexScreener\n"
                f"Min MCap: ${MIN_MCAP:,} · Vol: ${MIN_VOLUME_24H:,} · Liq: ${MIN_LIQUIDITY:,}\n"
                f"Blocked: {len(blocked_accounts)} accounts\n"
                f"Polling every {POLL_INTERVAL}s\n\n"
                f"📡 Mode: Signal all tokens passing market filters\n"
                f"🔍 /research 0x... for deep deployer + X analysis\n\n"
                f"Commands: /research · /block · /unblock · /blocklist · /status",
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

                # Log per-source new vs seen
                for src_name, src_list in [("bankr", bankr_launches), ("clanker", clanker_launches), ("virtuals", virtuals_launches)]:
                    src_new = sum(1 for l in src_list if l["address"] not in seen_tokens)
                    if src_new == 0 and len(src_list) > 0:
                        log.info(f"  [{src_name}] {len(src_list)} fetched, all already seen")

                new_count = 0
                signal_count = 0

                for launch in all_launches:
                    address = launch["address"]

                    if address in seen_tokens:
                        continue
                    seen_tokens.add(address)
                    new_count += 1

                    symbol = launch.get("symbol", "?")
                    source = launch.get("source", "?")

                    # ── STEP 1: Market data filter (MCap/Volume/Liquidity) ──
                    # Clear gecko cache for this address so we get fresh data
                    gecko_cache.pop(address, None)
                    dex = await fetch_geckoterminal(session, address)
                    passes, reason = passes_market_filters(dex)

                    if not passes:
                        log.info(f"  [{source}] ${symbol} — {reason}, skip → recheck queue")
                        # Add to recheck queue instead of forgetting forever
                        if len(recheck_queue) < RECHECK_MAX_QUEUE:
                            recheck_queue[address] = {
                                "launch": launch,
                                "first_seen": time.time(),
                                "last_check": time.time(),
                                "checks": 1,
                            }
                        continue

                    # ── STEP 2: Grab deployer X if available (bonus, not required) ──
                    deployer_x = ""
                    if source == "virtuals":
                        deployer_x = launch.get("creator_x", "") or launch.get("x_username", "")
                    else:
                        deployer_x = launch.get("x_username", "")

                    # Store for signal display
                    launch["x_username"] = deployer_x

                    # ── STEP 3: Signal to Telegram ──
                    if address not in signaled_tokens:
                        signaled_tokens.add(address)
                        signal_count += 1
                        alert_count += 1
                        tg_text = format_signal_telegram(launch, dex)
                        log.info(f"  📡 SIGNAL: [{source}] ${symbol} MCap {fmt_usd(dex['mcap'])} Vol {fmt_usd(dex['volume_24h'])}{f' @{deployer_x}' if deployer_x else ''}")
                        await send_telegram(session, tg_text)

                # ── RECHECK QUEUE: re-evaluate tokens that failed earlier ──
                now = time.time()
                expired = []
                for addr, entry in recheck_queue.items():
                    age = now - entry["first_seen"]
                    since_last = now - entry["last_check"]

                    # Too old → remove
                    if age > RECHECK_MAX_AGE or entry["checks"] >= RECHECK_MAX_CHECKS:
                        expired.append(addr)
                        continue

                    # Not time yet → skip
                    if since_last < RECHECK_INTERVAL:
                        continue

                    launch = entry["launch"]
                    symbol = launch.get("symbol", "?")
                    source = launch.get("source", "?")

                    # Clear cache and re-fetch
                    gecko_cache.pop(addr, None)
                    dex = await fetch_geckoterminal(session, addr)
                    entry["last_check"] = time.time()
                    entry["checks"] += 1

                    passes, reason = passes_market_filters(dex)
                    if not passes:
                        log.info(f"  ♻️ [{source}] ${symbol} recheck #{entry['checks']} — {reason}, still waiting")
                        continue

                    # Passed! Signal it (if not already signaled)
                    if addr in signaled_tokens:
                        log.info(f"  ♻️ [{source}] ${symbol} passed but already signaled, skipping")
                        expired.append(addr)
                        continue

                    deployer_x = ""
                    if source == "virtuals":
                        deployer_x = launch.get("creator_x", "") or launch.get("x_username", "")
                    else:
                        deployer_x = launch.get("x_username", "")
                    launch["x_username"] = deployer_x

                    signaled_tokens.add(addr)
                    signal_count += 1
                    alert_count += 1
                    tg_text = format_signal_telegram(launch, dex)
                    log.info(f"  📡 RECHECK SIGNAL: [{source}] ${symbol} MCap {fmt_usd(dex['mcap'])} Vol {fmt_usd(dex['volume_24h'])}{f' @{deployer_x}' if deployer_x else ''}")
                    await send_telegram(session, tg_text)
                    expired.append(addr)  # remove from queue after signaling

                for addr in expired:
                    recheck_queue.pop(addr, None)

                recheck_log = f", {len(recheck_queue)} in recheck queue" if recheck_queue else ""
                log.info(f"🔍 {new_count} new launches processed, {signal_count} signals sent{recheck_log}")

            except Exception as e:
                log.error(f"Poll loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
