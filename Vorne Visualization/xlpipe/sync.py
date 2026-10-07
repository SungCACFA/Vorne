"""The sync itself: device channels -> Store. No database code in here.

Per device, per channel, every run:

  1. Resolve the wanted fields against the channel's own schema.
  2. On first contact, test whether sync_id[gt] really filters. If it does,
     resume on sync_id (it advances on create, update and delete). If not,
     fall back to record_order.
  3. Incremental pull: keyset pages above the high-water mark. Closed
     records are upserted; open ones are skipped and the mark never moves
     past one. State is saved after every page, so an interrupted backfill
     resumes where it stopped.
  4. Trailing re-sync window (default 7 production days): re-pull every
     record in the window and upsert again. This catches edits (reasons
     assigned after the fact), retries anything quarantined, and lets
     deletes be reconciled: anything we hold in the window that the device
     no longer has is flagged deleted.

A device that cannot be reached is skipped; the others carry on.
"""

from __future__ import annotations

import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from . import VERSION
from .channels import SPECS, ChannelSpec
from .client import DeviceHTTPError, XLClient
from .metrics import event_id
from .reader import ChannelUnusable, DeviceReader
from .rows import MALFORMED, OK, OPEN, build_row, convert
from .store import Heartbeat, QuarantineItem, RowRejected, Store, StoreError, SystemicError, TransportError

log = logging.getLogger(__name__)


@dataclass
class ChannelResult:
    device_id: str
    channel: str
    resume_key: str = ""
    missing_fields: list[str] = field(default_factory=list)
    fetched: int = 0
    upserted: int = 0
    open_skipped: int = 0
    quarantined: int = 0
    flagged_deleted: int = 0
    window_from_day: int | None = None
    non_finite: dict[str, int] = field(default_factory=dict)
    day_spans: int = 0
    utc_offsets: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    seconds: float = 0.0


@dataclass
class SyncSettings:
    window_days: int = 7
    page_size: int = 1000
    # Refuse to flag more than this share of the window as deleted in one go.
    # A device returning nothing (API change, filter misbehaving) must not
    # wipe a week of data.
    max_delete_fraction: float = 0.2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _with_retry(fn: Callable[[], Any], what: str) -> Any:
    """Transport errors: exponential backoff to a 60 s ceiling, a few tries."""
    delay = 1.0
    for attempt in range(6):
        try:
            return fn()
        except TransportError as exc:
            if attempt == 5:
                raise
            log.warning("%s: %s - retrying in %.0fs", what, exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60.0)


