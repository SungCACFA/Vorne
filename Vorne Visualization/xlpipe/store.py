"""The storage interface the sync logic writes through.

Everything upstream - channel reads, zipping, resumption, the re-sync
window, quarantine decisions, row building - talks only to this. Today the
implementation is the local SQLite cache (cache.py). Later it becomes
Postgres direct, or an HTTPS endpoint on the Flask app in Azure; neither
needs a change above this line.

Errors are classified once, here, by SQLSTATE, so every implementation
reports failures the same way:

  TransportError   link or server temporarily unavailable -> retry with
                   backoff (60 s ceiling); next run catches up
  SystemicError    affects every row: schema drift, permissions, unknown
                   -> hold and back off, NEVER quarantine row by row
  RowRejected      this row's data is bad -> quarantine it, load the rest
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .channels import ChannelSpec


class StoreError(RuntimeError):
    def __init__(self, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class TransportError(StoreError):
    pass


class SystemicError(StoreError):
    pass


class RowRejected(StoreError):
    pass


def classify_sqlstate(sqlstate: str | None, message: str = "") -> type[StoreError]:
    """Map a Postgres SQLSTATE to a failure class.

    Only data exceptions (class 22) and NOT NULL violations are row-level.
    Everything else that is not transport is systemic, including the unknown:
    holding is recoverable, draining the queue into quarantine is not.
    """
    s = (sqlstate or "").upper()
    if s.startswith("08") or s in {"57P01", "57P02", "57P03", "53300", "40001", "40P01"} or s.startswith("53"):
        return TransportError
    if s.startswith("22") or s == "23502":
        return RowRejected
    return SystemicError


@dataclass
class SyncState:
    device_id: str
    channel: str
    resume_key: str = "sync_id"            # or 'record_order'
    last_sync_id: int | None = None
    last_record_order: int | None = None
    backfill_completed_at: str | None = None
    last_synced_at: str | None = None
    last_success_at: str | None = None
    last_window_from_day: int | None = None
    rows_loaded: int = 0
    rows_flagged_deleted: int = 0
    last_error: str | None = None
    last_error_at: str | None = None

    @property
    def fresh(self) -> bool:
        return self.last_sync_id is None and self.last_record_order is None and not self.backfill_completed_at


@dataclass
class QuarantineItem:
    natural_key: str
    error: str
    payload: dict[str, Any]
    sync_id: int | None = None
    sqlstate: str | None = None


@dataclass
class Heartbeat:
    instance_id: str
    hostname: str
    version: str
    run_started_at: str
    run_finished_at: str
    run_ok: bool
    devices_ok: int
    devices_total: int
    rows_upserted: int
    last_error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Store(Protocol):
    def load_state(self, device_id: str, channel: str) -> SyncState: ...
    def save_state(self, state: SyncState) -> None: ...
    def upsert_device(self, row: dict[str, Any]) -> None: ...
    def record_channel_fields(self, device_id: str, channel: str,
                              schema: dict[str, dict[str, Any]], wanted: list[str]) -> None: ...

    def upsert(self, spec: ChannelSpec, rows: list[dict[str, Any]]) -> int:
        """Insert or update by (device_id, key). An existing row is only
        overwritten by a strictly newer sync_id (or if it was flagged
        deleted), so a replay can never undo an edit. Returns rows changed."""
        ...

    def live_keys(self, spec: ChannelSpec, device_id: str, from_day: int) -> set[int]: ...
    def flag_deleted(self, spec: ChannelSpec, device_id: str, keys: set[int]) -> int: ...
    def quarantine(self, device_id: str, channel: str, items: list[QuarantineItem]) -> None: ...
    def resolve_quarantine(self, device_id: str, channel: str, keys: list[str]) -> None: ...
    def record_open_event(self, spec: ChannelSpec, device_id: str, row: dict[str, Any] | None) -> None:
        """Replace the channel's current open event (None: there is none)."""
        ...

    def refresh_part_standard(self) -> int: ...
    def heartbeat(self, hb: Heartbeat) -> None: ...
    def close(self) -> None: ...
