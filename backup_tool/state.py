from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Entry:
    size: int
    mtime_ns: int
    sha256: str
    uploaded: bool = False
    attempts: int = 0
    last_error: str = ""


class State:
    """What has been seen and what has reached the backend, kept in one JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, Entry] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.entries = {k: Entry(**v) for k, v in data.get("entries", {}).items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": {k: asdict(v) for k, v in self.entries.items()}}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def pending(self) -> list[str]:
        return sorted(k for k, v in self.entries.items() if not v.uploaded)
