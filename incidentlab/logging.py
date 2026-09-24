from __future__ import annotations

import json
from datetime import UTC, datetime


def log_event(event: str, **fields) -> None:
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "component": "incidentlab",
        "event": event,
        **fields,
    }
    print(json.dumps(record, ensure_ascii=False, default=str), flush=True)
