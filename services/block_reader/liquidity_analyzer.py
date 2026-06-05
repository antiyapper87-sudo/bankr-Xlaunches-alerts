from __future__ import annotations

from typing import Any

from services.block_reader.base_rpc import AsyncJsonRpcClient, get_logs_chunked, parse_hex_int
from services.block_reader.constants import GET_LOGS_CHUNK_BLOCKS, V2_BURN_TOPIC, V2_MINT_TOPIC, V3_BURN_TOPIC, V3_INITIALIZE_TOPIC, V3_MINT_TOPIC


LIQUIDITY_TOPICS = {
    "uniswap_v2": {V2_MINT_TOPIC: "add", V2_BURN_TOPIC: "remove"},
    "uniswap_v3": {V3_INITIALIZE_TOPIC: "initialize", V3_MINT_TOPIC: "add", V3_BURN_TOPIC: "remove"},
    "aerodrome": {V2_MINT_TOPIC: "add", V2_BURN_TOPIC: "remove"},
}


def normalize_dex_type(value: str | None) -> str:
    raw = str(value or "").lower()
    if "v3" in raw:
        return "uniswap_v3"
    if "aerodrome" in raw:
        return "aerodrome"
    if "v2" in raw or "uniswap" in raw:
        return "uniswap_v2"
    return "unknown"


async def fetch_pool_liquidity_logs(
    rpc: AsyncJsonRpcClient,
    *,
    pair_address: str,
    dex_type: str,
    from_block: int,
    to_block: int,
) -> list[dict[str, Any]]:
    dex = normalize_dex_type(dex_type)
    topic_map = LIQUIDITY_TOPICS.get(dex)
    if not pair_address or not topic_map:
        return []
    return await get_logs_chunked(
        rpc,
        address=pair_address,
        topics=[[topic.lower() for topic in topic_map]],
        from_block=from_block,
        to_block=to_block,
        chunk_size=GET_LOGS_CHUNK_BLOCKS,
        max_logs=1_000,
    )


def score_liquidity_logs(logs: list[dict[str, Any]], *, dex_type: str, pair_created_block: int) -> dict[str, Any]:
    dex = normalize_dex_type(dex_type)
    topic_map = LIQUIDITY_TOPICS.get(dex, {})
    add_count = 0
    remove_count = 0
    early_remove_count = 0
    evidence: list[dict[str, Any]] = []
    for log in logs:
        topic0 = str((log.get("topics") or [""])[0]).lower()
        event_type = topic_map.get(topic0)
        block_number = parse_hex_int(log.get("blockNumber"), 0) or 0
        tx_hash = str(log.get("transactionHash") or "").lower()
        if event_type in {"initialize", "add"}:
            add_count += 1
        elif event_type == "remove":
            remove_count += 1
            is_early = bool(pair_created_block and block_number <= pair_created_block + 30)
            if is_early:
                early_remove_count += 1
            evidence.append({
                "type": "early_liquidity_remove" if is_early else "liquidity_remove",
                "block_number": block_number,
                "tx_hash": tx_hash,
            })

    risk = 0.0
    if early_remove_count:
        risk += 60
    elif remove_count:
        risk += 20
    if add_count == 0 and logs:
        risk += 10

    return {
        "risk_score": min(100.0, risk),
        "add_count": add_count,
        "remove_count": remove_count,
        "early_remove_count": early_remove_count,
        "evidence": evidence[:6],
    }
