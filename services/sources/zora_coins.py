from __future__ import annotations

from services.sources.base import normalize_base_launch


class ZoraCoinsAdapter:
    source_name = "zora"
    chain = "base"
    source_priority = 80
    reliable_created_at = True
    creator_metadata_quality = "high"
    requires_market_confirmation = True

    async def fetch_new_launches(self, session):
        return []

    def normalize_launch(self, raw):
        candidate = normalize_base_launch(raw, source=self.source_name, source_priority=self.source_priority)
        if candidate:
            candidate.raw["coin_type"] = raw.get("coin_type") or raw.get("type") or ""
            candidate.raw["requires_market_confirmation"] = True
        return candidate
