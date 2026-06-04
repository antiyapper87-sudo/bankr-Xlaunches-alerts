"""
Whale Alert Bot — Bankr + Clanker + Virtuals + DexScreener + CoinGecko
========================================================
Monitors Base launch/discovery sources:
  1. Bankr API   — https://api.bankr.bot/token-launches
  2. Clanker API  — https://www.clanker.world/api/tokens
  3. Virtuals API — https://api2.virtuals.io/api/virtuals  (AI agent launches)
  4. DexScreener — profiles/boosts/CTO discovery + market data
  5. CoinGecko Onchain — Base new pools discovery

When a token passes market filters → alerts to Telegram + WhatsApp + Pushover.
Telegram signals include research and watchlist actions.

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
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import quote


def load_local_env(path: str = ".env.local") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env()

from database import (
    close_db,
    consume_user_command_quota,
    db_session,
    deactivate_watchlist_item,
    deactivate_tracked_wallet,
    get_due_watchlist_items,
    get_due_tracked_wallets,
    get_due_delivery_retries,
    get_pending_deliveries_for_signal,
    get_due_rechecks,
    get_api_budget_usage,
    get_bot_state,
    get_launch,
    get_launch_status,
    get_latest_token_research,
    get_tenant,
    get_tenant_settings,
    get_status_snapshot,
    init_db,
    list_tracked_wallets,
    list_watchlist_items,
    mark_delivery_failed,
    mark_delivery_retry,
    mark_delivery_sent,
    mark_delivery_sending,
    mark_launch_status,
    mark_tracked_wallet_checked,
    mark_watchlist_checked,
    provider_available,
    queue_recheck,
    record_api_budget_event,
    record_nitter_health_log,
    record_socialdata_usage_log,
    set_bot_state,
    set_provider_cooldown,
    store_verdict,
    update_tenant_min_score,
    upsert_user_feedback,
    upsert_launch,
    upsert_tracked_wallet,
    upsert_wallet_event,
    upsert_watchlist_item,
    utc_now,
)
from hermes_skills.social_intelligence import passes_social_intelligence_filters
from research_pipeline import (
    AUTO_VERDICT_ENABLED,
    AUTO_VERDICT_MAX_CONCURRENT,
    AUTO_VERDICT_TIMEOUT_SEC,
    ResearchDeps,
    build_signal_verdict_with_timeout,
    format_verdict_block,
)
from services.delivery import prepare_signal_fanout
from services.fomo import (
    FOMO_DEFAULT_CHAIN_ID,
    FOMO_ENABLED,
    build_fomo_url,
    fetch_fomo_top_holders,
    format_fomo_holders_card,
)
from services.observability import correlation_id, log_event
from services.project_narrative import (
    extract_project_narrative,
    narrative_token_type,
)
from services.social_evidence import (
    build_social_evidence,
    hide_contract_mentions,
    is_likely_english_text,
    is_recent_tweet,
    strip_non_english_content,
)
from services.social_fetcher import AlphaDetector, NitterFetcher, SmartFetchOrchestrator, SmartFetchResult, SocialDataFetcher
from services.tenants import ensure_telegram_tenant
from services.token_intelligence import analyze_token_intelligence
from services.tweet_provenance import annotate_tweet_source, ca_first_sort_key, strict_ca_query, strict_ticker_query
from settings import resolve_database_url, settings

# ─── Config from environment ──────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
AUTHORIZED_USER_IDS = {
    user_id.strip()
    for user_id in os.getenv("AUTHORIZED_USER_IDS", "544999608").split(",")
    if user_id.strip()
}
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "544999608").strip()
PUBLIC_RESEARCH_TOKEN_LIMIT = int(os.getenv("PUBLIC_RESEARCH_TOKEN_LIMIT", "5"))
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN", "")
WHATSAPP_GROUP_ID = os.getenv("WHATSAPP_GROUP_ID", "")
SOCIALDATA_API_KEY = os.getenv("SOCIALDATA_API_KEY", "")
MIN_FOLLOWERS = int(os.getenv("MIN_FOLLOWERS", "5000"))
RESEARCH_MIN_FOLLOWERS = int(os.getenv("RESEARCH_MIN_FOLLOWERS", "1000"))
RESEARCH_HIGH_SIGNAL_SCORE = int(os.getenv("RESEARCH_HIGH_SIGNAL_SCORE", "8"))
RESEARCH_MIN_QUALIFIED_TWEETS = int(os.getenv("RESEARCH_MIN_QUALIFIED_TWEETS", "5"))
RESEARCH_MIN_TWEET_VIEWS = int(os.getenv("RESEARCH_MIN_TWEET_VIEWS", "50"))
RESEARCH_MIN_TWEET_LIKES = int(os.getenv("RESEARCH_MIN_TWEET_LIKES", "5"))
REQUIRE_CA_SOCIAL_CONFIRMATION = os.getenv("REQUIRE_CA_SOCIAL_CONFIRMATION", "true").lower() == "true"
SOCIALDATA_SEARCH_MAX_PAGES = int(os.getenv("SOCIALDATA_SEARCH_MAX_PAGES", "4"))
SOCIALDATA_SEARCH_CACHE_TTL_SEC = int(os.getenv("SOCIALDATA_SEARCH_CACHE_TTL_SEC", "900"))
SOCIALDATA_SEARCH_EMPTY_CACHE_TTL_SEC = int(os.getenv("SOCIALDATA_SEARCH_EMPTY_CACHE_TTL_SEC", "300"))
SOCIALDATA_SEARCH_STALE_TTL_SEC = int(os.getenv("SOCIALDATA_SEARCH_STALE_TTL_SEC", "86400"))
SOCIALDATA_SEARCH_MAX_CALLS_PER_MIN = int(os.getenv("SOCIALDATA_SEARCH_MAX_CALLS_PER_MIN", "30"))
SOCIALDATA_SEARCH_MAX_CALLS_PER_HOUR = int(os.getenv("SOCIALDATA_SEARCH_MAX_CALLS_PER_HOUR", "240"))
NITTER_ENABLED = os.getenv("NITTER_ENABLED", "false").lower() == "true"
NITTER_BASE_URLS = [
    url.strip().rstrip("/")
    for url in os.getenv("NITTER_BASE_URLS", "").split(",")
    if url.strip()
]
NITTER_HEALTH_ENABLED = os.getenv("NITTER_HEALTH_ENABLED", "true").lower() == "true"
NITTER_HEALTH_INTERVAL_SEC = int(os.getenv("NITTER_HEALTH_INTERVAL_SEC", "300"))
NITTER_HEALTH_QUERY = os.getenv("NITTER_HEALTH_QUERY", "$GSPEED").strip() or "$GSPEED"
MIN_MCAP = int(os.getenv("MIN_MCAP", "50000"))
MIN_VOLUME_24H = int(os.getenv("MIN_VOLUME_24H", "30000"))
MIN_LIQUIDITY = int(os.getenv("MIN_LIQUIDITY", "30000"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
TELEGRAM_COMMAND_POLL_INTERVAL = float(os.getenv("TELEGRAM_COMMAND_POLL_INTERVAL", "0.5"))
TELEGRAM_GET_UPDATES_TIMEOUT = int(os.getenv("TELEGRAM_GET_UPDATES_TIMEOUT", "1"))
TELEGRAM_GET_UPDATES_LIMIT = int(os.getenv("TELEGRAM_GET_UPDATES_LIMIT", "25"))
TELEGRAM_BACKGROUND_COMMAND_LIMIT = int(os.getenv("TELEGRAM_BACKGROUND_COMMAND_LIMIT", "8"))

# ─── Pushover Config ──────────────────────────────────────────────────────────
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN", "")

ALCHEMY_RPC_URL = os.getenv("ALCHEMY_RPC_URL", "")

BANKR_API_URL = "https://api.bankr.bot/token-launches"
CLANKER_API_URL = "https://www.clanker.world/api/tokens"
VIRTUALS_API_URL = "https://api2.virtuals.io/api/virtuals"
GECKOTERMINAL_API_URL = "https://api.geckoterminal.com/api/v2"
SOCIALDATA_API_URL = "https://api.socialdata.tools/twitter/user"
DEXSCREENER_API_URL = "https://api.dexscreener.com"
COINGECKO_API_URL = "https://api.coingecko.com/api/v3"
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
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
COINGECKO_DISCOVERY_ENABLED = os.getenv("COINGECKO_DISCOVERY_ENABLED", "false").lower() == "true"
COINGECKO_DISCOVERY_LIMIT = int(os.getenv("COINGECKO_DISCOVERY_LIMIT", "25"))
COINGECKO_POLL_INTERVAL = int(os.getenv("COINGECKO_POLL_INTERVAL", "720"))
COINGECKO_RATE_LIMIT_PER_MIN = int(os.getenv("COINGECKO_RATE_LIMIT_PER_MIN", "12"))
COINGECKO_COOLDOWN_SEC = int(os.getenv("COINGECKO_COOLDOWN_SEC", "120"))

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
socialdata_search_cache: dict[str, tuple[float, float, list[dict]]] = {}
socialdata_search_inflight: dict[str, asyncio.Task] = {}
telegram_background_tasks: set[asyncio.Task] = set()
telegram_background_semaphore = asyncio.Semaphore(TELEGRAM_BACKGROUND_COMMAND_LIMIT)
nitter_health_state: dict[str, object] = {
    "ok": None,
    "last_check": "",
    "last_error": "",
    "last_ok": "",
    "base_url": "",
}
GECKO_CACHE_TTL_HIT = 120
GECKO_CACHE_TTL_MISS = 60
coingecko_calls: list[float] = []
last_coingecko_poll_at: float = 0
last_update_id: int = 0
alert_count: int = 0
default_tenant_db_id: int | None = None

# ─── Recheck queue ────────────────────────────────────────────────────────────
RECHECK_MAX_AGE = 3600
RECHECK_INTERVAL = 300
RECHECK_MAX_CHECKS = 12
RECHECK_MAX_QUEUE = 300
TELEGRAM_RETRY_BATCH = int(os.getenv("TELEGRAM_RETRY_BATCH", "20"))
TELEGRAM_MAX_DELIVERY_ATTEMPTS = int(os.getenv("TELEGRAM_MAX_DELIVERY_ATTEMPTS", "3"))
TELEGRAM_SIGNAL_DELIVERY_LIMIT = int(os.getenv("TELEGRAM_SIGNAL_DELIVERY_LIMIT", "2000"))
WATCHLIST_CHECK_INTERVAL = int(os.getenv("WATCHLIST_CHECK_INTERVAL", "900"))
WATCHLIST_CHECK_BATCH = int(os.getenv("WATCHLIST_CHECK_BATCH", "100"))
WATCHLIST_NOTIFY_MCAP_CHANGE_PCT = float(os.getenv("WATCHLIST_NOTIFY_MCAP_CHANGE_PCT", "50"))
WATCHLIST_NOTIFY_VOLUME_CHANGE_PCT = float(os.getenv("WATCHLIST_NOTIFY_VOLUME_CHANGE_PCT", "100"))
WATCHLIST_PAGE_SIZE = min(12, max(1, int(os.getenv("WATCHLIST_PAGE_SIZE", "10"))))
WATCHLIST_RECENT_HOURS = int(os.getenv("WATCHLIST_RECENT_HOURS", "24"))
WATCHLIST_STALE_HOURS = int(os.getenv("WATCHLIST_STALE_HOURS", "6"))
WALLET_MONITOR_ENABLED = os.getenv("WALLET_MONITOR_ENABLED", "false").lower() == "true"
WALLET_POLL_INTERVAL = int(os.getenv("WALLET_POLL_INTERVAL", "60"))
WALLET_POLL_BATCH = int(os.getenv("WALLET_POLL_BATCH", "50"))
WALLET_LOOKBACK_BLOCKS = int(os.getenv("WALLET_LOOKBACK_BLOCKS", "1200"))
WALLET_EVENT_RECENT_MINUTES = int(os.getenv("WALLET_EVENT_RECENT_MINUTES", "60"))

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
    {"command": "start", "description": "Subscribe and show introduction"},
    {"command": "help", "description": "Show public command menu"},
    {"command": "status", "description": "Runtime status"},
    {"command": "research", "description": "Research ticker or Base CA"},
    {"command": "r", "description": "Short alias for research"},
    {"command": "verdict2", "description": "Run Verdict 2.0 for Base CA"},
    {"command": "spoof_check", "description": "Run spoof checks for Base CA"},
    {"command": "summary", "description": "AI summary stub for Base CA"},
    {"command": "watch", "description": "Add Base CA to watchlist"},
    {"command": "unwatch", "description": "Remove Base CA from watchlist"},
    {"command": "watchlist", "description": "Show your watchlist"},
    {"command": "settings", "description": "Show or update signal settings"},
]

PUBLIC_COMMANDS = {
    "/start",
    "/help",
    "/status",
    "/research",
    "/r",
    "/verdict2",
    "/spoof-check",
    "/spoof_check",
    "/summary",
    "/watch",
    "/unwatch",
    "/watchlist",
    "/settings",
}

ADMIN_COMMANDS = {
    "/admin",
    "/test",
    "/block",
    "/unblock",
    "/blocklist",
    "/track",
    "/untrack",
    "/wallets",
    "/tracked_wallets",
}

def build_help_text() -> str:
    return (
        "🐋 <b>Base Bot</b>\n"
        "Early Base launch monitor with CA-verified X research.\n\n"
        "<b>Research</b>\n"
        "<code>/research 0x...</code> or <code>/research $TICKER</code>\n"
        "<code>/verdict2 0x...</code> · <code>/spoof_check 0x...</code> · <code>/summary 0x...</code>\n\n"
        "<b>Watchlist</b>\n"
        "<code>/watch 0x... [label]</code> · <code>/unwatch 0x...</code> · <code>/watchlist</code>\n\n"
        "<b>Bot</b>\n"
        "<code>/status</code> · <code>/settings</code>\n\n"
        "Signals arrive here after <code>/start</code>."
    )


def build_welcome_text() -> str:
    return (
        "🚨 <b>Base Bot is active</b>\n\n"
        "You are subscribed to early Base launch alerts.\n\n"
        "<b>Start here</b>\n"
        "<code>/research 0x...</code> — token research\n"
        "<code>/watch 0x...</code> — save a token\n"
        "<code>/status</code> — service health\n\n"
        "Use <code>/help</code> for the full command list."
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


def is_private_chat(msg: dict) -> bool:
    return (msg.get("chat", {}).get("type") or "") == "private"


def is_admin_update(msg: dict) -> bool:
    user_id = str(msg.get("from", {}).get("id", ""))
    return user_id == ADMIN_USER_ID


def telegram_user_id(msg: dict) -> str:
    return str(msg.get("from", {}).get("id", "")).strip()


def is_quota_exempt_user(msg: dict) -> bool:
    return telegram_user_id(msg) == ADMIN_USER_ID


async def consume_public_research_quota_for_message(msg: dict) -> tuple[bool, int, int]:
    if is_quota_exempt_user(msg):
        return True, 0, PUBLIC_RESEARCH_TOKEN_LIMIT
    user_id = telegram_user_id(msg)
    async with db_session() as db:
        return await consume_user_command_quota(
            db,
            telegram_user_id=user_id,
            command_key="research",
            limit=PUBLIC_RESEARCH_TOKEN_LIMIT,
        )


def format_research_quota_exhausted(used: int, limit: int) -> str:
    return (
        "🔒 <b>Research limit reached</b>\n\n"
        f"Free access allows <b>{limit}</b> token research requests.\n"
        f"Used: <b>{used}/{limit}</b>.\n\n"
        "Ask the bot owner for extended access."
    )


def telegram_user_title(msg: dict) -> str:
    user = msg.get("from", {}) or {}
    username = user.get("username")
    first_name = user.get("first_name") or ""
    last_name = user.get("last_name") or ""
    if username:
        return f"@{username}"
    return " ".join(part for part in (first_name, last_name) if part).strip() or str(msg.get("chat", {}).get("id", ""))


async def register_public_telegram_tenant(msg: dict) -> bool:
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not chat_id or not is_private_chat(msg):
        return False
    async with db_session() as db:
        await ensure_telegram_tenant(db, chat_id, title=telegram_user_title(msg))
    return True


async def ensure_tenant_for_chat(chat_id: str, title: str | None = None):
    async with db_session() as db:
        return await ensure_telegram_tenant(db, chat_id, title=title)


def telegram_callback_title(callback_query: dict) -> str:
    user = callback_query.get("from", {}) or {}
    username = user.get("username")
    first_name = user.get("first_name") or ""
    last_name = user.get("last_name") or ""
    if username:
        return f"@{username}"
    return " ".join(part for part in (first_name, last_name) if part).strip() or str(user.get("id", ""))


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
    query = f"{token_address.lower()} OR ${clean_symbol.upper()}" if clean_symbol else token_address.lower()
    return f"https://x.com/search?q={quote(query, safe='$')}&src=typed_query"


def build_signal_keyboard(token_address: str, symbol: str) -> dict:
    x_research_url = build_x_research_url(token_address, symbol)
    rows = [[{"text": "🔎 X Research", "url": x_research_url}]]
    if FOMO_ENABLED:
        ca = token_address.lower()
        rows.append([
            {"text": "👀 Fomo", "url": build_fomo_url(ca, FOMO_DEFAULT_CHAIN_ID)},
            {"text": "🚀 Deep Research", "callback_data": f"deep_research:{ca}"},
        ])
    rows.append([
        {"text": "⭐ Worth watching", "callback_data": f"watch:{token_address.lower()}"},
        {"text": "📤 Share ticker", "switch_inline_query": f"${(symbol or '').strip().lstrip('$').upper()}"},
    ])
    return {"inline_keyboard": rows}


def build_admin_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Nitter Status", "callback_data": "adm:nitter_status"},
                {"text": "Force Check", "callback_data": "adm:force_nitter"},
            ],
            [
                {"text": "Update Nitter cookies", "callback_data": "adm:update_cookies"},
                {"text": "SocialData Quota", "callback_data": "adm:socialdata_quota"},
            ],
        ]
    }


_address_map: dict[str, str] = {}
_xsignal_page_cache: dict[str, dict] = {}
XSIGNAL_PAGE_SIZE = 6
XSIGNAL_INLINE_THRESHOLD = 8


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
        keyboard = build_signal_keyboard(token_address, symbol) if token_address else None
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
    if parts and parts[0] == "adm":
        if str(user.get("id", "")) != ADMIN_USER_ID:
            await answer_callback_query(session, callback_id, "Admin only", show_alert=True)
            return
        action = parts[1] if len(parts) > 1 else ""
        if action == "nitter_status":
            await answer_callback_query(session, callback_id, "Nitter status")
            await send_telegram(session, await build_admin_panel_text(), chat_id, reply_markup=build_admin_keyboard())
            return
        if action == "force_nitter":
            ok, base_url, detail, response_ms, item_count = await check_nitter_health(session)
            async with db_session() as db:
                await record_nitter_health_log(
                    db,
                    base_url=base_url or (NITTER_BASE_URLS[0] if NITTER_BASE_URLS else ""),
                    status="ok" if ok else "down",
                    detail=detail,
                    response_ms=response_ms,
                    item_count=item_count,
                )
            now_iso = utc_now().isoformat()
            nitter_health_state.update(
                {
                    "ok": ok,
                    "last_check": now_iso,
                    "last_error": "" if ok else detail,
                    "last_ok": now_iso if ok else nitter_health_state.get("last_ok", ""),
                    "base_url": base_url,
                }
            )
            await answer_callback_query(session, callback_id, "Nitter checked")
            status = "OK" if ok else "DOWN"
            await send_telegram(
                session,
                f"{'✅' if ok else '🚨'} <b>Nitter {status}</b>\n\n{h(base_url or detail)}",
                chat_id,
                reply_markup=build_admin_keyboard(),
            )
            return
        if action == "update_cookies":
            await answer_callback_query(session, callback_id, "Cookie update instructions")
            await send_telegram(
                session,
                "🍪 <b>Update Nitter cookies</b>\n\n"
                "1. Export fresh X cookies/guest account cookies.\n"
                "2. SSH to the VM and update the self-hosted Nitter cookie/guest config.\n"
                "3. Restart the Nitter container.\n"
                "4. Press <b>Force Check</b> here.\n\n"
                "Do not paste cookies into Telegram.",
                chat_id,
                reply_markup=build_admin_keyboard(),
            )
            return
        if action == "socialdata_quota":
            async with db_session() as db:
                now = utc_now()
                minute_used = await get_api_budget_usage(db, provider="socialdata", since=now - timedelta(minutes=1))
                hour_used = await get_api_budget_usage(db, provider="socialdata", since=now - timedelta(hours=1))
            await answer_callback_query(session, callback_id, "SocialData quota")
            await send_telegram(
                session,
                "📊 <b>SocialData Quota</b>\n\n"
                f"Minute {minute_used}/{SOCIALDATA_SEARCH_MAX_CALLS_PER_MIN}\n"
                f"Hour {hour_used}/{SOCIALDATA_SEARCH_MAX_CALLS_PER_HOUR}\n"
                f"Cache keys {len(socialdata_search_cache)} · Inflight {len(socialdata_search_inflight)}",
                chat_id,
                reply_markup=build_admin_keyboard(),
            )
            return
        await answer_callback_query(session, callback_id, "Unknown admin action", show_alert=True)
        return

    if len(parts) == 2 and parts[0] == "watchlist_page":
        try:
            page = int(parts[1])
        except ValueError:
            await answer_callback_query(session, callback_id, "Invalid page", show_alert=True)
            return
        tenant = await ensure_tenant_for_chat(chat_id, title=telegram_callback_title(callback_query))
        async with db_session() as db:
            items = await list_watchlist_items(db, tenant_id=tenant.id, limit=200)
        launches = await load_watchlist_launches(items)
        pages = watchlist_page_count(items)
        page = max(1, min(page, pages))
        ok = await edit_telegram_message(
            session,
            chat_id,
            int(message_id),
            build_watchlist_message(items, page=page, launches=launches),
            reply_markup=build_watchlist_keyboard(items, page=page, launches=launches),
        )
        await answer_callback_query(session, callback_id, f"Page {page}/{pages}" if ok else "Page update failed")
        return

    if len(parts) == 2 and parts[0] == "wl_research":
        ca = parts[1].strip().lower()
        if not is_base_contract(ca):
            await answer_callback_query(session, callback_id, "Invalid token address", show_alert=True)
            return
        await answer_callback_query(session, callback_id, "Research queued")

        async def do_watchlist_research():
            try:
                report, action_keyboard, resolved_ca, _ = await research_token(session, ca, include_keyboard=True)
                social_evidence = _xsignal_page_cache.get(xsignal_cache_key(resolved_ca or ca))
                await send_telegram(
                    session,
                    report,
                    chat_id=chat_id,
                    reply_markup=merge_inline_keyboards(
                        action_keyboard,
                        build_xsignal_pagination_keyboard(resolved_ca or ca, social_evidence, 1),
                    ),
                )
            except Exception as exc:
                log.error(f"Watchlist research failed for {ca}: {exc}", exc_info=True)
                await send_telegram(session, f"❌ <b>Research failed</b>\n{h(str(exc)[:160])}", chat_id=chat_id)

        track_background_command(f"watchlist research {ca}", do_watchlist_research())
        return

    if len(parts) == 2 and parts[0] == "wl_unwatch":
        ca = parts[1].strip().lower()
        if not is_base_contract(ca):
            await answer_callback_query(session, callback_id, "Invalid token address", show_alert=True)
            return
        tenant = await ensure_tenant_for_chat(chat_id, title=telegram_callback_title(callback_query))
        async with db_session() as db:
            removed = await deactivate_watchlist_item(db, tenant_id=tenant.id, ca=ca)
            items = await list_watchlist_items(db, tenant_id=tenant.id, limit=200)
        launches = await load_watchlist_launches(items)
        await answer_callback_query(session, callback_id, "Removed" if removed else "Not in watchlist")
        message = callback_query.get("message", {}) or {}
        if "⭐ Watchlist" in str(message.get("text") or ""):
            await edit_telegram_message(
                session,
                chat_id,
                int(message_id),
                build_watchlist_message(items, page=1, launches=launches),
                reply_markup=build_watchlist_keyboard(items, page=1, launches=launches),
            )
        else:
            await send_telegram(
                session,
                "✅ <b>Removed from watchlist</b>" if removed else "⭐ <b>Not in watchlist</b>",
                chat_id=chat_id,
            )
        return

    if len(parts) == 2 and parts[0] == "deep_research":
        ca = parts[1].strip().lower()
        if not is_base_contract(ca):
            await answer_callback_query(session, callback_id, "Invalid token address", show_alert=True)
            return
        await answer_callback_query(session, callback_id, "Deep Research queued")

        async def do_deep_research():
            try:
                report, action_keyboard, resolved_ca, _ = await research_token(
                    session,
                    ca,
                    include_keyboard=True,
                    search_window_hours=168,
                    title="Deep Research",
                )
                target_ca = resolved_ca or ca
                social_evidence = _xsignal_page_cache.get(xsignal_cache_key(target_ca))
                await send_telegram(
                    session,
                    report,
                    chat_id=chat_id,
                    reply_markup=merge_inline_keyboards(
                        action_keyboard,
                        build_xsignal_pagination_keyboard(target_ca, social_evidence, 1),
                    ),
                )
            except Exception as exc:
                log.error(f"Deep Research failed for {ca}: {exc}", exc_info=True)
                await send_telegram(session, f"❌ <b>Deep Research failed</b>\n{h(str(exc)[:160])}", chat_id=chat_id)

        track_background_command(f"deep research {ca}", do_deep_research())
        return

    if len(parts) == 3 and parts[0] == "fomo_h":
        if not FOMO_ENABLED:
            await answer_callback_query(session, callback_id, "Fomo is disabled", show_alert=True)
            return
        try:
            chain_id = int(parts[1])
        except ValueError:
            await answer_callback_query(session, callback_id, "Invalid Fomo chain", show_alert=True)
            return
        ca = _address_map.get(parts[2].strip().lower(), parts[2].strip().lower())
        if not is_base_contract(ca):
            await answer_callback_query(session, callback_id, "Invalid token address", show_alert=True)
            return
        await answer_callback_query(session, callback_id, "Loading Fomo holders...")
        try:
            result = await fetch_fomo_top_holders(session, address=ca, chain_id=chain_id)
            await send_telegram(session, format_fomo_holders_card(result), chat_id=chat_id)
        except Exception as exc:
            log.warning(f"Fomo holders failed for {ca}: {exc}")
            await send_telegram(
                session,
                "👥 <b>Fomo holders unavailable</b>\n\n"
                "Session expired or Fomo blocked the request. Refresh Fomo cookies/token.",
                chat_id=chat_id,
            )
        return

    if len(parts) == 3 and parts[0] == "xpg":
        key = parts[1].strip()
        try:
            page = int(parts[2])
        except ValueError:
            await answer_callback_query(session, callback_id, "Invalid page", show_alert=True)
            return
        ca = _address_map.get(key, key)
        if not is_base_contract(ca):
            await answer_callback_query(session, callback_id, "Invalid token address", show_alert=True)
            return
        social_evidence = await load_xsignal_evidence_for_ca(ca)
        if not social_evidence:
            await answer_callback_query(session, callback_id, "X signal page expired", show_alert=True)
            return
        page_count = xsignal_page_count(social_evidence)
        page = max(1, min(page, page_count))
        message = callback_query.get("message", {}) or {}
        block = format_research_social_block(
            str(social_evidence.get("ticker") or ""),
            [],
            [],
            social_evidence=social_evidence,
            address=ca,
            page=page,
        )
        keyboard = merge_inline_keyboards(
            strip_xsignal_keyboard(message.get("reply_markup") or {}),
            build_xsignal_pagination_keyboard(ca, social_evidence, page),
        )
        ok = await edit_telegram_message(
            session,
            chat_id,
            int(message_id),
            replace_xsignal_block(message.get("text") or "", block),
            reply_markup=keyboard,
        )
        await answer_callback_query(session, callback_id, f"Page {page}/{page_count}" if ok else "Page update failed")
        return

    if len(parts) == 2 and parts[0] == "watch":
        ca = parts[1].strip().lower()
        if not is_base_contract(ca):
            await answer_callback_query(session, callback_id, "Invalid token address", show_alert=True)
            return
        tenant = await ensure_tenant_for_chat(chat_id, title=telegram_callback_title(callback_query))
        dex = await fetch_geckoterminal(session, ca)
        async with db_session() as db:
            item, inserted = await upsert_watchlist_item(db, tenant_id=tenant.id, ca=ca, label="", market_json=dex)
            await upsert_user_feedback(
                db,
                tenant_id=tenant.id,
                ca=ca,
                action="worth_watching",
                payload_json={"message_id": message_id, "username": username},
            )
            launch = await get_launch(db, ca)
        await answer_callback_query(session, callback_id, "⭐ Added to watchlist")
        await send_telegram(
            session,
            format_watch_added_message(item, launch, inserted=inserted),
            chat_id=chat_id,
            reply_markup=build_watch_item_keyboard(item, launch),
        )
        return

    if len(parts) == 3 and parts[0] == "fb":
        action = parts[1].strip().lower()
        ca = parts[2].strip().lower()
        if action not in {"skip", "worth_watching"} or not is_base_contract(ca):
            await answer_callback_query(session, callback_id, "Invalid feedback", show_alert=True)
            return
        tenant = await ensure_tenant_for_chat(chat_id, title=telegram_callback_title(callback_query))
        async with db_session() as db:
            await upsert_user_feedback(
                db,
                tenant_id=tenant.id,
                ca=ca,
                action=action,
                payload_json={"message_id": message_id, "username": username},
            )
        label = "Skipped" if action == "skip" else "Marked worth watching"
        await answer_callback_query(session, callback_id, f"Saved: {label}")
        return

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
                    await send_telegram(
                        session,
                        f"🔍 <b>X Research: ${symbol}</b>\n\n"
                        f"No CA-verified X mentions found after spam/engagement filters "
                        f"(min {RESEARCH_MIN_QUALIFIED_TWEETS} tweets mentioning the contract).",
                        chat_id=chat_id,
                    )
                    return

                lines = [f"🔍 <b>X Research: ${symbol}</b>\n"]
                for m in mentions:
                    text_clean = re.sub(r'https?://t\.co/\S+', '', m['text']).strip().replace('\n', ' ')
                    text_clean = h(hide_contract_mentions(text_clean, full_address))
                    if len(text_clean) > 200:
                        text_clean = text_clean[:197] + "..."
                    lines.extend([
                        f"",
                        f"<a href='{h(m['url'])}'>@{h(m['username'])}</a> · {h(m['date'])}",
                        f"❤️ {fmt_compact_number(int(m['likes']))} · 👁 {fmt_compact_number(int(m.get('views') or 0))} · 🔄 {fmt_compact_number(int(m['retweets']))}",
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
        await answer_callback_query(session, callback_id, f"🔎 Fetching Top ${symbol} tweets...")

        async def do_ticker_search():
            try:
                tweets = await search_x_mentions(
                    session,
                    symbol,
                    address="",
                    max_age_hours=24,
                    allow_tier3=True,
                    limit=12,
                    min_count=0,
                )
                if not tweets:
                    tweets = await search_x_ticker_recent(
                        session,
                        symbol,
                        address="",
                        limit=12,
                        max_age_hours=24,
                    )
                if not tweets:
                    await send_telegram(
                        session,
                        f"🔎 <b>Top tweets: ${symbol}</b>\n\n"
                        f"No qualified ticker tweets found after language, spam, and engagement filters.",
                        chat_id=chat_id,
                    )
                    return

                lines = [f"🔎 <b>Top tweets: ${symbol}</b>\n"]
                for t in tweets:
                    text_clean = re.sub(r'https?://t\.co/\S+', '', t['text']).strip().replace('\n', ' ')
                    text_clean = h(hide_contract_mentions(text_clean))
                    if len(text_clean) > 200:
                        text_clean = text_clean[:197] + "..."
                    lines.extend([
                        f"",
                        f"<a href='{h(t['url'])}'>@{h(t['username'])}</a> · {h(t['date'])}",
                        f"❤️ {fmt_compact_number(int(t['likes']))} · 👁 {fmt_compact_number(int(t.get('views') or 0))} · 🔄 {fmt_compact_number(int(t['retweets']))}",
                        f"<i>{text_clean}</i>" if text_clean else "<i>[media only]</i>",
                    ])
                await send_telegram(session, "\n".join(lines), chat_id=chat_id)
            except Exception as e:
                log.error(f"Ticker X search callback error: {e}")
                await send_telegram(session, f"❌ Ticker search failed: {str(e)[:100]}", chat_id=chat_id)

        asyncio.create_task(do_ticker_search())
        return


def pct_change(old: float | None, new: float | None) -> float | None:
    old = float(old or 0)
    new = float(new or 0)
    if old <= 0 or new <= 0:
        return None
    return ((new - old) / old) * 100


def format_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.0f}%"


def short_ca(ca: str) -> str:
    value = str(ca or "")
    return f"{value[:6]}...{value[-4:]}" if len(value) > 12 else value


def time_ago(dt: datetime | None) -> str:
    if not dt:
        return "n/a"
    value = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    seconds = max(0, int((utc_now() - value).total_seconds()))
    if seconds < 60:
        return "now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def watch_market(item) -> dict:
    return dict(item.last_market_json or {})


def launch_field(launch, key: str, default: str = "") -> str:
    if not launch:
        return default
    if isinstance(launch, dict):
        return str(launch.get(key) or default)
    return str(getattr(launch, key, default) or default)


def watch_symbol_name(item, launch=None) -> tuple[str, str]:
    market = watch_market(item)
    symbol = (
        launch_field(launch, "ticker") or launch_field(launch, "symbol")
        or market.get("token_symbol")
        or market.get("symbol")
        or item.label
        or short_ca(item.ca)
    )
    name = (
        launch_field(launch, "name")
        or market.get("token_name")
        or market.get("name")
        or item.label
        or ""
    )
    return str(symbol or "").lstrip("$"), str(name or "")


def watchlist_since_added_delta(item) -> tuple[float | None, float | None]:
    return pct_change(item.initial_mcap, item.last_mcap), pct_change(item.initial_volume, item.last_volume)


def watchlist_is_hot(item) -> bool:
    return (
        abs(float(item.last_mcap_change_pct or 0)) > 30
        or abs(float(item.last_volume_change_pct or 0)) > 30
    )


def watchlist_is_recent(item) -> bool:
    created_at = item.created_at if item.created_at.tzinfo else item.created_at.replace(tzinfo=timezone.utc)
    return (utc_now() - created_at).total_seconds() <= WATCHLIST_RECENT_HOURS * 3600


def watchlist_is_stale(item) -> bool:
    if not item.last_checked_at:
        return not watchlist_is_recent(item)
    checked_at = item.last_checked_at if item.last_checked_at.tzinfo else item.last_checked_at.replace(tzinfo=timezone.utc)
    return (utc_now() - checked_at).total_seconds() > WATCHLIST_STALE_HOURS * 3600


def get_watchlist_groups(items: list) -> list[tuple[str, list]]:
    buckets = {
        "🔥 Hot Movers": [],
        "🆕 Recently Added": [],
        "👀 Watching": [],
        "🕰 Stale": [],
    }
    for item in items:
        if watchlist_is_hot(item):
            buckets["🔥 Hot Movers"].append(item)
        elif watchlist_is_recent(item):
            buckets["🆕 Recently Added"].append(item)
        elif watchlist_is_stale(item):
            buckets["🕰 Stale"].append(item)
        else:
            buckets["👀 Watching"].append(item)
    return [(name, rows) for name, rows in buckets.items() if rows]


def ordered_watchlist_items(items: list) -> list:
    ordered: list = []
    for _, rows in get_watchlist_groups(items):
        ordered.extend(rows)
    return ordered


def watchlist_page_count(items: list) -> int:
    total = len(ordered_watchlist_items(items))
    return max(1, (total + WATCHLIST_PAGE_SIZE - 1) // WATCHLIST_PAGE_SIZE)


def visible_watchlist_items(items: list, page: int = 1) -> list:
    ordered = ordered_watchlist_items(items)
    page = max(1, min(page, watchlist_page_count(items)))
    start = (page - 1) * WATCHLIST_PAGE_SIZE
    return ordered[start:start + WATCHLIST_PAGE_SIZE]


def format_watchlist_item(item, launch=None, index: int = 1) -> dict:
    symbol, name = watch_symbol_name(item, launch)
    label = f" · {h(item.label)}" if item.label and item.label not in {symbol, name} else ""
    title_name = f" · {h(name[:42])}" if name and name != symbol else ""
    mcap = fmt_usd(item.last_mcap or 0) if item.last_mcap else "n/a"
    volume = fmt_usd(item.last_volume or 0) if item.last_volume else "n/a"
    liquidity = fmt_usd(item.last_liquidity or 0) if item.last_liquidity else "n/a"
    since_mcap, since_volume = watchlist_since_added_delta(item)
    last_mcap = item.last_mcap_change_pct
    last_volume = item.last_volume_change_pct
    deltas: list[str] = []
    if since_mcap is not None:
        deltas.append(f"add MC {format_pct(since_mcap)}")
    elif since_volume is not None:
        deltas.append(f"add Vol {format_pct(since_volume)}")
    if last_mcap is not None:
        deltas.append(f"last MC {format_pct(last_mcap)}")
    if last_volume is not None:
        deltas.append(f"last Vol {format_pct(last_volume)}")
    if not deltas:
        deltas.append("change n/a")
    checked = time_ago(item.last_checked_at) if item.last_checked_at else "no data yet"
    return {
        "symbol": symbol,
        "name": name,
        "text": (
            f"{index}. <b>${h(symbol)}</b>{title_name}{label}\n"
            f"   MC <b>{mcap}</b> · Vol {volume} · Liq {liquidity}\n"
            f"   {' · '.join(deltas[:2])} · added {time_ago(item.created_at)} · checked {checked}"
        ),
    }


async def load_watchlist_launches(items: list) -> dict[str, object]:
    launches: dict[str, object] = {}
    async with db_session() as db:
        for item in items:
            launch = await get_launch(db, item.ca)
            if launch:
                launches[item.ca] = launch
    return launches


def build_watch_item_keyboard(item, launch=None) -> dict:
    symbol, _ = watch_symbol_name(item, launch)
    ca = item.ca.lower()
    row = [
        {"text": "🔎 Research", "callback_data": f"wl_research:{ca}"},
        {"text": "🐦 X Research", "url": build_x_research_url(ca, symbol)},
    ]
    if FOMO_ENABLED:
        row.append({"text": "👀 Fomo", "url": build_fomo_url(ca, FOMO_DEFAULT_CHAIN_ID)})
    return {
        "inline_keyboard": [
            row,
            [{"text": "⭐ Unwatch", "callback_data": f"wl_unwatch:{ca}"}],
        ]
    }


def build_watchlist_keyboard(items: list, page: int = 1, launches: dict[str, object] | None = None) -> dict | None:
    launches = launches or {}
    visible = visible_watchlist_items(items, page)
    if not visible:
        return None
    page = max(1, min(page, watchlist_page_count(items)))
    rows: list[list[dict]] = []
    start = (page - 1) * WATCHLIST_PAGE_SIZE
    for offset, item in enumerate(visible, 1):
        symbol, _ = watch_symbol_name(item, launches.get(item.ca))
        ca = item.ca.lower()
        number = start + offset
        action_row = [
            {"text": f"{number} 🔎", "callback_data": f"wl_research:{ca}"},
            {"text": "🐦", "url": build_x_research_url(ca, symbol)},
        ]
        if FOMO_ENABLED:
            action_row.append({"text": "👀", "url": build_fomo_url(ca, FOMO_DEFAULT_CHAIN_ID)})
        action_row.append({"text": "⭐ Unwatch", "callback_data": f"wl_unwatch:{ca}"})
        rows.append(action_row)
    pages = watchlist_page_count(items)
    if pages > 1:
        prev_page = max(1, page - 1)
        next_page = min(pages, page + 1)
        rows.append([
            {"text": "← Prev", "callback_data": f"watchlist_page:{prev_page}"},
            {"text": f"Page {page}/{pages}", "callback_data": f"watchlist_page:{page}"},
            {"text": "Next →", "callback_data": f"watchlist_page:{next_page}"},
        ])
    return {"inline_keyboard": rows}


def build_watchlist_message(items: list, page: int = 1, launches: dict[str, object] | None = None) -> str:
    if not items:
        return (
            "⭐ <b>Watchlist</b>\n\n"
            "No saved tokens yet.\n"
            "<code>/watch 0x... [label]</code>"
        )
    launches = launches or {}
    pages = watchlist_page_count(items)
    page = max(1, min(page, pages))
    visible_ids = {item.id for item in visible_watchlist_items(items, page)}
    lines = [
        f"⭐ <b>Watchlist</b> · {len(items)} token(s)",
        f"Page <b>{page}/{pages}</b> · {WATCHLIST_PAGE_SIZE} per page",
        "",
    ]
    index = (page - 1) * WATCHLIST_PAGE_SIZE
    for group_name, group_items in get_watchlist_groups(items):
        visible_group = [item for item in group_items if item.id in visible_ids]
        if not visible_group:
            continue
        lines.append(f"<b>{group_name}</b>")
        for item in visible_group:
            index += 1
            lines.append(format_watchlist_item(item, launches.get(item.ca), index)["text"])
        lines.append("")
    return "\n".join(lines)[:3900]


def format_watch_added_message(item, launch=None, *, inserted: bool = True) -> str:
    status = "Added to watchlist" if inserted else "Watchlist updated"
    return f"⭐ <b>{status}</b>\n\n{format_watchlist_item(item, launch, 1)['text']}"


def format_watchlist_rows(items: list) -> str:
    return build_watchlist_message(items)


def format_settings_text(min_score: float) -> str:
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"Min signal score: <b>{min_score:.1f}/10</b>\n\n"
        "<code>/settings min_score 7.5</code>"
    )


async def build_admin_panel_text() -> str:
    now = utc_now()
    async with db_session() as db:
        db_status = await get_status_snapshot(db)
        social_min = await get_api_budget_usage(db, provider="socialdata", since=now - timedelta(minutes=1))
        social_hour = await get_api_budget_usage(db, provider="socialdata", since=now - timedelta(hours=1))

    nitter_ok = nitter_health_state.get("ok")
    nitter_label = "OK" if nitter_ok is True else "DOWN" if nitter_ok is False else "unknown"
    nitter_detail = (
        str(nitter_health_state.get("base_url") or "")
        if nitter_ok
        else str(nitter_health_state.get("last_error") or "not checked yet")
    )
    cooldowns = ", ".join(db_status["provider_cooldowns"]) or "none"
    return (
        "🛠 <b>Admin</b>\n\n"
        f"<b>Nitter</b>\n"
        f"Status: <b>{h(nitter_label)}</b>\n"
        f"Query: <code>{h(NITTER_HEALTH_QUERY)}</code>\n"
        f"Last check: <code>{h(str(nitter_health_state.get('last_check') or 'never'))}</code>\n"
        f"Last OK: <code>{h(str(nitter_health_state.get('last_ok') or 'never'))}</code>\n"
        f"Detail: {h(nitter_detail[:220])}\n\n"
        f"<b>SocialData</b>\n"
        f"Minute {social_min}/{SOCIALDATA_SEARCH_MAX_CALLS_PER_MIN} · "
        f"Hour {social_hour}/{SOCIALDATA_SEARCH_MAX_CALLS_PER_HOUR}\n"
        f"Cache keys: {len(socialdata_search_cache)} · Inflight: {len(socialdata_search_inflight)}\n\n"
        f"<b>Access</b>\n"
        f"Started: <b>{db_status['public_started_total']}</b> · "
        f"Users {db_status['telegram_users_active']} · Groups {db_status['telegram_groups_active']}\n"
        f"Public /research: users {db_status['research_users']} · "
        f"used {db_status['research_used_total']} · limit {PUBLIC_RESEARCH_TOKEN_LIMIT}/user\n\n"
        f"<b>Runtime</b>\n"
        f"BG tasks {len(telegram_background_tasks)}/{TELEGRAM_BACKGROUND_COMMAND_LIMIT} · "
        f"Recheck queue {db_status['queued_rechecks']}\n"
        f"Deliveries pending/retry/failed: "
        f"{db_status['deliveries_pending']}/{db_status['deliveries_retry']}/{db_status['deliveries_failed']}\n"
        f"Tenants {db_status['tenants_active']} · Signals {db_status['signals_total']}\n"
        f"Cooldowns: {h(cooldowns)}"
    )


def track_background_command(label: str, coro) -> None:
    async def runner():
        async with telegram_background_semaphore:
            await coro

    task = asyncio.create_task(runner())
    telegram_background_tasks.add(task)

    def cleanup(done: asyncio.Task) -> None:
        telegram_background_tasks.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Telegram background command failed [{label}]: {e}", exc_info=True)

    task.add_done_callback(cleanup)


# ─── Telegram Command Handler ─────────────────────────────────────────────────

async def handle_telegram_commands(session: aiohttp.ClientSession):
    global last_update_id, blocked_accounts
    if not TELEGRAM_BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {
        "offset": last_update_id + 1,
        "timeout": TELEGRAM_GET_UPDATES_TIMEOUT,
        "limit": TELEGRAM_GET_UPDATES_LIMIT,
    }

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
                callback_id = update["callback_query"].get("id", "unknown")
                track_background_command(f"callback {callback_id}", handle_trade_callback(session, update["callback_query"]))
                continue

            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            if not text.startswith("/"):
                continue

            log.info(f"📩 Command from chat {chat_id}: {text[:50]}")
            cmd = command_name(text)

            if cmd in ADMIN_COMMANDS and not is_admin_update(msg):
                await send_telegram(session, "⛔ <b>Admin only</b>", chat_id=chat_id)
                continue
            if cmd not in PUBLIC_COMMANDS and cmd not in ADMIN_COMMANDS and not is_admin_update(msg):
                await send_telegram(session, "Unknown command.\n<code>/help</code>", chat_id=chat_id)
                continue

            if cmd == "/start":
                registered = await register_public_telegram_tenant(msg)
                if registered:
                    await send_telegram(session, build_welcome_text(), chat_id=chat_id)
                else:
                    await send_telegram(session, build_welcome_text(), chat_id=chat_id)

            elif cmd == "/help":
                if is_private_chat(msg):
                    await register_public_telegram_tenant(msg)
                await send_telegram(session, build_help_text(), chat_id=chat_id)

            elif cmd == "/block":
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await send_telegram(session, "🚫 <b>Block account</b>\n<code>/block @username</code>", chat_id)
                    continue
                username = parts[1].strip().lstrip("@").lower()
                blocked_accounts.add(username)
                save_blocklist(blocked_accounts)
                follower_cache.pop(username, None)
                log.info(f"🚫 Blocked @{username}")
                await send_telegram(session, f"🚫 <b>Blocked</b>\n@{h(username)}", chat_id)

            elif cmd == "/unblock":
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await send_telegram(session, "✅ <b>Unblock account</b>\n<code>/unblock @username</code>", chat_id)
                    continue
                username = parts[1].strip().lstrip("@").lower()
                blocked_accounts.discard(username)
                save_blocklist(blocked_accounts)
                log.info(f"✅ Unblocked @{username}")
                await send_telegram(session, f"✅ <b>Unblocked</b>\n@{h(username)}", chat_id)

            elif cmd == "/blocklist":
                if blocked_accounts:
                    names = "\n".join(f"• @{u}" for u in sorted(blocked_accounts))
                    await send_telegram(session, f"🚫 <b>Blocked Accounts</b> · {len(blocked_accounts)}\n\n{h(names)}", chat_id)
                else:
                    await send_telegram(session, "🚫 <b>Blocked Accounts</b>\n\nEmpty.", chat_id)

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
                await send_telegram(session, "✅ <b>Test signal sent</b>", chat_id=chat_id)

            elif cmd == "/status":
                async with db_session() as db:
                    db_status = await get_status_snapshot(db)
                cooldowns = ", ".join(db_status["provider_cooldowns"]) or "none"
                await send_telegram(
                    session,
                    f"📡 <b>Status</b>\n\n"
                    f"<b>Runtime</b>\n"
                    f"Scan {POLL_INTERVAL}s · Commands {TELEGRAM_COMMAND_POLL_INTERVAL}s · BG {len(telegram_background_tasks)}/{TELEGRAM_BACKGROUND_COMMAND_LIMIT}\n\n"
                    f"<b>Signals</b>\n"
                    f"Tenants {db_status['tenants_active']} · Sent {db_status['signals_total']} · Queue {db_status['queued_rechecks']}\n"
                    f"Delivery {db_status['deliveries_pending']}/{db_status['deliveries_retry']}/{db_status['deliveries_failed']} pending/retry/failed\n\n"
                    f"<b>Filters</b>\n"
                    f"MC {fmt_usd(MIN_MCAP)} · Vol {fmt_usd(MIN_VOLUME_24H)} · Liq {fmt_usd(MIN_LIQUIDITY)}\n\n"
                    f"<b>Providers</b>\n"
                    f"CoinGecko {'ON' if COINGECKO_DISCOVERY_ENABLED and COINGECKO_API_KEY else 'OFF'} · "
                    f"Verdict {'ON' if AUTO_VERDICT_ENABLED else 'OFF'} · "
                    f"Wallets {'ON' if WALLET_MONITOR_ENABLED and ALCHEMY_RPC_URL else 'OFF'}\n"
                    f"Cooldowns: {h(cooldowns)}",
                    chat_id,
                )

            elif cmd == "/admin":
                await send_telegram(session, await build_admin_panel_text(), chat_id, reply_markup=build_admin_keyboard())

            elif cmd == "/verdict2":
                parts = text.split(maxsplit=1)
                ca = parts[1].strip() if len(parts) == 2 else ""
                if not is_base_contract(ca):
                    await send_telegram(session, "🤖 <b>Verdict 2.0</b>\n<code>/verdict2 0x...</code>", chat_id)
                    continue
                await send_telegram(session, f"🤖 <b>Verdict queued</b>\n<code>{ca.lower()}</code>", chat_id)

                async def do_verdict2():
                    try:
                        result = await analyze_ca_for_command(session, ca, requested_by="telegram_verdict2", include_summary=True)
                        await send_telegram(session, format_verdict2_report(result), chat_id)
                    except Exception as e:
                        log.error(f"Verdict2 command failed for {ca}: {e}", exc_info=True)
                        await send_telegram(session, f"❌ <b>Verdict failed</b>\n{h(str(e)[:160])}", chat_id)

                track_background_command(f"verdict2 {ca.lower()}", do_verdict2())

            elif cmd in ("/spoof-check", "/spoof_check"):
                parts = text.split(maxsplit=1)
                ca = parts[1].strip() if len(parts) == 2 else ""
                if not is_base_contract(ca):
                    await send_telegram(session, "🕵️ <b>Spoof Check</b>\n<code>/spoof_check 0x...</code>", chat_id)
                    continue
                await send_telegram(session, f"🕵️ <b>Spoof check queued</b>\n<code>{ca.lower()}</code>", chat_id)

                async def do_spoof_check():
                    try:
                        result = await analyze_ca_for_command(session, ca, requested_by="telegram_spoof", include_summary=False)
                        await send_telegram(session, format_spoof_report(result), chat_id)
                    except Exception as e:
                        log.error(f"Spoof command failed for {ca}: {e}", exc_info=True)
                        await send_telegram(session, f"❌ <b>Spoof check failed</b>\n{h(str(e)[:160])}", chat_id)

                track_background_command(f"spoof {ca.lower()}", do_spoof_check())

            elif cmd == "/summary":
                parts = text.split(maxsplit=1)
                ca = parts[1].strip() if len(parts) == 2 else ""
                if not is_base_contract(ca):
                    await send_telegram(session, "🧠 <b>Summary</b>\n<code>/summary 0x...</code>", chat_id)
                    continue
                await send_telegram(session, f"🧠 <b>Summary queued</b>\n<code>{ca.lower()}</code>", chat_id)

                async def do_summary():
                    try:
                        result = await analyze_ca_for_command(session, ca, requested_by="telegram_summary", include_summary=True)
                        await send_telegram(session, format_summary_report(result), chat_id)
                    except Exception as e:
                        log.error(f"Summary command failed for {ca}: {e}", exc_info=True)
                        await send_telegram(session, f"❌ <b>Summary failed</b>\n{h(str(e)[:160])}", chat_id)

                track_background_command(f"summary {ca.lower()}", do_summary())

            elif cmd == "/watch":
                parts = text.split(maxsplit=2)
                watch_query = parts[1].strip() if len(parts) >= 2 else ""
                label = parts[2].strip() if len(parts) >= 3 else ""
                if not watch_query:
                    await send_telegram(
                        session,
                        "⭐ <b>Watch token</b>\n"
                        "<code>/watch 0x... [label]</code>\n"
                        "<code>/watch $TICKER [label]</code>",
                        chat_id,
                    )
                    continue
                tenant = await ensure_tenant_for_chat(chat_id, title=telegram_user_title(msg))
                await send_telegram(session, f"⭐ <b>Watchlist update queued</b>\n{h(watch_query)}", chat_id)

                async def do_watch():
                    try:
                        ca, launch, dex = await resolve_watch_target(session, watch_query)
                        async with db_session() as db:
                            item, inserted = await upsert_watchlist_item(db, tenant_id=tenant.id, ca=ca, label=label, market_json=dex)
                            await upsert_user_feedback(
                                db,
                                tenant_id=tenant.id,
                                ca=ca,
                                action="worth_watching",
                                payload_json={"command": "/watch", "label": label},
                            )
                        await send_telegram(
                            session,
                            format_watch_added_message(item, launch, inserted=inserted),
                            chat_id,
                            reply_markup=build_watch_item_keyboard(item, launch),
                        )
                    except WatchTargetAmbiguous as e:
                        await send_telegram(session, format_watch_ambiguous_message(e.query, e.candidates), chat_id)
                    except Exception as e:
                        log.error(f"Watch command failed for {watch_query}: {e}", exc_info=True)
                        await send_telegram(session, f"❌ <b>Watch failed</b>\n{h(str(e)[:160])}", chat_id)

                track_background_command(f"watch {watch_query}", do_watch())

            elif cmd == "/unwatch":
                parts = text.split(maxsplit=1)
                ca = parts[1].strip().lower() if len(parts) == 2 else ""
                if not is_base_contract(ca):
                    await send_telegram(session, "⭐ <b>Unwatch token</b>\n<code>/unwatch 0x...</code>", chat_id)
                    continue
                tenant = await ensure_tenant_for_chat(chat_id, title=telegram_user_title(msg))
                async with db_session() as db:
                    removed = await deactivate_watchlist_item(db, tenant_id=tenant.id, ca=ca)
                if removed:
                    await send_telegram(session, f"✅ <b>Removed from watchlist</b>\n{h(short_ca(ca))}", chat_id)
                else:
                    await send_telegram(session, f"⭐ <b>Not in watchlist</b>\n{h(short_ca(ca))}", chat_id)

            elif cmd == "/watchlist":
                tenant = await ensure_tenant_for_chat(chat_id, title=telegram_user_title(msg))
                async with db_session() as db:
                    items = await list_watchlist_items(db, tenant_id=tenant.id, limit=200)
                launches = await load_watchlist_launches(items)
                await send_telegram(
                    session,
                    build_watchlist_message(items, page=1, launches=launches),
                    chat_id,
                    reply_markup=build_watchlist_keyboard(items, page=1, launches=launches),
                )

            elif cmd == "/settings":
                tenant = await ensure_tenant_for_chat(chat_id, title=telegram_user_title(msg))
                parts = text.split()
                async with db_session() as db:
                    if len(parts) == 3 and parts[1].lower() == "min_score":
                        try:
                            min_score = float(parts[2])
                        except ValueError:
                            await send_telegram(session, "⚙️ <b>Settings</b>\n<code>/settings min_score 7.5</code>", chat_id)
                            continue
                        settings_row = await update_tenant_min_score(db, tenant_id=tenant.id, min_score=min_score)
                    elif len(parts) == 1:
                        settings_row = await get_tenant_settings(db, tenant_id=tenant.id)
                    else:
                        await send_telegram(session, "⚙️ <b>Settings</b>\n<code>/settings</code>\n<code>/settings min_score 7.5</code>", chat_id)
                        continue
                await send_telegram(session, format_settings_text(float(settings_row.min_score)), chat_id)

            elif cmd == "/track":
                parts = text.split(maxsplit=2)
                address = parts[1].strip() if len(parts) >= 2 else ""
                label = parts[2].strip() if len(parts) >= 3 else ""
                if not is_base_contract(address):
                    await send_telegram(session, "🐋 <b>Track wallet</b>\n<code>/track 0x... [label]</code>", chat_id)
                    continue
                tenant = await ensure_tenant_for_chat(chat_id, title=telegram_user_title(msg))
                async with db_session() as db:
                    _, inserted = await upsert_tracked_wallet(db, tenant_id=tenant.id, address=address, label=label)
                if inserted:
                    label_text = f"\n{h(label)}" if label else ""
                    await send_telegram(session, f"✅ <b>Tracking wallet</b>{label_text}\n<code>{address.lower()}</code>", chat_id)
                else:
                    await send_telegram(session, f"✅ <b>Wallet tracking updated</b>\n<code>{address.lower()}</code>", chat_id)

            elif cmd == "/untrack":
                parts = text.split(maxsplit=1)
                address = parts[1].strip() if len(parts) == 2 else ""
                if not is_base_contract(address):
                    await send_telegram(session, "🐋 <b>Untrack wallet</b>\n<code>/untrack 0x...</code>", chat_id)
                    continue
                tenant = await ensure_tenant_for_chat(chat_id, title=telegram_user_title(msg))
                async with db_session() as db:
                    removed = await deactivate_tracked_wallet(db, tenant_id=tenant.id, address=address)
                if removed:
                    await send_telegram(session, f"✅ <b>Untracked wallet</b>\n<code>{address.lower()}</code>", chat_id)
                else:
                    await send_telegram(session, "🐋 <b>Wallet not found</b>\n<code>/wallets</code>", chat_id)

            elif cmd in ("/wallets", "/tracked_wallets"):
                tenant = await ensure_tenant_for_chat(chat_id, title=telegram_user_title(msg))
                async with db_session() as db:
                    wallets = await list_tracked_wallets(db, tenant_id=tenant.id, limit=50)
                if not wallets:
                    await send_telegram(session, "🐋 <b>Tracked Wallets</b>\n\nEmpty.\n<code>/track 0x... [label]</code>", chat_id)
                    continue
                lines = [f"🐋 <b>Tracked Wallets</b> · {len(wallets)}", ""]
                for idx, wallet in enumerate(wallets[:30], 1):
                    label = html.escape(wallet.label or "")
                    suffix = f" · <b>{label}</b>" if label else ""
                    address = wallet.address
                    lines.append(f"{idx}. <code>{address}</code>{suffix}")
                    lines.append(f"   <a href='https://basescan.org/address/{address}'>BaseScan</a>")
                await send_telegram(session, "\n".join(lines), chat_id)

            elif cmd in ("/research", "/r"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    await send_telegram(session, "🔍 <b>Research</b>\n<code>/research 0x...</code>\n<code>/research $TICKER</code>", chat_id)
                    continue
                ticker_query = parts[1].strip()
                allowed, used, limit = await consume_public_research_quota_for_message(msg)
                if not allowed:
                    await send_telegram(session, format_research_quota_exhausted(used, limit), chat_id)
                    continue
                await send_telegram(session, f"🔍 <b>Research queued</b>\n<code>{h(ticker_query)}</code>", chat_id)

                async def do_research():
                    try:
                        report, action_keyboard, resolved_ca, _ = await research_token(
                            session,
                            ticker_query,
                            include_keyboard=True,
                        )
                        ca_query = (resolved_ca or ticker_query).strip().lower()
                        social_evidence = (
                            _xsignal_page_cache.get(xsignal_cache_key(ca_query))
                            if is_base_contract(ca_query)
                            else None
                        )
                        await send_telegram(
                            session,
                            report,
                            chat_id,
                            reply_markup=merge_inline_keyboards(
                                action_keyboard,
                                build_xsignal_pagination_keyboard(ca_query, social_evidence, 1),
                            ),
                        )
                    except Exception as re:
                        log.error(f"Research error for {ticker_query}: {re}")
                        await send_telegram(session, f"❌ <b>Research failed</b>\n<code>{h(ticker_query)}</code>\n{h(str(re)[:120])}", chat_id)

                track_background_command(f"research {ticker_query}", do_research())

            else:
                await send_telegram(session, "Unknown command.\n<code>/help</code>", chat_id=chat_id)

    except Exception as e:
        log.warning(f"Telegram command check error: {e}")


async def telegram_command_loop(session: aiohttp.ClientSession) -> None:
    while True:
        await handle_telegram_commands(session)
        await asyncio.sleep(TELEGRAM_COMMAND_POLL_INTERVAL)


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


def to_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def non_negative_float(value, default: float = 0.0) -> float:
    return max(0.0, to_float(value, default))


COMMON_BASE_ASSET_ADDRESSES = {
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",  # cbETH
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
    "0x940181a94a35a4569e4529a3cdfb74e38fd98631",  # AERO
    "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b",  # VIRTUAL
}
COMMON_BASE_ASSET_SYMBOLS = {"WETH", "ETH", "USDC", "USDBC", "DAI", "CBETH", "CBBTC", "AERO", "VIRTUAL"}


def is_common_base_asset(address: str = "", symbol: str = "") -> bool:
    return str(address or "").lower() in COMMON_BASE_ASSET_ADDRESSES or str(symbol or "").upper() in COMMON_BASE_ASSET_SYMBOLS


def same_market_pool(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    pa = str(a.get("pair_address") or "").lower()
    pb = str(b.get("pair_address") or "").lower()
    return bool(pa and pb and pa == pb)


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
        mcap = to_float(best.get("market_cap_usd") or best.get("fdv_usd") or 0)
        vol_raw = best.get("volume_usd") or {}
        vol_24h = to_float(vol_raw.get("h24") or 0)
        liquidity = to_float(best.get("reserve_in_usd") or 0)
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
            "pair_address": (best.get("address") or "").lower(),
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
            elif same_market_pool(result, gecko_result):
                for key in ("liquidity", "mcap", "volume_24h"):
                    if gecko_result.get(key, 0) > result.get(key, 0):
                        result[key] = gecko_result[key]
                if not result.get("pair_created_at") and gecko_result.get("pair_created_at"):
                    result["pair_created_at"] = gecko_result["pair_created_at"]
                result["_source"] = "dexscreener+geckoterminal_same_pool"
            else:
                result["secondary_market_json"] = gecko_result
                result["selection_warning"] = "geckoterminal_fallback_pair_mismatch_not_merged"
                result["selection_reason"] = "dexscreener_primary_pair"

    # Cache the result
    gecko_cache[token_address] = (time.time(), result)
    return result


MAX_TOKEN_AGE = int(os.getenv("MAX_TOKEN_AGE", str(4 * 3600)))


def passes_market_filters(dex: dict | None, source: str = "", *, enforce_age: bool = True) -> tuple[bool, str]:
    """Check if token passes market filters.

    For safe launchpads (bankr, clanker, virtuals) — skip liquidity check
    because they have locked LP / bonding curves and can't rug.
    Only mcap + volume need to pass.
    """
    if dex is None:
        return False, "no market data"

    is_safe = source.lower() in SAFE_LAUNCHPADS

    pair_created = dex.get("pair_created_at", 0)
    if enforce_age and pair_created:
        age_seconds = time.time() - (pair_created / 1000)
        if age_seconds > MAX_TOKEN_AGE:
            age_hours = age_seconds / 3600
            return False, f"too old ({age_hours:.1f}h > {MAX_TOKEN_AGE//3600}h)"
    elif enforce_age:
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
RESEARCH_NOISE_PENALTIES = {
    "gm": 2,
    "gn": 2,
    "lfg": 1,
    "lol": 1,
    "lmao": 1,
    "meme": 2,
    "bro": 1,
    "vibes": 1,
    "shitpost": 2,
    "just vibes": 2,
    "not crypto advice": 1,
}
RESEARCH_TOP_AUTHORS = {
    "supercontraa",
    "game_for_one",
    "moneylord",
    "0xunihax0r",
    "decentrlizordie",
    "0xsammy",
    "aixbt_agent",
}
RESEARCH_GENERIC_HASHTAGS = {"crypto", "bitcoin", "ethereum", "web3", "blockchain", "defi", "base"}
RESEARCH_EXCLUDED_TOKENS = {"BTC", "ETH", "USDT", "USDC", "USD", "GM", "SOL"}
RESEARCH_HARD_SPAM_URLS = ("okai.hk/alpha",)
RESEARCH_TOKEN_RE = re.compile(r"\$([A-Za-z0-9]{2,10})\b")
RESEARCH_HASHTAG_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{2,20})\b")


def contains_term(text_lower: str, term: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(term.lower())}(?![A-Za-z0-9_])", text_lower))


def build_research_query(ticker: str, address: str = "") -> str:
    if address:
        return strict_ca_query(address)
    return build_ticker_research_query(ticker)


def build_ticker_research_query(ticker: str) -> str:
    return strict_ticker_query(ticker)


def build_address_research_query(address: str) -> str:
    return strict_ca_query(address)


def research_clean_text(text: str) -> str:
    text = re.sub(r"https?://\S+", "", str(text or ""))
    return " ".join(text.split())


def research_tokens(text: str) -> list[str]:
    found = {token.upper() for token in RESEARCH_TOKEN_RE.findall(text)}
    return sorted(
        token for token in found
        if token not in RESEARCH_EXCLUDED_TOKENS
        and not token.isdigit()
        and not re.fullmatch(r"\d+[KMB]?", token)
    )


def research_thesis_quality(text: str, tokens: list[str]) -> float:
    clean = research_clean_text(text)
    lower = clean.lower()
    words = len(clean.split())
    score = 0.0
    if words >= 40:
        score += 4
    elif words >= 25:
        score += 3
    elif words >= 15:
        score += 2
    elif words >= 8:
        score += 1
    if tokens:
        score += 2
    if re.search(r"\d+[%xXkKmMbB]|\$\d|\d+\.\d", clean):
        score += 2
    score += min(sum(1 for kw in RESEARCH_TIER1_KEYWORDS if contains_term(lower, kw)) * 2.0, 6.0)
    score += min(sum(1 for kw in RESEARCH_TIER2_KEYWORDS if contains_term(lower, kw)) * 1.0, 3.0)
    score += min(sum(1 for kw in RESEARCH_METRIC_KEYWORDS if contains_term(lower, kw)) * 1.5, 4.5)
    return score


def research_text_hash(text: str) -> str:
    clean = research_clean_text(text).lower().encode("utf-8")
    return hashlib.sha256(clean).hexdigest()[:16]


def research_word_set(text: str) -> set[str]:
    clean = research_clean_text(text).lower()
    return {w for w in clean.split() if len(w) > 2 and not w.startswith(("@", "#", "$"))}


def research_near_duplicate(a: str, b: str, threshold: float = 0.75) -> bool:
    left = research_word_set(a)
    right = research_word_set(b)
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= threshold


def research_has_spam_hashtag(text: str) -> bool:
    tags = [tag.lower() for tag in RESEARCH_HASHTAG_RE.findall(text)]
    if not tags:
        return False
    meaningful = [tag for tag in tags if tag not in RESEARCH_GENERIC_HASHTAGS]
    return len(tags) >= 2 or bool(meaningful)


def research_is_hard_spam(text: str) -> bool:
    normalized = str(text or "").lower().replace("https://", "").replace("http://", "")
    return any(url in normalized for url in RESEARCH_HARD_SPAM_URLS)


def research_has_min_engagement(tweet: dict) -> bool:
    return (
        int(tweet.get("views") or 0) >= RESEARCH_MIN_TWEET_VIEWS
        and int(tweet.get("likes") or 0) >= RESEARCH_MIN_TWEET_LIKES
    )


def research_relevance(tweet: dict, ticker: str, address: str = "") -> bool:
    annotated = annotate_tweet_source(tweet, ticker=ticker, address=address)
    has_ticker = bool(annotated.get("ticker_confirmed"))
    has_ca = bool(annotated.get("ca_confirmed"))
    if address:
        return has_ca
    return has_ticker or has_ca or bool(tweet.get("watched_influencer") and tweet.get("score", 0) >= 2)


def score_research_tweet(tweet: dict) -> dict:
    text = research_clean_text(tweet.get("text", ""))
    lower = text.lower()
    score = 0
    tier = 3
    tokens = research_tokens(text)
    tweet["tokens"] = tokens
    tweet["text_hash"] = research_text_hash(text)

    if len(text) < 20:
        tweet["score"] = 0
        tweet["tier"] = 3
        tweet["low_content"] = True
        return tweet

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

    if tweet.get("high_priority") or tweet.get("username", "").lower() in RESEARCH_TOP_AUTHORS:
        score += 5
        if tier == 3:
            tier = 2

    likes = int(tweet.get("likes") or 0)
    retweets = int(tweet.get("retweets") or 0)
    replies = int(tweet.get("replies") or 0)
    views = int(tweet.get("views") or 0)
    bookmarks = int(tweet.get("bookmarks") or 0)
    quotes = int(tweet.get("quotes") or 0)
    followers = int(tweet.get("followers") or 0)

    score += 6 if likes >= 200 else 4 if likes >= 50 else 2 if likes >= 10 else 0
    score += 4 if retweets >= 100 else 2 if retweets >= 25 else 1 if retweets >= 5 else 0
    score += 3 if replies >= 100 else 2 if replies >= 50 else 1 if replies >= 10 else 0
    score += 3 if views >= 100_000 else 2 if views >= 10_000 else 1 if views >= 1_000 else 0
    score += 3 if bookmarks >= 50 else 2 if bookmarks >= 10 else 1 if bookmarks >= 3 else 0
    score += 2 if quotes >= 20 else 1 if quotes >= 5 else 0
    score += 4 if followers >= 100_000 else 3 if followers >= 50_000 else 2 if followers >= 10_000 else 1 if followers >= 1_000 else 0

    created_at = tweet.get("created_at")
    if isinstance(created_at, datetime):
        dt = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
        age_hours = max(0.5, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
        likes_per_hour = likes / age_hours
        score += 3 if likes_per_hour >= 100 else 2 if likes_per_hour >= 30 else 1 if likes_per_hour >= 10 else 0

    for term, penalty in RESEARCH_NOISE_PENALTIES.items():
        if contains_term(lower, term):
            score -= penalty

    signal_hits = len(tokens) + tier1_hits + tier2_hits + metric_hits
    thesis_quality = research_thesis_quality(text, tokens)
    if signal_hits == 0:
        score = max(0, score) if tweet.get("watched_influencer") else 0
        tier = 3
    elif tier == 3 and (tokens or thesis_quality >= 4):
        tier = 2 if score >= 8 else 3
    if thesis_quality < 2 and not tweet.get("high_priority"):
        tier = 3

    tweet["score"] = max(score, 0)
    tweet["tier"] = tier
    tweet["thesis_quality"] = round(thesis_quality, 1)
    tweet["signal_hits"] = signal_hits
    tweet["low_content"] = thesis_quality < 2
    return tweet


def parse_socialdata_tweet(tweet: dict) -> dict | None:
    user = tweet.get("user", {})
    username = user.get("screen_name", "")
    tweet_id = tweet.get("id_str", "")
    text = (tweet.get("full_text") or tweet.get("text") or "").strip()
    if not username or not tweet_id or not text:
        return None
    if research_is_hard_spam(text):
        return None
    created_raw = tweet.get("tweet_created_at") or tweet.get("created_at") or ""
    created_dt = None
    if created_raw:
        try:
            created_dt = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        except ValueError:
            created_dt = None
    item = {
        "username": username,
        "name": user.get("name", ""),
        "bio": user.get("description", ""),
        "followers": int(user.get("followers_count") or 0),
        "text": text[:500],
        "likes": int(tweet.get("favorite_count") or 0),
        "retweets": int(tweet.get("retweet_count") or 0),
        "replies": int(tweet.get("reply_count") or 0),
        "views": int(tweet.get("views_count") or 0),
        "bookmarks": int(tweet.get("bookmark_count") or 0),
        "quotes": int(tweet.get("quote_count") or 0),
        "date": str(created_raw)[:16],
        "created_at": created_dt,
        "url": f"https://x.com/{username}/status/{tweet_id}",
        "source_provider": "socialdata",
        "high_priority": username.lower() in HIGH_PRIORITY_INFLUENCERS,
    }
    if not passes_social_intelligence_filters(item):
        return None
    if not research_has_min_engagement(item):
        return None
    return score_research_tweet(item)


def socialdata_normalize_query(query: str) -> str:
    return " ".join(str(query or "").strip().split()).lower()


def socialdata_search_cache_key(query: str, search_type: str, limit: int, max_pages: int) -> str:
    payload = {
        "query": socialdata_normalize_query(query),
        "type": (search_type or "Top").strip().lower(),
        "limit": int(limit),
        "max_pages": int(max_pages),
        "filters": {
            "version": "hermes-social-intelligence-v2",
            "min_tweets": RESEARCH_MIN_QUALIFIED_TWEETS,
            "min_views": RESEARCH_MIN_TWEET_VIEWS,
            "min_likes": RESEARCH_MIN_TWEET_LIKES,
            "spam_urls": RESEARCH_HARD_SPAM_URLS,
        },
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return f"socialdata:search:{digest}"


def socialdata_serialize_tweets(tweets: list[dict]) -> list[dict]:
    serialized = []
    for item in tweets:
        copy = dict(item)
        created_at = copy.get("created_at")
        if isinstance(created_at, datetime):
            copy["created_at"] = created_at.isoformat()
        serialized.append(copy)
    return serialized


def socialdata_deserialize_tweets(tweets: list[dict]) -> list[dict]:
    deserialized = []
    for item in tweets:
        copy = dict(item)
        created_at = copy.get("created_at")
        if isinstance(created_at, str) and created_at:
            try:
                copy["created_at"] = datetime.fromisoformat(created_at)
            except ValueError:
                copy["created_at"] = None
        deserialized.append(copy)
    return deserialized


async def get_socialdata_search_cache(cache_key: str, *, allow_stale: bool = False) -> list[dict] | None:
    now_ts = time.time()
    cached = socialdata_search_cache.get(cache_key)
    if cached:
        expires_ts, stale_ts, tweets = cached
        if now_ts <= expires_ts or (allow_stale and now_ts <= stale_ts):
            return socialdata_deserialize_tweets(tweets)

    async with db_session() as db:
        raw = await get_bot_state(db, cache_key)
    if not raw:
        return None

    try:
        payload = json.loads(raw)
        expires_at = datetime.fromisoformat(payload["expires_at"])
        stale_until = datetime.fromisoformat(payload["stale_until"])
        expires_ts = expires_at.timestamp()
        stale_ts = stale_until.timestamp()
        tweets = payload.get("tweets") or []
    except Exception:
        return None

    if now_ts <= expires_ts or (allow_stale and now_ts <= stale_ts):
        socialdata_search_cache[cache_key] = (expires_ts, stale_ts, tweets)
        return socialdata_deserialize_tweets(tweets)
    return None


async def set_socialdata_search_cache(cache_key: str, tweets: list[dict]) -> None:
    now = utc_now()
    ttl = SOCIALDATA_SEARCH_CACHE_TTL_SEC if tweets else SOCIALDATA_SEARCH_EMPTY_CACHE_TTL_SEC
    expires_at = now + timedelta(seconds=ttl)
    stale_until = now + timedelta(seconds=max(ttl, SOCIALDATA_SEARCH_STALE_TTL_SEC))
    serialized = socialdata_serialize_tweets(tweets)
    socialdata_search_cache[cache_key] = (expires_at.timestamp(), stale_until.timestamp(), serialized)
    async with db_session() as db:
        await set_bot_state(
            db,
            cache_key,
            {
                "expires_at": expires_at.isoformat(),
                "stale_until": stale_until.isoformat(),
                "tweets": serialized,
            },
        )


async def socialdata_search_budget_available() -> bool:
    now = utc_now()
    async with db_session() as db:
        minute_used = await get_api_budget_usage(
            db,
            provider="socialdata",
            since=now - timedelta(minutes=1),
        )
        hour_used = await get_api_budget_usage(
            db,
            provider="socialdata",
            since=now - timedelta(hours=1),
        )
        if minute_used >= SOCIALDATA_SEARCH_MAX_CALLS_PER_MIN or hour_used >= SOCIALDATA_SEARCH_MAX_CALLS_PER_HOUR:
            await set_provider_cooldown(
                db,
                provider="socialdata",
                cooldown_until=now + timedelta(seconds=60),
                reason=f"local budget cap minute={minute_used}, hour={hour_used}",
            )
            return False
    return True


async def socialdata_search_uncached(
    session: aiohttp.ClientSession,
    query: str,
    search_type: str = "Top",
    limit: int = 20,
    timeout_sec: int = 10,
    max_pages: int = SOCIALDATA_SEARCH_MAX_PAGES,
) -> list[dict]:
    results = []
    url = "https://api.socialdata.tools/twitter/search"
    headers = {
        "Authorization": f"Bearer {SOCIALDATA_API_KEY}",
        "Accept": "application/json",
    }

    cursor = None
    seen_urls = set()
    pages = 0
    while pages < max(1, max_pages) and len(results) < limit:
        if not await socialdata_search_budget_available():
            log.debug("SocialData search budget exhausted; using cache/fallback only")
            break

        params = {"query": query, "type": search_type}
        if cursor:
            params["cursor"] = cursor

        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
            await record_provider_response("socialdata", endpoint="twitter/search", status_code=resp.status, cooldown_seconds=120)
            if resp.status != 200:
                body = await resp.text()
                log.debug(f"SocialData search {resp.status}: {body[:160]}")
                break
            data = await resp.json()

        pages += 1
        for tweet in data.get("tweets", []):
            item = parse_socialdata_tweet(tweet)
            if not item or item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            results.append(item)
            if len(results) >= limit:
                break

        cursor = data.get("next_cursor")
        if not cursor:
            break

    return results


async def socialdata_search(
    session: aiohttp.ClientSession,
    query: str,
    search_type: str = "Top",
    limit: int = 20,
    timeout_sec: int = 10,
    max_pages: int = SOCIALDATA_SEARCH_MAX_PAGES,
) -> list[dict]:
    if not SOCIALDATA_API_KEY:
        return []

    cache_key = socialdata_search_cache_key(query, search_type, limit, max_pages)
    cached = await get_socialdata_search_cache(cache_key)
    if cached is not None:
        log.debug(f"SocialData search cache hit: {socialdata_normalize_query(query)[:80]}")
        return cached[:limit]

    if not await is_provider_available("socialdata"):
        stale = await get_socialdata_search_cache(cache_key, allow_stale=True)
        return (stale or [])[:limit]

    if not await socialdata_search_budget_available():
        stale = await get_socialdata_search_cache(cache_key, allow_stale=True)
        return (stale or [])[:limit]

    existing_task = socialdata_search_inflight.get(cache_key)
    if existing_task:
        try:
            return (await asyncio.shield(existing_task))[:limit]
        except Exception:
            return []

    async def run_search() -> list[dict]:
        results = await socialdata_search_uncached(
            session,
            query,
            search_type=search_type,
            limit=limit,
            timeout_sec=timeout_sec,
            max_pages=max_pages,
        )
        await set_socialdata_search_cache(cache_key, results)
        return socialdata_deserialize_tweets(socialdata_serialize_tweets(results))

    task = asyncio.create_task(run_search())
    socialdata_search_inflight[cache_key] = task
    try:
        return (await task)[:limit]
    except Exception as e:
        log.debug(f"SocialData search error for '{query[:60]}': {e}")
        stale = await get_socialdata_search_cache(cache_key, allow_stale=True)
        return (stale or [])[:limit]
    finally:
        socialdata_search_inflight.pop(cache_key, None)


def filter_research_tweets(
    tweets: list[dict],
    *,
    ticker: str,
    address: str = "",
    limit: int = 6,
    allow_tier3: bool = False,
    min_count: int = RESEARCH_MIN_QUALIFIED_TWEETS,
    max_age_hours: int = 24,
) -> list[dict]:
    selected: list[dict] = []
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    for tweet in tweets:
        if not tweet or tweet.get("url") in seen_urls:
            continue
        tweet = annotate_tweet_source(tweet, ticker=ticker, address=address, provider=tweet.get("source_provider") or tweet.get("source") or "")
        text = tweet.get("text", "")
        if (
            research_is_hard_spam(text)
            or not research_has_min_engagement(tweet)
            or not passes_social_intelligence_filters(tweet)
        ):
            continue
        if not is_recent_tweet(tweet, max_age_hours=max_age_hours):
            continue
        if research_has_spam_hashtag(text) and not tweet.get("high_priority") and not tweet.get("watched_influencer"):
            continue
        if not research_relevance(tweet, ticker, address):
            continue
        if tweet.get("text_hash") in seen_hashes:
            continue
        if any(research_near_duplicate(text, existing.get("text", "")) for existing in selected):
            continue
        score = int(tweet.get("score") or 0)
        tier = int(tweet.get("tier") or 3)
        qualifies = (
            tweet.get("high_priority")
            or tweet.get("watched_influencer")
            or tweet.get("followers", 0) >= RESEARCH_MIN_FOLLOWERS
            or score >= RESEARCH_HIGH_SIGNAL_SCORE
            or (allow_tier3 and score >= 2 and not tweet.get("low_content"))
        )
        if not qualifies:
            continue
        if tier <= 2 or allow_tier3 or tweet.get("high_priority") or tweet.get("watched_influencer"):
            selected.append(tweet)
            seen_urls.add(tweet["url"])
            seen_hashes.add(tweet.get("text_hash", ""))
        if len(selected) >= limit:
            break
    selected.sort(
        key=lambda m: (
            ca_first_sort_key(m)[0],
            1 if int(m.get("tier") or 3) == 1 else 0,
            int(m.get("score") or 0),
            float(m.get("thesis_quality") or 0),
            int(m.get("followers") or 0),
            int(m.get("likes") or 0),
        ),
        reverse=True,
    )
    if min_count and len(selected) < min_count:
        return []
    return selected[:limit]


async def search_x_mentions(
    session: aiohttp.ClientSession,
    ticker: str,
    token_name: str = "",
    address: str = "",
    max_age_hours: int = 24,
    allow_tier3: bool = False,
    limit: int = 24,
    min_count: int = RESEARCH_MIN_QUALIFIED_TWEETS,
) -> list[dict]:
    query = build_research_query(ticker, address)
    if not query:
        return []

    raw_mentions = await socialdata_search(session, f"{query} min_faves:5", search_type="Top", limit=80)
    mentions = filter_research_tweets(
        raw_mentions,
        ticker=ticker,
        address=address,
        limit=limit,
        max_age_hours=max_age_hours,
        allow_tier3=allow_tier3,
        min_count=min_count,
    )
    return mentions


async def search_x_ticker_recent(
    session: aiohttp.ClientSession,
    ticker: str,
    address: str = "",
    limit: int = 8,
    max_age_hours: int = 24,
) -> list[dict]:
    """Search latest tweets through Nitter only; SocialData is reserved for Top tweets."""
    return await search_nitter_mentions(
        session,
        ticker,
        address=address,
        limit=limit,
        max_age_hours=max_age_hours,
    )


async def search_influencer_mentions(
    session: aiohttp.ClientSession,
    ticker: str,
    address: str = "",
    limit: int = 8,
    max_age_hours: int = 24,
) -> list[dict]:
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
        tweets = await socialdata_search(session, f"{token_query} {from_query}", search_type="Latest", limit=max(limit * 2, RESEARCH_MIN_QUALIFIED_TWEETS))
        for tweet in tweets:
            if tweet["url"] in seen:
                continue
            tweet["watched_influencer"] = True
            tweet = score_research_tweet(tweet)
            seen.add(tweet["url"])
            found.append(tweet)
        if len(found) >= limit:
            break
    return filter_research_tweets(
        found,
        ticker=ticker,
        address=address,
        limit=limit,
        allow_tier3=True,
        max_age_hours=max_age_hours,
    )


def parse_nitter_rss_item(item: ET.Element, *, max_age_hours: int = 24) -> dict | None:
    title = item.findtext("title") or ""
    link = item.findtext("link") or ""
    pub_date = item.findtext("pubDate") or ""
    if not title or not link:
        return None
    username = ""
    match = re.search(r"/([^/]+)/status/", link)
    if match:
        username = match.group(1)
    created_at = None
    if pub_date:
        try:
            from email.utils import parsedate_to_datetime
            created_at = parsedate_to_datetime(pub_date)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except Exception:
            created_at = None
    tweet = {
        "username": username or "unknown",
        "name": username or "unknown",
        "bio": "",
        "followers": 0,
        "text": research_clean_text(title)[:500],
        "likes": 0,
        "retweets": 0,
        "replies": 0,
        "views": 0,
        "bookmarks": 0,
        "quotes": 0,
        "date": pub_date[:16],
        "created_at": created_at,
        "url": link.replace("nitter.net", "x.com"),
        "source": "nitter",
        "source_provider": "nitter",
    }
    if not is_recent_tweet(tweet, max_age_hours=max_age_hours):
        return None
    if not passes_social_intelligence_filters(tweet):
        return None
    return score_research_tweet(tweet)


async def search_nitter_mentions(
    session: aiohttp.ClientSession,
    ticker: str,
    address: str = "",
    limit: int = 12,
    max_age_hours: int = 24,
) -> list[dict]:
    if not NITTER_ENABLED or not NITTER_BASE_URLS:
        return []
    query = build_research_query(ticker, address)
    if not query:
        return []
    for base_url in NITTER_BASE_URLS:
        try:
            url = f"{base_url}/search/rss?f=tweets&q={quote(query)}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    continue
                body = await resp.text()
            root = ET.fromstring(body)
            parsed = []
            for item in root.findall("./channel/item"):
                tweet = parse_nitter_rss_item(item, max_age_hours=max_age_hours)
                if not tweet:
                    continue
                tweet = annotate_tweet_source(tweet, ticker=ticker, address=address, provider="nitter")
                if address and not tweet.get("ca_confirmed"):
                    continue
                parsed.append(tweet)
                if len(parsed) >= limit:
                    break
            return parsed
        except Exception as e:
            log.debug(f"Nitter search failed via {base_url}: {e}")
    return []


async def check_nitter_health(session: aiohttp.ClientSession) -> tuple[bool, str, str, int | None, int]:
    if not NITTER_ENABLED:
        return False, "", "Nitter disabled", None, 0
    if not NITTER_BASE_URLS:
        return False, "", "NITTER_BASE_URLS empty", None, 0

    errors: list[str] = []
    for base_url in NITTER_BASE_URLS:
        try:
            started = time.perf_counter()
            url = f"{base_url}/search/rss?f=tweets&q={quote(NITTER_HEALTH_QUERY)}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                response_ms = int((time.perf_counter() - started) * 1000)
                if resp.status != 200:
                    errors.append(f"{base_url}: HTTP {resp.status}")
                    continue
                body = await resp.text()
            root = ET.fromstring(body)
            items = root.findall("./channel/item")
            if not items:
                errors.append(f"{base_url}: RSS ok, 0 items")
                continue
            return True, base_url, f"{len(items)} RSS item(s)", response_ms, len(items)
        except Exception as e:
            errors.append(f"{base_url}: {str(e)[:90]}")
    return False, "", "; ".join(errors[:3]) or "unknown Nitter error", None, 0


async def nitter_health_loop(session: aiohttp.ClientSession) -> None:
    if not NITTER_HEALTH_ENABLED or not NITTER_ENABLED or not NITTER_BASE_URLS:
        nitter_health_state.update(
            {
                "ok": None,
                "last_error": "disabled" if not NITTER_ENABLED else "NITTER_BASE_URLS empty",
            }
        )
        return
    previous_ok: bool | None = None
    while True:
        ok, base_url, detail, response_ms, item_count = await check_nitter_health(session)
        async with db_session() as db:
            await record_nitter_health_log(
                db,
                base_url=base_url or (NITTER_BASE_URLS[0] if NITTER_BASE_URLS else ""),
                status="ok" if ok else "down",
                detail=detail,
                response_ms=response_ms,
                item_count=item_count,
            )
        now_iso = utc_now().isoformat()
        nitter_health_state.update(
            {
                "ok": ok,
                "last_check": now_iso,
                "last_error": "" if ok else detail,
                "base_url": base_url,
            }
        )
        if ok:
            nitter_health_state["last_ok"] = now_iso

        if previous_ok is not False and not ok and ADMIN_USER_ID:
            await send_telegram(
                session,
                "⚠️ <b>Nitter health failed</b>\n\n"
                f"{h(detail)}\n\n"
                "Cookies/profile likely need refresh.",
                chat_id=ADMIN_USER_ID,
                reply_markup=build_admin_keyboard(),
            )
        elif previous_ok is False and ok and ADMIN_USER_ID:
            await send_telegram(
                session,
                f"✅ <b>Nitter recovered</b>\n\n{h(base_url)} · {h(detail)}",
                chat_id=ADMIN_USER_ID,
                reply_markup=build_admin_keyboard(),
            )
        previous_ok = ok
        await asyncio.sleep(max(60, NITTER_HEALTH_INTERVAL_SEC))


def build_smart_fetch_orchestrator(session: aiohttp.ClientSession) -> SmartFetchOrchestrator:
    async def fetch_latest(*, ticker: str, address: str = "", limit: int = 12, max_age_hours: int = 24) -> list[dict]:
        return await search_nitter_mentions(
            session,
            ticker,
            address=address,
            limit=limit,
            max_age_hours=max_age_hours,
        )

    async def fetch_top(
        *,
        ticker: str,
        address: str = "",
        limit: int = 24,
        max_age_hours: int = 24,
        allow_tier3: bool = True,
        min_count: int = 0,
    ) -> list[dict]:
        return await search_x_mentions(
            session,
            ticker,
            address=address,
            max_age_hours=max_age_hours,
            allow_tier3=allow_tier3,
            limit=limit,
            min_count=min_count,
        )

    return SmartFetchOrchestrator(
        nitter=NitterFetcher(fetch_latest),
        socialdata=SocialDataFetcher(fetch_top),
        alpha_detector=AlphaDetector(),
    )


async def fetch_research_social_branch(
    orchestrator: SmartFetchOrchestrator,
    *,
    ticker: str,
    address: str = "",
    latest_limit: int = 12,
    top_limit: int = 24,
    latest_max_age_hours: int = 72,
    top_max_age_hours: int = 168,
    official_handles: set[str] | None = None,
    force_top_below: int = RESEARCH_MIN_QUALIFIED_TWEETS,
) -> SmartFetchResult:
    latest = await orchestrator.nitter.latest(
        ticker=ticker,
        address=address,
        limit=latest_limit,
        max_age_hours=latest_max_age_hours,
    )
    latest_evidence = build_social_evidence(
        latest,
        ticker=ticker,
        address=address,
        min_count=force_top_below,
        max_tweets=latest_limit,
        max_age_hours=latest_max_age_hours,
        include_context=bool(address),
        official_handles=official_handles,
    )
    latest_quality = int(latest_evidence.get("evidence_count") or 0)
    alpha_found, alpha_reason = orchestrator.alpha_detector.detect(latest)
    force_top = latest_quality < force_top_below
    top: list[dict] = []
    if alpha_found or force_top:
        top = await orchestrator.socialdata.top(
            ticker=ticker,
            address=address,
            limit=top_limit,
            max_age_hours=top_max_age_hours,
            min_count=0,
        )
    reason = alpha_reason
    if force_top and not alpha_found:
        reason = f"latest_quality_below_{force_top_below}"
    elif force_top and alpha_found:
        reason = f"{alpha_reason};latest_quality_below_{force_top_below}"
    return SmartFetchResult(
        latest=latest,
        top=top,
        alpha_found=alpha_found,
        socialdata_called=bool(top) or alpha_found or force_top,
        alpha_reason=reason,
    )


async def build_deep_social_evidence(
    session: aiohttp.ClientSession,
    *,
    ticker: str,
    address: str,
    seed_mentions: list[dict] | None = None,
    max_age_hours: int = 24,
) -> dict:
    seed_mentions = seed_mentions or []
    smart = await build_smart_fetch_orchestrator(session).fetch(
        ticker=ticker,
        address=address,
        latest_limit=12,
        top_limit=24,
        max_age_hours=max_age_hours,
        top_min_count=0,
    )

    combined: list[dict] = []
    seen: set[str] = set()
    for item in seed_mentions + smart.latest + smart.top:
        url = item.get("url", "")
        if not item or url in seen:
            continue
        seen.add(url)
        combined.append(item)
    evidence = build_social_evidence(
        combined,
        ticker=ticker,
        address=address,
        min_count=RESEARCH_MIN_QUALIFIED_TWEETS,
        max_tweets=24,
        max_age_hours=max_age_hours,
    )
    evidence["fetch_strategy"] = {
        "latest_provider": "nitter",
        "top_provider": "socialdata",
        "alpha_found": smart.alpha_found,
        "alpha_reason": smart.alpha_reason,
        "socialdata_called": smart.socialdata_called,
        "latest_count": len(smart.latest),
        "top_count": len(smart.top),
    }
    if smart.socialdata_called:
        async with db_session() as db:
            await record_socialdata_usage_log(
                db,
                endpoint="twitter/search",
                query_hash=hashlib.sha256(f"{ticker}:{address}:top".encode("utf-8")).hexdigest()[:24],
                mode="Top",
                result_count=len(smart.top),
                triggered_by_alpha=smart.alpha_found,
                alpha_reason=smart.alpha_reason,
            )
    return evidence


async def validate_ca_social_confirmation(
    session: aiohttp.ClientSession,
    *,
    ticker: str,
    address: str,
) -> tuple[bool, str, dict]:
    """Require social evidence to mention the exact contract, not only the ticker."""
    if not REQUIRE_CA_SOCIAL_CONFIRMATION:
        return True, "CA social confirmation disabled", {"enabled": False}
    if not address:
        return False, "missing contract address for CA social confirmation", {}
    if not SOCIALDATA_API_KEY:
        return False, "SocialData key missing; CA social confirmation unavailable", {}

    social_evidence = await build_deep_social_evidence(
        session,
        ticker=ticker,
        address=address,
    )
    ca_mentions = social_evidence.get("top_tweets") or []
    if social_evidence.get("qualified") and ca_mentions:
        top = sorted(
            ca_mentions,
            key=lambda item: (
                int(item.get("tweet_tier_score") or item.get("score") or 0),
                int(item.get("followers") or 0),
                int(item.get("likes") or 0),
            ),
            reverse=True,
        )[:5]
        evidence = {
            "enabled": True,
            "verified": True,
            "query_mode": "ca_only",
            "qualified_tweets": len(ca_mentions),
            "min_required": RESEARCH_MIN_QUALIFIED_TWEETS,
            "total_followers": sum(int(item.get("followers") or 0) for item in ca_mentions),
            "total_likes": sum(int(item.get("likes") or 0) for item in ca_mentions),
            "total_retweets": sum(int(item.get("retweets") or 0) for item in ca_mentions),
            "max_score": max((int(item.get("score") or 0) for item in ca_mentions), default=0),
            "avg_thesis_quality": round(
                sum(float(item.get("thesis_quality") or 0) for item in ca_mentions) / max(len(ca_mentions), 1),
                1,
            ),
            "social_evidence": social_evidence,
            "top_authors": [
                {
                    "username": item.get("username", ""),
                    "followers": int(item.get("followers") or 0),
                    "score": int(item.get("score") or 0),
                    "tier": int(item.get("tier") or 3),
                    "url": item.get("url", ""),
                }
                for item in top
            ],
        }
        return True, f"{len(ca_mentions)} CA-qualified X mention(s)", evidence

    fetch_strategy = social_evidence.get("fetch_strategy") or {}
    if not fetch_strategy.get("socialdata_called"):
        return (
            False,
            f"no Nitter alpha for CA; SocialData Top skipped ({fetch_strategy.get('alpha_reason') or 'no_alpha'})",
            {
                "enabled": True,
                "verified": False,
                "query_mode": "ca_only",
                "social_evidence": social_evidence,
            },
        )

    ticker_mentions = await search_x_mentions(session, ticker, address="", min_count=0)
    if ticker_mentions:
        return (
            False,
            f"ticker-qualified tweets found but no CA-qualified tweets; possible false contract/ticker hijack",
            {
                "enabled": True,
                "verified": False,
                "query_mode": "ca_only",
                "ticker_only_tweets": len(ticker_mentions),
            },
        )
    return False, "no CA-qualified X mentions passed filters", {"enabled": True, "verified": False, "query_mode": "ca_only"}


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


async def research_token(
    session: aiohttp.ClientSession,
    query: str,
    *,
    include_keyboard: bool = False,
    search_window_hours: int = 72,
    title: str = "Research",
) -> str | tuple[str, dict | None, str, str]:
    query = query.strip().lstrip("$").upper()
    if not query:
        text = "🔍 <b>Research</b>\n<code>/research 0x...</code>\n<code>/research $TICKER</code>"
        return (text, None, "", "") if include_keyboard else text

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

    nitter_age_hours = max(1, int(search_window_hours or 72))
    socialdata_age_hours = max(nitter_age_hours, 168)
    official_handles = dex_official_x_handles(dex)
    orchestrator = build_smart_fetch_orchestrator(session)
    ca_fetch = await fetch_research_social_branch(
        orchestrator,
        ticker=ticker,
        address=address,
        latest_limit=12,
        top_limit=24,
        latest_max_age_hours=nitter_age_hours,
        top_max_age_hours=socialdata_age_hours,
        official_handles=official_handles,
    )
    ticker_fetch = None
    if address:
        ticker_fetch = await fetch_research_social_branch(
            orchestrator,
            ticker=ticker,
            address="",
            latest_limit=12,
            top_limit=24,
            latest_max_age_hours=nitter_age_hours,
            top_max_age_hours=socialdata_age_hours,
            official_handles=official_handles,
        )
    x_mentions = ca_fetch.top + ((ticker_fetch.top if ticker_fetch else []) or [])
    influencer_mentions: list[dict] = []
    nitter_mentions = ca_fetch.latest + ((ticker_fetch.latest if ticker_fetch else []) or [])
    social_evidence = build_social_evidence(
        x_mentions + influencer_mentions + nitter_mentions,
        ticker=ticker,
        address=address,
        min_count=RESEARCH_MIN_QUALIFIED_TWEETS,
        max_tweets=24,
        max_age_hours=socialdata_age_hours,
        include_context=bool(address),
        official_handles=official_handles,
    )
    social_evidence["fetch_strategy"] = {
        "latest_provider": "nitter",
        "top_provider": "socialdata",
        "alpha_found": ca_fetch.alpha_found or bool(ticker_fetch and ticker_fetch.alpha_found),
        "alpha_reason": ca_fetch.alpha_reason if ca_fetch.alpha_found else ((ticker_fetch.alpha_reason if ticker_fetch else "") or ca_fetch.alpha_reason),
        "socialdata_called": ca_fetch.socialdata_called or bool(ticker_fetch and ticker_fetch.socialdata_called),
        "ca_latest_count": len(ca_fetch.latest),
        "ca_top_count": len(ca_fetch.top),
        "ticker_latest_count": len(ticker_fetch.latest) if ticker_fetch else 0,
        "ticker_top_count": len(ticker_fetch.top) if ticker_fetch else 0,
        "latest_count": len(nitter_mentions),
        "top_count": len(x_mentions),
    }
    for mode, fetch in (("ca", ca_fetch), ("ticker", ticker_fetch)):
        if not fetch or not fetch.socialdata_called:
            continue
        async with db_session() as db:
            await record_socialdata_usage_log(
                db,
                endpoint="twitter/search",
                query_hash=hashlib.sha256(f"{ticker}:{address}:{mode}:manual_top".encode("utf-8")).hexdigest()[:24],
                mode="Top",
                result_count=len(fetch.top),
                triggered_by_alpha=fetch.alpha_found,
                alpha_reason=fetch.alpha_reason,
            )
    launch_status = None
    launch_for_narrative = {
        "source": "manual",
        "address": address,
        "name": token_name or (dex or {}).get("token_name") or ticker,
        "symbol": ticker,
    }
    if address:
        async with db_session() as db:
            launch_status = await get_launch_status(db, address)
            existing_launch = await get_launch(db, address)
            if existing_launch and existing_launch.raw_json:
                launch_for_narrative = {
                    **existing_launch.raw_json,
                    "address": address,
                    "name": token_name or existing_launch.name or (dex or {}).get("token_name") or ticker,
                    "symbol": ticker or existing_launch.ticker or (dex or {}).get("token_symbol") or "",
                    "source": existing_launch.source or "manual",
                }
    if not dex and not x_mentions and not influencer_mentions and not nitter_mentions:
        text = (
            f"🔍 <b>No data found for ${ticker}</b>\n\n"
            f"No Base market data or CA-verified X mentions found after spam/engagement filters.\n"
            f"Try: /research 0x..."
        )
        return (text, None, address, ticker) if include_keyboard else text
    project_narrative = extract_project_narrative(
        ca=address,
        ticker=ticker,
        name=token_name or (dex or {}).get("token_name") or ticker,
        launch=launch_for_narrative,
        dex=dex,
        social_evidence=social_evidence,
    )

    body = format_research_card(
        title=title,
        token_name=token_name or (dex or {}).get("token_name") or ticker,
        ticker=ticker,
        address=address,
        dex=dex,
        deployer_info=deployer_info,
        x_mentions=x_mentions,
        influencer_mentions=influencer_mentions,
        nitter_mentions=nitter_mentions,
        social_evidence=social_evidence,
        project_narrative=project_narrative.to_dict(),
        launch_status=launch_status,
    )
    keyboard = build_signal_keyboard(address, ticker) if address else None
    return (body, keyboard, address, ticker) if include_keyboard else body


def clean_watch_query(value: str) -> str:
    return str(value or "").strip().lstrip("$")


class WatchTargetAmbiguous(ValueError):
    def __init__(self, query: str, candidates: list[dict]):
        super().__init__(f"Multiple Base tokens passed watch filters for {query}")
        self.query = query
        self.candidates = candidates


def gecko_search_pool_address(pool: dict) -> str:
    base_addr = pool.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
    return str(base_addr or "").replace("base_", "").lower()


def gecko_search_pool_symbol(pool: dict) -> str:
    pool_name = str((pool.get("attributes") or {}).get("name") or "")
    return pool_name.split(" / ")[0].strip() if " / " in pool_name else pool_name.strip()


def gecko_search_pool_market(pool: dict) -> dict:
    attrs = pool.get("attributes") or {}
    vol_raw = attrs.get("volume_usd") or {}
    price_changes = attrs.get("price_change_percentage") or {}
    return {
        "mcap": float(attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0),
        "volume_24h": float(vol_raw.get("h24") or 0),
        "liquidity": float(attrs.get("reserve_in_usd") or 0),
        "price_usd": attrs.get("base_token_price_usd") or attrs.get("price_in_usd") or "0",
        "price_change_1h": float(price_changes.get("h1") or 0),
        "price_change_24h": float(price_changes.get("h24") or 0),
        "pair_url": attrs.get("url") or f"https://www.geckoterminal.com/base/pools/{attrs.get('address', '')}",
        "pair_created_at": 0,
        "token_name": gecko_search_pool_symbol(pool),
        "token_symbol": gecko_search_pool_symbol(pool),
        "dex_id": attrs.get("dex_id", ""),
        "_source": "geckoterminal_search",
    }


def pool_passes_watch_filters(pool: dict) -> bool:
    market = gecko_search_pool_market(pool)
    return (
        market["mcap"] > 50_000
        and market["volume_24h"] > 50_000
        and market["liquidity"] > 50_000
    )


def watch_candidate(pool: dict) -> dict:
    address = gecko_search_pool_address(pool)
    market = gecko_search_pool_market(pool)
    return {
        "address": address,
        "symbol": market.get("token_symbol") or address[:8],
        "name": market.get("token_name") or "",
        "mcap": market.get("mcap") or 0,
        "volume_24h": market.get("volume_24h") or 0,
        "liquidity": market.get("liquidity") or 0,
    }


def choose_gecko_search_pool(pools: list[dict], query: str) -> dict | None:
    query_upper = clean_watch_query(query).upper()
    filtered = [pool for pool in pools if is_base_contract(gecko_search_pool_address(pool))]
    passing = [pool for pool in filtered if pool_passes_watch_filters(pool)]
    search_pool = passing or filtered
    best_pool = None
    best_score = -1.0
    for pool in search_pool:
        pool_attrs = pool.get("attributes") or {}
        vol_raw = pool_attrs.get("volume_usd") or {}
        volume = float(vol_raw.get("h24") or 0)
        reserve = float(pool_attrs.get("reserve_in_usd") or 0)
        address = gecko_search_pool_address(pool)
        if not is_base_contract(address):
            continue
        base_symbol = gecko_search_pool_symbol(pool).upper()
        exact_bonus = 1_000_000_000 if base_symbol == query_upper else 0
        score = exact_bonus + volume + reserve * 0.05
        if score > best_score:
            best_score = score
            best_pool = pool
    return best_pool


def format_watch_ambiguous_message(query: str, candidates: list[dict]) -> str:
    lines = [
        f"⭐ <b>Multiple Base tokens found for {h(query)}</b>",
        "",
        "These all pass watch filters: MC > 50K · Vol > 50K · Liq > 50K.",
        "Copy the exact CA you want and send:",
        "<code>/watch 0x...</code>",
        "",
    ]
    for idx, item in enumerate(candidates[:8], 1):
        lines.extend([
            f"{idx}. <b>${h(item.get('symbol') or '')}</b>"
            + (f" · {h(str(item.get('name') or '')[:36])}" if item.get("name") else ""),
            f"   MC {fmt_usd(item.get('mcap') or 0)} · Vol {fmt_usd(item.get('volume_24h') or 0)} · Liq {fmt_usd(item.get('liquidity') or 0)}",
            f"   <code>{h(item.get('address') or '')}</code>",
        ])
    if len(candidates) > 8:
        lines.append(f"\n+{len(candidates) - 8} more candidates hidden.")
    return "\n".join(lines)[:3900]


async def resolve_watch_target(session: aiohttp.ClientSession, query: str) -> tuple[str, dict, dict | None]:
    cleaned = clean_watch_query(query)
    if not cleaned:
        raise ValueError("missing token query")
    if is_base_contract(cleaned):
        launch, dex = await ensure_launch_for_analysis(session, cleaned.lower())
        return cleaned.lower(), launch, dex

    try:
        url = f"{GECKOTERMINAL_API_URL}/search/pools?query={quote(cleaned)}&network=base&page=1"
        headers = {"Accept": "application/json;version=20230302"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                raise ValueError(f"Base search returned {resp.status}")
            data = await resp.json()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Base search failed for {cleaned}") from exc

    pools = data.get("data", []) or []
    passing = [pool for pool in pools if is_base_contract(gecko_search_pool_address(pool)) and pool_passes_watch_filters(pool)]
    if len(passing) > 1:
        raise WatchTargetAmbiguous(cleaned, [watch_candidate(pool) for pool in passing])
    best_pool = passing[0] if len(passing) == 1 else choose_gecko_search_pool(pools, cleaned)
    if not best_pool:
        raise ValueError(f"No Base token found for {cleaned}")
    ca = gecko_search_pool_address(best_pool)
    launch, dex = await ensure_launch_for_analysis(session, ca)
    if dex:
        dex.setdefault("token_symbol", gecko_search_pool_symbol(best_pool))
        dex.setdefault("token_name", gecko_search_pool_symbol(best_pool))
    return ca, launch, dex


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


# ─── CoinGecko Onchain Discovery API ─────────────────────────────────────────

def _coingecko_rate_ok() -> bool:
    now = time.time()
    while coingecko_calls and coingecko_calls[0] < now - 60:
        coingecko_calls.pop(0)
    return len(coingecko_calls) < COINGECKO_RATE_LIMIT_PER_MIN


def _coingecko_included_map(included: list[dict]) -> dict[str, dict]:
    return {
        item.get("id", ""): item
        for item in included
        if isinstance(item, dict) and item.get("id")
    }


def _coingecko_related(item: dict, included_by_id: dict[str, dict], relation: str) -> dict:
    rel = ((item.get("relationships") or {}).get(relation) or {}).get("data") or {}
    if isinstance(rel, list):
        rel = rel[0] if rel else {}
    if not isinstance(rel, dict):
        return {}
    return included_by_id.get(rel.get("id", ""), {})


def choose_coingecko_pool_token_side(base_attrs: dict, quote_attrs: dict) -> tuple[str, dict] | None:
    candidates: list[tuple[str, dict, bool]] = []
    for side, attrs in (("base", base_attrs), ("quote", quote_attrs)):
        address = (attrs.get("address") or "").lower()
        symbol = (attrs.get("symbol") or "").upper()
        if not is_base_contract(address):
            continue
        candidates.append((side, attrs, is_common_base_asset(address, symbol)))
    non_common = [(side, attrs) for side, attrs, is_common in candidates if not is_common]
    if len(non_common) == 1:
        return non_common[0]
    return None


def _normalize_coingecko_pool(item: dict, included_by_id: dict[str, dict]) -> dict | None:
    attrs = item.get("attributes") or {}
    base = _coingecko_related(item, included_by_id, "base_token")
    quote = _coingecko_related(item, included_by_id, "quote_token")
    dex = _coingecko_related(item, included_by_id, "dex")
    base_attrs = base.get("attributes") or {}
    quote_attrs = quote.get("attributes") or {}
    dex_attrs = dex.get("attributes") or {}

    selected = choose_coingecko_pool_token_side(base_attrs, quote_attrs)
    if not selected:
        return None
    selected_side, token_attrs = selected
    address = (token_attrs.get("address") or "").lower()
    if not is_base_contract(address):
        return None

    pool_address = (attrs.get("address") or "").lower()
    price_changes = attrs.get("price_change_percentage") or {}
    volume = attrs.get("volume_usd") or {}
    txns = attrs.get("transactions") or {}
    h1_txns = txns.get("h1") or {}
    h24_txns = txns.get("h24") or {}

    created_at = attrs.get("pool_created_at") or ""
    pair_created_at = 0
    if created_at:
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            pair_created_at = int(dt.timestamp() * 1000)
        except ValueError:
            pair_created_at = 0

    name = token_attrs.get("name") or attrs.get("name") or "Unknown"
    symbol = token_attrs.get("symbol") or "?"
    dex_id = dex_attrs.get("identifier") or dex_attrs.get("name") or ""
    selected_price_key = "base_token_price_usd" if selected_side == "base" else "quote_token_price_usd"
    if selected_side == "base":
        selected_mcap = attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0
    else:
        selected_mcap = attrs.get("quote_token_market_cap_usd") or attrs.get("quote_token_fdv_usd") or 0

    market = {
        "mcap": non_negative_float(selected_mcap),
        "volume_24h": non_negative_float(volume.get("h24")),
        "liquidity": non_negative_float(attrs.get("reserve_in_usd")),
        "price_usd": attrs.get(selected_price_key) or "0",
        "price_change_1h": to_float(price_changes.get("h1")),
        "price_change_24h": to_float(price_changes.get("h24")),
        "pair_url": f"https://www.geckoterminal.com/base/pools/{pool_address or address}",
        "pair_created_at": pair_created_at,
        "pair_address": pool_address,
        "token_name": name,
        "token_symbol": symbol,
        "selected_token_side": selected_side,
        "selected_token_address": address,
        "selected_token_symbol": symbol,
        "base_token_address": (base_attrs.get("address") or "").lower(),
        "base_token_symbol": base_attrs.get("symbol") or "",
        "quote_token_address": (quote_attrs.get("address") or "").lower(),
        "quote_token_symbol": quote_attrs.get("symbol") or "",
        "dex_id": dex_id,
        "txns_h1_buys": int(to_float(h1_txns.get("buys"))),
        "txns_h1_sells": int(to_float(h1_txns.get("sells"))),
        "txns_h24_buys": int(to_float(h24_txns.get("buys"))),
        "txns_h24_sells": int(to_float(h24_txns.get("sells"))),
        "_source": "coingecko",
    }
    return {
        "source": "coingecko",
        "address": address,
        "name": name,
        "symbol": symbol,
        "x_username": "",
        "tweet_url": "",
        "image_uri": base_attrs.get("image_url") or "",
        "website_url": "",
        "pair_url": market["pair_url"],
        "source_method": "new_pools",
        "dex_id": dex_id,
        "created_at": created_at,
        "_dex": market,
    }


async def fetch_coingecko_new_pools(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch latest Base pools from CoinGecko Onchain with conservative credit usage."""
    global last_coingecko_poll_at

    if not COINGECKO_DISCOVERY_ENABLED:
        return []
    if not COINGECKO_API_KEY:
        log.warning("CoinGecko discovery enabled but COINGECKO_API_KEY is not set")
        return []
    if not await is_provider_available("coingecko"):
        log.debug("CoinGecko cooldown active, skipping new pools")
        return []

    now = time.time()
    if now - last_coingecko_poll_at < COINGECKO_POLL_INTERVAL:
        return []
    if not _coingecko_rate_ok():
        log.debug("CoinGecko local rate limit reached, skipping new pools")
        return []

    try:
        url = f"{COINGECKO_API_URL}/onchain/networks/base/new_pools"
        params = {"include": "base_token,quote_token,dex"}
        headers = {
            "Accept": "application/json",
            "x-cg-demo-api-key": COINGECKO_API_KEY,
        }
        coingecko_calls.append(now)
        last_coingecko_poll_at = now

        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            await record_provider_response(
                "coingecko",
                endpoint="onchain/base/new_pools",
                status_code=resp.status,
                cooldown_seconds=COINGECKO_COOLDOWN_SEC,
            )
            if resp.status == 429:
                log.warning("CoinGecko new_pools rate limited, cooling down")
                return []
            if resp.status != 200:
                body = await resp.text()
                log.warning(f"CoinGecko new_pools returned {resp.status}: {body[:160]}")
                return []
            raw = await resp.json()

        included_by_id = _coingecko_included_map(raw.get("included") or [])
        launches: list[dict] = []
        seen: set[str] = set()
        for item in raw.get("data") or []:
            launch = _normalize_coingecko_pool(item, included_by_id)
            if not launch or launch["address"] in seen:
                continue
            seen.add(launch["address"])
            launches.append(launch)
            if len(launches) >= COINGECKO_DISCOVERY_LIMIT:
                break

        log.info(f"CoinGecko: {len(launches)} Base new pools fetched")
        return launches
    except Exception as e:
        log.error(f"CoinGecko new_pools error: {e}")
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
    pair_created = dex.get("pair_created_at") or dex.get("pairCreatedAt") or dex.get("pool_created_at") or 0
    if not pair_created:
        return "n/a"
    if isinstance(pair_created, str):
        if pair_created.isdigit():
            pair_created = int(pair_created)
            if pair_created < 10_000_000_000:
                pair_created *= 1000
        else:
            try:
                dt = datetime.fromisoformat(pair_created.replace("Z", "+00:00"))
                pair_created = int(dt.timestamp() * 1000)
            except ValueError:
                return "n/a"
    if not isinstance(pair_created, (int, float)):
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


