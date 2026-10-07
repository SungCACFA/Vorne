#!/usr/bin/env python3
"""Is the sync correct, not merely running?

Checks the local cache against the devices and against what the brief says
must hold. Two severities:

  FAIL  the sync is wrong (totals disagree with the device, accounting broken)
  FLAG  the data has changed shape in a way the app must not ignore

    python verify.py                 all devices
    python verify.py --days 14       compare the last 14 production days
    python verify.py --offline       skip the device comparison

Exit 1 if any check FAILs. FLAGs do not fail the run.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

import yaml

from xlpipe.client import XLClient
from xlpipe.metrics import num, positional_events

FAIL, FLAG, OK, SKIP = "FAIL", "FLAG", "ok", "skip"
results: list[tuple[str, str]] = []


def say(level: str, check: str, detail: str = "") -> None:
    results.append((level, check))
    print(f"[{level:^4}] {check}")
    for line in detail.splitlines():
        print(f"       {line}")


# The totals compared between cache and device, for shift_hour.
SUMS = ("run_time", "unplanned_stop_time", "planned_stop_time", "not_scheduled_time",
        "ideal_time", "in_count", "good_count", "reject_count")


def check_against_device(db, dev: dict, days: int) -> None:
    """Sum the last N production days of closed hour records in the cache,
    and ask the device to aggregate the same records itself (group=none).

    Closed is expressed as "event id below the current event". Not
    sync_id[gt]=-1: verified on FR8 that group=none silently ignores the
    sync_id filter and folds the open hour into the total."""
    dev_id = dev["id"]
    last = db.execute("SELECT max(production_day_number) FROM hour_record WHERE device_id=?", (dev_id,)).fetchone()[0]
    if last is None:
        say(SKIP, f"{dev_id}: cache vs device", "no hour records cached")
        return
    from_day = last - days
    ours = db.execute(
        f"SELECT count(*), {', '.join(f'sum({c})' for c in SUMS)} FROM hour_record "
        "WHERE device_id=? AND deleted_at IS NULL AND production_day_number >= ? AND production_day_number <= ?",
        (dev_id, from_day, last)).fetchone()
    try:
        client = XLClient(dev["host"], timeout=60.0)
        current = positional_events(client.get_json("api/v0/channels/shift_hour/events/current",
                                                    {"fields": "shift_hour_event_id"}), ["shift_hour_event_id"])
        closed = {"shift_hour_event_id[lt]": str(current[0]["shift_hour_event_id"]),
                  "production_day_number[gte]": str(from_day), "production_day_number[lte]": str(last)}
        payload = client.get_json("api/v0/channels/shift_hour/events",
                                  {"fields": ",".join(SUMS), "group": "none", **closed})
        theirs_row = positional_events(payload, list(SUMS))[0]
        count = len(positional_events(client.get_json("api/v0/channels/shift_hour/events", {
            "fields": "shift_hour_event_id", "limit": "100000", **closed}), ["shift_hour_event_id"]))
    except Exception as exc:  # noqa: BLE001
        say(SKIP, f"{dev_id}: cache vs device", f"device unreachable: {exc}")
        return
    diffs = []
    if ours[0] != count:
        diffs.append(f"records: cache {ours[0]}, device {count}")
    for i, c in enumerate(SUMS, start=1):
        a, b = ours[i] or 0.0, num(theirs_row.get(c)) or 0.0
        if abs(a - b) > max(1e-6, 1e-9 * abs(b)):
            diffs.append(f"{c}: cache {a:,.3f}, device {b:,.3f}")
    span = f"production days {from_day}-{last}, {ours[0]} closed hour records"
    if diffs:
        say(FAIL, f"{dev_id}: cache vs device totals", span + "\n" + "\n".join(diffs)
            + "\nRun a refresh first; if it persists, the sync is dropping or double-counting.")
    else:
        say(OK, f"{dev_id}: cache vs device totals", span + "; all " + str(len(SUMS)) + " totals identical")


def check_accounting(db, dev_id: str) -> None:
    """Median gap over full hours within a few seconds; report the spread;
    flag rows beyond +-180 s. Nothing asserted on non-full rows."""
    gaps = sorted(r[0] for r in db.execute(
        "SELECT acct_gap_s FROM hour_record WHERE device_id=? AND is_full_hour AND deleted_at IS NULL "
        "AND acct_gap_s IS NOT NULL", (dev_id,)))
    if not gaps:
        say(SKIP, f"{dev_id}: time accounting", "no full hours")
        return
    med = statistics.median(gaps)
    pct = lambda p: gaps[min(len(gaps) - 1, int(len(gaps) * p))]  # noqa: E731
    beyond = [g for g in gaps if abs(g) > 180]
    detail = (f"{len(gaps)} full hours: median {med:+.3f}s, p1 {pct(.01):+.1f}s, p99 {pct(.99):+.1f}s, "
              f"min {gaps[0]:+.1f}s, max {gaps[-1]:+.1f}s")
    if abs(med) > 5:
        say(FAIL, f"{dev_id}: time accounting", detail + "\nmedian residual is more than 5 s off zero")
    elif beyond:
        say(FLAG, f"{dev_id}: time accounting", detail + f"\n{len(beyond)} full hour(s) beyond +-180 s")
    else:
        say(OK, f"{dev_id}: time accounting", detail)


def check_negative_durations(db, dev_id: str) -> None:
    """Events that end before they start, on any fact table with times.
    Suspected device clock corrections; kept as reported, shown as 0."""
    found = []
    for table, key, dur in (("state_event", "process_state_event_id", "duration_s"),
                            ("part_run", "part_event_id", "duration_s"), ("job_run", "job_event_id", "duration_s"),
                            ("hour_record", "shift_hour_event_id", None), ("shift_record", "shift_event_id", None)):
        cond = "end_time < start_time" + (f" OR {dur} < 0" if dur else "")
        for k, st, en in db.execute(f"SELECT {key}, start_time, end_time FROM {table} WHERE device_id=? "
                                    f"AND deleted_at IS NULL AND ({cond})", (dev_id,)):
            found.append(f"{table} {k}: {st[:19]}Z -> {en[:19]}Z")
    if found:
        say(FLAG, f"{dev_id}: negative durations", f"{len(found)} event(s) end before they start "
            "- suspected clock correction:\n" + "\n".join(found))
    else:
        say(OK, f"{dev_id}: negative durations", "none")


def check_part_job_1to1(db, dev_id: str) -> None:
    """Today every part run has a job run with identical boundaries. That is
    a configuration artefact, not a guarantee: a changeover mid-job breaks
    it. The app must not assume it; this says when it stops holding."""
    unmatched_part = db.execute(
        "SELECT count(*) FROM part_run p WHERE p.device_id=? AND p.deleted_at IS NULL AND NOT EXISTS ("
        " SELECT 1 FROM job_run j WHERE j.device_id=p.device_id AND j.deleted_at IS NULL"
        " AND j.start_time=p.start_time AND j.end_time=p.end_time)", (dev_id,)).fetchone()[0]
    unmatched_job = db.execute(
        "SELECT count(*) FROM job_run j WHERE j.device_id=? AND j.deleted_at IS NULL AND NOT EXISTS ("
        " SELECT 1 FROM part_run p WHERE p.device_id=j.device_id AND p.deleted_at IS NULL"
        " AND p.start_time=j.start_time AND p.end_time=j.end_time)", (dev_id,)).fetchone()[0]
    n = db.execute("SELECT count(*) FROM part_run WHERE device_id=? AND deleted_at IS NULL", (dev_id,)).fetchone()[0]
    if unmatched_part or unmatched_job:
        say(FLAG, f"{dev_id}: part/job runs 1:1",
            f"{unmatched_part} part run(s) without a matching job run, {unmatched_job} job run(s) without a "
            f"matching part run.\nJobs and parts no longer change together on this line.")
    else:
        say(OK, f"{dev_id}: part/job runs 1:1", f"all {n} part runs match a job run exactly")


def check_reject_identity(db, dev_id: str) -> None:
    """reject = in - good everywhere except a handful of unexplained rows."""
    rows = db.execute(
        "SELECT shift_hour_event_id, start_time, in_count, good_count, reject_count FROM hour_record "
        "WHERE device_id=? AND deleted_at IS NULL AND abs(reject_count - (in_count - good_count)) > 1e-6 "
        "ORDER BY start_time", (dev_id,)).fetchall()
    if rows:
        say(FLAG, f"{dev_id}: reject = in - good", f"{len(rows)} hour(s) where it does not hold "
            "- suspected manual count entry:\n" + "\n".join(
            f"id {r[0]} {r[1][:16]}Z in {r[2]:g} good {r[3]:g} reject {r[4]:g}" for r in rows))
    else:
        say(OK, f"{dev_id}: reject = in - good", "holds on every hour")


def check_id_continuity(db, dev: dict, offline: bool) -> None:
    """Every closed shift_hour id the device holds must be in the cache.

    The device itself skips ids now and then (AreaB has no 2759 or 2811,
    with no gap in time), so holes in the sequence are only a failure when
    the device has the record and we do not."""
    dev_id = dev["id"]
    ours = {r[0] for r in db.execute("SELECT shift_hour_event_id FROM hour_record WHERE device_id=? "
                                     "AND deleted_at IS NULL", (dev_id,))}
    if not ours:
        say(SKIP, f"{dev_id}: shift_hour ids", "no rows cached")
        return
    lo, hi = min(ours), max(ours)
    holes = sorted(set(range(lo, hi + 1)) - ours)
    if offline:
        say(FLAG if holes else OK, f"{dev_id}: shift_hour ids",
            f"ids {lo}-{hi}; not in cache: {holes or 'none'} (offline: cannot tell device gaps from misses)")
        return
    try:
        got = XLClient(dev["host"], timeout=60.0).get_json("api/v0/channels/shift_hour/events", {
            "fields": "shift_hour_event_id", "sync_id[gt]": "-1", "limit": "100000",
            "shift_hour_event_id[lte]": str(hi)})
        theirs = {r["shift_hour_event_id"] for r in positional_events(got, ["shift_hour_event_id"])}
    except Exception as exc:  # noqa: BLE001
        say(SKIP, f"{dev_id}: shift_hour ids", f"device unreachable: {exc}")
        return
    missed = sorted(theirs - ours)
    extra = sorted(ours - theirs)
    device_gaps = sorted(set(holes) - theirs)
    detail = f"ids {lo}-{hi}; skipped by the device itself: {device_gaps or 'none'}"
    if missed:
        say(FAIL, f"{dev_id}: shift_hour ids", detail + f"\non the device but not cached: {missed[:20]}")
    elif extra:
        say(FLAG, f"{dev_id}: shift_hour ids", detail + f"\ncached but gone from the device "
            f"(should have been flagged deleted): {extra[:20]}")
    else:
        say(OK, f"{dev_id}: shift_hour ids", detail + "; every device record is cached")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=7, help="production days to compare against the device")
    ap.add_argument("--offline", action="store_true", help="skip the device comparison")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    path = Path(cfg.get("cache_path", "cache/xl_cache.db"))
    if not path.exists():
        print(f"no cache at {path}; run: python -m xlpipe refresh")
        return 1
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    for dev in cfg["devices"]:
        print(f"\n== {dev['id']}")
        if not args.offline:
            check_against_device(db, dev, args.days)
        check_accounting(db, dev["id"])
        check_id_continuity(db, dev, args.offline)
        check_reject_identity(db, dev["id"])
        check_negative_durations(db, dev["id"])
        check_part_job_1to1(db, dev["id"])

    fails = sum(1 for lvl, _ in results if lvl == FAIL)
    flags = sum(1 for lvl, _ in results if lvl == FLAG)
    print(f"\n{fails} failure(s), {flags} flag(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
