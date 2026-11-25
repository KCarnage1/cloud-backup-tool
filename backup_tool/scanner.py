from __future__ import annotations

import fnmatch
import hashlib
import logging
import time
from pathlib import Path

from .state import Entry, State

log = logging.getLogger("backup")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(source: Path, state: State, exclude: tuple[str, ...], settle_seconds: float, now: float | None = None) -> list[str]:
    """Update the state for new or changed files and return the relative paths that need uploading.

    Symlinks are skipped so a link can't pull in files from outside the source folder.
    Files modified within the settle window are left for the next pass, since they may still be mid-write.
    """
    now = time.time() if now is None else now
    seen: set[str] = set()
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                log.debug("skipping symlink %s", path)
            continue
        rel = path.relative_to(source).as_posix()
        if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(path.name, pat) for pat in exclude):
            continue
        seen.add(rel)
        st = path.stat()
        old = state.entries.get(rel)
        if old and old.size == st.st_size and old.mtime_ns == st.st_mtime_ns:
            continue
        if now - st.st_mtime < settle_seconds:
            log.info("%s changed a moment ago, waiting for the next pass", rel)
            continue
        digest = sha256_of(path)
        if old and old.sha256 == digest:
            old.size, old.mtime_ns = st.st_size, st.st_mtime_ns
            continue
        state.entries[rel] = Entry(size=st.st_size, mtime_ns=st.st_mtime_ns, sha256=digest)
        log.info("queued %s (%d bytes)", rel, st.st_size)
    for gone in set(state.entries) - seen:
        if not state.entries[gone].uploaded:
            log.warning("%s disappeared before it was uploaded; dropping it from the queue", gone)
            del state.entries[gone]
    return state.pending()
