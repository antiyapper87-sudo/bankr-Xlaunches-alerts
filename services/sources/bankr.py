from __future__ import annotations

from services.sources.base import normalize_base_launch


class BankrAdapter:
    source_name = "bankr"
    chain = "base"
    source_priority = 80
    reliable_created_at = True
    creator_metadata_quality = "medium"

    async def fetch_new_launches(self, session):
        return []

    def normalize_launch(self, raw):
        return normalize_base_launch(raw, source=self.source_name, source_priority=self.source_priority)
