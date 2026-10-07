"""Every measure's definition, once. Every page pulls from here.

Templates mark a measure with term('available') (or term('available',
'Availability') for other wording). That renders a link to the definition,
with a dotted underline; with scripts on it opens a small panel, without them
it jumps to the page's Definitions list in the footer, which is built from
the terms the page used. The wording lives only in this file, so it cannot
drift between pages.

Wording: the brief asked for the wording of the measures guide (PDF), which
was not found on this machine. Until it is supplied, each entry below uses
the wording already on the site (column tooltips, footers, the products and
live notes, the README), and SOURCE says so. Replacing it is a change to this
file alone.
"""

from __future__ import annotations

from dataclasses import dataclass

SOURCE = "site wording; to be replaced with the measures guide (PDF)"


@dataclass(frozen=True)
class Term:
    label: str      # the measure's name as shown
    means: str      # one-line definition
    formula: str
    answers: str    # what it answers, in plain words
    caution: str


DEFS: dict[str, Term] = {
    "available": Term(
        "Available",
        "The share of scheduled time the line was running.",
        "run ÷ (run + unplanned stop + planned stop), full one-hour records only",
        "When the line was meant to be making product, how much of that time was it?",
        "Planned stops count against it, unlike the common OEE convention (run ÷ (run + unplanned)), so it reads "
        "lower than a textbook figure for the same hours; a parked line left in a stop state lowers it further."),
    "performance": Term(
        "Performance",
        "Ideal time as a share of run time.",
        "ideal ÷ run, full one-hour records only",
        "While the line was running, how close to its configured ideal cycle time did it cycle?",
        "Above 100% means the ideal cycle time configured for the part is wrong, not that the machine ran fast."),
    "calendar_use": Term(
        "Calendar use",
        "The share of all elapsed time that was scheduled.",
        "scheduled ÷ elapsed, all closed records",
        "How much of the calendar was the line planned to be used?",
        "Not the earlier report's \"Utilised\", which used full hours only and so dropped the overnight "
        "not-scheduled blocks; and time mislabelled as a stop inflates it."),
    "scheduled": Term(
        "Scheduled",
        "Time the line was meant to be producing.",
        "run + unplanned stop + planned stop",
        "How long was the line supposed to be making product?",
        "Includes planned stops (changeovers, maintenance); nights, weekends and other not-scheduled time are not in it."),
    "elapsed": Term(
        "Elapsed",
        "Clock time covered: for a period, every closed record; for a run, from its start to the next run's.",
        "scheduled + not scheduled; for a run, end − start",
        "How much calendar time does this cover?",
        "A run's elapsed time includes the nights and weekends inside it: elapsed − run h − stopped h is time the line "
        "was not scheduled."),
    "down": Term(
        "Down",
        "Unplanned stop time.",
        "sum of unplanned-stop time",
        "How long was the line stopped when it was meant to be running?",
        "Stops shorter than the down threshold are small stops and count as run time, not down time."),
    "planned": Term(
        "Planned stop",
        "Time stopped on purpose while scheduled: changeover, maintenance, setup.",
        "sum of planned-stop time",
        "How long was the line stopped by plan?",
        "Planned, but still time the line was not producing, and it counts against availability here."),
    "down_per_run": Term(
        "Down h per run h",
        "Unplanned stop hours for every hour the line was cycling.",
        "down h ÷ run h",
        "How much does this product stop for each hour it runs? Fair across products, since it does not depend on "
        "how long a run was.",
        "A short run where one stop dominates can read very high: weigh it by run hours."),
    "run_h": Term(
        "Run h",
        "Cycling time only: time the line was cycling, including slow cycles and small stops.",
        "the device's run time (no down time, no planned stops)",
        "How long was the line actually cycling?",
        "Small stops are inside it, so a line with many short jams can show high run hours and low performance."),
    "stopped_h": Term(
        "Stopped h",
        "Unplanned and planned stop time inside a run.",
        "unplanned stop + planned stop, for the run",
        "How long did the line stand during this run, for any reason?",
        "Not-scheduled time inside the run is in neither run h nor stopped h, so they do not add up to elapsed."),
    "small_stops": Term(
        "Small stops",
        "Stops shorter than the down threshold.",
        "a stop shorter than the device's down threshold",
        "How often did the line pause briefly without going down?",
        "They are counted inside run time, so they lower performance, not availability."),
    "earned_h": Term(
        "Earned h",
        "The device's ideal time: how long the output should have taken.",
        "run cycles × the ideal cycle time configured on the XL",
        "If every cycle had run at the configured ideal speed, how long would this have taken?",
        "Only as right as the configured ideal cycle time: check Derived ICT for a reconfiguration."),
    "variance_h": Term(
        "Variance h",
        "Run hours beyond what the output earned.",
        "actual run h − earned h",
        "How many hours were lost to running slower than the configured ideal?",
        "Measured against the device's ideal cycle time, not SAP; positive means slower than ideal."),
    "derived_ict": Term(
        "Derived ICT",
        "The ideal cycle time the device has actually been using for this part on this line, per machine cycle.",
        "median of ideal time ÷ run cycles over the line's runs of more than 200 cycles, all usable history",
        "What ideal cycle time is the XL measuring this part against?",
        "Where a line was reconfigured the median mixes both levels; the product page shows the current one."),
    "actual_spc": Term(
        "Actual s/cycle",
        "How long each machine cycle actually took.",
        "run time ÷ run cycles",
        "How fast did the line really cycle on this part?",
        "Per machine cycle, not per piece: comparing with SAP's per-piece standard needs pieces per cycle."),
    "production_day": Term(
        "Production day",
        "The working day a record belongs to, which starts with its first shift.",
        "from 23:00 the evening before when a third shift runs, otherwise from 07:00",
        "Which day's production is this part of?",
        "Not a calendar date: an unscheduled night stays with the day before, so a production day's records can "
        "run past 24 hours."),
    "open_run": Term(
        "Open run",
        "A part or job run the device has not closed yet.",
        "flagged when open more than 7 days, or more than 3× the part's median closed run (runs over 200 cycles)",
        "Is a job still accumulating on this line?",
        "Its figures reach no product total until it closes; if the job has finished, close it out on the XL."),
}


def definition(key: str) -> Term:
    return DEFS[key]
