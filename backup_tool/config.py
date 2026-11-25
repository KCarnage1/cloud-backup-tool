from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BackendConfig:
    type: str = "folder"
    path: Path | None = None
    bucket: str = ""
    endpoint_url: str | None = None
    prefix: str = ""


@dataclass(frozen=True)
class Config:
    source: Path
    state_dir: Path
    backend: BackendConfig
    interval_seconds: float = 300.0
    settle_seconds: float = 2.0
    retries: int = 5
    backoff_base: float = 1.0
    backoff_cap: float = 60.0
    exclude: tuple[str, ...] = field(default_factory=tuple)

    @property
    def log_file(self) -> Path:
        return self.state_dir / "backup.log"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"


def load(path: Path) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Config file {path} doesn't exist.") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} isn't valid TOML: {e}") from None

    base = Path(path).resolve().parent

    def rel(p: str) -> Path:
        q = Path(p).expanduser()
        return q if q.is_absolute() else (base / q).resolve()

    for key in ("source", "state_dir"):
        if key not in raw:
            raise ConfigError(f"Missing required setting '{key}'.")
    b = raw.get("backend", {})
    btype = b.get("type", "folder")
    if btype == "folder":
        if "path" not in b:
            raise ConfigError("backend.path is required for the folder backend.")
        backend = BackendConfig("folder", path=rel(b["path"]), prefix=b.get("prefix", ""))
    elif btype == "s3":
        if not b.get("bucket"):
            raise ConfigError("backend.bucket is required for the s3 backend.")
        backend = BackendConfig("s3", bucket=b["bucket"], endpoint_url=b.get("endpoint_url"), prefix=b.get("prefix", ""))
    else:
        raise ConfigError(f"Unknown backend type '{btype}'. Use 'folder' or 's3'.")

    cfg = Config(
        source=rel(raw["source"]),
        state_dir=rel(raw["state_dir"]),
        backend=backend,
        interval_seconds=float(raw.get("interval_seconds", 300)),
        settle_seconds=float(raw.get("settle_seconds", 2)),
        retries=int(raw.get("retries", 5)),
        backoff_base=float(raw.get("backoff_base", 1)),
        backoff_cap=float(raw.get("backoff_cap", 60)),
        exclude=tuple(raw.get("exclude", [])),
    )
    if cfg.interval_seconds < 1:
        raise ConfigError("interval_seconds must be at least 1.")
    if cfg.retries < 0:
        raise ConfigError("retries can't be negative.")
    if not cfg.source.is_dir():
        raise ConfigError(f"The source folder {cfg.source} doesn't exist.")
    return cfg
