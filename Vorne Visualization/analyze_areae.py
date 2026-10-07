"""AreaE: why do singles run 2x worse than triples?

Four singles variants occupy the four worst performance slots on that line,
every triples and 4FOR3 variant sits near the best. This tests whether the
cause is stop frequency, stop duration, specific reasons, changeover, or
something that changed over time.

Run from the folder holding cache/.
"""

import glob
import os
import sqlite3
import statistics as st

hits = (
    glob.glob("cache/*.db")
    + glob.glob("*.db")
    + glob.glob("**/cache/*.db", recursive=True)
)
if not hits:
    raise SystemExit("No .db found. Run from the folder containing cache/")

con = sqlite3.connect(hits[0])
con.row_factory = sqlite3.Row
print(f"cache: {os.path.abspath(hits[0])}\n")

LINE = "AreaE"


def fmt_of(part):
    """Classify a part name by pack format."""
    if not part:
        return "?"
    p = part.upper()
    if "SINGLE" in p:
        return "singles"
    if "TRIPLE" in p:
        return "triples"
    if "4FOR3" in p:
        return "4for3"
    return "other"


USABLE = """
  and exists (select 1 from device_config_epoch e
               where e.device_id = r.device_id
                 and e.config_epoch = r.config_epoch
                 and e.usable = 1)
"""

# ------------------------------------------------------------------ run level

runs = con.execute(
    f"""
    select r.part, r.job, r.start_time, r.end_time,
           r.run_time, r.unplanned_stop_time, r.planned_stop_time,
           r.run_cycles, r.ideal_time, r.small_stops, r.duration_s
      from part_run r
     where r.device_id = ?
       and r.deleted_at is null and r.end_time is not null
       and r.run_cycles > 1000 and r.run_time > 0
       {USABLE}
     order by r.start_time
    """,
    (LINE,),
).fetchall()

print("=" * 100)
print("1. EVERY RUN ON AreaE, BY PACK FORMAT")
print("=" * 100)
print(f"{'Started':11}{'Format':9}{'Part':26}{'Cycles':>10}{'s/cyc':>8}"
      f"{'Ratio':>7}{'Stop h':>8}{'Small':>7}")
print("-" * 100)

for r in runs:
    cycles = r["run_cycles"]
    actual = r["run_time"] / cycles
    ict = r["ideal_time"] / cycles if r["ideal_time"] else None
    stop_h = ((r["unplanned_stop_time"] or 0) + (r["planned_stop_time"] or 0)) / 3600
    print(
        f"{(r['start_time'] or '')[:10]:11}{fmt_of(r['part']):9}"
        f"{(r['part'] or '?')[:25]:26}{cycles:>10,.0f}{actual:>8.3f}"
        f"{(actual/ict) if ict else 0:>7.2f}{stop_h:>8.1f}"
        f"{(r['small_stops'] or 0):>7.0f}"
    )

# ------------------------------------------------------------- format summary

print("\n" + "=" * 100)
print("2. FORMAT SUMMARY")
print("=" * 100)

groups = {}
for r in runs:
    groups.setdefault(fmt_of(r["part"]), []).append(r)

print(f"{'Format':10}{'Runs':>6}{'Cycles':>12}{'Run h':>9}{'s/cyc':>8}{'Ratio':>7}"
      f"{'Stop h':>9}{'Stop/Run':>10}{'Small/kcyc':>12}")
print("-" * 100)

for fmt in ("triples", "4for3", "singles", "other"):
    rs = groups.get(fmt)
    if not rs:
        continue
    cycles = sum(r["run_cycles"] for r in rs)
    run_s = sum(r["run_time"] for r in rs)
    ideal_s = sum(r["ideal_time"] or 0 for r in rs)
    stop_s = sum((r["unplanned_stop_time"] or 0) + (r["planned_stop_time"] or 0) for r in rs)
    small = sum(r["small_stops"] or 0 for r in rs)
    actual = run_s / cycles
    ratio = actual / (ideal_s / cycles) if ideal_s else 0
    print(
        f"{fmt:10}{len(rs):>6}{cycles:>12,.0f}{run_s/3600:>9.1f}{actual:>8.3f}"
        f"{ratio:>7.2f}{stop_s/3600:>9.1f}{stop_s/run_s:>10.2f}"
        f"{small/(cycles/1000):>12.1f}"
    )

print("\ns/cyc and Ratio measure SPEED while running.")
print("Stop/Run and Small/kcyc measure INTERRUPTION. If singles are slow on")
print("speed but normal on stops, the cause is the machine setup for that")
print("format. If they stop more, it is a reliability problem.")

