# Phase 1 Service-Scale Plan

## Goal

Build Phase 1 as the foundation for a real service, not a personal bot.

Target capacity:

- 1000+ Telegram users/groups/tenants
- 500+ launches/hour processed
- 10k+ signal deliveries/day
- restart-safe state
- no duplicate deliveries per tenant
- graceful degradation under external API rate limits

Trading stays disabled. Solana, Discord, Stripe and LLM scoring stay out of Phase 1.

## Key Architecture Decision

For a personal/local bot:

- SQLite-first is enough.

For a 1000+ client service:

- Use Postgres as the source of truth.
- Use Redis/queue for fanout, retries, rate limits and background work.
- Keep a local SQLite-compatible repository only for development/smoke tests.

The important point is not the database brand. The important point is the boundary:

```text
bot/runtime code -> repository/service layer -> database
```

Do not let `main.py` directly own product state anymore.

## What Changes Versus The Previous Phase 1 Plan

The earlier SQLite-first plan was correct for a single-process deploy. For a client-facing
service, it is not enough because the hard part becomes delivery and tenancy, not only
launch deduplication.

New priorities:

1. Multi-tenant data model.
2. Signal delivery ledger.
3. Idempotency per tenant.
4. Queue-backed fanout.
5. Per-tenant settings and limits.
6. Observability and operational control.

Still rejected for Phase 1:

- dynamic thresholds as active filters
- LLM scoring
- on-chain trading
- subscriptions/Stripe
- Discord
- Solana

## Target Runtime Shape

```text
Telegram command process
  ├─ handles /start /status /research /settings
  └─ writes tenant/user/settings changes

Ingestion process
  ├─ polls Bankr/Clanker/Virtuals
  ├─ normalizes launches
  └─ upserts launches by CA

Enrichment workers
  ├─ market data
  ├─ social data
  └─ recheck scheduling

Scoring workers
  └─ deterministic verdict

Delivery workers
  ├─ select eligible tenants
  ├─ enforce per-tenant filters
  ├─ send Telegram
  ├─ store message_id
  └─ retry safely
```

For the first deploy these can still run as one process with internal async queues, but the
code must be split so each component can be moved to its own worker without rewriting logic.

## Required Module Boundaries

Create import-safe services. Workers must not import `main.py`.

```text
database.py
  DB connection, schema, migrations-lite, repository helpers

services/ingestion.py
  source polling normalization and launch upsert

services/market_data.py
  DexScreener/GeckoTerminal fetch and cache policy

services/social.py
  SocialData calls, watched accounts, budget/cache

services/scoring.py
  deterministic verdict orchestration

services/delivery.py
  Telegram delivery, retries, message_id persistence

services/tenants.py
  tenant settings, access control, limits

telegram_ui.py
  signal/research message formatting and keyboards

main.py
  process bootstrap and Telegram command polling only
```

## Data Model

### `tenants`

A tenant is a paying or free destination that receives signals. In Telegram this can be a
private user chat or a group.

```sql
CREATE TABLE tenants (
    id BIGSERIAL PRIMARY KEY,
    type TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT,
    plan TEXT NOT NULL DEFAULT 'free',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(type, external_id)
);
```

Types:

- `telegram_user`
- `telegram_group`
- later `discord_server`

### `users`

