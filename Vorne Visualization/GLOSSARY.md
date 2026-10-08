# Glossary source text

Authoritative wording for the clickable definitions in `webapp/glossary.py`.

Every entry has:

- **definition** — one sentence, plain words
- **formula** — monospace; omit where there isn't one
- **answers** — the question a person is actually asking
- **source** — where the definition comes from (people ask)
- **caution** — what catches people out; omit only where there genuinely isn't one

Sources used:

- **Vorne Vocab: Metrics & Dimensions**, rev. 2023-06-22 — the vendor's own
  definitions. Authoritative.
- **Vorne XL Tech Note TN000406**, *Understanding Run Cycles and Small Stops*,
  rev. 2022-09-16.
- **Vorne XL API endpoint reference**, v2.22.
- **Danbro device data** — findings established by querying the four lines
  directly. Flagged as such so nobody mistakes an observation for a vendor
  definition.

Where our figure matches Vorne's own definition, the entry says so. That
matters: it means our numbers tie out against XL's built-in reports.

---

## Time measures

### Available

**definition:** Of the time the line was scheduled for production, how much of
it was actually running.

**formula:** `run time ÷ production time`, where production time = run +
unplanned stop + planned stop

**answers:** Did it stay up?

**source:** Vorne Vocab — matches XL's own Availability exactly.

**caution:** Planned stops are in the denominator, so changeover and
maintenance pull this down. That is Vorne's definition and our figure ties out
against XL's. Note it differs from the textbook OEE convention, which excludes
planned downtime from the denominator and therefore reads higher. Uses complete
one-hour records only, because partial records distort a rate.

### Performance

**definition:** While running, how close the line came to its configured rate.

**formula:** `ideal time ÷ run time`

**answers:** Did it run fast?

**source:** Vorne Vocab — matches XL's own Performance exactly.

**caution:** Measured against the ideal cycle time configured on the device.
Vorne intends that to be set per part; on these four lines it is mostly one
nameplate speed copied across every part, so treat it as a relative measure
between products on the same line rather than distance from an achievable
target. See *Ideal cycle time*.

### Calendar use

**definition:** Of all clock time, how much was scheduled for production at
all.

**formula:** `production time ÷ all time`

**answers:** Are we using the asset?

**source:** Vorne Vocab — this is XL's **Utilization** metric, renamed here.

**caution:** Renamed from "Utilised" because the previous in-house report used
that label for a different calculation — it counted complete hours only, which
excluded the long unscheduled overnight blocks, the exact denominator this
measure exists to capture. It was reporting schedule adherence, not asset use.
Our figure matches Vorne's Utilization; the old one did not.

### Scheduled (production time)

**definition:** Time the line was meant to be producing, whether running or
stopped.

**formula:** `run + unplanned stop + planned stop`

**answers:** How much time did we intend to produce in?

**source:** Vorne Vocab calls this **Production Time**.

**caution:** Excludes unscheduled time. Most disagreements between two reports
come down to one using production time and the other all time. Vorne also
defines **Manufacturing Time** = run + down, which excludes planned stops — a
third denominator to watch for.

### Elapsed (all time)

**definition:** All clock time in the period, scheduled or not.

**formula:** `production time + not scheduled time`

**answers:** How much wall-clock time are we talking about?

**source:** Vorne Vocab calls this **All Time**.

**caution:** On a part run, elapsed minus run minus stopped leaves unscheduled
time, usually the largest share. The run tables do not show it, because a part
run is not a scheduling window.

### Running

**definition:** The machine was cycling and producing.

**answers:** How much of the time was it actually making something?

**source:** Vorne Vocab — the Run impact state.

**caution:** Cycles slower than ideal still count as running time; the excess
becomes a performance loss rather than downtime. Only once a cycle passes the
down threshold does it become an availability loss. See *Small stops*.

### Unplanned stop (down)

**definition:** Down when it should have been running — breakdowns, jams,
waiting on components.

**answers:** What went wrong?

**source:** Vorne Vocab — Down Time and Unplanned Stop Time are the same
metric in XL.