class ChannelSync:
    def __init__(self, reader: DeviceReader, store: Store, spec: ChannelSpec, settings: SyncSettings) -> None:
        self.reader = reader
        self.store = store
        self.spec = spec
        self.settings = settings
        self.device_id = reader.device_id
        self.result = ChannelResult(self.device_id, spec.channel)

    # ------------------------------------------------------------ loading

    def _load(self, records: list[dict[str, Any]], current_key: int | None) -> list:
        """Build, upsert and quarantine one batch. Returns the Built list in
        input order, so the caller can place its high-water mark."""
        now = _now()
        built = [build_row(self.spec, self.device_id, r, current_key=current_key, now=now) for r in records]
        r = self.result
        r.fetched += len(built)
        good, bad = [], []
        for b, raw in zip(built, records):
            if b.status == OPEN:
                r.open_skipped += 1
            elif b.status == MALFORMED:
                bad.append(QuarantineItem(str(b.key if b.key is not None else raw.get(self.spec.key)),
                                          b.reason or "malformed", raw, b.sync_id))
            else:
                good.append((b, raw))
                for f in b.non_finite:
                    r.non_finite[f] = r.non_finite.get(f, 0) + 1
                r.day_spans += b.day_was_span
                if b.utc_offset:
                    r.utc_offsets[b.utc_offset] = r.utc_offsets.get(b.utc_offset, 0) + 1

        loaded_keys: list[str] = []
        if good:
            rows = [b.row for b, _ in good]
            try:
                r.upserted += _with_retry(lambda: self.store.upsert(self.spec, rows), "upsert")
                loaded_keys = [str(b.key) for b, _ in good]
            except RowRejected:
                # One malformed row in the batch: find it, load the rest.
                for b, raw in good:
                    try:
                        r.upserted += _with_retry(lambda b=b: self.store.upsert(self.spec, [b.row]), "upsert")
                        loaded_keys.append(str(b.key))
                    except RowRejected as exc:
                        bad.append(QuarantineItem(str(b.key), str(exc), raw, b.sync_id, exc.sqlstate))
        if bad:
            for it in bad:
                log.error("[%s] %s quarantined %s: %s", self.device_id, self.spec.channel, it.natural_key, it.error)
            self.store.quarantine(self.device_id, self.spec.channel, bad)
            r.quarantined += len(bad)
        self.store.resolve_quarantine(self.device_id, self.spec.channel, loaded_keys)
        return built

    # --------------------------------------------------------------- run

    def run(self) -> ChannelResult:
        t0 = time.monotonic()
        spec, r = self.spec, self.result
        state = self.store.load_state(self.device_id, spec.channel)
        state.last_synced_at = _now().isoformat()
        try:
            fields, r.missing_fields = self.reader.resolve(spec)
            self.store.record_channel_fields(self.device_id, spec.channel,
                                             self.reader.channel_schema(spec.channel), spec.wanted)
            if r.missing_fields:
                log.warning("[%s] %s: fields not on this channel, stored as NULL: %s",
                            self.device_id, spec.channel, ", ".join(r.missing_fields))

            if state.fresh:
                state.resume_key = "sync_id" if self.reader.probe_sync_id_filter(spec) else "record_order"
            r.resume_key = state.resume_key
            current_key, current_day = self.reader.current(spec)

            # Incremental (the first run of a channel is a full backfill).
            key = state.resume_key
            mark = state.last_sync_id if key == "sync_id" else state.last_record_order
            # The open event carries sync_id -1. Starting a sync_id backfill
            # at [gt]=-1 keeps it out; unfiltered, it would sort first.
            after = mark if mark is not None else (-1 if key == "sync_id" else None)
            for page in self.reader.pages(spec, fields, key=key, after=after):
                built = self._load(page, current_key)
                for b in built:
                    if b.status == OPEN:
                        if key == "sync_id" and (b.sync_id is None or b.sync_id < 0):
                            continue  # no place in the sequence; nothing to hold back
                        break  # never move past an open event
                    v = b.sync_id if key == "sync_id" else b.record_order
                    if v is not None and (mark is None or v > mark):
                        mark = v
                if key == "sync_id":
                    state.last_sync_id = mark
                else:
                    state.last_record_order = mark
                state.rows_loaded += sum(1 for b in built if b.status == OK)
                self.store.save_state(state)
            if not state.backfill_completed_at:
                state.backfill_completed_at = _now().isoformat()

            # Trailing re-sync window and delete reconciliation.
            today = current_day if current_day is not None else self._latest_day(fields)
            if today is not None:
                from_day = today - self.settings.window_days
                r.window_from_day = from_day
                window = self.reader.window(spec, fields, from_day)
                self._load(window, current_key)
                device_keys = {event_id(w.get(spec.key)) for w in window}
                ours = self.store.live_keys(spec, self.device_id, from_day)
                gone = ours - device_keys
                if gone and len(gone) > max(5, self.settings.max_delete_fraction * len(ours)):
                    log.error("[%s] %s: device window lacks %d of our %d rows - too many to be real "
                              "deletes; not flagging. Investigate.", self.device_id, spec.channel,
                              len(gone), len(ours))
                elif gone:
                    r.flagged_deleted = self.store.flag_deleted(spec, self.device_id, gone)
                    state.rows_flagged_deleted += r.flagged_deleted
                    log.warning("[%s] %s: flagged %d row(s) deleted on the device: %s",
                                self.device_id, spec.channel, r.flagged_deleted, sorted(gone)[:10])
                state.last_window_from_day = from_day

            # The open event itself: never a fact, but the app needs to see it.
            if spec.has_end_time:
                current = self.reader.current_record(spec, fields)
                row = convert(spec, self.device_id, current)[0] if current else None
                self.store.record_open_event(spec, self.device_id, row)

            state.last_success_at = _now().isoformat()
            state.last_error = None
        except SystemicError:
            raise  # hold the whole run: this affects every row
        except (ChannelUnusable, DeviceHTTPError, StoreError, ValueError,
                requests.RequestException) as exc:
            r.error = f"{type(exc).__name__}: {exc}"
            state.last_error, state.last_error_at = r.error, _now().isoformat()
            log.error("[%s] %s failed: %s", self.device_id, spec.channel, r.error)
        finally:
            self.store.save_state(state)
            r.seconds = time.monotonic() - t0
        return r

    def _latest_day(self, fields: list[str]) -> int | None:
        if "production_day_number" not in fields:
            return None
        rows = self.reader._events(self.spec.channel, ["production_day_number"],
                                   {"sort": "-record_order", "limit": 1})
        return event_id(rows[0]["production_day_number"]) if rows else None


