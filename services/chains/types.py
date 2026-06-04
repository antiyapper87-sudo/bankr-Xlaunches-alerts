from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(slots=True)
class NormalizedTx:
    chain: str
    tx_hash: str
    block_number: int | None
    tx_index: int | None
    timestamp: datetime | None
    from_address: str | None
    to_address: str | None
    event_type: str
    token_id: str | None
    wallet_address: str | None
    pair_address: str | None
    amount_token: float | None
    amount_native: float | None
    raw: dict[str, Any] = field(default_factory=dict)


class ChainAdapter(Protocol):
    chain: str

    async def get_latest_block(self) -> int | None: ...
    async def estimate_block_at_time(self, ts: datetime) -> int | None: ...
    async def get_token_transfers(self, token_id: str, from_block: int, to_block: int) -> list[NormalizedTx]: ...
    async def get_wallet_funding(self, wallet: str, before_block: int, lookback_blocks: int) -> list[NormalizedTx]: ...
