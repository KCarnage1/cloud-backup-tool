from __future__ import annotations

import gzip
import hashlib
import logging
import random
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .backends import Backend, BackendError, FolderBackend, S3Backend, safe_key
from .config import Config
from .scanner import scan, sha256_of
from .state import State

log = logging.getLogger("backup")


@dataclass
class Report:
    uploaded: int = 0
    failed: int = 0
    skipped_unchanged: int = 0

    @property
    def ok(self) -> bool:
        return self.failed == 0


def make_backend(cfg: Config) -> Backend:
    b = cfg.backend
    if b.type == "folder":
        assert b.path is not None
        return FolderBackend(b.path, b.prefix)
    return S3Backend(b.bucket, b.prefix, b.endpoint_url)


def compress(src: Path, dest: Path) -> None:
    with src.open("rb") as fin, dest.open("wb") as raw:
        # mtime=0 keeps the archive byte-identical for identical content
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            shutil.copyfileobj(fin, gz, 1024 * 1024)


def upload_with_retry(backend: Backend, key: str, file: Path, meta: dict[str, str], cfg: Config,
                      sleep: Callable[[float], None] = time.sleep) -> str | None:
    """Try an upload up to retries+1 times with capped exponential backoff and jitter. Returns the last error, or None on success."""
    error = ""
    for attempt in range(cfg.retries + 1):
        try:
            backend.put(key, file, meta)
            return None
        except BackendError as e:
            error = str(e)
            if attempt == cfg.retries:
                break
            delay = min(cfg.backoff_cap, cfg.backoff_base * 2**attempt) * random.uniform(0.5, 1.0)
            log.warning("upload of %s failed (%s); retrying in %.1fs (%d/%d)", key, error, delay, attempt + 1, cfg.retries)
            sleep(delay)
    return error


def run_once(cfg: Config, backend: Backend | None = None, sleep: Callable[[float], None] = time.sleep,
             now: float | None = None) -> Report:
    backend = backend or make_backend(cfg)
    state = State(cfg.state_file)
    report = Report()
    todo = scan(cfg.source, state, cfg.exclude, cfg.settle_seconds, now)
    state.save()
    with tempfile.TemporaryDirectory(prefix="backup-") as tmp:
        for rel in todo:
            entry = state.entries[rel]
            src = cfg.source / rel
            if not src.is_file() or sha256_of(src) != entry.sha256:
                log.info("%s changed while queued; it will be picked up on the next pass", rel)
                del state.entries[rel]
                continue
            gz = Path(tmp) / "payload.gz"
            compress(src, gz)
            key = safe_key(rel + ".gz")
            err = upload_with_retry(backend, key, gz, {"sha256": entry.sha256, "size": str(entry.size)}, cfg, sleep)
            entry.attempts += 1
            if err is None:
                entry.uploaded, entry.last_error = True, ""
                report.uploaded += 1
                log.info("uploaded %s (%d -> %d bytes)", key, entry.size, gz.stat().st_size)
            else:
                entry.last_error = err
                report.failed += 1
                log.error("giving up on %s for this pass: %s (will retry next pass)", key, err)
            state.save()
    return report


def restore(cfg: Config, dest: Path, backend: Backend | None = None, only: str | None = None) -> tuple[int, list[str]]:
    """Download and decompress backed-up files into dest. Returns (restored count, problems)."""
    backend = backend or make_backend(cfg)
    dest = dest.resolve()
    restored, problems = 0, []
    with tempfile.TemporaryDirectory(prefix="restore-") as tmp:
        for key in backend.keys():
            rel = key[:-3]
            if only and not (rel == only or rel.startswith(only.rstrip("/") + "/")):
                continue
            try:
                target = (dest / safe_key(rel)).resolve()
                if dest not in target.parents:
                    problems.append(f"{key}: would land outside the restore folder")
                    continue
                payload = Path(tmp) / "payload.gz"
                meta = backend.get(key, payload)
                target.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                with gzip.open(payload, "rb") as fin, target.open("wb") as fout:
                    for chunk in iter(lambda: fin.read(1024 * 1024), b""):
                        h.update(chunk)
                        fout.write(chunk)
                if meta.get("sha256") and meta["sha256"] != h.hexdigest():
                    target.unlink(missing_ok=True)
                    problems.append(f"{key}: checksum mismatch, file not restored")
                    continue
                restored += 1
            except (BackendError, OSError, EOFError, gzip.BadGzipFile) as e:
                problems.append(f"{key}: {e}")
    return restored, problems
