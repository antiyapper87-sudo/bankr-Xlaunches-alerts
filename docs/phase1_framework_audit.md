# Phase 1 Framework Audit And Deploy Plan

## Context

This document fact-checks the proposed Phase 1 framework from the external audit and
turns it into a deployable plan for the current bot.

Current target:

- keep the product focused on Base
- keep trading disabled
- remove production-critical in-memory state
- avoid unnecessary infrastructure
- ship a stable deployable version before adding more product features

## Executive Decision

Do not implement the pasted framework exactly as written.

There are now two valid Phase 1 tracks:

### Track A: Personal Bot / Fast Local Deploy

Use this only if the goal is a stable bot for one operator or a small private group.

1. Add SQLite persistence first.
2. Keep the bot as a single async process for the first deploy.
3. Do not add Redis/RQ yet.
4. Do not add dynamic thresholds yet.
5. Move only production-critical state into SQLite.
6. Keep low-risk caches in memory.
7. Deploy the stable single-process version.

### Track B: Service For 1000+ Clients

Use this if the goal is a paid product serving many traders/tenants.

1. Use Postgres as source of truth.
2. Add Redis/queue for fanout, retries and rate limiting.
3. Add tenants, tenant settings and signal delivery ledger from day one.
4. Keep launch ingestion, enrichment, scoring and delivery as separate import-safe services.
5. Do not add LLM, billing, Discord, Solana or trading until delivery correctness is solved.

The pasted framework correctly identifies the persistence problem, but still misses the
main service-scale issue: tenant-specific delivery correctness. Redis/RQ alone does not
make the bot a service. The required product primitive is `signal_deliveries` with
idempotency per tenant.

## Fact Check: Current Holes

### Confirmed Holes

| Finding | Current state | Severity | Decision |
| --- | --- | --- | --- |
| `seen_tokens` is in memory | `main.py` still uses `seen_tokens: set[str]` | P0 | Move to SQLite |
| `signaled_tokens` is in memory | `main.py` still uses `signaled_tokens: set[str]` | P0 | Move to SQLite with unique constraints |
| `recheck_queue` is in memory | `main.py` still uses `recheck_queue: dict[str, dict]` | P0 | Move to SQLite recheck fields |
| Verdict cache is in memory | `research_pipeline.py` uses `_verdict_cache` | P1 | Persist after signals/rechecks are stable |
| `/status` reports process-local counters | uses `len(seen_tokens)`, `len(recheck_queue)` | P1 | Read counts from SQLite |
| No `.env.example` | only `.env.local` exists locally | P1 | Add `.env.example` in Phase 1 |
| No automated tests | only `py_compile` checks used | P1 | Add smoke tests for DB/filter/formatting |
| `main.py` is too large | polling, commands, formatting, research, data fetch all mixed | P2 | Split only after persistence is stable |

### Already Closed Holes

| Finding | Current state | Decision |
| --- | --- | --- |
| Trading runtime in bot | removed from command/callback/signal surface | Keep bot research-only |
| Stale Telegram updates after restart | fixed: `deleteWebhook(drop_pending_updates=True)` on startup | Leave as-is |
| Telegram send return type ambiguity | fixed: `send_telegram()` returns `int | None` | Leave as-is |
| Signal UI too noisy | first UX pass done | Iterate later |

## Fact Check: Proposed Framework

### SQLite

Verdict: keep only for Track A and local development.

SQLite is the right persistence layer for Phase 1. It gives durable state without external
ops and is enough for the current source volume.

For the 1000+ client service track, SQLite is not the production source of truth. Use
Postgres for tenant state, signal deliveries and operational visibility.

Required changes:

- use WAL mode
- use explicit unique constraints
- persist launch/signal/recheck state
- store raw launch and market JSON as text JSON
- keep schema simple

### SQLAlchemy + aiosqlite

Verdict: optional, but not the best first move.

The pasted plan proposes SQLAlchemy async models. This is workable, but it adds abstraction
before the data model is proven. For this codebase, a smaller `aiosqlite` wrapper is easier
to debug and safer to deploy quickly.

Decision:

- use `aiosqlite` directly for Track A / local smoke tests
- defer SQLAlchemy until the schema stabilizes or a dashboard/API needs ORM ergonomics

### Redis + RQ

Verdict: reject for Phase 1.

Problems:

