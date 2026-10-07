"""Does run length explain the performance ratios across all four lines?

AreaE's singles look 60% worse than triples, but the one large singles run
performed like triples. If that holds fleet-wide, the performance numbers are
substantially measuring lot size, and the fix is scheduling rather than
maintenance.

Run from the folder holding cache/.
"""

import glob
import math
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

USABLE = """
  and exists (select 1 from device_config_epoch e
               where e.device_id = r.device_id
                 and e.config_epoch = r.config_epoch
                 and e.usable = 1)
"""

runs = con.execute(
    f"""
    select r.device_id, r.part, r.start_time,
           r.run_cycles, r.run_time, r.ideal_time,
           r.unplanned_stop_time, r.planned_stop_time, r.small_stops
      from part_run r
     where r.deleted_at is null and r.end_time is not null
       and r.run_cycles > 500 and r.run_time > 0 and r.ideal_time > 0
       {USABLE}
     order by r.device_id, r.run_cycles
    """
).fetchall()

rows = []
for r in runs:
    cycles = r["run_cycles"]
    rows.append(
        {
            "line": r["device_id"],
            "part": r["part"] or "?",
            "cycles": cycles,
            "ratio": (r["run_time"] / cycles) / (r["ideal_time"] / cycles),
            "stop_h": ((r["unplanned_stop_time"] or 0) + (r["planned_stop_time"] or 0)) / 3600,
            "small_per_k": (r["small_stops"] or 0) / (cycles / 1000),
        }
    )

# ------------------------------------------------------------------ buckets

BUCKETS = [
    ("under 2k", 0, 2_000),
    ("2k - 5k", 2_000, 5_000),
    ("5k - 10k", 5_000, 10_000),
    ("10k - 25k", 10_000, 25_000),
    ("25k - 50k", 25_000, 50_000),
    ("over 50k", 50_000, math.inf),
]

print("=" * 96)
print("1. PERFORMANCE RATIO BY RUN LENGTH - ALL FOUR LINES POOLED")
print("=" * 96)
print(f"{'Run size':12}{'Runs':>6}{'Cycles':>12}{'Median ratio':>14}"
      f"{'Mean ratio':>12}{'Small/kcyc':>12}")
print("-" * 96)

for label, lo, hi in BUCKETS:
    sel = [r for r in rows if lo <= r["cycles"] < hi]
    if not sel:
        continue
    ratios = [r["ratio"] for r in sel]
    print(
        f"{label:12}{len(sel):>6}{sum(r['cycles'] for r in sel):>12,}"
        f"{st.median(ratios):>14.2f}{sum(ratios)/len(ratios):>12.2f}"
        f"{st.median([r['small_per_k'] for r in sel]):>12.1f}"
    )

# -------------------------------------------------------------- per line

print("\n" + "=" * 96)
print("2. THE SAME, PER LINE")
print("=" * 96)

for line in sorted({r["line"] for r in rows}):
    sel_line = [r for r in rows if r["line"] == line]
    print(f"\n  {line}   {len(sel_line)} runs")
    print(f"  {'Run size':12}{'Runs':>6}{'Cycles':>12}{'Median ratio':>14}")
    print("  " + "-" * 44)
    for label, lo, hi in BUCKETS:
        sel = [r for r in sel_line if lo <= r["cycles"] < hi]
        if not sel:
            continue
        print(
            f"  {label:12}{len(sel):>6}{sum(r['cycles'] for r in sel):>12,}"
            f"{st.median([r['ratio'] for r in sel]):>14.2f}"
        )

# ------------------------------------------------------- correlation

print("\n" + "=" * 96)
print("3. CORRELATION, log(cycles) vs ratio")
print("=" * 96)
print("Strongly negative means long runs perform better. Computed per line so")
print("a line's own standard cannot confound it.\n")
print(f"{'Line':8}{'Runs':>6}{'Correlation':>14}  reading")
print("-" * 96)


def pearson(xs, ys):
    n = len(xs)
    if n < 4:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else None