**caution:** Reason assignment often happens after the fact, so recent stops
may show unassigned and fill in later. The sync picks up those edits.

### Planned stop

**definition:** Not producing, but intended — changeover, setup, scheduled
maintenance, breaks.

**answers:** What did we choose not to produce through?

**source:** Vorne Vocab — the Planned Stop impact state.

**caution:** Some long events on these lines are labelled maintenance but look
like a parked line, which inflates planned stop and pulls availability down.
Those are flagged as implausible durations. *(Danbro device data)*

### Not scheduled

**definition:** No shift was running. Nights, weekends, idle lines.

**answers:** When was nobody there?

**source:** Vorne Vocab — the Not Scheduled impact state.

**caution:** Usually the largest of the four buckets. Excluding it is what made
the previous report's utilisation figure look so much better than reality.

### MTBF

**definition:** How long the line runs on average before a fault stops it.

**formula:** `run time ÷ down events`

**answers:** How reliable is it between failures?

**source:** Vorne Vocab.

### MTTR

**definition:** How long it takes on average to get running again after a
fault.

**formula:** `down time ÷ down events`

**answers:** How quickly do we recover?

**source:** Vorne Vocab.

---

## OEE

### OEE

**definition:** The share of scheduled production time that was fully
productive — running, at full speed, making only good pieces.

**formula:** `availability × performance × quality`

**answers:** How much of our scheduled time actually turned into good product?

**source:** Vorne Vocab. 100% OEE means perfect production: always running, as
fast as possible, no rejects.

**caution:** Currently withheld on these four lines, because Quality cannot be
computed — see *Quality*. The fault inflates quality rather than depressing it,
so an OEE figure taken from these machines reads better than reality.

OEE has been reported from these devices in the past, and those figures were
not reliable. This is recoverable: once reject scanning and the count inputs
are configured, Quality becomes real and OEE returns, per line, from the date
it is fixed.

Availability and Performance are published on their own in the meantime,
because both derive from time, which reconciles to the second.

Note that OEE multiplies its three factors, so it is unforgiving. A quality
figure of 1.48 does not make OEE slightly wrong; it makes it meaningless. It
also cannot be partially published — two of three factors is not an OEE.

### Availability loss

**definition:** The share of scheduled time lost because the line was not
running at all.

**formula:** `100% − availability`

**answers:** How much did stoppages cost us?

**source:** Vorne Vocab. Accounts for both planned and unplanned stops.

**caution:** Includes changeover and maintenance, because Vorne puts planned
stops inside production time. A line with heavy changeover will show a large
availability loss without anything being broken.

### Performance loss

**definition:** The share of scheduled time lost to running slower than the
ideal cycle time.

**formula:** `(100% − performance) × availability`

**answers:** How much did slow running cost us?

**source:** Vorne Vocab.

**caution:** Not simply `1 − performance`. It is scaled by availability,
because you can only lose speed during time you were actually running. This
catches people out: a line with 50% performance and 50% availability has a 25%
performance loss, not 50%. The three OEE losses are built this way so they add
up with OEE to exactly 100% of scheduled time.

### Quality loss

**definition:** The share of scheduled time lost to making pieces that did not
meet standard.

**formula:** `(100% − quality) × availability × performance`

**answers:** How much did rejects cost us?

**source:** Vorne Vocab. Includes anything needing rework.

**caution:** Withheld here along with Quality. Scaled by both availability and
performance for the same reason as performance loss — you can only make a
reject during time you were running and producing.

### OEE loss

**definition:** The share of scheduled production time that was not productive.

**formula:** `100% − OEE`, equivalently availability loss + performance loss +
quality loss

**answers:** How much of our scheduled time did we not get value from?

**source:** Vorne Vocab.

**caution:** The three component losses are deliberately constructed so that
they plus OEE total 100% of scheduled time. That is what makes them comparable
against each other, and it is why each is scaled by the factors ahead of it.

### Fully productive time

**definition:** The time it would have taken to make only the good pieces, at
full speed, with no stoppages.

