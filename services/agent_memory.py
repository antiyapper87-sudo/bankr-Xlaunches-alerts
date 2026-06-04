from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_relevant_memories, upsert_agent_memory, upsert_pattern_memory
from models.outcomes import TokenOutcome
from services.memory_scoring import bounded_memory_context, compute_score_adjustment


SUCCESS_LABELS = {"winner_1h", "winner_4h", "winner_24h", "slow_grind"}
FAILURE_LABELS = {"dumped", "dead", "flat", "fake_volume"}
RUG_LABELS = {"rug_or_liquidity_removed"}


def classify_counts(rows: list[TokenOutcome]) -> tuple[int, int, int, int]:
    success = failure = rug = pump = 0
    for row in rows:
        label = row.final_outcome_label or ""
        if label in SUCCESS_LABELS:
            success += 1
            pump += 1
        elif label in RUG_LABELS:
            rug += 1
            failure += 1
        elif label in FAILURE_LABELS:
            failure += 1
    return success, failure, rug, pump


async def update_memory_from_outcome(db: AsyncSession, outcome: TokenOutcome) -> list[str]:
    updated: list[str] = []
    subjects = []
    if outcome.deployer_wallet:
        subjects.append(("dev_wallet", f"{outcome.chain}:{outcome.deployer_wallet.lower()}", "dev_wallet_repeated_failure"))
    if outcome.launch_source:
        subjects.append(("launch_source", f"{outcome.chain}:source:{outcome.launch_source}", "launch_source_quality"))
    if outcome.ticker:
        subjects.append(("ticker", f"{outcome.chain}:ticker:{outcome.ticker.lower()}", "ticker_collision_danger"))

    for subject_type, subject_id, memory_type in subjects:
        stmt = select(TokenOutcome).where(TokenOutcome.status == "completed")
        if subject_type == "dev_wallet":
            stmt = stmt.where(TokenOutcome.deployer_wallet == outcome.deployer_wallet)
        elif subject_type == "launch_source":
            stmt = stmt.where(TokenOutcome.launch_source == outcome.launch_source)
        elif subject_type == "ticker":
            stmt = stmt.where(TokenOutcome.chain == outcome.chain, TokenOutcome.ticker == outcome.ticker)
        rows = list(await db.scalars(stmt.limit(200)))
        sample_size = len(rows)
        success, failure, rug, pump = classify_counts(rows)
        adjustment, confidence, polarity = compute_score_adjustment(
            success_count=success,
            failure_count=failure,
            rug_count=rug,
            sample_size=sample_size,
        )
        if sample_size < 3 and not rug:
            continue
        key = f"{subject_id}:{memory_type}"
        insight = (
            f"{subject_type} history: {success} winners, {failure} failures, {rug} rugs/liquidity removals "
            f"across {sample_size} completed outcomes."
        )
        await upsert_agent_memory(
            db,
            memory_key=key,
            memory_type=memory_type,
            chain=outcome.chain,
            subject_type=subject_type,
            subject_id=subject_id,
            insight=insight,
            normalized_json={
                "score_adjustment": adjustment,
                "sample_size": sample_size,
                "success_count": success,
                "failure_count": failure,
                "rug_count": rug,
            },
            polarity=polarity,
            confidence=confidence,
            evidence_count=sample_size,
        )
        await upsert_pattern_memory(
            db,
            pattern_key=key,
            pattern_type=memory_type,
            chain=outcome.chain,
            entity_key=subject_id,
            sample_size=sample_size,
            success_count=success,
            failure_count=failure,
            rug_count=rug,
            pump_count=pump,
            score_adjustment=adjustment,
            confidence=confidence,
            evidence_json={"outcome_ids": [row.id for row in rows[:20]]},
        )
        updated.append(key)
    return updated


async def build_memory_context(db: AsyncSession, *, chain: str, deployer_wallet: str = "", ticker: str = "", launch_source: str = "") -> dict[str, Any]:
    memories = []
    if deployer_wallet:
        memories.extend(await get_relevant_memories(db, subject_type="dev_wallet", subject_id=f"{chain}:{deployer_wallet.lower()}", limit=3))
    if ticker:
        memories.extend(await get_relevant_memories(db, subject_type="ticker", subject_id=f"{chain}:ticker:{ticker.lower()}", limit=2))
    if launch_source:
        memories.extend(await get_relevant_memories(db, subject_type="launch_source", subject_id=f"{chain}:source:{launch_source}", limit=2))
    by_key = {}
    for item in memories:
        by_key[item.memory_key] = item
    return bounded_memory_context(list(by_key.values()))


async def rebuild_memory_for_completed_outcomes(db: AsyncSession, *, limit: int = 100) -> int:
    rows = list(
        await db.scalars(
            select(TokenOutcome)
            .where(TokenOutcome.status == "completed", TokenOutcome.final_outcome_label.is_not(None))
            .order_by(TokenOutcome.last_checked_at.desc())
            .limit(limit)
        )
    )
    changed = Counter()
    for row in rows:
        for key in await update_memory_from_outcome(db, row):
            changed[key] += 1
    return len(changed)
