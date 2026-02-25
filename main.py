"""
Whale Alert Bot — Bankr + Clanker
==================================
Monitors TWO sources for new token launches on Base:
  1. Bankr API  — https://api.bankr.bot/token-launches  (via cloudscraper)
  2. Clanker API — https://www.clanker.world/api/tokens

Filters:
  - Launched BY an X account with MIN_FOLLOWERS+ followers
  - Market cap  >= MIN_MCAP   (checked via DexScreener, default 50000)
  - Volume 24h  >= MIN_VOLUME (checked via DexScreener, default 50000)
  - Liquidity   >= MIN_LIQ    (checked via DexScreener, default 30000)

Deploy: GitHub + Railway
Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
          WHAPI_TOKEN (optional), WHATSAPP_GROUP_ID (optional),
          MIN_FOLLOWERS, MIN_MCAP, MIN_VOLUME, MIN_LIQ, POLL_INTERVAL
"""

import asyncio
import aiohttp
import cloudscraper
import logging
import os
import re
import json

# ─── Config ───────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
WHAPI_TOKEN        = os.getenv("WHAPI_TOKEN", "")
WHATSAPP_GROUP_ID  = os.getenv("WHATSAPP_GROUP_ID", "")
MIN_FOLLOWERS      = int(os.getenv("MIN_FOLLOWERS", "10000"))
MIN_MCAP           = float(os.getenv("MIN_MCAP",    "50000"))
MIN_VOLUME         = float(os.getenv("MIN_VOLUME",  "50000"))
MIN_LIQ            = float(os.getenv("MIN_LIQ",     "30000"))
POLL_INTERVAL      = int(os.getenv("POLL_INTERVAL", "30"))

BANKR_API_URL   = "https://api.bankr.bot/token-launches"
CLANKER_API_URL = "https://www.clanker.world/api/tokens"

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
blocked_accounts: set[str] = set()
BLOCKLIST_FILE = "/data/blocklist.json" if os.path.exists("/data") else "blocklist.json"

# Cloudscraper instance reused across requests (bypasses Cloudflare on Bankr)
_scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "darwin", "mobile": False}
)

# ─── Blocklist ────────────────────────────────────────────────────────────────

def load_blocklist():
    global blocked_accounts
    try:
        with open(BLOCKLIST_FILE) as f:
            blocked_accounts = set(json.load(f))
        log.info(f"📋 Loaded {len(blocked_accounts)} blocked accounts")
    except FileNotFoundError:
        blocked_accounts = set()
    except Exception as e:
        log.error(f"Blocklist load error: {e}")
        blocked_accounts = set()

def save_blocklist():
    try:
        with open(BLOCKLIST_FILE, "w") as f:
            json.dump(list(blocked_accounts), f)
    except Exception as e:
        log.error(f"Blocklist save error: {e}")

# ─── Telegram ─────────────────────────────────────────────────────────────────

async def send_telegram(session: aiohttp.ClientSession, text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        async with session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        ) as resp:
            if resp.status == 200:
                log.info("✅ Telegram sent")
                return True
            log.error(f"❌ Telegram {resp.status}: {await resp.text()}")
            return False
    except Exception as e:
        log.error(f"❌ Telegram error: {e}")
        return False

# ─── WhatsApp ────────────────────────────────────────────────────────────────

async def send_whatsapp(session: aiohttp.ClientSession, text: str) -> bool:
    if not WHAPI_TOKEN or not WHATSAPP_GROUP_ID:
        return False
    try:
        async with session.post(
            "https://gate.whapi.cloud/messages/text",
            json={"to": WHATSAPP_GROUP_ID, "body": text},
            headers={"Authorization": f"Bearer {WHAPI_TOKEN}"},
            timeout=10,
        ) as resp:
            if resp.status in (200, 201):
                log.info("✅ WhatsApp sent")
                return True
            log.error(f"❌ WhatsApp {resp.status}: {await resp.text()}")
            return False
    except Exception as e:
        log.error(f"❌ WhatsApp error: {e}")
        return False

async def send_alert(session: aiohttp.ClientSession, tg_text: str, wa_text: str):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        await send_telegram(session, tg_text)
    if WHAPI_TOKEN and WHATSAPP_GROUP_ID:
        await send_whatsapp(session, wa_text)

# ─── Follower Count ───────────────────────────────────────────────────────────

