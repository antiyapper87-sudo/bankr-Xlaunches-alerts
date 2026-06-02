from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("whale-alert")


def correlation_id(source: str, ca: str) -> str:
    return f"{source}:{(ca or '').lower()}"


def log_event(event: str, **fields: Any) -> None:
    log.info(
        json.dumps(
            {
                "event": event,
                "ts": datetime.now(timezone.utc).isoformat(),
                **fields,
            },
            ensure_ascii=False,
            default=str,
        )
    )
