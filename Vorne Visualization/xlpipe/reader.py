"""Read closed events from one device's channel endpoints. GET only.

Verified on FR8, firmware 2.22.14.2, shift_hour:

  * Records are slices rolled up into one row per channel event; the
    channel's event id (e.g. shift_hour_event_id) equals event_id.
  * sync_id[gt] filters on the rolled-up event: a value inside an event's
    slice range still returns the whole event with full totals. So
    incremental pulls by sync_id are safe.
  * The open event reports sync_id = -1, so sync_id[gt] never returns it.
  * record_order filters on the event's min(record_order), so it cannot see
    an edit to an older event. Fallback only.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from .channels import ChannelSpec
from .client import Client
from .metrics import event_id, positional_events, unwrap, unwrap_values

log = logging.getLogger(__name__)


class ChannelUnusable(RuntimeError):
    """The channel lacks a field that loading cannot do without."""


class DeviceReader:
    def __init__(self, client: Client, device_id: str, *, page_size: int = 1000) -> None:
        self.client = client
        self.device_id = device_id
        self.page_size = page_size
        self._schemas: dict[str, dict[str, dict[str, Any]]] = {}

    # ----------------------------------------------------------- identity

    def identity(self) -> dict[str, Any]:
        """Device registry row: api/v0/device (values double-wrapped) plus
        rest/cpe/attributes (bare)."""
        payload = self.client.get_json("api/v0/device")
        _, meta = unwrap(payload)
        dev = unwrap_values(payload)
        try:
            attrs = self.client.get_json("rest/cpe/attributes")
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] attributes unavailable: %s", self.device_id, exc)
            attrs = {}
        return {
            "device_id": self.device_id,
            "host": self.client.host,
            "asset_name": dev.get("asset_name"),
            "asset_uuid": meta.get("asset_id"),
            "mac": meta.get("device_id") or dev.get("device_id"),
            "model": attrs.get("device_model"),
            "firmware": dev.get("firmware_version") or attrs.get("version"),
            "data_version": meta.get("data_version") or attrs.get("data_version"),
            "device_time": dev.get("time"),
        }

    # -------------------------------------------------------------- schema

    def channel_schema(self, channel: str) -> dict[str, dict[str, Any]]:
        if channel not in self._schemas:
            data, _ = unwrap(self.client.get_json(f"api/v0/channels/{channel}"))
            fields = data.get("fields") if isinstance(data, dict) else None
            self._schemas[channel] = {f["name"]: f for f in fields or [] if isinstance(f, dict) and "name" in f}
        return self._schemas[channel]

    def resolve(self, spec: ChannelSpec) -> tuple[list[str], list[str]]:
        """(fields to request, wanted fields this channel does not have).

        A missing ordinary field becomes a NULL column. A missing key or
        sync_id makes the channel unloadable, and says so.
        """
        available = self.channel_schema(spec.channel)
        if not available:
            raise ChannelUnusable(f"{spec.channel}: channel schema is empty or unreadable")
        missing = [f for f in spec.wanted if f not in available]
        for essential in (spec.key, "sync_id"):
            if essential in missing:
                raise ChannelUnusable(f"{spec.channel}: no {essential} field")
        return [f for f in spec.wanted if f in available], missing

    # -------------------------------------------------------------- events

    def _events(self, channel: str, fields: list[str], params: dict[str, Any], path: str = "events") -> list[dict[str, Any]]:
        query = {"fields": ",".join(fields), **{k: str(v) for k, v in params.items()}}
        payload = self.client.get_json(f"api/v0/channels/{channel}/{path}", query)
        return positional_events(payload, fields)

    def current(self, spec: ChannelSpec) -> tuple[int | None, int | None]:
        """(key, production day) of the channel's open event, per the device."""
        fields = [spec.key, "production_day_number"]
        try:
            got = self._events(spec.channel, fields, {}, path="events/current")
        except Exception as exc:  # noqa: BLE001
            log.info("[%s] %s: no events/current (%s)", self.device_id, spec.channel, exc)
            return None, None
        if not got:
            return None, None
        return event_id(got[0].get(spec.key)), event_id(got[0].get("production_day_number"))

    def probe_sync_id_filter(self, spec: ChannelSpec) -> bool:
        """Does sync_id[gt] actually filter on this channel?

        Take the three highest-sync_id events, filter above the middle one,
        and check the device returns only the newest. Two requests.
        """
        try:
            top = self._events(spec.channel, [spec.key, "sync_id"], {"sort": "-sync_id", "limit": 3})
            if len(top) < 3:
                return True  # too little history to tell; nothing to lose
            mid = event_id(top[1]["sync_id"])
            got = self._events(spec.channel, [spec.key, "sync_id"],
                               {"sync_id[gt]": mid, "sort": "sync_id", "limit": 10})
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] %s: sync_id probe failed: %s", self.device_id, spec.channel, exc)
            return False
        ids = [event_id(r["sync_id"]) for r in got]
        ok = bool(ids) and all(i is not None and i > mid for i in ids) and event_id(top[0]["sync_id"]) in ids
        if not ok:
            log.warning("[%s] %s: sync_id[gt]=%s returned %s; falling back to record_order",
                        self.device_id, spec.channel, mid, ids)
        return ok

    def pages(self, spec: ChannelSpec, fields: list[str], *, key: str, after: int | None,
              extra: dict[str, Any] | None = None) -> Iterator[list[dict[str, Any]]]:
        """Keyset paging, ascending by `key` (sync_id or record_order).

        Keyset rather than offset: if the device prunes old records partway
        through a long backfill, offsets would shift and skip rows.
        """
        if key not in fields:
            raise ChannelUnusable(f"{spec.channel}: cannot page by {key}, field not requested")
        cursor = after
        while True:
            params: dict[str, Any] = {"sort": key, "limit": self.page_size, **(extra or {})}
            if cursor is not None:
                params[f"{key}[gt]"] = cursor
            page = self._events(spec.channel, fields, params)
            if not page:
                return
            yield page
            last = event_id(page[-1].get(key))
            if len(page) < self.page_size or last is None or (cursor is not None and last <= cursor):
                return
            cursor = last

    def window(self, spec: ChannelSpec, fields: list[str], from_day: int) -> list[dict[str, Any]]:
        """Every record from production day `from_day` on, open ones included."""
        out: list[dict[str, Any]] = []
        for page in self.pages(spec, fields, key="record_order", after=None,
                               extra={"production_day_number[gte]": from_day}):
            out.extend(page)
        return out
