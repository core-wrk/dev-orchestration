"""Append-only machine-readable run timeline."""

import json
from datetime import UTC, datetime
from pathlib import Path


def append_event(path: Path, event: dict) -> None:
    record = {"ts": datetime.now(UTC).isoformat(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
