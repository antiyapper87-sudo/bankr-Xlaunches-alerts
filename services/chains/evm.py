from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiohttp

from services.chains.types import NormalizedTx


def parse_hex_int(value: Any) -> int | None:
    try:
        if isinstance(value, str) and value.startswith("0x"):
            return int(value, 16)
        if value is not None:
            return int(value)
    except (TypeError, ValueError):
        return None
    return None


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


class BaseEvmAdapter:
    chain = "base"

    def __init__(self, session: aiohttp.ClientSession, rpc_url: str):
        self.session = session
        self.rpc_url = rpc_url

    async def rpc(self, method: str, params: list[Any]) -> dict[str, Any] | None:
        if not self.rpc_url:
            return None
        try:
            async with self.session.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                data = await resp.json()
                if data.get("error"):
                    return {"error": data.get("error")}
                return data.get("result")
        except Exception:
            return None

    async def get_latest_block(self) -> int | None:
        return parse_hex_int(await self.rpc("eth_blockNumber", []))

    async def estimate_block_at_time(self, ts: datetime) -> int | None:
        latest = await self.get_latest_block()
        if latest is None:
            return None
        age_seconds = max(0, (datetime.now(timezone.utc) - ts).total_seconds())
        return max(0, latest - int(age_seconds / 2.0))

    async def get_token_transfers(self, token_id: str, from_block: int, to_block: int) -> list[NormalizedTx]:
        params = [{
            "fromBlock": hex(max(0, from_block)),
            "toBlock": hex(max(from_block, to_block)),
            "contractAddresses": [token_id.lower()],
            "category": ["erc20"],
            "withMetadata": True,
            "excludeZeroValue": True,
            "maxCount": "0x12c",
        }]
        result = await self.rpc("alchemy_getAssetTransfers", params)
        transfers = (result or {}).get("transfers") if isinstance(result, dict) else []
        out: list[NormalizedTx] = []
        for item in transfers or []:
            tx_hash = str(item.get("hash") or "").lower()
            if not tx_hash:
                continue
            from_address = str(item.get("from") or "").lower() or None
            to_address = str(item.get("to") or "").lower() or None
            wallet = to_address or from_address
            out.append(
                NormalizedTx(
                    chain=self.chain,
                    tx_hash=tx_hash,
                    block_number=parse_hex_int(item.get("blockNum")),
                    tx_index=None,
                    timestamp=None,
                    from_address=from_address,
                    to_address=to_address,
                    event_type="transfer",
                    token_id=token_id.lower(),
                    wallet_address=wallet,
                    pair_address=None,
                    amount_token=to_float(item.get("value")),
                    amount_native=None,
                    raw=item,
                )
            )
        return out

    async def get_wallet_funding(self, wallet: str, before_block: int, lookback_blocks: int) -> list[NormalizedTx]:
        params = [{
            "fromBlock": hex(max(0, before_block - lookback_blocks)),
            "toBlock": hex(max(0, before_block)),
            "toAddress": wallet.lower(),
            "category": ["external", "erc20"],
            "withMetadata": True,
            "excludeZeroValue": True,
            "maxCount": "0x64",
        }]
        result = await self.rpc("alchemy_getAssetTransfers", params)
        transfers = (result or {}).get("transfers") if isinstance(result, dict) else []
        out: list[NormalizedTx] = []
        for item in transfers or []:
            out.append(
                NormalizedTx(
                    chain=self.chain,
                    tx_hash=str(item.get("hash") or "").lower(),
                    block_number=parse_hex_int(item.get("blockNum")),
                    tx_index=None,
                    timestamp=None,
                    from_address=str(item.get("from") or "").lower() or None,
                    to_address=str(item.get("to") or "").lower() or None,
                    event_type="funding",
                    token_id=None,
                    wallet_address=wallet.lower(),
                    pair_address=None,
                    amount_token=to_float(item.get("value")),
                    amount_native=None,
                    raw=item,
                )
            )
        return [tx for tx in out if tx.tx_hash]
