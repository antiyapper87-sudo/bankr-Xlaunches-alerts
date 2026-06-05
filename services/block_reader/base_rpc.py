from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from typing import Any

import aiohttp


class RpcError(RuntimeError):
    pass


@dataclass(slots=True)
class RpcConfig:
    url: str
    timeout_sec: float = 10.0
    retries: int = 2
    concurrency: int = 4
    backoff_base_sec: float = 0.5


def parse_hex_int(value: Any, default: int | None = None) -> int | None:
    try:
        if isinstance(value, str) and value.startswith("0x"):
            return int(value, 16)
        if value is not None:
            return int(value)
    except (TypeError, ValueError):
        return default
    return default


class AsyncJsonRpcClient:
    def __init__(self, session: aiohttp.ClientSession, config: RpcConfig):
        self.session = session
        self.config = config
        self._ids = itertools.count(1)
        self._sem = asyncio.Semaphore(config.concurrency)

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        if not self.config.url:
            raise RpcError("RPC URL is not configured")
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params or [],
        }
        async with self._sem:
            last_error: Exception | None = None
            for attempt in range(self.config.retries + 1):
                try:
                    async with self.session.post(
                        self.config.url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=self.config.timeout_sec),
                    ) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status != 200:
                            raise RpcError(f"{method} HTTP {resp.status}: {str(data)[:200]}")
                        if data.get("error"):
                            raise RpcError(f"{method} RPC error: {data['error']}")
                        return data.get("result")
                except Exception as exc:
                    last_error = exc
                    if attempt >= self.config.retries:
                        break
                    await asyncio.sleep(self.config.backoff_base_sec * (2**attempt))
            raise RpcError(f"{method} failed after retries: {last_error}")

    async def batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        if not calls:
            return []
        if not self.config.url:
            raise RpcError("RPC URL is not configured")
        payload = [
            {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
            for method, params in calls
        ]
        async with self._sem:
            async with self.session.post(
                self.config.url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout_sec),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    raise RpcError(f"batch HTTP {resp.status}: {str(data)[:200]}")
                by_id = {item.get("id"): item for item in data if isinstance(item, dict)}
                out: list[Any] = []
                for req in payload:
                    item = by_id.get(req["id"]) or {}
                    out.append(None if item.get("error") else item.get("result"))
                return out


async def get_logs_chunked(
    rpc: AsyncJsonRpcClient,
    *,
    address: str,
    topics: list[Any] | None,
    from_block: int,
    to_block: int,
    chunk_size: int = 80,
    max_logs: int = 5_000,
) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    start = max(0, int(from_block))
    end = max(start, int(to_block))
    while start <= end and len(logs) < max_logs:
        chunk_end = min(end, start + chunk_size - 1)
        params = {
            "address": address.lower(),
            "fromBlock": hex(start),
            "toBlock": hex(chunk_end),
        }
        if topics is not None:
            params["topics"] = topics
        result = await rpc.call("eth_getLogs", [params])
        if isinstance(result, list):
            logs.extend([item for item in result if isinstance(item, dict)])
        start = chunk_end + 1
    return logs[:max_logs]
