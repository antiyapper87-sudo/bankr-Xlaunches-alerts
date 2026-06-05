from __future__ import annotations

from collections import OrderedDict
from typing import Any

from services.block_reader.base_rpc import AsyncJsonRpcClient, get_logs_chunked, parse_hex_int
from services.block_reader.constants import (
    BALANCE_OF_SELECTOR,
    DECIMALS_SELECTOR,
    MAX_LOGS_PER_SCAN,
    TOTAL_SUPPLY_SELECTOR,
    TRANSFER_TOPIC,
)
from services.block_reader.types import BuyerPosition, TokenTransfer


def normalize_address(value: str | None) -> str:
    value = str(value or "").lower()
    if value.startswith("0x") and len(value) == 42:
        return value
    if len(value) >= 40:
        return "0x" + value[-40:]
    return ""


def decode_uint256(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value or "0x0"), 16)
    except (TypeError, ValueError):
        return default


def decode_transfer_log(log: dict[str, Any]) -> TokenTransfer | None:
    topics = [str(item).lower() for item in (log.get("topics") or [])]
    if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
        return None
    tx_hash = str(log.get("transactionHash") or "").lower()
    block_number = parse_hex_int(log.get("blockNumber"))
    log_index = parse_hex_int(log.get("logIndex"), 0)
    token = str(log.get("address") or "").lower()
    from_address = normalize_address(topics[1])
    to_address = normalize_address(topics[2])
    amount_raw = decode_uint256(log.get("data"))
    if not tx_hash or block_number is None or not token or not from_address or not to_address:
        return None
    return TokenTransfer(
        tx_hash=tx_hash,
        block_number=block_number,
        log_index=int(log_index or 0),
        token=token,
        from_address=from_address,
        to_address=to_address,
        amount_raw=amount_raw,
    )


async def fetch_token_transfers(
    rpc: AsyncJsonRpcClient,
    *,
    token: str,
    from_block: int,
    to_block: int,
    max_logs: int = MAX_LOGS_PER_SCAN,
) -> list[TokenTransfer]:
    logs = await get_logs_chunked(
        rpc,
        address=token,
        topics=[TRANSFER_TOPIC],
        from_block=from_block,
        to_block=to_block,
        chunk_size=80,
        max_logs=max_logs,
    )
    decoded = [decode_transfer_log(item) for item in logs]
    return [item for item in decoded if item is not None]


def first_buyers_from_transfers(
    transfers: list[TokenTransfer],
    *,
    pair_address: str,
    limit: int = 20,
) -> list[BuyerPosition]:
    pair = pair_address.lower()
    buyers: OrderedDict[str, BuyerPosition] = OrderedDict()
    for tx in sorted(transfers, key=lambda item: (item.block_number, item.log_index)):
        if tx.from_address != pair or tx.to_address == pair:
            continue
        if tx.to_address not in buyers:
            buyers[tx.to_address] = BuyerPosition(
                wallet=tx.to_address,
                first_buy_block=tx.block_number,
                first_buy_tx=tx.tx_hash,
            )
            if len(buyers) >= limit:
                break
    return list(buyers.values())


def compute_positions_from_transfers(
    transfers: list[TokenTransfer],
    *,
    pair_address: str,
    wallets: set[str],
) -> dict[str, BuyerPosition]:
    pair = pair_address.lower()
    wallets = {wallet.lower() for wallet in wallets if wallet}
    positions: dict[str, BuyerPosition] = {}
    for tx in sorted(transfers, key=lambda item: (item.block_number, item.log_index)):
        if tx.from_address == pair and tx.to_address in wallets:
            pos = positions.setdefault(
                tx.to_address,
                BuyerPosition(wallet=tx.to_address, first_buy_block=tx.block_number, first_buy_tx=tx.tx_hash),
            )
            pos.bought_raw += tx.amount_raw
            pos.buy_count += 1
            if tx.block_number < pos.first_buy_block:
                pos.first_buy_block = tx.block_number
                pos.first_buy_tx = tx.tx_hash
        elif tx.to_address == pair and tx.from_address in wallets:
            pos = positions.setdefault(
                tx.from_address,
                BuyerPosition(wallet=tx.from_address, first_buy_block=tx.block_number, first_buy_tx=tx.tx_hash),
            )
            pos.sold_raw += tx.amount_raw
            pos.sell_count += 1
    return positions


def _address_call_arg(address: str) -> str:
    return normalize_address(address)[2:].rjust(64, "0")


async def call_uint256(rpc: AsyncJsonRpcClient, *, to: str, data: str) -> int:
    result = await rpc.call("eth_call", [{"to": to.lower(), "data": data}, "latest"])
    return decode_uint256(result)


async def get_total_supply(rpc: AsyncJsonRpcClient, token: str) -> int:
    return await call_uint256(rpc, to=token, data=TOTAL_SUPPLY_SELECTOR)


async def get_decimals(rpc: AsyncJsonRpcClient, token: str) -> int:
    value = await call_uint256(rpc, to=token, data=DECIMALS_SELECTOR)
    return value if 0 <= value <= 36 else 18


async def get_current_balances(
    rpc: AsyncJsonRpcClient,
    *,
    token: str,
    wallets: list[str],
    batch_size: int = 25,
) -> dict[str, int]:
    out: dict[str, int] = {}
    cleaned = [normalize_address(wallet) for wallet in wallets if normalize_address(wallet)]
    for idx in range(0, len(cleaned), batch_size):
        chunk = cleaned[idx : idx + batch_size]
        calls = [
            ("eth_call", [{"to": token.lower(), "data": BALANCE_OF_SELECTOR + _address_call_arg(wallet)}, "latest"])
            for wallet in chunk
        ]
        results = await rpc.batch(calls)
        for wallet, result in zip(chunk, results, strict=False):
            out[wallet] = decode_uint256(result)
    return out
