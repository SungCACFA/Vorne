"""Is the 1.5-4.9x 'slower than standard' real, or a per-piece/per-cycle
units error in the XL configuration?

Four independent checks, printed per part per line. Run from the folder
holding cache/.
"""

import glob
import os
import sqlite3

hits = (
    glob.glob("cache/*.db")
    + glob.glob("*.db")
    + glob.glob("**/cache/*.db", recursive=True)
)
if not hits:
    raise SystemExit("No .db found. Run this from the folder containing cache/")

path = hits[0]
print(f"cache: {os.path.abspath(path)}\n")
con = sqlite3.connect(path)
con.row_factory = sqlite3.Row


def q(sql, *args):
    return con.execute(sql, args).fetchall()


USABLE = """
  and exists (
    select 1 from device_config_epoch e
     where e.device_id = r.device_id
       and e.config_epoch = r.config_epoch
       and e.usable = 1
  )
"""

# ---------------------------------------------------------------- check 1

print("=" * 108)
print("1. STANDARDS, per part per line   (closed runs, >200 cycles, usable epochs)")
print("=" * 108)
print(
    f"{'Line':7}{'Part':26}{'Runs':>5}{'Cycles':>11}"
    f"{'ICT dev':>9}{'ICT der':>9}{'Actual':>9}{'Ratio':>7}"
    f"{'pc/cyc':>8}{'tgt pc/cyc':>11}"
)
print("-" * 108)

rows = q(
    f"""
    select r.device_id, r.part,
           count(*)                        as runs,
           sum(r.run_cycles)               as cycles,
           sum(r.run_time)                 as run_s,
           sum(r.ideal_time)               as ideal_s,
           sum(r.good_count)               as good,
           sum(r.in_count)                 as infeed,
           sum(r.target_count)             as tgt_count,
           sum(r.target_cycles)            as tgt_cycles,
           avg(r.ideal_cycle_time)         as ict_device
      from part_run r
     where r.deleted_at is null
       and r.end_time is not null
       and r.run_cycles > 200
       and r.run_time > 0
       {USABLE}
     group by r.device_id, r.part
     having sum(r.run_cycles) > 1000
     order by r.device_id, sum(r.run_time) desc
    """
)


def num(v, places=3, dash="-"):
    if v is None:
        return dash
    return f"{v:.{places}f}"


summary = []
for r in rows:
    cycles = r["cycles"] or 0
    ict_derived = (r["ideal_s"] / cycles) if cycles else None
    actual = (r["run_s"] / cycles) if cycles else None
    ratio = (actual / ict_derived) if (actual and ict_derived) else None
    pc_per_cycle = (r["good"] / cycles) if (cycles and r["good"]) else None
    tgt_pc = (
        (r["tgt_count"] / r["tgt_cycles"])
        if (r["tgt_cycles"] and r["tgt_count"])
        else None
    )
    summary.append((r["device_id"], r["part"], ratio, pc_per_cycle, tgt_pc))
    print(
        f"{r['device_id']:7}{(r['part'] or '?')[:25]:26}{r['runs']:>5}{cycles:>11,}"
        f"{num(r['ict_device']):>9}{num(ict_derived):>9}{num(actual):>9}"
        f"{num(ratio, 2):>7}{num(pc_per_cycle, 2):>8}{num(tgt_pc, 2):>11}"
    )

print(
    "\nICT dev = the device's own ideal_cycle_time field.  "
    "ICT der = ideal_time / run_cycles."
)
print(
    "pc/cyc  = good_count / run_cycles  (actual pieces per cycle, if counts were sound)"
)
print(
    "tgt pc/cyc = target_count / target_cycles  (what the DEVICE believes per cycle)"
)

# ---------------------------------------------------------------- check 2

