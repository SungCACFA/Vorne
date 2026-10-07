# Glossary source text

Authoritative wording for the clickable definitions in `webapp/glossary.py`.
Replace the placeholder text with this.

Each entry has four parts:

- **definition** — one sentence, plain words
- **formula** — shown in monospace; omit where there isn't one
- **answers** — the question a person is actually asking
- **caution** — the thing that catches people out; omit only where there
  genuinely isn't one

Keep the cautions. They are the reason the definitions are worth having, and
several of them record findings that cost real effort to establish.

---

## Time measures

### Available

**definition:** Of the time the line was scheduled to run, how much of it was
actually running.

**formula:** `running ÷ (running + unplanned stop + planned stop)`

**answers:** Did it stay up?

**caution:** This convention includes planned stops in the denominator, so
changeover and maintenance pull it down. The common OEE convention leaves them
out, so our figure reads lower than a textbook one. Uses complete one-hour
records only, because partial records distort a rate.

### Performance

**definition:** While it was running, how close the line came to its
configured rate.

**formula:** `ideal time ÷ running time`

**answers:** Did it run fast?

**caution:** Measured against the cycle time configured on the device, which
on these machines is mostly one nameplate speed per line rather than a
per-product standard. Treat it as a relative measure between products on the
same line, not as distance from an achievable target.

### Calendar use

**definition:** Of all clock time in the period, how much was scheduled for
production at all.

**formula:** `scheduled ÷ elapsed`

**answers:** Are we using the asset?

**caution:** Reads far lower than the previous report's "Utilised" column. The
old calculation used complete hours only, which excluded the long unscheduled
overnight blocks — the exact denominator this measure exists to capture. It
was reporting schedule adherence, not asset use. Different measure, not a
changed number.

### Scheduled

**definition:** Time the line was meant to be producing, whether it was
running or stopped.

**formula:** `running + unplanned stop + planned stop`

**answers:** How much time did we intend to produce in?

**caution:** Excludes unscheduled time. Most disagreements between two reports
come down to one using scheduled and the other elapsed.

### Elapsed

**definition:** All clock time in the period, scheduled or not.

**formula:** `scheduled + not scheduled`

**answers:** How much wall-clock time are we talking about?

**caution:** On a part run, elapsed minus running minus stopped is unscheduled
time. That remainder is usually the largest part, and it is not shown on the
run tables because a part run is not a scheduling window.

### Running

**definition:** The machine was cycling and producing.

**answers:** How much of the time was it actually making something?

**caution:** Short interruptions too brief to log as downtime stay inside
running time and show up as slow cycles instead. See *small stops*.

### Unplanned stop

**definition:** Down when it should have been running — breakdowns, jams,
waiting on components.

**answers:** What went wrong?

**caution:** Reason assignment often happens after the fact, so recent stops
may show as unassigned and fill in later. The sync picks up those edits.

### Planned stop

**definition:** Not producing, but intended — changeover, setup, scheduled
maintenance, breaks.

**answers:** What did we choose not to produce through?

**caution:** Some long events on these lines are labelled maintenance but look
like a parked line, which inflates planned stop. Those are flagged as
implausible durations.

### Not scheduled

**definition:** No shift was running. Nights, weekends, idle lines.

**answers:** When was nobody there?

**caution:** Usually the largest of the four buckets. Excluding it is what
made the old utilisation figure look so much better than reality.

---

## Cycle and output measures

### Cycle

**definition:** One stroke of the machine.

**answers:** What is the unit the device counts?

**caution:** A cycle is not a piece. One cycle may produce several sellable
units. Cycle counts are reliable on these machines; piece counts are not.

### Piece

**definition:** One sellable unit.

**answers:** How many did we actually make?

**caution:** Not reliably counted on these machines. Reject count is inferred
rather than measured, and no reject reason has ever been scanned, so all
piece-based figures are withheld.

### Ideal cycle time (ICT)

**definition:** The seconds per cycle the machine is configured to achieve at
full rate.

**formula:** `median of (ideal time ÷ run cycles)` across runs over 200 cycles

**answers:** What should one stroke take?

**caution:** This is a configured value read back from the device, not a
measurement. On these lines it is largely a single nameplate speed copied to
every part — ADVIL in three different carton sizes shares one cycle time to
three decimal places. Every variance figure is only as good as this number.

### Actual s/cycle

**definition:** What one cycle really took, averaged over the period.

