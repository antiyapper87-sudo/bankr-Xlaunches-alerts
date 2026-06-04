from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Tenant, TenantSettings, upsert_tenant


DEFAULT_SIGNAL_SOURCES = ["bankr", "clanker", "virtuals", "dexscreener", "coingecko"]
LEGACY_SAFE_ONLY_SOURCES = {"bankr", "clanker", "virtuals"}


async def ensure_telegram_tenant(db: AsyncSession, chat_id: str, title: str | None = None) -> Tenant:
    tenant_type = "telegram_group" if str(chat_id).startswith("-") else "telegram_user"
    tenant = await upsert_tenant(
        db,
        tenant_type=tenant_type,
        external_id=str(chat_id),
        title=title,
    )
    settings = await db.scalar(select(TenantSettings).where(TenantSettings.tenant_id == tenant.id))
    if settings:
        enabled = settings.enabled_sources or {}
        sources = enabled.get("sources")
        if not sources or set(sources).issubset(LEGACY_SAFE_ONLY_SOURCES):
            settings.enabled_sources = {"sources": DEFAULT_SIGNAL_SOURCES}
    return tenant
