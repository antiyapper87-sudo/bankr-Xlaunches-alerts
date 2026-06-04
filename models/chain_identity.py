from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, JSONCompat, utc_now


class ChainTokenIdentity(Base):
    __tablename__ = "chain_token_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)

    ticker: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str | None] = mapped_column(String(256))
    launch_source: Mapped[str | None] = mapped_column(String(64), index=True)
    source_method: Mapped[str | None] = mapped_column(String(64))

    deployer_wallet: Mapped[str | None] = mapped_column(String(128), index=True)
    creator_handle: Mapped[str | None] = mapped_column(String(128), index=True)
    pair_address: Mapped[str | None] = mapped_column(String(128), index=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reliable_created_at: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    source_confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="low")
    raw_refs_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("chain", "token_id", name="uq_chain_token_identity"),
        Index("ix_chain_token_ticker_seen", "chain", "ticker", "first_seen_at"),
        Index("ix_chain_token_source_seen", "chain", "launch_source", "first_seen_at"),
        Index("ix_chain_token_deployer_seen", "chain", "deployer_wallet", "first_seen_at"),
    )
