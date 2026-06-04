from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, JSONCompat


class TokenOutcome(Base):
    __tablename__ = "token_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)

    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    initial_verdict_version: Mapped[str | None] = mapped_column(String(64))
    initial_score: Mapped[float | None] = mapped_column(Float)
    initial_label: Mapped[str | None] = mapped_column(String(32))

    launch_source: Mapped[str | None] = mapped_column(String(64), index=True)
    ticker: Mapped[str | None] = mapped_column(String(64), index=True)
    deployer_wallet: Mapped[str | None] = mapped_column(String(128), index=True)
    pair_address: Mapped[str | None] = mapped_column(String(128), index=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    snapshot_1h_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCompat)
    snapshot_4h_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCompat)
    snapshot_24h_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCompat)
    snapshot_7d_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCompat)

    max_mcap_1h: Mapped[float | None] = mapped_column(Float)
    max_mcap_4h: Mapped[float | None] = mapped_column(Float)
    max_mcap_24h: Mapped[float | None] = mapped_column(Float)
    max_mcap_7d: Mapped[float | None] = mapped_column(Float)

    max_volume_24h: Mapped[float | None] = mapped_column(Float)
    min_liquidity_24h: Mapped[float | None] = mapped_column(Float)
    max_liquidity_24h: Mapped[float | None] = mapped_column(Float)

    mcap_change_1h_pct: Mapped[float | None] = mapped_column(Float)
    mcap_change_4h_pct: Mapped[float | None] = mapped_column(Float)
    mcap_change_24h_pct: Mapped[float | None] = mapped_column(Float)

    liquidity_removed_pct_24h: Mapped[float | None] = mapped_column(Float)
    holder_change_24h_pct: Mapped[float | None] = mapped_column(Float)

    rug_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    dump_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    pump_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    dead_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    final_outcome_label: Mapped[str | None] = mapped_column(String(32), index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="tracking", index=True)

    __table_args__ = (
        UniqueConstraint("chain", "token_id", name="uq_token_outcomes_chain_token"),
        Index("ix_outcomes_due", "status", "next_check_at"),
        Index("ix_outcomes_source_label", "launch_source", "final_outcome_label"),
        Index("ix_outcomes_deployer_label", "deployer_wallet", "final_outcome_label"),
        Index("ix_outcomes_ticker_seen", "chain", "ticker", "first_seen_at"),
    )
