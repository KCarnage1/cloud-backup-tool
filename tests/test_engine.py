import gzip
import json
import os
from pathlib import Path

import pytest

from backup_tool.backends import Backend, BackendError, FolderBackend
from backup_tool.engine import compress, make_backend, restore, run_once, upload_with_retry
from backup_tool.state import State
from conftest import write


class Flaky(Backend):
    """Fails the first `fail_times` puts, then behaves like a folder."""

    def __init__(self, inner: Backend, fail_times: int):
        self.inner, self.fail_times, self.calls = inner, fail_times, 0

    def put(self, key, file, metadata):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise BackendError("connection reset")
        self.inner.put(key, file, metadata)

    def get(self, key, dest):
        return self.inner.get(key, dest)

    def keys(self):
        return self.inner.keys()


def no_sleep(_):
    pass


def test_new_files_are_compressed_and_uploaded(cfg):
    write(cfg.source / "a.txt", "hello " * 1000)
    write(cfg.source / "sub/b.txt", "world")
    report = run_once(cfg, sleep=no_sleep)
    assert (report.uploaded, report.failed) == (2, 0)
    remote = make_backend(cfg)
    assert remote.keys() == ["a.txt.gz", "sub/b.txt.gz"]
    stored = (cfg.backend.path / "a.txt.gz").read_bytes()
    assert len(stored) < len("hello " * 1000)
    assert gzip.decompress(stored).decode() == "hello " * 1000


def test_second_pass_uploads_nothing_until_a_file_changes(cfg):
    f = write(cfg.source / "a.txt", "one")
    run_once(cfg, sleep=no_sleep)
    assert run_once(cfg, sleep=no_sleep).uploaded == 0
    write(f, "two", age=30)
    assert run_once(cfg, sleep=no_sleep).uploaded == 1
    assert gzip.decompress((cfg.backend.path / "a.txt.gz").read_bytes()) == b"two"


def test_touching_a_file_without_changing_it_does_not_reupload(cfg):
    f = write(cfg.source / "a.txt", "same")
    run_once(cfg, sleep=no_sleep)
    os.utime(f, (1_000_000_000, 1_000_000_000))
    assert run_once(cfg, sleep=no_sleep).uploaded == 0


def test_retry_succeeds_after_transient_failures(cfg):
    write(cfg.source / "a.txt", "data")
    backend = Flaky(FolderBackend(cfg.backend.path), fail_times=2)
    delays = []
    report = run_once(cfg, backend, sleep=delays.append)
    assert report.ok and backend.calls == 3
    assert len(delays) == 2 and delays[1] > delays[0] * 0.5


def test_failed_upload_stays_queued_and_succeeds_next_pass(cfg):
    write(cfg.source / "a.txt", "data")
    backend = Flaky(FolderBackend(cfg.backend.path), fail_times=cfg.retries + 1)
    first = run_once(cfg, backend, sleep=no_sleep)
    assert (first.uploaded, first.failed) == (0, 1)
    state = State(cfg.state_file)
    assert state.pending() == ["a.txt"] and "connection reset" in state.entries["a.txt"].last_error
    second = run_once(cfg, backend, sleep=no_sleep)
    assert second.uploaded == 1 and State(cfg.state_file).pending() == []


def test_backoff_is_capped(cfg):
    from dataclasses import replace
    c = replace(cfg, retries=6, backoff_base=10, backoff_cap=15)
    delays = []
    f = write(cfg.source / "x", "x")
    err = upload_with_retry(Flaky(FolderBackend(cfg.backend.path), 99), "x.gz", f, {}, c, delays.append)
    assert err == "connection reset" and len(delays) == 6 and max(delays) <= 15


def test_files_modified_just_now_wait_for_the_next_pass(cfg):
    from dataclasses import replace
    write(cfg.source / "busy.txt", "half writ", age=0)
    c = replace(cfg, settle_seconds=5)
    assert run_once(c, sleep=no_sleep).uploaded == 0
    os.utime(cfg.source / "busy.txt", (1, 1))
    assert run_once(c, sleep=no_sleep).uploaded == 1


