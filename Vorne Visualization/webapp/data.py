"""Read-only queries against the local cache, shaped for the pages.

Two populations, kept apart deliberately (see schema.sql daily_time_accounting):
  all records  -> hour columns and utilization (scheduled / elapsed)
  full hours   -> availability and performance (rates over scheduled time)

Rows from unusable configuration epochs are never aggregated here. A
production day touched by one is dropped whole, and the page says so.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Toronto")
EPOCH = date(1970, 1, 1)


def production_day(n: int) -> date:
    return EPOCH + timedelta(days=n)


@dataclass
class Totals:
    """Seconds in each bucket, for one device over some set of days."""

    run: float = 0.0
    unp: float = 0.0
    pln: float = 0.0
    nsc: float = 0.0
    ideal: float = 0.0
    f_run: float = 0.0   # full hours only
    f_unp: float = 0.0
    f_pln: float = 0.0
    f_nsc: float = 0.0
    f_ideal: float = 0.0
    records: int = 0
    full_hours: int = 0
    days: int = 0

    def add(self, o: "Totals") -> None:
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(self, k) + getattr(o, k))

    @property
    def sched(self) -> float:
        return self.run + self.unp + self.pln

    @property
    def elapsed(self) -> float:
        return self.sched + self.nsc

    @property
    def availability(self) -> float | None:
        s = self.f_run + self.f_unp + self.f_pln
        return self.f_run / s if s else None

    @property
    def performance(self) -> float | None:
        return self.f_ideal / self.f_run if self.f_run else None

    @property
    def utilization(self) -> float | None:
        return self.sched / self.elapsed if self.elapsed else None

    @property
    def utilization_full_hours_only(self) -> float | None:
        """The old report's method, shown only to explain the difference."""
        s = self.f_run + self.f_unp + self.f_pln
        e = s + self.f_nsc
        return s / e if e else None


@dataclass
class DeviceHistory:
    device_id: str
    first_day: int | None          # first production day in the cache
    first_usable_day: int | None   # after the last day touched by an unusable epoch
    last_day: int | None
    exclusions: list[str] = field(default_factory=list)  # reasons, for the note
    last_closed_end: datetime | None = None


@dataclass
class Snapshot:
    devices: list[str]
    history: dict[str, DeviceHistory]
    daily: dict[tuple[str, int], Totals]     # (device, production day) -> totals; usable days only
    last_refresh: datetime | None

    @property
    def fleet_from_day(self) -> int | None:
        firsts = [h.first_usable_day for h in self.history.values() if h.first_usable_day is not None]
        return max(firsts) if len(firsts) == len(self.history) and firsts else None

    @property
    def last_day(self) -> int | None:
        lasts = [h.last_day for h in self.history.values() if h.last_day is not None]
        return max(lasts) if lasts else None


_DAILY = """
SELECT h.device_id, h.production_day_number,
       sum(run_time), sum(unplanned_stop_time), sum(planned_stop_time), sum(not_scheduled_time), sum(ideal_time),
       sum(CASE WHEN is_full_hour THEN run_time END),
       sum(CASE WHEN is_full_hour THEN unplanned_stop_time END),
       sum(CASE WHEN is_full_hour THEN planned_stop_time END),
       sum(CASE WHEN is_full_hour THEN not_scheduled_time END),
       sum(CASE WHEN is_full_hour THEN ideal_time END),
       count(*), sum(is_full_hour),
       min(COALESCE(e.usable, 1))
FROM hour_record h
LEFT JOIN device_config_epoch e USING (device_id, config_epoch)
WHERE h.deleted_at IS NULL AND h.production_day_number IS NOT NULL
GROUP BY 1, 2
"""


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def load(cache_path: str | Path) -> Snapshot:
    path = Path(cache_path)
    if not path.exists():
        raise FileNotFoundError(f"no cache at {path}; run: python -m xlpipe refresh")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        devices = [r[0] for r in db.execute("SELECT device_id FROM device ORDER BY device_id")]
        daily: dict[tuple[str, int], Totals] = {}
        tainted: dict[str, int] = {}   # last production day touched by an unusable epoch
        firsts: dict[str, int] = {}
        lasts: dict[str, int] = {}
        for r in db.execute(_DAILY):
            dev, day = r[0], r[1]
            firsts[dev] = min(firsts.get(dev, day), day)
            lasts[dev] = max(lasts.get(dev, day), day)
            if not r[14]:
                tainted[dev] = max(tainted.get(dev, day), day)
                continue
            v = [x or 0.0 for x in r[2:12]]
            daily[(dev, day)] = Totals(*v, records=r[12], full_hours=r[13] or 0, days=1)
        # Drop usable-looking days that precede a tainted one (cannot happen
        # with epochs as configured, but never aggregate inside an excluded era).
        for (dev, day) in list(daily):
            if dev in tainted and day <= tainted[dev]:
                del daily[(dev, day)]

        history = {}
        for dev in devices:
            reasons = [r[0] for r in db.execute(
                "SELECT exclusion_reason FROM device_config_epoch WHERE device_id=? AND usable=0 "
                "AND exclusion_reason IS NOT NULL ORDER BY config_epoch", (dev,))]
            last_end = db.execute("SELECT max(end_time) FROM hour_record WHERE device_id=? AND deleted_at IS NULL",
                                  (dev,)).fetchone()[0]
            history[dev] = DeviceHistory(
                device_id=dev,
                first_day=firsts.get(dev),
                first_usable_day=(tainted[dev] + 1) if dev in tainted else firsts.get(dev),
                last_day=lasts.get(dev),
                exclusions=reasons if dev in tainted else [],
                last_closed_end=_ts(last_end),
            )
        hb = db.execute("SELECT max(last_heartbeat) FROM sync_heartbeat").fetchone()[0]
        return Snapshot(devices=devices, history=history, daily=daily, last_refresh=_ts(hb))
    finally:
        db.close()


