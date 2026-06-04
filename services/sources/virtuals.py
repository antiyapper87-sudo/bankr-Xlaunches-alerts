from __future__ import annotations

from services.sources.base import normalize_base_launch


class VirtualsAdapter:
    source_name = "virtuals"
    chain = "base"
    source_priority = 80
    reliable_created_at = True
    creator_metadata_quality = "high"

    async def fetch_new_launches(self, session):
        return []

    def normalize_launch(self, raw):
        return normalize_base_launch(raw, source=self.source_name, source_priority=self.source_priority)
