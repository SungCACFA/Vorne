"""Is ideal cycle time a per-product standard, or one nameplate speed per line?

If per-product, cycle times should spread widely across pack formats on the
same machine. If nameplate, they cluster tightly and the spread is noise.

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

USABLE = """
  and exists (select 1 from device_config_epoch e
               where e.device_id = r.device_id
                 and e.config_epoch = r.config_epoch
                 and e.usable = 1)
"""

# Per-part ICT, every part with any usable closed run.
rows = con.execute(
    f"""
    select r.device_id, r.part,
           count(*) as runs,
           sum(r.run_cycles) as cycles,
           sum(r.ideal_time) as ideal_s,
           sum(r.run_time)   as run_s
      from part_run r
     where r.deleted_at is null and r.end_time is not null
       and r.run_cycles > 50 and r.ideal_time > 0
       {USABLE}
     group by r.device_id, r.part
     order by r.device_id, sum(r.ideal_time) / sum(r.run_cycles)
    """
).fetchall()

by_line = {}
for r in rows:
    ict = r["ideal_s"] / r["cycles"]
    actual = r["run_s"] / r["cycles"]
    by_line.setdefault(r["device_id"], []).append(
        (r["part"], ict, actual, r["runs"], r["cycles"])
    )

print("=" * 104)
print("1. EVERY PART'S IDEAL CYCLE TIME, GROUPED BY LINE")
print("=" * 104)

for line, parts in by_line.items():
    icts = [p[1] for p in parts]
    med = st.median(icts)
    spread = (max(icts) - min(icts)) / med if med else 0
    print(f"\n  {line}   {len(parts)} parts   median ICT {med:.3f}s   "
          f"full spread {spread:.0%} of median")
    print(f"  {'Part':30}{'ICT':>8}{'vs med':>9}{'Actual':>9}{'Ratio':>7}{'Runs':>6}{'Cycles':>11}")
    print("  " + "-" * 94)
    for part, ict, actual, runs, cycles in parts:
        dev = (ict / med - 1) if med else 0
        flag = "  <-- differs" if abs(dev) > 0.10 else ""
        print(f"  {(part or '?')[:29]:30}{ict:>8.3f}{dev:>+8.0%}"
              f"{actual:>9.3f}{actual/ict:>7.2f}{runs:>6}{cycles:>11,}{flag}")

# ---------------------------------------------------------------- verdict

print("\n" + "=" * 104)
print("2. VERDICT PER LINE")
print("=" * 104)
print(f"{'Line':8}{'Parts':>7}{'Median':>9}{'Min':>8}{'Max':>8}{'Spread':>9}"
      f"{'Within 5%':>11}  reading")
print("-" * 104)

for line, parts in by_line.items():
    icts = [p[1] for p in parts]
    med = st.median(icts)
    within = sum(1 for v in icts if abs(v / med - 1) <= 0.05)
    spread = (max(icts) - min(icts)) / med if med else 0
    share = within / len(icts)
    if share >= 0.8:
        reading = "ONE NAMEPLATE SPEED, copied to every part"
    elif share >= 0.5:
        reading = "mostly nameplate, a few parts configured"
    else:
        reading = "genuinely per-product standards"
    print(f"{line:8}{len(parts):>7}{med:>9.3f}{min(icts):>8.3f}{max(icts):>8.3f}"
          f"{spread:>9.0%}{share:>11.0%}  {reading}")

# ---------------------------------------------------------------- pack formats

print("\n" + "=" * 104)
print("3. SAME PRODUCT FAMILY, DIFFERENT PACK SIZE")
print("=" * 104)
print("Different formats must run at different cycle times. If they don't,")
print("the standard was never set per product.\n")

FAMILIES = ("ADVIL", "PREMARIN", "VOLTACARE", "CLEANSING_GEL", "ABREVA",
            "ROGAINE", "REGAINE", "OTRIVIN", "OMNARIS", "XHANCE")

for fam in FAMILIES:
    members = []
    for line, parts in by_line.items():
        for part, ict, actual, runs, cycles in parts:
            if part and part.upper().startswith(fam):
                members.append((line, part, ict, actual, cycles))
    if len(members) < 2:
        continue
    icts = [m[2] for m in members]
    span = (max(icts) - min(icts)) / st.median(icts)
    print(f"  {fam}  ({len(members)} variants, ICT span {span:.0%})")
    for line, part, ict, actual, cycles in sorted(members, key=lambda m: m[2]):
        print(f"      {line:7}{part[:32]:33}ICT {ict:.3f}   actual {actual:.3f}"
              f"   {actual/ict:>5.2f}x   {cycles:>10,} cyc")
    print()

# ---------------------------------------------------------------- useful part

print("=" * 104)
print("4. WHAT IS STILL VALID: relative performance within a line")
print("=" * 104)
print("Every part on a line shares one baseline, so ratios BETWEEN parts are")
print("comparable even though the absolute standard is wrong.\n")

for line, parts in by_line.items():
    ranked = sorted(
        [p for p in parts if p[4] > 20000], key=lambda p: p[2] / p[1], reverse=True
    )
    if len(ranked) < 2:
        continue
    print(f"  {line}  (parts over 20k cycles, slowest first)")
    best = ranked[-1]
    for part, ict, actual, runs, cycles in ranked:
        rel = (actual / ict) / (best[2] / best[1])
        print(f"      {(part or '?')[:32]:33}{actual/ict:>6.2f}x standard"
              f"   {rel:>5.2f}x the best part on this line")
    print()

con.close()