async def get_follower_count(session: aiohttp.ClientSession, username: str) -> int | None:
    username = username.lstrip("@").strip()
    if not username:
        return None
    if username in follower_cache:
        return follower_cache[username]

    count = None

    # Method 1: Twitter syndication API
    try:
        async with session.get(
            f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        ) as resp:
            if resp.status == 200:
                text = await resp.text()
                for pat in [r'"followers_count":(\d+)', r'"followersCount":(\d+)', r'followers_count&quot;:(\d+)']:
                    m = re.search(pat, text)
                    if m:
                        count = int(m.group(1))
                        break
    except Exception:
        pass

    # Method 2: Nitter fallback
    if count is None:
        for instance in ["https://nitter.privacydev.net", "https://nitter.poast.org"]:
            try:
                async with session.get(f"{instance}/{username}", timeout=8, allow_redirects=True) as resp:
                    if resp.status == 200:
                        stats = re.findall(r'class="profile-stat-num"[^>]*>([\d,]+)', await resp.text())
                        if len(stats) >= 3:
                            count = int(stats[2].replace(",", ""))
                            break
            except Exception:
                continue

    if count is None:
        log.warning(f"@{username} → follower count unknown")
    follower_cache[username] = count
    return count

# ─── DexScreener stats ────────────────────────────────────────────────────────

async def get_dexscreener_stats(session: aiohttp.ClientSession, address: str) -> dict:
    """Returns mcap, volume (24h), liquidity for a Base token. Zeros if not found yet."""
    empty = {"mcap": 0.0, "volume": 0.0, "liquidity": 0.0}
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            timeout=10,
        ) as resp:
            if resp.status != 200:
                return empty
            data = await resp.json()

        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "base"]
        if not pairs:
            return empty

        # Use the pair with the highest liquidity
        pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
        return {
            "mcap":      float(pair.get("fdv") or 0),
            "volume":    float((pair.get("volume") or {}).get("h24", 0) or 0),
            "liquidity": float((pair.get("liquidity") or {}).get("usd", 0) or 0),
        }
    except Exception as e:
        log.debug(f"DexScreener error {address}: {e}")
        return empty

# ─── X username extraction ────────────────────────────────────────────────────

_X_SKIP = {"home", "explore", "search", "settings", "i", "intent", "share", "compose"}

def extract_x_from_urls(urls) -> str | None:
    """Extract deployer X username only from explicit x.com/twitter.com URLs."""
    if not urls:
        return None
    if isinstance(urls, str):
        try:
            urls = json.loads(urls)
        except Exception:
            urls = [urls]
    for url in urls:
        if not isinstance(url, str):
            continue
        m = re.search(r'(?:x\.com|twitter\.com)/(@?[\w]+)/?', url, re.IGNORECASE)
        if m:
            u = m.group(1).lstrip("@")
            if u.lower() not in _X_SKIP:
                return u
    return None

# ─── Fetch Bankr ─────────────────────────────────────────────────────────────

async def fetch_bankr() -> list[dict]:
    """
    Uses cloudscraper to bypass Cloudflare on Bankr.
    deployer.xUsername = person who tweeted @bankrbot = the actual launcher.
    """
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: _scraper.get(BANKR_API_URL, timeout=15))
        if resp.status_code != 200:
            log.error(f"Bankr API error: {resp.status_code}")
            return []
        data = resp.json()
    except Exception as e:
        log.error(f"Bankr fetch error: {e}")
        return []

    launches = data if isinstance(data, list) else data.get("launches", data.get("tokens", []))

    # Log one sample so we can verify field names
    if launches:
        log.info(f"BANKR SAMPLE: {json.dumps(launches[0], default=str)[:600]}")

    normalized = []
    for launch in launches:
        address = (launch.get("tokenAddress") or launch.get("contractAddress") or "").strip()
        if not address:
            continue
        deployer   = launch.get("deployer") or {}
        x_username = (deployer.get("xUsername") or deployer.get("x_username") or "").lstrip("@").strip()

        normalized.append({
            "source":      "bankr",
            "address":     address.lower(),
            "name":        launch.get("tokenName", "Unknown"),
            "symbol":      launch.get("tokenSymbol", "?"),
            "x_username":  x_username,
            "tweet_url":   launch.get("tweetUrl", ""),
            "website":     launch.get("websiteUrl", ""),
            "clanker_url": "",
        })

    log.info(f"Bankr: {len(normalized)} launches fetched")
    return normalized

# ─── Fetch Clanker ────────────────────────────────────────────────────────────

