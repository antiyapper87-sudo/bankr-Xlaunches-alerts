from __future__ import annotations

from collections import Counter, defaultdict

from services.block_reader.types import BlockRiskSummary
from services.chains.types import NormalizedTx


def severity_for(score: float) -> str:
    if score >= 70:
        return "HIGH"
    if score >= 35:
        return "MEDIUM"
    if score > 0:
        return "LOW"
    return "NONE"


def detect_bundle_like_patterns(transfers: list[NormalizedTx], *, dev_wallet: str = "") -> BlockRiskSummary:
    summary = BlockRiskSummary()
    if not transfers:
        return summary

    buyers = [tx for tx in transfers if tx.to_address and tx.wallet_address]
    unique_buyers = {tx.to_address for tx in buyers if tx.to_address}
    by_block: dict[int, set[str]] = defaultdict(set)
    for tx in buyers:
        if tx.block_number is not None and tx.to_address:
            by_block[tx.block_number].add(tx.to_address)

    top_block_buyers = max((len(wallets) for wallets in by_block.values()), default=0)
    if len(unique_buyers) >= 5 and top_block_buyers >= 4:
        summary.bundle_risk += 25
        summary.signals.append({
            "type": "same_block_buy_cluster",
            "severity": "MEDIUM",
            "title": "Same-block buyer cluster",
            "details": f"{top_block_buyers} wallets bought in one block; {len(unique_buyers)} unique early buyers.",
            "risk_score": 25,
        })

    first_blocks = sorted(block for block in by_block if block is not None)[:10]
    first_block_buyers = {wallet for block in first_blocks for wallet in by_block[block]}
    if len(first_block_buyers) >= 8:
        summary.bundle_risk += 15
        summary.signals.append({
            "type": "fresh_wallet_cluster",
            "severity": "LOW",
            "title": "Dense first-block buyer group",
            "details": f"{len(first_block_buyers)} unique wallets appeared in the first scanned blocks.",
            "risk_score": 15,
        })

    dev_wallet = (dev_wallet or "").lower()
    if dev_wallet:
        dev_related = [tx for tx in transfers if tx.from_address == dev_wallet or tx.to_address == dev_wallet]
        if len(dev_related) >= 2:
            summary.dev_dump_risk += 30
            summary.signals.append({
                "type": "deployer_adjacent_flow",
                "severity": "MEDIUM",
                "title": "Deployer-adjacent token flow",
                "details": f"{len(dev_related)} early transfers touch the deployer wallet.",
                "risk_score": 30,
            })

    sender_counts = Counter(tx.from_address for tx in buyers if tx.from_address)
    shared_sender, shared_count = sender_counts.most_common(1)[0] if sender_counts else ("", 0)
    if shared_sender and shared_count >= 3:
        summary.bundle_risk += 25
        summary.funding_quality = "suspicious"
        summary.signals.append({
            "type": "shared_source_transfers",
            "severity": "MEDIUM",
            "title": "Shared source transfer cluster",
            "details": f"{shared_count} early transfers came from the same source wallet.",
            "risk_score": 25,
        })

    summary.bundle_risk = min(summary.bundle_risk, 100)
    summary.dev_dump_risk = min(summary.dev_dump_risk, 100)
    if summary.funding_quality == "unknown" and summary.signals:
        summary.funding_quality = "mixed"
    summary.confidence = "MEDIUM" if len(transfers) >= 20 else "LOW"
    return summary
