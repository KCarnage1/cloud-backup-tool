import os
import time
from pathlib import Path

import pytest

from backup_tool.config import BackendConfig, Config


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    src = tmp_path / "src"
    src.mkdir()
    return Config(
        source=src,
        state_dir=tmp_path / "state",
        backend=BackendConfig("folder", path=tmp_path / "remote"),
        settle_seconds=0,
        retries=3,
        backoff_base=0.01,
    )


def write(path: Path, text: str, age: float = 60) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    t = time.time() - age
    os.utime(path, (t, t))
    return path