**formula:** `percent good × ideal time`

**answers:** What was the genuinely productive core of our time?

**source:** Vorne Vocab.

**caution:** Depends on piece counts, so withheld here. It is OEE expressed in
hours rather than as a percentage, which many people find easier to act on.

### Hidden factory time

**definition:** Untapped capacity — the additional production available without
buying anything.

**formula:** `OEE lost time + schedule lost time`

**answers:** How much more could we make with what we already own?

**source:** Vorne Vocab.

**caution:** Represents around-the-clock perfect production, so it is a ceiling
rather than a target. Useful for framing capacity arguments, not for setting
goals. Depends on OEE, so withheld here; Calendar use shows the scheduling half
of it on its own.

### Six big losses

**definition:** Vorne's breakdown of where production time goes: down events,
planned stops, cycle losses, small stops, startup rejects and production
rejects.

**answers:** Which category of loss is costing us most?

**source:** Vorne Vocab.

**caution:** The first four are published here, as they derive from time and
cycles. The two reject categories depend on counts and are withheld. Note that
cycle loss and small stop loss both count only the time in **excess** of ideal
cycle time, not the whole cycle.

### Efficiency

**definition:** How production compares against the takt-based target.

**formula:** `good count ÷ target count`

**answers:** Are we hitting the plan?

**source:** Vorne Vocab. Identical to its Rate Efficiency metric. 100% or above
means the target was met.

**caution:** A different measure from Performance, and frequently confused with
it. Performance compares against the ideal cycle time with no losses budgeted;
Efficiency compares against takt time, which budgets for expected downtime,
slow cycles and rejects. Withheld here because it depends on good count.

---

## Cycle and output measures

### Cycle

**definition:** One stroke of the machine.

**answers:** What is the unit the device counts?

**source:** Vorne Vocab.

**caution:** A cycle is not a piece. Vorne splits cycles three ways by
duration: **run cycles** (up to the small-stop threshold, default 500% of ideal
cycle time), **small stops** (between that and the down threshold, default 3
minutes), and **partial cycles** (one per run event, covering the boundaries).
Total Cycles is all three added together. *(TN000406)*

### Piece

**definition:** One sellable unit.

**answers:** How many did we actually make?

**source:** Vorne Vocab.

**caution:** Not reliably counted on these machines. See *Quality* and
*Reject count* under measures deliberately absent. Cycle counts are sound;
piece counts are not.

### Ideal cycle time (ICT)

**definition:** The theoretical fastest time to complete one cycle for the part
being run.

**formula:** `ideal time ÷ total cycles`

**answers:** What should one stroke take?

**source:** Vorne Vocab — configured per part on the device; our figure is
Vorne's **Avg. Ideal Cycle Time**, recovered from the device's own ideal time.

**caution:** This is a configured value read back, not a measurement. Vorne
intends it per part. On these four lines it is largely a single nameplate speed
copied across every part — all four ADVIL variants return 0.54545455 s, which is
6/11 s: a rate of 6,600 cycles per hour, entered once as the line's nameplate
speed. OTRIVIN differs between AreaB and AreaD because the standard tracks the
machine rather than the product. By total cycles every run of a part returns the
identical value — FR8 ESTRADIOL is exactly 0.375 s across all 88 runs — which
confirms it is a configured constant rather than a measurement. Every variance
figure is only as good as this number. *(Danbro device data)*

### Actual s/cycle

**definition:** What one cycle really took, averaged over the period.

**formula:** `run hours × 3600 ÷ total cycles`

**answers:** How long per stroke, in practice?

**source:** Vorne Vocab — this is **Avg. Cycle Time**, total cycle time over
total cycles.

**caution:** Running time only. Stopped time is excluded, so this measures
speed rather than uptime.

### vs derived

**definition:** How far actual cycle time sits from the configured standard.

**formula:** `actual s/cycle ÷ ideal cycle time`

**answers:** How far off standard?

**source:** Derived here; it is Performance inverted.

