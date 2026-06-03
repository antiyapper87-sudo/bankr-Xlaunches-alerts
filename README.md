# Bankr X Launches Alerts

Monitors early Base token launches from Bankr, Clanker, Virtuals and DEX market data.
Alerts Telegram with market context, X research links, watched-influencer signals and an
optional deterministic auto-verdict block.

## How it works

1. Polls launch sources and rechecks new tokens while market data is still indexing.
2. Enriches launches with DexScreener/GeckoTerminal market data.
3. Filters by market cap, volume, liquidity and source-specific safety rules.
4. Sends Telegram alerts with X Research, Ticker X, Copy CA and chart/trading links.
5. Optionally attaches deterministic research verdicts in the background.
6. Builds Phase 2 Verdict 2.0 research, spoof checks and AI-summary stubs for Base CAs.

## Setup

### 1. Create Telegram Bot
- Message [@BotFather](https://t.me/BotFather) on Telegram
- Send `/newbot` and follow the prompts
- Copy the bot token

### 2. Get Chat ID
- Send `/start` to your new bot
- Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
- Find `"chat":{"id":` — that's your chat ID (use group ID for groups)

### 3. Deploy
- Push this repo to GitHub
- Provision Postgres and Redis
- Add environment variables (see below)
- Run migrations before starting bot/workers

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | — | Chat/group ID for alerts |
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
| `AUTO_VERDICT_ENABLED` | ❌ | `false` | Attach deterministic research verdicts to signal messages |
| `AUTO_VERDICT_TIMEOUT_SEC` | ❌ | `12` | Max time spent building one verdict |
| `AUTO_VERDICT_MAX_CONCURRENT` | ❌ | `2` | Max concurrent verdict jobs |
| `TRADING_ENABLED` | ❌ | `false` | Enables Telegram trade commands/buttons only when trader IDs are configured |
| `TRADER_USER_IDS` | Required for trading | — | Comma-separated Telegram user IDs authorized for trade commands |
| `ALLOW_UNSAFE_TRADING` | ❌ | `false` | Must stay false until quote/minOut-protected on-chain trading is implemented |
| `ALCHEMY_RPC_URL` | Required for wallet/trading | — | Base RPC URL |
| `PRIVATE_KEY` | Required for wallet/trading | — | Trading wallet private key |
| `AUTO_EXECUTE` | ❌ | `false` | Bankr API auto-buy after a Telegram signal is sent |
| `BANKR_EXECUTION_API_KEY` | Required for auto-execute | — | Bankr execution API key |

Phase 2 research commands:

```text
/verdict2 0xCONTRACT
/spoof-check 0xCONTRACT
/summary 0xCONTRACT
```

The AI summary provider is currently a deterministic stub. It is intentionally not wired to
an external AI model yet.

Trading is fail-closed:

- `TRADING_ENABLED=true` is ignored unless `TRADER_USER_IDS` is non-empty.
- `/buy`, `/sell`, `/wallet` and inline trade callbacks reject non-trader users.
- On-chain buy/sell execution returns an error unless `ALLOW_UNSAFE_TRADING=true`.
- Keep `ALLOW_UNSAFE_TRADING=false` until slippage quotes, non-zero `amountOutMinimum`,
  nonce serialization and async-safe web3 execution are implemented.

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