async def fetch_clanker(session: aiohttp.ClientSession) -> list[dict]:
    """
    Fetches Clanker token launches using the official API.
    Uses includeMarket=true to get mcap/volume directly (no DexScreener needed for Clanker).
    Max page size is 20 per the docs.

    X username priority (strict — no description scraping):
      1. social_context.platform contains twitter/x/bankr → social_context.id
      2. socialLinks — [{"name": "x", "link": "https://x.com/..."}]  (live API format)
      3. metadata.socialMediaUrls — [{"platform": "twitter", "url": "..."}]  (docs format)
    """
    try:
        async with session.get(
            CLANKER_API_URL,
            params={
                "sort":          "desc",
                "limit":         "20",
                "includeMarket": "true",
                "chainId":       "8453",
            },
            headers={"User-Agent": "WhaleAlertBot/1.0", "Accept": "application/json"},
            timeout=15,
        ) as resp:
            if resp.status != 200:
                log.error(f"Clanker API error: {resp.status}")
                return []
            data = await resp.json()
    except Exception as e:
        log.error(f"Clanker fetch error: {e}")
        return []

    tokens = data if isinstance(data, list) else data.get("data", data.get("tokens", []))

    normalized = []
    for token in tokens:
        address = (token.get("contract_address") or "").strip()
        if not address:
            continue

        x_username = None

        # Priority 1: social_context — set when deployed via X/Bankr
        sc       = token.get("social_context") or {}
        platform = str(sc.get("platform") or "").lower()
        if any(kw in platform for kw in ("twitter", "x.com", "bankr")):
            ctx_id = str(sc.get("id") or sc.get("userId") or "").strip().lstrip("@")
            if ctx_id:
                x_username = ctx_id

        # Priority 2: socialLinks (confirmed live API format from logs)
        if not x_username:
            for entry in (token.get("socialLinks") or []):
                if isinstance(entry, dict) and entry.get("name", "").lower() in ("x", "twitter"):
                    x_username = extract_x_from_urls([entry.get("link", "")])
                    if x_username:
                        break

        # Priority 3: metadata.socialMediaUrls (documented format)
        if not x_username:
            for entry in ((token.get("metadata") or {}).get("socialMediaUrls") or []):
                if isinstance(entry, dict) and entry.get("platform", "").lower() in ("twitter", "x"):
                    x_username = extract_x_from_urls([entry.get("url", "")])
                    if x_username:
                        break

        # Market data from includeMarket=true
        market = (token.get("related") or {}).get("market") or {}
        mcap   = float(market.get("market_cap") or market.get("marketCap") or 0)
        volume = float(market.get("volume_24h") or market.get("volume") or 0)

        normalized.append({
            "source":      "clanker",
            "address":     address.lower(),
            "name":        token.get("name", "Unknown"),
            "symbol":      token.get("symbol") or token.get("ticker", "?"),
            "x_username":  x_username or "",
            "tweet_url":   "",
            "website":     "",
            "clanker_url": f"https://clanker.world/clanker/{address}",
            "mcap_hint":   mcap,
            "volume_hint": volume,
        })

    log.info(f"Clanker: {len(normalized)} launches fetched")
    return normalized

# ─── Format Alert ────────────────────────────────────────────────────────────

def fmt_usd(n: float) -> str:
    if n >= 1_000_000: return f"${n/1_000_000:.1f}M"
    if n >= 1_000:     return f"${n/1_000:.0f}K"
    return f"${n:.0f}"

def fmt_followers(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)