for line in sorted({r["line"] for r in rows}):
    sel = [r for r in rows if r["line"] == line]
    c = pearson([math.log(r["cycles"]) for r in sel], [r["ratio"] for r in sel])
    if c is None:
        continue
    if c < -0.6:
        reading = "run length dominates"
    elif c < -0.3:
        reading = "run length matters"
    elif c < -0.1:
        reading = "weak effect"
    else:
        reading = "run length does NOT explain it"
    print(f"{line:8}{len(sel):>6}{c:>14.2f}  {reading}")

allc = pearson([math.log(r["cycles"]) for r in rows], [r["ratio"] for r in rows])
print(f"\n{'POOLED':8}{len(rows):>6}{allc:>14.2f}")

# ------------------------------------------- same part, short vs long

print("\n" + "=" * 96)
print("4. THE CLEANEST TEST: SAME PART, SHORT RUNS vs LONG RUNS")
print("=" * 96)
print("Same product, same line, same standard. Only lot size differs.\n")
print(f"{'Line':7}{'Part':26}{'Short runs':>12}{'ratio':>7}"
      f"{'Long runs':>12}{'ratio':>7}{'Gap':>8}")
print("-" * 96)

groups = {}
for r in rows:
    groups.setdefault((r["line"], r["part"]), []).append(r)

gaps = []
for (line, part), sel in sorted(groups.items()):
    if len(sel) < 4:
        continue
    sel = sorted(sel, key=lambda r: r["cycles"])
    half = len(sel) // 2
    short, long = sel[:half], sel[-half:]
    rs = st.median([r["ratio"] for r in short])
    rl = st.median([r["ratio"] for r in long])
    gaps.append(rs / rl if rl else 1)
    print(
        f"{line:7}{part[:25]:26}{st.median([r['cycles'] for r in short]):>12,.0f}"
        f"{rs:>7.2f}{st.median([r['cycles'] for r in long]):>12,.0f}{rl:>7.2f}"
        f"{rs/rl if rl else 0:>8.2f}x"
    )

if gaps:
    print(f"\n  Median gap across {len(gaps)} parts: {st.median(gaps):.2f}x")
    print("  That is how much of the 'performance problem' is lot size,")
    print("  measured within a single product so nothing else varies.")

# ------------------------------------------------------- what it is worth

print("\n" + "=" * 96)
print("5. WHAT CONSOLIDATION WOULD BE WORTH")
print("=" * 96)
print("If every run under 10k cycles had instead run at the median ratio of")
print("runs over 25k on the same line.\n")
print(f"{'Line':8}{'Short runs':>12}{'Cycles':>12}{'Run h now':>12}"
      f"{'Run h at long-run rate':>24}{'Saving h':>10}")
print("-" * 96)

total_saving = 0.0
for line in sorted({r["line"] for r in rows}):
    sel_line = [r for r in rows if r["line"] == line]
    longs = [r["ratio"] for r in sel_line if r["cycles"] >= 25_000]
    shorts = [r for r in sel_line if r["cycles"] < 10_000]
    if not longs or not shorts:
        continue
    target = st.median(longs)
    cycles = sum(r["cycles"] for r in shorts)
    # run hours now, and at the long-run ratio, using each run's own standard
    now_h = sum(r["cycles"] * r["ratio"] for r in shorts) / 3600
    then_h = sum(r["cycles"] * target for r in shorts) / 3600
    # normalise: ratio is unitless, so scale by the line's mean ideal s/cycle
    scale = sum(r["cycles"] for r in sel_line) and 1.0
    saving = (now_h - then_h) * scale
    total_saving += saving
    print(
        f"{line:8}{len(shorts):>12}{cycles:>12,}{now_h:>12.0f}"
        f"{then_h:>24.0f}{saving:>10.0f}"
    )

print(f"\n  Total indicative saving: {total_saving:,.0f} run hours over the period.")
print("  Indicative only - hours here are cycles x ratio, so they are in units")
print("  of 'ideal seconds', not clock hours. Treat as a relative scale.")

con.close()