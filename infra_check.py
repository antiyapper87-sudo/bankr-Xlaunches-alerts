from __future__ import annotations

import asyncio
import json

from redis import Redis
from sqlalchemy import text

from database import close_db, db_session, init_db
from services.hermes_context import load_hermes_context
from settings import resolve_database_url, settings


async def check_database() -> dict[str, str]:
    await init_db(resolve_database_url(), auto_create=False)
    try:
        async with db_session() as db:
            await db.execute(text("select 1"))
            for table in (
                "token_research",
                "verdict_v2",
                "spoof_signals",
                "ai_summaries",
                "user_watchlists",
                "user_feedback",
                "tracked_wallets",
                "wallet_events",
                "nitter_health_logs",
                "socialdata_usage_logs",
            ):
                await db.execute(text(f"select 1 from {table} limit 0"))
        return {
            "database": "ok",
            "phase2_schema": "ok",
            "phase3_schema": "ok",
            "phase4_schema": "ok",
            "social_fetcher_schema": "ok",
        }
    finally:
        await close_db()


def check_redis() -> dict[str, str]:
    redis = Redis.from_url(settings.redis_url)
    try:
        redis.ping()
        return {"redis": "ok"}
    finally:
        redis.close()


def check_hermes_rules() -> dict[str, str]:
    context = load_hermes_context()
    return {"hermes_rules": "ok" if context["loaded"] else f"missing:{','.join(context['missing'])}"}


async def main() -> None:
    result = {}
    result.update(await check_database())
    result.update(check_redis())
    result.update(check_hermes_rules())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