**caution:** 1.00× is on standard, above is slower. A 2.00× ratio is 50%
performance. Inherits the standards problem above, so compare products on the
same line rather than reading the absolute number.

### Earned hours

**definition:** The hours the output should have taken at standard.

**formula:** `total cycles × ideal cycle time ÷ 3600`

**answers:** What was the output worth, in hours?

**source:** Vorne Vocab — this is **Ideal Time** expressed in hours.

**caution:** Think of a quoted job: quoted at three hours, took eight — the
mechanic earned three and burned eight. Measures speed only; stoppages are not
in it. Note Vorne also has an **Earned Labor** metric, which is a different
thing entirely and unavailable here. See *Labour*.

### Variance hours

**definition:** Hours lost to running slower than standard.

**formula:** `actual run hours − earned hours`

**answers:** How much capacity did slow running cost?

**source:** Derived here. Vorne's nearest equivalents are **Cycle Lost Time**
and **Small Stop Lost Time**, two of its Six Big Losses.

**caution:** Positive means slower than standard. Does not include stopped time
at all — a part can show large variance while the line barely stopped, or none
while it stood still for days. Variance is Performance; stopped hours are
Availability. Vorne notes that a *negative* lost time means the ideal cycle
time is set too high, which is a useful check in the other direction.

### Run h

**definition:** Hours the machine was cycling.

**answers:** How long was it actually producing?

**source:** Vorne Vocab — **Run Time**.

**caution:** Excludes stopped and unscheduled time, so it will not reconcile
against elapsed time on its own. On a part run, elapsed minus run minus stopped
leaves unscheduled time, which is usually the largest share.

### Stopped h

**definition:** Hours scheduled but not running — unplanned and planned stops
together.

**answers:** How long was it down while staffed?

**source:** Derived here from Vorne's Down Time and Planned Stop Time.

**caution:** Not the same as Vorne's **Down Time**, which is unplanned only.
Where a column is headed "Down h" it means unplanned stops alone.

### Unplanned h per run h

**definition:** Unplanned stop hours for every hour the line spent running this
product.

**formula:** `unplanned stop hours ÷ run hours`

**answers:** Does this product break down more than the others on the same
machine?

**source:** Derived here.

**caution:** Counts unplanned stops only — setup and changeover are planned
stops and are not in it. Only meaningful between products on the same line,
where the machine, crew and stop reasons are shared; comparing across lines
compares different equipment. Implausible stops are excluded.

### Small stops

**definition:** Cycles longer than the small-stop threshold but shorter than
the down threshold.

**answers:** Is it hesitating rather than stopping?

**source:** Vorne Vocab and TN000406. Default thresholds are 500% of ideal
cycle time, and 3 minutes, both configurable per part on the device.

**caution:** These never appear as downtime — the excess over ideal cycle time
becomes a performance loss instead, which is why a line can look slow with no
stop reason explaining it. Compare per thousand cycles rather than as a raw
count.

### Run cycles

**definition:** Cycles completed within the small-stop threshold — the machine
running normally.

**answers:** How much did it do at proper speed?

**source:** Vorne Vocab.

**caution:** Not the same as Total Cycles, which adds small stops and partial
cycles. Vorne computes ideal time from **total** cycles, so any cycle-time
figure must use the same denominator or it comes out inflated.

### Total cycles

**definition:** Every cycle, however long it took.

**formula:** `run cycles + small stops + partial cycles`

**answers:** How many strokes in total?

**source:** Vorne Vocab.

**caution:** The correct denominator for any average cycle time, because
Vorne's ideal time is ideal cycle time × total cycles.

### Equipment cycles

**definition:** Every cycle across all production states, not only while
running.

**answers:** How much wear has the machine taken?

**source:** Vorne Vocab — intended for cycle-based maintenance programmes.

**caution:** All other cycle metrics count only during run events. This one
does not, so it will be higher.

### Takt time

**definition:** The expected pace of production for the part, including
budgeted losses.

**answers:** What pace are we meant to hold?

**source:** Vorne Vocab — set per part, drives the target counter.