def dex_social_label(dex: dict | None, social_type: str) -> str:
    for item in (dex or {}).get("socials") or []:
        if (item.get("type") or "").lower() == social_type:
            url = item.get("url") or ""
            if social_type == "twitter" and "x.com/" in url:
                return "@" + url.rstrip("/").split("/")[-1]
            return url
    return ""


def dex_official_x_handles(dex: dict | None) -> set[str]:
    handles: set[str] = set()
    for item in (dex or {}).get("socials") or []:
        if (item.get("type") or "").lower() != "twitter":
            continue
        url = str(item.get("url") or "")
        if "x.com/" not in url and "twitter.com/" not in url:
            continue
        handle = url.rstrip("/").split("/")[-1].split("?")[0].strip().lstrip("@").lower()
        if handle:
            handles.add(handle)
    return handles


def dex_website_domain(dex: dict | None) -> str:
    websites = (dex or {}).get("websites") or []
    if not websites:
        return ""
    url = websites[0].get("url") if isinstance(websites[0], dict) else str(websites[0])
    domain = str(url or "").replace("https://", "").replace("http://", "").split("/", 1)[0]
    return domain.replace("www.", "")


def infer_initial_token_type(launch: dict, dex: dict | None) -> str:
    text = " ".join([
        str(launch.get("name") or ""),
        str(launch.get("symbol") or ""),
        str((dex or {}).get("token_name") or ""),
        dex_website_domain(dex),
    ]).lower()
    if any(term in text for term in ("privacy", "private", "veil", "cash", "shield")):
        return "Privacy / Base protocol"
    if any(term in text for term in ("agent", "ai", "bot", "automation")):
        return "AI / Agent"
    if any(term in text for term in ("app", "protocol", "terminal", "tool", "finance")):
        return "Utility / Protocol"
    return "Unclear / Experimental"


