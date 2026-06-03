from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, JSONCompat, utc_now


class VerdictV2(Base):
    __tablename__ = "verdict_v2"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ca: Mapped[str] = mapped_column(ForeignKey("launches.ca"), nullable=False, index=True)
    research_id: Mapped[int | None] = mapped_column(ForeignKey("token_research.id"))
    score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    score_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False)
    verdict_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False)
    human_readable: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="verdict-v2.0")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    __table_args__ = (
        Index("ix_verdict_v2_ca_created", "ca", "created_at"),
        Index("ix_verdict_v2_label_score", "label", "score"),
    )


class AISummary(Base):
    __tablename__ = "ai_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ca: Mapped[str] = mapped_column(ForeignKey("launches.ca"), nullable=False, index=True)
    verdict_v2_id: Mapped[int | None] = mapped_column(ForeignKey("verdict_v2.id"))
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="stub")
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="stub-v1")
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("ca", "language", "provider", "model", name="uq_ai_summary_ca_lang_provider_model"),
    )
