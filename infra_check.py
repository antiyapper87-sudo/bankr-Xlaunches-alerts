from __future__ import annotations

import asyncio
import json

from redis import Redis
from sqlalchemy import text

from database import close_db, db_session, init_db
from settings import resolve_database_url, settings


async def check_database() -> dict[str, str]:
    await init_db(resolve_database_url(), auto_create=False)
    try:
        async with db_session() as db:
            await db.execute(text("select 1"))
        return {"database": "ok"}
    finally:
        await close_db()


def check_redis() -> dict[str, str]:
    redis = Redis.from_url(settings.redis_url)
    try:
        redis.ping()
        return {"redis": "ok"}
    finally:
        redis.close()


async def main() -> None:
    result = {}
    result.update(await check_database())
    result.update(check_redis())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
