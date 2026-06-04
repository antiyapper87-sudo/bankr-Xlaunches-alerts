from __future__ import annotations

from typing import Any

import aiohttp


def num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


async def fetch_dexscreener_token(session: aiohttp.ClientSession, token_address: str) -> dict[str, Any] | None:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address.lower()}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None
    pairs = [pair for pair in data.get("pairs") or [] if str(pair.get("chainId") or "").lower() == "base"]
    if not pairs:
        return None
    best = max(pairs, key=lambda pair: num((pair.get("liquidity") or {}).get("usd")) + num((pair.get("volume") or {}).get("h24")))
    base = best.get("baseToken") or {}
    return {
        "_source": "dexscreener",
        "token_name": base.get("name") or "",
        "token_symbol": base.get("symbol") or "",
        "mcap": num(best.get("marketCap") or best.get("fdv")),
        "fdv": num(best.get("fdv")),
        "volume_24h": num((best.get("volume") or {}).get("h24")),
        "liquidity": num((best.get("liquidity") or {}).get("usd")),
        "price_usd": best.get("priceUsd") or "0",
        "price_change_1h": num((best.get("priceChange") or {}).get("h1")),
        "pair_address": best.get("pairAddress") or "",
        "pair_created_at": int(best.get("pairCreatedAt") or 0),
        "dex_id": best.get("dexId") or "",
        "url": best.get("url") or "",
        "raw": best,
    }