def test_excluded_files_and_symlinks_are_ignored(cfg):
    from dataclasses import replace
    write(cfg.source / "keep.txt", "k")
    write(cfg.source / "skip.log", "s")
    write(cfg.source / "cache/tmp.bin", "c")
    outside = write(cfg.source.parent / "secret.txt", "do not upload")
    (cfg.source / "link.txt").symlink_to(outside)
    c = replace(cfg, exclude=("*.log", "cache/*"))
    run_once(c, sleep=no_sleep)
    assert make_backend(c).keys() == ["keep.txt.gz"]


def test_file_deleted_before_upload_is_dropped(cfg):
    f = write(cfg.source / "a.txt", "x")
    from backup_tool.scanner import scan
    state = State(cfg.state_file)
    scan(cfg.source, state, (), 0)
    state.save()
    f.unlink()
    assert run_once(cfg, sleep=no_sleep).uploaded == 0
    assert State(cfg.state_file).pending() == []


def test_restore_round_trip_with_checksum(cfg, tmp_path):
    write(cfg.source / "docs/a.txt", "alpha")
    write(cfg.source / "b.bin", "bravo")
    run_once(cfg, sleep=no_sleep)
    out = tmp_path / "out"
    count, problems = restore(cfg, out)
    assert (count, problems) == (2, [])
    assert (out / "docs/a.txt").read_text() == "alpha"
    count, _ = restore(cfg, tmp_path / "only", only="docs")
    assert count == 1 and not (tmp_path / "only/b.bin").exists()


def test_restore_rejects_a_corrupted_object(cfg, tmp_path):
    write(cfg.source / "a.txt", "precious")
    run_once(cfg, sleep=no_sleep)
    bad = cfg.backend.path / "a.txt.gz"
    bad.write_bytes(gzip.compress(b"tampered"))
    count, problems = restore(cfg, tmp_path / "out")
    assert count == 0 and "checksum mismatch" in problems[0]
    assert not (tmp_path / "out/a.txt").exists()


def test_restore_refuses_keys_that_escape_the_folder(cfg, tmp_path):
    (cfg.backend.path).mkdir(parents=True)
    class Evil(FolderBackend):
        def keys(self):
            return ["../escape.txt.gz"]
    count, problems = restore(cfg, tmp_path / "out", backend=Evil(cfg.backend.path))
    assert count == 0 and problems and not (tmp_path / "escape.txt").exists()


def test_compression_is_deterministic(tmp_path):
    f = write(tmp_path / "f.txt", "same content")
    compress(f, tmp_path / "1.gz")
    os.utime(f, (5, 5))
    compress(f, tmp_path / "2.gz")
    assert (tmp_path / "1.gz").read_bytes() == (tmp_path / "2.gz").read_bytes()


def test_broken_storage_location_is_a_failed_pass_not_a_crash(cfg):
    write(cfg.source / "a.txt", "data")
    cfg.backend.path.write_text("a file where the backup folder should be")
    report = run_once(cfg, sleep=no_sleep)
    assert (report.uploaded, report.failed) == (0, 1)
    state = State(cfg.state_file)
    assert state.pending() == ["a.txt"] and state.entries["a.txt"].last_error
    cfg.backend.path.unlink()
    assert run_once(cfg, sleep=no_sleep).uploaded == 1
    assert State(cfg.state_file).pending() == []


def test_unreadable_source_file_fails_that_file_only(cfg, monkeypatch):
    write(cfg.source / "ok.txt", "fine")
    write(cfg.source / "locked.txt", "secret")
    from backup_tool import engine
    from backup_tool.scanner import scan, sha256_of
    state = State(cfg.state_file)
    scan(cfg.source, state, (), 0)
    state.save()

    def flaky_hash(path):
        if path.name == "locked.txt":
            raise PermissionError(13, "Permission denied")
        return sha256_of(path)

    monkeypatch.setattr(engine, "sha256_of", flaky_hash)
    report = run_once(cfg, sleep=no_sleep)
    assert report.uploaded == 1 and report.failed == 1
    assert "can't read locked.txt" in State(cfg.state_file).entries["locked.txt"].last_error
