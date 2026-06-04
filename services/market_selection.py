from __future__ import annotations

from typing import Any


COMMON_BASE_ASSETS = {
    "weth",
    "eth",
    "usdc",
    "usdbc",
    "dai",
    "cbeth",
    "wsteth",
}


def num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def canonical_pool_score(pool: dict[str, Any]) -> float:
    return (
        num(pool.get("liquidity")) * 2.0
        + num(pool.get("volume_24h"))
        + min(num(pool.get("mcap")), 10_000_000) * 0.05
    )


def select_canonical_market(pools: list[dict[str, Any]], *, token_id: str = "", ticker: str = "") -> dict[str, Any] | None:
    token_id = token_id.lower()
    ticker = ticker.strip().lstrip("$").lower()
    candidates: list[dict[str, Any]] = []
    for pool in pools:
        base_address = str(pool.get("base_token_address") or pool.get("token_address") or pool.get("address") or "").lower()
        base_symbol = str(pool.get("base_token_symbol") or pool.get("token_symbol") or pool.get("symbol") or "").strip().lstrip("$").lower()
        quote_symbol = str(pool.get("quote_token_symbol") or "").strip().lower()
        if base_symbol in COMMON_BASE_ASSETS and quote_symbol not in COMMON_BASE_ASSETS:
            continue
        if token_id and base_address and base_address != token_id:
            continue
        if ticker and base_symbol and base_symbol != ticker:
            continue
        enriched = dict(pool)
        enriched["canonical_score"] = canonical_pool_score(pool)
        enriched["canonical_pool_confidence"] = "HIGH" if token_id and base_address == token_id else "MEDIUM"
        candidates.append(enriched)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item["canonical_score"])
