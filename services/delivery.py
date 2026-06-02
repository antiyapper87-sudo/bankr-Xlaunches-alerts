from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    Signal,
    SignalDelivery,
    create_delivery_if_absent,
    create_delivery_batch,
    create_signal_if_absent,
    list_eligible_tenant_deliveries,
)


async def prepare_tenant_delivery(
    db: AsyncSession,
    *,
    ca: str,
    tenant_id: int,
    chat_id: str,
    verdict_score: float | None = None,
    verdict_label: str | None = None,
    payload_json: dict | None = None,
) -> tuple[Signal, SignalDelivery, bool]:
    signal, _ = await create_signal_if_absent(
        db,
        ca=ca,
        verdict_score=verdict_score,
        verdict_label=verdict_label,
    )
    delivery, inserted = await create_delivery_if_absent(
        db,
        signal_id=signal.id,
        tenant_id=tenant_id,
        channel="telegram",
        destination_id=str(chat_id),
        payload_json=payload_json,
    )
    return signal, delivery, inserted


async def prepare_signal_fanout(
    db: AsyncSession,
    *,
    ca: str,
    source: str,
    verdict_score: float | None = None,
    verdict_label: str | None = None,
    payload_json: dict | None = None,
    page_size: int = 1000,
) -> tuple[Signal, int]:
    signal, _ = await create_signal_if_absent(
        db,
        ca=ca,
        verdict_score=verdict_score,
        verdict_label=verdict_label,
    )
    total_inserted = 0
    offset = 0
    while True:
        deliveries = await list_eligible_tenant_deliveries(
            db,
            source=source,
            verdict_score=verdict_score,
            limit=page_size,
            offset=offset,
        )
        if not deliveries:
            break
        for delivery in deliveries:
            delivery["payload_json"] = payload_json
        total_inserted += await create_delivery_batch(db, signal_id=signal.id, deliveries=deliveries)
        if len(deliveries) < page_size:
            break
        offset += page_size
    return signal, total_inserted
