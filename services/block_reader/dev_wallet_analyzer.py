from __future__ import annotations

from services.block_reader.types import TokenTransfer


def analyze_deployer_behavior(
    transfers: list[TokenTransfer],
    *,
    deployer: str | None,
    pair_address: str,
    early_wallets: set[str],
) -> dict:
    deployer = str(deployer or "").lower()
    pair = str(pair_address or "").lower()
    early_wallets = {wallet.lower() for wallet in early_wallets if wallet}
    if not deployer or len(deployer) != 42:
        return {"risk_score": 0.0, "confidence": "LOW", "evidence": []}

    sent_to_early: list[TokenTransfer] = []
    buys: list[TokenTransfer] = []
    sells: list[TokenTransfer] = []
    touches: list[TokenTransfer] = []
    for tx in transfers:
        if tx.from_address == deployer or tx.to_address == deployer:
            touches.append(tx)
        if tx.from_address == deployer and tx.to_address in early_wallets:
            sent_to_early.append(tx)
        if tx.from_address == pair and tx.to_address == deployer:
            buys.append(tx)
        if tx.from_address == deployer and tx.to_address == pair:
            sells.append(tx)

    risk = 0.0
    evidence: list[dict] = []
    if sent_to_early:
        risk += min(35, 15 + len(sent_to_early) * 5)
        evidence.append({"type": "deployer_sent_to_early_wallets", "count": len(sent_to_early)})
    if buys:
        risk += 10
        evidence.append({"type": "deployer_bought_early", "count": len(buys)})
    if sells:
        risk += 35
        evidence.append({"type": "deployer_sold_into_pool", "count": len(sells)})

    return {
        "risk_score": min(100.0, risk),
        "confidence": "MEDIUM" if touches else "LOW",
        "evidence": evidence,
        "touch_count": len(touches),
    }
