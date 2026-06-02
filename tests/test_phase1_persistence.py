from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio

from database import (
    close_db,
    db_session,
    get_due_delivery_retries,
    get_due_rechecks,
    get_status_snapshot,
    init_db,
    mark_delivery_retry,
    mark_delivery_sending,
    provider_available,
    queue_recheck,
    set_provider_cooldown,
    upsert_launch,
    upsert_tenant,
    utc_now,
)
from services.delivery import prepare_signal_fanout, prepare_tenant_delivery


def ca(n: int) -> str:
    return "0x" + f"{n:040x}"


@pytest_asyncio.fixture()
async def db_url(tmp_path):
    path = tmp_path / "phase1.db"
    await init_db(f"sqlite+aiosqlite:///{path}", auto_create=True)
    try:
        yield f"sqlite+aiosqlite:///{path}"
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_launch_dedupe_and_recheck_survive_restart(db_url):
    token_ca = ca(1)
    async with db_session() as db:
        _, inserted = await upsert_launch(
            db,
            ca=token_ca,
            ticker="ONE",
            name="One",
            source="bankr",
            raw_json={"address": token_ca, "symbol": "ONE", "source": "bankr"},
            launched_at=utc_now(),
            status="new",
        )
        assert inserted is True
        _, inserted_again = await upsert_launch(
            db,
            ca=token_ca,
            ticker="ONE",
            name="One",
            source="bankr",
            raw_json={"address": token_ca, "symbol": "ONE", "source": "bankr"},
            launched_at=utc_now(),
            status="new",
        )
        assert inserted_again is False

        recheck_ca = ca(2)
        await upsert_launch(
            db,
            ca=recheck_ca,
            ticker="TWO",
            name="Two",
            source="clanker",
            raw_json={"address": recheck_ca, "symbol": "TWO", "source": "clanker"},
            launched_at=utc_now(),
            status="new",
        )
        await queue_recheck(
            db,
            ca=recheck_ca,
            reason="no market data",
            next_check_at=utc_now() - timedelta(seconds=1),
            no_data=True,
        )

    await close_db()
    await init_db(db_url, auto_create=False)
    async with db_session() as db:
        due = await get_due_rechecks(db, now=utc_now(), limit=10)
        assert [row.ca for row in due] == [recheck_ca]


@pytest.mark.asyncio
async def test_fanout_is_idempotent_for_1000_tenants(db_url):
    token_ca = ca(3)
    async with db_session() as db:
        await upsert_launch(
            db,
            ca=token_ca,
            ticker="FAN",
            name="Fanout",
            source="bankr",
            raw_json={"address": token_ca, "symbol": "FAN", "source": "bankr"},
            launched_at=utc_now(),
            status="new",
        )
        for idx in range(1000):
            await upsert_tenant(db, tenant_type="telegram_user", external_id=str(10_000 + idx), title=f"user-{idx}")

        _, inserted = await prepare_signal_fanout(
            db,
            ca=token_ca,
            source="bankr",
            verdict_score=8.0,
            verdict_label="watch",
            payload_json={"telegram_text": "signal", "reply_markup": {}},
        )
        assert inserted == 1000

        _, inserted_again = await prepare_signal_fanout(
            db,
            ca=token_ca,
            source="bankr",
            verdict_score=8.0,
            verdict_label="watch",
            payload_json={"telegram_text": "signal", "reply_markup": {}},
        )
        assert inserted_again == 0

        status = await get_status_snapshot(db)
        assert status["tenants_active"] == 1000
        assert status["signals_total"] == 1
        assert status["deliveries_pending"] == 1000


@pytest.mark.asyncio
async def test_delivery_retry_ledger_survives_restart(db_url):
    token_ca = ca(4)
    async with db_session() as db:
        await upsert_launch(
            db,
            ca=token_ca,
            ticker="RET",
            name="Retry",
            source="bankr",
            raw_json={"address": token_ca, "symbol": "RET", "source": "bankr"},
            launched_at=utc_now(),
            status="new",
        )
        tenant = await upsert_tenant(db, tenant_type="telegram_user", external_id="544999608", title="dm")
        _, delivery, _ = await prepare_tenant_delivery(
            db,
            ca=token_ca,
            tenant_id=tenant.id,
            chat_id="544999608",
            payload_json={"telegram_text": "retry me", "reply_markup": {}},
        )
        await mark_delivery_sending(db, delivery_id=delivery.id)
        await mark_delivery_retry(
            db,
            delivery_id=delivery.id,
            error="telegram send failed",
            next_retry_at=utc_now() - timedelta(seconds=1),
        )

    await close_db()
    await init_db(db_url, auto_create=False)
    async with db_session() as db:
        due = await get_due_delivery_retries(db, now=utc_now(), limit=10)
        assert len(due) == 1
        assert due[0].payload_json["telegram_text"] == "retry me"


def test_signal_format_stays_under_telegram_limit():
    from main import format_signal_telegram

    token_ca = ca(5)
    text = format_signal_telegram(
        {"address": token_ca, "symbol": "FMT", "name": "Format", "source": "bankr"},
        {
            "mcap": 50_000,
            "volume_24h": 25_000,
            "liquidity": 10_000,
            "price_usd": "0.001",
            "price_change_1h": 12.3,
            "pair_url": f"https://dexscreener.com/base/{token_ca}",
        },
    )
    assert len(text) < 4096
    assert "/research" in text


@pytest.mark.asyncio
async def test_provider_cooldown_datetime_survives_sqlite_roundtrip(db_url):
    async with db_session() as db:
        await set_provider_cooldown(
            db,
            provider="geckoterminal",
            cooldown_until=utc_now() + timedelta(minutes=1),
            reason="429",
        )

    await close_db()
    await init_db(db_url, auto_create=False)
    async with db_session() as db:
        assert await provider_available(db, "geckoterminal") is False
