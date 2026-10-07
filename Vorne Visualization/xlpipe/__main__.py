"""Command line.

    python -m xlpipe refresh                       all devices, all channels
    python -m xlpipe refresh --device FR8 --channel shift_hour
    python -m xlpipe refresh --record fixtures/    also save raw responses
    python -m xlpipe report                        what the cache holds
    python -m xlpipe live [--interval S]           the live writer, in the foreground

refresh is single-instance: a second concurrent run exits immediately
rather than starting a second backfill.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

import yaml

from .cache import SQLiteCache
from .channels import SPECS
from .client import RecordingClient, XLClient
from .sync import SyncSettings, sync_all


class AlreadyRunning(RuntimeError):
    pass


class RunLock:
    """OS-level exclusive lock on a file; released automatically if the
    process dies, so a crash never leaves a stale lock behind."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fh = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.fh.close()
            raise AlreadyRunning(f"another refresh holds {self.path}") from exc
        return self

    def __exit__(self, *exc) -> None:
        if self.fh:
            self.fh.close()


def load_config(path: str) -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    unknown = [c for c in cfg.get("channels", []) if c not in SPECS]
    if unknown:
        raise SystemExit(f"{path}: unknown channel(s) {unknown}; known: {sorted(SPECS)}")
    return cfg


def cmd_refresh(args, cfg) -> int:
    devices = cfg["devices"]
    if args.device:
        devices = [d for d in devices if d["id"] in args.device]
        if not devices:
            raise SystemExit(f"no configured device matches {args.device}")
    channels = args.channel or cfg.get("channels") or list(SPECS)
    settings = SyncSettings(**(cfg.get("sync") or {}))
    cache_path = Path(cfg.get("cache_path", "cache/xl_cache.db"))

    factory = None
    if args.record:
        factory = lambda host: RecordingClient(XLClient(host, timeout=60.0), Path(args.record) / host)  # noqa: E731

    with RunLock(cache_path.with_suffix(".lock")):
        store = SQLiteCache(cache_path)
        try:
            store.apply_epochs(cfg.get("epochs") or {})
            results = sync_all(devices, channels, store, settings, client_factory=factory)
            restamped = store.restamp_epochs()
            if restamped:
                logging.info("config_epoch re-stamped on %d row(s)", restamped)
            logging.info("part_standard refreshed for %d part(s)", store.refresh_part_standard())
        finally:
            store.close()

    summary = [asdict(r) for r in results]
    (cache_path.parent / "last_refresh.json").write_text(json.dumps(summary, indent=1, default=str))
    for d in results:
        if d.error:
            print(f"{d.device_id:6} {d.error}")
            continue
        for c in d.channels:
            status = c.error or (f"{c.upserted} upserted, {c.open_skipped} open skipped, "
                                 f"{c.quarantined} quarantined, {c.flagged_deleted} flagged deleted")
            print(f"{d.device_id:6} {c.channel:14} fetched {c.fetched:6}  {status}  ({c.seconds:.1f}s)")
    failed = any(d.error or any(c.error for c in d.channels) for d in results)
    return 1 if failed else 0


def live_lock(cfg: dict) -> RunLock:
    """One live writer at a time, whether standalone or inside the app."""
    return RunLock(Path(cfg.get("cache_path", "cache/xl_cache.db")).with_suffix(".live.lock"))


def cmd_live(args, cfg) -> int:
    from . import live

    interval, timeout = live.settings(cfg)
    interval = args.interval or interval
    with live_lock(cfg):
        stop, threads = live.start(Path(cfg.get("cache_path", "cache/xl_cache.db")), cfg["devices"],
                                   interval=interval, timeout=timeout)
        logging.info("live writer: %d devices every %.0f s; Ctrl+C to stop", len(threads), interval)
        try:
            while any(t.is_alive() for t in threads):
                stop.wait(1.0)
        except KeyboardInterrupt:
            stop.set()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m xlpipe")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh", help="pull closed events from the devices into the cache")
    r.add_argument("--device", action="append", help="device id; repeatable")
    r.add_argument("--channel", action="append", choices=sorted(SPECS), help="repeatable")
    r.add_argument("--record", metavar="DIR", help="also save every raw response (test fixtures)")
    sub.add_parser("report", help="summarise what the cache holds")
    lv = sub.add_parser("live", help="poll every device's real-time data into current_state")
    lv.add_argument("--interval", type=float, help="seconds between reads of each device")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(args.config)
    if args.cmd == "refresh":
        try:
            return cmd_refresh(args, cfg)
        except AlreadyRunning as exc:
            print(exc)
            return 3
    if args.cmd == "live":
        try:
            return cmd_live(args, cfg)
        except AlreadyRunning as exc:
            print(f"a live writer is already running ({exc})")
            return 3
    from .report import report
    return report(cfg)


if __name__ == "__main__":
    sys.exit(main())
