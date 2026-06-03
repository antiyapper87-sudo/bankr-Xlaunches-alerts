from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, JSONCompat, utc_now


class SpoofSignal(Base):
    __tablename__ = "spoof_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ca: Mapped[str] = mapped_column(ForeignKey("launches.ca"), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium", index=True)
    score_impact: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONCompat, nullable=False, default=dict)
    detector_version: Mapped[str] = mapped_column(String(64), nullable=False, default="spoof-detector-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        UniqueConstraint("ca", "signal_type", "detector_version", name="uq_spoof_signal_ca_type_version"),
        Index("ix_spoof_ca_severity", "ca", "severity"),
    )