- RQ is synchronous by default; current enrichment/research code is async.
- The proposed `worker.py` imports from `main.py`, which would create circular imports and
  can accidentally start bot runtime logic inside workers.
- Redis adds another deploy dependency before it is necessary.
- Queue semantics do not solve the current P0 issue by themselves; persistence does.
- RQ scheduler is extra operational surface and not needed for one bot process.

Decision:

- no Redis/RQ in Phase 1 deploy
- use SQLite-backed state and bounded async tasks inside the bot process
- revisit external workers only after `main.py` is split into import-safe modules

### Dynamic Thresholds

Verdict: defer.

Dynamic thresholds are risky before we have enough stored launch history and before the
scoring model is stable. A bad moving median can silently suppress good early signals or
increase noise.

Decision:

- keep static env thresholds for Phase 1
- collect market distributions in SQLite
- add a read-only `/thresholds` or report first
- only then allow dynamic thresholds behind a feature flag

### Replacing `recheck_queue` With `status = "new"`

Verdict: insufficient.

The recheck queue currently stores scheduling and state:

- `first_seen`
- `last_check`
- `checks`
- `no_data`
- `last_mcap`

A single `status` field cannot represent this.

Decision:

Add explicit recheck fields to `launches`:

- `status`
- `first_seen_at`
- `last_checked_at`
- `next_check_at`
- `check_count`
- `no_data`
- `last_mcap`
- `last_market_json`

### Migrating Existing Runtime State

Verdict: the pasted migration plan is too casual.

It says old `seen_tokens` can be skipped. That is dangerous if the bot deploys with an empty
DB and starts alerting on old launch-source history.

Decision:

- on first boot, seed current Bankr/Clanker/Virtuals launch lists into SQLite as `seeded`
- do not send alerts for seeded rows
- only alert rows first observed after seeding

## What To Cut

Remove from immediate Phase 1:

- Redis
- RQ
- RQ scheduler
- SQLAlchemy ORM
- dynamic thresholds
- separate worker process
- automatic worker startup from bot runtime
- historical ticker spoof system
- user settings
- subscription/multi-tenant logic
- wallet tracking expansion

Keep for immediate Phase 1:

- SQLite durable launch state
- durable signal records
- durable recheck state
- durable verdict cache if cheap after core state is done
- `.env.example`
- focused smoke tests
- architecture update

## Better Phase 1: Deployable Variant

### Goal

Ship a stable bot that can restart without duplicate alerts and without losing recheck
state.

### Runtime Shape

```text
single Python async process
  ├─ Telegram polling
  ├─ launch source polling
  ├─ SQLite persistence
  ├─ bounded enrichment/recheck work
  └─ background verdict edit tasks
```

No Redis. No separate worker. No second deploy unit.

### New Files

```text
database.py                  SQLite connection, schema, CRUD helpers
tests/smoke_db.py             DB init + dedupe smoke test
tests/smoke_formatting.py     signal/verdict formatting smoke test
.env.example                  documented deploy config
```

Optional if code extraction is needed:

```text
market_data.py                DexScreener/GeckoTerminal fetchers
telegram_ui.py                signal/research formatting
```

Only extract modules if it makes the persistence patch smaller and safer.

### SQLite Schema

#### `launches`

