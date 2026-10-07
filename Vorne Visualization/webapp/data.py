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
from datetime import date, datetime, timedelta, timezone
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


def part_daily(cache_path: str | Path, dev: str, days: list[int], part: str) -> dict[int, Totals]:
    """Per production day, the hour records that name this part (changeover
    hours count for both parts), as Totals. Only the given days, which the
    caller has already restricted to usable ones."""
    if not days:
        return {}
    q = _DAILY.replace("GROUP BY 1, 2", "AND h.device_id = ? AND h.production_day_number BETWEEN ? AND ? "
                       "AND instr(',' || h.part || ',', ?) > 0 GROUP BY 1, 2")
    keep = set(days)
    out = {}
    with closing(_ro(cache_path)) as db:
        for r in db.execute(q, (dev, days[0], days[-1], f",{part},")):
            if r[1] in keep and r[14]:
                out[r[1]] = Totals(*[x or 0.0 for x in r[2:12]], records=r[12], full_hours=r[13] or 0, days=1)
    return out


def line_parts(cache_path, dev: str, lo: int, hi: int) -> list[str]:
    """Every part named in this line's hour records in [lo, hi]."""
    with closing(_ro(cache_path)) as db:
        rows = db.execute(f"SELECT DISTINCT x.part FROM hour_record x {_USABLE} AND x.part IS NOT NULL",
                          (dev, lo, hi)).fetchall()
    return sorted({p for (v,) in rows for p in v.split(",") if p and p != "null"})


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


def _of_part(part: str | None, hour_record: bool = False) -> tuple[str, tuple]:
    """Filter to one product. State events carry the device's own single part;
    an hour record lists every part in the hour, comma-joined, so a changeover
    hour belongs to both parts."""
    if not part:
        return "", ()
    if hour_record:
        return " AND instr(',' || x.part || ',', ?) > 0", (f",{part},",)
    return " AND x.part = ?", (part,)


def activities(cache_path, dev: str, lo: int, hi: int, part: str | None = None) -> list[dict]:
    """Hours by process_state and performance_impact."""
    extra, args = _of_part(part)
    with closing(_ro(cache_path)) as db:
        rows = db.execute(
            f"SELECT process_state, performance_impact, count(*), sum(duration_s) "
            f"FROM state_event x {_USABLE}{extra} GROUP BY 1, 2 ORDER BY 4 DESC", (dev, lo, hi, *args)).fetchall()
    return [{"state": s, "impact": i, "events": n, "seconds": sec or 0.0} for s, i, n, sec in rows]


def _not_in(ids) -> tuple[str, tuple]:
    ids = tuple(sorted(ids or ()))
    return (f" AND process_state_event_id NOT IN ({','.join('?' * len(ids))})", ids) if ids else ("", ())


def reasons(cache_path, dev: str, lo: int, hi: int, impact: str, exclude_ids=(),
            part: str | None = None) -> list[dict]:
    """Stop reasons for one performance_impact, ranked by time lost.
    `exclude_ids` leaves out events flagged implausible (charts do; tables don't)."""
    extra, ids = _not_in(exclude_ids)
    p_extra, p_args = _of_part(part)
    extra, ids = extra + p_extra, ids + p_args
    with closing(_ro(cache_path)) as db:
        rows = db.execute(
            f"SELECT COALESCE(process_state_reason_display_name, process_state_reason, 'Unassigned'), "
            f"count(*), sum(duration_s) FROM state_event x {_USABLE} AND performance_impact = ?{extra} "
            f"GROUP BY 1 ORDER BY 3 DESC", (dev, lo, hi, impact, *ids)).fetchall()
    total = sum(r[2] or 0 for r in rows) or 1.0
    out, cum = [], 0.0
    for name, n, sec in rows:
        cum += sec or 0
        out.append({"reason": name, "events": n, "seconds": sec or 0.0, "cumulative": cum / total})
    return out


