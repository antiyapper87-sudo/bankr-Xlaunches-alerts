from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TokenTransfer:
    tx_hash: str
    block_number: int
    log_index: int
    token: str
    from_address: str
    to_address: str
    amount_raw: int


@dataclass(slots=True)
class BuyerPosition:
    wallet: str
    first_buy_block: int
    first_buy_tx: str
    bought_raw: int = 0
    sold_raw: int = 0
    current_balance_raw: int = 0
    buy_count: int = 0
    sell_count: int = 0
    funding_source: str | None = None
    funding_confidence: float = 0.0

    @property
    def sold_ratio(self) -> float | None:
        if self.bought_raw <= 0:
            return None
        return max(0.0, min(1.0, self.sold_raw / self.bought_raw))


@dataclass(slots=True)
class EvidenceItem:
    type: str
    severity: str
    message: str
    tx_hash: str | None = None
    block_number: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BlockRiskSummary:
    bundle_risk: float = 0.0
    sniper_score: float = 0.0
    prebuy_risk: float = 0.0
    dev_dump_risk: float = 0.0
    liquidity_risk: float = 0.0
    holder_concentration_risk: float = 0.0
    funding_quality: str = "unknown"
    confidence: str = "LOW"
    pair_address: str = ""
    dex_type: str = "unknown"
    suspected_bundle_wallets_count: int = 0
    bundle_total_bought_pct: float | None = None
    bundle_current_held_pct: float | None = None
    bundle_sold_pct: float | None = None
    first_buyers_count: int = 0
    first_blocks_scanned: int = 0
    signals: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    raw_metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def overall_risk_score(self) -> float:
        return max(
            float(self.bundle_risk or 0),
            float(self.sniper_score or 0),
            float(self.prebuy_risk or 0),
            float(self.dev_dump_risk or 0),
            float(self.liquidity_risk or 0),
            float(self.holder_concentration_risk or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_risk": self.bundle_risk,
            "bundle_risk_score": self.bundle_risk,
            "sniper_score": self.sniper_score,
            "prebuy_risk": self.prebuy_risk,
            "dev_dump_risk": self.dev_dump_risk,
            "dev_risk_score": self.dev_dump_risk,
            "liquidity_risk": self.liquidity_risk,
            "liquidity_risk_score": self.liquidity_risk,
            "holder_concentration_risk": self.holder_concentration_risk,
            "holder_concentration_score": self.holder_concentration_risk,
            "overall_risk_score": self.overall_risk_score,
            "funding_quality": self.funding_quality,
            "confidence": self.confidence,
            "pair_address": self.pair_address,
            "dex_type": self.dex_type,
            "suspected_bundle_wallets_count": self.suspected_bundle_wallets_count,
            "bundle_total_bought_pct": self.bundle_total_bought_pct,
            "bundle_current_held_pct": self.bundle_current_held_pct,
            "bundle_sold_pct": self.bundle_sold_pct,
            "first_buyers_count": self.first_buyers_count,
            "first_blocks_scanned": self.first_blocks_scanned,
            "signals": self.signals,
            "evidence": [
                {
                    "type": item.type,
                    "severity": item.severity,
                    "message": item.message,
                    "tx_hash": item.tx_hash,
                    "block_number": item.block_number,
                    "data": item.data,
                }
                for item in self.evidence
            ],
            "raw_metrics": self.raw_metrics,
        }
