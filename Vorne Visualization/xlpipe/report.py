"""What the cache holds, and where it disagrees with the brief.

Plain text, read-only. Dates are shown in plant time (America/Toronto);
production days as their date, labelled "production day".
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .channels import SPECS

TZ = ZoneInfo("America/Toronto")


def _local(ts: str | None) -> str:
    if not ts:
        return "-"
    return datetime.fromisoformat(ts).astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def _pday(n: int | None) -> str:
    return (date(1970, 1, 1) + timedelta(days=n)).isoformat() if n is not None else "-"


def report(cfg: dict) -> int:
    path = Path(cfg.get("cache_path", "cache/xl_cache.db"))
    if not path.exists():
        print(f"no cache at {path}; run: python -m xlpipe refresh")
        return 1
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731

    hb = q("SELECT last_heartbeat, last_run_ok, devices_ok, devices_total FROM sync_heartbeat")
    if hb:
        print(f"last refresh {_local(hb[0][0])}  ok={bool(hb[0][1])}  devices {hb[0][2]}/{hb[0][3]}")

    for dev_id, host, name, fw, dv, model, mac in q(
            "SELECT device_id, host, asset_name, firmware, data_version, model, mac FROM device ORDER BY device_id"):
        print(f"\n=== {dev_id}  {host}  asset {name}  {model}  fw {fw}  data model {dv}  {mac}")
        print(f"  {'channel':14} {'rows':>6} {'del':>4} {'first start':16}   {'last end':16}  "
              f"{'production days':23} {'resume':7}")
        for spec in SPECS.values():
            t, k = spec.table, spec.key
            time_cols = "min(start_time), max(end_time)" if spec.has_end_time else "NULL, NULL"
            n, dead, first, lastend, d0, d1 = q(
                f"SELECT count(*) FILTER (WHERE deleted_at IS NULL), count(*) FILTER (WHERE deleted_at IS NOT NULL), "
                f"{time_cols}, min(production_day_number), max(production_day_number) "
                f"FROM {t} WHERE device_id = ?", dev_id)[0]
            st = q("SELECT resume_key, last_error FROM sync_state WHERE device_id=? AND channel=?", dev_id, spec.channel)
            resume = st[0][0] if st else "-"
            print(f"  {spec.channel:14} {n:6} {dead:4} {_local(first):16} -> {_local(lastend):16}  "
                  f"{_pday(d0)} -> {_pday(d1)}  {resume}")
            if st and st[0][1]:
                print(f"      ERROR {st[0][1]}")

        missing = q("SELECT channel, field FROM channel_field WHERE device_id=? AND present=0 ORDER BY 1,2", dev_id)
        print("  fields missing vs brief: " + (", ".join(f"{c}.{f}" for c, f in missing) or "none"))

        # ---- the brief's section 3/4 claims, checked
        h = q("""SELECT count(*),
                        sum(is_full_hour),
                        sum(reject_count < 0),
                        sum(abs(reject_count - (in_count - good_count)) > 1e-6),
                        sum(good_count > in_count),
                        sum(is_count_adjustment),
                        sum(ideal_time > run_time AND run_time > 0)
                 FROM hour_record WHERE device_id=? AND deleted_at IS NULL""", dev_id)[0]
        gaps = [r[0] for r in q("SELECT acct_gap_s FROM hour_record WHERE device_id=? AND is_full_hour "
                                "AND deleted_at IS NULL AND acct_gap_s IS NOT NULL", dev_id)]
        print(f"  hour_record: {h[0]} rows, {h[1]} full hours; negative rejects {h[2]}; "
              f"reject != in-good {h[3]}; good > in {h[4]}; count-adjustment signature {h[5]}; "
              f"performance > 1 {h[6]}")
        if gaps:
            gaps.sort()
            big = sum(abs(g) > 180 for g in gaps)
            print(f"  time accounting (full hours): median gap {statistics.median(gaps):+.3f}s, "
                  f"p1 {gaps[len(gaps)//100]:+.1f}s, p99 {gaps[len(gaps)*99//100]:+.1f}s, "
                  f"min {gaps[0]:+.1f}s, max {gaps[-1]:+.1f}s; beyond +-180s: {big}")
        spans = q("SELECT 'part', count(*) FROM part_run WHERE device_id=? AND production_day_number_raw IS NOT NULL "
                  "UNION ALL SELECT 'job', count(*) FROM job_run WHERE device_id=? AND production_day_number_raw IS NOT NULL "
                  "UNION ALL SELECT 'state', count(*) FROM state_event WHERE device_id=? AND production_day_number_raw IS NOT NULL",
                  dev_id, dev_id, dev_id)
        print("  production_day_number not a single int: " + ", ".join(f"{a} {b}" for a, b in spans))
        impacts = q("SELECT performance_impact, count(*), round(sum(duration_s)/3600.0, 1) FROM state_event "
                    "WHERE device_id=? AND deleted_at IS NULL GROUP BY 1 ORDER BY 3 DESC", dev_id)
        print("  process_state by performance_impact (events, hours): "
              + "; ".join(f"{i} {n} / {hrs}h" for i, n, hrs in impacts))

    print("\nconfiguration epochs:")
    for dev, ep, frm, usable, trusted, why in q(
            "SELECT device_id, config_epoch, effective_from, usable, counts_trusted, exclusion_reason "
            "FROM device_config_epoch ORDER BY 1, 2"):
        n = q("SELECT count(*) FROM hour_record WHERE device_id=? AND config_epoch=?", dev, ep)[0][0]
        print(f"  {dev:6} epoch {ep}  from {frm:32} usable={bool(usable)!s:5} counts_trusted={bool(trusted)!s:5} "
              f"{n:5} hour rows" + (f"  [{why}]" if why else ""))
    firsts = []
    for (dev,) in q("SELECT device_id FROM device ORDER BY 1"):
        bad = q("SELECT max(h.production_day_number) FROM hour_record h JOIN device_config_epoch e "
                "USING (device_id, config_epoch) WHERE h.device_id=? AND e.usable=0", dev)[0][0]
        first = bad + 1 if bad is not None else q("SELECT min(production_day_number) FROM hour_record "
                                                    "WHERE device_id=?", dev)[0][0]
        firsts.append(first)
        print(f"  {dev:6} usable history from production day {_pday(first)}")
    print(f"  fleet window (all four usable): production day {_pday(max(firsts))} onward")

    quar = q("SELECT device_id, channel, count(*) FROM sync_quarantine WHERE resolved_at IS NULL GROUP BY 1, 2")
    print("\nquarantine (open): " + (", ".join(f"{d}/{c} {n}" for d, c, n in quar) or "empty"))
    return 0
