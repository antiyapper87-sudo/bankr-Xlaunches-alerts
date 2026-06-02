from __future__ import annotations

import asyncio

from database import db_session, init_db, mark_launch_status, utc_now
from settings import resolve_database_url, settings
from services.observability import log_event


def enrich_launch(ca: str) -> None:
    asyncio.run(_enrich_launch(ca))


async def _enrich_launch(ca: str) -> None:
    await init_db(resolve_database_url(), auto_create=settings.database_auto_create)
    async with db_session() as db:
        await mark_launch_status(
            db,
            ca=ca,
            status="queued_recheck",
            reason="worker skeleton: enrichment is still handled by main runtime",
        )
        log_event("worker_enrich_placeholder", ca=ca, at=utc_now())
