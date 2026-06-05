from __future__ import annotations

import asyncio
import os

import aiohttp

from database import (
    backfill_chain_identities_from_launches,
    close_db,
    db_session,
    get_launch,
    init_db,
    list_due_token_outcomes,
    mark_launch_status,
    utc_now,
)
from services.agent_memory import rebuild_memory_for_completed_outcomes
from services.block_reader.evm_reader import scan_base_token_blocks
from services.market_data import fetch_dexscreener_token
from services.observability import log_event
from services.outcome_tracker import initial_snapshot_from_outcome, next_due_window, record_outcome_snapshot
from settings import resolve_database_url, settings


async def init_worker_db() -> None:
    await init_db(resolve_database_url(), auto_create=settings.database_auto_create)


def backfill_identities(limit: int = 500) -> int:
    return asyncio.run(_backfill_identities(limit))


async def _backfill_identities(limit: int) -> int:
    await init_worker_db()
    try:
        async with db_session() as db:
            count = await backfill_chain_identities_from_launches(db, limit=limit)
            log_event("worker_identity_backfill", count=count, at=utc_now())
            return count
    finally:
        await close_db()


def process_due_outcomes(limit: int = 50) -> int:
    return asyncio.run(_process_due_outcomes(limit))


async def _process_due_outcomes(limit: int) -> int:
    await init_worker_db()
    processed = 0
    try:
        async with aiohttp.ClientSession() as session:
            async with db_session() as db:
                due = await list_due_token_outcomes(db, now=utc_now(), limit=limit)
            for outcome in due:
                market = await fetch_dexscreener_token(session, outcome.token_id)
                window = next_due_window(outcome)
                async with db_session() as db:
                    await record_outcome_snapshot(
                        db,
                        chain=outcome.chain,
                        token_id=outcome.token_id,
                        window=window,
                        dex=market,
                        initial_snapshot=initial_snapshot_from_outcome(outcome),
                    )
                processed += 1
        log_event("worker_outcomes_processed", count=processed, at=utc_now())
        return processed
    finally:
        await close_db()


def run_block_reader(ca: str) -> dict:
    return asyncio.run(_run_block_reader(ca))


async def _run_block_reader(ca: str) -> dict:
    await init_worker_db()
    try:
        async with aiohttp.ClientSession() as session:
            market = await fetch_dexscreener_token(session, ca)
            async with db_session() as db:
                launch_row = await get_launch(db, ca.lower())
                launch = launch_row.raw_json if launch_row and launch_row.raw_json else {"address": ca.lower()}
                result = await scan_base_token_blocks(
                    db,
                    session,
                    rpc_url=settings.alchemy_rpc_url or os.getenv("ALCHEMY_RPC_URL", ""),
                    token_id=ca.lower(),
                    dex=market or (launch_row.market_json if launch_row else None),
                    launch=launch,
                )
            log_event("worker_block_reader_done", ca=ca.lower(), result=result, at=utc_now())
            return result
    finally:
        await close_db()


def rebuild_memory(limit: int = 100) -> int:
    return asyncio.run(_rebuild_memory(limit))


async def _rebuild_memory(limit: int) -> int:
    await init_worker_db()
    try:
        async with db_session() as db:
            count = await rebuild_memory_for_completed_outcomes(db, limit=limit)
            log_event("worker_memory_rebuild", count=count, at=utc_now())
            return count
    finally:
        await close_db()


def enrich_launch(ca: str) -> None:
    asyncio.run(_enrich_launch(ca))


async def _enrich_launch(ca: str) -> None:
    await init_worker_db()
    try:
        async with aiohttp.ClientSession() as session:
            market = await fetch_dexscreener_token(session, ca)
            async with db_session() as db:
                launch_row = await get_launch(db, ca.lower())
                launch = launch_row.raw_json if launch_row and launch_row.raw_json else {"address": ca.lower()}
                result = await scan_base_token_blocks(
                    db,
                    session,
                    rpc_url=settings.alchemy_rpc_url or os.getenv("ALCHEMY_RPC_URL", ""),
                    token_id=ca.lower(),
                    dex=market or (launch_row.market_json if launch_row else None),
                    launch=launch,
                )
                await mark_launch_status(
                    db,
                    ca=ca,
                    status="queued_recheck",
                    reason=f"worker enrichment completed: block_reader={result.get('status', 'unknown')}",
                )
    finally:
        await close_db()
