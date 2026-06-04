from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, JSONCompat, utc_now


class TrackedWallet(Base):
    __tablename__ = "tracked_wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    label: Mapped[str | None] = mapped_column(String(128))
    chain: Mapped[str] = mapped_column(String(32), nullable=False, default="base", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    last_checked_block: Mapped[int | None] = mapped_column(Integer)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "address", "chain", name="uq_tracked_wallet_tenant_address_chain"),
        Index("ix_tracked_wallets_status_checked", "status", "last_checked_at"),
    )


class WalletEvent(Base):
    __tablename__ = "wallet_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tracked_wallet_id: Mapped[int] = mapped_column(ForeignKey("tracked_wallets.id"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    wallet_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    ca: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, default="erc20_transfer")
    amount: Mapped[float | None] = mapped_column(Float)
    amount_usd: Mapped[float | None] = mapped_column(Float)
    tx_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    block_number: Mapped[int | None] = mapped_column(Integer, index=True)
    event_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    __table_args__ = (
        UniqueConstraint("tx_hash", "wallet_address", "ca", "direction", name="uq_wallet_event_tx_wallet_ca_direction"),
        Index("ix_wallet_events_ca_created", "ca", "created_at"),
        Index("ix_wallet_events_wallet_created", "wallet_address", "created_at"),
    )
