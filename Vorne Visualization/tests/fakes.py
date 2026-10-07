"""An in-memory XL channel endpoint, for driving the sync through edits,
deletes and open events that a recording cannot stage.

Behaviour copied from the real device (verified on FR8, 2.22.14.2):
  * positional arrays in the order of `fields`
  * the open event carries sync_id = -1
  * filters apply to the whole event; `sort` may be prefixed with '-'
"""

from __future__ import annotations

import re
from typing import Any

from xlpipe.channels import SPECS

_OPS = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b,
        "lte": lambda a, b: a <= b, "eq": lambda a, b: a == b}


class FakeDevice:
    def __init__(self, channel: str = "shift_hour", *, honour_sync_filter: bool = True) -> None:
        self.host = "fake"
        self.spec = SPECS[channel]
        self.events: dict[int, dict[str, Any]] = {}
        self.honour_sync_filter = honour_sync_filter
        self.log: list[tuple[str, dict]] = []
        self._next_sync = 100

    # ------------------------------------------------------------ editing

    def add(self, key: int, *, day: int, open_: bool = False, **fields) -> dict:
        """A record; closed ones get the next sync_id, like a device write."""
        ev = {f: None for f in self.spec.wanted}
        ev.update({self.spec.key: key, "record_order": key * 1000, "production_day_number": day,
                   "start_time": f"2026-10-0{1 + key % 5}T{key % 24:02d}:00:00+00:00",
                   "end_time": None if open_ else f"2026-10-0{1 + key % 5}T{key % 24:02d}:59:00+00:00",
                   "run_time": 1000.0, "unplanned_stop_time": 1000.0, "planned_stop_time": 1000.0,
                   "not_scheduled_time": 540.0, "ideal_time": 500.0,
                   "in_count": 10.0, "good_count": 8.0, "reject_count": 2.0})
        ev.update(fields)
        ev["sync_id"] = -1 if open_ else self._bump()
        self.events[key] = ev
        return ev

    def edit(self, key: int, **fields) -> None:
        """An after-the-fact change: new values and a new, higher sync_id."""
        self.events[key].update(fields)
        self.events[key]["sync_id"] = self._bump()

    def close(self, key: int) -> None:
        self.edit(key, end_time=self.events[key]["start_time"].replace(":00:00+", ":59:00+"))

    def delete(self, key: int) -> None:
        del self.events[key]
        self._bump()  # deletes advance sync_id too, with nothing to fetch

    def _bump(self) -> int:
        self._next_sync += 1
        return self._next_sync

    # ---------------------------------------------------------------- API

    def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        params = dict(params or {})
        self.log.append((path, params))
        ch = self.spec.channel
        if path == "api/v0/device":
            return {"data": {"asset_name": {"metadata": {}, "value": "FAKE"},
                             "firmware_version": {"metadata": {}, "value": "2.22.14.2"}},
                    "meta": {"asset_id": "00000000-0000-0000-0000-000000000000", "device_id": "00:00", "data_version": "1.6"}}
        if path == "rest/cpe/attributes":
            return {"asset_name": "FAKE", "device_model": "xlhd", "version": "2.22.14.2", "data_version": "1.6"}
        if path.startswith("api/v0/channels/") and path.count("/") == 3 and path != f"api/v0/channels/{ch}":
            return {"data": {"fields": []}, "meta": {}}       # a channel this fake does not model
        if path == f"api/v0/channels/{ch}":
            return {"data": {"fields": [{"name": f} for f in self.spec.wanted]}, "meta": {}}
        fields = params.pop("fields").split(",")
        rows = list(self.events.values())
        if path.endswith("/events/current"):
            rows = [e for e in rows if e["sync_id"] == -1][:1]
        else:
            for k, v in params.items():
                m = re.fullmatch(r"(\w+)\[(\w+)\]", k)
                if not m:
                    continue
                f, op = m.groups()
                if f == "sync_id" and not self.honour_sync_filter:
                    continue
                rows = [e for e in rows if e.get(f) is not None and _OPS[op](e[f], float(v))]
            if "sort" in params:
                desc = params["sort"].startswith("-")
                f = params["sort"].lstrip("-")
                rows.sort(key=lambda e: e[f], reverse=desc)
            rows = rows[: int(params.get("limit", 10**9))]
        return {"data": {"events": [[e.get(f) for f in fields] for e in rows]}, "meta": {}}

    def event_requests(self) -> list[dict]:
        return [p for path, p in self.log if path.endswith("/events")]
