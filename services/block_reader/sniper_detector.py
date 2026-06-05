from __future__ import annotations

from services.block_reader.bundle_detector import pct
from services.block_reader.types import BuyerPosition


def detect_sniper_patterns(
    positions: list[BuyerPosition],
    *,
    total_supply_raw: int,
    pair_created_block: int,
) -> dict:
    if not positions or not pair_created_block:
        return {"risk_score": 0.0, "evidence": []}

    first_block = [pos for pos in positions if pos.first_buy_block <= pair_created_block + 1]
    first_5 = [pos for pos in positions if pos.first_buy_block <= pair_created_block + 5]
    first_20 = sorted(positions, key=lambda item: (item.first_buy_block, item.wallet))[:20]
    first_20_bought_pct = pct(sum(pos.bought_raw for pos in first_20), total_supply_raw)

    risk = 0.0
    evidence: list[dict] = []
    if first_block:
        risk += 20
        evidence.append({"type": "first_block_buyers", "wallets": len(first_block)})
    if len(first_5) >= 10:
        risk += 15
        evidence.append({"type": "crowded_first_5_blocks", "wallets": len(first_5)})
    if first_20_bought_pct > 25:
        risk += 25
        evidence.append({"type": "high_first_20_allocation", "bought_pct": round(first_20_bought_pct, 2)})

    return {
        "risk_score": min(100.0, risk),
        "first_block_wallets": len(first_block),
        "first_5_block_wallets": len(first_5),
        "first_20_bought_pct": round(first_20_bought_pct, 2),
        "evidence": evidence,
    }
