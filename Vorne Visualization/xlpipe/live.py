"""The live writer: devices -> current_state, one row per device.

    python -m xlpipe live                  every 5 s (config.yaml live:)
    python -m xlpipe live --interval 10

`python -m webapp` also runs this loop, in background threads, so there is
one process to start. The /live page reads only the current_state table and
never calls a device, so any number of viewers cost one write loop, and the
table can move to Postgres (schema.sql current_state) without touching the
page.

One thread per device: each reads, writes its own row and sleeps, so a slow
or unreachable device only ever delays itself. A successful read replaces the
row and stamps observed_at with when the reading was taken. A failed read
touches only attempted_at and last_error: the last good values stay, with
their true age, and the page marks them stale. Nothing is ever cleared to
blank.

GET only: api/v0/real_time/data, plus the open process_state event for when
the present state began. No piece counts are read into the table.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .client import XLClient
from .metrics import positional_events

log = logging.getLogger(__name__)

INTERVAL_S = 5.0
TIMEOUT_S = 3.0

# Mirrors schema.sql current_state, without its count columns (no piece counts
# are shown anywhere until the count inputs are reviewed), plus when the
# present state and shift began.
DDL = """
CREATE TABLE IF NOT EXISTS current_state (
    device_id TEXT PRIMARY KEY,
    observed_at TEXT,            -- when the last successful reading was taken (writer clock, UTC)
    attempted_at TEXT,           -- last attempt, successful or not
    last_error TEXT,             -- NULL after a successful read
    device_time TEXT,            -- the device's own clock at that reading
    process_state TEXT, process_state_label TEXT,
    process_state_reason TEXT, process_state_reason_display_name TEXT,
    performance_impact TEXT, production_phase TEXT,
    part TEXT, part_label TEXT, job TEXT,
    shift TEXT, shift_label TEXT,
    process_state_event_id INTEGER, shift_event_id INTEGER,
    state_start TEXT,            -- when the present process state began
    state_elapsed_s REAL,        -- the device's time in it, interruptions excluded
    shift_start TEXT, shift_elapsed_s REAL,
    shift_run_time REAL, shift_unplanned_stop_time REAL, shift_planned_stop_time REAL,
    shift_not_scheduled_time REAL, shift_ideal_time REAL
);
"""

FIELDS = ("device_time", "process_state", "process_state_label", "process_state_reason",
          "process_state_reason_display_name", "performance_impact", "production_phase", "part", "part_label",
          "job", "shift", "shift_label", "process_state_event_id", "shift_event_id", "state_start",
          "state_elapsed_s", "shift_start", "shift_elapsed_s", "shift_run_time", "shift_unplanned_stop_time",
          "shift_planned_stop_time", "shift_not_scheduled_time", "shift_ideal_time")

STATE_FIELDS = ["process_state_event_id", "start_time", "process_state", "duration"]


def _text(v: Any) -> str | None:
    """The device writes the string "null" for nothing."""
    return None if v is None or v == "null" or v == "" else str(v)


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def from_real_time(payload: dict, state_events: list[dict] | None = None) -> dict[str, Any]:
    """One current_state row from api/v0/real_time/data and the open
    process_state event. Shift figures are the device's own, for the shift so
    far; nothing is recomputed here."""
    d = payload["data"]
    cat, name, m, ids = d["categories"], d["category_display_names"], d["metrics"], d.get("event_ids", {})

    def shift(metric: str) -> Any:
        return (m.get(metric) or {}).get("shift")

    st = (state_events or [None])[0] or {}
    reason = _text(cat.get("process_state_reason"))
    return {
        "device_time": _text(shift("end_time")),
        "process_state": _text(cat.get("process_state")),
        "process_state_label": _text(name.get("process_state")),
        "process_state_reason": reason,
        "process_state_reason_display_name": _text(name.get("process_state_reason")) if reason else None,
        "performance_impact": _text(cat.get("performance_impact")),
        "production_phase": _text(cat.get("production_phase")),
        "part": _text(cat.get("part")), "part_label": _text(name.get("part")),
        "job": _text(cat.get("job")),
        "shift": _text(cat.get("shift")), "shift_label": _text(name.get("shift")),
        "process_state_event_id": ids.get("process_state_event_id"),
        "shift_event_id": ids.get("shift_event_id"),
        # only trust the state's start if it is the same event the reading describes
        "state_start": _text(st.get("start_time"))
        if st.get("process_state_event_id") == ids.get("process_state_event_id") else None,
        "state_elapsed_s": _num(shift("elapsed_time")),
        "shift_start": _text(shift("start_time")), "shift_elapsed_s": _num(shift("duration")),
        "shift_run_time": _num(shift("run_time")),
        "shift_unplanned_stop_time": _num(shift("unplanned_stop_time")),
        "shift_planned_stop_time": _num(shift("planned_stop_time")),
        "shift_not_scheduled_time": _num(shift("not_scheduled_time")),
        "shift_ideal_time": _num(shift("ideal_time")),
    }


def read_device(client) -> dict[str, Any]:
    payload = client.get_json("api/v0/real_time/data")
    states = positional_events(client.get_json(
        "api/v0/channels/process_state/events/current", {"fields": ",".join(STATE_FIELDS)}), STATE_FIELDS)
    return from_real_time(payload, states)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(DDL)
    return db


def write_ok(db: sqlite3.Connection, device_id: str, row: dict[str, Any], observed_at: str) -> None:
    cols = ("device_id", "observed_at", "attempted_at", "last_error") + FIELDS
    vals = (device_id, observed_at, observed_at, None) + tuple(row.get(f) for f in FIELDS)
    db.execute(f"INSERT INTO current_state ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
               f"ON CONFLICT (device_id) DO UPDATE SET "
               + ", ".join(f"{c} = excluded.{c}" for c in cols[1:]), vals)


def write_failed(db: sqlite3.Connection, device_id: str, error: str, attempted_at: str) -> None:
    """Keep the last good values and their observed_at; record the failure."""
    db.execute("INSERT INTO current_state (device_id, attempted_at, last_error) VALUES (?, ?, ?) "
               "ON CONFLICT (device_id) DO UPDATE SET attempted_at = excluded.attempted_at, "
               "last_error = excluded.last_error", (device_id, attempted_at, error[:500]))


def poll_device(db_path, device_id: str, client) -> bool:
    """One read of one device into its row. True if the read succeeded."""
    try:
        row = read_device(client)
        ok, err = True, None
    except Exception as exc:  # noqa: BLE001 - any failure is recorded against the device
        ok, err = False, f"{type(exc).__name__}: {exc}"
    when = _now()           # the moment the reading (or the failure) came back
    with closing(connect(db_path)) as db:
        if ok:
            write_ok(db, device_id, row, when)
        else:
            write_failed(db, device_id, err, when)
    return ok


def device_loop(db_path, device_id: str, client_factory: Callable[[], Any], interval: float,
                stop: threading.Event) -> None:
    client = client_factory()
    while not stop.is_set():
        started = datetime.now(timezone.utc)
        try:
            ok = poll_device(db_path, device_id, client)
            if not ok:
                log.warning("[%s] live read failed", device_id)
        except Exception:  # noqa: BLE001 - the cache was busy, say; keep looping, the age will show it
            log.exception("[%s] could not write current_state", device_id)
        spent = (datetime.now(timezone.utc) - started).total_seconds()
        stop.wait(max(0.0, interval - spent))


def start(db_path, devices: list[dict], *, interval: float = INTERVAL_S, timeout: float = TIMEOUT_S,
          client_factory: Callable[[str], Any] | None = None) -> tuple[threading.Event, list[threading.Thread]]:
    """Start one daemon thread per device. Returns the stop event and threads."""
    make = client_factory or (lambda host: XLClient(host, timeout=timeout))
    with closing(connect(db_path)):
        pass                 # the table exists before the first page load
    stop = threading.Event()
    threads = []
    for d in devices:
        t = threading.Thread(target=device_loop, name=f"live-{d['id']}", daemon=True,
                             args=(db_path, d["id"], (lambda h=d["host"]: make(h)), interval, stop))
        t.start()
        threads.append(t)
    return stop, threads


def settings(cfg: dict) -> tuple[float, float]:
    live = cfg.get("live") or {}
    return float(live.get("interval_seconds", INTERVAL_S)), float(live.get("timeout_seconds", TIMEOUT_S))
