"""Turn one zipped channel record into a row. Pure; no I/O.

Each record ends up as exactly one of:

  open       still accumulating on the device. Never stored, and the
             high-water mark never moves past it.
  malformed  cannot be stored as-is (no usable key, a value of the wrong
             kind). Quarantined with the reason; the re-sync window retries.
  ok         a row ready to upsert.

Values are kept as reported. Counts are not clamped or recomputed; a
negative reject_count passes straight through. The only substitution is
NaN/infinity -> NULL, because no database can store them, and those are
counted so they can be reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .channels import DAY, ID, NUM, TEXT, TS, ChannelSpec
from .metrics import DEVICE_NULL, event_id, is_non_finite, num, parse_ts, text, utc_iso

OPEN, MALFORMED, OK = "open", "malformed", "ok"


@dataclass
class Built:
    status: str
    row: dict[str, Any] | None = None
    reason: str | None = None
    key: int | None = None
    sync_id: int | None = None
    record_order: int | None = None
    non_finite: list[str] = field(default_factory=list)
    utc_offset: str | None = None  # of start_time as the device sent it
    day_was_span: bool = False


def _absent(raw: Any) -> bool:
    return raw is None or raw == DEVICE_NULL or raw == ""


def parse_day(raw: Any) -> tuple[int | None, str | None]:
    """production_day_number -> (first day, raw text if it was not one int).

    A run crossing midnight may come back as a list or a span. That is
    normal operation: keep the first day as the key and the raw value
    alongside, never reject it.
    """
    if _absent(raw):
        return None, None
    single = event_id(raw)
    if single is not None:
        return single, None
    if isinstance(raw, (list, tuple)):
        first = next((event_id(v) for v in raw if event_id(v) is not None), None)
        return first, ",".join(str(v) for v in raw)
    m = re.search(r"\d+", str(raw))
    return (int(m.group()) if m else None), str(raw)


def build_row(
    spec: ChannelSpec,
    device_id: str,
    values: dict[str, Any],
    *,
    current_key: int | None,
    now: datetime,
) -> Built:
    key = event_id(values.get(spec.key))
    sync_id = event_id(values.get("sync_id"))
    record_order = event_id(values.get("record_order"))
    out = Built(OK, key=key, sync_id=sync_id, record_order=record_order)

    if key is None:
        out.status, out.reason = MALFORMED, f"{spec.key} missing or not an integer: {values.get(spec.key)!r}"
        return out

    # Closed-ness comes from the device, never inferred from age alone.
    if sync_id == -1:
        out.status, out.reason = OPEN, "sync_id -1 (device marks the current event)"
        return out
    if current_key is not None and key == current_key:
        out.status, out.reason = OPEN, "is events/current"
        return out
    if spec.has_end_time:
        end = parse_ts(values.get("end_time"))
        if end is None or end > now:
            out.status, out.reason = OPEN, "end_time missing or not yet in the past"
            return out

    raw_start = values.get("start_time")
    if isinstance(raw_start, str):
        m = re.search(r"([+-]\d\d:\d\d|Z)$", raw_start)
        out.utc_offset = m.group(1) if m else None

    row, problems = convert(spec, device_id, values, out)
    if problems:
        out.status, out.reason = MALFORMED, "; ".join(problems)
        return out
    out.row = row
    return out


def convert(spec: ChannelSpec, device_id: str, values: dict[str, Any],
            out: Built | None = None) -> tuple[dict[str, Any], list[str]]:
    """Type-convert one record into a row, whether closed or open.

    Returns (row, problems); a non-empty problems list means the record is
    malformed. `out`, if given, collects NaN/inf and day-span bookkeeping.
    """
    out = out if out is not None else Built(OK)
    row: dict[str, Any] = {"device_id": device_id}
    problems: list[str] = []
    for f in spec.wanted:
        raw = values.get(f)
        kind = spec.kinds.get(f, NUM)
        col = spec.column(f)
        if kind == DAY:
            first, raw_text = parse_day(raw)
            row[col] = first
            if spec.keep_raw_day:
                row["production_day_number_raw"] = raw_text
            out.day_was_span = out.day_was_span or raw_text is not None
            continue
        if _absent(raw):
            row[col] = None
            continue
        if kind == ID:
            v = event_id(raw)
        elif kind == TS:
            parsed = parse_ts(raw)
            v = utc_iso(parsed)
        elif kind == TEXT:
            # A record spanning a changeover returns category fields as a
            # list, e.g. part ["SOFDRA", "OTRIVIN"]. Keep every value, in
            # the device's order, comma-joined (device keys never contain
            # commas).
            if isinstance(raw, list):
                parts = [t for t in (text(x) for x in raw) if t is not None]
                v = ",".join(parts) if parts else None
                row[col] = v
                continue
            v = text(raw)
        else:
            if is_non_finite(raw):
                out.non_finite.append(f)
                row[col] = None
                continue
            v = num(raw)
        if v is None:
            problems.append(f"{f}={raw!r} is not a valid {kind}")
        row[col] = v

    return row, problems
