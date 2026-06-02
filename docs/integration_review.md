# Integration Review: Early Token Research Stack

Date: 2026-06-02

Scope:
- Current project: `r48-r48/bankr-Xlaunches-alerts`, branch `main`
- Upstream source: `antiyapper87-sudo/bankr-Xlaunches-alerts`, commits through `de3ed73`
- Reference project: `r48-r48/Jarvis`

## Executive Summary

Do not merge `origin/main` directly. It contains useful verdict work, but it was built against an older `main.py` and would collide with our current bot changes: command menu, DM authorization, Jarvis influencer lists, X Research URL button, tracked-wallet commands, and private repo hygiene.

The right integration is a small research layer after the market filters pass:

1. Keep signal delivery instant.
2. Send the Telegram signal immediately.
3. Run background post-signal research.
4. Edit the same signal with a compact verdict.
5. Store evidence for later deep research.

The upstream `verdict.py` is directionally correct, but should be refactored before production use. It duplicates SocialData calls already present in our current `main.py`, has no concurrency/cost guard, and puts optional Grok live search directly in the hot signal path background task. We should extract its ideas into a single `research_pipeline.py` and make LLM/Grok an optional second pass.

## Upstream Commits Not In Our Branch

Commits from `origin/main` missing in our `main`:

- `27c6c61 Update main.py`
- `b12b502 Create verdict.py`
- `79db0b1 Update main.py`
- `1414058 Update verdict.py`
- `de3ed73 Update main.py`

Relevant additions:

- Adds `verdict.py`.
- Imports `build_verdict` and `format_verdict_block` in `main.py`.
- Changes `send_signal()` to capture Telegram `message_id`.
- Starts `_attach_verdict()` as a background task.
- Edits the original Telegram signal after research completes.
- Adds Nitter button.
- Adds optional xAI/Grok live X search.

Merge risk:

- `origin/main` would delete our `.gitignore`, `data/accounts.txt`, and `data/high_priority.txt` in a direct diff view.
- `main.py` has diverged substantially.
- Upstream keyboard does not include our final `X Research` URL layout.
- Upstream startup command list does not include our new `/help`, `/track`, `/wallets`.

## Current Project Integration Points

Current useful hooks:

- `send_signal()` in `main.py`
  - formats Telegram/WhatsApp alerts
  - sends all alerts via `send_alert_all()`
  - sends Pushover
  - marks token as signaled

- `send_telegram()` and `edit_telegram_message()`
  - already return `message_id` and can edit existing messages

- `build_trade_keyboard()`
  - already has Banana Gun, X Research URL, Copy CA, Ticker X

- `research_token()`
  - already does market data, deployer identity, notable X mentions, watched influencer mentions

- SocialData helpers:
  - `socialdata_search()`
  - `search_x_mentions()`
  - `search_influencer_mentions()`
  - `parse_socialdata_tweet()`
  - `score_research_tweet()`

This means the upstream verdict idea should call our research helpers, not create a parallel SocialData subsystem.

## Target Architecture

Add a deterministic research pipeline:

```text
market filter passes
  -> send signal immediately
  -> background research task
      -> gather evidence
      -> compute deterministic score
      -> optional LLM/Grok synthesis
      -> edit signal with verdict block
      -> persist evidence
```

Recommended files:

- `research_pipeline.py`
  - evidence collection
  - deterministic score
  - verdict object
  - optional LLM/Grok adapter

- `research_formatters.py`
  - Telegram verdict block
  - deep research report chunks

- `research_store.py`
  - SQLite or JSONL storage for signal evidence
  - avoid in-memory-only verdicts

Keep `main.py` as orchestration only. It should not grow another 500 lines.

## Verdict Object

Use a stable internal shape:

```python
{
    "token": {
        "address": "...",
        "symbol": "...",
        "name": "...",
        "source": "bankr|clanker|virtuals|dexscreener",
    },
    "market": {
        "mcap": 0,
        "volume_24h": 0,
        "liquidity": 0,
        "price_change_1h": 0,
        "age_seconds": 0,
    },
    "deployer": {
        "handle": "",
        "followers": 0,
        "bio": "",
        "verified": False,
        "source": "launch_api|resolved|none",
    },
    "social": {
        "notable_mentions": [],
        "watched_influencer_mentions": [],
        "project_account": None,
        "x_search_url": "",
    },
    "score": {
        "value": 0.0,
        "label": "SOLID|MID|WEAK|SPAM",
        "reasons": [],
        "risk_flags": [],
    },
    "llm": {
        "used": False,
        "provider": "",
        "summary": "",
    },
}
```

This lets us reuse the same evidence for:

