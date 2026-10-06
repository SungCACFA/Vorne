"""Pure functions that turn XL payloads into values. No I/O.

Two rules apply throughout (BRIEF section 4):

  * Values are kept exactly as the device reports them. Nothing is
    clamped, absolute-valued or recomputed. A negative reject_count is real
    information about a real problem.
  * The only values ever replaced are ones Postgres physically cannot store:
    NaN and +/-infinity in a double precision column become NULL.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


# The device's way of saying "nothing here". Seen live on categories
# (team: "null") and in channel events (shift: "null" between shifts). It is
# also the dimension key for "No Part" / "No Team", so it really does mean
# absent, and storing the literal string would make `IS NULL` lie.
DEVICE_NULL = "null"


# ------------------------------------------------------------------ values


def num(value: Any) -> float | None:
    """Coerce to a finite float, or None.

    Sign and magnitude pass through untouched. Only values Postgres double
    precision cannot hold (NaN, +/-inf) or that are not numbers at all become
    None.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or value == DEVICE_NULL:
            return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def is_non_finite(value: Any) -> bool:
    """True for a float NaN or infinity - the values num() silently drops."""
    return isinstance(value, float) and not math.isfinite(value)


def text(value: Any) -> str | None:
    """Category values arrive as strings, with "null" meaning absent."""
    if value is None:
        return None
    out = str(value)
    return None if out == DEVICE_NULL or out == "" else out


def event_id(value: Any) -> int | None:
    """Event ids are integers; tolerate them arriving as 12.0 or "12"."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    f = num(value)
    if f is None or f != int(f):
        return None
    return int(f)


def parse_ts(value: Any) -> datetime | None:
    """Device timestamps: ISO-8601 with offset, e.g.
    "2026-10-02T09:00:00.000000-04:00". Epoch seconds or milliseconds are
    accepted too. A naive timestamp is rejected rather than guessed at.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value <= 0:
            return None
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s or s == DEVICE_NULL:
        return None
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def json_safe(value: Any) -> tuple[Any, int]:
    """Deep-copy a JSON-able structure with NaN/inf replaced by None.

    Postgres jsonb rejects NaN just like double precision does, and one bad
    value aborts the whole batch. Returns the cleaned value and how many
    replacements were made, so callers can log it instead of hiding it.
    """
    if isinstance(value, float):
        return (None, 1) if not math.isfinite(value) else (value, 0)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        n = 0
        for k, v in value.items():
            out[str(k)], c = json_safe(v)
            n += c
        return out, n
    if isinstance(value, (list, tuple)):
        items = []
        n = 0
        for v in value:
            cv, c = json_safe(v)
            items.append(cv)
            n += c
        return items, n
    return value, 0


# --------------------------------------------------------------- envelopes


class DeviceError(RuntimeError):
    """The device answered, but with an error in meta.error."""


def unwrap(payload: Any) -> tuple[Any, dict[str, Any]]:
    """Split {"data": ..., "meta": ...} into (data, meta).

    Two endpoints return bare objects (rest/cpe/attributes and the scoreboard
    graphic); anything without a "data" key is passed through unchanged.
    """
    if isinstance(payload, dict) and "data" in payload:
        meta = payload.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        error = meta.get("error")
        if error:
            detail = error.get("detail") if isinstance(error, dict) else error
            raise DeviceError(str(detail))
        return payload["data"], meta
    return payload, {}


def section(payload: Any, name: str) -> Any:
    """Body of a real_time/data/<name> response.

    The device nests it under its own name: {"data": {"metrics": {...}}}.
    """
    data, _ = unwrap(payload)
    if isinstance(data, dict) and len(data) == 1 and name in data:
        return data[name]
    return data


def positional_events(payload: Any, fields: list[str]) -> list[dict[str, Any]]:
    """Zip channel-endpoint positional arrays back to field names.

    Channel endpoints answer {"events": [[v1, v2, ...], ...]} in the order the
    fields were requested, sometimes inside the usual data envelope. A row
    whose length does not match the request is an error, not something to
    guess about.
    """
    data, _ = unwrap(payload)
    events = data.get("events") if isinstance(data, dict) else data
    if events is None:
        return []
    if not isinstance(events, list):
        raise ValueError(f"expected a list of events, got {type(events).__name__}")
    if events and not isinstance(events[0], list):
        events = [events]  # a single bare event
    out = []
    for row in events:
        if not isinstance(row, list) or len(row) != len(fields):
            got = len(row) if isinstance(row, list) else type(row).__name__
            raise ValueError(f"event has {got} values for {len(fields)} requested fields")
        out.append(dict(zip(fields, row)))
    return out