**caution:** Not the same as ideal cycle time. Takt time budgets for downtime,
slow cycles and rejects; ideal cycle time is the theoretical best with no
losses. Takt drives Vorne's **Efficiency** metric (good count over target
count), which is a different measure from Performance.

---

## Structure and context

### Production day

**definition:** The plant's own day, starting with its first shift.

**answers:** Which day does this belong to?

**source:** Vorne Vocab — "production days are aligned to shift boundaries so
that shifts crossing midnight stay within one day."

**caution:** Starts at 23:00 the previous evening where a third shift runs,
otherwise 07:00. An unscheduled overnight block stays with the day before, so a
production day can exceed 24 hours. Never equals a midnight-to-midnight
calendar day. Vorne's production weeks always run Monday to Sunday. *(Danbro
device data for the shift times)*

### Open run

**definition:** A part or job run the device has not closed yet.

**answers:** Is this still accumulating?

**source:** Derived here.

**caution:** Excluded from every total, because partial hours against partial
cycles give meaningless ratios. A run open far longer than that part's normal
duration usually means someone forgot to close it on the device, and everything
produced since is accumulating into one bucket. *(Danbro device data)*

### Changeover hour

**definition:** An hour in which more than one part ran.

**answers:** Why are two products on one row?

**source:** Derived here from the device's own reporting.

**caution:** The device reports every part that ran in the hour, joined by a
slash. The order is the device's and is not guaranteed chronological.

### Production phase

**definition:** Which stage of a part run an event falls in — changeover,
startup, or steady state.

**answers:** Was this a settled run or still ramping?

**source:** Vorne Vocab.

**caution:** Vorne notes these phases have different loss characteristics,
which is why startup rejects are tracked separately from production rejects.

### Implausible duration

**definition:** A stop far longer than anything normal for its category.

**answers:** Is this a real stop or a mislabelled parked line?

**source:** Derived here.

**caution:** A 56-hour "down" or a 260-hour "maintenance" distorts every
average it touches. These are excluded from the charts but kept in the tables,
which is why a table and a chart can rank the same reason differently.
*(Danbro device data)*

### Config epoch

**definition:** A period during which a device's configuration was unchanged.

**answers:** Are these numbers comparable with those?

**source:** Derived here.

**caution:** Each device recorded the wrong timezone when first installed,
which shifts shift and production-day boundaries. Those periods are kept but
excluded by default rather than silently dropped. *(Danbro device data)*

---

## Measures deliberately absent

### Quality

**status:** Withheld.

**source of the definition:** Vorne Vocab — `good count ÷ total count`, where
total count = good count + reject count.

**why it is withheld:** The reject sensor is not wired on any of the four
lines, and no reject reason has ever been scanned in the entire history. In its
place `reject count` is returning the same value as Vorne's **WIP Count**
(in count − good count), which we confirmed holds on every row. That makes
total count equal to in count, so Quality is computing good ÷ infeed rather
than good ÷ produced, and exceeds 100% whenever the good-count sensor leads the
infeed sensor. Negative rejects appear on 923 hourly records on FR8 and
roughly 400 on each of the other three lines.

XL's arithmetic is correct. Its count inputs are miswired. *(Danbro device
data)*

### OEE

**status:** Withheld. See *OEE* in the main glossary for the definition and why it is currently
withheld.

### TEEP

**status:** Withheld. TEEP is `OEE × Utilization` (Vorne Vocab); see *OEE* in the main glossary for
why it is currently withheld.

### Labour

**status:** Not available from the devices.

**source of the definition:** Vorne Vocab — Total Labor accumulates each second
of production time multiplied by **Current Team Size**, entered by an operator.
Labor Efficiency is earned labor ÷ total labor.

**why it is unavailable:** Across more than 1,800 shifts on all four lines, crew size has
never been entered once, so total labor is zero everywhere and labor efficiency
cannot be computed. Labour standards do exist on many parts — earned labor is
populated on around 840 shifts — so the expectation is configured but no actuals are
being captured. Labour hours must come from SAP. *(Danbro device data)*
