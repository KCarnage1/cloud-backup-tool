from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .backends import BackendError
from .config import Config, ConfigError, load
from .engine import restore, run_once
from .state import State


def setup_logging(cfg: Config, verbose: bool) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("backup")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (RotatingFileHandler(cfg.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        root.addHandler(h)


def cmd_run(cfg: Config, _args) -> int:
    report = run_once(cfg)
    print(f"{report.uploaded} uploaded, {report.failed} failed.")
    return 0 if report.ok else 1


def cmd_watch(cfg: Config, _args) -> int:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log = logging.getLogger("backup")
    log.info("watching %s every %.0fs", cfg.source, cfg.interval_seconds)
    while not stop.is_set():
        try:
            run_once(cfg, sleep=lambda s: stop.wait(s))
        except (BackendError, OSError) as e:
            log.error("pass failed: %s", e)
        stop.wait(cfg.interval_seconds)
    log.info("stopped")
    return 0


def cmd_status(cfg: Config, _args) -> int:
    state = State(cfg.state_file)
    total = len(state.entries)
    pending = state.pending()
    print(f"{total} files tracked, {total - len(pending)} backed up, {len(pending)} waiting.")
    for rel in pending:
        e = state.entries[rel]
        print(f"  waiting: {rel}  (attempts: {e.attempts}{', last error: ' + e.last_error if e.last_error else ''})")
    return 0


def cmd_restore(cfg: Config, args) -> int:
    count, problems = restore(cfg, Path(args.dest), only=args.only)
    print(f"Restored {count} file{'s' if count != 1 else ''} into {args.dest}.")
    for p in problems:
        print(f"  problem: {p}", file=sys.stderr)
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="backup-tool", description="Compress changed files and upload them, retrying failures.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-c", "--config", default="backup.toml", help="path to the TOML config (default: backup.toml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("run-once", help="one pass: scan, compress, upload").set_defaults(fn=cmd_run)
    sub.add_parser("watch", help="repeat passes on the configured interval until stopped").set_defaults(fn=cmd_watch)
    sub.add_parser("status", help="show what is backed up and what is waiting").set_defaults(fn=cmd_status)
    r = sub.add_parser("restore", help="download and decompress backed-up files")
    r.add_argument("dest", help="folder to restore into")
    r.add_argument("--only", help="restore just this file or folder (relative path)")
    r.set_defaults(fn=cmd_restore)
    args = ap.parse_args(argv)
    try:
        cfg = load(Path(args.config))
        setup_logging(cfg, args.verbose)
        return args.fn(cfg, args)
    except ConfigError as e:
        print(f"Config problem: {e}", file=sys.stderr)
        return 2
    except BackendError as e:
        print(f"Storage problem: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
