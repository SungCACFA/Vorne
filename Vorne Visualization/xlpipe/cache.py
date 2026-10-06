"""Local SQLite cache: the temporary storage target.

Implements the Store interface with the same semantics schema.sql gives
Postgres - keyed upserts guarded by sync_id, deletes flagged rather than
removed, quarantine retried by the re-sync window - so the sync logic and
row builders are exercised exactly as they will be in production.

Fact tables are generated from the channel specs, so column names match
schema.sql. The derived columns (is_full_hour, acct_gap_s,
is_count_adjustment) are SQLite generated columns with the same meaning.

It is a cache: if a spec changes, delete the file and refresh. A full pull
of all four devices takes minutes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .channels import DAY, ID, NUM, SPECS, TEXT, TS, ChannelSpec
from .metrics import json_safe
from .store import (
    Heartbeat,
    QuarantineItem,
    RowRejected,
    StoreError,
    SyncState,
    SystemicError,
    TransportError,
)

_SQL_TYPE = {ID: "INTEGER", DAY: "INTEGER", TS: "TEXT", TEXT: "TEXT", NUM: "REAL"}

_ELAPSED = "(unixepoch(end_time, 'subsec') - unixepoch(start_time, 'subsec'))"
_BUCKETS = "(run_time + unplanned_stop_time + planned_stop_time + not_scheduled_time)"
_GENERATED = {
    "hour_record": {
        "is_full_hour": f"round({_ELAPSED}, 3) = 3600.0",
        "acct_gap_s": f"{_ELAPSED} - {_BUCKETS}",
        "is_count_adjustment": "reject_count = -good_count AND run_time = 0 AND good_count > 0",
    },
    "shift_record": {
        "acct_gap_s": f"{_ELAPSED} - {_BUCKETS}",
        "is_count_adjustment": "reject_count = -good_count AND run_time = 0 AND good_count > 0",
    },
}

_CONTROL = """
CREATE TABLE IF NOT EXISTS device (
    device_id TEXT PRIMARY KEY, host TEXT NOT NULL, asset_name TEXT, asset_uuid TEXT,
    mac TEXT, model TEXT, firmware TEXT, data_version TEXT,
    first_seen TEXT NOT NULL, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS device_config_epoch (
    device_id TEXT NOT NULL, config_epoch INTEGER NOT NULL, effective_from TEXT NOT NULL,
    counts_trusted INTEGER NOT NULL DEFAULT 0, usable INTEGER NOT NULL DEFAULT 1,
    exclusion_reason TEXT, note TEXT,
    PRIMARY KEY (device_id, config_epoch)
);
CREATE TABLE IF NOT EXISTS sync_state (
    device_id TEXT NOT NULL, channel TEXT NOT NULL,
    resume_key TEXT NOT NULL DEFAULT 'sync_id',
    last_sync_id INTEGER, last_record_order INTEGER,
    backfill_completed_at TEXT, last_synced_at TEXT, last_success_at TEXT,
    last_window_from_day INTEGER,
    rows_loaded INTEGER NOT NULL DEFAULT 0, rows_flagged_deleted INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, last_error_at TEXT,
    PRIMARY KEY (device_id, channel)
);
CREATE TABLE IF NOT EXISTS sync_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL, channel TEXT NOT NULL, natural_key TEXT NOT NULL,
    sync_id INTEGER, payload TEXT NOT NULL, error TEXT NOT NULL, sqlstate TEXT,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1, resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS sync_quarantine_open
    ON sync_quarantine (device_id, channel, natural_key) WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS sync_heartbeat (
    instance_id TEXT PRIMARY KEY, hostname TEXT, version TEXT, last_heartbeat TEXT NOT NULL,
    last_run_started_at TEXT, last_run_finished_at TEXT, last_run_ok INTEGER,
    devices_ok INTEGER, devices_total INTEGER, rows_upserted INTEGER, last_error TEXT,
    detail TEXT
);
-- Not in schema.sql: what each channel's field schema actually offered,
-- so drift between devices or firmware is queryable.
CREATE TABLE IF NOT EXISTS channel_field (
    device_id TEXT NOT NULL, channel TEXT NOT NULL, field TEXT NOT NULL,
    wanted INTEGER NOT NULL, present INTEGER NOT NULL, type TEXT, subtype TEXT, aggregation TEXT,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (device_id, channel, field)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fact_ddl(spec: ChannelSpec) -> str:
    cols = []
    for f in spec.wanted:
        col = spec.column(f)
        typ = _SQL_TYPE[spec.kinds.get(f, NUM)]
        cols.append(f"{col} {typ}" + (" NOT NULL" if f == spec.key else ""))
    if spec.keep_raw_day:
        cols.append("production_day_number_raw TEXT")
    for name, expr in _GENERATED.get(spec.table, {}).items():
        cols.append(f"{name} GENERATED ALWAYS AS ({expr}) STORED")
    cols += [
        "config_epoch INTEGER NOT NULL DEFAULT 1",
        "deleted_at TEXT",
        "first_loaded_at TEXT NOT NULL",
        "updated_at TEXT NOT NULL",
        f"PRIMARY KEY (device_id, {spec.key})",
    ]
    body = ",\n    ".join(["device_id TEXT NOT NULL", *cols])
    idx = "" if not spec.has_end_time else (
        f"\nCREATE INDEX IF NOT EXISTS {spec.table}_time ON {spec.table} (device_id, start_time);")
    return (f"CREATE TABLE IF NOT EXISTS {spec.table} (\n    {body}\n);"
            f"\nCREATE INDEX IF NOT EXISTS {spec.table}_day ON {spec.table} (device_id, production_day_number);"
            + idx)


def _classify(exc: sqlite3.Error) -> StoreError:
    msg = str(exc)
    if isinstance(exc, sqlite3.IntegrityError) and "NOT NULL" in msg:
        return RowRejected(msg, "23502")
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in msg or "busy" in msg):
        return TransportError(msg)
    return SystemicError(msg)


class SQLiteCache:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(_CONTROL)
        for spec in SPECS.values():
            self.db.executescript(fact_ddl(spec))

    # ------------------------------------------------------------- state

    def load_state(self, device_id: str, channel: str) -> SyncState:
        with self._lock:
            r = self.db.execute("SELECT * FROM sync_state WHERE device_id=? AND channel=?",
                                (device_id, channel)).fetchone()
        return SyncState(**dict(r)) if r else SyncState(device_id, channel)

    def save_state(self, s: SyncState) -> None:
        cols = list(SyncState.__dataclass_fields__)
        with self._lock:
            self.db.execute(
                f"INSERT OR REPLACE INTO sync_state ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [getattr(s, c) for c in cols],
            )

    def upsert_device(self, row: dict[str, Any]) -> None:
        now = _now()
        with self._lock:
            self.db.execute(
                """INSERT INTO device (device_id, host, asset_name, asset_uuid, mac, model, firmware,
                                       data_version, first_seen, last_seen)
                   VALUES (:device_id, :host, :asset_name, :asset_uuid, :mac, :model, :firmware,
                           :data_version, :now, :now)
                   ON CONFLICT (device_id) DO UPDATE SET
                       host = excluded.host, asset_name = excluded.asset_name,
                       asset_uuid = excluded.asset_uuid, mac = excluded.mac, model = excluded.model,
                       firmware = excluded.firmware, data_version = excluded.data_version,
                       last_seen = excluded.last_seen""",
                {**row, "now": now},
            )
            # Epoch 1, counts untrusted, as schema.sql seeds it.
            self.db.execute(
                "INSERT OR IGNORE INTO device_config_epoch (device_id, config_epoch, effective_from, note) "
                "VALUES (?, 1, '-infinity', 'initial configuration')",
                (row["device_id"],),
            )

    def record_channel_fields(self, device_id: str, channel: str,
                              schema: dict[str, dict[str, Any]], wanted: list[str]) -> None:
        now = _now()
        rows = [(device_id, channel, f, 1, int(f in schema),
                 schema.get(f, {}).get("type"), schema.get(f, {}).get("subtype"),
                 schema.get(f, {}).get("aggregation"), now) for f in wanted]
        with self._lock:
            self.db.executemany("INSERT OR REPLACE INTO channel_field VALUES (?,?,?,?,?,?,?,?,?)", rows)

    # ------------------------------------------------------------- facts

    def upsert(self, spec: ChannelSpec, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        cols = spec.columns
        updatable = [c for c in cols if c not in ("device_id", spec.key)]
        t = spec.table
        sql = (
            f"INSERT INTO {t} ({', '.join(cols)}, first_loaded_at, updated_at) "
            f"VALUES ({', '.join(':' + c for c in cols)}, :_now, :_now) "
            f"ON CONFLICT (device_id, {spec.key}) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in updatable)
            + ", deleted_at = NULL, updated_at = excluded.updated_at "
            f"WHERE {t}.sync_id IS NULL OR excluded.sync_id > {t}.sync_id OR {t}.deleted_at IS NOT NULL"
        )
        now = _now()
        params = [{**{c: r.get(c) for c in cols}, "_now": now} for r in rows]
        with self._lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                before = self.db.total_changes
                self.db.executemany(sql, params)
                changed = self.db.total_changes - before
                self.db.execute("COMMIT")
                return changed
            except sqlite3.Error as exc:
                self.db.execute("ROLLBACK")
                raise _classify(exc) from exc

    def live_keys(self, spec: ChannelSpec, device_id: str, from_day: int) -> set[int]:
        with self._lock:
            got = self.db.execute(
                f"SELECT {spec.key} FROM {spec.table} WHERE device_id = ? AND deleted_at IS NULL "
                f"AND production_day_number >= ?", (device_id, from_day)).fetchall()
        return {r[0] for r in got}

    def flag_deleted(self, spec: ChannelSpec, device_id: str, keys: set[int]) -> int:
        if not keys:
            return 0
        now = _now()
        with self._lock:
            cur = self.db.executemany(
                f"UPDATE {spec.table} SET deleted_at = ?, updated_at = ? "
                f"WHERE device_id = ? AND {spec.key} = ? AND deleted_at IS NULL",
                [(now, now, device_id, k) for k in keys])
            return cur.rowcount

    # -------------------------------------------------------- quarantine

    def quarantine(self, device_id: str, channel: str, items: list[QuarantineItem]) -> None:
        now = _now()
        with self._lock:
            for it in items:
                payload, _ = json_safe(it.payload)
                self.db.execute(
                    """INSERT INTO sync_quarantine (device_id, channel, natural_key, sync_id, payload,
                                                   error, sqlstate, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (device_id, channel, natural_key) WHERE resolved_at IS NULL
                       DO UPDATE SET attempts = attempts + 1, last_seen_at = excluded.last_seen_at,
                                     error = excluded.error, payload = excluded.payload""",
                    (device_id, channel, it.natural_key, it.sync_id,
                     json.dumps(payload, default=str), it.error, it.sqlstate, now, now))

    def resolve_quarantine(self, device_id: str, channel: str, keys: list[str]) -> None:
        if not keys:
            return
        now = _now()
        with self._lock:
            self.db.executemany(
                "UPDATE sync_quarantine SET resolved_at = ? WHERE device_id = ? AND channel = ? "
                "AND natural_key = ? AND resolved_at IS NULL",
                [(now, device_id, channel, k) for k in keys])

    # ------------------------------------------------------------ epochs

    def apply_epochs(self, epochs: dict[str, list[dict[str, Any]]]) -> None:
        """Load configuration epochs from config.yaml. In Postgres these are
        rows a human inserts; here config is the only place to keep them."""
        with self._lock:
            for dev, eras in (epochs or {}).items():
                for e in eras:
                    self.db.execute(
                        """INSERT INTO device_config_epoch (device_id, config_epoch, effective_from,
                                  counts_trusted, usable, exclusion_reason, note)
                           VALUES (?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT (device_id, config_epoch) DO UPDATE SET
                               effective_from = excluded.effective_from,
                               counts_trusted = excluded.counts_trusted, usable = excluded.usable,
                               exclusion_reason = excluded.exclusion_reason, note = excluded.note""",
                        (dev, int(e["epoch"]), str(e["from"]), int(bool(e.get("counts_trusted", False))),
                         int(bool(e.get("usable", True))), e.get("exclusion_reason"), e.get("note")))

    def restamp_epochs(self) -> int:
        """Set every fact row's config_epoch from its own start_time (schema.sql
        does this with triggers). Returns rows changed."""
        at = lambda col: (f"(e.effective_from = '-infinity' OR "  # noqa: E731
                          f"unixepoch(e.effective_from, 'subsec') <= unixepoch({col}, 'subsec'))")
        order = "CASE WHEN e.effective_from = '-infinity' THEN -1e18 ELSE unixepoch(e.effective_from, 'subsec') END"
        changed = 0
        with self._lock:
            for spec in SPECS.values():
                col = "t.start_time" if spec.has_end_time else "date(t.production_day_number * 86400, 'unixepoch')"
                pick = (f"COALESCE((SELECT e.config_epoch FROM device_config_epoch e "
                        f"WHERE e.device_id = t.device_id AND {at(col)} ORDER BY {order} DESC LIMIT 1), 1)")
                cur = self.db.execute(f"UPDATE {spec.table} AS t SET config_epoch = {pick} "
                                      f"WHERE config_epoch IS NOT {pick}")
                changed += cur.rowcount
        return changed

    # --------------------------------------------------------- heartbeat

    def heartbeat(self, hb: Heartbeat) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO sync_heartbeat VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (hb.instance_id, hb.hostname, hb.version, hb.run_finished_at, hb.run_started_at,
                 hb.run_finished_at, int(hb.run_ok), hb.devices_ok, hb.devices_total,
                 hb.rows_upserted, hb.last_error, json.dumps(hb.extra, default=str)))

    def close(self) -> None:
        with self._lock:
            self.db.close()