def build_initial_product_line(launch: dict, dex: dict | None) -> str:
    name = (dex or {}).get("token_name") or launch.get("name") or launch.get("symbol") or "Token"
    website = dex_website_domain(dex)
    x_handle = dex_social_label(dex, "twitter")
    token_type = infer_initial_token_type(launch, dex)
    if token_type.startswith("Privacy"):
        base = f"{name} appears positioned as a privacy-oriented Base protocol"
    else:
        base = f"{name} has live Base market data"
    sources = []
    if website:
        sources.append(f"site {website}")
    if x_handle:
        sources.append(f"X {x_handle}")
    if sources:
        return f"{base}; metadata links: {', '.join(sources[:2])}."
    return f"{base}; product proof still needs verified research."


def build_ai_summary_placeholder(launch: dict, dex: dict | None, verdict: dict | None = None) -> str:
    if verdict:
        return verdict.get("human_readable") or ""

    social_confirmation = launch.get("social_confirmation") or {}
    social_evidence = social_confirmation.get("social_evidence") or social_confirmation
    narrative = extract_project_narrative(
        ca=launch.get("address") or "",
        ticker=launch.get("symbol") or "",
        name=(dex or {}).get("token_name") or launch.get("name") or "",
        launch=launch,
        dex=dex,
        social_evidence=social_evidence if isinstance(social_evidence, dict) else {},
    )
    token_type = narrative_token_type(narrative, infer_initial_token_type(launch, dex))
    focus = "market data unavailable"
    risks = ["deep research not connected yet"]
    score = 0.0
    if dex:
        mcap = float(dex.get("mcap") or 0)
        volume = float(dex.get("volume_24h") or 0)
        liquidity = float(dex.get("liquidity") or 0)
        focus = f"MC {fmt_usd(mcap)} · Vol {fmt_usd(volume)} · Liq {fmt_usd(liquidity)}"
        if mcap >= MIN_MCAP:
            score += 2.0
        if volume >= MIN_VOLUME_24H:
            score += 2.0
        if liquidity >= MIN_LIQUIDITY:
            score += 1.5
        if dex_website_domain(dex):
            score += 0.8
        if dex_social_label(dex, "twitter"):
            score += 0.7
        if narrative.confidence == "HIGH":
            score += 1.0
        elif narrative.confidence == "MEDIUM":
            score += 0.6
        if mcap and liquidity and mcap / max(liquidity, 1) > 10:
            risks.append("MC is high relative to liquidity")
        elif narrative.is_ticker_only_evidence:
            risks.append("narrative is ticker-only, not CA-confirmed")
        else:
            risks.append("social and spoof checks still pending")
    score = min(score, 7.0)
    return (
        f"🧠 <b>AI brief</b> • Initial <b>{score:.1f}/10</b> · <b>WAIT</b>\n\n"
        f"• <b>Type:</b> {h(token_type)}\n"
        f"• <b>Product:</b> {h(narrative.product[:220])}\n"
        f"• <b>Why value:</b> {h(narrative.why_it_matters[:220])}\n"
        f"• <b>Focus:</b> {h(focus)}\n"
        f"• <b>Risks:</b> {h('; '.join(risks[:2]))}"
    )