```sql
CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id TEXT UNIQUE,
    username TEXT,
    first_name TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### `tenant_members`

```sql
CREATE TABLE tenant_members (
    tenant_id BIGINT NOT NULL REFERENCES tenants(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL DEFAULT 'member',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);
```

Roles:

- `owner`
- `admin`
- `member`

### `tenant_settings`

```sql
CREATE TABLE tenant_settings (
    tenant_id BIGINT PRIMARY KEY REFERENCES tenants(id),
    min_score REAL NOT NULL DEFAULT 6.0,
    enabled_sources JSONB NOT NULL DEFAULT '["bankr","clanker","virtuals"]',
    delivery_mode TEXT NOT NULL DEFAULT 'all',
    max_signals_per_day INTEGER,
    quiet_hours JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Settings must exist in Phase 1 even if UI for editing them is minimal. This prevents a
later rewrite when monetization and premium tiers arrive.

### `launches`

```sql
CREATE TABLE launches (
    ca TEXT PRIMARY KEY,
    ticker TEXT,
    name TEXT,
    source TEXT NOT NULL,
    launched_at TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_json JSONB NOT NULL,
    market_json JSONB,
    status TEXT NOT NULL,
    status_reason TEXT,
    check_count INTEGER NOT NULL DEFAULT 0,
    no_data BOOLEAN NOT NULL DEFAULT false,
    last_checked_at TIMESTAMPTZ,
    next_check_at TIMESTAMPTZ,
    last_mcap REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Statuses:

- `seeded`
- `new`
- `queued_recheck`
- `enriched`
- `scored`
- `fanout_pending`
- `signaled`
- `skipped`
- `expired`
- `failed`

### `verdicts`

```sql
CREATE TABLE verdicts (
    ca TEXT PRIMARY KEY REFERENCES launches(ca),
    score REAL NOT NULL,
    label TEXT NOT NULL,
    verdict_json JSONB NOT NULL,
    model_version TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Use `model_version` from day one. It will matter once scoring changes.

### `signals`

A signal is the product-level event: “this token should be considered for delivery”.

```sql
CREATE TABLE signals (
    id BIGSERIAL PRIMARY KEY,
    ca TEXT NOT NULL REFERENCES launches(ca),
    verdict_score REAL,
    verdict_label TEXT,
    status TEXT NOT NULL DEFAULT 'fanout_pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(ca)
);
```

### `signal_deliveries`

A delivery is tenant-specific. This is the most important table for 1000+ clients.

```sql
CREATE TABLE signal_deliveries (
    id BIGSERIAL PRIMARY KEY,
    signal_id BIGINT NOT NULL REFERENCES signals(id),
    tenant_id BIGINT NOT NULL REFERENCES tenants(id),
    channel TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    status TEXT NOT NULL,
    message_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    last_error TEXT,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(signal_id, tenant_id, channel)
);
```

Statuses:

- `pending`
- `sending`
- `delivered`
- `retry`
- `failed`
- `suppressed`

This table prevents duplicate Telegram messages per tenant.

### `api_budget_events`

```sql
CREATE TABLE api_budget_events (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    endpoint TEXT,
    cost_units INTEGER NOT NULL DEFAULT 1,
    status_code INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Needed for SocialData and GeckoTerminal cost/rate visibility.

### `audit_events`

```sql
CREATE TABLE audit_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT,
    user_id BIGINT,
    event_type TEXT NOT NULL,
    payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Use for admin actions, settings changes and future billing/debugging.

## Indexes And Constraints

Required:

```sql
CREATE INDEX idx_launches_status_next_check ON launches(status, next_check_at);
CREATE INDEX idx_launches_ticker_seen ON launches(ticker, first_seen_at);
CREATE INDEX idx_verdicts_score ON verdicts(score);
CREATE INDEX idx_signal_deliveries_status_retry ON signal_deliveries(status, next_retry_at);
CREATE INDEX idx_signal_deliveries_tenant ON signal_deliveries(tenant_id, created_at);
CREATE INDEX idx_api_budget_provider_time ON api_budget_events(provider, created_at);
```

Critical uniqueness:

```sql
UNIQUE(type, external_id)                  -- tenants
UNIQUE(ca)                                 -- launches
UNIQUE(ca)                                 -- signals
UNIQUE(signal_id, tenant_id, channel)      -- signal_deliveries
```

## Queue Design

Use explicit job types:

```text
ingest.launch_source_poll
enrich.launch_market_data
score.launch_verdict
delivery.create_fanout
delivery.send_telegram
delivery.retry_failed
maintenance.expire_rechecks
maintenance.api_budget_rollup
```

Queue priorities:

```text
critical: Telegram command responses, delivery retries
default: enrichment, scoring
bulk: source polling, maintenance
```

Do not put Telegram command handling behind a slow research queue. User commands must stay
responsive even if enrichment is backed up.

## Fanout Flow

```text
signal created
  -> select eligible tenants
  -> apply tenant settings
  -> insert signal_deliveries(pending)
  -> delivery workers send with rate limits
  -> store Telegram message_id
  -> update delivered/failed/retry
```

Eligibility filters:

- tenant active
- source enabled
- verdict score >= tenant min_score
- plan limit not exceeded
- quiet hours respected

## Rate Limiting

Required limits:

- Telegram global send rate.
- Telegram per-chat send rate.
- SocialData request budget.
- GeckoTerminal cooldown.
- Per-tenant daily signal limit.

Minimum implementation:

- DB counters for daily tenant deliveries.
- Redis token buckets for Telegram sends.
- Provider cooldown flags in DB or Redis.

## Observability

Phase 1 service deploy is not done without observability.

Add `/status` backed by DB:

- total tenants
- active tenants
- launches seen last hour
- signals created today
- deliveries pending/retry/failed
- source poll status
- external API cooldown status
- DB path/DSN label, never full secret

Add logs:

- source poll duration
- new launches count
- enrichment duration
- score distribution
- fanout count
- Telegram delivery failures
- API 429s

Add health command/log:

```text
DB OK
Redis OK
Telegram OK
Source polling OK
Queue depth OK
```

## Deployment Topology

### Local Dev

```text
bot process
SQLite
optional Redis
```

### Production Service

```text
bot-commands worker
source-ingestion worker
enrichment worker
scoring worker
delivery worker
maintenance worker
managed Postgres
managed Redis
```

Start with fewer physical processes if needed, but keep code boundaries ready for this split.

## Environment Variables

Core:

```text
APP_ENV=local|staging|production
DATABASE_URL=postgresql://...
LOCAL_DATABASE_URL=sqlite:///data/bot.db
REDIS_URL=redis://...
```

Telegram:

```text
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
AUTHORIZED_USER_IDS=
```

Providers:

```text
SOCIALDATA_API_KEY=
GECKO_COOLDOWN_SEC=60
```

Limits:

```text
MAX_ENRICHMENT_CONCURRENCY=8
MAX_VERDICT_CONCURRENCY=4
MAX_DELIVERY_CONCURRENCY=20
FREE_DAILY_SIGNAL_LIMIT=10
DEFAULT_MIN_SCORE=6.0
```

Feature flags:

```text
AUTO_VERDICT_ENABLED=true
TRADING_ENABLED=false
ALLOW_UNSAFE_TRADING=false
DYNAMIC_THRESHOLDS_ENABLED=false
LLM_SCORING_ENABLED=false
```

## What To Build In Phase 1

Build:

- tenant model
- durable launch model
- durable recheck model
- signal model
- signal delivery ledger
- per-tenant settings storage
- fanout idempotency
- Telegram delivery retries
- DB-backed `/status`
- `.env.example`
- smoke tests
- architecture update

## High-ROI Implementation Improvements

These items are mandatory Phase 1 hardening work. They should be implemented before moving
to Verdict 2.0, watchlists, wallet tracking, billing or Discord.

### 1. Idempotency Is The Main Invariant

Impact: maximum. Effort: medium.

The service must not rely on process memory to decide whether a signal was already sent.
The database must enforce uniqueness.

Pattern:

```python
class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True)
    ca = Column(String(42), nullable=False, index=True)
    tenant_id = Column(String(64), nullable=False, default="default")
    chat_id = Column(String(64), nullable=False)
    message_id = Column(Integer)
    status = Column(String(24), nullable=False, default="sent")
    sent_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "ca", name="uq_signal_tenant_ca"),
    )
```

Repository helper:

```python
async def already_signaled(db, tenant_id: str, ca: str) -> bool:
    stmt = select(Signal.id).where(
        Signal.tenant_id == tenant_id,
        Signal.ca == ca.lower(),
    )
    return await db.scalar(stmt) is not None
```

Rule:

- no signal send without a DB uniqueness path
- no future fanout without tenant-level idempotency

### 2. Recheck State Must Be Durable

Impact: high. Effort: low.

Do not replace `recheck_queue` with only `status = "new"`. Recheck has scheduling state.

Required launch fields:

```python
status = Column(String(32), nullable=False, default="new")
first_seen_at = Column(DateTime(timezone=True), nullable=False)
last_checked_at = Column(DateTime(timezone=True))
next_check_at = Column(DateTime(timezone=True), index=True)
check_count = Column(Integer, nullable=False, default=0)
no_data = Column(Boolean, nullable=False, default=False)
last_mcap = Column(Float)
status_reason = Column(Text)
market_json = Column(JSON)
```

Required index:

```python
Index("ix_launches_status_next_check", "status", "next_check_at")
```

### 3. Add Alembic Immediately

Impact: high. Effort: low.

Schema changes will accelerate in Phase 2 and Phase 3. Do not rely on manual SQL edits.

Setup:

```bash
pip install alembic
alembic init migrations
```

`migrations/env.py` pattern:

```python
from database import Base

target_metadata = Base.metadata
```

Rule:

- production schema changes go through migrations
- `Base.metadata.create_all()` is allowed only for local smoke tests

### 4. Use Explicit DB Session Boundaries

Impact: high. Effort: low.

No implicit commits hidden deep inside business logic.

Pattern:

```python
async_session = async_sessionmaker(
    engine,
    expire_on_commit=False,
    autoflush=False,
)

@asynccontextmanager
async def db_session():
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

Usage:

```python
async with db_session() as db:
    launch = await upsert_launch(db, raw_launch)
```

### 5. Queue Jobs Must Be Idempotent

Impact: high. Effort: medium.

Jobs should pass stable identifiers, not large mutable payloads.

Pattern:

```python
def enqueue_launch(queue, ca: str):
    ca = ca.lower()
    queue.enqueue(
        "worker.enrich_launch",
        ca,
        job_id=f"enrich:{ca}",
        retry=Retry(max=3, interval=[10, 30, 90]),
        job_timeout=60,
    )
```

Worker wrapper:

```python
def enrich_launch(ca: str):
    asyncio.run(_enrich_launch(ca))

async def _enrich_launch(ca: str):
    async with db_session() as db:
        launch = await get_launch_for_update(db, ca)
        if not launch or launch.status not in {"new", "queued_recheck"}:
            return
```

Rules:

- workers must not import `main.py`
- workers import only service modules
- job IDs must be deterministic
- retries must not create duplicate deliveries

### 6. Add Outbox / Delivery Ledger

Impact: maximum for 1000+ clients. Effort: medium.

The delivery ledger is what makes fanout reliable.

Pattern:

```python
class SignalDelivery(Base):
    __tablename__ = "signal_deliveries"

    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False)
    tenant_id = Column(String(64), nullable=False)
    channel = Column(String(32), nullable=False, default="telegram")
    destination_id = Column(String(64), nullable=False)
    status = Column(String(24), nullable=False, default="pending")
    message_id = Column(Integer)
    attempt_count = Column(Integer, nullable=False, default=0)
    next_retry_at = Column(DateTime(timezone=True))
    last_error = Column(Text)

    __table_args__ = (
        UniqueConstraint("signal_id", "tenant_id", "channel", name="uq_delivery_once"),
        Index("ix_delivery_status_retry", "status", "next_retry_at"),
    )
```

Flow:

1. Create `signal`.
2. Create `signal_deliveries`.
3. Delivery worker sends Telegram.
4. Store `message_id`.
5. Retry failed rows using `next_retry_at`.

### 7. Structured JSON Logs With `correlation_id`

Impact: high. Effort: low.

Every CA must be traceable from source polling to Telegram edit.

Pattern:

```python
def log_event(event: str, **fields):
    log.info(json.dumps({
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        **fields,
    }, ensure_ascii=False))
```

Usage:

```python
cid = f"{source}:{ca.lower()}"
log_event("launch_seen", correlation_id=cid, ca=ca, source=source)
log_event("signal_sent", correlation_id=cid, ca=ca, message_id=message_id)
```

Required events:

- `source_poll_started`
- `source_poll_finished`
- `launch_seen`
- `market_enriched`
- `launch_queued_recheck`
- `signal_created`
- `delivery_sent`
- `delivery_failed`
- `verdict_attached`

### 8. Provider Circuit Breakers

Impact: high. Effort: medium.

External API failures must degrade one provider, not the whole bot.

Table:

```python
class ProviderCooldown(Base):
    __tablename__ = "provider_cooldowns"

    provider = Column(String(32), primary_key=True)
    cooldown_until = Column(DateTime(timezone=True), nullable=False)
    reason = Column(Text)
```

Helper:

```python
async def provider_available(db, provider: str) -> bool:
    row = await db.get(ProviderCooldown, provider)
    return not row or row.cooldown_until <= datetime.now(timezone.utc)
```

Providers that need cooldown:

- SocialData
- GeckoTerminal
- DexScreener
- Telegram send API

### 9. Pydantic Settings v2

Impact: medium. Effort: low.

Move away from scattered `os.getenv`.

Pattern:

```python
class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_chat_id: str
    database_url: str = "sqlite+aiosqlite:///data/bot.db"
    redis_url: str = "redis://localhost:6379/0"
    auto_verdict_enabled: bool = True
    trading_enabled: bool = False
    allow_unsafe_trading: bool = False

    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")
```

Rules:

- secrets only through env
- `.env.local` never committed
- `.env.example` always updated with new keys
- feature flags default safe/off

### 10. Smoke Tests Before Big Refactors

Impact: high. Effort: low.

Minimum tests:

```python
async def test_signal_dedup(db):
    await create_signal(db, tenant_id="t1", ca="0xabc", chat_id="1")
    with pytest.raises(IntegrityError):
        await create_signal(db, tenant_id="t1", ca="0xabc", chat_id="1")
```

Required smoke coverage:

- DB schema init
- CA dedupe
- tenant delivery uniqueness
- restart no-duplicate scenario
- recheck survives restart
- signal formatting does not exceed Telegram limit
- verdict formatting works with empty social data

Do not build:

- billing
- Discord
- Solana
- dynamic threshold auto-filtering
- LLM scoring
- trading
- advanced wallet tracking
- dashboard

## Operational Landmines To Avoid

- `worker.py imports main.py`: circular imports and accidental bot startup inside workers.
- RQ retry without idempotency: duplicate Telegram messages.
- SQLite as production DB for 1000 tenants: lock contention under fanout.
- Missing `message_id`: verdict edits and retry reconciliation become unreliable.
- No Alembic: Phase 2 schema changes become manual and unsafe.
- No provider cooldown: SocialData/Gecko budget gets burned and the bot enters 429 loops.
- No tenant model: monetization and Discord require a rewrite.
- `/status` from memory: useless after restart and wrong with workers.
- Raw payloads retained forever: DB bloat and privacy risk.
- Dynamic thresholds as active filters too early: silent suppression of good signals.

## Acceptance Criteria

Phase 1 service-grade is complete when:

- Restart does not duplicate launch alerts.
- Restart does not lose recheck state.
- One signal can be delivered to 1000 tenants without duplicate rows.
- Failed Telegram sends are retried without creating duplicate messages.
- Per-tenant `min_score` and source settings are enforced.
- `/status` reports DB-backed delivery and queue state.
- API 429s move provider into cooldown/degraded mode.
- Smoke tests cover dedupe, fanout, and restart-state scenarios.
- Production deploy has Postgres and Redis health checks.
- Trading remains disabled.
- Alembic initial migration exists.
- Workers do not import `main.py`.
- `database.py` is committed with the Phase 1 implementation.

## Strong Recommendation

If the goal is a paid service for 1000+ traders, do not ship Phase 1 as a personal-bot
persistence patch.

Ship Phase 1 as a service foundation:

- Postgres as source of truth.
- Redis/queue for fanout and retries.
- Delivery ledger from day one.
- Tenant settings from day one.
- No LLM/trading/billing until delivery correctness is solved.

This is more work upfront, but it avoids rebuilding the product at the exact moment users
start paying for reliability.
