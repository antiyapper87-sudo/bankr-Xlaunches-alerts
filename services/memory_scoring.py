from __future__ import annotations

from typing import Any


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compute_score_adjustment(*, success_count: int, failure_count: int, rug_count: int, sample_size: int) -> tuple[float, float, str]:
    if sample_size <= 0:
        return 0.0, 0.0, "unknown"
    failure_rate = failure_count / sample_size
    success_rate = success_count / sample_size
    rug_rate = rug_count / sample_size
    confidence = clamp(min(1.0, sample_size / 10), 0.0, 1.0)
    adjustment = (success_rate * 8.0) - (failure_rate * 8.0) - (rug_rate * 6.0)
    label = "positive" if adjustment > 1 else "negative" if adjustment < -1 else "neutral"
    return clamp(adjustment, -12.0, 8.0), confidence, label


def bounded_memory_context(memories: list[Any]) -> dict[str, Any]:
    if not memories:
        return {"score_adjustment": 0.0, "confidence": 0.0, "matches": []}
    adjustment = clamp(sum(float(getattr(item, "normalized_json", {}).get("score_adjustment") or 0) for item in memories), -12.0, 8.0)
    confidence = max(float(getattr(item, "confidence", 0) or 0) for item in memories)
    return {
        "score_adjustment": adjustment,
        "confidence": confidence,
        "matches": [
            {
                "memory_type": item.memory_type,
                "insight": item.insight,
                "confidence": item.confidence,
            }
            for item in memories
        ],
        "risk_notes": [item.insight for item in memories if item.polarity == "negative"][:2],
        "positive_notes": [item.insight for item in memories if item.polarity == "positive"][:2],
    }