# ----------------------------------------------------------------- periods

GRANULARITIES = (("day", "Day"), ("week", "Week"), ("month", "Month"), ("ytd", "Year to date"))
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")


def period_key(gran: str, day: int) -> str:
    d = production_day(day)
    if gran == "day":
        return d.isoformat()
    if gran == "week":
        return (d - timedelta(days=d.weekday())).isoformat()  # Monday-start, as the old report
    if gran == "month":
        return d.strftime("%Y-%m")
    return str(d.year)


def nice(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1][:3]} {d.year}"


def period_label(gran: str, key: str) -> str:
    if gran == "ytd":
        return f"{key} to date"
    if gran == "month":
        y, m = key.split("-")
        return f"{MONTHS[int(m) - 1]} {y}"
    d = date.fromisoformat(key)
    return f"Production day {nice(d)}" if gran == "day" else f"Week of {nice(d)}"


# ---------------------------------------------------------------- line page

_USABLE = ("LEFT JOIN device_config_epoch e USING (device_id, config_epoch) "
           "WHERE device_id = ? AND x.deleted_at IS NULL AND COALESCE(e.usable, 1) = 1 "
           "AND x.production_day_number BETWEEN ? AND ?")


def _ro(cache_path: str | Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(cache_path)}?mode=ro", uri=True)


def activities(cache_path, dev: str, lo: int, hi: int) -> list[dict]:
    """Hours by process_state and performance_impact."""
    with closing(_ro(cache_path)) as db:
        rows = db.execute(
            f"SELECT process_state, performance_impact, count(*), sum(duration_s) "
            f"FROM state_event x {_USABLE} GROUP BY 1, 2 ORDER BY 4 DESC", (dev, lo, hi)).fetchall()
    return [{"state": s, "impact": i, "events": n, "seconds": sec or 0.0} for s, i, n, sec in rows]


def reasons(cache_path, dev: str, lo: int, hi: int, impact: str) -> list[dict]:
    """Stop reasons for one performance_impact, ranked by time lost."""
    with closing(_ro(cache_path)) as db:
        rows = db.execute(
            f"SELECT COALESCE(process_state_reason_display_name, process_state_reason, 'Unassigned'), "
            f"count(*), sum(duration_s) FROM state_event x {_USABLE} AND performance_impact = ? "
            f"GROUP BY 1 ORDER BY 3 DESC", (dev, lo, hi, impact)).fetchall()
    total = sum(r[2] or 0 for r in rows) or 1.0
    out, cum = [], 0.0
    for name, n, sec in rows:
        cum += sec or 0
        out.append({"reason": name, "events": n, "seconds": sec or 0.0, "cumulative": cum / total})
    return out


def hours(cache_path, dev: str, lo: int, hi: int) -> list[dict]:
    """Every record with scheduled time, newest first."""
    with closing(_ro(cache_path)) as db:
        rows = db.execute(
            f"SELECT production_day_number, start_time, end_time, shift, part, job, run_time, "
            f"unplanned_stop_time, planned_stop_time, ideal_time, is_full_hour "
            f"FROM hour_record x {_USABLE} "
            f"AND run_time + unplanned_stop_time + planned_stop_time > 0 ORDER BY start_time DESC",
            (dev, lo, hi)).fetchall()
    out = []
    for day, st, en, shift, part, job, run, unp, pln, ideal, full in rows:
        out.append({
            "day": day, "start": _ts(st), "end": _ts(en), "shift": shift, "part": part, "job": job,
            "run": run or 0.0, "unp": unp or 0.0, "pln": pln or 0.0,
            "performance": (ideal / run) if run else None, "full": bool(full),
        })
    return out
