"""The /live page's data: the current_state table, and nothing else.

This module never calls a device. The writer (xlpipe/live.py) fills the
table; any number of viewers read it. Ages are "how old is this reading",
measured from observed_at, the moment the writer took it, not from when the
page loaded: if the writer stops, the age climbs and the page shows it.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def readings(cache_path: str | Path, devices: list[str], now: datetime, stale_after: float) -> list[dict] | None:
    """One dict per configured line, in nav order. None if the writer has
    never run (no table, or no row at all)."""
    path = Path(cache_path)
    if not path.exists():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        try:
            got = {r["device_id"]: dict(r) for r in db.execute("SELECT * FROM current_state")}
        except sqlite3.OperationalError:          # no such table: the writer has never started
            return None
    if not got:
        return None
    out = []
    for dev in devices:
        r = got.get(dev) or {"device_id": dev}
        observed, attempted = _ts(r.get("observed_at")), _ts(r.get("attempted_at"))
        r["observed"], r["attempted"] = observed, attempted
        r["age_s"] = (now - observed).total_seconds() if observed else None
        r["never_read"] = observed is None
        # failed since the last good reading: the values shown are the last good ones
        r["failing"] = bool(r.get("last_error")) and (observed is None or (attempted and attempted > observed))
        r["stale"] = r["never_read"] or r["failing"] or r["age_s"] > stale_after
        r["state_start_ts"], r["shift_start_ts"] = _ts(r.get("state_start")), _ts(r.get("shift_start"))
        run = r.get("shift_run_time") or 0.0
        unp = r.get("shift_unplanned_stop_time") or 0.0
        pln = r.get("shift_planned_stop_time") or 0.0
        # the site's conventions: availability = run / (run + unplanned + planned); performance = ideal / run
        r["availability"] = run / (run + unp + pln) if run + unp + pln > 0 else None
        r["performance"] = (r.get("shift_ideal_time") or 0.0) / run if run > 0 else None
        out.append(r)
    return out