- immediate signal verdict
- `/research`
- future `/deep`
- wallet-confirmation analysis
- offline tuning

## What To Integrate From Upstream Verdict

Keep:

- Background verdict task after signal delivery.
- Editing original Telegram message instead of sending a second message.
- SocialData profile fetch for deployer bio/followers.
- Project page detection by ticker/name.
- Notable mention summary.
- Heuristic score fallback.
- Optional Grok/xAI pass behind feature flags.
- Compact `🧠 VERDICT` block.

Modify:

- Do not import upstream `verdict.py` as-is.
- Do not let it duplicate `search_x_mentions()` or SocialData parsing.
- Add strict timeout and concurrency controls.
- Add per-token dedup/cache.
- Add failure-safe edit behavior: if edit fails, log only; do not retry aggressively.
- Use `chat_id` from the sent signal, not global `TELEGRAM_CHAT_ID`, so DM/test signals can also be edited correctly.
- Make Grok off by default until cost/latency is measured.

Drop:

- Hardcoded default `GROK_MODEL="grok-4.3"` until confirmed available in our xAI account.
- Live X search on every token by default.
- Formatting that says "No project page found by tools" in every signal. It is noisy. Prefer compact risk flags.
- Any claims that are not backed by collected evidence or LLM citations.

## What To Keep From Jarvis

Already kept:

- `data/accounts.txt`
- `data/high_priority.txt`
- watched influencer searches
- scoring ideas for thesis/traction/followers
- local tracked wallet commands

Keep next:

- Evidence hierarchy from `ROADMAP.md`:
  - profile/official posts
  - high-signal thesis tweets
  - curated account commentary
  - tier-3 whispers
  - on-chain confirmation

- Scoring ideas from `app/filters.py`:
  - thesis quality
  - engagement score
  - exchange-listing demotion
  - spam/template filters
  - convergence across authors

- Output ideas from `app/formatter.py`:
  - brief summary first
  - tiered tweets below
  - citations/links for strong claims

- Storage ideas from `app/db.py`:
  - processed tweets
  - sent tweets
  - onchain events
  - tracked wallets

Drop for now:

- aiogram rewrite.
- FSM state machinery.
- Twikit login/cookie pool.
- Multi-user twikit routing.
- Summary digest scheduler.
- Solana Helius monitor as-is.
- Pydantic settings migration as a first step.
- OpenAI reranker/deep assistant before evidence persistence exists.

Reason: those components solve a different app shape. Pulling them now would increase complexity before our core Base launch signal/research loop is stable.

## Wallet Tracking Direction

Jarvis wallet tracking is Solana/Helius-specific. Our bot is Base-first.

Do not port `app/onchain.py` directly.

Build Base wallet monitoring instead:

- Use Alchemy Base APIs if `ALCHEMY_RPC_URL` is available.
- Alternative: BaseScan/Blockscout APIs.
- Track ERC-20 transfers and swap router interactions for wallets in `data/tracked_wallets.json`.
- Store events in SQLite.
- Alert only when:
  - tracked wallet buys a token we recently signaled, or
  - multiple tracked wallets converge on the same token, or
  - a tracked wallet buys a fresh token that passes minimal market sanity.

Suggested new env:

```text
WALLET_MONITOR_ENABLED=false
WALLET_POLL_INTERVAL=20
WALLET_MIN_USD=100
WALLET_LOOKBACK_MINUTES=60
```

## Recommended Phases

### Phase 1: Safe Verdict Integration

Goal: append a deterministic verdict to signals without changing signal latency.

Tasks:

- Add `research_pipeline.py`.
- Move/merge existing research helpers from `main.py` into that module.
- Add `build_signal_verdict(session, launch, dex)`.
- Add `format_verdict_block(verdict)`.
- Change `send_signal()`:
  - build keyboard once
  - call `send_telegram()` directly to capture `message_id`
  - send WhatsApp separately
  - start `asyncio.create_task(attach_verdict(...))`
- Add feature flag:

```text
AUTO_VERDICT_ENABLED=true
AUTO_VERDICT_TIMEOUT_SEC=12
AUTO_VERDICT_MAX_CONCURRENT=2
```

Acceptance:

- Signal sends immediately.
- Verdict appears in the same Telegram message.
- If SocialData fails, signal remains untouched.
- No duplicate SocialData functions.
- No LLM required.

### Phase 2: Evidence Persistence

Goal: stop losing research state after restart.

Tasks:

- Add SQLite `data/bot.db`.
- Tables:
  - `signals`
  - `research_evidence`
  - `tweet_mentions`
  - `tracked_wallets`
  - `wallet_events`
- Store raw evidence snippets with URLs.
- Add `/last`, `/evidence 0x...`, or `/research 0x... deep` later.

