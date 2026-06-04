# Hermes Agent Memory Policy

Status: current source of truth.

## Allowed to Store in Durable Memory

- Confirmed patterns of successful/failed launches (with CA and date).
- Creator profiles (dev wallets, recurring accounts).
- Thesis history for specific tokens (to track evolution).
- List of accounts that regularly turn out to be paid shill / KOL-pack.
- Patterns of successful narrative (for example, "cat + ai" or "real utility in meme wrapper").
- Own mistakes (when a thesis turned out to be wrong and why).

## Forbidden to Store

- Raw tweets or users' personal data.
- Any API keys, RPCs, private data.
- Full lists of shill accounts (only aggregated patterns).
- Negative labels on specific people without hard evidence.

## How Memory Works

- After each analysis, Hermes may call the `/memory-update` command with key insights.
- Memory is stored in a separate `agent_memory` table (JSONB + timestamp + confidence).
- Before a new analysis, Hermes always checks memory by ticker/CA/dev wallet.
- Memory has TTL: records older than 30 days are automatically archived or deleted.

## Example Memory Record

```json
{
  "type": "pattern",
  "key": "dev_wallet_0x...",
  "insight": "This dev has already launched 7 tokens, 6 of them dumped in the first 4 hours",
  "confidence": 90,
  "evidence_count": 6,
  "last_seen": "2026-06-03"
}
```
