from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(slots=True)
class LaunchCandidate:
    chain: str
    token_id: str
    ticker: str
    name: str
    source: str
    source_method: str = ""
    source_created_at: datetime | None = None
    reliable_created_at: bool = False
    deployer_wallet: str = ""
    creator_handle: str = ""
    pair_address: str = ""
    website_url: str = ""
    tweet_url: str = ""
    image_uri: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    market_hint: dict[str, Any] | None = None


class LaunchSourceAdapter(Protocol):
    source_name: str
    chain: str
    source_priority: int
    reliable_created_at: bool
    creator_metadata_quality: str

    async def fetch_new_launches(self, session) -> list[dict[str, Any]]: ...
    def normalize_launch(self, raw: dict[str, Any]) -> LaunchCandidate | None: ...


def normalize_base_launch(raw: dict[str, Any], *, source: str, source_priority: int = 60) -> LaunchCandidate | None:
    token_id = str(raw.get("address") or raw.get("ca") or raw.get("token_id") or "").lower()
    if not token_id.startswith("0x") or len(token_id) != 42:
        return None
    return LaunchCandidate(
        chain="base",
        token_id=token_id,
        ticker=str(raw.get("symbol") or raw.get("ticker") or "").lstrip("$"),
        name=str(raw.get("name") or raw.get("token_name") or ""),
        source=source,
        source_method=str(raw.get("source_method") or raw.get("method") or ""),
        source_created_at=raw.get("created_at") if isinstance(raw.get("created_at"), datetime) else None,
        reliable_created_at=bool(raw.get("reliable_created_at")),
        deployer_wallet=str(raw.get("deployer_wallet") or raw.get("msg_sender") or "").lower(),
        creator_handle=str(raw.get("x_username") or raw.get("creator_handle") or "").lstrip("@"),
        pair_address=str(raw.get("pair_address") or "").lower(),
        website_url=str(raw.get("website_url") or ""),
        tweet_url=str(raw.get("tweet_url") or ""),
        image_uri=str(raw.get("image_uri") or ""),
        raw={**raw, "source_priority": source_priority},
        market_hint=raw.get("market_hint") if isinstance(raw.get("market_hint"), dict) else None,
    )