def format_alert(launch: dict, followers: int, stats: dict) -> tuple[str, str]:
    name        = launch["name"]
    symbol      = launch["symbol"]
    address     = launch["address"]
    x_username  = launch["x_username"]
    source      = launch["source"]
    tweet_url   = launch.get("tweet_url", "")
    website     = launch.get("website", "")
    clanker_url = launch.get("clanker_url", "")
    src_label   = "🏦 Bankr" if source == "bankr" else "⚙️ Clanker"

    tg = [
        "🐋 <b>WHALE LAUNCH DETECTED</b>",
        "",
        f"🪙 <b>{name}</b> (${symbol})",
        f"👤 <a href='https://x.com/{x_username}'>@{x_username}</a> — <b>{fmt_followers(followers)} followers</b>",
        f"🚀 Source: {src_label}",
        "",
        f"💹 MCap: <b>{fmt_usd(stats['mcap'])}</b>  |  Vol 24h: <b>{fmt_usd(stats['volume'])}</b>  |  Liq: <b>{fmt_usd(stats['liquidity'])}</b>",
        "",
        "📍 Chain: Base",
        f"📋 CA: <code>{address}</code>",
    ]
    if tweet_url:   tg.append(f"🐦 <a href='{tweet_url}'>Original Tweet</a>")
    if website:     tg.append(f"🌐 <a href='{website}'>Website</a>")
    if clanker_url: tg.append(f"🔗 <a href='{clanker_url}'>Clanker Page</a>")
    tg += [
        "",
        f"📊 <a href='https://dexscreener.com/base/{address}'>DexScreener</a> | <a href='https://www.dextools.io/app/en/base/pair-explorer/{address}'>DexTools</a>",
        f"💰 <a href='https://app.uniswap.org/swap?outputCurrency={address}&chain=base'>Buy on Uniswap</a>",
    ]

    wa = [
        "🐋 *WHALE LAUNCH DETECTED*",
        "",
        f"🪙 *{name}* (${symbol})",
        f"👤 @{x_username} — *{fmt_followers(followers)} followers*",
        f"🚀 Source: {src_label}",
        "",
        f"💹 MCap: {fmt_usd(stats['mcap'])}  |  Vol: {fmt_usd(stats['volume'])}  |  Liq: {fmt_usd(stats['liquidity'])}",
        "",
        "📍 Chain: Base",
        f"📋 CA: {address}",
    ]
    if tweet_url:   wa.append(f"🐦 {tweet_url}")
    if clanker_url: wa.append(f"🔗 {clanker_url}")
    wa += [
        "",
        f"📊 https://dexscreener.com/base/{address}",
        f"💰 https://app.uniswap.org/swap?outputCurrency={address}&chain=base",
    ]

    return "\n".join(tg), "\n".join(wa)

# ─── Telegram Commands ────────────────────────────────────────────────────────

async def handle_commands(session: aiohttp.ClientSession):
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        async with session.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": handle_commands.last_id + 1, "timeout": 0},
            timeout=10,
        ) as resp:
            if resp.status != 200:
                return
            data = await resp.json()
    except Exception:
        return

    for update in data.get("result", []):
        handle_commands.last_id = update["update_id"]
        msg  = update.get("message", {})
        text = msg.get("text", "").strip()
        cid  = str(msg.get("chat", {}).get("id", ""))
        if cid != TELEGRAM_CHAT_ID:
            continue

        if text.startswith("/block "):
            u = text.split(None, 1)[1].lstrip("@").lower()
            blocked_accounts.add(u); save_blocklist(); follower_cache.pop(u, None)
            await send_telegram(session, f"🚫 Blocked @{u}")

        elif text.startswith("/unblock "):
            u = text.split(None, 1)[1].lstrip("@").lower()
            if u in blocked_accounts:
                blocked_accounts.discard(u); save_blocklist()
                await send_telegram(session, f"✅ Unblocked @{u}")
            else:
                await send_telegram(session, f"@{u} not in blocklist.")

        elif text == "/blocklist":
            body = ("🚫 <b>Blocked:</b>\n" + "\n".join(f"• @{u}" for u in sorted(blocked_accounts))
                    if blocked_accounts else "No blocked accounts.")
            await send_telegram(session, body)

        elif text == "/status":
            await send_telegram(session,
                f"🐋 <b>Bot Status</b>\n"
                f"Seen tokens: {len(seen_tokens)}\n"
                f"Blocked: {len(blocked_accounts)}\n"
                f"Min followers: {MIN_FOLLOWERS:,}\n"
                f"Min MCap: {fmt_usd(MIN_MCAP)} | Vol: {fmt_usd(MIN_VOLUME)} | Liq: {fmt_usd(MIN_LIQ)}\n"
                f"Poll: {POLL_INTERVAL}s"
            )

handle_commands.last_id = 0

# ─── Main Loop ────────────────────────────────────────────────────────────────