```sql
CREATE TABLE IF NOT EXISTS launches (
    ca TEXT PRIMARY KEY,
    ticker TEXT,
    name TEXT,
    source TEXT NOT NULL,
    first_seen_at INTEGER NOT NULL,
    launched_at INTEGER,
    raw_json TEXT NOT NULL,
    market_json TEXT,
    status TEXT NOT NULL,
    status_reason TEXT,
    check_count INTEGER NOT NULL DEFAULT 0,
    no_data INTEGER NOT NULL DEFAULT 0,
    last_checked_at INTEGER,
    next_check_at INTEGER,
    last_mcap REAL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

Recommended statuses:

- `seeded`
- `new`
- `queued_recheck`
- `skipped`
- `signaled`
- `expired`
- `failed`

#### `signals`

```sql
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ca TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id INTEGER,
    source TEXT,
    ticker TEXT,
    verdict_score REAL,
    verdict_json TEXT,
    sent_at INTEGER NOT NULL,
    UNIQUE(ca, chat_id)
);
```

The `UNIQUE(ca, chat_id)` constraint is the real duplicate-signal guard.

#### `verdict_cache`

```sql
CREATE TABLE IF NOT EXISTS verdict_cache (
    ca TEXT PRIMARY KEY,
    verdict_json TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
```

#### `bot_state`

```sql
CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
```

Use this for:

- first successful seed completed
- last Telegram update id, if needed later
- schema/runtime flags

### Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_launches_status_next_check
ON launches(status, next_check_at);

CREATE INDEX IF NOT EXISTS idx_launches_ticker_seen
ON launches(ticker, first_seen_at);

CREATE INDEX IF NOT EXISTS idx_signals_ca
ON signals(ca);
```

### Startup Flow

```text
1. init_db()
2. deleteWebhook(drop_pending_updates=True)
3. setMyCommands()
4. health checks
5. seed current launch source history into launches(status='seeded')
6. start normal poll loop
```

Important:

- seeding must not send alerts
- later polls should insert only unseen CAs as `new`
- existing `seeded/skipped/expired/signaled` rows must not alert again after restart

### Poll Loop Flow

```text
fetch sources
  -> normalize launch
  -> INSERT OR IGNORE launches
  -> if inserted as new:
       fetch market data
       if pass:
          send signal
          insert signal row
          update launch status=signaled
       else if recheckable:
          update launch status=queued_recheck + next_check_at
       else:
          update launch status=skipped/expired

process due rechecks
  -> SELECT launches WHERE status='queued_recheck' AND next_check_at <= now
  -> fetch fresh market data
  -> pass => send signal + insert signal + status=signaled
  -> fail but eligible => update next_check_at/check_count
  -> expired => status=expired
```

### Duplicate Signal Guard

Before sending:

```text
check signals where ca = ? and chat_id = ?
```

After successful Telegram send:

```text
insert into signals(ca, chat_id, message_id, ...)
```

If insert fails due to unique constraint:

- do not send again
- mark launch as `signaled` or leave existing state unchanged

Best practical sequence:

1. check no signal exists
2. send Telegram
3. insert signal row
4. if insert fails, log duplicate risk

For a stronger guarantee later, add `signal_attempts` with a lock state. For Phase 1,
single-process execution is enough.

### What Remains In Memory

Allowed in memory for Phase 1:

- follower cache
- market data short TTL cache
- `_address_map` for callbacks
- counters for current process logs

Move later if needed:

- follower cache into SQLite with TTL
- `_address_map` into signal metadata
- process counters into DB-derived `/status`

### `/status` After Phase 1

Should read from SQLite:

- total launches
- signaled count
- queued rechecks
- skipped/expired count
- verdict enabled
- last source poll time
- DB path

## Deployment Plan

### Phase 1A: Persistence Only

Scope:

- add `aiosqlite`
- add `database.py`
- create tables
- seed current source history
- replace `seen_tokens`, `signaled_tokens`, `recheck_queue`
- update `/status`
- add `.env.example`

Deploy when:

- restart does not resend old source history
- restart keeps queued rechecks
- duplicate signal test passes

### Phase 1B: Cleanup And Tests

Scope:

- add smoke tests
- add DB backup note
- update `architecture.md`
- add simple admin command or log line for DB health

Deploy when:

- `py_compile` passes
- smoke DB test passes
- bot runs locally for one polling cycle
- Telegram startup message is sent once

### Phase 1C: Optional Extraction

Only if needed:

- move market fetchers to `market_data.py`
- move Telegram formatting to `telegram_ui.py`

Do not do this before persistence works.

## Acceptance Criteria

Phase 1 is done only when:

- Bot can restart without duplicate alerts.
- Previously queued rechecks survive restart.
- Signal uniqueness is enforced by SQLite.
- `/status` reflects DB state.
- `.env.example` exists.
- `architecture.md` is updated.
- Local deploy runs in `screen` cleanly.
- No Redis is required.
- No trading is enabled.

## Final Recommendation

For the current private bot, deploy the simpler SQLite-first architecture.

The pasted framework is useful as a direction, but too heavy for the current codebase. The
best production step is a durable single-process bot. Redis/RQ should be considered only
after the source fetching, market enrichment, verdict pipeline, and Telegram sending are
split into import-safe modules with stable function boundaries.

For a real service with 1000+ clients, do not stop at SQLite-first. Use the service-grade
Phase 1 plan in `docs/phase1_service_scale_plan.md`: Postgres, Redis/queue, tenant model,
signal delivery ledger, idempotent fanout, retries, rate limits and DB-backed observability.
