"""
Whale Alert Bot — Bankr + Clanker + Virtuals + DexScreener
========================================================
Monitors FIVE sources for new token launches on Base:
  1. Bankr API   — https://api.bankr.bot/token-launches
  2. Clanker API  — https://www.clanker.world/api/tokens
  3. Virtuals API — https://api2.virtuals.io/api/virtuals  (AI agent launches)
  4. DexScreener — market data (MCap, Volume, Liquidity)
  5. DexScreener — catch-all via profiles/boosts/search (ApeStore, direct deploys)

When a token passes market filters → alerts to Telegram + WhatsApp + Pushover.
Telegram signals include inline buy/sell buttons (20% / 50% / 100%).
Auto-execution via Bankr Agent API when AUTO_EXECUTE=true.

Liquidity filter is SKIPPED for safe launchpads (Bankr, Clanker, Virtuals)
because they have locked LP / bonding curves — no rug possible.

Deploy: GitHub + Railway
"""

import asyncio
import aiohttp
import logging
import os
import re
import json
import time
import html
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from database import (
    close_db,
    db_session,
    get_due_delivery_retries,
    get_due_rechecks,
    get_launch,
    get_launch_status,
    get_status_snapshot,
    init_db,
    mark_delivery_failed,
    mark_delivery_retry,
    mark_delivery_sent,
    mark_delivery_sending,
    mark_launch_status,
    provider_available,
    queue_recheck,
    record_api_budget_event,
    set_bot_state,
    set_provider_cooldown,
    signal_exists_for_tenant,
    store_verdict,
    upsert_launch,
    utc_now,
)
from research_pipeline import (
    AUTO_VERDICT_ENABLED,
    AUTO_VERDICT_MAX_CONCURRENT,
    AUTO_VERDICT_TIMEOUT_SEC,
    ResearchDeps,
    build_signal_verdict_with_timeout,
    format_verdict_block,
)
from services.delivery import prepare_tenant_delivery
from services.observability import correlation_id, log_event
from services.tenants import ensure_telegram_tenant
from services.token_intelligence import analyze_token_intelligence
from settings import resolve_database_url, settings

