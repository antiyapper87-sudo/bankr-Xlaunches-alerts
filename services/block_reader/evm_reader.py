from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    insert_liquidity_event,
    insert_holder_snapshot,
    list_bundle_signals,
    upsert_block_scan,
    upsert_bundle_signal,
    upsert_prebuy_signal,
    upsert_token_transaction,
)
from services.block_reader.base_rpc import AsyncJsonRpcClient, RpcConfig
from services.block_reader.bundle_detector import detect_bundle_clusters, severity_for
from services.block_reader.constants import BALANCE_BATCH_SIZE, MAX_FIRST_BUYERS, MAX_LOGS_PER_SCAN, QUICK_SCAN_MAX_BLOCKS, RPC_MAX_CONCURRENCY, RPC_RETRIES, RPC_TIMEOUT_SEC
from services.block_reader.dev_wallet_analyzer import analyze_deployer_behavior
from services.block_reader.liquidity_analyzer import fetch_pool_liquidity_logs, normalize_dex_type, score_liquidity_logs
from services.block_reader.sniper_detector import detect_sniper_patterns
from services.block_reader.token_events import (
    compute_positions_from_transfers,
    fetch_token_transfers,
    first_buyers_from_transfers,
    get_current_balances,
    get_total_supply,
)
from services.block_reader.types import BlockRiskSummary, EvidenceItem


PAIR_WINDOW_BEFORE_BLOCKS = 20
PAIR_WINDOW_AFTER_BLOCKS = max(20, QUICK_SCAN_MAX_BLOCKS - PAIR_WINDOW_BEFORE_BLOCKS)
MAX_TX_TO_PARSE = 300


def estimate_pair_block_from_age(latest_block: int, pair_created_at: int | None) -> int | None:
    if not latest_block or not pair_created_at:
        return None
    age_seconds = max(0, datetime.now(timezone.utc).timestamp() - (pair_created_at / 1000 if pair_created_at > 10_000_000_000 else pair_created_at))
    return max(0, latest_block - int(age_seconds / 2.0))


