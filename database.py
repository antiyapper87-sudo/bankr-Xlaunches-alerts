from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    delete,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def pct_change(old: float | None, new: float | None) -> float | None:
    old = float(old or 0)
    new = float(new or 0)
    if old <= 0 or new <= 0:
        return None
    return ((new - old) / old) * 100


class JSONCompat(TypeDecorator):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(256))
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("type", "external_id", name="uq_tenant_type_external"),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    username: Mapped[str | None] = mapped_column(String(128))
    first_name: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class TenantMember(Base):
    __tablename__ = "tenant_members"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class TenantSettings(Base):
    __tablename__ = "tenant_settings"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    min_score: Mapped[float] = mapped_column(Float, nullable=False, default=6.0)
    enabled_sources: Mapped[dict[str, Any]] = mapped_column(
        JSONCompat,
        nullable=False,
        default=lambda: {"sources": ["bankr", "clanker", "virtuals", "dexscreener", "coingecko"]},
    )
    delivery_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="all")
    max_signals_per_day: Mapped[int | None] = mapped_column(Integer)
    quiet_hours: Mapped[dict[str, Any] | None] = mapped_column(JSONCompat)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class Launch(Base):
    __tablename__ = "launches"

    ca: Mapped[str] = mapped_column(String(42), primary_key=True)
    ticker: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str | None] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False)
    market_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCompat)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    check_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_data: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_mcap: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        Index("ix_launches_status_next_check", "status", "next_check_at"),
        Index("ix_launches_ticker_seen", "ticker", "first_seen_at"),
    )


class Verdict(Base):
    __tablename__ = "verdicts"

    ca: Mapped[str] = mapped_column(ForeignKey("launches.ca"), primary_key=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    verdict_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, default="deterministic-v1")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        Index("ix_verdicts_score", "score"),
    )


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ca: Mapped[str] = mapped_column(ForeignKey("launches.ca"), nullable=False, unique=True, index=True)
    verdict_score: Mapped[float | None] = mapped_column(Float)
    verdict_label: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="fanout_pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class SignalDelivery(Base):
    __tablename__ = "signal_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), nullable=False)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="telegram")
    destination_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    message_id: Mapped[str | None] = mapped_column(String(128))
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCompat)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("signal_id", "tenant_id", "channel", name="uq_delivery_once"),
        Index("ix_delivery_status_retry", "status", "next_retry_at"),
        Index("ix_delivery_tenant_created", "tenant_id", "created_at"),
    )


class VerdictCache(Base):
    __tablename__ = "verdict_cache"

    ca: Mapped[str] = mapped_column(String(42), primary_key=True)
    verdict_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class ProviderCooldown(Base):
    __tablename__ = "provider_cooldowns"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    cooldown_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class ApiBudgetEvent(Base):
    __tablename__ = "api_budget_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    endpoint: Mapped[str | None] = mapped_column(String(256))
    cost_units: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status_code: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    __table_args__ = (
        Index("ix_api_budget_provider_time", "provider", "created_at"),
    )


class NitterHealthLog(Base):
    __tablename__ = "nitter_health_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_url: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    detail: Mapped[str | None] = mapped_column(Text)
    response_ms: Mapped[int | None] = mapped_column(Integer)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    __table_args__ = (
        Index("ix_nitter_health_status_created", "status", "created_at"),
    )


