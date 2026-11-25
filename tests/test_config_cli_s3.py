import json
import os
from pathlib import Path

import pytest

from backup_tool import cli
from backup_tool.backends import BackendError, S3Backend, safe_key
from backup_tool.config import ConfigError, load
from backup_tool.engine import restore, run_once
from conftest import write


def make_config(tmp_path: Path, body: str = "") -> Path:
    (tmp_path / "src").mkdir(exist_ok=True)
    p = tmp_path / "backup.toml"
    p.write_text(f'source = "src"\nstate_dir = "state"\n[backend]\ntype = "folder"\npath = "remote"\n{body}', encoding="utf-8")
    return p


def test_config_loads_relative_paths(tmp_path):
    cfg = load(make_config(tmp_path, ""))
    assert cfg.source == (tmp_path / "src").resolve() and cfg.backend.path == (tmp_path / "remote").resolve()


@pytest.mark.parametrize("text,fragment", [
    ('state_dir = "s"\n', "Missing required setting 'source'"),
    ('source = "src"\nstate_dir = "s"\n[backend]\ntype = "ftp"\n', "Unknown backend type"),
    ('source = "src"\nstate_dir = "s"\n[backend]\ntype = "s3"\n', "backend.bucket"),
    ('source = "nope"\nstate_dir = "s"\n[backend]\npath = "r"\n', "doesn't exist"),
    ("source = [", "isn't valid TOML"),
])
def test_config_errors_are_specific(tmp_path, text, fragment):
    (tmp_path / "src").mkdir()
    p = tmp_path / "bad.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match=fragment):
        load(p)


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="doesn't exist"):
        load(tmp_path / "none.toml")


def test_cli_end_to_end(tmp_path, capsys):
    conf = make_config(tmp_path, "settle_seconds = 0\n")
    write(tmp_path / "src/note.txt", "hello")
    assert cli.main(["-c", str(conf), "run-once"]) == 0
    assert "1 uploaded, 0 failed" in capsys.readouterr().out
    assert cli.main(["-c", str(conf), "status"]) == 0
    assert "1 files tracked, 1 backed up, 0 waiting" in capsys.readouterr().out
    assert cli.main(["-c", str(conf), "restore", str(tmp_path / "back")]) == 0
    assert (tmp_path / "back/note.txt").read_text() == "hello"
    assert (tmp_path / "state/backup.log").read_text().count("uploaded note.txt.gz") == 1


def test_cli_reports_config_problems_with_exit_code_2(tmp_path, capsys):
    assert cli.main(["-c", str(tmp_path / "missing.toml"), "status"]) == 2
    assert "Config problem" in capsys.readouterr().err


@pytest.mark.parametrize("key", ["/abs", "a/../b", "a//b", "../x"])
def test_unsafe_keys_are_rejected(key):
    with pytest.raises(BackendError):
        safe_key(key)


boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket="backups")
        yield client


def test_s3_backend_round_trip_against_a_mock_server(cfg, s3, tmp_path):
    write(cfg.source / "docs/a.txt", "stored remotely")
    backend = S3Backend("backups", prefix="laptop", client=s3)
    assert run_once(cfg, backend).uploaded == 1
    assert backend.keys() == ["docs/a.txt.gz"]
    assert "laptop/docs/a.txt.gz" in [o["Key"] for o in s3.list_objects_v2(Bucket="backups")["Contents"]]
    count, problems = restore(cfg, tmp_path / "out", backend=backend)
    assert (count, problems) == (1, [])
    assert (tmp_path / "out/docs/a.txt").read_text() == "stored remotely"


def test_s3_failure_is_reported_as_backend_error(cfg, s3, tmp_path):
    backend = S3Backend("missing-bucket", client=s3)
    f = write(tmp_path / "x.gz", "x")
    with pytest.raises(BackendError):
        backend.put("x.gz", f, {})
