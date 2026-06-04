from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, JSONCompat, utc_now


class ProjectLore(Base):
    __tablename__ = "project_lore"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)

    category: Mapped[str | None] = mapped_column(String(64), index=True)
    utility: Mapped[str | None] = mapped_column(Text)
    mechanics: Mapped[str | None] = mapped_column(Text)
    target_users: Mapped[str | None] = mapped_column(Text)
    meme_lore_hook: Mapped[str | None] = mapped_column(Text)
    founder_identity: Mapped[str | None] = mapped_column(Text)
    community_quality: Mapped[str | None] = mapped_column(Text)

    narrative_summary: Mapped[str] = mapped_column(Text, nullable=False)
    why_it_matters: Mapped[str] = mapped_column(Text, nullable=False)
    lore_bullets_json: Mapped[list[dict[str, Any]] | list[str]] = mapped_column(JSONCompat, nullable=False, default=list)

    attribution_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown", index=True)
    attribution_confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="LOW", index=True)
    ca_confirmed_evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    project_confirmed_evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ticker_context_evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    evidence_refs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONCompat, nullable=False, default=list)
    extraction_version: Mapped[str] = mapped_column(String(64), nullable=False, default="lore-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("identity_key", "extraction_version", name="uq_project_lore_identity_version"),
        Index("ix_project_lore_category_conf", "category", "attribution_confidence"),
    )


class LoreEvidence(Base):
    __tablename__ = "lore_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_lore_id: Mapped[int | None] = mapped_column(Integer, index=True)
    identity_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(128))
    excerpt_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    short_excerpt: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    __table_args__ = (
        Index("ix_lore_evidence_identity_type", "identity_key", "evidence_type"),
    )
