from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, JSONCompat, utc_now


class VerdictV3(Base):
    __tablename__ = "verdict_v3"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False, default="base")
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="LOW", index=True)
    output_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False)
    human_readable: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="deterministic")
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="verdict-v3-shadow")
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="3.0")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="shadow", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    __table_args__ = (
        UniqueConstraint("identity_key", "input_hash", "version", name="uq_verdict_v3_identity_input_version"),
        Index("ix_verdict_v3_identity_created", "identity_key", "created_at"),
        Index("ix_verdict_v3_label_score", "label", "score"),
    )
