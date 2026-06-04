# Infrastructure Runbook

## Current Target

Phase 1 infra is a service foundation:

- Postgres is the source of truth in staging/production.
- Redis is the queue/runtime coordination backend.
- Bot, workers and maintenance run as separate processes.
- Migrations are applied before bot/worker startup.

SQLite stays for local smoke/dev only.

## Local Infra

Start Postgres and Redis:

```bash
docker compose up -d postgres redis
```

Use the infra env template:

```bash
cp .env.infra.example .env.infra
set -a
source .env.infra
set +a
```

Apply migrations:

```bash
alembic upgrade head
```

Run the bot against local Postgres/Redis:

```bash
python main.py
```

Run an RQ worker:

```bash
rq worker "${RQ_QUEUE_NAME:-launches}" --url "$REDIS_URL"
```

Run retention cleanup:

```bash
python maintenance.py
```

Check infra connectivity:

```bash
python infra_check.py
```

`infra_check.py` also verifies that Phase 2 tables exist after migrations.

## Process Model

Recommended production processes:

```text
release      alembic upgrade head
bot          python main.py
worker       rq worker ${RQ_QUEUE_NAME:-launches} --url $REDIS_URL
maintenance  python maintenance.py
```

Do not run more than one `bot` process while Telegram long polling is active. Multiple
workers are allowed because DB selection uses idempotency and Postgres row locking where
needed.

## Required Production Environment

```text
APP_ENV=production
DATABASE_URL=postgresql+asyncpg://...
DATABASE_AUTO_CREATE=false
REDIS_URL=redis://...
RQ_QUEUE_NAME=launches
TELEGRAM_BOT_TOKEN=...
AUTHORIZED_USER_IDS=...
SOCIALDATA_API_KEY=...
COINGECKO_API_KEY=...
COINGECKO_DISCOVERY_ENABLED=true
COINGECKO_POLL_INTERVAL=720
TELEGRAM_SIGNAL_DELIVERY_LIMIT=2000
WATCHLIST_CHECK_INTERVAL=900
WATCHLIST_CHECK_BATCH=100
WALLET_MONITOR_ENABLED=false
WALLET_POLL_INTERVAL=60
WALLET_POLL_BATCH=50
```

`TELEGRAM_CHAT_ID` is optional. Set it only when you want one default group/chat tenant
in addition to self-serve DM subscribers from `/start`.

Trading is not part of the runtime. Do not configure trading or private-key execution
environment variables on the bot host.

## Deployment Checklist

- Provision managed Postgres.
- Provision managed Redis.
- Set `APP_ENV=production`.
- Set `DATABASE_AUTO_CREATE=false`.
- Run `alembic upgrade head` before bot/worker startup.
- Start exactly one `bot` process.
- Start one or more `worker` processes.
- Confirm `/status` reports DB-backed launch, signal, delivery and cooldown state.
- Send `/start` from a fresh Telegram user and confirm it receives the English welcome
  plus future DM signals.
- Add `/watch 0xCONTRACT test`, confirm `/watchlist`, then `/unwatch 0xCONTRACT`.
- Add `/track 0xWALLET label`, confirm `/wallets`, then `/untrack 0xWALLET`.

## Operational Notes

- `data/*.db` files are local-only and ignored by git.
- Provider 429s are persisted in `provider_cooldowns`.
- CoinGecko Demo credits are limited; `COINGECKO_POLL_INTERVAL=720` keeps usage near
  5 calls/hour and about 3.6k calls/month.
- Failed Telegram signal sends are stored in `signal_deliveries` with retry status.
- Signal fanout is tenant-driven. `/start` creates an active `telegram_user` tenant with
  Bankr, Clanker, Virtuals, DexScreener and CoinGecko enabled by default.
- Watchlist checks run inside the bot loop for now. They are DB-backed and can move to
  an RQ worker later without changing Telegram commands.
- Tracked wallets are DB-backed. Wallet polling is off by default and requires a Base
  Alchemy-compatible `ALCHEMY_RPC_URL` plus `WALLET_MONITOR_ENABLED=true`.
- `worker.py` must stay import-safe and must not import `main.py`.