# ─── Config from environment ──────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
AUTHORIZED_USER_IDS = {
    user_id.strip()
    for user_id in os.getenv("AUTHORIZED_USER_IDS", "544999608").split(",")
    if user_id.strip()
}
TRADER_USER_IDS = {
    user_id.strip()
    for user_id in (
        os.getenv("TRADER_USER_IDS", "")
        or os.getenv("TRADER_USER_ID", "")
    ).split(",")
    if user_id.strip()
}
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN", "")
WHATSAPP_GROUP_ID = os.getenv("WHATSAPP_GROUP_ID", "")
SOCIALDATA_API_KEY = os.getenv("SOCIALDATA_API_KEY", "")
MIN_FOLLOWERS = int(os.getenv("MIN_FOLLOWERS", "5000"))
RESEARCH_MIN_FOLLOWERS = int(os.getenv("RESEARCH_MIN_FOLLOWERS", "1000"))
RESEARCH_HIGH_SIGNAL_SCORE = int(os.getenv("RESEARCH_HIGH_SIGNAL_SCORE", "8"))
MIN_MCAP = int(os.getenv("MIN_MCAP", "50000"))
MIN_VOLUME_24H = int(os.getenv("MIN_VOLUME_24H", "30000"))
MIN_LIQUIDITY = int(os.getenv("MIN_LIQUIDITY", "30000"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

# ─── Bankr Execution Config ───────────────────────────────────────────────────
BANKR_EXECUTION_API_KEY = os.getenv("BANKR_EXECUTION_API_KEY", "")
BANKR_BUY_AMOUNT = int(os.getenv("BANKR_BUY_AMOUNT", "100"))
AUTO_EXECUTE = os.getenv("AUTO_EXECUTE", "false").lower() == "true"

# ─── Pushover Config ──────────────────────────────────────────────────────────
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN", "")

# ─── Trader Config ────────────────────────────────────────────────────────────
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"
if TRADING_ENABLED and not TRADER_USER_IDS:
    logging.getLogger("whale-alert").error("TRADING_ENABLED=true but TRADER_USER_IDS is empty — disabling trading")
    TRADING_ENABLED = False
ALCHEMY_RPC_URL = os.getenv("ALCHEMY_RPC_URL", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

BANKR_API_URL = "https://api.bankr.bot/token-launches"
BANKR_AGENT_API_URL = "https://api.bankr.bot/agent/prompt"
CLANKER_API_URL = "https://www.clanker.world/api/tokens"
VIRTUALS_API_URL = "https://api2.virtuals.io/api/virtuals"
GECKOTERMINAL_API_URL = "https://api.geckoterminal.com/api/v2"
SOCIALDATA_API_URL = "https://api.socialdata.tools/twitter/user"
DEXSCREENER_API_URL = "https://api.dexscreener.com"
CLANKER_CHAIN_ID_BASE = 8453
CLANKER_PAGE_SIZE = 10
CLANKER_POLL_PAGES = int(os.getenv("CLANKER_POLL_PAGES", "5"))
DEXSCREENER_DISCOVERY_ENABLED = os.getenv("DEXSCREENER_DISCOVERY_ENABLED", "true").lower() == "true"
DEXSCREENER_DISCOVERY_LIMIT = int(os.getenv("DEXSCREENER_DISCOVERY_LIMIT", "40"))
DEXSCREENER_DISCOVERY_ENDPOINTS = (
    ("profiles", "/token-profiles/latest/v1"),
    ("boosts", "/token-boosts/latest/v1"),
    ("community_takeovers", "/community-takeovers/latest/v1"),
)

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("whale-alert")

# ─── State ────────────────────────────────────────────────────────────────────

follower_cache: dict[str, int | None] = {}
gecko_cache: dict[str, tuple[float, dict | None]] = {}
GECKO_CACHE_TTL_HIT = 120
GECKO_CACHE_TTL_MISS = 60
last_update_id: int = 0
alert_count: int = 0
execution_count: int = 0
default_tenant_db_id: int | None = None

# ─── Recheck queue ────────────────────────────────────────────────────────────
RECHECK_MAX_AGE = 3600
RECHECK_INTERVAL = 300
RECHECK_MAX_CHECKS = 12
RECHECK_MAX_QUEUE = 300
TELEGRAM_RETRY_BATCH = int(os.getenv("TELEGRAM_RETRY_BATCH", "20"))
TELEGRAM_MAX_DELIVERY_ATTEMPTS = int(os.getenv("TELEGRAM_MAX_DELIVERY_ATTEMPTS", "3"))

# ─── Blocklist ────────────────────────────────────────────────────────────────

BLOCKLIST_FILE = Path("/data/blocklist.json") if Path("/data").exists() else Path("blocklist.json")
DATA_DIR = Path("/data") if Path("/data").exists() else Path("data")
INFLUENCER_ACCOUNTS_FILE = DATA_DIR / "accounts.txt"
HIGH_PRIORITY_ACCOUNTS_FILE = DATA_DIR / "high_priority.txt"
TRACKED_WALLETS_FILE = DATA_DIR / "tracked_wallets.json"

# ─── Safe launchpads (locked LP / bonding curves — no rug possible) ───────────
SAFE_LAUNCHPADS = {"bankr", "clanker", "virtuals"}


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


def load_account_file(path: Path) -> list[str]:
    try:
        if not path.exists():
            return []
        accounts = []
        for line in path.read_text(encoding="utf-8").splitlines():
            account = line.strip().lstrip("@")
            if account and not account.startswith("#"):
                accounts.append(account)
        return accounts
    except Exception as e:
        log.error(f"Error loading accounts from {path}: {e}")
        return []


WATCHED_INFLUENCERS = load_account_file(INFLUENCER_ACCOUNTS_FILE)
HIGH_PRIORITY_INFLUENCERS = {u.lower() for u in load_account_file(HIGH_PRIORITY_ACCOUNTS_FILE)}


def load_tracked_wallets() -> list[dict]:
    try:
        if TRACKED_WALLETS_FILE.exists():
            data = json.loads(TRACKED_WALLETS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [w for w in data if isinstance(w, dict) and w.get("address")]
    except Exception as e:
        log.error(f"Error loading tracked wallets: {e}")
    return []


def save_tracked_wallets(wallets: list[dict]):
    try:
        TRACKED_WALLETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRACKED_WALLETS_FILE.write_text(json.dumps(wallets, indent=2), encoding="utf-8")
    except Exception as e:
        log.error(f"Error saving tracked wallets: {e}")


def add_tracked_wallet(address: str, label: str = "", chain: str = "base") -> bool:
    address = address.strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
        return False
    wallets = load_tracked_wallets()
    normalized = address.lower()
    for wallet in wallets:
        if wallet["address"].lower() == normalized:
            wallet["label"] = label
            wallet["chain"] = chain
            save_tracked_wallets(wallets)
            return True
    wallets.append({
        "address": normalized,
        "label": label,
        "chain": chain,
        "added_at": datetime.now(timezone.utc).isoformat(),
    })
    save_tracked_wallets(wallets)
    return True


def remove_tracked_wallet(address: str) -> bool:
    normalized = address.strip().lower()
    wallets = load_tracked_wallets()
    updated = [w for w in wallets if w["address"].lower() != normalized]
    if len(updated) == len(wallets):
        return False
    save_tracked_wallets(updated)
    return True


# ─── Bankr Auto-Execution ─────────────────────────────────────────────────────

async def execute_bankr_buy(session: aiohttp.ClientSession, token_address: str, symbol: str, source: str) -> bool:
    global execution_count

    if not AUTO_EXECUTE:
        return False

    if not BANKR_EXECUTION_API_KEY:
        log.warning("⚠️ AUTO_EXECUTE=true but BANKR_EXECUTION_API_KEY not set — skipping execution")
        return False

    try:
        prompt = f"buy ${BANKR_BUY_AMOUNT} of {token_address} on base"
        async with session.post(
            BANKR_AGENT_API_URL,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": BANKR_EXECUTION_API_KEY,
            },
            json={"prompt": prompt},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()

            if resp.status == 202 and data.get("success"):
                job_id = data.get("jobId", "?")
                thread_id = data.get("threadId", "?")
                execution_count += 1
                log.info(f"  💸 EXECUTED: [{source}] ${symbol} — ${BANKR_BUY_AMOUNT} buy submitted | jobId: {job_id} | threadId: {thread_id}")
                return True
            elif resp.status == 403:
                log.error(f"  ❌ Bankr execution forbidden (403) — check API key permissions at bankr.bot/api")
                return False
            elif resp.status == 429:
                reset_at = data.get("resetAt", "")
                log.warning(f"  ⚠️ Bankr rate limit hit — resets at {reset_at}")
                return False
            else:
                log.error(f"  ❌ Bankr execution failed {resp.status}: {data}")
                return False

    except Exception as e:
        log.error(f"  ❌ Bankr execution error for ${symbol}: {e}")
        return False


# ─── Telegram ─────────────────────────────────────────────────────────────────

async def send_telegram(session: aiohttp.ClientSession, text: str, chat_id: str = "", reply_markup: dict = None) -> int | None:
    """Send a Telegram message. Returns message_id on success."""
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                message_id = data.get("result", {}).get("message_id")
                return message_id if isinstance(message_id, int) else None
            else:
                body = await resp.text()
                log.error(f"Telegram error {resp.status} (chat {target}): {body[:200]}")
                return None
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return None


async def edit_telegram_message(session: aiohttp.ClientSession, chat_id: str, message_id: int, text: str, reply_markup: dict = None) -> bool:
    """Edit an existing Telegram message."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return True
            body = await resp.text()
            log.warning(f"Telegram edit error {resp.status} (chat {chat_id}, msg {message_id}): {body[:240]}")
            return False
    except Exception as e:
        log.error(f"Telegram edit failed: {e}")
        return False


async def answer_callback_query(session: aiohttp.ClientSession, callback_query_id: str, text: str = "", show_alert: bool = False) -> bool:
    """Acknowledge a Telegram callback query (removes loading spinner)."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": show_alert,
    }
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return resp.status == 200
    except Exception as e:
        log.error(f"answerCallbackQuery failed: {e}")
        return False


# ─── Bot Commands ─────────────────────────────────────────────────────────────

BOT_COMMANDS = [
    {"command": "start", "description": "Show command menu"},
    {"command": "help", "description": "Show command menu"},
    {"command": "status", "description": "Runtime status"},
    {"command": "research", "description": "Research ticker or Base CA"},
    {"command": "r", "description": "Short alias for research"},
    {"command": "verdict2", "description": "Run Verdict 2.0 for Base CA"},
    {"command": "spoof_check", "description": "Run spoof checks for Base CA"},
    {"command": "summary", "description": "AI summary stub for Base CA"},
    {"command": "test", "description": "Send a test signal"},
    {"command": "wallets", "description": "List tracked wallets"},
    {"command": "track", "description": "Track wallet: /track 0x... label"},
    {"command": "untrack", "description": "Stop tracking wallet"},
    {"command": "block", "description": "Block X account"},
    {"command": "unblock", "description": "Unblock X account"},
    {"command": "blocklist", "description": "List blocked accounts"},
    {"command": "wallet", "description": "Show bot trading wallet"},
    {"command": "buy", "description": "Manual buy when trading enabled"},
    {"command": "sell", "description": "Manual sell when trading enabled"},
]


def build_help_text() -> str:
    auth = ", ".join(sorted(AUTHORIZED_USER_IDS)) if AUTHORIZED_USER_IDS else "none"
    return (
        "🐋 <b>Base Bot Commands</b>\n\n"
        "<b>Research</b>\n"
        "• <code>/research $TICKER</code> — token research\n"
        "• <code>/research 0xCONTRACT</code> — CA research on Base\n"
        "• <code>/verdict2 0xCONTRACT</code> — Verdict 2.0\n"
        "• <code>/spoof_check 0xCONTRACT</code> — spoof/risk checks\n"
        "• <code>/summary 0xCONTRACT</code> — cached AI summary stub\n"
        "• <code>/r $TICKER</code> — short research alias\n"
        "• Signal buttons: X Research, Ticker X, Copy CA\n\n"
        "<b>Wallet tracking</b>\n"
        "• <code>/track 0xADDRESS [label]</code> — add wallet\n"
        "• <code>/untrack 0xADDRESS</code> — remove wallet\n"
        "• <code>/wallets</code> — list tracked wallets\n\n"
        "<b>Bot control</b>\n"
        "• <code>/status</code> — runtime status\n"
        "• <code>/test</code> — send test signal\n"
        "• <code>/block @user</code> / <code>/unblock @user</code>\n"
        "• <code>/blocklist</code> — blocked X accounts\n\n"
        "<b>Trading</b>\n"
        "• <code>/wallet</code> — bot wallet, requires trading config\n"
        "• <code>/buy 0xADDRESS 20</code> / <code>/sell 0xADDRESS 50</code>\n\n"
        f"DM access: <code>{auth}</code>"
    )


def command_name(text: str) -> str:
    head = text.split(maxsplit=1)[0].lower()
    return head.split("@", 1)[0]


def is_base_contract(value: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", str(value or "").strip()))


def is_authorized_update(msg: dict) -> bool:
    chat_id = str(msg.get("chat", {}).get("id", ""))
    user_id = str(msg.get("from", {}).get("id", ""))
    return chat_id == TELEGRAM_CHAT_ID or user_id in AUTHORIZED_USER_IDS


def is_trader_user(user_id: str) -> bool:
    return bool(TRADER_USER_IDS) and user_id in TRADER_USER_IDS


def is_fresh_telegram_message(msg: dict, max_age_sec: int = 60) -> bool:
    msg_date = int(msg.get("date") or 0)
    return bool(msg_date) and time.time() - msg_date <= max_age_sec


async def set_bot_commands(session: aiohttp.ClientSession) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    try:
        async with session.post(url, json={"commands": BOT_COMMANDS}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                log.info("✅ Telegram command menu updated")
                return True
            body = await resp.text()
            log.warning(f"setMyCommands failed {resp.status}: {body[:200]}")
    except Exception as e:
        log.warning(f"setMyCommands error: {e}")
    return False


async def delete_telegram_webhook(session: aiohttp.ClientSession, drop_pending_updates: bool = True) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
    try:
        async with session.post(
            url,
            json={"drop_pending_updates": drop_pending_updates},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                log.info("✅ Telegram webhook cleared; pending updates dropped")
                return True
            body = await resp.text()
            log.warning(f"deleteWebhook failed {resp.status}: {body[:200]}")
    except Exception as e:
        log.warning(f"deleteWebhook error: {e}")
    return False


# ─── Inline Keyboard Builder ──────────────────────────────────────────────────

def build_x_research_url(token_address: str, symbol: str) -> str:
    clean_symbol = (symbol or "").strip().lstrip("$")
    query = f"{token_address} OR ${clean_symbol}" if clean_symbol else token_address
    return f"https://x.com/search?q={quote(query, safe='$')}&src=typed_query"


def build_trade_keyboard(token_address: str, symbol: str) -> dict:
    addr = token_address[:20]
    sym = symbol[:10]
    banana_url = f"https://t.me/BananaGun_bot?start={token_address}"
    x_research_url = build_x_research_url(token_address, symbol)
    return {
        "inline_keyboard": [
            [
                {"text": "🔎 X Research", "url": x_research_url},
            ],
            [
                {"text": "📊 Gecko", "url": f"https://www.geckoterminal.com/base/tokens/{token_address}"},
                {"text": "📈 GMGN", "url": f"https://gmgn.ai/base/token/{token_address}"},
            ],
            [
                {"text": "📋 Copy CA", "callback_data": f"copyca:0:{addr}"},
                {"text": "🔎 Ticker X", "callback_data": f"xtickerx:{sym}:{addr}"},
            ],
            [
                {"text": "🍌 Banana Gun", "url": banana_url},
            ],
        ]
    }


_address_map: dict[str, str] = {}


# ─── WhatsApp via Whapi ───────────────────────────────────────────────────────

async def send_whatsapp(session: aiohttp.ClientSession, text: str) -> bool:
    if not WHAPI_TOKEN or not WHATSAPP_GROUP_ID:
        return False
    url = "https://gate.whapi.cloud/messages/text"
    headers = {
        "Authorization": f"Bearer {WHAPI_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"to": WHATSAPP_GROUP_ID, "body": text}
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


# ─── Pushover Emergency Alert ─────────────────────────────────────────────────

async def send_pushover(session: aiohttp.ClientSession, title: str, message: str, url: str = "") -> bool:
    if not PUSHOVER_USER_KEY or not PUSHOVER_API_TOKEN:
        return False
    payload = {
        "token": PUSHOVER_API_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "title": title,
        "message": message,
        "priority": 2,
        "retry": 30,
        "expire": 600,
        "sound": "persistent",
    }
    if url:
        payload["url"] = url
        payload["url_title"] = "Open DexScreener"
    try:
        async with session.post("https://api.pushover.net/1/messages.json", data=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                log.info("🔔 Pushover emergency alert sent")
                return True
            else:
                body = await resp.text()
                log.error(f"Pushover error {resp.status}: {body[:200]}")
                return False
    except Exception as e:
        log.error(f"Pushover send failed: {e}")
        return False


async def send_alert_all(session: aiohttp.ClientSession, tg_text: str, wa_text: str, token_address: str = "", symbol: str = ""):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        keyboard = build_trade_keyboard(token_address, symbol) if token_address else None
        await send_telegram(session, tg_text, reply_markup=keyboard)
    if WHAPI_TOKEN and WHATSAPP_GROUP_ID:
        await send_whatsapp(session, wa_text)


# ─── Trade Callback Handler ───────────────────────────────────────────────────

async def handle_trade_callback(session: aiohttp.ClientSession, callback_query: dict):
    callback_id = callback_query["id"]
    data = callback_query.get("data", "")
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
    message_id = callback_query.get("message", {}).get("message_id")
    user = callback_query.get("from", {})
    username = user.get("username", user.get("first_name", "unknown"))

    parts = data.split(":")
    if len(parts) != 3:
        await answer_callback_query(session, callback_id, "Invalid callback data")
        return

    action, second, addr_truncated = parts

    if action == "copyca":
        full_address = _address_map.get(addr_truncated, addr_truncated)
        await answer_callback_query(session, callback_id, "📋 Contract address sent below")
        await send_telegram(session, f"<code>{full_address}</code>", chat_id=chat_id)
        return

    if action == "xresearch":
        symbol = second
        await answer_callback_query(session, callback_id, f"🔍 Searching X for ${symbol}...")
        full_address = _address_map.get(addr_truncated, addr_truncated)

        async def do_x_research():
            try:
                mentions = await search_x_mentions(session, symbol, address=full_address)
                if not mentions:
                    await send_telegram(session, f"🔍 <b>X Research: ${symbol}</b>\n\nNo notable mentions found (10K+ followers).", chat_id=chat_id)
                    return

                lines = [f"🔍 <b>X Research: ${symbol}</b>\n"]
                for m in mentions:
                    f_count = m['followers']
                    f_str = f"{f_count/1_000_000:.1f}M" if f_count >= 1_000_000 else f"{f_count/1_000:.0f}K"
                    text_clean = re.sub(r'https?://t\.co/\S+', '', m['text']).strip().replace('\n', ' ')
                    text_clean = text_clean.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    if len(text_clean) > 200:
                        text_clean = text_clean[:197] + "..."
                    lines.extend([
                        f"",
                        f"<a href='{m['url']}'>@{m['username']}</a> · {f_str} followers · {m['date']}",
                        f"❤️ {m['likes']} 🔁 {m['retweets']}",
                        f"<i>{text_clean}</i>" if text_clean else "<i>[media only]</i>",
                    ])
                await send_telegram(session, "\n".join(lines), chat_id=chat_id)
            except Exception as e:
                log.error(f"X research callback error: {e}")
                await send_telegram(session, f"❌ X research failed: {str(e)[:100]}", chat_id=chat_id)

        asyncio.create_task(do_x_research())
        return

    if action == "xtickerx":
        symbol = second
        await answer_callback_query(session, callback_id, f"🔎 Fetching latest ${symbol} tweets...")
        full_address = _address_map.get(addr_truncated, addr_truncated)

        async def do_ticker_search():
            try:
                tweets = await search_x_ticker_recent(session, symbol, full_address)
                if not tweets:
                    await send_telegram(session, f"🔎 <b>Latest tweets: ${symbol}</b>\n\nNo recent tweets found.", chat_id=chat_id)
                    return

                lines = [f"🔎 <b>Latest tweets: ${symbol}</b>\n"]
                for t in tweets:
                    f_count = t['followers']
                    if f_count >= 1_000_000:
                        f_str = f"{f_count/1_000_000:.1f}M"
                    elif f_count >= 1_000:
                        f_str = f"{f_count/1_000:.0f}K"
                    else:
                        f_str = str(f_count)
                    text_clean = re.sub(r'https?://t\.co/\S+', '', t['text']).strip().replace('\n', ' ')
                    text_clean = text_clean.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    if len(text_clean) > 200:
                        text_clean = text_clean[:197] + "..."
                    lines.extend([
                        f"",
                        f"<a href='{t['url']}'>@{t['username']}</a> · {f_str} · {t['date']}",
                        f"❤️ {t['likes']} 🔁 {t['retweets']}",
                        f"<i>{text_clean}</i>" if text_clean else "<i>[media only]</i>",
                    ])
                await send_telegram(session, "\n".join(lines), chat_id=chat_id)
            except Exception as e:
                log.error(f"Ticker X search callback error: {e}")
                await send_telegram(session, f"❌ Ticker search failed: {str(e)[:100]}", chat_id=chat_id)

        asyncio.create_task(do_ticker_search())
        return

    user_id = str(callback_query.get("from", {}).get("id", ""))
    if not is_trader_user(user_id):
        await answer_callback_query(session, callback_id, "⛔ Not authorized", show_alert=True)
        return

    if not TRADING_ENABLED:
        await answer_callback_query(session, callback_id, "⚠️ Trading not enabled. Set TRADING_ENABLED=true", show_alert=True)
        return

    callback_msg_date = callback_query.get("message", {}).get("date", 0)
    if callback_msg_date and time.time() - int(callback_msg_date) > 60:
        await answer_callback_query(session, callback_id, "Command expired. Send it again.", show_alert=True)
        return

    percent_str = second
    percent = int(percent_str)

    full_address = _address_map.get(addr_truncated)
    if not full_address:
        await answer_callback_query(session, callback_id, "⚠️ Token address not found — signal too old?", show_alert=True)
        return

    await answer_callback_query(session, callback_id, f"⏳ Executing {action.upper()} {percent}%...")
    log.info(f"  🎯 [{username}] {action.upper()} {percent}% → {full_address[:12]}...")

    try:
        from trader import buy_token, sell_token, get_eth_balance, get_token_balance

        if action == "buy":
            eth_bal = await get_eth_balance()
            result = await buy_token(full_address, percent)
        else:
            result = await sell_token(full_address, percent)

        if result["success"]:
            tx_hash = result["tx_hash"]
            basescan_url = f"https://basescan.org/tx/{tx_hash}"
            if action == "buy":
                amount_eth = result.get("amount_eth", 0)
                confirm_text = f"✅ <b>BUY executed</b> — {percent}% ({amount_eth:.4f} ETH)\n🔗 <a href='{basescan_url}'>View on BaseScan</a>"
            else:
                amount_tokens = result.get("amount_tokens", 0)
                confirm_text = f"✅ <b>SELL executed</b> — {percent}% ({amount_tokens:.2f} tokens)\n🔗 <a href='{basescan_url}'>View on BaseScan</a>"
            await send_telegram(session, confirm_text, chat_id=chat_id)
            log.info(f"  ✅ Trade confirmed: {tx_hash}")
        else:
            error = result.get("error", "Unknown error")
            await send_telegram(session, f"❌ <b>Trade failed</b> — {action.upper()} {percent}%\n<code>{error[:200]}</code>", chat_id=chat_id)
            log.error(f"  ❌ Trade failed: {error}")

    except Exception as e:
        log.error(f"handle_trade_callback error: {e}")
        await send_telegram(session, f"❌ Trade error: {str(e)[:150]}", chat_id=chat_id)


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

            if "callback_query" in update:
                await handle_trade_callback(session, update["callback_query"])
                continue

            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            if not text.startswith("/"):
                continue

            log.info(f"📩 Command from chat {chat_id}: {text[:50]}")
            cmd = command_name(text)

            if not is_authorized_update(msg):
                await send_telegram(session, "⛔ Not authorized for this bot.", chat_id=chat_id)
                continue

            if cmd in ("/start", "/help"):
                await send_telegram(session, build_help_text(), chat_id=chat_id)

            elif cmd == "/block":
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

            elif cmd == "/unblock":
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await send_telegram(session, "Usage: /unblock @username", chat_id)
                    continue
                username = parts[1].strip().lstrip("@").lower()
                blocked_accounts.discard(username)
                save_blocklist(blocked_accounts)
                log.info(f"✅ Unblocked @{username}")
                await send_telegram(session, f"✅ Unblocked <b>@{username}</b>", chat_id)

            elif cmd == "/blocklist":
                if blocked_accounts:
                    names = "\n".join(f"• @{u}" for u in sorted(blocked_accounts))
                    await send_telegram(session, f"🚫 <b>Blocked ({len(blocked_accounts)}):</b>\n{names}", chat_id)
                else:
                    await send_telegram(session, "No accounts blocked.", chat_id)

            elif cmd == "/buy":
                user_id = str(msg.get("from", {}).get("id", ""))
                if not is_trader_user(user_id):
                    await send_telegram(session, "⛔ Not authorized for trading", chat_id=chat_id)
                    continue
                if not is_fresh_telegram_message(msg):
                    await send_telegram(session, "Command expired. Send it again.", chat_id=chat_id)
                    continue
                parts = text.split()
                if len(parts) != 3:
                    await send_telegram(session, "Usage: /buy 0xADDRESS 20\n(percent = 20, 50, or 100)", chat_id=chat_id)
                    continue
                token_addr = parts[1].strip()
                try:
                    percent = int(parts[2].strip())
                    if percent not in [20, 50, 100]:
                        raise ValueError
                except ValueError:
                    await send_telegram(session, "❌ Percent must be 20, 50, or 100", chat_id=chat_id)
                    continue
                if not token_addr.startswith("0x") or len(token_addr) != 42:
                    await send_telegram(session, "❌ Invalid token address", chat_id=chat_id)
                    continue
                if not TRADING_ENABLED:
                    await send_telegram(session, "⚠️ Trading not enabled. Set TRADING_ENABLED=true", chat_id=chat_id)
                    continue
                await send_telegram(session, f"⏳ Buying {percent}% ETH of <code>{token_addr}</code>...", chat_id=chat_id)
                try:
                    from trader import buy_token
                    result = await buy_token(token_addr, percent)
                    if result["success"]:
                        tx_hash = result["tx_hash"]
                        amount_eth = result.get("amount_eth", 0)
                        await send_telegram(session, f"✅ <b>BUY executed</b> — {percent}% ({amount_eth:.4f} ETH)\n🔗 <a href='https://basescan.org/tx/{tx_hash}'>View on BaseScan</a>", chat_id=chat_id)
                    else:
                        await send_telegram(session, f"❌ Buy failed: <code>{result.get('error','')[:200]}</code>", chat_id=chat_id)
                except Exception as e:
                    await send_telegram(session, f"❌ Buy error: {str(e)[:150]}", chat_id=chat_id)

            elif cmd == "/sell":
                user_id = str(msg.get("from", {}).get("id", ""))
                if not is_trader_user(user_id):
                    await send_telegram(session, "⛔ Not authorized for trading", chat_id=chat_id)
                    continue
                if not is_fresh_telegram_message(msg):
                    await send_telegram(session, "Command expired. Send it again.", chat_id=chat_id)
                    continue
                parts = text.split()
                if len(parts) != 3:
                    await send_telegram(session, "Usage: /sell 0xADDRESS 50\n(percent = 20, 50, or 100)", chat_id=chat_id)
                    continue
                token_addr = parts[1].strip()
                try:
                    percent = int(parts[2].strip())
                    if percent not in [20, 50, 100]:
                        raise ValueError
                except ValueError:
                    await send_telegram(session, "❌ Percent must be 20, 50, or 100", chat_id=chat_id)
                    continue
                if not token_addr.startswith("0x") or len(token_addr) != 42:
                    await send_telegram(session, "❌ Invalid token address", chat_id=chat_id)
                    continue
                if not TRADING_ENABLED:
                    await send_telegram(session, "⚠️ Trading not enabled. Set TRADING_ENABLED=true", chat_id=chat_id)
                    continue
                await send_telegram(session, f"⏳ Selling {percent}% of <code>{token_addr}</code>...", chat_id=chat_id)
                try:
                    from trader import sell_token
                    result = await sell_token(token_addr, percent)
                    if result["success"]:
                        tx_hash = result["tx_hash"]
                        amount_tokens = result.get("amount_tokens", 0)
                        await send_telegram(session, f"✅ <b>SELL executed</b> — {percent}% ({amount_tokens:.2f} tokens)\n🔗 <a href='https://basescan.org/tx/{tx_hash}'>View on BaseScan</a>", chat_id=chat_id)
                    else:
                        await send_telegram(session, f"❌ Sell failed: <code>{result.get('error','')[:200]}</code>", chat_id=chat_id)
                except Exception as e:
                    await send_telegram(session, f"❌ Sell error: {str(e)[:150]}", chat_id=chat_id)

            elif cmd == "/test":
                test_launch = {
                    "source": "bankr",
                    "address": "0x1234567890abcdef1234567890abcdef12345678",
                    "name": "Test Token",
                    "symbol": "TEST",
                    "x_username": "testuser",
                    "tweet_url": "",
                    "image_uri": "",
                }
                test_dex = {
                    "mcap": 98500,
                    "volume_24h": 74000,
                    "liquidity": 45200,
                    "price_usd": "0.000098",
                    "price_change_1h": 12.4,
                    "price_change_24h": 34.1,
                    "pair_url": "https://dexscreener.com/base/test",
                    "pair_created_at": int(time.time() - 180) * 1000,
                }
                _address_map["0x1234567890abcdef"] = "0x1234567890abcdef1234567890abcdef12345678"
                await send_signal(session, test_launch, test_dex, "bankr", "TEST")
                await send_telegram(session, "✅ Test signal sent", chat_id=chat_id)

            elif cmd == "/status":
                exec_status = f"✅ ON (${BANKR_BUY_AMOUNT}/trade)" if AUTO_EXECUTE else "❌ OFF"
                trade_status = "✅ ON" if TRADING_ENABLED else "❌ OFF"
                pushover_status = "✅ ON" if PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN else "❌ OFF"
                verdict_status = (
                    f"✅ ON ({AUTO_VERDICT_TIMEOUT_SEC:.0f}s timeout, {AUTO_VERDICT_MAX_CONCURRENT} concurrent)"
                    if AUTO_VERDICT_ENABLED else "❌ OFF"
                )
                async with db_session() as db:
                    db_status = await get_status_snapshot(db)
                await send_telegram(
                    session,
                    f"📡 <b>Signal Bot</b>\n\n"
                    f"• Sources: Bankr + Clanker + Virtuals + DexScreener\n"
                    f"• Active tenants: {db_status['tenants_active']}\n"
                    f"• Launches total: {db_status['launches_total']}\n"
                    f"• Launches signaled: {db_status['launches_signaled']}\n"
                    f"• Signals sent: {alert_count}\n"
                    f"• DB signals: {db_status['signals_total']}\n"
                    f"• Recheck queue: {db_status['queued_rechecks']}\n"
                    f"• Deliveries pending/retry/failed: {db_status['deliveries_pending']}/{db_status['deliveries_retry']}/{db_status['deliveries_failed']}\n"
                    f"• Provider cooldowns: {', '.join(db_status['provider_cooldowns']) or 'none'}\n"
                    f"• Blocked: {len(blocked_accounts)} accounts\n"
                    f"• Min MCap: ${MIN_MCAP:,}\n"
                    f"• Min Volume: ${MIN_VOLUME_24H:,}\n"
                    f"• Min Liquidity: ${MIN_LIQUIDITY:,} (DexScreener only)\n"
                    f"• 🔓 Safe sources (no liq check): {', '.join(SAFE_LAUNCHPADS)}\n"
                    f"• Poll interval: {POLL_INTERVAL}s\n"
                    f"• Auto-execute: {exec_status}\n"
                    f"• Executions: {execution_count}\n"
                    f"• Inline trading: {trade_status}\n"
                    f"• Auto-verdict: {verdict_status}\n"
                    f"• Pushover alerts: {pushover_status}",
                    chat_id,
                )

            elif cmd == "/verdict2":
                parts = text.split(maxsplit=1)
                ca = parts[1].strip() if len(parts) == 2 else ""
                if not is_base_contract(ca):
                    await send_telegram(session, "Usage: /verdict2 0xCONTRACT", chat_id)
                    continue
                await send_telegram(session, f"🤖 Running Verdict 2.0 for <code>{ca.lower()}</code>...", chat_id)
                try:
                    result = await analyze_ca_for_command(session, ca, requested_by="telegram_verdict2", include_summary=True)
                    await send_telegram(session, format_verdict2_report(result), chat_id)
                except Exception as e:
                    log.error(f"Verdict2 command failed for {ca}: {e}", exc_info=True)
                    await send_telegram(session, f"❌ Verdict 2.0 failed: {h(str(e)[:160])}", chat_id)

            elif cmd in ("/spoof-check", "/spoof_check"):
                parts = text.split(maxsplit=1)
                ca = parts[1].strip() if len(parts) == 2 else ""
                if not is_base_contract(ca):
                    await send_telegram(session, "Usage: /spoof_check 0xCONTRACT", chat_id)
                    continue
                await send_telegram(session, f"🕵️ Running spoof checks for <code>{ca.lower()}</code>...", chat_id)
                try:
                    result = await analyze_ca_for_command(session, ca, requested_by="telegram_spoof", include_summary=False)
                    await send_telegram(session, format_spoof_report(result), chat_id)
                except Exception as e:
                    log.error(f"Spoof command failed for {ca}: {e}", exc_info=True)
                    await send_telegram(session, f"❌ Spoof check failed: {h(str(e)[:160])}", chat_id)

            elif cmd == "/summary":
                parts = text.split(maxsplit=1)
                ca = parts[1].strip() if len(parts) == 2 else ""
                if not is_base_contract(ca):
                    await send_telegram(session, "Usage: /summary 0xCONTRACT", chat_id)
                    continue
                await send_telegram(session, f"🧠 Building summary for <code>{ca.lower()}</code>...", chat_id)
                try:
                    result = await analyze_ca_for_command(session, ca, requested_by="telegram_summary", include_summary=True)
                    await send_telegram(session, format_summary_report(result), chat_id)
                except Exception as e:
                    log.error(f"Summary command failed for {ca}: {e}", exc_info=True)
                    await send_telegram(session, f"❌ Summary failed: {h(str(e)[:160])}", chat_id)

            elif cmd == "/wallet":
                user_id = str(msg.get("from", {}).get("id", ""))
                if not is_trader_user(user_id):
                    await send_telegram(session, "⛔ Not authorized for trading wallet", chat_id)
                    continue
                if not TRADING_ENABLED:
                    await send_telegram(session, "⚠️ Trading not enabled.", chat_id)
                    continue
                try:
                    from trader import get_eth_balance, get_web3, get_wallet
                    w3 = get_web3()
                    account = get_wallet(w3)
                    eth_bal = await get_eth_balance(w3)
                    await send_telegram(
                        session,
                        f"💼 <b>Bot Wallet</b>\n\n"
                        f"<code>{account.address}</code>\n"
                        f"💎 ETH: <b>{eth_bal:.4f}</b>\n"
                        f"🔗 <a href='https://basescan.org/address/{account.address}'>BaseScan</a>",
                        chat_id,
                    )
                except Exception as e:
                    await send_telegram(session, f"❌ Wallet error: {e}", chat_id)

            elif cmd == "/track":
                parts = text.split(maxsplit=2)
                address = parts[1].strip() if len(parts) >= 2 else ""
                label = parts[2].strip() if len(parts) >= 3 else ""
                if add_tracked_wallet(address, label):
                    label_text = f" — {html.escape(label)}" if label else ""
                    await send_telegram(session, f"✅ Tracking wallet\n<code>{address.lower()}</code>{label_text}", chat_id)
                else:
                    await send_telegram(session, "Usage: /track 0xADDRESS [label]", chat_id)

            elif cmd == "/untrack":
                parts = text.split(maxsplit=1)
                address = parts[1].strip() if len(parts) == 2 else ""
                if remove_tracked_wallet(address):
                    await send_telegram(session, f"✅ Untracked wallet\n<code>{address.lower()}</code>", chat_id)
                else:
                    await send_telegram(session, "Wallet not found. Usage: /untrack 0xADDRESS", chat_id)

            elif cmd in ("/wallets", "/tracked_wallets"):
                wallets = load_tracked_wallets()
                if not wallets:
                    await send_telegram(session, "No wallets tracked yet.\nUse /track 0xADDRESS [label] to add one.", chat_id)
                    continue
                lines = [f"🐋 <b>Tracked Wallets ({len(wallets)})</b>"]
                for idx, wallet in enumerate(wallets[:30], 1):
                    label = html.escape(wallet.get("label", ""))
                    suffix = f" — {label}" if label else ""
                    address = wallet["address"]
                    lines.append(f"{idx}. <code>{address}</code>{suffix}")
                    lines.append(f"   <a href='https://basescan.org/address/{address}'>BaseScan</a>")
                await send_telegram(session, "\n".join(lines), chat_id)

            elif cmd in ("/research", "/r"):
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

            else:
                await send_telegram(session, "Unknown command. Use /help.", chat_id=chat_id)

    except Exception as e:
        log.warning(f"Telegram command check error: {e}")


# ─── SocialData.tools Follower Lookup ─────────────────────────────────────────

async def get_follower_count(session: aiohttp.ClientSession, username: str) -> int | None:
    username = username.lstrip("@").strip().lower()
    if not username:
        return None

    if username in follower_cache:
        return follower_cache[username]

    if not SOCIALDATA_API_KEY:
        log.warning(f"@{username} → SOCIALDATA_API_KEY not set!")
        follower_cache[username] = None
        return None
    if not await is_provider_available("socialdata"):
        log.debug(f"@{username} → SocialData cooldown active")
        return None

    count = None
    try:
        url = f"{SOCIALDATA_API_URL}/{username}"
        headers = {
            "Authorization": f"Bearer {SOCIALDATA_API_KEY}",
            "Accept": "application/json",
        }
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            await record_provider_response("socialdata", endpoint="twitter/user", status_code=resp.status, cooldown_seconds=120)
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


# ─── DexScreener Market Data ──────────────────────────────────────────────────

def choose_dexscreener_pair(pairs: list[dict], token_address: str) -> dict | None:
    token_address = token_address.lower()
    base_pairs = [
        pair for pair in pairs
        if ((pair.get("baseToken") or {}).get("address") or "").lower() == token_address
    ]
    candidate_pairs = base_pairs or [
        pair for pair in pairs
        if ((pair.get("quoteToken") or {}).get("address") or "").lower() == token_address
    ] or pairs

    best = None
    best_liq = -1.0
    for pair in candidate_pairs:
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        if liq > best_liq:
            best_liq = liq
            best = pair
    return best


def normalize_dexscreener_pair(best: dict, token_address: str) -> dict:
    mcap = float(best.get("marketCap") or best.get("fdv") or 0)
    vol_24h = float((best.get("volume") or {}).get("h24") or 0)
    liquidity = float((best.get("liquidity") or {}).get("usd") or 0)
    price_change = best.get("priceChange") or {}
    base_token = best.get("baseToken") or {}
    quote_token = best.get("quoteToken") or {}
    txns = best.get("txns") or {}
    info = best.get("info") or {}
    boosts = best.get("boosts") or {}
    token_meta = base_token
    if (base_token.get("address") or "").lower() != token_address.lower():
        token_meta = quote_token or base_token

    return {
        "mcap": mcap,
        "volume_24h": vol_24h,
        "liquidity": liquidity,
        "price_usd": best.get("priceUsd", "0"),
        "price_change_1h": float(price_change.get("h1") or 0),
        "price_change_24h": float(price_change.get("h24") or 0),
        "pair_url": best.get("url", f"https://dexscreener.com/base/{token_address}"),
        "pair_created_at": best.get("pairCreatedAt", 0),
        "pair_address": best.get("pairAddress", ""),
        "token_name": token_meta.get("name", ""),
        "token_symbol": token_meta.get("symbol", ""),
        "base_token_address": base_token.get("address", ""),
        "quote_token_address": quote_token.get("address", ""),
        "quote_token_symbol": quote_token.get("symbol", ""),
        "dex_id": best.get("dexId", ""),
        "txns_h1_buys": int((txns.get("h1") or {}).get("buys") or 0),
        "txns_h1_sells": int((txns.get("h1") or {}).get("sells") or 0),
        "txns_h24_buys": int((txns.get("h24") or {}).get("buys") or 0),
        "txns_h24_sells": int((txns.get("h24") or {}).get("sells") or 0),
        "boosts_active": int(boosts.get("active") or 0) if isinstance(boosts, dict) else 0,
        "image_url": info.get("imageUrl", ""),
        "websites": info.get("websites") or [],
        "socials": info.get("socials") or [],
        "_source": "dexscreener",
    }


async def _fetch_dexscreener(session: aiohttp.ClientSession, token_address: str) -> dict | None:
    """Fetch market data from DexScreener (primary source)."""
    if not await is_provider_available("dexscreener"):
        log.debug(f"DexScreener cooldown active, skipping {token_address[:10]}...")
        return None
    try:
        url = f"{DEXSCREENER_API_URL}/token-pairs/v1/base/{token_address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            await record_provider_response("dexscreener", endpoint="token-pairs/base", status_code=resp.status, cooldown_seconds=60)
            if resp.status == 404:
                return None
            if resp.status == 429:
                log.warning("DexScreener rate limited, backing off...")
                await asyncio.sleep(2)
                return None
            if resp.status != 200:
                log.debug(f"DexScreener {resp.status} for {token_address[:10]}...")
                return None
            raw = await resp.json()

        if isinstance(raw, list):
            pairs = raw
        elif isinstance(raw, dict):
            pairs = raw.get("pairs") or raw.get("data") or []
        else:
            pairs = []

        if not pairs:
            return None

        best = choose_dexscreener_pair(pairs, token_address)
        if not best:
            return None

        return normalize_dexscreener_pair(best, token_address)
    except Exception as e:
        log.debug(f"DexScreener error for {token_address[:10]}...: {e}")
        return None


# ─── GeckoTerminal rate limiter (30 calls/min free tier) ─────────────────────
_gecko_calls: list[float] = []
GECKO_RATE_LIMIT = 25  # stay under 30/min with safety margin
GECKO_COOLDOWN_SEC = int(os.getenv("GECKO_COOLDOWN_SEC", "60"))
_gecko_cooldown_until: float = 0


def _gecko_rate_ok() -> bool:
    """Check if we can make a GeckoTerminal call without hitting rate limit."""
    now = time.time()
    if now < _gecko_cooldown_until:
        return False
    # Purge calls older than 60s
    while _gecko_calls and _gecko_calls[0] < now - 60:
        _gecko_calls.pop(0)
    return len(_gecko_calls) < GECKO_RATE_LIMIT


def _mark_gecko_rate_limited() -> None:
    global _gecko_cooldown_until
    _gecko_cooldown_until = max(_gecko_cooldown_until, time.time() + GECKO_COOLDOWN_SEC)
    log.warning(f"GeckoTerminal rate limited (429), cooling down {GECKO_COOLDOWN_SEC}s")


async def _fetch_geckoterminal_api(session: aiohttp.ClientSession, token_address: str) -> dict | None:
    """Fetch market data from GeckoTerminal (fallback source). 30 calls/min free tier."""
    if not await is_provider_available("geckoterminal"):
        log.debug(f"GeckoTerminal cooldown active, skipping fallback for {token_address[:10]}...")
        return None
    if not _gecko_rate_ok():
        log.debug(f"GeckoTerminal rate limit reached, skipping fallback for {token_address[:10]}...")
        return None

    try:
        url = f"{GECKOTERMINAL_API_URL}/networks/base/tokens/{token_address}"
        headers = {"Accept": "application/json;version=20230302"}
        _gecko_calls.append(time.time())

        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            await record_provider_response("geckoterminal", endpoint="token", status_code=resp.status, cooldown_seconds=GECKO_COOLDOWN_SEC)
            if resp.status == 429:
                _mark_gecko_rate_limited()
                return None
            if resp.status != 200:
                log.debug(f"GeckoTerminal {resp.status} for {token_address[:10]}...")
                return None
            data = await resp.json()

        # GeckoTerminal returns token info — we need pool data for mcap/vol/liq
        # Try the pools endpoint instead
        token_data = data.get("data", {})
        attrs = token_data.get("attributes", {})

        # Now fetch pools for this token
        pools_url = f"{GECKOTERMINAL_API_URL}/networks/base/tokens/{token_address}/pools"
        params = {"page": 1}
        _gecko_calls.append(time.time())

        async with session.get(pools_url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            await record_provider_response("geckoterminal", endpoint="pools", status_code=resp.status, cooldown_seconds=GECKO_COOLDOWN_SEC)
            if resp.status == 429:
                _mark_gecko_rate_limited()
                return None
            if resp.status != 200:
                log.debug(f"GeckoTerminal pools {resp.status} for {token_address[:10]}...")
                return None
            pools_data = await resp.json()

        pools = pools_data.get("data", [])
        if not pools:
            return None

        # Find the best pool by reserve (liquidity)
        best = None
        best_reserve = -1
        for pool in pools:
            pool_attrs = pool.get("attributes", {})
            reserve = float(pool_attrs.get("reserve_in_usd") or 0)
            if reserve > best_reserve:
                best_reserve = reserve
                best = pool_attrs

        if not best:
            return None

        # Parse GeckoTerminal pool format
        mcap = float(best.get("market_cap_usd") or best.get("fdv_usd") or 0)
        vol_raw = best.get("volume_usd") or {}
        vol_24h = float(vol_raw.get("h24") or 0)
        liquidity = float(best.get("reserve_in_usd") or 0)
        price_changes = best.get("price_change_percentage") or {}

        token_name = attrs.get("name", "")
        token_symbol = attrs.get("symbol", "")

        result = {
            "mcap": mcap,
            "volume_24h": vol_24h,
            "liquidity": liquidity,
            "price_usd": best.get("base_token_price_usd", "0"),
            "price_change_1h": float(price_changes.get("h1") or 0),
            "price_change_24h": float(price_changes.get("h24") or 0),
            "pair_url": f"https://www.geckoterminal.com/base/pools/{best.get('address', token_address)}",
            "pair_created_at": 0,  # GeckoTerminal returns ISO date, convert if needed
            "token_name": token_name,
            "token_symbol": token_symbol,
            "dex_id": best.get("dex_id", ""),
            "_source": "geckoterminal",
        }

        # Try to parse pool_created_at from ISO string
        created_str = best.get("pool_created_at", "")
        if created_str:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                result["pair_created_at"] = int(dt.timestamp() * 1000)
            except Exception:
                pass

        log.info(f"  🦎 GeckoTerminal fallback: {token_address[:10]}... mcap ${mcap:,.0f} vol ${vol_24h:,.0f} liq ${liquidity:,.0f}")
        return result

    except Exception as e:
        log.debug(f"GeckoTerminal error for {token_address[:10]}...: {e}")
        return None


async def fetch_geckoterminal(session: aiohttp.ClientSession, token_address: str) -> dict | None:
    """Fetch market data: DexScreener first, GeckoTerminal fallback.

    GeckoTerminal is used when DexScreener returns:
    - No data at all (not indexed yet)
    - $0 liquidity (common with Uniswap V4 pairs)
    - $0 mcap but token exists
    """
    token_address = token_address.lower()

    if token_address in gecko_cache:
        cached_time, cached_result = gecko_cache[token_address]
        ttl = GECKO_CACHE_TTL_HIT if cached_result else GECKO_CACHE_TTL_MISS
        if time.time() - cached_time < ttl:
            return cached_result

    # ── Primary: DexScreener ──
    result = await _fetch_dexscreener(session, token_address)

    # ── Fallback: GeckoTerminal ──
    # Trigger when DexScreener returns nothing OR has data gaps
    needs_fallback = False
    if result is None:
        needs_fallback = True
    elif result.get("liquidity", 0) == 0 and result.get("mcap", 0) > 0:
        # DexScreener has the token but $0 liquidity — V4 indexing gap
        needs_fallback = True
    elif result.get("mcap", 0) == 0 and result.get("volume_24h", 0) == 0:
        # DexScreener returned empty data
        needs_fallback = True

    if needs_fallback:
        gecko_result = await _fetch_geckoterminal_api(session, token_address)
        if gecko_result:
            if result is None:
                # DexScreener had nothing — use GeckoTerminal entirely
                result = gecko_result
            else:
                # DexScreener had partial data — merge: prefer non-zero values
                if gecko_result.get("liquidity", 0) > result.get("liquidity", 0):
                    result["liquidity"] = gecko_result["liquidity"]
                if gecko_result.get("mcap", 0) > result.get("mcap", 0):
                    result["mcap"] = gecko_result["mcap"]
                if gecko_result.get("volume_24h", 0) > result.get("volume_24h", 0):
                    result["volume_24h"] = gecko_result["volume_24h"]
                if not result.get("pair_created_at") and gecko_result.get("pair_created_at"):
                    result["pair_created_at"] = gecko_result["pair_created_at"]
                result["_source"] = "dexscreener+geckoterminal"

    # Cache the result
    gecko_cache[token_address] = (time.time(), result)
    return result


MAX_TOKEN_AGE = int(os.getenv("MAX_TOKEN_AGE", str(4 * 3600)))


def passes_market_filters(dex: dict | None, source: str = "") -> tuple[bool, str]:
    """Check if token passes market filters.

    For safe launchpads (bankr, clanker, virtuals) — skip liquidity check
    because they have locked LP / bonding curves and can't rug.
    Only mcap + volume need to pass.
    """
    if dex is None:
        return False, "no market data"

    is_safe = source.lower() in SAFE_LAUNCHPADS

    pair_created = dex.get("pair_created_at", 0)
    if pair_created:
        age_seconds = time.time() - (pair_created / 1000)
        if age_seconds > MAX_TOKEN_AGE:
            age_hours = age_seconds / 3600
            return False, f"too old ({age_hours:.1f}h > {MAX_TOKEN_AGE//3600}h)"
    else:
        mcap_check = float(dex.get("mcap", 0))
        if mcap_check > 200_000:
            return False, f"no creation time + high mcap ${mcap_check:,.0f}, likely old"

    mcap = dex.get("mcap", 0)
    vol = dex.get("volume_24h", 0)
    liq = dex.get("liquidity", 0)

    failures = []
    if mcap < MIN_MCAP:
        failures.append(f"mcap ${mcap:,.0f} < ${MIN_MCAP:,}")
    if vol < MIN_VOLUME_24H:
        failures.append(f"vol ${vol:,.0f} < ${MIN_VOLUME_24H:,}")
    # Only check liquidity for non-safe sources (DexScreener discoveries, unknown DEXes)
    if not is_safe and liq < MIN_LIQUIDITY:
        failures.append(f"liq ${liq:,.0f} < ${MIN_LIQUIDITY:,}")

    if failures:
        return False, ", ".join(failures)

    return True, ""


# ─── Token Research ───────────────────────────────────────────────────────────

RESEARCH_TIER1_KEYWORDS = {
    "thesis", "conviction", "undervalued", "re-rating", "moat",
    "flywheel", "asymmetric", "mispriced", "alpha", "inevitable",
    "paradigm", "revaluation", "asymmetrical",
}
RESEARCH_TIER2_KEYWORDS = {
    "buy", "bag", "catalyst", "add", "loading", "dip", "entry",
    "hold", "accumulation", "position", "gem", "play", "upside",
    "watchlist", "long term", "early", "sleeper", "overlooked",
    "under the radar",
}
RESEARCH_METRIC_KEYWORDS = {
    "tvl", "volume", "revenue", "fees", "yield", "mainnet", "tge",
    "listing", "unlock", "fdv", "mc", "inflows", "outflows",
    "whale", "dominance", "holders", "burn", "deflationary",
    "onchain", "circulating supply",
}


def contains_term(text_lower: str, term: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(term.lower())}(?![A-Za-z0-9_])", text_lower))


def build_research_query(ticker: str, address: str = "") -> str:
    clean_ticker = (ticker or "").strip().lstrip("$")
    terms = []
    if address:
        terms.append(address.lower())
    if clean_ticker:
        terms.append(f"${clean_ticker.upper()}")
    if not terms:
        return ""
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def score_research_tweet(tweet: dict) -> dict:
    text = re.sub(r"https?://t\.co/\S+", "", tweet.get("text", "")).strip()
    lower = text.lower()
    score = 0
    tier = 3

    tier1_hits = sum(1 for kw in RESEARCH_TIER1_KEYWORDS if contains_term(lower, kw))
    tier2_hits = sum(1 for kw in RESEARCH_TIER2_KEYWORDS if contains_term(lower, kw))
    metric_hits = sum(1 for kw in RESEARCH_METRIC_KEYWORDS if contains_term(lower, kw))
    if tier1_hits:
        score += tier1_hits * 6
        tier = 1
    if tier2_hits:
        score += tier2_hits * 3
        if tier != 1:
            tier = 2
    score += metric_hits * 2

    if tweet.get("high_priority"):
        score += 5
        if tier == 3:
            tier = 2

    likes = int(tweet.get("likes") or 0)
    retweets = int(tweet.get("retweets") or 0)
    replies = int(tweet.get("replies") or 0)
    views = int(tweet.get("views") or 0)
    followers = int(tweet.get("followers") or 0)

    score += 6 if likes >= 200 else 4 if likes >= 50 else 2 if likes >= 10 else 0
    score += 4 if retweets >= 100 else 2 if retweets >= 25 else 1 if retweets >= 5 else 0
    score += 3 if replies >= 100 else 2 if replies >= 50 else 1 if replies >= 10 else 0
    score += 3 if views >= 100_000 else 2 if views >= 10_000 else 1 if views >= 1_000 else 0
    score += 4 if followers >= 100_000 else 3 if followers >= 50_000 else 2 if followers >= 10_000 else 1 if followers >= 1_000 else 0

    if any(contains_term(lower, noise) for noise in ("gm", "gn", "lol", "lmao", "vibes", "shitpost")):
        score -= 2

    tweet["score"] = max(score, 0)
    tweet["tier"] = tier
    return tweet


def parse_socialdata_tweet(tweet: dict) -> dict | None:
    user = tweet.get("user", {})
    username = user.get("screen_name", "")
    tweet_id = tweet.get("id_str", "")
    text = (tweet.get("full_text") or tweet.get("text") or "").strip()
    if not username or not tweet_id or not text:
        return None
    item = {
        "username": username,
        "name": user.get("name", ""),
        "followers": int(user.get("followers_count") or 0),
        "text": text[:500],
        "likes": int(tweet.get("favorite_count") or 0),
        "retweets": int(tweet.get("retweet_count") or 0),
        "replies": int(tweet.get("reply_count") or 0),
        "views": int(tweet.get("views_count") or 0),
        "date": (tweet.get("tweet_created_at") or "")[:16],
        "url": f"https://x.com/{username}/status/{tweet_id}",
        "high_priority": username.lower() in HIGH_PRIORITY_INFLUENCERS,
    }
    return score_research_tweet(item)


async def socialdata_search(
    session: aiohttp.ClientSession,
    query: str,
    search_type: str = "Top",
    limit: int = 20,
    timeout_sec: int = 10,
) -> list[dict]:
    if not SOCIALDATA_API_KEY:
        return []

    results = []
    try:
        url = "https://api.socialdata.tools/twitter/search"
        headers = {
            "Authorization": f"Bearer {SOCIALDATA_API_KEY}",
            "Accept": "application/json",
        }
        params = {"query": query, "type": search_type}

        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.debug(f"SocialData search {resp.status}: {body[:160]}")
                return []
            data = await resp.json()

        seen_urls = set()
        for tweet in data.get("tweets", []):
            item = parse_socialdata_tweet(tweet)
            if not item or item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            results.append(item)
            if len(results) >= limit:
                break
    except Exception as e:
        log.debug(f"SocialData search error for '{query[:60]}': {e}")

    return results


async def search_x_mentions(session: aiohttp.ClientSession, ticker: str, token_name: str = "", address: str = "") -> list[dict]:
    query = build_research_query(ticker, address)
    if not query:
        return []

    mentions = await socialdata_search(session, f"{query} min_faves:5", search_type="Top", limit=20)
    mentions = [
        m for m in mentions
        if m["followers"] >= RESEARCH_MIN_FOLLOWERS
        or m.get("high_priority")
        or m.get("score", 0) >= RESEARCH_HIGH_SIGNAL_SCORE
    ]
    mentions.sort(key=lambda m: (m.get("tier", 3) == 1, m.get("score", 0), m["followers"], m["likes"]), reverse=True)
    return mentions[:6]


async def search_x_ticker_recent(session: aiohttp.ClientSession, ticker: str, address: str = "", limit: int = 8) -> list[dict]:
    """Search X for most recent tweets mentioning $TICKER or contract."""
    query = build_research_query(ticker, address)
    if not query:
        return []
    return await socialdata_search(session, query, search_type="Latest", limit=limit)


async def search_influencer_mentions(session: aiohttp.ClientSession, ticker: str, address: str = "", limit: int = 8) -> list[dict]:
    """Search watched influencer accounts imported from Jarvis."""
    if not WATCHED_INFLUENCERS:
        return []

    token_query = build_research_query(ticker, address)
    if not token_query:
        return []

    found = []
    seen = set()
    batch_size = 12
    for i in range(0, len(WATCHED_INFLUENCERS), batch_size):
        batch = WATCHED_INFLUENCERS[i:i + batch_size]
        from_query = "(" + " OR ".join(f"from:{account}" for account in batch) + ")"
        tweets = await socialdata_search(session, f"{token_query} {from_query}", search_type="Latest", limit=limit)
        for tweet in tweets:
            if tweet["url"] in seen:
                continue
            tweet["watched_influencer"] = True
            seen.add(tweet["url"])
            found.append(tweet)
        if len(found) >= limit:
            break
    found.sort(key=lambda m: (m.get("high_priority", False), m.get("score", 0), m["followers"], m["likes"]), reverse=True)
    return found[:limit]


async def resolve_deployer_x(session: aiohttp.ClientSession, address: str) -> dict:
    result = {
        "x_username": "",
        "farcaster_username": "",
        "farcaster_display": "",
        "source_method": "",
        "follower_count": None,
    }

    try:
        url = f"https://www.clanker.world/api/tokens?sort=desc&page=1&pageSize=50"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                tokens = data.get("data", data if isinstance(data, list) else [])
                for token in tokens:
                    token_addr = (token.get("contract_address") or token.get("address") or "").lower()
                    if token_addr == address.lower():
                        msg_sender = token.get("msg_sender", "")
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
                                            result["source_method"] = f"clanker search-creator (wallet {msg_sender[:10]}...)"
                            except Exception as e:
                                log.debug(f"Clanker search-creator error: {e}")

                        if not result["x_username"]:
                            desc = token.get("description", "") or ""
                            auto_match = re.search(r'(?:automated|requested|launched|created|deployed)\s+by\s+@(\w{1,15})', desc, re.IGNORECASE)
                            if auto_match:
                                result["x_username"] = auto_match.group(1)
                                result["source_method"] = "clanker description (automated by)"
                            else:
                                desc_match = re.search(r'@(\w{1,15})', desc)
                                if desc_match:
                                    candidate = desc_match.group(1)
                                    if candidate.lower() not in ("clanker", "bankr", "bankrbot", "base", "everyone"):
                                        result["x_username"] = candidate
                                        result["source_method"] = "clanker description"
                        break
    except Exception as e:
        log.debug(f"Clanker deployer lookup error: {e}")

    if not result["x_username"] and result["farcaster_username"]:
        try:
            fc_user = result["farcaster_username"]
            followers = await get_follower_count(session, fc_user)
            if followers and followers > 100:
                result["x_username"] = fc_user
                result["follower_count"] = followers
                result["source_method"] += " → X match via farcaster username"
        except Exception:
            pass

    if result["x_username"] and result["follower_count"] is None:
        result["follower_count"] = await get_follower_count(session, result["x_username"])

    return result


async def research_token(session: aiohttp.ClientSession, query: str) -> str:
    query = query.strip().lstrip("$").upper()
    if not query:
        return "Usage: /research $TICKER or /research 0x..."

    is_address = query.lower().startswith("0x") and len(query) == 42
    ticker = query
    dex = None
    address = ""
    token_name = ""

    if is_address:
        address = query.lower()
        dex = await fetch_geckoterminal(session, address)
        if dex:
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
        try:
            url = f"{GECKOTERMINAL_API_URL}/search/pools?query={ticker}&network=base&page=1"
            headers = {"Accept": "application/json;version=20230302"}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pools = data.get("data", [])
                    best_pool = None
                    best_score = -1

                    for pool in pools:
                        pool_attrs = pool.get("attributes", {})
                        pool_name = pool_attrs.get("name", "")
                        vol_raw = pool_attrs.get("volume_usd") or {}
                        vol_24h = float(vol_raw.get("h24") or 0)
                        base_addr = pool.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
                        if base_addr:
                            base_addr = base_addr.replace("base_", "")
                        if not base_addr:
                            continue
                        base_symbol = pool_name.split(" / ")[0].strip().upper() if " / " in pool_name else ""
                        score = vol_24h + (1_000_000_000 if base_symbol == ticker else 0)
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

    deployer_info = None
    if address:
        deployer_info = await resolve_deployer_x(session, address)

    x_mentions = await search_x_mentions(session, ticker, token_name, address)
    influencer_mentions = await search_influencer_mentions(session, ticker, address)
    existing_urls = {m["url"] for m in x_mentions}
    influencer_mentions = [m for m in influencer_mentions if m["url"] not in existing_urls]
    launch_status = None
    if address:
        async with db_session() as db:
            launch_status = await get_launch_status(db, address)
    was_alerted = launch_status == "signaled"

    if not dex and not x_mentions and not influencer_mentions:
        return (
            f"🔍 <b>No data found for ${ticker}</b>\n\n"
            f"No market data on Base or notable X mentions.\n"
            f"Token might be on another chain.\n"
            f"Try: /research 0x..."
        )

    safe_name = h(token_name or ticker)
    safe_ticker = h(ticker)
    lines = [f"🔍 <b>Research: {safe_name}</b> (${safe_ticker})\n"]

    if address:
        lines.append(f"📋 <code>{address}</code>\n")
        lines.append(f"🔎 <a href='{build_x_research_url(address, ticker)}'>X Research</a>\n")

    lines.extend([
        "🧠 <b>Quick take</b> <i>(stub)</i>",
        f"└ {h(build_research_takeaway(dex, x_mentions, influencer_mentions))}",
        "",
    ])

    if dex:
        change_1h = dex.get("price_change_1h", 0)
        change_24h = dex.get("price_change_24h", 0)
        lines.extend([
            f"📊 <b>Market Data:</b>",
            f"├ 💰 MCap: {fmt_usd(dex['mcap'])}",
            f"├ 💧 Liquidity: {fmt_usd(dex['liquidity'])}",
            f"├ 📈 Volume 24h: {fmt_usd(dex['volume_24h'])}",
            f"├ 💵 Price: ${float(dex.get('price_usd') or 0):.6f}",
            f"├ {'🟢' if change_1h >= 0 else '🔴'} 1h: {change_1h:+.1f}%",
            f"└ {'🟢' if change_24h >= 0 else '🔴'} 24h: {change_24h:+.1f}%",
            "",
        ])
        passes, reason = passes_market_filters(dex)
        lines.append(("✅ Passes market filters\n") if passes else (f"❌ Fails filter: {reason}\n"))
    else:
        lines.append("⚠️ No market data found\n")

    if was_alerted:
        lines.append("📡 Token already signaled by scanner\n")
    elif launch_status:
        lines.append(f"🗂 Scanner status: <code>{h(launch_status)}</code>\n")

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

        lines.append(f"└ 🔎 Found via: {method}" if method else "└ 🔎 No deployer info in API metadata")
        lines.append("")

    if x_mentions:
        lines.append(f"🐦 <b>Notable X mentions (${ticker} / CA):</b>")
        for m in x_mentions:
            f_count = m['followers']
            f_str = f"{f_count/1_000_000:.1f}M" if f_count >= 1_000_000 else f"{f_count/1_000:.0f}K" if f_count >= 1_000 else str(f_count)
            text_clean = re.sub(r'https?://t\.co/\S+', '', m['text']).strip().replace('\n', ' ').replace('  ', ' ')
            text_clean = html.escape(text_clean)
            if len(text_clean) > 280:
                text_clean = text_clean[:277] + "..."
            tier = m.get("tier", 3)
            score = m.get("score", 0)
            hp = " ★" if m.get("high_priority") else ""
            lines.extend([
                f"",
                f"├ <a href='{m['url']}'>@{html.escape(m['username'])}</a>{hp} ({f_str} followers) · T{tier}/S{score} · {m['date']}",
                f"│ ❤️ {m['likes']} 🔁 {m['retweets']} 💬 {m.get('replies', 0)}",
                f"│ <i>{text_clean}</i>" if text_clean else f"│ <i>[media only]</i>",
            ])
        lines.append("")
    else:
        lines.append(f"\n🐦 No notable X mentions found for ${ticker}\n")

    if influencer_mentions:
        lines.append(f"👀 <b>Watched influencer mentions:</b>")
        for m in influencer_mentions[:6]:
            f_count = m['followers']
            f_str = f"{f_count/1_000_000:.1f}M" if f_count >= 1_000_000 else f"{f_count/1_000:.0f}K" if f_count >= 1_000 else str(f_count)
            text_clean = re.sub(r'https?://t\.co/\S+', '', m['text']).strip().replace('\n', ' ').replace('  ', ' ')
            text_clean = html.escape(text_clean)
            if len(text_clean) > 220:
                text_clean = text_clean[:217] + "..."
            hp = " ★" if m.get("high_priority") else ""
            lines.extend([
                f"",
                f"├ <a href='{m['url']}'>@{html.escape(m['username'])}</a>{hp} ({f_str}) · S{m.get('score', 0)} · {m['date']}",
                f"│ <i>{text_clean}</i>" if text_clean else f"│ <i>[media only]</i>",
            ])
        lines.append("")

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

        all_launches = data.get("launches", data if isinstance(data, list) else [])
        log.info(f"Bankr: {len(all_launches)} launches fetched")

        for launch in all_launches:
            address = (launch.get("tokenAddress") or "").lower()
            if not address:
                continue
            if (launch.get("chain") or "base").lower() != "base":
                continue
            deployer = launch.get("deployer", {}) or {}
            x_username = deployer.get("xUsername", "")
            normalized.append({
                "source": "bankr",
                "address": address,
                "name": launch.get("tokenName", "Unknown"),
                "symbol": launch.get("tokenSymbol", "?"),
                "x_username": x_username or "",
                "deployer_wallet": deployer.get("walletAddress", ""),
                "status": launch.get("status", ""),
                "tweet_url": launch.get("tweetUrl", ""),
                "image_uri": launch.get("imageUri", ""),
                "website_url": launch.get("websiteUrl", ""),
                "created_at": launch.get("createdAt") or launch.get("launchedAt") or launch.get("timestamp") or "",
            })
    except Exception as e:
        log.error(f"Bankr fetch error: {e}")
    return normalized


# ─── Clanker API ──────────────────────────────────────────────────────────────

async def fetch_clanker(session: aiohttp.ClientSession) -> list[dict]:
    if not await is_provider_available("clanker"):
        log.debug("Clanker cooldown active, skipping source")
        return []

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://www.clanker.world/clankers/chain/base",
        "Origin": "https://www.clanker.world",
    }
    normalized = []
    try:
        all_tokens = []
        seen_addresses = set()
        for offset in range(0, max(1, CLANKER_POLL_PAGES) * CLANKER_PAGE_SIZE, CLANKER_PAGE_SIZE):
            params = {
                "limit": CLANKER_PAGE_SIZE,
                "offset": offset,
                "includeMarket": "false",
                "includeUser": "true",
                "sort": "desc",
                "sortBy": "deployed-at",
                "chainId": CLANKER_CHAIN_ID_BASE,
            }
            async with session.get(CLANKER_API_URL, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                await record_provider_response(
                    "clanker",
                    endpoint="tokens",
                    status_code=resp.status,
                    cooldown_seconds=300 if resp.status == 403 else 60,
                    reason="vercel challenge" if resp.status == 403 else "",
                )
                if resp.status != 200:
                    if offset == 0:
                        log.warning(f"Clanker API returned {resp.status}")
                    break
                data = await resp.json()
            tokens = data.get("data", data if isinstance(data, list) else [])
            for token in tokens:
                address = (token.get("contract_address") or token.get("address") or "").lower()
                if not address or address in seen_addresses:
                    continue
                seen_addresses.add(address)
                all_tokens.append(token)
            if len(tokens) < CLANKER_PAGE_SIZE:
                break

        log.info(f"Clanker: {len(all_tokens)} launches fetched")

        for token in all_tokens:
            address = (token.get("contract_address") or token.get("address") or "").lower()
            if not address:
                continue
            if int(token.get("chain_id") or CLANKER_CHAIN_ID_BASE) != CLANKER_CHAIN_ID_BASE:
                continue

            x_username = ""
            social_urls = token.get("socialMediaUrls") or token.get("socialLinks") or []
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
                "deployer_wallet": token.get("msg_sender", ""),
                "tweet_url": "",
                "image_uri": token.get("img_url", ""),
                "created_at": token.get("deployed_at") or token.get("created_at") or token.get("createdAt") or "",
            })
    except Exception as e:
        log.error(f"Clanker fetch error: {e}")
    return normalized


# ─── DexScreener Discovery API ────────────────────────────────────────────────

def extract_x_username_from_links(links: list[dict] | None) -> str:
    for link in links or []:
        url = (link.get("url") or "").strip()
        if "twitter.com/" not in url and "x.com/" not in url:
            continue
        match = re.search(r'(?:twitter\.com|x\.com)/(@?\w{1,15})', url)
        if not match:
            continue
        candidate = match.group(1).lstrip("@")
        if candidate.lower() not in ("home", "explore", "search", "settings", "i"):
            return candidate
    return ""


def extract_website_from_links(links: list[dict] | None) -> str:
    for link in links or []:
        url = (link.get("url") or "").strip()
        link_type = (link.get("type") or "").lower()
        if not url or link_type in {"twitter", "telegram", "discord"}:
            continue
        if "x.com/" in url or "twitter.com/" in url or "t.me/" in url:
            continue
        return url
    return ""


async def fetch_dexscreener_bulk_market(session: aiohttp.ClientSession, addresses: list[str]) -> dict[str, dict]:
    if not addresses:
        return {}
    if not await is_provider_available("dexscreener"):
        return {}

    market_by_ca: dict[str, dict] = {}
    for i in range(0, len(addresses), 30):
        batch = addresses[i:i + 30]
        url = f"{DEXSCREENER_API_URL}/tokens/v1/base/{','.join(batch)}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                await record_provider_response("dexscreener", endpoint="tokens/base", status_code=resp.status, cooldown_seconds=60)
                if resp.status == 429:
                    log.warning("DexScreener bulk token lookup rate limited, backing off...")
                    break
                if resp.status != 200:
                    log.debug(f"DexScreener bulk token lookup returned {resp.status}")
                    continue
                raw = await resp.json()
        except Exception as e:
            log.debug(f"DexScreener bulk token lookup error: {e}")
            continue

        pairs = raw if isinstance(raw, list) else raw.get("pairs", []) if isinstance(raw, dict) else []
        for address in batch:
            token_pairs = [
                pair for pair in pairs
                if ((pair.get("baseToken") or {}).get("address") or "").lower() == address
                or ((pair.get("quoteToken") or {}).get("address") or "").lower() == address
            ]
            best = choose_dexscreener_pair(token_pairs, address)
            if best:
                market_by_ca[address] = normalize_dexscreener_pair(best, address)
    return market_by_ca


async def fetch_dexscreener_discoveries(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch Base token discoveries from DexScreener latest profile/boost streams."""
    if not DEXSCREENER_DISCOVERY_ENABLED:
        return []
    if not await is_provider_available("dexscreener"):
        log.debug("DexScreener cooldown active, skipping discovery")
        return []

    by_address: dict[str, dict] = {}
    try:
        for source_method, path in DEXSCREENER_DISCOVERY_ENDPOINTS:
            url = f"{DEXSCREENER_API_URL}{path}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                await record_provider_response("dexscreener", endpoint=source_method, status_code=resp.status, cooldown_seconds=60)
                if resp.status == 429:
                    log.warning(f"DexScreener {source_method} rate limited, skipping discovery")
                    break
                if resp.status != 200:
                    log.debug(f"DexScreener {source_method} returned {resp.status}")
                    continue
                raw = await resp.json()

            rows = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
            for item in rows:
                if (item.get("chainId") or "").lower() != "base":
                    continue
                address = (item.get("tokenAddress") or "").lower()
                if not is_base_contract(address) or address in by_address:
                    continue
                links = item.get("links") or []
                by_address[address] = {
                    "source": "dexscreener",
                    "address": address,
                    "name": "",
                    "symbol": "?",
                    "x_username": extract_x_username_from_links(links),
                    "tweet_url": "",
                    "website_url": extract_website_from_links(links),
                    "image_uri": item.get("icon") or "",
                    "description": item.get("description") or "",
                    "pair_url": item.get("url") or f"https://dexscreener.com/base/{address}",
                    "source_method": source_method,
                    "created_at": item.get("date") or item.get("claimDate") or "",
                }
                if len(by_address) >= DEXSCREENER_DISCOVERY_LIMIT:
                    break
            if len(by_address) >= DEXSCREENER_DISCOVERY_LIMIT:
                break

        addresses = list(by_address)
        market_by_ca = await fetch_dexscreener_bulk_market(session, addresses)
        for address, dex in market_by_ca.items():
            launch = by_address[address]
            launch["_dex"] = dex
            launch["name"] = dex.get("token_name") or launch.get("name") or "Unknown"
            launch["symbol"] = dex.get("token_symbol") or launch.get("symbol") or "?"
            if dex.get("pair_created_at") and not launch.get("created_at"):
                launch["created_at"] = dex["pair_created_at"]

        launches = list(by_address.values())
        log.info(f"DexScreener: {len(launches)} Base discoveries fetched")
        return launches
    except Exception as e:
        log.error(f"DexScreener discovery error: {e}")
        return []


# ─── Virtuals API ─────────────────────────────────────────────────────────────

async def fetch_virtuals(session: aiohttp.ClientSession) -> list[dict]:
    normalized = []
    try:
        for status in [5, 3, 1, 2, 4]:
            params = {
                "filters[status]": status,
                "sort": "createdAt:desc",
                "populate[0]": "image",
                "pagination[page]": 1,
                "pagination[pageSize]": 50,
            }
            async with session.get(VIRTUALS_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()

            agents = data.get("data", [])
            status_labels = {5: "sentient", 3: "prototype", 1: "init", 2: "pending", 4: "bonding"}
            log.info(f"Virtuals ({status_labels.get(status, status)}): {len(agents)} agents fetched")

            for agent in agents:
                address = (agent.get("tokenAddress") or "").lower()
                if not address:
                    continue

                agent_socials = agent.get("socials", {}) or {}
                verified_usernames = agent_socials.get("VERIFIED_USERNAMES", {}) or {}
                x_username = verified_usernames.get("TWITTER", "")

                creator = agent.get("creator", {}) or {}
                creator_socials = creator.get("socials", {}) or {}
                creator_verified = creator_socials.get("VERIFIED_USERNAMES", {}) or {}
                creator_x = creator_verified.get("TWITTER", "")

                if not x_username:
                    x_username = creator_x

                video_pitch = agent_socials.get("VIDEO_PITCH", {}) or {}
                image = agent.get("image", {}) or {}

                normalized.append({
                    "source": "virtuals",
                    "address": address,
                    "name": agent.get("name", "Unknown"),
                    "symbol": agent.get("symbol", "?"),
                    "x_username": x_username or "",
                    "creator_x": creator_x or "",
                    "tweet_url": video_pitch.get("TWEET_URL", ""),
                    "image_uri": image.get("url", ""),
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


def h(value) -> str:
    return html.escape(str(value or ""), quote=True)


def clean_x_handle(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", str(value or "").strip().lstrip("@"))[:32]


def fmt_token_age(dex: dict | None) -> str:
    if not dex:
        return "n/a"
    pair_created = dex.get("pair_created_at", 0)
    if not pair_created:
        return "n/a"
    age_seconds = max(0, time.time() - (pair_created / 1000))
    if age_seconds < 3600:
        return f"{int(age_seconds / 60)}m"
    if age_seconds < 86400:
        return f"{age_seconds / 3600:.1f}h"
    return f"{age_seconds / 86400:.1f}d"


def build_signal_reason(launch: dict, dex: dict | None) -> str:
    reasons: list[str] = []
    source = launch.get("source", "")
    if source in SAFE_LAUNCHPADS:
        reasons.append(f"{source.title()} launchpad")
    elif source:
        reasons.append(f"{source.title()} discovery")

    if dex:
        mcap = float(dex.get("mcap") or 0)
        volume = float(dex.get("volume_24h") or 0)
        liquidity = float(dex.get("liquidity") or 0)
        market_ok = mcap >= MIN_MCAP and volume >= MIN_VOLUME_24H
        if source not in SAFE_LAUNCHPADS:
            market_ok = market_ok and liquidity >= MIN_LIQUIDITY
        if market_ok:
            reasons.append("market filters passed")
        if source in SAFE_LAUNCHPADS:
            reasons.append("liq check skipped")

    if launch.get("x_username") or launch.get("creator_x"):
        reasons.append("deployer social found")

    return "; ".join(reasons[:3]) if reasons else "Passed scan filters"


def build_ai_summary_placeholder(launch: dict, dex: dict | None, verdict: dict | None = None) -> str:
    if verdict:
        return verdict.get("human_readable") or ""

    market = "pending market read"
    if dex:
        market = (
            f"MC {fmt_usd(float(dex.get('mcap') or 0))} · "
            f"Vol {fmt_usd(float(dex.get('volume_24h') or 0))} · "
            f"Liq {fmt_usd(float(dex.get('liquidity') or 0))}"
        )
        age = fmt_token_age(dex)
        if age != "n/a":
            market += f" · Age {age}"

    return (
        "🧠 <b>AI brief</b> • Score pending\n\n"
        f"• <b>Type:</b> pending classification\n"
        "• <b>Owner:</b> pending Base/X identity check\n"
        f"• <b>Market:</b> {h(market)}\n"
        "• <b>Product:</b> pending narrative/product read\n"
        "• <b>Risks:</b> pending spoof/liquidity checks"
    )


def build_research_takeaway(dex: dict | None, x_mentions: list[dict], influencer_mentions: list[dict]) -> str:
    if not dex and not x_mentions and not influencer_mentions:
        return "No Base market or social signal found yet."
    positives: list[str] = []
    risks: list[str] = []
    if dex:
        passes, reason = passes_market_filters(dex)
        if passes:
            positives.append("market filters pass")
        else:
            risks.append(reason)
    else:
        risks.append("market data missing")

    if influencer_mentions:
        positives.append(f"{len(influencer_mentions)} watched mention(s)")
    elif x_mentions:
        positives.append(f"{len(x_mentions)} notable X mention(s)")
    else:
        risks.append("no notable X coverage")

    left = "; ".join(positives[:2]) if positives else "weak confirmation"
    right = "; ".join(risks[:2]) if risks else "no major deterministic risk flagged"
    return f"{left}. Risk: {right}."


SOURCE_EMOJIS = {"bankr": "🏦", "clanker": "⚙️", "virtuals": "🤖", "dexscreener": "📊"}


def format_signal_telegram(launch: dict, dex: dict | None, executed: bool = False, job_id: str = "") -> str:
    source_key = launch["source"]
    source = h(source_key.upper())
    name = h(launch["name"])
    symbol = h(str(launch["symbol"]).lstrip("$"))
    address = launch["address"]
    x_username = clean_x_handle(launch.get("x_username", ""))
    tweet_url = h(launch.get("tweet_url", ""))
    source_emoji = SOURCE_EMOJIS.get(source_key, "📡")

    market_line = "Market: n/a"
    momentum_line = ""
    if dex:
        change_1h = dex.get("price_change_1h", 0)
        change_emoji = "🟢" if change_1h >= 0 else "🔴"
        liq_val = dex.get('liquidity', 0)
        liq_note = " (safe source)" if source_key in SAFE_LAUNCHPADS else ""
        market_line = (
            f"MCap {fmt_usd(dex['mcap'])} · Vol {fmt_usd(dex['volume_24h'])} · "
            f"Liq {fmt_usd(liq_val)}{liq_note}"
        )
        momentum_line = f"{change_emoji} 1h {change_1h:+.1f}% · Age {fmt_token_age(dex)}"

    identity_bits = [f"{source_emoji} {source}"]
    if x_username:
        identity_bits.append(f"<a href='https://x.com/{x_username}'>@{x_username}</a>")

    extra_lines: list[str] = []
    if source_key == "virtuals":
        creator_x = clean_x_handle(launch.get("creator_x", ""))
        if creator_x and creator_x != x_username:
            extra_lines.append(f"Creator <a href='https://x.com/{creator_x}'>@{creator_x}</a>")
        holders = launch.get("holder_count", 0)
        if holders:
            extra_lines.append(f"Holders {holders:,}")

    if source_key == "dexscreener":
        dex_id = launch.get("dex_id", "")
        extra_lines.append(f"Via {h(dex_id.title() if dex_id else 'Unknown DEX')}")

    execution_line = ""
    if executed:
        execution_line = f"\n💸 Auto-bought ${BANKR_BUY_AMOUNT} via Bankr" + (f" (job: <code>{job_id}</code>)" if job_id else "")
    elif AUTO_EXECUTE and not BANKR_EXECUTION_API_KEY:
        execution_line = "\n⚠️ Auto-execute ON but no API key set"

    links = [
        f"<a href='https://www.geckoterminal.com/base/tokens/{address}'>Gecko</a>",
        f"<a href='https://gmgn.ai/base/token/{address}'>GMGN</a>",
    ]
    if source_key == "clanker":
        links.append(f"<a href='https://www.clanker.world/clanker/{address}'>Clanker</a>")
    elif source_key == "virtuals":
        vid = h(launch.get("virtuals_id", ""))
        if vid:
            links.append(f"<a href='https://app.virtuals.io/virtuals/{vid}'>Virtuals</a>")
    elif source_key == "dexscreener":
        pair_url = h(launch.get("pair_url", ""))
        if pair_url:
            links.append(f"<a href='{pair_url}'>Chart</a>")
    if tweet_url:
        links.append(f"<a href='{tweet_url}'>Tweet</a>")
    links.append(f"<a href='https://app.uniswap.org/swap?chain=base&amp;outputCurrency={address}'>Uniswap</a>")

    details_line = " · ".join(identity_bits)
    if extra_lines:
        details_line += "\n" + " · ".join(extra_lines[:2])

    return (
        f"📡 <b>${symbol}</b> · {name}\n"
        f"{details_line}\n\n"
        f"📊 <b>Snapshot</b>\n"
        f"├ {market_line}\n"
        f"└ {momentum_line or 'Momentum n/a'}\n\n"
        f"🎯 <b>Why surfaced</b>\n"
        f"└ {h(build_signal_reason(launch, dex))}\n\n"
        f"{build_ai_summary_placeholder(launch, dex)}"
        f"{execution_line}\n\n"
        f"🔗 " + " · ".join(links) + "\n"
        f"<code>{address}</code>\n"
        f"/research {address}"
    )


def format_alert_whatsapp(launch: dict, dex: dict | None, executed: bool = False) -> str:
    source = launch["source"].upper()
    name = launch["name"]
    symbol = launch["symbol"]
    address = launch["address"]
    x_username = launch.get("x_username", "")
    tweet_url = launch.get("tweet_url", "")
    source_emoji = SOURCE_EMOJIS.get(launch["source"], "📡")

    lines = [f"📡 *SIGNAL* {source_emoji} {source}", "", f"*{name}* (${symbol})"]

    if x_username:
        lines.append(f"👤 Deployer: @{x_username}")

    if launch["source"] == "virtuals":
        creator_x = launch.get("creator_x", "")
        if creator_x and creator_x != x_username:
            lines.append(f"👷 Creator: @{creator_x}")
        holders = launch.get("holder_count", 0)
        if holders:
            lines.append(f"👥 Holders: {holders:,}")

    if launch["source"] == "dexscreener":
        dex_id = launch.get("dex_id", "")
        lines.append(f"🏭 Via: {dex_id.title() if dex_id else 'Unknown DEX'}")

    if dex:
        change_1h = dex.get("price_change_1h", 0)
        change_emoji = "🟢" if change_1h >= 0 else "🔴"
        liq_note = " 🔓" if launch["source"] in SAFE_LAUNCHPADS else ""
        lines.extend([
            "",
            f"├ 💰 MCap: {fmt_usd(dex['mcap'])}",
            f"├ 📈 Vol: {fmt_usd(dex['volume_24h'])}",
            f"├ 💧 Liq: {fmt_usd(dex['liquidity'])}{liq_note}",
            f"└ {change_emoji} 1h: {change_1h:+.1f}%",
        ])

    if executed:
        lines.extend(["", f"💸 Auto-bought ${BANKR_BUY_AMOUNT} via Bankr"])

    lines.extend([
        "", "🔗 Links:",
        f"├ Gecko: https://www.geckoterminal.com/base/tokens/{address}",
        f"├ GMGN: https://gmgn.ai/base/token/{address}",
    ])
    if launch["source"] == "clanker":
        lines.append(f"├ Clanker: https://www.clanker.world/clanker/{address}")
    elif launch["source"] == "virtuals":
        vid = launch.get("virtuals_id", "")
        if vid:
            lines.append(f"├ Virtuals: https://app.virtuals.io/virtuals/{vid}")
    elif launch["source"] == "dexscreener":
        pair_url = launch.get("pair_url", "")
        if pair_url:
            lines.append(f"├ Chart: {pair_url}")
    if x_username:
        lines.append(f"├ X: https://x.com/{x_username}")
    if tweet_url:
        lines.append(f"├ Tweet: {tweet_url}")
    lines.extend([
        f"└ Uniswap: https://app.uniswap.org/swap?chain=base&outputCurrency={address}",
        "", address,
    ])

    return "\n".join(lines)


def parse_launch_datetime(launch: dict) -> datetime | None:
    for key in ("created_at", "createdAt", "launched_at", "launchedAt", "deployed_at", "deployedAt", "timestamp"):
        raw = launch.get(key)
        if not raw:
            continue
        if isinstance(raw, (int, float)):
            value = float(raw)
            if value > 10_000_000_000:
                value = value / 1000
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


async def persist_launch_seen(launch: dict, status: str = "new") -> tuple[bool, str]:
    address = (launch.get("address") or "").lower()
    if not address:
        return False, ""
    async with db_session() as db:
        _, inserted = await upsert_launch(
            db,
            ca=address,
            ticker=launch.get("symbol", ""),
            name=launch.get("name", ""),
            source=launch.get("source", ""),
            raw_json=launch,
            launched_at=parse_launch_datetime(launch),
            status=status,
        )
    return inserted, address


def recheck_delay_for(reason: str, no_data: bool = False) -> int:
    if no_data:
        return min(RECHECK_INTERVAL, 120)
    return RECHECK_INTERVAL


def seconds_since(dt: datetime | None) -> float:
    if not dt:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (utc_now() - dt).total_seconds())


async def is_provider_available(provider: str) -> bool:
    async with db_session() as db:
        return await provider_available(db, provider)


async def record_provider_response(
    provider: str,
    *,
    endpoint: str = "",
    status_code: int | None = None,
    cooldown_seconds: int = 60,
    reason: str = "",
) -> None:
    async with db_session() as db:
        await record_api_budget_event(
            db,
            provider=provider,
            endpoint=endpoint,
            status_code=status_code,
        )
        if status_code in {403, 429}:
            await set_provider_cooldown(
                db,
                provider=provider,
                cooldown_until=utc_now() + timedelta(seconds=cooldown_seconds),
                reason=reason or ("access challenge" if status_code == 403 else "rate limited"),
            )


async def persist_recheck(
    address: str,
    reason: str,
    *,
    no_data: bool,
    dex: dict | None = None,
) -> None:
    async with db_session() as db:
        await queue_recheck(
            db,
            ca=address,
            reason=reason,
            next_check_at=utc_now() + timedelta(seconds=recheck_delay_for(reason, no_data=no_data)),
            no_data=no_data,
            market_json=dex,
            last_mcap=float((dex or {}).get("mcap") or 0) if dex else None,
        )


async def process_delivery_retries(session: aiohttp.ClientSession) -> int:
    async with db_session() as db:
        due = await get_due_delivery_retries(db, now=utc_now(), limit=TELEGRAM_RETRY_BATCH)

    processed = 0
    for delivery in due:
        payload = delivery.payload_json or {}
        text = payload.get("telegram_text")
        reply_markup = payload.get("reply_markup")
        if not text:
            async with db_session() as db:
                await mark_delivery_failed(db, delivery_id=delivery.id, error="missing delivery payload")
            continue
        if delivery.attempt_count >= TELEGRAM_MAX_DELIVERY_ATTEMPTS:
            async with db_session() as db:
                await mark_delivery_failed(db, delivery_id=delivery.id, error="max telegram delivery attempts reached")
            continue

        async with db_session() as db:
            await mark_delivery_sending(db, delivery_id=delivery.id)

        message_id = await send_telegram(
            session,
            text,
            chat_id=delivery.destination_id,
            reply_markup=reply_markup,
        )
        async with db_session() as db:
            if message_id is not None:
                await mark_delivery_sent(db, delivery_id=delivery.id, message_id=str(message_id))
                processed += 1
            else:
                backoff = min(60 * (2 ** max(delivery.attempt_count, 0)), 900)
                await mark_delivery_retry(
                    db,
                    delivery_id=delivery.id,
                    error="telegram retry failed",
                    next_retry_at=utc_now() + timedelta(seconds=backoff),
                )
    return processed


async def ensure_launch_for_analysis(session: aiohttp.ClientSession, ca: str) -> tuple[dict, dict | None]:
    ca = ca.lower()
    dex = await fetch_geckoterminal(session, ca)
    launch = {
        "source": "manual",
        "address": ca,
        "name": (dex or {}).get("token_name") or ca[:10],
        "symbol": (dex or {}).get("token_symbol") or ca[:6],
        "x_username": "",
        "tweet_url": "",
        "image_uri": "",
    }
    async with db_session() as db:
        existing = await get_launch(db, ca)
    if existing:
        return existing.raw_json or launch, dex or existing.market_json
    await persist_launch_seen(launch, status="manual_research")
    if dex:
        async with db_session() as db:
            await mark_launch_status(db, ca=ca, status="manual_research", reason="manual analysis", market_json=dex)
    return launch, dex


def command_market_line(result: dict) -> str:
    verdict = result.get("verdict") or {}
    research = verdict.get("research") or (result.get("research") or {}).get("processed_data") or {}
    market = research.get("market") or {}
    if not market:
        return "Market: unavailable"
    parts = [
        f"MC {fmt_usd(float(market.get('mcap') or 0))}",
        f"Vol {fmt_usd(float(market.get('volume_24h') or 0))}",
        f"Liq {fmt_usd(float(market.get('liquidity') or 0))}",
    ]
    if market.get("age_minutes") is not None:
        parts.append(f"Age {float(market['age_minutes']):.0f}m")
    if market.get("dex_id"):
        parts.append(str(market["dex_id"]))
    return " · ".join(parts)


def format_verdict2_report(result: dict) -> str:
    launch = result.get("launch") or {}
    verdict = result.get("verdict") or {}
    summary = result.get("summary") or {}
    research = verdict.get("research") or {}
    source_info = research.get("source") or {}
    human = verdict.get("human_readable") or "No verdict generated."
    ca = launch.get("ca", "")
    lines = [
        f"🤖 <b>Verdict 2.0</b> · ${h(launch.get('symbol') or '')} · <b>{h(verdict.get('label') or '')}</b>",
        f"<code>{h(ca)}</code>",
        f"Source: <b>{h(launch.get('source') or source_info.get('source') or 'unknown')}</b>",
        h(command_market_line(result)),
        "",
        human,
    ]
    if summary:
        lines.extend(["", f"📝 <b>Summary stub</b>\n{h(summary.get('summary_text', ''))}"])
    return "\n".join(lines)[:3900]


def format_spoof_report(result: dict) -> str:
    launch = result.get("launch") or {}
    signals = result.get("spoof_signals") or []
    verdict = result.get("verdict") or {}
    lines = [
        f"🕵️ <b>Spoof Check</b> · ${h(launch.get('symbol') or '')}",
        f"<code>{h(launch.get('ca') or '')}</code>",
        f"Verdict: <b>{h(verdict.get('label') or '')}</b> · {float(verdict.get('score') or 0) / 10:.1f}/10",
        h(command_market_line(result)),
        "",
    ]
    if not signals:
        lines.append("No deterministic spoof signals found yet.")
    else:
        for signal in signals[:8]:
            impact = float(signal.get("score_impact") or 0)
            lines.append(f"• <b>{h(signal.get('severity'))}</b> -{impact:.0f} · {h(signal.get('title'))}")
            if signal.get("details"):
                lines.append(f"  {h(signal.get('details'))}")
    return "\n".join(lines)[:3900]


def format_summary_report(result: dict) -> str:
    launch = result.get("launch") or {}
    summary = result.get("summary") or {}
    verdict = result.get("verdict") or {}
    return (
        f"🧠 <b>AI Summary</b> <i>(stub)</i> · ${h(launch.get('symbol') or '')}\n"
        f"<code>{h(launch.get('ca') or '')}</code>\n\n"
        f"{h(command_market_line(result))}\n\n"
        f"{h(summary.get('summary_text') or 'Summary unavailable')}\n\n"
        f"Verdict: <b>{h(verdict.get('label'))}</b> · {float(verdict.get('score') or 0) / 10:.1f}/10"
    )[:3900]


async def analyze_ca_for_command(
    session: aiohttp.ClientSession,
    ca: str,
    *,
    requested_by: str,
    include_summary: bool = True,
    language: str = "en",
) -> dict:
    launch, dex = await ensure_launch_for_analysis(session, ca)
    async with db_session() as db:
        return await analyze_token_intelligence(
            db,
            ca=ca,
            dex=dex,
            requested_by=requested_by,
            include_summary=include_summary,
            language=language,
        )


# ─── Signal Handler ───────────────────────────────────────────────────────────

async def send_signal(session: aiohttp.ClientSession, launch: dict, dex: dict, source: str, symbol: str, is_recheck: bool = False) -> bool:
    global alert_count

    address = launch["address"]
    prefix = "RECHECK " if is_recheck else ""
    cid = correlation_id(source, address)

    addr_truncated = address[:20]
    _address_map[addr_truncated] = address

    tg_text = format_signal_telegram(launch, dex, executed=False, job_id="")
    wa_text = format_alert_whatsapp(launch, dex, executed=False)
    keyboard = build_trade_keyboard(address, symbol)
    delivery_payload = {
        "telegram_text": tg_text,
        "reply_markup": keyboard,
        "ca": address,
        "symbol": symbol,
        "source": source,
    }

    if default_tenant_db_id is not None:
        async with db_session() as db:
            if await signal_exists_for_tenant(db, ca=address, tenant_id=default_tenant_db_id):
                log.info(f"  📡 [{source}] ${symbol} already delivered for default tenant, skipping")
                await mark_launch_status(db, ca=address, status="signaled", reason="already delivered", market_json=dex)
                return False
            _, delivery, delivery_inserted = await prepare_tenant_delivery(
                db,
                ca=address,
                tenant_id=default_tenant_db_id,
                chat_id=TELEGRAM_CHAT_ID,
                payload_json=delivery_payload,
            )
            if not delivery_inserted:
                log.info(f"  📡 [{source}] ${symbol} delivery row exists, skipping duplicate")
                return False
            delivery_id = delivery.id
    else:
        delivery_id = None

    log.info(
        f"  📡 {prefix}SIGNAL: [{source}] ${symbol} "
        f"MCap {fmt_usd(dex['mcap'])} Vol {fmt_usd(dex['volume_24h'])}"
        + (f" @{launch.get('x_username')}" if launch.get('x_username') else "")
    )

    if delivery_id is not None:
        async with db_session() as db:
            await mark_delivery_sending(db, delivery_id=delivery_id)

    message_id = await send_telegram(session, tg_text, reply_markup=keyboard)
    if message_id is None:
        log.error(f"  ❌ Telegram signal failed: [{source}] ${symbol} {address}")
        if delivery_id is not None:
            async with db_session() as db:
                await mark_delivery_retry(
                    db,
                    delivery_id=delivery_id,
                    error="telegram send failed",
                    next_retry_at=utc_now() + timedelta(minutes=1),
                )
        return False

    if delivery_id is not None:
        async with db_session() as db:
            await mark_delivery_sent(db, delivery_id=delivery_id, message_id=str(message_id))
            await mark_launch_status(db, ca=address, status="signaled", reason="telegram delivered", market_json=dex)
            log_event("signal_sent", correlation_id=cid, ca=address, source=source, message_id=message_id)

    if WHAPI_TOKEN and WHATSAPP_GROUP_ID:
        await send_whatsapp(session, wa_text)

    dex_url = f"https://dexscreener.com/base/{address}"
    pushover_msg = f"${symbol} · MCap {fmt_usd(dex['mcap'])} · Vol {fmt_usd(dex['volume_24h'])}"
    if launch.get("x_username"):
        pushover_msg += f" · @{launch['x_username']}"
    await send_pushover(session, f"🐋 {source.upper()}: ${symbol}", pushover_msg, url=dex_url)

    alert_count += 1

    if AUTO_EXECUTE and isinstance(message_id, int):
        asyncio.create_task(
            attach_execution_result(session, TELEGRAM_CHAT_ID, message_id, tg_text, keyboard, launch, source, symbol)
        )

    if AUTO_VERDICT_ENABLED and isinstance(message_id, int):
        asyncio.create_task(
            attach_signal_verdict(session, TELEGRAM_CHAT_ID, message_id, tg_text, keyboard, launch, dex, source, symbol)
        )

    return True


async def attach_execution_result(
    session: aiohttp.ClientSession,
    chat_id: str,
    message_id: int,
    base_text: str,
    keyboard: dict,
    launch: dict,
    source: str,
    symbol: str,
):
    address = launch["address"]
    executed = await execute_bankr_buy(session, address, symbol, source)
    status = f"💸 <b>Auto-buy submitted</b> via Bankr" if executed else "⚠️ Auto-buy not submitted"
    ok = await edit_telegram_message(session, chat_id, message_id, f"{base_text}\n\n{status}", reply_markup=keyboard)
    if ok:
        log.info(f"  💸 Execution status attached: [{source}] ${symbol} → {'submitted' if executed else 'skipped'}")


async def attach_signal_verdict(
    session: aiohttp.ClientSession,
    chat_id: str,
    message_id: int,
    base_text: str,
    keyboard: dict,
    launch: dict,
    dex: dict | None,
    source: str,
    symbol: str,
):
    address = launch.get("address", "")
    try:
        async with db_session() as db:
            if not await get_launch(db, address):
                await upsert_launch(
                    db,
                    ca=address,
                    ticker=launch.get("symbol", ""),
                    name=launch.get("name", ""),
                    source=launch.get("source", source),
                    raw_json=launch,
                    launched_at=parse_launch_datetime(launch),
                    status="signaled",
                )
            result = await analyze_token_intelligence(
                db,
                ca=address,
                dex=dex,
                requested_by="signal_auto_verdict",
                include_summary=True,
                language="en",
            )
    except Exception as e:
        log.warning(f"  ⚠️ Verdict 2.0 skipped: [{source}] ${symbol}: {e}")
        return

    verdict = result.get("verdict") or {}
    verdict_block = verdict.get("human_readable") or build_ai_summary_placeholder(
        launch,
        dex,
        {
            "score": verdict.get("score", 0),
            "label": verdict.get("label", "WAIT"),
            "reasons": verdict.get("reasons", []),
            "risks": verdict.get("risks", []),
            "research": (result.get("research") or {}).get("processed_data") or {},
        },
    )
    placeholder = build_ai_summary_placeholder(launch, dex)
    new_text = (
        base_text.replace(placeholder, verdict_block)
        if placeholder in base_text
        else f"{base_text}\n\n{verdict_block}"
    )
    if len(new_text) > 3900:
        new_text = new_text[:3800] + "\n\n<i>Verdict 2.0 truncated</i>"

    ok = await edit_telegram_message(session, chat_id, message_id, new_text, reply_markup=keyboard)
    if ok:
        log.info(
            f"  🧠 Verdict 2.0: [{source}] ${symbol} → "
            f"{verdict.get('label')} ({float(verdict.get('score') or 0) / 10:.1f}/10)"
        )
    else:
        log.warning(f"  ⚠️ Verdict 2.0 edit rejected: [{source}] ${symbol}")


# ─── Seeding ──────────────────────────────────────────────────────────────────

async def seed_existing(session: aiohttp.ClientSession):
    log.info("📋 Seeding existing tokens...")
    bankr = await fetch_bankr(session)
    clanker = await fetch_clanker(session)
    dexscreener = await fetch_dexscreener_discoveries(session)
    virtuals = await fetch_virtuals(session)

    all_launches = bankr + clanker + dexscreener + virtuals
    inserted = 0

    for launch in all_launches:
        was_inserted, addr = await persist_launch_seen(launch, status="seeded")
        inserted += int(was_inserted)
        if was_inserted:
            log_event("launch_seeded", correlation_id=correlation_id(launch.get("source", "?"), addr), ca=addr, source=launch.get("source", "?"))

    log.info(
        f"📋 Seeded {inserted} new DB rows "
        f"(Bankr: {len(bankr)}, Clanker: {len(clanker)}, DexScreener: {len(dexscreener)}, Virtuals: {len(virtuals)}) "
        f"— existing rows skipped"
    )


# ─── Main Loop ────────────────────────────────────────────────────────────────

async def main():
    global alert_count, default_tenant_db_id

    if not TELEGRAM_BOT_TOKEN:
        log.error("❌ TELEGRAM_BOT_TOKEN not set!")
    if not TELEGRAM_CHAT_ID:
        log.error("❌ TELEGRAM_CHAT_ID not set!")
    if not SOCIALDATA_API_KEY:
        log.error("❌ SOCIALDATA_API_KEY not set!")

    log.info("=" * 60)
    log.info("  🐋 Whale Alert Bot (Bankr + Clanker + Virtuals + DexScreener)")
    log.info(f"  Min followers : {MIN_FOLLOWERS:,}")
    log.info(f"  Min MCap      : ${MIN_MCAP:,}")
    log.info(f"  Min Volume 24h: ${MIN_VOLUME_24H:,}")
    log.info(f"  Min Liquidity : ${MIN_LIQUIDITY:,} (DexScreener-sourced only)")
    log.info(f"  Safe sources  : {', '.join(SAFE_LAUNCHPADS)} (liq check SKIPPED)")
    log.info(f"  Poll interval : {POLL_INTERVAL}s")
    log.info(f"  Telegram      : {'✅' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else '❌'}")
    log.info(f"  Authorized DMs: {', '.join(sorted(AUTHORIZED_USER_IDS)) if AUTHORIZED_USER_IDS else 'none'}")
    log.info(f"  WhatsApp      : {'✅' if WHAPI_TOKEN and WHATSAPP_GROUP_ID else '❌'}")
    log.info(f"  SocialData    : {'✅' if SOCIALDATA_API_KEY else '❌'}")
    log.info(f"  Auto-verdict  : {'✅ ON' if AUTO_VERDICT_ENABLED else '❌ OFF'} ({AUTO_VERDICT_TIMEOUT_SEC:.0f}s, max {AUTO_VERDICT_MAX_CONCURRENT})")
    log.info(f"  Pushover      : {'✅' if PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN else '❌ NOT SET'}")
    log.info(f"  Auto-execute  : {'✅ ON — $' + str(BANKR_BUY_AMOUNT) + '/trade' if AUTO_EXECUTE else '❌ OFF'}")
    log.info(f"  Inline trading: {'✅ ON' if TRADING_ENABLED else '❌ OFF (set TRADING_ENABLED=true)'}")
    log.info(f"  Bankr Exec Key: {'✅' if BANKR_EXECUTION_API_KEY else '❌ NOT SET'}")
    log.info("=" * 60)

    await init_db(resolve_database_url(), auto_create=settings.database_auto_create)
    async with db_session() as db:
        if TELEGRAM_CHAT_ID:
            tenant = await ensure_telegram_tenant(db, TELEGRAM_CHAT_ID, title="default alerts")
            default_tenant_db_id = tenant.id
            await set_bot_state(db, "default_tenant_id", str(tenant.id))
        await set_bot_state(db, "last_start_at", utc_now().isoformat())
    log.info(f"✅ Database initialized ({settings.app_env}, tenant={default_tenant_db_id})")

    async with aiohttp.ClientSession() as session:
        await delete_telegram_webhook(session, drop_pending_updates=True)
        await set_bot_commands(session)

        # DexScreener health check
        try:
            test_addr = "0x532f27101965dd16442e59d40670faf5ebb142e4"
            test_url = f"{DEXSCREENER_API_URL}/token-pairs/v1/base/{test_addr}"
            async with session.get(test_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                test_data = await resp.json()
                if isinstance(test_data, list) and len(test_data) > 0:
                    p = test_data[0]
                    log.info(f"✅ DexScreener OK — BRETT: ${float(p.get('priceUsd') or 0):.4f}, mcap ${float(p.get('marketCap') or 0):,.0f}")
                else:
                    log.warning(f"⚠️ DexScreener unexpected format: {type(test_data).__name__}")
        except Exception as e:
            log.error(f"❌ DexScreener health check failed: {e}")

        # Bankr execution health check
        if AUTO_EXECUTE and BANKR_EXECUTION_API_KEY:
            log.info("🔍 Verifying Bankr execution API key...")
            try:
                async with session.post(
                    BANKR_AGENT_API_URL,
                    headers={"Content-Type": "application/json", "X-API-Key": BANKR_EXECUTION_API_KEY},
                    json={"prompt": "what are my token balances on base?"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if resp.status == 202 and data.get("success"):
                        log.info(f"✅ Bankr execution API OK — jobId: {data.get('jobId', '?')}")
                    elif resp.status == 403:
                        log.error("❌ Bankr API key rejected (403) — check bankr.bot/api for Agent API access")
                    else:
                        log.warning(f"⚠️ Bankr API check: {resp.status} — {data}")
            except Exception as e:
                log.error(f"❌ Bankr execution health check failed: {e}")

        # Pushover health check
        if PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN:
            log.info("🔍 Verifying Pushover credentials...")
            try:
                async with session.post(
                    "https://api.pushover.net/1/users/validate.json",
                    data={"token": PUSHOVER_API_TOKEN, "user": PUSHOVER_USER_KEY},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        log.info("✅ Pushover credentials valid")
                    else:
                        body = await resp.text()
                        log.error(f"❌ Pushover validation failed {resp.status}: {body[:200]}")
            except Exception as e:
                log.error(f"❌ Pushover health check failed: {e}")

        await seed_existing(session)

        trade_note = "\n💸 Inline trading: ✅ ON — tap buttons on signals to buy/sell" if TRADING_ENABLED else "\n💸 Inline trading: ❌ OFF"
        pushover_note = "\n🔔 Pushover: ✅ Emergency alerts ON" if PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN else "\n🔔 Pushover: ❌ OFF"
        verdict_note = (
            f"\n🧠 Auto-verdict: ✅ ON — deterministic research, AI stub"
            if AUTO_VERDICT_ENABLED else "\n🧠 Auto-verdict: ❌ OFF"
        )
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            exec_note = f"\n💸 Auto-execute: ON (${BANKR_BUY_AMOUNT}/trade)" if AUTO_EXECUTE else "\n💸 Auto-execute: OFF"
            await send_telegram(
                session,
                f"🐋 <b>Whale Alert Bot started</b>\n\n"
                f"Sources: Bankr + Clanker + Virtuals + DexScreener\n"
                f"Market data: DexScreener\n"
                f"Min MCap: ${MIN_MCAP:,} · Vol: ${MIN_VOLUME_24H:,}\n"
                f"Liq: ${MIN_LIQUIDITY:,} (DexScreener only — 🔓 skipped for Bankr/Clanker/Virtuals)\n"
                f"Polling every {POLL_INTERVAL}s"
                f"{exec_note}"
                f"{trade_note}"
                f"{pushover_note}"
                f"{verdict_note}\n\n"
                f"Commands: /help · /research · /status · /wallets · /track",
            )

        while True:
            try:
                await handle_telegram_commands(session)
                retried_deliveries = await process_delivery_retries(session)
                if retried_deliveries:
                    log.info(f"📨 Retried {retried_deliveries} Telegram deliveries")

                bankr_launches = await fetch_bankr(session)
                clanker_launches = await fetch_clanker(session)
                dexscreener_launches = await fetch_dexscreener_discoveries(session)
                virtuals_launches = await fetch_virtuals(session)
                all_launches = bankr_launches + clanker_launches + dexscreener_launches + virtuals_launches

                new_count = 0
                signal_count = 0
                no_data_count = 0

                for launch in all_launches:
                    address = (launch["address"] or "").lower()
                    if not address:
                        continue

                    symbol = launch.get("symbol", "?")
                    source = launch.get("source", "?")
                    inserted, _ = await persist_launch_seen(launch, status="new")
                    if not inserted:
                        continue

                    cid = correlation_id(source, address)
                    log_event("launch_seen", correlation_id=cid, ca=address, source=source, symbol=symbol)

                    if source == "dexscreener" and launch.get("_dex"):
                        dex = launch["_dex"]
                    else:
                        dex = await fetch_geckoterminal(session, address)
                    if source == "dexscreener" and dex:
                        launch["name"] = dex.get("token_name") or launch.get("name") or "Unknown"
                        launch["symbol"] = dex.get("token_symbol") or launch.get("symbol") or "?"
                        symbol = launch["symbol"]

                    passes, reason = passes_market_filters(dex, source=source)

                    if not passes:
                        if "too old" in reason or "likely old" in reason:
                            async with db_session() as db:
                                await mark_launch_status(db, ca=address, status="expired", reason=reason, market_json=dex)
                            log.debug(f"  [{source}] ${symbol} — {reason}, skip (permanent)")
                        elif reason == "no market data":
                            no_data_count += 1
                            await persist_recheck(address, reason, no_data=True, dex=dex)
                            log.debug(f"  [{source}] ${symbol} — no market data, recheck queue (short)")
                        else:
                            new_count += 1
                            log.info(f"  [{source}] ${symbol} — {reason}, skip → recheck queue")
                            await persist_recheck(address, reason, no_data=False, dex=dex)
                        continue

                    new_count += 1

                    if launch["source"] == "virtuals":
                        launch["x_username"] = launch.get("creator_x", "") or launch.get("x_username", "")

                    if await send_signal(session, launch, dex, source, symbol, is_recheck=False):
                        signal_count += 1

                # ── Persistent Recheck Queue ──
                async with db_session() as db:
                    due_rechecks = await get_due_rechecks(db, now=utc_now(), limit=RECHECK_MAX_QUEUE)

                expired_names = []
                for entry in due_rechecks:
                    addr = entry.ca
                    launch = entry.raw_json
                    symbol = launch.get("symbol", "?")
                    source = launch.get("source", "?")
                    is_no_data = bool(entry.no_data)
                    max_checks = 6 if is_no_data else RECHECK_MAX_CHECKS
                    max_age = 1800 if is_no_data else RECHECK_MAX_AGE

                    if seconds_since(entry.first_seen_at) > max_age or entry.check_count >= max_checks:
                        async with db_session() as db:
                            await mark_launch_status(db, ca=addr, status="expired", reason="recheck expired", market_json=entry.market_json)
                        expired_names.append(f"${symbol}[{source}/{'nd' if is_no_data else 'd'}]")
                        continue
                    if entry.check_count >= 2 and float(entry.last_mcap or 0) < 1000:
                        async with db_session() as db:
                            await mark_launch_status(db, ca=addr, status="expired", reason="low mcap after rechecks", market_json=entry.market_json)
                        expired_names.append(f"${symbol}[{source}/low]")
                        continue

                    gecko_cache.pop(addr, None)
                    dex = await fetch_geckoterminal(session, addr)

                    passes, reason = passes_market_filters(dex, source=source)
                    if not passes:
                        if "too old" in reason or "likely old" in reason:
                            log.debug(f"  ♻️ [{source}] ${symbol} — {reason}, dropping")
                            async with db_session() as db:
                                await mark_launch_status(db, ca=addr, status="expired", reason=reason, market_json=dex)
                            expired_names.append(f"${symbol}[{source}/old]")
                            continue
                        if reason != "no market data":
                            log.info(f"  ♻️ [{source}] ${symbol} recheck #{entry.check_count + 1} — {reason}, still waiting")
                        await persist_recheck(addr, reason, no_data=(reason == "no market data"), dex=dex)
                        continue

                    if launch["source"] == "virtuals":
                        launch["x_username"] = launch.get("creator_x", "") or launch.get("x_username", "")

                    if await send_signal(session, launch, dex, source, symbol, is_recheck=True):
                        signal_count += 1

                if expired_names:
                    log.info(f"  🗑️ Recheck expired ({len(expired_names)}): {', '.join(expired_names[:10])}{'...' if len(expired_names) > 10 else ''}")

                async with db_session() as db:
                    db_status = await get_status_snapshot(db)
                recheck_log = f", {db_status['queued_rechecks']} in DB recheck queue" if db_status["queued_rechecks"] else ""
                no_data_log = f", {no_data_count} no-data queued" if no_data_count else ""
                log.info(f"🔍 {new_count} new launches processed, {signal_count} signals sent{no_data_log}{recheck_log}")

            except Exception as e:
                log.error(f"Poll loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL)


async def run() -> None:
    try:
        await main()
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(run())