def hours(cache_path, dev: str, lo: int, hi: int, part: str | None = None) -> list[dict]:
    """Every record with scheduled time, newest first."""
    extra, args = _of_part(part, hour_record=True)
    with closing(_ro(cache_path)) as db:
        rows = db.execute(
            f"SELECT production_day_number, start_time, end_time, shift, part, job, run_time, "
            f"unplanned_stop_time, planned_stop_time, ideal_time, is_full_hour "
            f"FROM hour_record x {_USABLE}{extra} "
            f"AND run_time + unplanned_stop_time + planned_stop_time > 0 ORDER BY start_time DESC",
            (dev, lo, hi, *args)).fetchall()
    out = []
    for day, st, en, shift, part, job, run, unp, pln, ideal, full in rows:
        out.append({
            "day": day, "start": _ts(st), "end": _ts(en), "shift": shift, "part": part, "job": job,
            "run": run or 0.0, "unp": unp or 0.0, "pln": pln or 0.0,
            "performance": (ideal / run) if run else None, "full": bool(full),
        })
    return out


# ------------------------------------------------------------ open runs

OPEN_DAYS_LIMIT = 7
OPEN_MEDIAN_MULTIPLE = 3
MIN_RUNS_FOR_MEDIAN = 3


@dataclass
class OpenRun:
    device_id: str
    channel: str             # part | job
    part: str | None
    job: str | None
    start: datetime | None
    run_seconds: float
    open_seconds: float
    median_seconds: float | None   # this part's closed runs on this device
    median_runs: int
    reasons: list[str] = field(default_factory=list)  # why it is flagged; empty = fine

    @property
    def open_days(self) -> float:
        return self.open_seconds / 86400


