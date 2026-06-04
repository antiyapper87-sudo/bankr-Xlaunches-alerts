from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, JSONCompat, utc_now


class AgentMemory(Base):
    __tablename__ = "agent_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    memory_key: Mapped[str] = mapped_column(String(220), nullable=False, unique=True)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    chain: Mapped[str | None] = mapped_column(String(32), index=True)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    insight: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    polarity: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    reviewed_by_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_agent_memory_lookup", "subject_type", "subject_id", "status"),
        Index("ix_agent_memory_type_conf", "memory_type", "confidence"),
        Index("ix_agent_memory_expiry", "status", "expires_at"),
    )


class PatternMemory(Base):
    __tablename__ = "pattern_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pattern_key: Mapped[str] = mapped_column(String(220), nullable=False, unique=True)
    pattern_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    chain: Mapped[str | None] = mapped_column(String(32), index=True)
    entity_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rug_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pump_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_initial_score: Mapped[float | None] = mapped_column(Float)
    avg_max_mcap_24h: Mapped[float | None] = mapped_column(Float)
    avg_return_24h_pct: Mapped[float | None] = mapped_column(Float)
    score_adjustment: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)


class DevWalletProfile(Base):
    __tablename__ = "dev_wallet_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    wallet_address: Mapped[str] = mapped_column(String(128), nullable=False)
    wallet_key: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    launches_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signaled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    winner_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rug_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    median_max_mcap_24h: Mapped[float | None] = mapped_column(Float)
    best_mcap: Mapped[float | None] = mapped_column(Float)
    avg_liquidity_removed_pct: Mapped[float | None] = mapped_column(Float)
    repeated_tickers_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    recent_launches_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONCompat, nullable=False, default=list)
    risk_label: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown", index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    __table_args__ = (
        UniqueConstraint("chain", "wallet_address", name="uq_dev_wallet_chain_address"),
        Index("ix_dev_wallet_risk_conf", "risk_label", "confidence"),
    )


class NarrativePattern(Base):
    __tablename__ = "narrative_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str | None] = mapped_column(String(32), index=True)
    narrative_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    examples_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    winners_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failures_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rugs_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_social_score: Mapped[float | None] = mapped_column(Float)
    avg_bundle_risk: Mapped[float | None] = mapped_column(Float)
    avg_return_24h_pct: Mapped[float | None] = mapped_column(Float)
    score_adjustment: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("chain", "narrative_key", name="uq_narrative_pattern_chain_key"),
    )


class SocialAccountPattern(Base):
    __tablename__ = "social_account_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="x")
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    account_key: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    mentions_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ca_confirmed_mentions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    winner_mentions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_mentions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suspected_paid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    template_reuse_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reliability_score: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    shill_risk_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)

    __table_args__ = (
        UniqueConstraint("platform", "username", name="uq_social_account_platform_username"),
        Index("ix_social_account_reliability", "reliability_score", "shill_risk_score"),
    )
