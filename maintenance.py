from __future__ import annotations

import asyncio
from datetime import timedelta

from database import cleanup_old_rows, close_db, db_session, init_db, utc_now
from settings import resolve_database_url, settings


async def cleanup_old_rows_job() -> dict[str, int]:
    await init_db(resolve_database_url(), auto_create=settings.database_auto_create)
    try:
        now = utc_now()
        async with db_session() as db:
            return await cleanup_old_rows(
                db,
                launch_before=now - timedelta(days=60),
                api_budget_before=now - timedelta(days=30),
                audit_before=now - timedelta(days=180),
            )
    finally:
        await close_db()


def cleanup_old_rows_sync() -> dict[str, int]:
    return asyncio.run(cleanup_old_rows_job())


if __name__ == "__main__":
    print(cleanup_old_rows_sync())
