from __future__ import annotations

from services.sources.base import normalize_base_launch


class CoinGeckoAdapter:
    source_name = "coingecko"
    chain = "base"
    source_priority = 60
    reliable_created_at = False
    creator_metadata_quality = "low"

    async def fetch_new_launches(self, session):
        return []

    def normalize_launch(self, raw):
        return normalize_base_launch(raw, source=self.source_name, source_priority=self.source_priority)