async def poll_loop():
    log.info("=" * 60)
    log.info("  🐋 Whale Alert Bot (Bankr + Clanker)")
    log.info(f"  Min followers : {MIN_FOLLOWERS:,}")
    log.info(f"  Min MCap      : {fmt_usd(MIN_MCAP)}")
    log.info(f"  Min Volume 24h: {fmt_usd(MIN_VOLUME)}")
    log.info(f"  Min Liquidity : {fmt_usd(MIN_LIQ)}")
    log.info(f"  Poll interval : {POLL_INTERVAL}s")
    log.info(f"  Telegram : {'✅ configured' if TELEGRAM_BOT_TOKEN else '❌ not set'}")
    log.info(f"  WhatsApp : {'✅ configured' if WHAPI_TOKEN else 'not configured'}")
    log.info("=" * 60)

    load_blocklist()

    async with aiohttp.ClientSession() as session:

        # Seed to avoid alerting on launches that existed before bot started
        log.info("📋 Seeding existing tokens...")
        initial_bankr   = await fetch_bankr()
        initial_clanker = await fetch_clanker(session)
        for t in initial_bankr + initial_clanker:
            seen_tokens.add(t["address"])
        log.info(f"📋 Seeded {len(seen_tokens)} tokens "
                 f"(Bankr: {len(initial_bankr)}, Clanker: {len(initial_clanker)})")

        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            await send_telegram(session,
                f"🐋 <b>Whale Alert Bot started</b>\n"
                f"Filters: {MIN_FOLLOWERS:,}+ followers | "
                f"MCap {fmt_usd(MIN_MCAP)}+ | Vol {fmt_usd(MIN_VOLUME)}+ | Liq {fmt_usd(MIN_LIQ)}+\n"
                f"Commands: /block @user · /unblock @user · /blocklist · /status"
            )

        while True:
            try:
                await handle_commands(session)

                bankr   = await fetch_bankr()
                clanker = await fetch_clanker(session)
                new_count = whale_count = 0

                for launch in bankr + clanker:
                    address = launch["address"]
                    if address in seen_tokens:
                        continue
                    seen_tokens.add(address)
                    new_count += 1

                    source     = launch["source"]
                    symbol     = launch["symbol"]
                    x_username = launch["x_username"]

                    # ── Filter 1: must have a deployer X account ──
                    if not x_username:
                        log.info(f"  [{source}] ${symbol} — no deployer X, skip")
                        continue

                    # ── Filter 2: not blocked ──
                    if x_username.lower() in blocked_accounts:
                        log.info(f"  [{source}] ${symbol} @{x_username} — blocked, skip")
                        continue

                    # ── Filter 3: follower count ──
                    followers = await get_follower_count(session, x_username)
                    if followers is None:
                        log.info(f"  [{source}] ${symbol} @{x_username} — followers unknown, skip")
                        continue
                    if followers < MIN_FOLLOWERS:
                        log.info(f"  [{source}] ${symbol} @{x_username} — {followers:,} followers, skip")
                        continue

                    # ── Filter 4: market stats ──
                    # Use Clanker's built-in market data if available, else DexScreener
                    mcap_hint   = launch.get("mcap_hint", 0)
                    volume_hint = launch.get("volume_hint", 0)

                    if mcap_hint > 0 or volume_hint > 0:
                        # Clanker already returned market data via includeMarket=true
                        stats = {
                            "mcap":      mcap_hint,
                            "volume":    volume_hint,
                            "liquidity": 0,  # not in Clanker API, check DexScreener only for liq
                        }
                        # Still need liquidity from DexScreener
                        dex = await get_dexscreener_stats(session, address)
                        stats["liquidity"] = dex["liquidity"]
                        # If Clanker mcap/volume were 0 (new token), fall back to DexScreener
                        if stats["mcap"] == 0:   stats["mcap"]   = dex["mcap"]
                        if stats["volume"] == 0: stats["volume"] = dex["volume"]
                    else:
                        stats = await get_dexscreener_stats(session, address)
                    if stats["mcap"] < MIN_MCAP:
                        log.info(f"  [{source}] ${symbol} — MCap {fmt_usd(stats['mcap'])}, skip")
                        continue
                    if stats["volume"] < MIN_VOLUME:
                        log.info(f"  [{source}] ${symbol} — Vol {fmt_usd(stats['volume'])}, skip")
                        continue
                    if stats["liquidity"] < MIN_LIQ:
                        log.info(f"  [{source}] ${symbol} — Liq {fmt_usd(stats['liquidity'])}, skip")
                        continue

                    # ── All filters passed → ALERT ──
                    whale_count += 1
                    log.info(
                        f"  🐋 [{source}] ${symbol} @{x_username} — "
                        f"{followers:,} followers | MCap {fmt_usd(stats['mcap'])} | "
                        f"Vol {fmt_usd(stats['volume'])} | Liq {fmt_usd(stats['liquidity'])} — ALERT!"
                    )
                    tg_text, wa_text = format_alert(launch, followers, stats)
                    await send_alert(session, tg_text, wa_text)
                    await asyncio.sleep(1)

                if new_count:
                    log.info(f"🔍 {new_count} new launches processed, {whale_count} alerts sent")

            except Exception as e:
                log.error(f"Poll loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL)

# ─── Entry ────────────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN: log.error("❌ TELEGRAM_BOT_TOKEN not set!")
    if not TELEGRAM_CHAT_ID:   log.error("❌ TELEGRAM_CHAT_ID not set!")
    asyncio.run(poll_loop())

if __name__ == "__main__":
    main()
