from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, JSONCompat, utc_now


class UserWatchlist(Base):
    __tablename__ = "user_watchlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    ca: Mapped[str] = mapped_column(ForeignKey("launches.ca"), nullable=False, index=True)
    label: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    last_mcap: Mapped[float | None] = mapped_column(Float)
    last_volume: Mapped[float | None] = mapped_column(Float)
    last_liquidity: Mapped[float | None] = mapped_column(Float)
    last_price_usd: Mapped[str | None] = mapped_column(String(64))
    initial_mcap: Mapped[float | None] = mapped_column(Float)
    initial_volume: Mapped[float | None] = mapped_column(Float)
    initial_liquidity: Mapped[float | None] = mapped_column(Float)
    previous_mcap: Mapped[float | None] = mapped_column(Float)
    previous_volume: Mapped[float | None] = mapped_column(Float)
    last_mcap_change_pct: Mapped[float | None] = mapped_column(Float)
    last_volume_change_pct: Mapped[float | None] = mapped_column(Float)
    last_market_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCompat)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "ca", name="uq_user_watchlists_tenant_ca"),
        Index("ix_user_watchlists_status_checked", "status", "last_checked_at"),
    )


class UserFeedback(Base):
    __tablename__ = "user_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    ca: Mapped[str] = mapped_column(ForeignKey("launches.ca"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="telegram")
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCompat)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "ca", name="uq_user_feedback_tenant_ca"),
        Index("ix_user_feedback_action_created", "action", "created_at"),
    )