class SocialDataUsageLog(Base):
    __tablename__ = "socialdata_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    query_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    mode: Mapped[str | None] = mapped_column(String(32))
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    triggered_by_alpha: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    alpha_reason: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    __table_args__ = (
        Index("ix_socialdata_usage_created", "created_at"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONCompat)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class BotState(Base):
    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


from models import (  # noqa: E402
    AISummary,
    HistoricalLaunch,
    SpoofSignal,
    TokenResearch,
    TrackedWallet,
    UserFeedback,
    UserWatchlist,
    VerdictV2,
    WalletEvent,
)


engine: AsyncEngine | None = None
SessionLocal: async_sessionmaker[AsyncSession] | None = None


def normalize_ca(ca: str) -> str:
    return (ca or "").strip().lower()


def create_db_engine(database_url: str) -> AsyncEngine:
    connect_args: dict[str, Any] = {}
    engine_kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    else:
        engine_kwargs.update({"pool_size": 10, "max_overflow": 20})
    return create_async_engine(database_url, connect_args=connect_args, **engine_kwargs)


async def init_db(database_url: str, *, auto_create: bool = False) -> None:
    global engine, SessionLocal
    engine = create_db_engine(database_url)
    SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    if auto_create:
        async with engine.begin() as conn:
            if database_url.startswith("sqlite"):
                await conn.execute(text("PRAGMA journal_mode=WAL"))
                await conn.execute(text("PRAGMA busy_timeout=5000"))
            await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    global engine, SessionLocal
    if engine is not None:
        await engine.dispose()
    engine = None
    SessionLocal = None


@asynccontextmanager
async def db_session():
    if SessionLocal is None:
        raise RuntimeError("Database is not initialized")
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def upsert_tenant(
    db: AsyncSession,
    *,
    tenant_type: str,
    external_id: str,
    title: str | None = None,
) -> Tenant:
    stmt = select(Tenant).where(Tenant.type == tenant_type, Tenant.external_id == external_id)
    tenant = await db.scalar(stmt)
    if tenant:
        if title and tenant.title != title:
            tenant.title = title
            tenant.updated_at = utc_now()
        return tenant

    tenant = Tenant(type=tenant_type, external_id=external_id, title=title)
    db.add(tenant)
    await db.flush()
    db.add(TenantSettings(tenant_id=tenant.id))
    return tenant


async def get_tenant_settings(db: AsyncSession, *, tenant_id: int) -> TenantSettings:
    settings = await db.get(TenantSettings, tenant_id)
    if settings:
        return settings
    settings = TenantSettings(tenant_id=tenant_id)
    db.add(settings)
    await db.flush()
    return settings


async def get_tenant(db: AsyncSession, *, tenant_id: int) -> Tenant | None:
    return await db.get(Tenant, tenant_id)


async def update_tenant_min_score(db: AsyncSession, *, tenant_id: int, min_score: float) -> TenantSettings:
    settings = await get_tenant_settings(db, tenant_id=tenant_id)
    settings.min_score = max(0.0, min(10.0, float(min_score)))
    settings.updated_at = utc_now()
    await db.flush()
    return settings


async def upsert_launch(
    db: AsyncSession,
    *,
    ca: str,
    ticker: str,
    name: str,
    source: str,
    raw_json: dict[str, Any],
    launched_at: datetime | None = None,
    status: str = "new",
) -> tuple[Launch, bool]:
    ca = normalize_ca(ca)
    launch = await db.get(Launch, ca)
    if launch:
        return launch, False

    launch = Launch(
        ca=ca,
        ticker=(ticker or "").lstrip("$")[:64],
        name=(name or "")[:256],
        source=source,
        launched_at=launched_at,
        first_seen_at=utc_now(),
        raw_json=raw_json,
        status=status,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(launch)
    await db.flush()
    return launch, True


async def launch_exists(db: AsyncSession, ca: str) -> bool:
    return await db.scalar(select(Launch.ca).where(Launch.ca == normalize_ca(ca))) is not None


async def get_launch_status(db: AsyncSession, ca: str) -> str | None:
    return await db.scalar(select(Launch.status).where(Launch.ca == normalize_ca(ca)))


async def get_launch(db: AsyncSession, ca: str) -> Launch | None:
    return await db.get(Launch, normalize_ca(ca))


async def upsert_historical_launch(
    db: AsyncSession,
    *,
    launch: Launch,
    deployer: str | None = None,
    final_status: str | None = None,
) -> HistoricalLaunch:
    ca = normalize_ca(launch.ca)
    row = await db.scalar(select(HistoricalLaunch).where(HistoricalLaunch.ca == ca))
    raw = launch.raw_json or {}
    market = launch.market_json or {}
    if row is None:
        row = HistoricalLaunch(
            ca=ca,
            ticker=(launch.ticker or "").lstrip("$").upper(),
            name=launch.name,
            source=launch.source,
            deployer=deployer or raw.get("x_username") or raw.get("creator_x"),
            launched_at=launch.launched_at,
            first_seen_at=launch.first_seen_at,
            final_status=final_status or launch.status,
            max_mcap=float(market.get("mcap") or launch.last_mcap or 0),
            max_volume=float(market.get("volume_24h") or 0),
            raw_json=raw,
        )
        db.add(row)
        await db.flush()
        return row
    row.ticker = (launch.ticker or row.ticker or "").lstrip("$").upper()
    row.name = launch.name or row.name
    row.source = launch.source or row.source
    row.deployer = deployer or row.deployer or raw.get("x_username") or raw.get("creator_x")
    row.final_status = final_status or launch.status
    row.max_mcap = max(float(market.get("mcap") or launch.last_mcap or 0), float(row.max_mcap or 0))
    row.max_volume = max(float(market.get("volume_24h") or 0), float(row.max_volume or 0))
    row.raw_json = raw
    row.updated_at = utc_now()
    return row


async def get_ticker_history(db: AsyncSession, *, ticker: str, since: datetime | None = None, limit: int = 25) -> list[HistoricalLaunch]:
    ticker = (ticker or "").lstrip("$").upper()
    stmt = select(HistoricalLaunch).where(HistoricalLaunch.ticker == ticker)
    if since is not None:
        stmt = stmt.where(HistoricalLaunch.first_seen_at >= since)
    stmt = stmt.order_by(HistoricalLaunch.first_seen_at.desc()).limit(limit)
    return list(await db.scalars(stmt))


async def get_deployer_history(
    db: AsyncSession,
    *,
    deployer: str,
    since: datetime | None = None,
    exclude_ca: str | None = None,
    limit: int = 50,
) -> list[HistoricalLaunch]:
    deployer = (deployer or "").strip()
    if not deployer:
        return []
    stmt = select(HistoricalLaunch).where(func.lower(HistoricalLaunch.deployer) == deployer.lower())
    if since is not None:
        stmt = stmt.where(HistoricalLaunch.first_seen_at >= since)
    if exclude_ca:
        stmt = stmt.where(HistoricalLaunch.ca != normalize_ca(exclude_ca))
    stmt = stmt.order_by(HistoricalLaunch.first_seen_at.desc()).limit(limit)
    return list(await db.scalars(stmt))


async def get_recent_launches_by_ticker(db: AsyncSession, *, ticker: str, since: datetime | None = None, limit: int = 25) -> list[Launch]:
    ticker = (ticker or "").lstrip("$").upper()
    stmt = select(Launch).where(func.upper(Launch.ticker) == ticker)
    if since is not None:
        stmt = stmt.where(Launch.first_seen_at >= since)
    stmt = stmt.order_by(Launch.first_seen_at.desc()).limit(limit)
    return list(await db.scalars(stmt))


async def queue_recheck(
    db: AsyncSession,
    *,
    ca: str,
    reason: str,
    next_check_at: datetime,
    no_data: bool,
    market_json: dict[str, Any] | None = None,
    last_mcap: float | None = None,
) -> None:
    launch = await db.get(Launch, normalize_ca(ca))
    if not launch:
        return
    launch.status = "queued_recheck"
    launch.status_reason = reason
    launch.next_check_at = next_check_at
    launch.last_checked_at = utc_now()
    launch.check_count += 1
    launch.no_data = no_data
    launch.market_json = market_json
    launch.last_mcap = last_mcap
    launch.updated_at = utc_now()


async def mark_launch_status(
    db: AsyncSession,
    *,
    ca: str,
    status: str,
    reason: str = "",
    market_json: dict[str, Any] | None = None,
    raw_json: dict[str, Any] | None = None,
) -> None:
    launch = await db.get(Launch, normalize_ca(ca))
    if not launch:
        return
    launch.status = status
    launch.status_reason = reason or None
    if raw_json is not None:
        launch.raw_json = raw_json
    if market_json is not None:
        launch.market_json = market_json
        try:
            launch.last_mcap = float(market_json.get("mcap") or 0)
        except Exception:
            launch.last_mcap = None
    launch.last_checked_at = utc_now()
    launch.updated_at = utc_now()


async def get_due_rechecks(db: AsyncSession, *, now: datetime, limit: int) -> list[Launch]:
    stmt = (
        select(Launch)
        .where(Launch.status == "queued_recheck", Launch.next_check_at <= now)
        .order_by(Launch.next_check_at.asc())
        .limit(limit)
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    result = await db.scalars(stmt)
    return list(result)


async def create_signal_if_absent(
    db: AsyncSession,
    *,
    ca: str,
    verdict_score: float | None = None,
    verdict_label: str | None = None,
) -> tuple[Signal, bool]:
    ca = normalize_ca(ca)
    signal = await db.scalar(select(Signal).where(Signal.ca == ca))
    if signal:
        return signal, False
    signal = Signal(ca=ca, verdict_score=verdict_score, verdict_label=verdict_label)
    db.add(signal)
    await db.flush()
    return signal, True


async def signal_exists_for_tenant(db: AsyncSession, *, ca: str, tenant_id: int, channel: str = "telegram") -> bool:
    stmt = (
        select(SignalDelivery.id)
        .join(Signal, Signal.id == SignalDelivery.signal_id)
        .where(
            Signal.ca == normalize_ca(ca),
            SignalDelivery.tenant_id == tenant_id,
            SignalDelivery.channel == channel,
            SignalDelivery.status.in_(("pending", "sending", "delivered", "retry")),
        )
    )
    return await db.scalar(stmt) is not None


async def create_delivery_if_absent(
    db: AsyncSession,
    *,
    signal_id: int,
    tenant_id: int,
    channel: str,
    destination_id: str,
    payload_json: dict[str, Any] | None = None,
) -> tuple[SignalDelivery, bool]:
    stmt = select(SignalDelivery).where(
        SignalDelivery.signal_id == signal_id,
        SignalDelivery.tenant_id == tenant_id,
        SignalDelivery.channel == channel,
    )
    delivery = await db.scalar(stmt)
    if delivery:
        if payload_json and not delivery.payload_json:
            delivery.payload_json = payload_json
            delivery.updated_at = utc_now()
        return delivery, False
    delivery = SignalDelivery(
        signal_id=signal_id,
        tenant_id=tenant_id,
        channel=channel,
        destination_id=destination_id,
        status="pending",
        payload_json=payload_json,
    )
    db.add(delivery)
    await db.flush()
    return delivery, True


async def create_delivery_batch(
    db: AsyncSession,
    *,
    signal_id: int,
    deliveries: list[dict[str, Any]],
) -> int:
    inserted = 0
    for item in deliveries:
        _, was_inserted = await create_delivery_if_absent(
            db,
            signal_id=signal_id,
            tenant_id=int(item["tenant_id"]),
            channel=item.get("channel", "telegram"),
            destination_id=str(item["destination_id"]),
            payload_json=item.get("payload_json"),
        )
        inserted += int(was_inserted)
    return inserted


async def list_eligible_tenant_deliveries(
    db: AsyncSession,
    *,
    source: str,
    verdict_score: float | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    stmt = (
        select(Tenant.id, Tenant.external_id, TenantSettings.enabled_sources, TenantSettings.min_score)
        .join(TenantSettings, TenantSettings.tenant_id == Tenant.id)
        .where(Tenant.status == "active", Tenant.type.in_(("telegram_user", "telegram_group")))
        .order_by(Tenant.id.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).all()
    deliveries: list[dict[str, Any]] = []
    for tenant_id, external_id, enabled_sources, min_score in rows:
        sources = (enabled_sources or {}).get("sources", [])
        if source and sources and source not in sources:
            continue
        if verdict_score is not None and verdict_score < float(min_score or 0):
            continue
        deliveries.append(
            {
                "tenant_id": tenant_id,
                "channel": "telegram",
                "destination_id": external_id,
            }
        )
    return deliveries


async def mark_delivery_sending(db: AsyncSession, *, delivery_id: int) -> None:
    delivery = await db.get(SignalDelivery, delivery_id)
    if not delivery:
        return
    delivery.status = "sending"
    delivery.attempt_count += 1
    delivery.updated_at = utc_now()


async def mark_delivery_sent(db: AsyncSession, *, delivery_id: int, message_id: str) -> None:
    delivery = await db.get(SignalDelivery, delivery_id)
    if not delivery:
        return
    delivery.status = "delivered"
    delivery.message_id = str(message_id)
    delivery.delivered_at = utc_now()
    delivery.updated_at = utc_now()


async def mark_delivery_retry(db: AsyncSession, *, delivery_id: int, error: str, next_retry_at: datetime) -> None:
    delivery = await db.get(SignalDelivery, delivery_id)
    if not delivery:
        return
    delivery.status = "retry"
    delivery.attempt_count += 1
    delivery.last_error = error[:2000]
    delivery.next_retry_at = next_retry_at
    delivery.updated_at = utc_now()


async def mark_delivery_failed(db: AsyncSession, *, delivery_id: int, error: str) -> None:
    delivery = await db.get(SignalDelivery, delivery_id)
    if not delivery:
        return
    delivery.status = "failed"
    delivery.last_error = error[:2000]
    delivery.next_retry_at = None
    delivery.updated_at = utc_now()


async def get_due_delivery_retries(db: AsyncSession, *, now: datetime, limit: int) -> list[SignalDelivery]:
    stmt = (
        select(SignalDelivery)
        .where(SignalDelivery.status == "retry", SignalDelivery.next_retry_at <= now)
        .order_by(SignalDelivery.next_retry_at.asc())
        .limit(limit)
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    return list(await db.scalars(stmt))


async def get_pending_deliveries_for_signal(db: AsyncSession, *, signal_id: int, limit: int = 1000) -> list[SignalDelivery]:
    stmt = (
        select(SignalDelivery)
        .where(SignalDelivery.signal_id == signal_id, SignalDelivery.status == "pending")
        .order_by(SignalDelivery.id.asc())
        .limit(limit)
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    return list(await db.scalars(stmt))


async def upsert_watchlist_item(
    db: AsyncSession,
    *,
    tenant_id: int,
    ca: str,
    label: str = "",
    market_json: dict[str, Any] | None = None,
) -> tuple[UserWatchlist, bool]:
    ca = normalize_ca(ca)
    stmt = select(UserWatchlist).where(UserWatchlist.tenant_id == tenant_id, UserWatchlist.ca == ca)
    row = await db.scalar(stmt)
    market_json = market_json or None
    mcap = float((market_json or {}).get("mcap") or 0) if market_json else None
    volume = float((market_json or {}).get("volume_24h") or 0) if market_json else None
    liquidity = float((market_json or {}).get("liquidity") or 0) if market_json else None
    price = str((market_json or {}).get("price_usd") or "") or None
    if row:
        row.status = "active"
        if label:
            row.label = label[:128]
        if market_json:
            if row.initial_mcap is None:
                row.initial_mcap = mcap
            if row.initial_volume is None:
                row.initial_volume = volume
            if row.initial_liquidity is None:
                row.initial_liquidity = liquidity
            row.last_market_json = market_json
            row.last_mcap = mcap
            row.last_volume = volume
            row.last_liquidity = liquidity
            row.last_price_usd = price
            row.last_checked_at = utc_now()
        row.updated_at = utc_now()
        await db.flush()
        return row, False
    row = UserWatchlist(
        tenant_id=tenant_id,
        ca=ca,
        label=label[:128] or None,
        status="active",
        last_market_json=market_json,
        last_mcap=mcap,
        last_volume=volume,
        last_liquidity=liquidity,
        last_price_usd=price,
        initial_mcap=mcap,
        initial_volume=volume,
        initial_liquidity=liquidity,
        previous_mcap=None,
        previous_volume=None,
        last_mcap_change_pct=None,
        last_volume_change_pct=None,
        last_checked_at=utc_now() if market_json else None,
    )
    db.add(row)
    await db.flush()
    return row, True


async def deactivate_watchlist_item(db: AsyncSession, *, tenant_id: int, ca: str) -> bool:
    row = await db.scalar(
        select(UserWatchlist).where(UserWatchlist.tenant_id == tenant_id, UserWatchlist.ca == normalize_ca(ca))
    )
    if not row or row.status != "active":
        return False
    row.status = "inactive"
    row.updated_at = utc_now()
    await db.flush()
    return True


async def list_watchlist_items(db: AsyncSession, *, tenant_id: int, limit: int = 50) -> list[UserWatchlist]:
    stmt = (
        select(UserWatchlist)
        .where(UserWatchlist.tenant_id == tenant_id, UserWatchlist.status == "active")
        .order_by(UserWatchlist.created_at.desc())
        .limit(limit)
    )
    return list(await db.scalars(stmt))


async def get_due_watchlist_items(db: AsyncSession, *, now: datetime, limit: int, min_interval_seconds: int) -> list[UserWatchlist]:
    cutoff = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    cutoff = cutoff.timestamp() - min_interval_seconds
    cutoff_dt = datetime.fromtimestamp(cutoff, timezone.utc)
    stmt = (
        select(UserWatchlist)
        .where(
            UserWatchlist.status == "active",
            (UserWatchlist.last_checked_at.is_(None) | (UserWatchlist.last_checked_at <= cutoff_dt)),
        )
        .order_by(UserWatchlist.last_checked_at.asc().nullsfirst(), UserWatchlist.id.asc())
        .limit(limit)
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    return list(await db.scalars(stmt))


async def mark_watchlist_checked(
    db: AsyncSession,
    *,
    watchlist_id: int,
    market_json: dict[str, Any] | None,
    notified: bool = False,
) -> UserWatchlist | None:
    row = await db.get(UserWatchlist, watchlist_id)
    if not row:
        return None
    row.last_checked_at = utc_now()
    if market_json:
        next_mcap = float(market_json.get("mcap") or 0)
        next_volume = float(market_json.get("volume_24h") or 0)
        next_liquidity = float(market_json.get("liquidity") or 0)
        if row.initial_mcap is None and row.last_mcap is not None:
            row.initial_mcap = row.last_mcap
        if row.initial_volume is None and row.last_volume is not None:
            row.initial_volume = row.last_volume
        if row.initial_liquidity is None and row.last_liquidity is not None:
            row.initial_liquidity = row.last_liquidity
        row.previous_mcap = row.last_mcap
        row.previous_volume = row.last_volume
        row.last_mcap_change_pct = pct_change(row.last_mcap, next_mcap)
        row.last_volume_change_pct = pct_change(row.last_volume, next_volume)
        row.last_market_json = market_json
        row.last_mcap = next_mcap
        row.last_volume = next_volume
        row.last_liquidity = next_liquidity
        row.last_price_usd = str(market_json.get("price_usd") or "") or None
    if notified:
        row.last_notified_at = utc_now()
    row.updated_at = utc_now()
    await db.flush()
    return row


async def upsert_user_feedback(
    db: AsyncSession,
    *,
    tenant_id: int,
    ca: str,
    action: str,
    source: str = "telegram",
    payload_json: dict[str, Any] | None = None,
    note: str = "",
) -> tuple[UserFeedback, bool]:
    ca = normalize_ca(ca)
    stmt = select(UserFeedback).where(UserFeedback.tenant_id == tenant_id, UserFeedback.ca == ca)
    row = await db.scalar(stmt)
    if row:
        row.action = action[:32]
        row.source = source[:32]
        row.payload_json = payload_json or {}
        row.note = note[:2000] if note else None
        row.updated_at = utc_now()
        await db.flush()
        return row, False
    row = UserFeedback(
        tenant_id=tenant_id,
        ca=ca,
        action=action[:32],
        source=source[:32],
        payload_json=payload_json or {},
        note=note[:2000] if note else None,
    )
    db.add(row)
    await db.flush()
    return row, True


async def upsert_tracked_wallet(
    db: AsyncSession,
    *,
    tenant_id: int,
    address: str,
    label: str = "",
    chain: str = "base",
) -> tuple[TrackedWallet, bool]:
    address = normalize_ca(address)
    chain = (chain or "base").lower()
    stmt = select(TrackedWallet).where(
        TrackedWallet.tenant_id == tenant_id,
        TrackedWallet.address == address,
        TrackedWallet.chain == chain,
    )
    row = await db.scalar(stmt)
    if row:
        row.status = "active"
        row.label = label[:128] or row.label
        row.updated_at = utc_now()
        await db.flush()
        return row, False
    row = TrackedWallet(
        tenant_id=tenant_id,
        address=address,
        label=label[:128] or None,
        chain=chain,
        status="active",
    )
    db.add(row)
    await db.flush()
    return row, True


async def deactivate_tracked_wallet(db: AsyncSession, *, tenant_id: int, address: str, chain: str = "base") -> bool:
    row = await db.scalar(
        select(TrackedWallet).where(
            TrackedWallet.tenant_id == tenant_id,
            TrackedWallet.address == normalize_ca(address),
            TrackedWallet.chain == (chain or "base").lower(),
        )
    )
    if not row or row.status != "active":
        return False
    row.status = "inactive"
    row.updated_at = utc_now()
    await db.flush()
    return True


async def list_tracked_wallets(db: AsyncSession, *, tenant_id: int | None = None, limit: int = 100) -> list[TrackedWallet]:
    stmt = select(TrackedWallet).where(TrackedWallet.status == "active")
    if tenant_id is not None:
        stmt = stmt.where(TrackedWallet.tenant_id == tenant_id)
    stmt = stmt.order_by(TrackedWallet.created_at.desc()).limit(limit)
    return list(await db.scalars(stmt))


async def get_due_tracked_wallets(db: AsyncSession, *, now: datetime, limit: int, min_interval_seconds: int) -> list[TrackedWallet]:
    cutoff_dt = ensure_aware(now) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    cutoff_dt = datetime.fromtimestamp(cutoff_dt.timestamp() - min_interval_seconds, timezone.utc)
    stmt = (
        select(TrackedWallet)
        .where(
            TrackedWallet.status == "active",
            TrackedWallet.chain == "base",
            (TrackedWallet.last_checked_at.is_(None) | (TrackedWallet.last_checked_at <= cutoff_dt)),
        )
        .order_by(TrackedWallet.last_checked_at.asc().nullsfirst(), TrackedWallet.id.asc())
        .limit(limit)
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    return list(await db.scalars(stmt))


async def mark_tracked_wallet_checked(
    db: AsyncSession,
    *,
    wallet_id: int,
    block_number: int | None = None,
) -> TrackedWallet | None:
    row = await db.get(TrackedWallet, wallet_id)
    if not row:
        return None
    if block_number is not None:
        row.last_checked_block = max(int(block_number), int(row.last_checked_block or 0))
    row.last_checked_at = utc_now()
    row.updated_at = utc_now()
    await db.flush()
    return row


async def upsert_wallet_event(
    db: AsyncSession,
    *,
    tracked_wallet_id: int,
    tenant_id: int,
    wallet_address: str,
    ca: str,
    direction: str,
    tx_hash: str,
    block_number: int | None = None,
    amount: float | None = None,
    amount_usd: float | None = None,
    event_type: str = "erc20_transfer",
    event_json: dict[str, Any] | None = None,
    status: str = "new",
) -> tuple[WalletEvent, bool]:
    wallet_address = normalize_ca(wallet_address)
    ca = normalize_ca(ca)
    direction = direction[:16]
    stmt = select(WalletEvent).where(
        WalletEvent.tx_hash == tx_hash,
        WalletEvent.wallet_address == wallet_address,
        WalletEvent.ca == ca,
        WalletEvent.direction == direction,
    )
    row = await db.scalar(stmt)
    if row:
        return row, False
    row = WalletEvent(
        tracked_wallet_id=tracked_wallet_id,
        tenant_id=tenant_id,
        wallet_address=wallet_address,
        ca=ca,
        direction=direction,
        event_type=event_type[:32],
        amount=amount,
        amount_usd=amount_usd,
        tx_hash=tx_hash[:128],
        block_number=block_number,
        event_json=event_json or {},
        status=status[:32],
    )
    db.add(row)
    await db.flush()
    return row, True


async def list_recent_wallet_events_for_ca(
    db: AsyncSession,
    *,
    ca: str,
    since: datetime,
    limit: int = 20,
) -> list[WalletEvent]:
    stmt = (
        select(WalletEvent)
        .where(WalletEvent.ca == normalize_ca(ca), WalletEvent.created_at >= since)
        .order_by(WalletEvent.created_at.desc())
        .limit(limit)
    )
    return list(await db.scalars(stmt))


async def store_verdict(
    db: AsyncSession,
    *,
    ca: str,
    verdict: dict[str, Any],
    expires_at: datetime | None = None,
) -> None:
    ca = normalize_ca(ca)
    score = verdict.get("score") or {}
    value = float(score.get("value") or 0)
    label = str(score.get("label") or "UNKNOWN")
    existing = await db.get(Verdict, ca)
    if existing:
        existing.score = value
        existing.label = label
        existing.verdict_json = verdict
        existing.expires_at = expires_at
        existing.updated_at = utc_now()
    else:
        db.add(Verdict(ca=ca, score=value, label=label, verdict_json=verdict, expires_at=expires_at))

    cache = await db.get(VerdictCache, ca)
    if expires_at:
        if cache:
            cache.verdict_json = verdict
            cache.expires_at = expires_at
        else:
            db.add(VerdictCache(ca=ca, verdict_json=verdict, expires_at=expires_at))


async def get_cached_verdict(db: AsyncSession, ca: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or utc_now()
    cache = await db.get(VerdictCache, normalize_ca(ca))
    if not cache or ensure_aware(cache.expires_at) <= now:
        return None
    return cache.verdict_json


async def start_token_research(
    db: AsyncSession,
    *,
    ca: str,
    source: str = "",
    requested_by: str = "pipeline",
) -> tuple[TokenResearch, bool]:
    ca = normalize_ca(ca)
    stmt = select(TokenResearch).where(TokenResearch.ca == ca, TokenResearch.requested_by == requested_by)
    row = await db.scalar(stmt)
    if row:
        if row.status in {"completed", "in_progress"}:
            return row, False
        row.status = "in_progress"
        row.error = None
        row.started_at = utc_now()
        row.updated_at = utc_now()
        return row, False
    row = TokenResearch(
        ca=ca,
        source=source or None,
        requested_by=requested_by,
        status="in_progress",
        started_at=utc_now(),
        raw_data={},
        processed_data={},
    )
    db.add(row)
    await db.flush()
    return row, True


async def complete_token_research(
    db: AsyncSession,
    *,
    research_id: int,
    raw_data: dict[str, Any],
    processed_data: dict[str, Any],
) -> TokenResearch | None:
    row = await db.get(TokenResearch, research_id)
    if not row:
        return None
    row.status = "completed"
    row.raw_data = raw_data
    row.processed_data = processed_data
    row.completed_at = utc_now()
    row.updated_at = utc_now()
    row.error = None
    return row


async def fail_token_research(db: AsyncSession, *, research_id: int, error: str) -> None:
    row = await db.get(TokenResearch, research_id)
    if not row:
        return
    row.status = "failed"
    row.error = error[:2000]
    row.updated_at = utc_now()


async def get_latest_token_research(db: AsyncSession, ca: str) -> TokenResearch | None:
    stmt = (
        select(TokenResearch)
        .where(TokenResearch.ca == normalize_ca(ca))
        .order_by(TokenResearch.created_at.desc())
        .limit(1)
    )
    return await db.scalar(stmt)


async def upsert_spoof_signal(
    db: AsyncSession,
    *,
    ca: str,
    signal_type: str,
    severity: str,
    score_impact: float,
    title: str,
    details: str = "",
    evidence_json: dict[str, Any] | None = None,
    detector_version: str = "spoof-detector-v1",
) -> SpoofSignal:
    ca = normalize_ca(ca)
    stmt = select(SpoofSignal).where(
        SpoofSignal.ca == ca,
        SpoofSignal.signal_type == signal_type,
        SpoofSignal.detector_version == detector_version,
    )
    row = await db.scalar(stmt)
    if row:
        row.severity = severity
        row.score_impact = score_impact
        row.title = title[:160]
        row.details = details[:2000] if details else None
        row.evidence_json = evidence_json or {}
        return row
    row = SpoofSignal(
        ca=ca,
        signal_type=signal_type,
        severity=severity,
        score_impact=score_impact,
        title=title[:160],
        details=details[:2000] if details else None,
        evidence_json=evidence_json or {},
        detector_version=detector_version,
    )
    db.add(row)
    await db.flush()
    return row


async def list_spoof_signals(db: AsyncSession, ca: str) -> list[SpoofSignal]:
    stmt = (
        select(SpoofSignal)
        .where(SpoofSignal.ca == normalize_ca(ca))
        .order_by(SpoofSignal.created_at.desc())
    )
    return list(await db.scalars(stmt))


async def create_verdict_v2(
    db: AsyncSession,
    *,
    ca: str,
    research_id: int | None,
    score: float,
    label: str,
    score_json: dict[str, Any],
    verdict_json: dict[str, Any],
    human_readable: str,
    version: str = "verdict-v2.0",
) -> VerdictV2:
    row = VerdictV2(
        ca=normalize_ca(ca),
        research_id=research_id,
        score=score,
        label=label,
        score_json=score_json,
        verdict_json=verdict_json,
        human_readable=human_readable,
        version=version,
        status="completed",
    )
    db.add(row)
    await db.flush()
    return row


async def get_latest_verdict_v2(db: AsyncSession, ca: str) -> VerdictV2 | None:
    stmt = (
        select(VerdictV2)
        .where(VerdictV2.ca == normalize_ca(ca))
        .order_by(VerdictV2.created_at.desc())
        .limit(1)
    )
    return await db.scalar(stmt)


async def upsert_ai_summary(
    db: AsyncSession,
    *,
    ca: str,
    language: str,
    summary_text: str,
    summary_json: dict[str, Any],
    verdict_v2_id: int | None = None,
    provider: str = "stub",
    model: str = "stub-v1",
    expires_at: datetime | None = None,
) -> AISummary:
    ca = normalize_ca(ca)
    stmt = select(AISummary).where(
        AISummary.ca == ca,
        AISummary.language == language,
        AISummary.provider == provider,
        AISummary.model == model,
    )
    row = await db.scalar(stmt)
    if row:
        row.verdict_v2_id = verdict_v2_id
        row.summary_text = summary_text
        row.summary_json = summary_json
        row.expires_at = expires_at
        row.updated_at = utc_now()
        return row
    row = AISummary(
        ca=ca,
        verdict_v2_id=verdict_v2_id,
        language=language,
        provider=provider,
        model=model,
        summary_text=summary_text,
        summary_json=summary_json,
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return row


async def get_cached_ai_summary(
    db: AsyncSession,
    *,
    ca: str,
    language: str = "en",
    provider: str = "stub",
    model: str = "stub-v1",
    now: datetime | None = None,
) -> AISummary | None:
    now = now or utc_now()
    stmt = select(AISummary).where(
        AISummary.ca == normalize_ca(ca),
        AISummary.language == language,
        AISummary.provider == provider,
        AISummary.model == model,
    )
    row = await db.scalar(stmt)
    if not row:
        return None
    if row.expires_at and ensure_aware(row.expires_at) <= now:
        return None
    return row


async def set_provider_cooldown(db: AsyncSession, *, provider: str, cooldown_until: datetime, reason: str = "") -> None:
    row = await db.get(ProviderCooldown, provider)
    if row:
        row.cooldown_until = cooldown_until
        row.reason = reason[:2000] if reason else None
        row.updated_at = utc_now()
    else:
        db.add(ProviderCooldown(provider=provider, cooldown_until=cooldown_until, reason=reason[:2000] if reason else None))


async def provider_available(db: AsyncSession, provider: str, *, now: datetime | None = None) -> bool:
    now = now or utc_now()
    row = await db.get(ProviderCooldown, provider)
    return not row or ensure_aware(row.cooldown_until) <= now


async def record_api_budget_event(
    db: AsyncSession,
    *,
    provider: str,
    endpoint: str = "",
    cost_units: int = 1,
    status_code: int | None = None,
) -> None:
    db.add(ApiBudgetEvent(provider=provider, endpoint=endpoint or None, cost_units=cost_units, status_code=status_code))


async def record_nitter_health_log(
    db: AsyncSession,
    *,
    base_url: str = "",
    status: str,
    detail: str = "",
    response_ms: int | None = None,
    item_count: int = 0,
) -> None:
    db.add(
        NitterHealthLog(
            base_url=base_url or None,
            status=status,
            detail=detail[:2000] if detail else None,
            response_ms=response_ms,
            item_count=int(item_count or 0),
        )
    )


async def record_socialdata_usage_log(
    db: AsyncSession,
    *,
    endpoint: str,
    query_hash: str = "",
    mode: str = "",
    result_count: int = 0,
    triggered_by_alpha: bool = False,
    alpha_reason: str = "",
) -> None:
    db.add(
        SocialDataUsageLog(
            endpoint=endpoint,
            query_hash=query_hash or None,
            mode=mode or None,
            result_count=int(result_count or 0),
            triggered_by_alpha=bool(triggered_by_alpha),
            alpha_reason=alpha_reason[:128] if alpha_reason else None,
        )
    )


async def get_api_budget_usage(
    db: AsyncSession,
    *,
    provider: str,
    since: datetime,
    endpoint: str | None = None,
) -> int:
    query = select(func.coalesce(func.sum(ApiBudgetEvent.cost_units), 0)).where(
        ApiBudgetEvent.provider == provider,
        ApiBudgetEvent.created_at >= since,
    )
    if endpoint:
        query = query.where(ApiBudgetEvent.endpoint == endpoint)
    value = await db.scalar(query)
    return int(value or 0)


async def audit_event(
    db: AsyncSession,
    *,
    event_type: str,
    tenant_id: int | None = None,
    user_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    db.add(AuditEvent(event_type=event_type, tenant_id=tenant_id, user_id=user_id, payload=payload))


async def get_bot_state(db: AsyncSession, key: str) -> str | None:
    row = await db.get(BotState, key)
    return row.value if row else None


async def set_bot_state(db: AsyncSession, key: str, value: str | dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)
    row = await db.get(BotState, key)
    if row:
        row.value = payload
        row.updated_at = utc_now()
    else:
        db.add(BotState(key=key, value=payload))


async def get_status_snapshot(db: AsyncSession) -> dict[str, Any]:
    active_tenants = await db.scalar(select(func.count()).select_from(Tenant).where(Tenant.status == "active"))
    total_launches = await db.scalar(select(func.count()).select_from(Launch))
    queued_rechecks = await db.scalar(select(func.count()).select_from(Launch).where(Launch.status == "queued_recheck"))
    signaled = await db.scalar(select(func.count()).select_from(Launch).where(Launch.status == "signaled"))
    skipped = await db.scalar(select(func.count()).select_from(Launch).where(Launch.status.in_(("skipped", "expired"))))
    signals = await db.scalar(select(func.count()).select_from(Signal))
    pending_deliveries = await db.scalar(select(func.count()).select_from(SignalDelivery).where(SignalDelivery.status == "pending"))
    retry_deliveries = await db.scalar(select(func.count()).select_from(SignalDelivery).where(SignalDelivery.status == "retry"))
    failed_deliveries = await db.scalar(select(func.count()).select_from(SignalDelivery).where(SignalDelivery.status == "failed"))
    cooldowns = await db.scalars(select(ProviderCooldown.provider).where(ProviderCooldown.cooldown_until > utc_now()))
    return {
        "tenants_active": int(active_tenants or 0),
        "launches_total": int(total_launches or 0),
        "queued_rechecks": int(queued_rechecks or 0),
        "launches_signaled": int(signaled or 0),
        "launches_skipped_or_expired": int(skipped or 0),
        "signals_total": int(signals or 0),
        "deliveries_pending": int(pending_deliveries or 0),
        "deliveries_retry": int(retry_deliveries or 0),
        "deliveries_failed": int(failed_deliveries or 0),
        "provider_cooldowns": list(cooldowns),
    }


async def cleanup_old_rows(
    db: AsyncSession,
    *,
    launch_before: datetime,
    api_budget_before: datetime,
    audit_before: datetime,
) -> dict[str, int]:
    api_deleted = (await db.execute(delete(ApiBudgetEvent).where(ApiBudgetEvent.created_at < api_budget_before))).rowcount or 0
    audit_deleted = (await db.execute(delete(AuditEvent).where(AuditEvent.created_at < audit_before))).rowcount or 0
    launch_rows = await db.scalars(
        select(Launch).where(
            Launch.created_at < launch_before,
            Launch.status.in_(("seeded", "expired", "skipped")),
        )
    )
    launches_trimmed = 0
    for launch in launch_rows:
        launch.raw_json = {"retained": False, "ca": launch.ca, "ticker": launch.ticker, "source": launch.source}
        launch.market_json = None
        launch.updated_at = utc_now()
        launches_trimmed += 1
    return {
        "api_budget_events_deleted": int(api_deleted),
        "audit_events_deleted": int(audit_deleted),
        "launch_payloads_trimmed": launches_trimmed,
    }
