from __future__ import annotations

from collections import Counter, defaultdict

from services.block_reader.types import BlockRiskSummary, BuyerPosition
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


def pct(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return max(0.0, (float(part) / float(total)) * 100.0)


def detect_bundle_clusters(
    positions: list[BuyerPosition],
    *,
    total_supply_raw: int,
    pair_created_block: int,
) -> dict:
    if not positions:
        return {
            "risk_score": 0.0,
            "suspected_wallets_count": 0,
            "total_bought_pct": 0.0,
            "current_held_pct": 0.0,
            "sold_pct_of_allocation": 0.0,
            "evidence": [],
        }

    positions = sorted(positions, key=lambda item: (item.first_buy_block, item.wallet))
    by_block: dict[int, list[BuyerPosition]] = defaultdict(list)
    for pos in positions:
        by_block[pos.first_buy_block].append(pos)

    top_block_positions = max(by_block.values(), key=len)
    first_3 = [
        pos
        for pos in positions
        if pair_created_block and pos.first_buy_block <= pair_created_block + 3
    ]
    suspected: dict[str, BuyerPosition] = {}
    evidence: list[dict] = []
    risk = 0.0

    if len(top_block_positions) >= 5:
        risk += 25
        for pos in top_block_positions:
            suspected[pos.wallet] = pos
        evidence.append({
            "type": "same_block_buy_cluster",
            "wallets": len(top_block_positions),
            "block_number": top_block_positions[0].first_buy_block,
        })

    if len(first_3) >= 8:
        risk += 15
        for pos in first_3:
            suspected[pos.wallet] = pos
        evidence.append({"type": "first_3_block_cluster", "wallets": len(first_3)})

    bought_values = [pos.bought_raw for pos in positions if pos.bought_raw > 0]
    if len(bought_values) >= 5:
        median = sorted(bought_values)[len(bought_values) // 2]
        similar = [
            pos
            for pos in positions
            if median and pos.bought_raw and abs(pos.bought_raw - median) / median <= 0.15
        ]
        if len(similar) >= 5:
            risk += 10
            for pos in similar:
                suspected[pos.wallet] = pos
            evidence.append({"type": "similar_buy_size_cluster", "wallets": len(similar)})

    cluster_positions = list(suspected.values()) or positions[: min(len(positions), 5)]
    total_bought = sum(pos.bought_raw for pos in cluster_positions)
    total_held = sum(max(0, pos.current_balance_raw) for pos in cluster_positions)
    total_sold = sum(pos.sold_raw for pos in cluster_positions)
    bought_pct = pct(total_bought, total_supply_raw)
    held_pct = pct(total_held, total_supply_raw)
    sold_pct = pct(total_sold, total_bought)

    if bought_pct > 10:
        risk += 20
        evidence.append({"type": "high_early_allocation", "bought_pct": round(bought_pct, 2)})
    if held_pct > 5:
        risk += 10
        evidence.append({"type": "cluster_still_holding", "held_pct": round(held_pct, 2)})
    if sold_pct > 50:
        risk += 20
        evidence.append({"type": "cluster_dumped_allocation", "sold_pct": round(sold_pct, 2)})

    return {
        "risk_score": min(100.0, risk),
        "suspected_wallets_count": len(cluster_positions),
        "total_bought_pct": round(bought_pct, 2),
        "current_held_pct": round(held_pct, 2),
        "sold_pct_of_allocation": round(sold_pct, 2),
        "evidence": evidence[:8],
        "wallets": [pos.wallet for pos in cluster_positions[:20]],
    }