print("\n" + "=" * 108)
print("2. THE DECISIVE TEST")
print("=" * 108)
print("If ICT was entered per PIECE where XL expects per CYCLE, then the true")
print("ratio is (ratio / pieces-per-cycle), and it should land near 1.00.\n")
print(f"{'Line':7}{'Part':26}{'Ratio':>8}{'pc/cyc':>9}{'Corrected':>11}  verdict")
print("-" * 108)

for dev, part, ratio, pc, _tgt in summary:
    if not (ratio and pc):
        continue
    corrected = ratio / pc
    if 0.85 <= corrected <= 1.20:
        verdict = "UNITS ERROR - runs at standard once converted"
    elif corrected < 0.85:
        verdict = "overcorrects - faster than standard"
    else:
        verdict = "still slow after conversion"
    print(
        f"{dev:7}{(part or '?')[:25]:26}{ratio:>8.2f}{pc:>9.2f}"
        f"{corrected:>11.2f}  {verdict}"
    )

# ---------------------------------------------------------------- check 3

print("\n" + "=" * 108)
print("3. DOES THE DEVICE'S OWN ICT FIELD AGREE WITH ideal_time / run_cycles?")
print("=" * 108)
print("A mismatch would mean our derivation is wrong rather than the config.\n")

rows = q(
    f"""
    select r.device_id, r.part, r.ideal_cycle_time as ict, r.ideal_time, r.run_cycles
      from part_run r
     where r.deleted_at is null
       and r.end_time is not null
       and r.run_cycles > 200
       and r.ideal_cycle_time is not null
       {USABLE}
    """
)
agree = off = 0
worst = []
for r in rows:
    derived = r["ideal_time"] / r["run_cycles"]
    ict = r["ict"]
    if ict and abs(derived - ict) / ict < 0.02:
        agree += 1
    else:
        off += 1
        if ict:
            worst.append((abs(derived - ict) / ict, r["device_id"], r["part"], ict, derived))
print(f"  agree within 2%: {agree}     disagree: {off}     of {len(rows)} runs")
for pct, dev, part, ict, derived in sorted(worst, reverse=True)[:5]:
    print(f"    {dev:7}{(part or '?')[:24]:25} device {ict:.4f}  derived {derived:.4f}  ({pct:.0%})")

# ---------------------------------------------------------------- check 4

print("\n" + "=" * 108)
print("4. SANITY: implied rates for the largest part on each line")
print("=" * 108)
print(f"{'Line':7}{'Part':26}{'Std cyc/h':>11}{'Act cyc/h':>11}{'Std pc/h':>11}{'Act pc/h':>11}")
print("-" * 108)

seen = set()
for r in rows[:0]:
    pass

rows2 = q(
    f"""
    select r.device_id, r.part,
           sum(r.run_cycles) as cycles, sum(r.run_time) as run_s,
           sum(r.ideal_time) as ideal_s, sum(r.good_count) as good
      from part_run r
     where r.deleted_at is null and r.end_time is not null
       and r.run_cycles > 200 and r.run_time > 0
       {USABLE}
     group by r.device_id, r.part
     order by r.device_id, sum(r.run_time) desc
    """
)
for r in rows2:
    if r["device_id"] in seen:
        continue
    seen.add(r["device_id"])
    cycles, run_s, ideal_s = r["cycles"], r["run_s"], r["ideal_s"]
    if not (cycles and run_s and ideal_s):
        continue
    ict = ideal_s / cycles
    act = run_s / cycles
    pc = (r["good"] / cycles) if r["good"] else None
    std_cyc = 3600 / ict
    act_cyc = 3600 / act
    print(
        f"{r['device_id']:7}{(r['part'] or '?')[:25]:26}{std_cyc:>11,.0f}{act_cyc:>11,.0f}"
        f"{(std_cyc*pc) if pc else 0:>11,.0f}{(act_cyc*pc) if pc else 0:>11,.0f}"
    )

print("\nAsk engineering which of these four numbers matches the machine's")
print("rated speed. That single answer settles the whole question.")
con.close()