Acceptance:

- Every signaled token has a persisted evidence record.
- `/research` can reuse stored evidence before spending new API calls.

### Phase 3: Optional LLM/Grok Synthesis

Goal: better human-readable verdicts, not dependency on LLM for correctness.

Tasks:

- Add `llm_judge.py` or provider adapter.
- Default off:

```text
LLM_VERDICT_ENABLED=false
XAI_API_KEY=
GROK_MODEL=
GROK_LIVE_SEARCH=false
```

- Input: compact evidence object.
- Output: strict JSON.
- Add citation/source references for strong claims.
- Add cost guard:

```text
LLM_VERDICT_ONLY_IF_SCORE_BETWEEN=3,8
LLM_VERDICT_MAX_PER_HOUR=20
```

Acceptance:

- Bot works with no LLM key.
- LLM failure never blocks or breaks signals.
- LLM claims are tied to evidence.

### Phase 4: Base Wallet Confirmation

Goal: add on-chain confirmation as a second signal layer.

Tasks:

- Move `/track`, `/untrack`, `/wallets` state to SQLite.
- Poll tracked wallets on Base.
- Detect buys/sells.
- Cross-reference recent signal addresses.
- Add verdict boost/risk from wallet actions.

Acceptance:

- Smart wallet buy after signal adds positive evidence.
- Wallet sell/dump after signal adds risk evidence.
- Multiple wallet convergence appears in verdict block.

### Phase 5: Deep Research Mode

Goal: turn bot from alert tool into early-stage token research assistant.

Commands:

- `/research $TICKER`
- `/research 0xCA`
- `/deep 0xCA`
- `/ask analyze 0xCA`

Output shape:

- Verdict
- Bull case
- Bear case
- Unknowns
- Market state
- Notable sources
- Wallet confirmation
- Links/citations

## Immediate Implementation Notes

### `send_signal()` should change from this pattern:

```python
await send_alert_all(session, tg_text, wa_text, token_address=address, symbol=symbol)
```

### To this pattern:

```python
keyboard = build_trade_keyboard(address, symbol)
message_id = await send_telegram(session, tg_text, reply_markup=keyboard)
if WHAPI_TOKEN and WHATSAPP_GROUP_ID:
    await send_whatsapp(session, wa_text)

if AUTO_VERDICT_ENABLED and isinstance(message_id, int):
    asyncio.create_task(attach_verdict(session, chat_id, message_id, tg_text, keyboard, launch, dex))
```

Use `chat_id` explicitly. Do not hardcode `TELEGRAM_CHAT_ID` inside `attach_verdict()`.

### `build_trade_keyboard()` can add Nitter safely:

```python
NITTER_BASE = os.getenv("NITTER_BASE", "https://nitter.net").rstrip("/")
nitter_url = f"{NITTER_BASE}/search?f=tweets&q=%24{quote(sym)}"
```

Add as a small fourth button only if Telegram layout remains readable.

## Main Risks

### P0: API spend and rate limits

Auto-verdict multiplies SocialData calls per signal. The bot already polls and rechecks frequently. Add cache and concurrency limits before enabling at full speed.

### P0: Telegram edit failures

Edited messages have length limits and parse mode constraints. Verdict formatting must be short and HTML-safe.

### P1: LLM hallucination

Grok/OpenAI can overstate findings. Only use LLM as synthesis over collected evidence. Include `Unknowns` when evidence is weak.

### P1: Duplicate research logic

Current `main.py` already has research helpers. Upstream `verdict.py` duplicates them. Consolidate before adding more features.

### P1: Bot becoming one giant file

`main.py` is already large. Further integration should create modules, not add hundreds more lines to `main.py`.

## Decision: What We Should Do Next

Next engineering step:

1. Create `research_pipeline.py`.
2. Move current SocialData tweet parsing/scoring into it.
3. Add deterministic verdict builder.
4. Add compact verdict formatter.
5. Integrate background message edit into `send_signal()`.
6. Keep Grok off.
7. Run local smoke tests.
8. Push to private repo.

This gives us a production-friendly base. After that, add SQLite persistence and Base wallet monitoring.

## Definition Of Done For First Integration PR

- No direct merge from `origin/main`.
- `verdict.py` ideas incorporated through `research_pipeline.py`.
- Signal latency unchanged.
- Auto-verdict feature flag works.
- Bot runs without `XAI_API_KEY`.
- SocialData failure does not break signals.
- Telegram help/status mentions auto-verdict state.
- Tests/smoke checks cover:
  - no SocialData key
  - SocialData returns empty
  - Telegram edit succeeds
  - Telegram edit fails
  - verdict block fits below message limit