async def scan_base_token_blocks(
    db: AsyncSession,
    session: aiohttp.ClientSession,
    *,
    rpc_url: str,
    token_id: str,
    dex: dict[str, Any] | None = None,
    launch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dex = dex or {}
    launch = launch or {}
    rpc = AsyncJsonRpcClient(
        session,
        RpcConfig(
            url=rpc_url,
            timeout_sec=RPC_TIMEOUT_SEC,
            retries=RPC_RETRIES,
            concurrency=RPC_MAX_CONCURRENCY,
        ),
    )
    try:
        latest_raw = await rpc.call("eth_blockNumber", [])
        latest = int(latest_raw, 16)
    except Exception as exc:
        await upsert_block_scan(db, chain="base", token_id=token_id, status="pending", confidence="LOW", summary_json={"reason": "rpc unavailable", "error": str(exc)[:160]})
        return {"provider": "alchemy", "confidence": "LOW", "signals": [], "bundle_risk": 0, "prebuy_risk": 0, "status": "pending"}

    pair_address = str(dex.get("pair_address") or "").lower()
    pair_block = dex.get("pair_block") or dex.get("created_block")
    if pair_block is None:
        pair_block = estimate_pair_block_from_age(latest, dex.get("pair_created_at"))
    if pair_block is None or not pair_address:
        reason = "pair address missing" if not pair_address else "pair block missing"
        await upsert_block_scan(db, chain="base", token_id=token_id, pair_address=pair_address, status="pending", confidence="LOW", summary_json={"reason": reason, "latest_block": latest})
        return {"provider": "alchemy", "confidence": "LOW", "signals": [], "bundle_risk": 0, "prebuy_risk": 0, "status": "pending"}

    from_block = max(0, int(pair_block) - PAIR_WINDOW_BEFORE_BLOCKS)
    to_block = min(latest, int(pair_block) + PAIR_WINDOW_AFTER_BLOCKS)
    dex_type = normalize_dex_type(dex.get("dex_id") or dex.get("dex_type") or "")
    transfers = (await fetch_token_transfers(rpc, token=token_id, from_block=from_block, to_block=to_block, max_logs=MAX_LOGS_PER_SCAN))[:MAX_TX_TO_PARSE]
    first_buyers = first_buyers_from_transfers(transfers, pair_address=pair_address, limit=MAX_FIRST_BUYERS)
    first_wallets = [pos.wallet for pos in first_buyers]
    positions = compute_positions_from_transfers(transfers, pair_address=pair_address, wallets=set(first_wallets))
    balances = await get_current_balances(rpc, token=token_id, wallets=first_wallets[:MAX_FIRST_BUYERS], batch_size=BALANCE_BATCH_SIZE)
    for wallet, balance in balances.items():
        if wallet in positions:
            positions[wallet].current_balance_raw = balance
    try:
        total_supply = await get_total_supply(rpc, token_id)
    except Exception:
        total_supply = 0

    for tx in transfers:
        if tx.from_address == pair_address:
            event_type = "buy"
            wallet = tx.to_address
            counterparty = pair_address
        elif tx.to_address == pair_address:
            event_type = "sell"
            wallet = tx.from_address
            counterparty = pair_address
        else:
            event_type = "transfer"
            wallet = tx.to_address
            counterparty = tx.from_address
        await upsert_token_transaction(
            db,
            chain="base",
            token_id=token_id,
            tx_hash=tx.tx_hash,
            event_type=event_type,
            wallet_address=wallet or "",
            counterparty_address=counterparty or "",
            pair_address=pair_address,
            block_number=tx.block_number,
            tx_index=tx.log_index,
            amount_token=float(tx.amount_raw),
            amount_native=None,
            raw_json={
                "from": tx.from_address,
                "to": tx.to_address,
                "amount_raw": str(tx.amount_raw),
                "log_index": tx.log_index,
            },
        )

    bundle = detect_bundle_clusters(list(positions.values()), total_supply_raw=total_supply, pair_created_block=int(pair_block))
    sniper = detect_sniper_patterns(list(positions.values()), total_supply_raw=total_supply, pair_created_block=int(pair_block))
    dev = analyze_deployer_behavior(
        transfers,
        deployer=launch.get("deployer_wallet") or launch.get("msg_sender") or "",
        pair_address=pair_address,
        early_wallets=set(first_wallets),
    )
    liq_logs = await fetch_pool_liquidity_logs(rpc, pair_address=pair_address, dex_type=dex_type, from_block=from_block, to_block=to_block)
    liq = score_liquidity_logs(liq_logs, dex_type=dex_type, pair_created_block=int(pair_block))
    confidence = "HIGH" if len(transfers) >= 20 and first_buyers else "MEDIUM" if first_buyers else "LOW"
    summary = BlockRiskSummary(
        bundle_risk=float(bundle.get("risk_score") or 0),
        sniper_score=float(sniper.get("risk_score") or 0),
        prebuy_risk=float(bundle.get("risk_score") or 0) if int(bundle.get("suspected_wallets_count") or 0) >= 8 else 0.0,
        dev_dump_risk=float(dev.get("risk_score") or 0),
        liquidity_risk=float(liq.get("risk_score") or 0),
        holder_concentration_risk=float(bundle.get("current_held_pct") or 0),
        funding_quality="mixed" if bundle.get("evidence") else "unknown",
        confidence=confidence,
        pair_address=pair_address,
        dex_type=dex_type,
        suspected_bundle_wallets_count=int(bundle.get("suspected_wallets_count") or 0),
        bundle_total_bought_pct=bundle.get("total_bought_pct"),
        bundle_current_held_pct=bundle.get("current_held_pct"),
        bundle_sold_pct=bundle.get("sold_pct_of_allocation"),
        first_buyers_count=len(first_buyers),
        first_blocks_scanned=max(0, to_block - from_block),
        raw_metrics={
            "transfers_count": len(transfers),
            "first_buyers": first_wallets[:20],
            "bundle": bundle,
            "sniper": sniper,
            "dev": dev,
            "liquidity": liq,
            "free_tier_mode": True,
        },
    )
    for item in bundle.get("evidence") or []:
        summary.signals.append({"type": item["type"], "severity": severity_for(float(bundle.get("risk_score") or 0)), "title": item["type"].replace("_", " ").title(), "details": str(item), "risk_score": float(bundle.get("risk_score") or 0)})
        summary.evidence.append(EvidenceItem(type=item["type"], severity=severity_for(float(bundle.get("risk_score") or 0)), message=str(item), data=item))
    for item in dev.get("evidence") or []:
        summary.signals.append({"type": item["type"], "severity": severity_for(float(dev.get("risk_score") or 0)), "title": item["type"].replace("_", " ").title(), "details": str(item), "risk_score": float(dev.get("risk_score") or 0)})
        summary.evidence.append(EvidenceItem(type=item["type"], severity=severity_for(float(dev.get("risk_score") or 0)), message=str(item), data=item))
    for item in sniper.get("evidence") or []:
        summary.signals.append({"type": item["type"], "severity": severity_for(float(sniper.get("risk_score") or 0)), "title": item["type"].replace("_", " ").title(), "details": str(item), "risk_score": float(sniper.get("risk_score") or 0)})
        summary.evidence.append(EvidenceItem(type=item["type"], severity=severity_for(float(sniper.get("risk_score") or 0)), message=str(item), data=item))
    for item in liq.get("evidence") or []:
        summary.signals.append({"type": item["type"], "severity": severity_for(float(liq.get("risk_score") or 0)), "title": item["type"].replace("_", " ").title(), "details": str(item), "risk_score": float(liq.get("risk_score") or 0)})
        summary.evidence.append(EvidenceItem(type=item["type"], severity=severity_for(float(liq.get("risk_score") or 0)), message=str(item), data=item))

    for signal in summary.signals:
        await upsert_bundle_signal(
            db,
            chain="base",
            token_id=token_id,
            signal_type=signal["type"],
            severity=signal.get("severity") or severity_for(float(signal.get("risk_score") or 0)),
            risk_score=float(signal.get("risk_score") or 0),
            score_impact=-min(15, float(signal.get("risk_score") or 0) / 4),
            title=signal.get("title") or signal["type"],
            details=signal.get("details") or "",
            evidence_json={"from_block": from_block, "to_block": to_block, "sample_size": len(transfers), "pair_address": pair_address},
            detector_version="bundle-v2",
        )
    if summary.prebuy_risk:
        await upsert_prebuy_signal(db, chain="base", token_id=token_id, severity=severity_for(summary.prebuy_risk), risk_score=summary.prebuy_risk, evidence_json=summary.to_dict(), detector_version="prebuy-v2")
    for item in liq.get("evidence") or []:
        await insert_liquidity_event(
            db,
            chain="base",
            token_id=token_id,
            event_type=item.get("type") or "liquidity_event",
            tx_hash=item.get("tx_hash") or "",
            evidence_json=item,
        )
    await insert_holder_snapshot(
        db,
        chain="base",
        token_id=token_id,
        provider="alchemy-early-buyers",
        holder_count=len(first_wallets) or None,
        top_10_pct=summary.bundle_current_held_pct,
        fresh_wallet_pct=summary.bundle_total_bought_pct,
        snapshot_json={"sampled_transfer_count": len(transfers), "window": [from_block, to_block], "first_buyers": first_wallets[:20]},
    )
    await upsert_block_scan(
        db,
        chain="base",
        token_id=token_id,
        pair_address=pair_address,
        from_block=from_block,
        to_block=to_block,
        confidence=summary.confidence,
        status="completed",
        provider="alchemy",
        summary_json=summary.to_dict(),
    )
    existing = await list_bundle_signals(db, chain="base", token_id=token_id)
    result = summary.to_dict()
    result["provider"] = "alchemy"
    result["status"] = "completed"
    result["persisted_signals"] = len(existing)
    return result