# ------------------------------------------------------------------- runs


@dataclass
class DeviceResult:
    device_id: str
    host: str
    identity: dict[str, Any] = field(default_factory=dict)
    channels: list[ChannelResult] = field(default_factory=list)
    error: str | None = None
    requests: int = 0


def sync_device(device: dict[str, Any], channels: list[str], store: Store, settings: SyncSettings,
                client_factory: Callable[[str], Any] | None = None) -> DeviceResult:
    dev_id, host = device["id"], device["host"]
    out = DeviceResult(dev_id, host)
    client = client_factory(host) if client_factory else XLClient(host, timeout=60.0, max_rps=8.0)
    reader = DeviceReader(client, dev_id, page_size=settings.page_size)
    try:
        out.identity = reader.identity()
        store.upsert_device(out.identity)
    except (requests.RequestException, DeviceHTTPError, ValueError) as exc:
        out.error = f"unreachable: {exc}"
        log.error("[%s] %s - skipping this device", dev_id, out.error)
        return out
    for ch in channels:
        out.channels.append(ChannelSync(reader, store, SPECS[ch], settings).run())
    out.requests = getattr(client, "requests_made", 0)
    return out


def sync_all(devices: list[dict[str, Any]], channels: list[str], store: Store, settings: SyncSettings,
             *, instance_id: str | None = None, client_factory: Callable[[str], Any] | None = None,
             ) -> list[DeviceResult]:
    started = _now()
    results: list[DeviceResult] = []
    error: str | None = None
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(devices)), thread_name_prefix="dev") as pool:
            futures = [pool.submit(sync_device, d, d.get("channels") or channels, store, settings, client_factory)
                       for d in devices]
            results = [f.result() for f in futures]
    except SystemicError as exc:
        error = f"held: {exc}"
        log.error("store rejected writes systemically (%s); holding until the next run", exc)
        raise
    finally:
        ok = sum(1 for d in results if not d.error and not any(c.error for c in d.channels))
        errors = [d.error for d in results if d.error] + [
            f"{c.device_id}/{c.channel}: {c.error}" for d in results for c in d.channels if c.error]
        store.heartbeat(Heartbeat(
            instance_id=instance_id or socket.gethostname(),
            hostname=socket.gethostname(),
            version=VERSION,
            run_started_at=started.isoformat(),
            run_finished_at=_now().isoformat(),
            run_ok=error is None and ok == len(devices),
            devices_ok=ok,
            devices_total=len(devices),
            rows_upserted=sum(c.upserted for d in results for c in d.channels),
            last_error=error or (errors[0] if errors else None),
        ))
    return results
