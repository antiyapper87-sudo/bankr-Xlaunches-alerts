# Bankr X Launches Alerts

Monitors early Base token launches from Bankr, Clanker, DexScreener, CoinGecko Onchain and Virtuals.
Any Telegram user can press `/start` in DM to subscribe. Alerts are delivered with market context,
X research links, watched-influencer signals and an optional deterministic auto-verdict block.

## How it works

1. Polls launch sources and rechecks new tokens while market data is still indexing.
2. Enriches launches with DexScreener/GeckoTerminal market data.
3. Filters by market cap, volume, liquidity and source-specific safety rules.
4. Fans out Telegram alerts to active DM/group tenants with X Research, Ticker X, Copy CA and chart/trading links.
5. Optionally attaches deterministic research verdicts in the background.
6. Builds Phase 2 Verdict 2.0 research, spoof checks and AI-summary stubs for Base CAs.
7. Supports Phase 3 retention features: user watchlists, per-user min score and signal feedback.
8. Stores Phase 4 tracked wallets and wallet events for Base smart-money confirmation.

## Setup

### 1. Create Telegram Bot
- Message [@BotFather](https://t.me/BotFather) on Telegram
- Send `/newbot` and follow the prompts
- Copy the bot token

### 2. Subscribe Users
- Send `/start` to the bot in DM
- The bot registers that Telegram chat as an active tenant and sends an English introduction
- `TELEGRAM_CHAT_ID` is optional and is only used as a default group/admin alert destination

### 3. Deploy
- Push this repo to GitHub
- Provision Postgres and Redis
- Add environment variables (see below)
- Run migrations before starting bot/workers

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ❌ | — | Optional default chat/group ID for alerts and admin group commands |
| `AUTHORIZED_USER_IDS` | ❌ | `544999608` | Comma-separated Telegram user IDs allowed to DM commands |
| `SOCIALDATA_API_KEY` | ✅ | — | SocialData key for X follower and tweet research |
| `APP_ENV` | ❌ | `local` | Use `production`/`staging` with Postgres |
| `DATABASE_URL` | Required for service deploy | — | Postgres URL, e.g. `postgresql+asyncpg://...` |
| `LOCAL_DATABASE_URL` | ❌ | `sqlite+aiosqlite:///data/bot.db` | Local SQLite fallback only |
| `DATABASE_AUTO_CREATE` | ❌ | `true` local, `false` prod | Auto-create tables locally; production uses Alembic |
| `REDIS_URL` | Required for workers | `redis://localhost:6379/0` | Redis connection for RQ |
| `RQ_QUEUE_NAME` | ❌ | `launches` | RQ queue name |
| `MIN_FOLLOWERS` | ❌ | `5000` | Minimum deployer follower count for launchpad filters |
| `MIN_MCAP` | ❌ | `50000` | Minimum market cap |
| `MIN_VOLUME_24H` | ❌ | `30000` | Minimum 24h volume |
| `MIN_LIQUIDITY` | ❌ | `30000` | Minimum liquidity for non-safe DEX-sourced launches |
| `POLL_INTERVAL` | ❌ | `30` | Seconds between API polls |
| `DEXSCREENER_DISCOVERY_ENABLED` | ❌ | `true` | Poll DexScreener latest Base profiles/boosts/CTOs as a launch source |
| `DEXSCREENER_DISCOVERY_LIMIT` | ❌ | `40` | Max DexScreener Base discoveries per poll |
| `COINGECKO_API_KEY` | Required if enabled | — | CoinGecko Demo API key for Onchain Base `new_pools` discovery |
| `COINGECKO_DISCOVERY_ENABLED` | ❌ | `false` | Poll CoinGecko Onchain latest Base pools as a DEX discovery source |
| `COINGECKO_DISCOVERY_LIMIT` | ❌ | `25` | Max CoinGecko Base pools normalized per poll |
| `COINGECKO_POLL_INTERVAL` | ❌ | `720` | Seconds between CoinGecko calls; 720s is 5 calls/hour, about 3.6k calls/month |
| `COINGECKO_RATE_LIMIT_PER_MIN` | ❌ | `12` | Local safety cap under the Demo plan limit |
| `AUTO_VERDICT_ENABLED` | ❌ | `false` | Attach deterministic research verdicts to signal messages |
| `AUTO_VERDICT_TIMEOUT_SEC` | ❌ | `12` | Max time spent building one verdict |
| `AUTO_VERDICT_MAX_CONCURRENT` | ❌ | `2` | Max concurrent verdict jobs |
| `SAME_TICKER_EXTERNAL_ENABLED` | ❌ | `true` | Checks GeckoTerminal for older same-ticker Base markets during Verdict 2.0 |
| `SAME_TICKER_PRIOR_LOOKBACK_HOURS` | ❌ | `48` | Lookback for prior same-ticker markets that passed scanner filters |
| `SAME_TICKER_EXTERNAL_TIMEOUT_SEC` | ❌ | `8` | Timeout for same-ticker GeckoTerminal fallback |
| `TELEGRAM_SIGNAL_DELIVERY_LIMIT` | ❌ | `2000` | Max pending Telegram deliveries sent synchronously per signal |
| `WATCHLIST_CHECK_INTERVAL` | ❌ | `900` | Seconds between per-token watchlist market checks |
| `WATCHLIST_CHECK_BATCH` | ❌ | `100` | Max due watchlist rows checked per bot loop |
| `WATCHLIST_NOTIFY_MCAP_CHANGE_PCT` | ❌ | `50` | Alert threshold for watchlist market-cap move |
| `WATCHLIST_NOTIFY_VOLUME_CHANGE_PCT` | ❌ | `100` | Alert threshold for watchlist volume move |
| `WALLET_MONITOR_ENABLED` | ❌ | `false` | Enables tracked-wallet polling on Base |
| `WALLET_POLL_INTERVAL` | ❌ | `60` | Seconds between tracked wallet checks |
| `WALLET_POLL_BATCH` | ❌ | `50` | Max tracked wallets checked per loop |
| `WALLET_LOOKBACK_BLOCKS` | ❌ | `1200` | Initial Base block lookback for new tracked wallets |
| `ALCHEMY_RPC_URL` | Required for wallet monitoring | — | Base RPC URL |

Public Telegram commands:

```text
/start
/help
/status
/research $TICKER
/research 0xCONTRACT
/verdict2 0xCONTRACT
/spoof_check 0xCONTRACT
/summary 0xCONTRACT
/watch 0xCONTRACT [label]
/unwatch 0xCONTRACT
/watchlist
/settings
/settings min_score 7.5
```

`/research` uses the same compact token-card layout as scanner alerts. X mentions are
filtered before display with the imported Jarvis rules: exact ticker/CA relevance,
tiered thesis/action/metric scoring, noise and hashtag spam suppression, trusted-account
boosts, engagement/velocity boosts and duplicate removal.

Admin wallet-tracking commands:

```text
/track 0xWALLET [label]
/untrack 0xWALLET
/wallets
```

Wallet monitoring is off by default. When `WALLET_MONITOR_ENABLED=true` and
`ALCHEMY_RPC_URL` points to a Base Alchemy endpoint, ERC-20 transfer events are stored in
`wallet_events` and recent inflow appears as Smart Money evidence in Verdict 2.0.

The AI summary provider is currently a deterministic stub. It is intentionally not wired to
an external AI model yet.

Trading is intentionally not part of this bot. Signal actions are limited to research,
watchlist and feedback flows.

## Local Testing

```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=xxx SOCIALDATA_API_KEY=xxx python main.py
```

## Local Service Infra

Start local Postgres and Redis:

```bash
docker compose up -d postgres redis
cp .env.infra.example .env.infra
set -a && source .env.infra && set +a
alembic upgrade head
python infra_check.py
python main.py
```

Run a queue worker in another shell:

```bash
set -a && source .env.infra && set +a
rq worker "${RQ_QUEUE_NAME:-launches}" --url "$REDIS_URL"
```

Production process shape is defined in `Procfile`:

```text
release: alembic upgrade head
bot: python main.py
worker: rq worker ${RQ_QUEUE_NAME:-launches} --url $REDIS_URL
maintenance: python maintenance.py
```

Use exactly one `bot` process for Telegram long polling. Multiple `worker` processes are allowed.
See `docs/infra_runbook.md` for the deployment checklist.