**formula:** `run hours × 3600 ÷ run cycles`

**answers:** How long per stroke, in practice?

**caution:** Running time only. Stopped time is excluded, so this measures
speed rather than uptime.

### vs derived

**definition:** How far actual cycle time sits from the configured standard.

**formula:** `actual s/cycle ÷ ideal cycle time`

**answers:** How far off standard?

**caution:** 1.00× is on standard, above is slower. This is Performance
inverted — a 2.00× ratio is 50% performance. Inherits the standards problem
above, so compare products on the same line rather than reading the absolute
number.

### Earned hours

**definition:** The hours the output should have taken at standard.

**formula:** `run cycles × ideal cycle time ÷ 3600`

**answers:** What was the output worth, in hours?

**caution:** Think of a quoted job: quoted at three hours, took eight — the
mechanic earned three and burned eight. Measures speed only; stoppages are not
in it.

### Variance hours

**definition:** Hours lost to running slower than standard.

**formula:** `actual run hours − earned hours`

**answers:** How much capacity did slow running cost?

**caution:** Positive means slower than standard. Does not include stopped
time at all — a part can show large variance while the line barely stopped, or
none while it stood still for days. Variance is Performance, stopped hours are
Availability.

### Run h

**definition:** Hours the machine was cycling.

**answers:** How long was it actually producing?

**caution:** Same measure as "Actual h" on the standards table. Excludes
stopped and unscheduled time, so it will not reconcile against elapsed time
on its own.

### Stopped h

**definition:** Hours scheduled but not running — unplanned and planned stops
together.

**answers:** How long was it down while staffed?

**caution:** On a part run, elapsed minus run minus stopped leaves unscheduled
time, which is usually the largest share.

### Small stops

**definition:** Interruptions too brief to be logged as downtime.

**answers:** Is it hesitating rather than stopping?

**caution:** These never appear as downtime. They surface as slow cycle time
instead, which is why a line can look slow without any stop reason explaining
it. Compare per thousand cycles rather than as a raw count.

### Run cycles

**definition:** Machine strokes completed while running.

**answers:** How much did it actually do?

**caution:** The most reliable output number these devices produce. Prefer it
over piece counts for any comparison.

---

## Structure and context

### Production day

**definition:** The plant's own day, starting with its first shift.

**answers:** Which day does this belong to?

**caution:** Starts at 23:00 the previous evening where a third shift runs,
otherwise 07:00. An unscheduled overnight block stays with the day before, so
a production day can exceed 24 hours. Never equals a midnight-to-midnight
calendar day.

### Open run

**definition:** A part or job run the device has not closed yet.

**answers:** Is this still accumulating?

**caution:** Excluded from every total, because partial hours against partial
cycles give meaningless ratios. A run open far longer than that part's normal
duration usually means someone forgot to close it on the device, and
everything produced since is accumulating into one bucket.

### Changeover hour

**definition:** An hour in which more than one part ran.

**answers:** Why are two products on one row?

**caution:** The device reports every part that ran in the hour, joined by a
slash. The order is the device's and is not guaranteed chronological.

### Implausible duration

**definition:** A stop far longer than anything normal for its category.

**answers:** Is this a real stop or a mislabelled parked line?

**caution:** A 56-hour "down" or a 260-hour "maintenance" distorts every
average it touches. These are excluded from the charts but kept in the tables,
which is why a table and a chart can rank the same reason differently.

### Config epoch

**definition:** A period during which a device's configuration was unchanged.

**answers:** Are these numbers comparable with those?

**caution:** Each device recorded the wrong timezone when first installed,
which shifts shift and production-day boundaries. Those periods are kept but
excluded by default rather than silently dropped.

---

## Measures deliberately absent

### Quality

**status:** Withheld.

**why:** Reject count is derived as infeed minus good count rather than
measured, and the reject-reason log is empty across the entire history on all
four machines. Where the good-count sensor reads higher than infeed, rejects
go negative. Quality exceeds 100% in places, which is not possible.

### OEE

**status:** Withheld.

**why:** OEE contains Quality, so it inherits the fault above. Availability
and Performance are published separately because they derive from time, which
reconciles.

### Labour

**status:** Not available from the devices.

**why:** Labour is calculated from a crew size entered by an operator at shift
start. Across 1,803 shifts on all four lines, crew size has never been
entered once. Labour hours must come from SAP.