def open_runs(cache_path, now: datetime | None = None) -> list[OpenRun]:
    """Each device's open part and job run, flagged when open more than
    7 days or more than 3x that part's median closed run on that device.

    The median is over substantial runs only (more than 200 cycles, the same
    threshold as the derived ideal cycle time). Lines record many short
    fragments - 10 of FR8's 22 ADVIL runs - which would drag the median to
    hours and make every ordinary run look overdue."""
    now = now or datetime.now(TZ)
    out = []
    with closing(_ro(cache_path)) as db:
        for dev, ch, part, job, st, run in db.execute(
                "SELECT device_id, channel, part, job, start_time, run_time FROM open_event "
                "WHERE channel IN ('part', 'job') ORDER BY device_id, channel DESC"):
            start = _ts(st)
            table = "part_run" if ch == "part" else "job_run"
            key_col = "part" if ch == "part" else "job"
            durs = sorted(r[0] for r in db.execute(
                f"SELECT (julianday(end_time) - julianday(start_time)) * 86400 FROM {table} "
                f"WHERE device_id=? AND {key_col}=? AND deleted_at IS NULL AND end_time IS NOT NULL "
                f"AND run_cycles > 200",
                (dev, part if ch == "part" else job)))
            med = durs[len(durs) // 2] if len(durs) >= MIN_RUNS_FOR_MEDIAN else None
            open_s = (now - start).total_seconds() if start else 0.0
            r = OpenRun(dev, ch, part, job, start, run or 0.0, open_s, med, len(durs))
            if open_s > OPEN_DAYS_LIMIT * 86400:
                r.reasons.append(f"open {r.open_days:.0f} days, over the {OPEN_DAYS_LIMIT}-day limit")
            if med and open_s > OPEN_MEDIAN_MULTIPLE * med:
                r.reasons.append(f"{open_s / med:.1f}x this {'part' if ch == 'part' else 'job'}'s median run "
                                 f"on {dev} ({med / 86400:.1f} days, {len(durs)} runs over 200 cycles)")
            out.append(r)
    return out


def merge_part_job(runs: list[OpenRun]) -> list[OpenRun]:
    """Today a line's part and job runs open and close together; one alert
    per line then, not two. Kept separate whenever they differ."""
    parts = {(r.device_id, r.start): r for r in runs if r.channel == "part"}
    out = []
    for r in runs:
        if r.channel == "job" and (r.device_id, r.start) in parts:
            parts[(r.device_id, r.start)].channel = "part and job"
            continue
        out.append(r)
    return sorted(out, key=lambda r: r.open_seconds, reverse=True)


# -------------------------------------------------------------- products

SAP_UNITS = (("s_per_piece", "Seconds per piece"), ("pieces_per_hour", "Pieces per hour"),
             ("hours_per_thousand", "Hours per 1,000"))


def from_s_per_piece(v: float | None, unit: str) -> float | None:
    if v is None:
        return None
    return {"s_per_piece": v, "pieces_per_hour": 3600 / v if v else None, "hours_per_thousand": v / 3.6}[unit]


def standards(cache_path) -> dict[str, dict]:
    with closing(_ro(cache_path)) as db:
        db.row_factory = sqlite3.Row
        return {r["part"]: dict(r) for r in db.execute("SELECT * FROM part_standard")}


def device_ict(cache_path) -> dict[tuple[str, str], dict]:
    """Derived ideal cycle time per (device, part): median and spread of
    ideal_time / run_cycles over that device's runs with run_cycles > 200."""
    import statistics

    per: dict[tuple[str, str], list[float]] = {}
    with closing(_ro(cache_path)) as db:
        for dev, part, ict in db.execute(
                "SELECT device_id, part, ideal_time / run_cycles FROM part_run "
                "WHERE deleted_at IS NULL AND part IS NOT NULL AND run_cycles > 200"):
            per.setdefault((dev, part), []).append(ict)
    return {k: {"ict": statistics.median(v), "sd": statistics.stdev(v) if len(v) > 1 else None, "n": len(v)}
            for k, v in per.items()}


def runs(cache_path, table: str, dev: str, lo: int, hi: int) -> list[dict]:
    """Closed runs starting in [lo, hi], usable epochs only, with whether
    their counts may be shown."""
    key = "part_event_id" if table == "part_run" else "job_event_id"
    with closing(_ro(cache_path)) as db:
        db.row_factory = sqlite3.Row
        got = db.execute(
            f"SELECT x.*, {key} AS event_key, COALESCE(e.counts_trusted, 0) AS counts_trusted "
            f"FROM {table} x {_USABLE}", (dev, lo, hi)).fetchall()
    out = []
    for r in got:
        d = dict(r)
        d["device_id"] = dev
        d["start"], d["end"] = _ts(d["start_time"]), _ts(d["end_time"])
        out.append(d)
    return out


def save_standard(cache_path, part: str, *, sap: float | None = ..., unit: str | None = None,
                  epc: float | None = ..., who: str = "web") -> None:
    """Write the hand-entered columns, and only those (as schema.sql's grants
    allow the app role). `...` means leave that value unchanged."""
    now = datetime.now(TZ).astimezone().isoformat()
    db = sqlite3.connect(str(cache_path), timeout=10)
    try:
        db.execute("INSERT INTO part_standard (part, updated_at) VALUES (?, ?) ON CONFLICT (part) DO NOTHING",
                   (part, now))
        if sap is not ...:
            db.execute("UPDATE part_standard SET sap_standard=?, sap_unit=?, sap_updated_at=?, sap_updated_by=?, "
                       "updated_at=? WHERE part=?", (sap, unit if sap is not None else None, now, who, now, part))
        if epc is not ...:
            db.execute("UPDATE part_standard SET expected_count_per_cycle=?, epc_updated_at=?, epc_updated_by=?, "
                       "updated_at=? WHERE part=?", (epc, now, who, now, part))
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------- heatmap

IMPACTS = ("run", "unplanned_stop", "planned_stop", "not_scheduled")


def production_day_window(n: int) -> tuple[datetime, list[datetime]]:
    """Production day n as drawn on the heatmap: the nominal window from
    23:00 the evening before to 23:00, split at each local clock hour.

    The device files an unscheduled night with the day before, so a
    production day's records can run past 24 hours. The heatmap draws time
    where it happened instead, so rows never overlap and nothing is dropped.
    Returns (window start, the 25 hour boundaries); DST days repeat or skip a
    wall-clock hour, handled by converting each boundary on its own."""
    d = production_day(n)
    start = datetime(d.year, d.month, d.day, 23, tzinfo=TZ) - timedelta(days=1)
    bounds = [(start.replace(tzinfo=None) + timedelta(hours=h)).replace(tzinfo=TZ) for h in range(25)]
    return start, bounds


def hour_slots(cache_path, dev: str, days: list[int], part: str | None = None) -> dict[int, list[dict]]:
    """For each production day, 24 cells of seconds by performance impact,
    from the device-timed state events - plus the state that took most of
    each impact, for the tooltip. Unusable epochs and deleted rows excluded;
    events that end before they start are skipped (suspected clock
    corrections, already flagged in data_quality)."""
    if not days:
        return {}
    first = production_day_window(days[0])[0]
    last = production_day_window(days[-1])[1][-1]
    with closing(_ro(cache_path)) as db:
        events = db.execute(
            "SELECT start_time, end_time, performance_impact, process_state, "
            "COALESCE(process_state_reason_display_name, process_state_reason), x.part "
            "FROM state_event x LEFT JOIN device_config_epoch e USING (device_id, config_epoch) "
            "WHERE device_id = ? AND x.deleted_at IS NULL AND COALESCE(e.usable, 1) = 1 "
            "AND end_time > ? AND start_time < ? ORDER BY start_time",
            (dev, first.astimezone(timezone.utc).isoformat(), last.astimezone(timezone.utc).isoformat())).fetchall()
    evs = []
    for st, en, imp, state, reason, ev_part in events:
        s, e = _ts(st), _ts(en)
        if s and e and e > s:
            # With a product chosen, other products' events still own their time (so the chosen
            # part's events keep exactly their own durations), painted as "another product".
            kind = "other" if part and ev_part != part else imp if imp in IMPACTS else "not_scheduled"
            evs.append((s, e, kind, state, reason))

    out: dict[int, list[dict]] = {}
    for n in days:
        _, bounds = production_day_window(n)
        day = [ev for ev in evs if ev[0] < bounds[-1] and ev[1] > bounds[0]]
        cells = []
        for h in range(24):
            secs, what = paint(day, bounds[h], bounds[h + 1])
            covered = sum(secs.values())
            dominant = max(secs, key=lambda k: secs[k]) if covered > 0 else None
            top = max(what.items(), key=lambda kv: kv[1])[0] if what else None
            cells.append({"start": bounds[h], "seconds": secs, "covered": covered, "dominant": dominant,
                          "top_state": top[1] if top else None, "top_reason": top[2] if top else None})
        out[n] = cells
    return out


def paint(events: list[tuple], a: datetime, b: datetime) -> tuple[dict[str, float], dict[tuple, float]]:
    """Seconds by impact in [a, b), giving each instant to exactly one event.

    The device reports an interrupted state (a stop running into a break, a
    weekend of no production) as one event spanning the interruption, with
    the interrupting event inside it; its duration already excludes the
    interruption. So: while events overlap, the latest-started one owns the
    time. Verified to reproduce the device's own durations."""
    inside = [ev for ev in events if ev[0] < b and ev[1] > a]
    cuts = sorted({a, b, *(t for ev in inside for t in (ev[0], ev[1]) if a < t < b)})
    secs = {k: 0.0 for k in IMPACTS}
    what: dict[tuple, float] = {}
    for x, y in zip(cuts, cuts[1:]):
        live = [ev for ev in inside if ev[0] <= x and ev[1] >= y]
        if not live:
            continue
        ev = max(live, key=lambda ev: ev[0])
        d = (y - x).total_seconds()
        secs[ev[2]] = secs.get(ev[2], 0.0) + d
        what[(ev[2], ev[3], ev[4])] = what.get((ev[2], ev[3], ev[4]), 0.0) + d
    return secs, what


# ------------------------------------------------- implausible durations

@dataclass(frozen=True)
class ImplausibleRule:
    """When a single state event is too long to be what it says it is.
    Values come from config.yaml (charts:); these are the defaults."""

    cap_hours: float = 24.0      # always flagged above this much net time in one state
    floor_hours: float = 12.0    # ...or above this AND
    multiple: float = 10.0       # ...more than this many times the state's median on that line


def implausible(cache_path, rule: ImplausibleRule = ImplausibleRule(), dev: str | None = None) -> list[dict]:
    """Planned and unplanned stops far too long for their state - a 260 h
    "maintenance", a two-day "changeover", a "down" left open over a weekend.
    Medians are per line per state over all usable history. Durations are the
    device's own, which already exclude interruptions."""
    import statistics

    with closing(_ro(cache_path)) as db:
        rows = db.execute(
            "SELECT device_id, process_state_event_id, process_state, performance_impact, "
            "COALESCE(process_state_reason_display_name, process_state_reason), start_time, end_time, "
            "duration_s, production_day_number, part "
            "FROM state_event x LEFT JOIN device_config_epoch e USING (device_id, config_epoch) "
            "WHERE x.deleted_at IS NULL AND COALESCE(e.usable, 1) = 1 AND duration_s > 0 "
            "AND performance_impact IN ('planned_stop', 'unplanned_stop')").fetchall()
    per: dict[tuple, list[float]] = {}
    for r in rows:
        per.setdefault((r[0], r[2]), []).append(r[7])
    med = {k: statistics.median(v) for k, v in per.items()}
    out = []
    for d, eid, state, imp, reason, st, en, dur, day, part in rows:
        if dev and d != dev:
            continue
        m = med[(d, state)]
        if dur > rule.cap_hours * 3600 or (dur > rule.floor_hours * 3600 and dur > rule.multiple * m):
            out.append({"device_id": d, "event_id": eid, "state": state, "impact": imp, "reason": reason,
                        "start": _ts(st), "end": _ts(en), "seconds": dur, "day": day, "part": part,
                        "median_seconds": m, "times_median": dur / m if m else None})
    return sorted(out, key=lambda r: -r["seconds"])


# -------------------------------------------------------- run/stop rhythm

def weekly_rhythm(cache_path, dev: str, lo: int, hi: int, exclude_ids=(), part: str | None = None) -> list[dict]:
    """Per Monday-start week: mean minutes of a run burst (a 'run' state
    event) and of an unplanned stop. Shortening run bursts lead falling
    availability."""
    p_extra, p_args = _of_part(part)
    with closing(_ro(cache_path)) as db:
        rows = db.execute(
            f"SELECT production_day_number, performance_impact, duration_s FROM state_event x {_USABLE} "
            f"AND performance_impact IN ('run', 'unplanned_stop') AND duration_s > 0{_not_in(exclude_ids)[0]}{p_extra}",
            (dev, lo, hi, *_not_in(exclude_ids)[1], *p_args)).fetchall()
    weeks: dict[str, dict] = {}
    for day, imp, dur in rows:
        w = weeks.setdefault(period_key("week", day), {"run_n": 0, "run_s": 0.0, "stop_n": 0, "stop_s": 0.0})
        if imp == "run":
            w["run_n"] += 1
            w["run_s"] += dur
        else:
            w["stop_n"] += 1
            w["stop_s"] += dur
    out = []
    for k in sorted(weeks):
        w = weeks[k]
        out.append({"week": k, "run_bursts": w["run_n"], "stops": w["stop_n"],
                    "run_min": w["run_s"] / w["run_n"] / 60 if w["run_n"] else None,
                    "stop_min": w["stop_s"] / w["stop_n"] / 60 if w["stop_n"] else None})
    return out


# ------------------------------------------------------------ changeovers

def changeovers(cache_path, dev: str, lo: int, hi: int, exclude_event_ids: set[int] = frozenset()) -> list[dict]:
    """Every change from one part run to the next, with what it cost.

    The sequence comes from part_run (the device's own runs); a change is run
    i -> run i+1. Cost = planned-stop time (changeover, setup, lot change) from
    the last running before run i+1 starts to the first running inside it.
    Unplanned downs in that window are left out (they are not the
    changeover's cost), and so are events flagged implausible - a parked line
    labelled "maintenance" would otherwise swamp the cell. Same part twice in
    a row is a new job on the same part; kept, on the matrix diagonal.
    Changes are counted in the period of the run they lead into. When runs
    follow each other with no running between them their windows overlap, so
    each stop is charged once, to the first change it falls in."""
    with closing(_ro(cache_path)) as db:
        runs = db.execute(
            "SELECT part, job, start_time, end_time, production_day_number FROM part_run x "
            "LEFT JOIN device_config_epoch e USING (device_id, config_epoch) "
            "WHERE device_id = ? AND x.deleted_at IS NULL AND COALESCE(e.usable, 1) = 1 ORDER BY start_time",
            (dev,)).fetchall()
        evs = db.execute(
            "SELECT process_state_event_id, start_time, end_time, performance_impact, process_state, duration_s "
            "FROM state_event WHERE device_id = ? AND deleted_at IS NULL AND end_time > start_time "
            "ORDER BY start_time", (dev,)).fetchall()
    evs = [(eid, _ts(s), _ts(e), imp, ps, max(0.0, d or 0.0)) for eid, s, e, imp, ps, d in evs]
    run_starts = [s for _, s, _, imp, *_ in evs if imp == "run"]
    run_ends = [e for _, _, e, imp, *_ in evs if imp == "run"]
    import bisect

    out = []
    charged: set[int] = set()   # each stop counts once, against the first change it belongs to
    for (a, a_job, *_), (b, b_job, b_start, b_end, b_day) in zip(runs, runs[1:]):
        if not a or not b or b_day is None:
            continue
        in_period = lo <= b_day <= hi
        bs, be = _ts(b_start), _ts(b_end)
        ends_before = [e for e in run_ends if e <= bs]
        last_run = max(ends_before) if ends_before else bs
        i = bisect.bisect_left(run_starts, bs)
        first_run = run_starts[i] if i < len(run_starts) and run_starts[i] < be else be
        cost, flagged = 0.0, 0.0
        for eid, s, e, imp, ps, d in evs:
            if s >= first_run:
                break
            if imp == "planned_stop" and s >= last_run and eid not in charged:
                charged.add(eid)
                if eid in exclude_event_ids:
                    flagged += d
                else:
                    cost += d
        if not in_period:
            continue
        out.append({"from": a, "to": b, "from_job": a_job, "to_job": b_job, "start": bs,
                    "seconds": cost, "excluded_seconds": flagged, "same_part": a == b})
    return out


# ------------------------------------------------------------ product pages

def part_runs(cache_path, dev: str, lo: int, hi: int, exclude_ids=()) -> list[dict]:
    """Every closed part run on one line starting in [lo, hi] (usable epochs),
    with the unplanned stops that started inside it.

    Run, stop and ideal hours are part_run's own, verbatim. Down hours for
    the charts are re-summed from the state events inside the run so that
    events flagged implausible can be left out; with none flagged the two
    agree to the second on every run but one (FR8 ESTRADIOL from 21 Sep:
    13 s, in the device's own total). Events are assigned
    to a run by time, not by the event's part field, which also carries the
    still-open run's part (AreaD's OTRIVIN, open since 25 Aug)."""
    import bisect

    excl = set(exclude_ids or ())
    with closing(_ro(cache_path)) as db:
        runs = db.execute(
            f"SELECT part, job, start_time, end_time, run_time, unplanned_stop_time, planned_stop_time, ideal_time, "
            f"run_cycles, production_day_number FROM part_run x {_USABLE} AND end_time IS NOT NULL "
            f"ORDER BY start_time", (dev, lo, hi)).fetchall()
        if not runs:
            return []
        evs = db.execute(
            "SELECT process_state_event_id, start_time, duration_s, "
            "COALESCE(process_state_reason_display_name, process_state_reason, 'Unassigned') "
            "FROM state_event x LEFT JOIN device_config_epoch e USING (device_id, config_epoch) "
            "WHERE device_id = ? AND x.deleted_at IS NULL AND COALESCE(e.usable, 1) = 1 "
            "AND performance_impact = 'unplanned_stop' AND start_time >= ? AND start_time < ? ORDER BY start_time",
            (dev, runs[0][2], max(r[3] for r in runs))).fetchall()
    out = []
    for part, job, st, en, run, unp, pln, ideal, cycles, day in runs:
        out.append({"device_id": dev, "part": part, "job": job, "start": _ts(st), "end": _ts(en), "day": day,
                    "run_s": run or 0.0, "unp_s": unp or 0.0, "pln_s": pln or 0.0, "ideal_s": ideal or 0.0,
                    "cycles": cycles or 0.0, "ict": (ideal / cycles) if cycles and cycles > 200 and ideal else None,
                    "down_s": 0.0, "flagged_n": 0, "flagged_s": 0.0, "reasons": {}})
    starts = [r["start"] for r in out]
    for eid, st, dur, reason in evs:
        s = _ts(st)
        i = bisect.bisect_right(starts, s) - 1
        if i < 0 or s >= out[i]["end"]:
            continue
        r = out[i]
        if eid in excl:
            r["flagged_n"] += 1
            r["flagged_s"] += dur or 0.0
            continue
        r["down_s"] += dur or 0.0
        n_s = r["reasons"].setdefault(reason, [0, 0.0])
        n_s[0] += 1
        n_s[1] += dur or 0.0
    for r in out:
        r["ratio"] = r["down_s"] / r["run_s"] if r["run_s"] else None
    return out


def part_days(cache_path, part: str) -> dict[str, list[int]]:
    """Production days on which each line started a closed run of this part,
    usable epochs only. Cheap: lets a page with nothing to show say so."""
    with closing(_ro(cache_path)) as db:
        rows = db.execute(
            "SELECT device_id, production_day_number FROM part_run x "
            "LEFT JOIN device_config_epoch e USING (device_id, config_epoch) "
            "WHERE part = ? AND x.deleted_at IS NULL AND COALESCE(e.usable, 1) = 1 AND end_time IS NOT NULL "
            "AND production_day_number IS NOT NULL ORDER BY 1, 2", (part,)).fetchall()
    out: dict[str, list[int]] = {}
    for dev, day in rows:
        out.setdefault(dev, []).append(day)
    return out


def ict_history(cache_path, dev: str, part: str) -> list[dict]:
    """Derived ideal cycle time (ideal_time / run_cycles) of every run over
    200 cycles of this part on this line, all usable history, oldest first."""
    with closing(_ro(cache_path)) as db:
        rows = db.execute(
            "SELECT start_time, ideal_time / run_cycles, run_cycles FROM part_run x "
            "LEFT JOIN device_config_epoch e USING (device_id, config_epoch) "
            "WHERE device_id = ? AND part = ? AND x.deleted_at IS NULL AND COALESCE(e.usable, 1) = 1 "
            "AND run_cycles > 200 ORDER BY start_time", (dev, part)).fetchall()
    return [{"start": _ts(st), "ict": v, "cycles": n} for st, v, n in rows]


ICT_TOLERANCE = 0.03   # runs within 3% of a level's median belong to it


def ict_levels(points: list[dict], tolerance: float = ICT_TOLERANCE) -> dict:
    """Split a part's run-by-run ideal cycle times into configuration levels.

    The XL computes ideal time as cycles x the ideal cycle time configured for
    the part, so on an unchanged configuration the derived value barely moves
    (FR8's ESTRADIOL: 88 runs within 2%). A run starts a new level when it is
    more than `tolerance` off the current one AND the next run agrees with it;
    a run off its level that the next run does not confirm is a single
    outlier (a change made mid-run, or a counting glitch), not a
    reconfiguration. Returns levels (median value, first and last run),
    the outlier indices, and the changes between consecutive levels."""
    import statistics

    vals = [p["ict"] for p in points]
    if not vals:
        return {"levels": [], "outliers": set(), "changes": []}
    groups, outliers = [[0]], set()
    for i in range(1, len(vals)):
        level = statistics.median(vals[j] for j in groups[-1])
        if abs(vals[i] / level - 1) <= tolerance:
            groups[-1].append(i)
        elif i + 1 < len(vals) and abs(vals[i + 1] / vals[i] - 1) <= tolerance:
            groups.append([i])
        else:
            outliers.add(i)
    # A one-run level between others is an outlier too; then rejoin neighbours that now agree.
    if len(groups) > 1:
        for g in [g for g in groups if len(g) == 1]:
            outliers.add(g[0])
        groups = [g for g in groups if len(g) > 1] or groups[:1]
    merged = [groups[0]]
    for g in groups[1:]:
        a = statistics.median(vals[j] for j in merged[-1])
        b = statistics.median(vals[j] for j in g)
        if abs(b / a - 1) <= tolerance:
            merged[-1] = merged[-1] + g
        else:
            merged.append(g)
    levels = []
    for g in merged:
        members = [j for j in range(g[0], g[-1] + 1) if j not in outliers]
        levels.append({"value": statistics.median(vals[j] for j in members), "first": g[0], "last": g[-1],
                       "start": points[g[0]]["start"], "end": points[g[-1]]["start"], "runs": len(members)})
    changes = [{"index": b["first"], "start": b["start"], "before": a["value"], "after": b["value"],
                "shift": b["value"] / a["value"] - 1} for a, b in zip(levels, levels[1:])]
    for i, p in enumerate(points):
        p["outlier"] = i in outliers
    return {"levels": levels, "outliers": outliers, "changes": changes}