def build_research_takeaway(dex: dict | None, x_mentions: list[dict], influencer_mentions: list[dict]) -> str:
    if not dex and not x_mentions and not influencer_mentions:
        return "No Base market or social signal found yet."
    positives: list[str] = []
    risks: list[str] = []
    if dex:
        passes, reason = passes_market_filters(dex, enforce_age=False)
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


def infer_research_token_type(token_name: str, ticker: str, x_mentions: list[dict]) -> str:
    text = " ".join([token_name, ticker] + [m.get("text", "")[:160] for m in x_mentions[:3]]).lower()
    if any(term in text for term in ("agent", "ai", "bot", "inference", "autonomous")):
        return "AI agent / Utility"
    if any(term in text for term in ("terminal", "scanner", "framework", "protocol", "app", "api", "tool")):
        return "Utility / Tooling"
    if any(term in text for term in ("community", "meme", "mascot", "cult")):
        return "Narrative / Community"
    return "Memecoin / Experimental"


def compact_sentence_text(value: str, *, limit: int = 520) -> str:
    clean = " ".join(str(value or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def research_risk_label(score: float, risks: list[str], social_evidence: dict | None) -> str:
    trust = (social_evidence or {}).get("trust_summary") or {}
    primary = (
        int(trust.get("ca_confirmed") or 0)
        + int(trust.get("pair_confirmed") or 0)
        + int(trust.get("project_confirmed") or 0)
    )
    ticker_context = int(trust.get("ticker_strong") or 0) + int(trust.get("ticker_only") or 0)
    if any("collision" in risk.lower() for risk in risks):
        return "HIGH RISK"
    if primary == 0 and ticker_context > 0:
        return "MEDIUM RISK"
    if score >= 7.0 and primary > 0:
        return "LOW RISK"
    if score >= 5.5:
        return "MEDIUM RISK"
    return "HIGH RISK"


def research_recommendation(score: float, social_evidence: dict | None, risks: list[str]) -> str:
    trust = (social_evidence or {}).get("trust_summary") or {}
    primary = (
        int(trust.get("ca_confirmed") or 0)
        + int(trust.get("pair_confirmed") or 0)
        + int(trust.get("project_confirmed") or 0)
    )
    if any("collision" in risk.lower() for risk in risks):
        return "SKIP"
    if score >= 7.5 and primary > 0:
        return "WATCH"
    if score >= 6.0:
        return "WATCH" if primary > 0 else "HIGH RISK"
    if score >= 4.5:
        return "HIGH RISK"
    return "SKIP"


def format_research_ai_brief(
    *,
    token_name: str,
    ticker: str,
    dex: dict | None,
    deployer_info: dict | None,
    x_mentions: list[dict],
    influencer_mentions: list[dict],
    nitter_mentions: list[dict] | None = None,
    social_evidence: dict | None = None,
    project_narrative: dict | None = None,
    launch_status: str | None = None,
) -> str:
    score = 0.0
    risks: list[str] = []
    if dex:
        passes, reason = passes_market_filters(dex, enforce_age=False)
        if passes:
            score += 3.5
        else:
            risks.append(reason)
    else:
        risks.append("market data missing")
    if influencer_mentions:
        score += 2.0
    elif x_mentions or nitter_mentions or (social_evidence or {}).get("top_tweets"):
        score += 1.2
    else:
        risks.append("no notable X coverage")
    if deployer_info and deployer_info.get("x_username"):
        score += 1.0
    if launch_status == "signaled":
        score += 1.0

    product = (project_narrative or {}).get("product") or "No product differentiation confirmed from current evidence."
    top_tweet = (influencer_mentions or x_mentions or [{}])[0]
    if not (project_narrative or {}).get("product") and top_tweet.get("text") and float(top_tweet.get("thesis_quality") or 0) >= 4:
        product = research_clean_text(top_tweet["text"])[:180]
    elif not (project_narrative or {}).get("product") and token_name:
        product = f"{token_name} has market/social traces, but product proof is not confirmed yet."

    thesis = (social_evidence or {}).get("thesis") or ""
    social_score = int((social_evidence or {}).get("social_score") or 0)
    trust = (social_evidence or {}).get("trust_summary") or {}
    primary_evidence = (
        int(trust.get("ca_confirmed") or 0)
        + int(trust.get("pair_confirmed") or 0)
        + int(trust.get("project_confirmed") or 0)
    )
    ticker_context = int(trust.get("ticker_strong") or 0) + int(trust.get("ticker_only") or 0)
    project_value = (
        narrative_token_type(project_narrative, "")
        if project_narrative else ""
    ) or (social_evidence or {}).get("project_value") or infer_research_token_type(token_name, ticker, x_mentions)
    if social_score:
        score += min(2.4, social_score / 35)
    if (project_narrative or {}).get("confidence") == "HIGH":
        score += 1.0
    elif (project_narrative or {}).get("confidence") == "MEDIUM":
        score += 0.6
    product_lower = product.lower()
    if any(term in product_lower for term in ("inference", "intelligence", "marketplace", "protocol", "platform", "privacy", "infrastructure")):
        score += 0.8
    if primary_evidence == 0 and ticker_context > 0:
        risks.append("ticker context is not contract-confirmed")
        score = min(score, 6.8)
    score = min(10.0, score)
    thesis_line = thesis if thesis else product[:180]
    lore_context = (project_narrative or {}).get("key_lore_context") or ""
    risk_label = research_risk_label(score, risks, social_evidence)
    recommendation = research_recommendation(score, social_evidence, risks)
    return (
        f"🧠 <b>AI brief</b> • Score <b>{score:.1f}/10</b>"
        + (f" · Social <b>{social_score}/100</b>" if social_score else "")
        + f" · <b>{risk_label}</b>"
        + "\n\n"
        + f"• <b>Recommendation:</b> {h(recommendation)}\n"
        + f"• <b>Type:</b> {h(project_value)}\n"
        + f"• <b>Product:</b> {h(compact_sentence_text(product, limit=560))}\n"
        + f"• <b>Thesis:</b> {h(compact_sentence_text(thesis_line, limit=360))}\n"
        + (f"• <b>Key Lore / Context:</b> {h(compact_sentence_text(lore_context, limit=320))}\n" if lore_context else "")
        + f"• <b>Risks:</b> {h('; '.join(risks[:2]) if risks else 'no major deterministic risk detected')}"
    )


def fmt_compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return str(value)


def format_compact_evidence_refs(social_evidence: dict, *, limit: int = 3) -> str:
    tweets = social_evidence.get("top_tweets") or []
    if not tweets:
        return "none"
    parts = []
    for item in tweets[:limit]:
        ref = int(item.get("ref") or 0)
        username = h(item.get("username") or "unknown")
        url = item.get("url") or ""
        views = fmt_compact_number(int(item.get("views") or 0))
        likes = fmt_compact_number(int(item.get("likes") or 0))
        retweets = fmt_compact_number(int(item.get("retweets") or 0))
        label = f"[{ref}] @{username} ❤️ {likes} · 👁 {views} · 🔄 {retweets}"
        if url:
            parts.append(f"<a href='{h(url)}'>{label}</a>")
        else:
            parts.append(label)
    return " · ".join(parts)


def xsignal_cache_key(address: str) -> str:
    return (address or "").lower()[:12]


def remember_xsignal_evidence(address: str, social_evidence: dict | None) -> None:
    if not address or not social_evidence:
        return
    key = xsignal_cache_key(address)
    _address_map[key] = address.lower()
    _xsignal_page_cache[key] = social_evidence


def xsignal_visible_tweets(social_evidence: dict | None) -> list[dict]:
    tweets = (social_evidence or {}).get("top_tweets") or []
    return [
        item for item in tweets
        if item.get("language") == "en" and is_likely_english_text(item.get("excerpt") or "")
    ]


def xsignal_page_count(social_evidence: dict | None) -> int:
    count = len(xsignal_visible_tweets(social_evidence))
    if count <= XSIGNAL_INLINE_THRESHOLD:
        return 1
    return max(1, (count + XSIGNAL_PAGE_SIZE - 1) // XSIGNAL_PAGE_SIZE)


def build_xsignal_pagination_keyboard(address: str, social_evidence: dict | None, page: int = 1) -> dict | None:
    pages = xsignal_page_count(social_evidence)
    if pages <= 1 or not address:
        return None
    key = xsignal_cache_key(address)
    _address_map[key] = address.lower()
    prev_page = max(1, page - 1)
    next_page = min(pages, page + 1)
    return {
        "inline_keyboard": [
            [
                {"text": "← Prev", "callback_data": f"xpg:{key}:{prev_page}"},
                {"text": f"Page {page}/{pages}", "callback_data": f"xpg:{key}:{page}"},
                {"text": "Next →", "callback_data": f"xpg:{key}:{next_page}"},
            ]
        ]
    }


def strip_xsignal_keyboard(keyboard: dict | None) -> dict | None:
    rows = []
    for row in (keyboard or {}).get("inline_keyboard") or []:
        kept = [
            button for button in row
            if not str(button.get("callback_data") or "").startswith("xpg:")
        ]
        if kept:
            rows.append(kept)
    return {"inline_keyboard": rows} if rows else None


def merge_inline_keyboards(*keyboards: dict | None) -> dict | None:
    rows: list[list[dict]] = []
    for keyboard in keyboards:
        if keyboard and keyboard.get("inline_keyboard"):
            rows.extend(keyboard["inline_keyboard"])
    return {"inline_keyboard": rows} if rows else None


def replace_xsignal_block(message_text: str, new_block: str) -> str:
    text = str(message_text or "")
    match = re.search(r"🐦\s*<b>X signal</b>", text, flags=re.IGNORECASE)
    if not match:
        return (text.rstrip() + "\n\n" + new_block).strip()
    return (text[:match.start()].rstrip() + "\n\n" + new_block).strip()


async def load_xsignal_evidence_for_ca(address: str) -> dict | None:
    key = xsignal_cache_key(address)
    if key in _xsignal_page_cache:
        return _xsignal_page_cache[key]

    async with db_session() as db:
        launch = await get_launch(db, address)
        if launch:
            raw = launch.raw_json or {}
            social_evidence = ((raw.get("social_confirmation") or {}).get("social_evidence") or {})
            if social_evidence:
                remember_xsignal_evidence(address, social_evidence)
                return social_evidence
        research = await get_latest_token_research(db, address)
        processed = (research.processed_data if research else {}) or {}

    social = processed.get("social") or {}
    tweets = social.get("evidence_tweets") or []
    if not tweets:
        return None
    social_evidence = {
        "ticker": processed.get("symbol") or social.get("ticker") or "",
        "qualified_tweets": int(social.get("qualified_tweets") or len(tweets)),
        "max_age_hours": 24,
        "thesis": social.get("evidence_thesis") or "",
        "value_assessment": social.get("value_assessment") or "",
        "social_score": int(social.get("social_score") or 0),
        "score_breakdown": social.get("score_breakdown") or {},
        "top_tweets": tweets,
    }
    remember_xsignal_evidence(address, social_evidence)
    return social_evidence


def format_research_social_block(
    ticker: str,
    mentions: list[dict],
    influencer_mentions: list[dict],
    *,
    social_evidence: dict | None = None,
    nitter_mentions: list[dict] | None = None,
    address: str = "",
    page: int = 1,
) -> str:
    evidence_tweets = (social_evidence or {}).get("top_tweets") or []
    if evidence_tweets:
        remember_xsignal_evidence(address, social_evidence)
        thesis = hide_contract_mentions((social_evidence or {}).get("thesis") or "", address)
        lines = ["🐦 <b>X signal</b>"]
        if thesis:
            lines.append(f"\n<b>Thesis:</b> {h(thesis[:520])}")
        visible_tweets = xsignal_visible_tweets(social_evidence)
        total_visible = len(visible_tweets)
        page_count = xsignal_page_count(social_evidence)
        page = max(1, min(int(page or 1), page_count))
        if total_visible > XSIGNAL_INLINE_THRESHOLD:
            start = (page - 1) * XSIGNAL_PAGE_SIZE
            page_tweets = visible_tweets[start:start + XSIGNAL_PAGE_SIZE]
        else:
            page_tweets = visible_tweets[:XSIGNAL_INLINE_THRESHOLD]
        for item in page_tweets:
            ref = int(item.get("ref") or 0)
            url = item.get("url") or ""
            author = h(item.get("username", "unknown"))
            likes = fmt_compact_number(int(item.get("likes") or 0))
            views = fmt_compact_number(int(item.get("views") or 0))
            retweets = fmt_compact_number(int(item.get("retweets") or 0))
            importance = h(item.get("importance") or item.get("reason") or "qualified mention")
            excerpt = h(strip_non_english_content(hide_contract_mentions(item.get("excerpt") or "", address))[:220])
            if not excerpt:
                continue
            header = f"[{ref}] <a href='{h(url)}'>@{author}</a>" if url else f"[{ref}] @{author}"
            lines.append(
                f"\n{header} · 👁 {views} · ❤️ {likes} · 🔄 {retweets}\n"
                f"{importance}\n"
                f"“{excerpt}”"
            )
        return "\n".join(lines)

    combined: list[dict] = []
    seen: set[str] = set()
    for item in influencer_mentions + mentions + (nitter_mentions or []):
        if item.get("url") in seen:
            continue
        combined.append(item)
        seen.add(item.get("url", ""))
    if not combined:
        return (
            f"🐦 <b>X signal</b>\n"
            f"• No CA-verified qualified tweets for ${h(ticker)} "
            f"(min {RESEARCH_MIN_QUALIFIED_TWEETS}, {RESEARCH_MIN_TWEET_VIEWS}+ views, "
            f"{RESEARCH_MIN_TWEET_LIKES}+ likes)."
        )
    lines = ["🐦 <b>X signal</b>"]
    for item in combined[:4]:
        tier = int(item.get("tier") or 3)
        marker = "🟢🟢" if tier == 1 else "🟢" if tier == 2 else "🟡"
        hp = " ★" if item.get("high_priority") else ""
        author = h(item.get("username", "unknown"))
        likes = fmt_compact_number(int(item.get("likes") or 0))
        views = fmt_compact_number(int(item.get("views") or 0))
        retweets = fmt_compact_number(int(item.get("retweets") or 0))
        clean_text = strip_non_english_content(hide_contract_mentions(research_clean_text(item.get("text", "")), address))
        if not is_likely_english_text(clean_text):
            continue
        text = h(clean_text[:300])
        lines.append(
            f"• {marker} <a href='{item.get('url')}'>@{author}</a>{hp} · "
            f"❤️ {likes} · 👁 {views} · 🔄 {retweets}\n"
            f"  <i>{text}</i>"
        )
    return "\n".join(lines) if len(lines) > 1 else (
        f"🐦 <b>X signal</b>\n"
        f"• No English qualified tweets for ${h(ticker)} after filtering."
    )


def format_research_card(
    *,
    token_name: str,
    ticker: str,
    address: str,
    dex: dict | None,
    deployer_info: dict | None,
    x_mentions: list[dict],
    influencer_mentions: list[dict],
    nitter_mentions: list[dict] | None = None,
    social_evidence: dict | None = None,
    project_narrative: dict | None = None,
    launch_status: str | None = None,
    title: str = "Research",
) -> str:
    safe_name = h(token_name or ticker or address[:10])
    safe_ticker = h(str(ticker or "").lstrip("$"))
    source = title.upper()
    age = fmt_token_age(dex) if dex else "n/a"
    mcap = float((dex or {}).get("mcap") or 0)
    volume = float((dex or {}).get("volume_24h") or 0)
    liquidity = float((dex or {}).get("liquidity") or 0)
    change_1h = float((dex or {}).get("price_change_1h") or 0)
    one_h_emoji = "🟢" if change_1h >= 0 else "🔴"
    links: list[str] = []
    if address:
        links = [
            f"<a href='https://dexscreener.com/base/{address}'>DexScreener</a>",
            f"<a href='https://gmgn.ai/base/token/{address}'>GMGN</a>",
            f"<a href='{build_x_research_url(address, ticker)}'>Tweet</a>",
            f"<a href='https://app.uniswap.org/swap?chain=base&amp;outputCurrency={address}'>Uniswap</a>",
        ]
    status_line = ""
    if launch_status == "signaled":
        status_line = "\n📡 Already surfaced by scanner"
    elif launch_status:
        status_line = f"\n🗂 Scanner status: <code>{h(launch_status)}</code>"

    body = (
        f"🔍 <b>{h(title)}</b> 🧪 <b>{h(source)}</b>\n\n"
        f"<b>{safe_name}</b> - ${safe_ticker}\n\n"
        f"🕐 <b>Launched:</b> {h(age + ' ago' if age != 'n/a' else 'n/a')}\n"
        f"•💰 <b>Market Cap.:</b> {fmt_usd(mcap)}\n"
        f"•📈 <b>Volume:</b> {fmt_usd(volume)}\n"
        f"•💧 <b>Liquidity:</b> {fmt_usd(liquidity)}\n"
        f"•{one_h_emoji} <b>1h:</b> {change_1h:+.1f}%{status_line}\n\n"
        + (f"🔗 " + " · ".join(links) + "\n\n" if links else "")
        + format_research_ai_brief(
            token_name=token_name,
            ticker=ticker,
            dex=dex,
            deployer_info=deployer_info,
            x_mentions=x_mentions,
            influencer_mentions=influencer_mentions,
            social_evidence=social_evidence,
            project_narrative=project_narrative,
            launch_status=launch_status,
        )
        + "\n\n"
        + format_research_social_block(
            ticker,
            x_mentions,
            influencer_mentions,
            nitter_mentions=nitter_mentions,
            social_evidence=social_evidence,
            address=address,
        )
    )
    return body[:3900]


SOURCE_EMOJIS = {"bankr": "🤖", "clanker": "⚙️", "virtuals": "🤖", "dexscreener": "📊", "coingecko": "🦎"}


def format_signal_telegram(launch: dict, dex: dict | None) -> str:
    source_key = launch["source"]
    source = h(source_key.upper())
    name = h(launch["name"])
    symbol = h(str(launch["symbol"]).lstrip("$"))
    address = launch["address"]
    x_username = clean_x_handle(launch.get("x_username", ""))
    tweet_url = h(launch.get("tweet_url", ""))
    source_emoji = SOURCE_EMOJIS.get(source_key, "📡")

    age = "n/a"
    mcap = volume = liquidity = 0.0
    change_1h = 0.0
    if dex:
        mcap = float(dex.get("mcap") or 0)
        volume = float(dex.get("volume_24h") or 0)
        liquidity = float(dex.get("liquidity") or 0)
        change_1h = float(dex.get("price_change_1h") or 0)
        age = fmt_token_age(dex)

    launch_title = f"<b>{name}</b> - ${symbol}"
    if x_username:
        launch_title += f" · <a href='https://x.com/{x_username}'>@{x_username}</a>"

    source_lines: list[str] = []
    if source_key == "virtuals":
        creator_x = clean_x_handle(launch.get("creator_x", ""))
        if creator_x and creator_x != x_username:
            source_lines.append(f"Creator <a href='https://x.com/{creator_x}'>@{creator_x}</a>")
        holders = launch.get("holder_count", 0)
        if holders:
            source_lines.append(f"Holders {holders:,}")

    if source_key in {"dexscreener", "coingecko"}:
        dex_id = launch.get("dex_id", "")
        source_lines.append(f"Via {h(dex_id.title() if dex_id else 'Unknown DEX')}")

    dexscreener_url = f"https://dexscreener.com/base/{address}"
    if dex and dex.get("_source") == "dexscreener" and dex.get("pair_url"):
        dexscreener_url = h(dex.get("pair_url"))
    links = [
        f"<a href='{dexscreener_url}'>DexScreener</a>",
        f"<a href='https://gmgn.ai/base/token/{address}'>GMGN</a>",
    ]
    if tweet_url:
        links.append(f"📝 <a href='{tweet_url}'>Tweet</a>")
    links.append(f"<a href='https://app.uniswap.org/swap?chain=base&amp;outputCurrency={address}'>Uniswap</a>")

    liq_lock = " 🔓" if source_key in SAFE_LAUNCHPADS else ""
    one_h_emoji = "🟢" if change_1h >= 0 else "🔴"
    source_note = (" · ".join(source_lines[:2]) + "\n") if source_lines else ""

    return (
        f"🚨 <b>New Launch</b> {source_emoji} <b>{source}</b>\n\n"
        f"{launch_title}\n"
        f"{source_note}\n"
        f"🕐 <b>Launched:</b> {h(age + ' ago' if age != 'n/a' else 'n/a')}\n"
        f"•💰 <b>Market Cap.:</b> {fmt_usd(mcap)}\n"
        f"•📈 <b>Volume:</b> {fmt_usd(volume)}\n"
        f"•💧 <b>Liquidity:</b> {fmt_usd(liquidity)}{liq_lock}\n"
        f"•{one_h_emoji} <b>1h:</b> {change_1h:+.1f}%\n\n"
        f"🔗 " + " · ".join(links) + "\n\n"
        f"{build_ai_summary_placeholder(launch, dex)}"
    )


def format_alert_whatsapp(launch: dict, dex: dict | None) -> str:
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


SEED_SUPPRESS_MIN_AGE_SECONDS = int(os.getenv("SEED_SUPPRESS_MIN_AGE_SECONDS", "1800"))


def launch_age_seconds(launch: dict) -> float | None:
    dt = parse_launch_datetime(launch)
    if dt:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (utc_now() - dt).total_seconds())
    pair_created = (launch.get("_dex") or {}).get("pair_created_at") or 0
    try:
        value = float(pair_created or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 10_000_000_000:
        value = value / 1000
    return max(0.0, time.time() - value)


def should_seed_suppress_launch(launch: dict) -> bool:
    age = launch_age_seconds(launch)
    return age is not None and age >= SEED_SUPPRESS_MIN_AGE_SECONDS


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


def build_watchlist_change_message(item, market: dict, launch) -> str | None:
    mcap = float(market.get("mcap") or 0)
    volume = float(market.get("volume_24h") or 0)
    liquidity = float(market.get("liquidity") or 0)
    mcap_change = pct_change(item.last_mcap, mcap)
    volume_change = pct_change(item.last_volume, volume)
    triggers: list[str] = []
    if mcap_change is not None and abs(mcap_change) >= WATCHLIST_NOTIFY_MCAP_CHANGE_PCT:
        triggers.append(f"MC {format_pct(mcap_change)}")
    if volume_change is not None and abs(volume_change) >= WATCHLIST_NOTIFY_VOLUME_CHANGE_PCT:
        triggers.append(f"Vol {format_pct(volume_change)}")
    if not triggers:
        return None
    symbol, name = watch_symbol_name(item, launch)
    title_name = f" · {h(name[:42])}" if name and name != symbol else ""
    label = f" · {h(item.label)}" if item.label else ""
    since_mcap = pct_change(item.initial_mcap, mcap)
    since_volume = pct_change(item.initial_volume, volume)
    since_parts = []
    if since_mcap is not None:
        since_parts.append(f"add MC {format_pct(since_mcap)}")
    if since_volume is not None:
        since_parts.append(f"add Vol {format_pct(since_volume)}")
    since_line = f"\nSince add: {' · '.join(since_parts[:2])}" if since_parts else ""
    return (
        f"⭐ <b>Watchlist move</b>\n\n"
        f"<b>${h(symbol)}</b>{title_name}{label}\n"
        f"Move: <b>{h(' · '.join(triggers))}</b>{since_line}\n"
        f"MC <b>{fmt_usd(mcap)}</b> · Vol {fmt_usd(volume)} · Liq {fmt_usd(liquidity)}\n"
        f"Added {time_ago(item.created_at)} · checked now"
    )


async def process_watchlist_checks(session: aiohttp.ClientSession) -> int:
    async with db_session() as db:
        due = await get_due_watchlist_items(
            db,
            now=utc_now(),
            limit=WATCHLIST_CHECK_BATCH,
            min_interval_seconds=WATCHLIST_CHECK_INTERVAL,
        )

    notified_count = 0
    for item in due:
        market = await fetch_geckoterminal(session, item.ca)
        async with db_session() as db:
            launch = await get_launch(db, item.ca)
            tenant = await get_tenant(db, tenant_id=item.tenant_id)
        message = build_watchlist_change_message(item, market or {}, launch) if market and tenant else None
        async with db_session() as db:
            await mark_watchlist_checked(db, watchlist_id=item.id, market_json=market, notified=bool(message))
        if not message or not tenant:
            continue
        sent = await send_telegram(
            session,
            message,
            chat_id=tenant.external_id,
            reply_markup=build_watch_item_keyboard(item, launch),
        )
        if sent is not None:
            notified_count += 1
    return notified_count


def parse_hex_int(value) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, str) and value.startswith("0x"):
            return int(value, 16)
        return int(value)
    except (TypeError, ValueError):
        return None


async def fetch_latest_base_block(session: aiohttp.ClientSession) -> int | None:
    if not ALCHEMY_RPC_URL:
        return None
    try:
        async with session.post(
            ALCHEMY_RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            data = await resp.json()
            return parse_hex_int(data.get("result"))
    except Exception as e:
        log.debug(f"Base block lookup failed: {e}")
        return None


async def fetch_wallet_asset_transfers(
    session: aiohttp.ClientSession,
    *,
    wallet_address: str,
    from_block: int,
    to_block: int,
) -> list[dict]:
    if not ALCHEMY_RPC_URL:
        return []
    params_base = {
        "fromBlock": hex(max(0, from_block)),
        "toBlock": hex(max(from_block, to_block)),
        "category": ["erc20"],
        "withMetadata": True,
        "excludeZeroValue": True,
        "maxCount": "0x64",
    }
    transfers: list[dict] = []
    for key in ("fromAddress", "toAddress"):
        params = {**params_base, key: wallet_address}
        try:
            async with session.post(
                ALCHEMY_RPC_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers", "params": [params]},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                data = await resp.json()
                if data.get("error"):
                    log.warning(f"Alchemy transfer lookup error for {wallet_address[:10]}: {data['error']}")
                    return []
                batch = (data.get("result") or {}).get("transfers") or []
                transfers.extend(batch)
        except Exception as e:
            log.warning(f"Wallet transfer lookup failed for {wallet_address[:10]}: {e}")
            return transfers
    by_key: dict[tuple[str, str, str], dict] = {}
    for transfer in transfers:
        contract = ((transfer.get("rawContract") or {}).get("address") or transfer.get("asset") or "").lower()
        tx_hash = (transfer.get("hash") or "").lower()
        direction = "in" if (transfer.get("to") or "").lower() == wallet_address.lower() else "out"
        by_key[(tx_hash, contract, direction)] = transfer
    return list(by_key.values())


def normalize_wallet_transfer(wallet_address: str, transfer: dict) -> dict | None:
    raw_contract = transfer.get("rawContract") or {}
    ca = (raw_contract.get("address") or "").lower()
    if not is_base_contract(ca):
        return None
    tx_hash = (transfer.get("hash") or "").lower()
    if not tx_hash:
        return None
    direction = "in" if (transfer.get("to") or "").lower() == wallet_address.lower() else "out"
    return {
        "ca": ca,
        "direction": direction,
        "tx_hash": tx_hash,
        "block_number": parse_hex_int(transfer.get("blockNum")),
        "amount": to_float(transfer.get("value"), 0.0),
        "event_json": transfer,
    }


async def process_tracked_wallet_checks(session: aiohttp.ClientSession) -> int:
    if not WALLET_MONITOR_ENABLED:
        return 0
    latest_block = await fetch_latest_base_block(session)
    if latest_block is None:
        return 0
    async with db_session() as db:
        wallets = await get_due_tracked_wallets(
            db,
            now=utc_now(),
            limit=WALLET_POLL_BATCH,
            min_interval_seconds=WALLET_POLL_INTERVAL,
        )

    inserted_count = 0
    for wallet in wallets:
        from_block = int(wallet.last_checked_block or max(0, latest_block - WALLET_LOOKBACK_BLOCKS))
        transfers = await fetch_wallet_asset_transfers(
            session,
            wallet_address=wallet.address,
            from_block=from_block + 1 if wallet.last_checked_block else from_block,
            to_block=latest_block,
        )
        async with db_session() as db:
            tenant = await get_tenant(db, tenant_id=wallet.tenant_id)
            max_block = wallet.last_checked_block or from_block
            alerts: list[str] = []
            for transfer in transfers:
                event = normalize_wallet_transfer(wallet.address, transfer)
                if not event:
                    continue
                max_block = max(max_block or 0, int(event.get("block_number") or 0))
                row, inserted = await upsert_wallet_event(
                    db,
                    tracked_wallet_id=wallet.id,
                    tenant_id=wallet.tenant_id,
                    wallet_address=wallet.address,
                    ca=event["ca"],
                    direction=event["direction"],
                    tx_hash=event["tx_hash"],
                    block_number=event.get("block_number"),
                    amount=event.get("amount"),
                    event_json=event.get("event_json") or {},
                )
                if not inserted:
                    continue
                inserted_count += 1
                status = await get_launch_status(db, event["ca"])
                if tenant and event["direction"] == "in" and status in {"signaled", "queued_recheck", "manual_research"}:
                    alerts.append(
                        f"🐋 <b>Smart wallet inflow</b>\n"
                        f"{h(wallet.label or wallet.address[:10])} received token we track\n"
                        f"<code>{event['ca']}</code>\n"
                        f"Tx: <a href='https://basescan.org/tx/{event['tx_hash']}'>BaseScan</a>\n"
                        f"/research {event['ca']}"
                    )
            await mark_tracked_wallet_checked(db, wallet_id=wallet.id, block_number=max_block or latest_block)
        if tenant:
            for alert in alerts[:3]:
                await send_telegram(session, alert, chat_id=tenant.external_id)
    return inserted_count


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
        existing_raw = dict(existing.raw_json or launch)
        existing_dex = dex or existing.market_json
        enriched_raw, reason = await enrich_launch_social_confirmation(session, existing_raw, ca)
        if enriched_raw != existing_raw:
            async with db_session() as db:
                await mark_launch_status(
                    db,
                    ca=ca,
                    status=existing.status,
                    reason=reason,
                    market_json=existing_dex,
                    raw_json=enriched_raw,
                )
        return enriched_raw, existing_dex
    await persist_launch_seen(launch, status="manual_research")
    launch, reason = await enrich_launch_social_confirmation(session, launch, ca)
    if dex:
        async with db_session() as db:
            await mark_launch_status(
                db,
                ca=ca,
                status="manual_research",
                reason=reason or "manual analysis",
                market_json=dex,
                raw_json=launch,
            )
    return launch, dex


async def enrich_launch_social_confirmation(
    session: aiohttp.ClientSession,
    launch: dict,
    ca: str,
) -> tuple[dict, str]:
    """Attach CA-only social evidence for manual verdict commands without blocking the command."""
    if launch.get("social_confirmation"):
        return launch, "social confirmation already captured"
    ticker = str(launch.get("symbol") or "").lstrip("$")
    if not ticker or not is_base_contract(ca):
        return launch, "manual analysis"
    try:
        social_ok, social_reason, social_evidence = await validate_ca_social_confirmation(
            session,
            ticker=ticker,
            address=ca,
        )
    except Exception as e:
        log.warning(f"Manual social enrichment failed for {ca}: {e}")
        return launch, "manual social enrichment failed"
    enriched = dict(launch)
    enriched["social_confirmation"] = {
        **(social_evidence or {}),
        "verified": bool(social_ok and (social_evidence or {}).get("verified")),
    }
    return enriched, social_reason


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
    score = float(verdict.get("score") or 0) / 10
    lines = [
        f"🤖 <b>Verdict 2.0</b> · <b>{h(verdict.get('label') or '')}</b> · {score:.1f}/10",
        f"${h(launch.get('symbol') or '')} · {h(launch.get('source') or source_info.get('source') or 'unknown')}",
        f"<code>{h(ca)}</code>",
        h(command_market_line(result)),
        "",
        human,
    ]
    if summary:
        lines.extend(["", f"📝 <b>Summary</b>\n{h(summary.get('summary_text', ''))}"])
    return "\n".join(lines)[:3900]


def format_spoof_report(result: dict) -> str:
    launch = result.get("launch") or {}
    signals = result.get("spoof_signals") or []
    verdict = result.get("verdict") or {}
    lines = [
        f"🕵️ <b>Spoof Check</b> · ${h(launch.get('symbol') or '')}",
        f"<code>{h(launch.get('ca') or '')}</code>",
        f"<b>{h(verdict.get('label') or '')}</b> · {float(verdict.get('score') or 0) / 10:.1f}/10",
        h(command_market_line(result)),
        "",
    ]
    if not signals:
        lines.append("No deterministic spoof signals found.")
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
        f"🧠 <b>Summary</b> · ${h(launch.get('symbol') or '')}\n"
        f"<code>{h(launch.get('ca') or '')}</code>\n\n"
        f"{h(command_market_line(result))}\n\n"
        f"{h(summary.get('summary_text') or 'Summary unavailable')}\n\n"
        f"<b>{h(verdict.get('label'))}</b> · {float(verdict.get('score') or 0) / 10:.1f}/10"
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

    tg_text = format_signal_telegram(launch, dex)
    wa_text = format_alert_whatsapp(launch, dex)
    keyboard = build_signal_keyboard(address, symbol)
    delivery_payload = {
        "telegram_text": tg_text,
        "reply_markup": keyboard,
        "ca": address,
        "symbol": symbol,
        "source": source,
    }

    async with db_session() as db:
        signal, delivery_inserted = await prepare_signal_fanout(
            db,
            ca=address,
            source=source,
            payload_json=delivery_payload,
            page_size=1000,
        )
        if delivery_inserted == 0:
            log.info(f"  📡 [{source}] ${symbol} already delivered or no active tenants, skipping")
            await mark_launch_status(
                db,
                ca=address,
                status="signaled",
                reason="already delivered or no tenants",
                market_json=dex,
                raw_json=launch,
            )
            return False
        pending_deliveries = await get_pending_deliveries_for_signal(
            db,
            signal_id=signal.id,
            limit=TELEGRAM_SIGNAL_DELIVERY_LIMIT,
        )

    log.info(
        f"  📡 {prefix}SIGNAL: [{source}] ${symbol} → {len(pending_deliveries)} Telegram deliveries "
        f"MCap {fmt_usd(dex['mcap'])} Vol {fmt_usd(dex['volume_24h'])}"
        + (f" @{launch.get('x_username')}" if launch.get('x_username') else "")
    )

    successful_messages: list[tuple[str, int]] = []
    for delivery in pending_deliveries:
        async with db_session() as db:
            await mark_delivery_sending(db, delivery_id=delivery.id)

        message_id = await send_telegram(session, tg_text, chat_id=delivery.destination_id, reply_markup=keyboard)
        async with db_session() as db:
            if message_id is not None:
                await mark_delivery_sent(db, delivery_id=delivery.id, message_id=str(message_id))
                successful_messages.append((delivery.destination_id, message_id))
                log_event(
                    "signal_delivery_sent",
                    correlation_id=cid,
                    ca=address,
                    source=source,
                    chat_id=delivery.destination_id,
                    message_id=message_id,
                )
            else:
                await mark_delivery_retry(
                    db,
                    delivery_id=delivery.id,
                    error="telegram send failed",
                    next_retry_at=utc_now() + timedelta(minutes=1),
                )

    if not successful_messages:
        log.error(f"  ❌ Telegram signal failed for all tenants: [{source}] ${symbol} {address}")
        return False

    async with db_session() as db:
        await mark_launch_status(db, ca=address, status="signaled", reason="telegram delivered", market_json=dex, raw_json=launch)
        log_event(
            "signal_sent",
            correlation_id=cid,
            ca=address,
            source=source,
            delivered=len(successful_messages),
            queued=len(pending_deliveries),
        )

    if WHAPI_TOKEN and WHATSAPP_GROUP_ID:
        await send_whatsapp(session, wa_text)

    dex_url = f"https://dexscreener.com/base/{address}"
    pushover_msg = f"${symbol} · MCap {fmt_usd(dex['mcap'])} · Vol {fmt_usd(dex['volume_24h'])}"
    if launch.get("x_username"):
        pushover_msg += f" · @{launch['x_username']}"
    await send_pushover(session, f"🐋 {source.upper()}: ${symbol}", pushover_msg, url=dex_url)

    alert_count += len(successful_messages)

    if AUTO_VERDICT_ENABLED:
        asyncio.create_task(
            attach_signal_verdict_to_messages(session, successful_messages, tg_text, keyboard, launch, dex, source, symbol)
        )

    return True


async def build_signal_verdict_text(
    base_text: str,
    launch: dict,
    dex: dict | None,
    source: str,
    symbol: str,
) -> str | None:
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
        return None

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
    processed = (result.get("research") or {}).get("processed_data") or {}
    social = processed.get("social") or {}
    evidence_tweets = social.get("evidence_tweets") or []
    if evidence_tweets:
        social_evidence = {
            "ticker": processed.get("symbol") or symbol,
            "qualified_tweets": int(social.get("qualified_tweets") or len(evidence_tweets)),
            "max_age_hours": 24,
            "thesis": social.get("evidence_thesis") or "",
            "value_assessment": social.get("value_assessment") or "",
            "social_score": int(social.get("social_score") or 0),
            "score_breakdown": social.get("score_breakdown") or {},
            "top_tweets": evidence_tweets,
        }
        xsignal_block = format_research_social_block(
            symbol,
            [],
            [],
            social_evidence=social_evidence,
            address=address,
            page=1,
        )
        if "🐦 <b>X Signal</b>" not in new_text:
            new_text = f"{new_text}\n\n{xsignal_block}"
    if len(new_text) > 3900:
        new_text = new_text[:3800] + "\n\n<i>Verdict 2.0 truncated</i>"
    log.info(
        f"  🧠 Verdict 2.0: [{source}] ${symbol} → "
        f"{verdict.get('label')} ({float(verdict.get('score') or 0) / 10:.1f}/10)"
    )
    return new_text


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
    new_text = await build_signal_verdict_text(base_text, launch, dex, source, symbol)
    if not new_text:
        return
    address = launch.get("address", "")
    social_evidence = _xsignal_page_cache.get(xsignal_cache_key(address))
    merged_keyboard = merge_inline_keyboards(
        keyboard,
        build_xsignal_pagination_keyboard(address, social_evidence, 1),
    )
    ok = await edit_telegram_message(session, chat_id, message_id, new_text, reply_markup=merged_keyboard)
    if ok:
        log.info(f"  🧠 Verdict 2.0 edit attached: [{source}] ${symbol} → {chat_id}/{message_id}")
    else:
        log.warning(f"  ⚠️ Verdict 2.0 edit rejected: [{source}] ${symbol}")


async def attach_signal_verdict_to_messages(
    session: aiohttp.ClientSession,
    messages: list[tuple[str, int]],
    base_text: str,
    keyboard: dict,
    launch: dict,
    dex: dict | None,
    source: str,
    symbol: str,
):
    new_text = await build_signal_verdict_text(base_text, launch, dex, source, symbol)
    if not new_text:
        return
    address = launch.get("address", "")
    social_evidence = _xsignal_page_cache.get(xsignal_cache_key(address))
    merged_keyboard = merge_inline_keyboards(
        keyboard,
        build_xsignal_pagination_keyboard(address, social_evidence, 1),
    )
    edited = 0
    for chat_id, message_id in messages:
        if await edit_telegram_message(session, chat_id, message_id, new_text, reply_markup=merged_keyboard):
            edited += 1
    log.info(f"  🧠 Verdict 2.0 edited {edited}/{len(messages)} Telegram deliveries for [{source}] ${symbol}")


# ─── Seeding ──────────────────────────────────────────────────────────────────

async def seed_existing(session: aiohttp.ClientSession):
    log.info("📋 Seeding existing tokens...")
    bankr = await fetch_bankr(session)
    clanker = await fetch_clanker(session)
    dexscreener = await fetch_dexscreener_discoveries(session)
    coingecko = await fetch_coingecko_new_pools(session)
    virtuals = await fetch_virtuals(session)

    all_launches = bankr + clanker + dexscreener + coingecko + virtuals
    inserted = 0
    fresh_skipped = 0

    for launch in all_launches:
        if not should_seed_suppress_launch(launch):
            fresh_skipped += 1
            continue
        was_inserted, addr = await persist_launch_seen(launch, status="seeded")
        inserted += int(was_inserted)
        if was_inserted:
            log_event("launch_seeded", correlation_id=correlation_id(launch.get("source", "?"), addr), ca=addr, source=launch.get("source", "?"))

    log.info(
        f"📋 Seeded {inserted} new DB rows "
        f"(fresh/unknown left eligible: {fresh_skipped}) "
        f"(Bankr: {len(bankr)}, Clanker: {len(clanker)}, DexScreener: {len(dexscreener)}, "
        f"CoinGecko: {len(coingecko)}, Virtuals: {len(virtuals)}) "
        f"— existing rows skipped"
    )


# ─── Main Loop ────────────────────────────────────────────────────────────────

async def main():
    global alert_count, default_tenant_db_id

    if not TELEGRAM_BOT_TOKEN:
        log.error("❌ TELEGRAM_BOT_TOKEN not set!")
    if not TELEGRAM_CHAT_ID:
        log.warning("⚠️ TELEGRAM_CHAT_ID not set — no default group tenant; public /start DMs still work")
    if not SOCIALDATA_API_KEY:
        log.error("❌ SOCIALDATA_API_KEY not set!")

    log.info("=" * 60)
    log.info("  🐋 Whale Alert Bot (Bankr + Clanker + Virtuals + DexScreener + CoinGecko)")
    log.info(f"  Min followers : {MIN_FOLLOWERS:,}")
    log.info(f"  Min MCap      : ${MIN_MCAP:,}")
    log.info(f"  Min Volume 24h: ${MIN_VOLUME_24H:,}")
    log.info(f"  Min Liquidity : ${MIN_LIQUIDITY:,} (DexScreener-sourced only)")
    log.info(f"  Safe sources  : {', '.join(SAFE_LAUNCHPADS)} (liq check SKIPPED)")
    log.info(f"  Poll interval : {POLL_INTERVAL}s")
    log.info(f"  Telegram      : {'✅' if TELEGRAM_BOT_TOKEN else '❌'}" + (f" (default {TELEGRAM_CHAT_ID})" if TELEGRAM_CHAT_ID else " (self-serve DMs)"))
    log.info(f"  Authorized DMs: {', '.join(sorted(AUTHORIZED_USER_IDS)) if AUTHORIZED_USER_IDS else 'none'}")
    log.info(f"  WhatsApp      : {'✅' if WHAPI_TOKEN and WHATSAPP_GROUP_ID else '❌'}")
    log.info(f"  SocialData    : {'✅' if SOCIALDATA_API_KEY else '❌'}")
    log.info(
        f"  CoinGecko     : "
        f"{'✅ ON' if COINGECKO_DISCOVERY_ENABLED and COINGECKO_API_KEY else '❌ OFF'} "
        f"(new_pools every {COINGECKO_POLL_INTERVAL}s, limit {COINGECKO_DISCOVERY_LIMIT})"
    )
    log.info(f"  Auto-verdict  : {'✅ ON' if AUTO_VERDICT_ENABLED else '❌ OFF'} ({AUTO_VERDICT_TIMEOUT_SEC:.0f}s, max {AUTO_VERDICT_MAX_CONCURRENT})")
    log.info(f"  Pushover      : {'✅' if PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN else '❌ NOT SET'}")
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
        asyncio.create_task(telegram_command_loop(session))
        asyncio.create_task(nitter_health_loop(session))
        log.info(
            "✅ Telegram command loop started "
            f"(interval={TELEGRAM_COMMAND_POLL_INTERVAL}s, timeout={TELEGRAM_GET_UPDATES_TIMEOUT}s)"
        )
        log.info(
            "✅ Nitter health loop configured "
            f"(enabled={NITTER_HEALTH_ENABLED}, interval={NITTER_HEALTH_INTERVAL_SEC}s)"
        )

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

        pushover_note = "\n🔔 Pushover: ✅ Emergency alerts ON" if PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN else "\n🔔 Pushover: ❌ OFF"
        verdict_note = (
            f"\n🧠 Auto-verdict: ✅ ON — deterministic research, AI stub"
            if AUTO_VERDICT_ENABLED else "\n🧠 Auto-verdict: ❌ OFF"
        )
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            await send_telegram(
                session,
                f"🐋 <b>Whale Alert Bot started</b>\n\n"
                f"Sources: Bankr + Clanker + Virtuals + DexScreener + CoinGecko\n"
                f"Market data: DexScreener + GeckoTerminal fallback\n"
                f"Min MCap: ${MIN_MCAP:,} · Vol: ${MIN_VOLUME_24H:,}\n"
                f"Liq: ${MIN_LIQUIDITY:,} (DexScreener only — 🔓 skipped for Bankr/Clanker/Virtuals)\n"
                f"Polling every {POLL_INTERVAL}s"
                f"{pushover_note}"
                f"{verdict_note}\n\n"
                f"Commands: /start · /help · /research · /status",
            )

        while True:
            try:
                retried_deliveries = await process_delivery_retries(session)
                if retried_deliveries:
                    log.info(f"📨 Retried {retried_deliveries} Telegram deliveries")
                watchlist_notifications = await process_watchlist_checks(session)
                if watchlist_notifications:
                    log.info(f"⭐ Sent {watchlist_notifications} watchlist update(s)")
                wallet_events = await process_tracked_wallet_checks(session)
                if wallet_events:
                    log.info(f"🐋 Stored {wallet_events} tracked wallet event(s)")

                bankr_launches = await fetch_bankr(session)
                clanker_launches = await fetch_clanker(session)
                dexscreener_launches = await fetch_dexscreener_discoveries(session)
                coingecko_launches = await fetch_coingecko_new_pools(session)
                virtuals_launches = await fetch_virtuals(session)
                all_launches = bankr_launches + clanker_launches + dexscreener_launches + coingecko_launches + virtuals_launches

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

                    if source in {"dexscreener", "coingecko"} and launch.get("_dex"):
                        dex = launch["_dex"]
                    else:
                        dex = await fetch_geckoterminal(session, address)
                    if source in {"dexscreener", "coingecko"} and dex:
                        launch["name"] = dex.get("token_name") or launch.get("name") or "Unknown"
                        launch["symbol"] = dex.get("token_symbol") or launch.get("symbol") or "?"
                        launch["dex_id"] = dex.get("dex_id") or launch.get("dex_id", "")
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

                    social_ok, social_reason, social_evidence = await validate_ca_social_confirmation(
                        session,
                        ticker=symbol,
                        address=address,
                    )
                    if not social_ok:
                        async with db_session() as db:
                            await mark_launch_status(
                                db,
                                ca=address,
                                status="filtered",
                                reason=social_reason,
                                market_json=dex,
                            )
                        log.info(f"  🧹 [{source}] ${symbol} — {social_reason}, skip signal")
                        continue
                    launch["social_confirmation"] = social_evidence
                    async with db_session() as db:
                        await mark_launch_status(
                            db,
                            ca=address,
                            status="new",
                            reason=social_reason,
                            market_json=dex,
                            raw_json=launch,
                        )

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

                    social_ok, social_reason, social_evidence = await validate_ca_social_confirmation(
                        session,
                        ticker=symbol,
                        address=addr,
                    )
                    if not social_ok:
                        async with db_session() as db:
                            await mark_launch_status(
                                db,
                                ca=addr,
                                status="filtered",
                                reason=social_reason,
                                market_json=dex,
                            )
                        log.info(f"  🧹 [{source}] ${symbol} recheck — {social_reason}, skip signal")
                        expired_names.append(f"${symbol}[{source}/social]")
                        continue
                    launch["social_confirmation"] = social_evidence
                    async with db_session() as db:
                        await mark_launch_status(
                            db,
                            ca=addr,
                            status="new",
                            reason=social_reason,
                            market_json=dex,
                            raw_json=launch,
                        )

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
