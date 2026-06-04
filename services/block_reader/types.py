from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BlockRiskSummary:
    bundle_risk: float = 0.0
    prebuy_risk: float = 0.0
    dev_dump_risk: float = 0.0
    holder_concentration_risk: float = 0.0
    funding_quality: str = "unknown"
    confidence: str = "LOW"
    signals: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_risk": self.bundle_risk,
            "prebuy_risk": self.prebuy_risk,
            "dev_dump_risk": self.dev_dump_risk,
            "holder_concentration_risk": self.holder_concentration_risk,
            "funding_quality": self.funding_quality,
            "confidence": self.confidence,
            "signals": self.signals,
        }
