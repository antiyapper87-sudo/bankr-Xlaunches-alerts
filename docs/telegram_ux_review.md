# Telegram Signal UX Review

## Goal

Make each Telegram signal easier to scan in 5-10 seconds:

1. What token is this?
2. Why did the bot surface it?
3. What are the key market numbers?
4. Is there any social/influencer confirmation?
5. What should be checked next?

The UI should reduce noise and preserve enough context for early-stage token decisions.

## Problems In The Previous Output

- Signal messages mixed identity, market stats, links and commands without a clear hierarchy.
- Links consumed too much vertical space and competed with token context.
- The verdict block repeated details instead of acting like a concise summary.
- The initial message had no stable place for a future AI summary.
- Research flow buttons put buy action ahead of investigation, which is not ideal while trading execution is intentionally disabled.

## Current Proposed Signal Structure

```text
📡 $SYMBOL · Token Name
Source · @deployer

📊 Snapshot
├ MCap ... · Vol ... · Liq ...
└ 1h ... · Age ...

🎯 Why surfaced
└ concise deterministic reason

🧠 AI brief (placeholder)
├ source/age/data status
└ next check

🔗 Gecko · GMGN · Source/Tweet · Uniswap
CA
/research CA
```

When deterministic verdict enrichment completes, the placeholder is replaced with:

```text
🧠 AI brief (deterministic) · LABEL score/10
├ Why: strongest 1-2 reasons
├ Risk: strongest 1-2 risks
└ X: top social signal or no strong confirmation
```

## Button Priority

The inline keyboard is now research-first:

1. X Research
2. Gecko / GMGN
3. Copy CA / Ticker X
4. Banana Gun

Trading remains available as an external action, but it no longer occupies the first decision slot.

## AI Integration Placeholder

No model is connected yet. The placeholder is intentionally deterministic and conservative:

- It does not make unsupported claims.
- It points to the next useful checks.
- It reserves a stable UI slot for future model output.

Future AI output should stay short:

- 1 line: bias / thesis
- 1 line: main risk
- Optional 1 line: what to verify next

## Open UX Questions

- Should the signal include both text links and inline buttons, or should most links move to buttons?
- Should `Banana Gun` be hidden completely while internal trading is disabled?
- Should the verdict label be renamed from `SOLID/MID/WEAK` to more trading-native terms like `WATCH/WAIT/SKIP`?
- Should watched influencer mentions be shown directly in the first signal when available, or only inside the edited verdict block?
- Should `/research` return a compact default view with an optional verbose mode later?
