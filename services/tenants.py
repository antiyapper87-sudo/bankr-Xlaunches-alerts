from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from database import Tenant, upsert_tenant


async def ensure_telegram_tenant(db: AsyncSession, chat_id: str, title: str | None = None) -> Tenant:
    tenant_type = "telegram_group" if str(chat_id).startswith("-") else "telegram_user"
    return await upsert_tenant(
        db,
        tenant_type=tenant_type,
        external_id=str(chat_id),
        title=title,
    )
