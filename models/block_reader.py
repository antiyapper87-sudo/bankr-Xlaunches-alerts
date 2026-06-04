from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, JSONCompat, utc_now


class BlockScan(Base):
    __tablename__ = "block_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    pair_address: Mapped[str | None] = mapped_column(String(128), index=True)
    from_block: Mapped[int | None] = mapped_column(Integer)
    to_block: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="LOW", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="alchemy")
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("identity_key", "provider", name="uq_block_scan_identity_provider"),
        Index("ix_block_scans_status_updated", "status", "updated_at"),
    )


class TokenTransaction(Base):
    __tablename__ = "token_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    tx_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    block_number: Mapped[int | None] = mapped_column(Integer, index=True)
    tx_index: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    wallet_address: Mapped[str | None] = mapped_column(String(128), index=True)
    counterparty_address: Mapped[str | None] = mapped_column(String(128), index=True)
    pair_address: Mapped[str | None] = mapped_column(String(128), index=True)
    amount_token: Mapped[float | None] = mapped_column(Float)
    amount_native: Mapped[float | None] = mapped_column(Float)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    __table_args__ = (
        UniqueConstraint("chain", "tx_hash", "token_id", "event_type", "wallet_address", name="uq_token_tx_event_wallet"),
        Index("ix_token_transactions_identity_block", "identity_key", "block_number"),
    )


class WalletCluster(Base):
    __tablename__ = "wallet_clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    cluster_key: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    cluster_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    wallets_json: Mapped[list[str]] = mapped_column(JSONCompat, nullable=False, default=list)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class BundleSignal(Base):
    __tablename__ = "bundle_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    risk_score: Mapped[float] = mapped_column(Float, nullable=False)
    score_impact: Mapped[float] = mapped_column(Float, nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    detector_version: Mapped[str] = mapped_column(String(64), nullable=False, default="bundle-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        UniqueConstraint("identity_key", "signal_type", "detector_version", name="uq_bundle_signal_identity_type_version"),
        Index("ix_bundle_signal_severity_score", "severity", "risk_score"),
    )


class PrebuySignal(Base):
    __tablename__ = "prebuy_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    risk_score: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    detector_version: Mapped[str] = mapped_column(String(64), nullable=False, default="prebuy-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        UniqueConstraint("identity_key", "detector_version", name="uq_prebuy_signal_identity_version"),
    )


class HolderSnapshot(Base):
    __tablename__ = "holder_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    holder_count: Mapped[int | None] = mapped_column(Integer)
    top_1_pct: Mapped[float | None] = mapped_column(Float)
    top_5_pct: Mapped[float | None] = mapped_column(Float)
    top_10_pct: Mapped[float | None] = mapped_column(Float)
    dev_related_pct: Mapped[float | None] = mapped_column(Float)
    fresh_wallet_pct: Mapped[float | None] = mapped_column(Float)
    provider: Mapped[str | None] = mapped_column(String(64))
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    __table_args__ = (
        Index("ix_holder_snapshots_identity_time", "identity_key", "observed_at"),
    )


class LiquidityEvent(Base):
    __tablename__ = "liquidity_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    liquidity_change_pct: Mapped[float | None] = mapped_column(Float)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
