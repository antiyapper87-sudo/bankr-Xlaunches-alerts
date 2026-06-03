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
TELEGRAM_CHAT_ID=...
AUTHORIZED_USER_IDS=...
SOCIALDATA_API_KEY=...
```

Trading remains disabled unless explicitly configured:

```text
TRADING_ENABLED=false
ALLOW_UNSAFE_TRADING=false
```

## Deployment Checklist

- Provision managed Postgres.
- Provision managed Redis.
- Set `APP_ENV=production`.
- Set `DATABASE_AUTO_CREATE=false`.
- Run `alembic upgrade head` before bot/worker startup.
- Start exactly one `bot` process.
- Start one or more `worker` processes.
- Confirm `/status` reports DB-backed launch, signal, delivery and cooldown state.
- Keep `ALLOW_UNSAFE_TRADING=false`.

## Operational Notes

- `data/*.db` files are local-only and ignored by git.
- Provider 429s are persisted in `provider_cooldowns`.
- Failed Telegram signal sends are stored in `signal_deliveries` with retry status.
- `worker.py` must stay import-safe and must not import `main.py`.
