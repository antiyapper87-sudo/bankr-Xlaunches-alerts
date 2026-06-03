from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, JSONCompat, utc_now


class TokenResearch(Base):
    __tablename__ = "token_research"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ca: Mapped[str] = mapped_column(ForeignKey("launches.ca"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    source: Mapped[str | None] = mapped_column(String(64), index=True)
    requested_by: Mapped[str] = mapped_column(String(32), nullable=False, default="pipeline")
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    processed_data: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("ca", "requested_by", name="uq_token_research_ca_requested_by"),
        Index("ix_token_research_status_created", "status", "created_at"),
    )


class HistoricalLaunch(Base):
    __tablename__ = "historical_launches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ca: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(256))
    source: Mapped[str | None] = mapped_column(String(64), index=True)
    deployer: Mapped[str | None] = mapped_column(String(128), index=True)
    launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    final_status: Mapped[str | None] = mapped_column(String(32), index=True)
    max_mcap: Mapped[float | None] = mapped_column(Float)
    max_volume: Mapped[float | None] = mapped_column(Float)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCompat)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("ca", name="uq_historical_launches_ca"),
        Index("ix_historical_ticker_seen", "ticker", "first_seen_at"),
        Index("ix_historical_deployer_seen", "deployer", "first_seen_at"),
    )