# ----------------------------------------------------------- small stops

print("\n" + "=" * 100)
print("3. SMALL STOPS - the usual cause of a slow cycle time")
print("=" * 100)
print("A 'small stop' is too brief to be logged as downtime, so it shows up")
print("as slow running instead. This is where format problems usually hide.\n")
print(f"{'Part':28}{'Cycles':>11}{'Small stops':>13}{'per 1k cyc':>12}{'Ratio':>8}")
print("-" * 100)

per_part = con.execute(
    f"""
    select r.part,
           sum(r.run_cycles) as cycles, sum(r.small_stops) as small,
           sum(r.run_time) as run_s, sum(r.ideal_time) as ideal_s
      from part_run r
     where r.device_id = ?
       and r.deleted_at is null and r.end_time is not null
       and r.run_cycles > 1000
       {USABLE}
     group by r.part
     order by sum(r.run_cycles) desc
    """,
    (LINE,),
).fetchall()

for r in per_part:
    cycles = r["cycles"]
    small = r["small"] or 0
    ratio = (r["run_s"] / cycles) / (r["ideal_s"] / cycles) if r["ideal_s"] else 0
    print(
        f"{(r['part'] or '?')[:27]:28}{cycles:>11,.0f}{small:>13,.0f}"
        f"{small/(cycles/1000):>12.1f}{ratio:>8.2f}"
    )

# ------------------------------------------------------------ stop reasons

print("\n" + "=" * 100)
print("4. STOP REASONS BY FORMAT")
print("=" * 100)
print("Does a characteristic failure mode attach to singles?\n")

events = con.execute(
    """
    select s.part, s.process_state_reason_display_name as reason,
           s.performance_impact, s.duration_s
      from state_event s
     where s.device_id = ?
       and s.deleted_at is null
       and s.duration_s > 0
       and s.performance_impact in ('unplanned_stop','planned_stop')
       and s.part is not null
    """,
    (LINE,),
).fetchall()

tally = {}
fmt_hours = {}
for e in events:
    fmt = fmt_of(e["part"])
    if fmt in ("?", "other"):
        continue
    key = (fmt, e["reason"] or "(no reason)", e["performance_impact"])
    t = tally.setdefault(key, [0, 0.0])
    t[0] += 1
    t[1] += e["duration_s"] / 3600
    fmt_hours[fmt] = fmt_hours.get(fmt, 0) + e["duration_s"] / 3600

for fmt in ("triples", "singles", "4for3"):
    rows = sorted(
        [(k, v) for k, v in tally.items() if k[0] == fmt],
        key=lambda kv: -kv[1][1],
    )[:10]
    if not rows:
        continue
    total = fmt_hours.get(fmt, 0)
    print(f"  {fmt.upper()}  ({total:.1f} stop hours total)")
    print(f"    {'Reason':40}{'Impact':16}{'Events':>8}{'Hours':>8}{'Avg min':>9}{'Share':>7}")
    for (_f, reason, impact), (n, hours) in rows:
        print(
            f"    {reason[:39]:40}{impact[:15]:16}{n:>8}{hours:>8.1f}"
            f"{hours*60/n:>9.0f}{hours/total if total else 0:>7.0%}"
        )
    print()

# ----------------------------------------------------------------- over time

print("=" * 100)
print("5. HAS IT ALWAYS BEEN THIS WAY?")
print("=" * 100)
print("Ratio by month and format. A step change points at a date; a flat")
print("line points at the format itself.\n")

by_month = {}
for r in runs:
    month = (r["start_time"] or "")[:7]
    fmt = fmt_of(r["part"])
    if fmt in ("?", "other"):
        continue
    k = (month, fmt)
    acc = by_month.setdefault(k, [0, 0, 0])
    acc[0] += r["run_time"]
    acc[1] += r["ideal_time"] or 0
    acc[2] += r["run_cycles"]

months = sorted({k[0] for k in by_month})
print(f"{'Month':10}{'triples':>12}{'4for3':>12}{'singles':>12}")
print("-" * 100)
for m in months:
    cells = []
    for fmt in ("triples", "4for3", "singles"):
        acc = by_month.get((m, fmt))
        if acc and acc[1]:
            cells.append(f"{acc[0]/acc[1]:>12.2f}")
        else:
            cells.append(f"{'-':>12}")
    print(f"{m:10}" + "".join(cells))

con.close()