"""Every measure's definition, once. Every page pulls from here.

Templates mark a measure with term('available') (or term('available',
'Availability') for other wording). That renders a link to the definition,
with a dotted underline; with scripts on it opens a small panel, without them
it jumps to the page's Definitions list in the footer, which is built from
the terms the page used (and the entries those refer to).

The wording is GLOSSARY.md's, word for word; tests/test_explain.py parses
that file and fails if any entry here differs from it. Formulas and cautions
keep its markup: `code` renders as code, *words* as emphasis. Change the
wording in GLOSSARY.md first, then here.

"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Term:
    label: str                       # the entry's heading in GLOSSARY.md
    means: str | None = None         # definition
    formula: str | None = None
    answers: str | None = None
    caution: str | None = None
    status: str | None = None        # measures deliberately absent: status and why, instead of the four parts
    why: str | None = None
    see: tuple[str, ...] = ()        # other entries this one refers to
    section: str = ""
    source: str | None = None        # where the definition comes from (Vorne Vocab, TN000406, the API, device data)


T, C, S, A = "Time measures", "Cycle and output measures", "Structure and context", "Measures deliberately absent"

DEFS: dict[str, Term] = {
    # ------------------------------------------------------------ time measures
    "available": Term(
        "Available",
        "Of the time the line was scheduled to run, how much of it was actually running.",
        "`running ÷ (running + unplanned stop + planned stop)`",
        "Did it stay up?",
        "This convention includes planned stops in the denominator, so changeover and maintenance pull it down. "
        "The common OEE convention leaves them out, so our figure reads lower than a textbook one. Uses complete "
        "one-hour records only, because partial records distort a rate.",
        see=("planned",), section=T),
    "performance": Term(
        "Performance",
        "While it was running, how close the line came to its configured rate.",
        "`ideal time ÷ running time`",
        "Did it run fast?",
        "Measured against the cycle time configured on the device, which on these machines is mostly one "
        "nameplate speed per line rather than a per-product standard. Treat it as a relative measure between "
        "products on the same line, not as distance from an achievable target.",
        see=("derived_ict",), section=T),
    "calendar_use": Term(
        "Calendar use",
        "Of all clock time in the period, how much was scheduled for production at all.",
        "`scheduled ÷ elapsed`",
        "Are we using the asset?",
        "Reads far lower than the previous report's \"Utilised\" column. The old calculation used complete hours "
        "only, which excluded the long unscheduled overnight blocks — the exact denominator this measure exists "
        "to capture. It was reporting schedule adherence, not asset use. Different measure, not a changed number.",
        see=("scheduled", "elapsed"), section=T),
    "scheduled": Term(
        "Scheduled",
        "Time the line was meant to be producing, whether it was running or stopped.",
        "`running + unplanned stop + planned stop`",
        "How much time did we intend to produce in?",
        "Excludes unscheduled time. Most disagreements between two reports come down to one using scheduled and "
        "the other elapsed.",
        see=("elapsed",), section=T),
    "elapsed": Term(
        "Elapsed",
        "All clock time in the period, scheduled or not.",
        "`scheduled + not scheduled`",
        "How much wall-clock time are we talking about?",
        "On a part run, elapsed minus running minus stopped is unscheduled time. That remainder is usually the "
        "largest part, and it is not shown on the run tables because a part run is not a scheduling window.",
        see=("scheduled", "not_scheduled"), section=T),
    "running": Term(
        "Running",
        "The machine was cycling and producing.",
        None,
        "How much of the time was it actually making something?",
        "Short interruptions too brief to log as downtime stay inside running time and show up as slow cycles "
        "instead. See *small stops*.",
        see=("small_stops",), section=T),
    "down": Term(
        "Unplanned stop",
        "Down when it should have been running — breakdowns, jams, waiting on components.",
        None,
        "What went wrong?",
        "Reason assignment often happens after the fact, so recent stops may show as unassigned and fill in "
        "later. The sync picks up those edits.",
        section=T),
    "planned": Term(
        "Planned stop",
        "Not producing, but intended — changeover, setup, scheduled maintenance, breaks.",
        None,
        "What did we choose not to produce through?",
        "Some long events on these lines are labelled maintenance but look like a parked line, which inflates "
        "planned stop. Those are flagged as implausible durations.",
        see=("implausible",), section=T),
    "not_scheduled": Term(
        "Not scheduled",
        "No shift was running. Nights, weekends, idle lines.",
        None,
        "When was nobody there?",
        "Usually the largest of the four buckets. Excluding it is what made the old utilisation figure look so "
        "much better than reality.",
        see=("calendar_use",), section=T),
    # --------------------------------------------------- cycle and output measures
    "cycle": Term(
        "Cycle",
        "One stroke of the machine.",
        None,
        "What is the unit the device counts?",
        "A cycle is not a piece. One cycle may produce several sellable units. Cycle counts are reliable on these "
        "machines; piece counts are not.",
        see=("piece",), section=C),
    "piece": Term(
        "Piece",
        "One sellable unit.",
        None,
        "How many did we actually make?",
        "Not reliably counted on these machines. Reject count is inferred rather than measured, and no reject "
        "reason has ever been scanned, so all piece-based figures are withheld.",
        see=("cycle", "quality"), section=C),
    "derived_ict": Term(
        "Ideal cycle time (ICT)",
        "The seconds per cycle the machine is configured to achieve at full rate.",
        "`median of (ideal time ÷ run cycles)` across runs over 200 cycles",
        "What should one stroke take?",
        "This is a configured value read back from the device, not a measurement. On these lines it is largely a "
        "single nameplate speed copied to every part — ADVIL in three different carton sizes shares one cycle time "
        "to three decimal places. Every variance figure is only as good as this number.",
        see=("variance_h",), section=C),
    "actual_spc": Term(
        "Actual s/cycle",
        "What one cycle really took, averaged over the period.",
        "`run hours × 3600 ÷ run cycles`",
        "How long per stroke, in practice?",
        "Running time only. Stopped time is excluded, so this measures speed rather than uptime.",
        see=("derived_ict", "vs_derived"), section=C),
    "vs_derived": Term(
        "vs derived",
        "How far actual cycle time sits from the configured standard.",
        "`actual s/cycle ÷ ideal cycle time`",
        "How far off standard?",
        "1.00× is on standard, above is slower. This is Performance inverted — a 2.00× ratio is 50% performance. "
        "Inherits the standards problem above, so compare products on the same line rather than reading the "
        "absolute number.",
        see=("performance", "derived_ict"), section=C),
    "earned_h": Term(
        "Earned hours",
        "The hours the output should have taken at standard.",
        "`run cycles × ideal cycle time ÷ 3600`",
        "What was the output worth, in hours?",
        "Think of a quoted job: quoted at three hours, took eight — the mechanic earned three and burned eight. "
        "Measures speed only; stoppages are not in it.",
        see=("variance_h",), section=C),
    "variance_h": Term(
        "Variance hours",
        "Hours lost to running slower than standard.",
        "`actual run hours − earned hours`",
        "How much capacity did slow running cost?",
        "Positive means slower than standard. Does not include stopped time at all — a part can show large "
        "variance while the line barely stopped, or none while it stood still for days. Variance is Performance, "
        "stopped hours are Availability.",
        see=("performance", "available", "stopped_h"), section=C),
    "run_h": Term(
        "Run h",
        "Hours the machine was cycling.",
        None,
        "How long was it actually producing?",
        "Excludes stopped and unscheduled time, so it will not reconcile against elapsed time on its own. On a part "
        "run, elapsed minus run minus stopped leaves unscheduled time, which is usually the largest share.",
        see=("running", "elapsed"), section=C),
    "stopped_h": Term(
        "Stopped h",
        "Hours scheduled but not running — unplanned and planned stops together.",
        None,
        "How long was it down while staffed?",
        "On a part run, elapsed minus run minus stopped leaves unscheduled time, which is usually the largest "
        "share.",
        see=("down", "planned", "elapsed"), section=C),
    "down_per_run": Term(
        "Unplanned h per run h",
        "Unplanned stop hours for every hour the line spent running, on this product.",
        "`unplanned stop hours ÷ run hours`",
        "Does this product stop more than the others on the same machine?",
        "Only meaningful between products on the same line, where the machine, crew and stop reasons are shared. "
        "Comparing across lines compares different equipment. Counts unplanned stops only: setup and changeover "
        "are planned stops and are not in it.",
        see=("down", "run_h"), section=C),
    "small_stops": Term(
        "Small stops",
        "Interruptions too brief to be logged as downtime.",
        None,
        "Is it hesitating rather than stopping?",
        "These never appear as downtime. They surface as slow cycle time instead, which is why a line can look "
        "slow without any stop reason explaining it. Compare per thousand cycles rather than as a raw count.",
        see=("running", "performance"), section=C),
    "run_cycles": Term(
        "Run cycles",
        "Machine strokes completed while running.",
        None,
        "How much did it actually do?",
        "The most reliable output number these devices produce. Prefer it over piece counts for any comparison.",
        see=("cycle", "piece"), section=C),
    # ----------------------------------------------------- structure and context
    "production_day": Term(
        "Production day",
        "The plant's own day, starting with its first shift.",
        None,
        "Which day does this belong to?",
        "Starts at 23:00 the previous evening where a third shift runs, otherwise 07:00. An unscheduled overnight "
        "block stays with the day before, so a production day can exceed 24 hours. Never equals a "
        "midnight-to-midnight calendar day.",
        section=S),
    "open_run": Term(
        "Open run",
        "A part or job run the device has not closed yet.",
        None,
        "Is this still accumulating?",
        "Excluded from every total, because partial hours against partial cycles give meaningless ratios. A run "
        "open far longer than that part's normal duration usually means someone forgot to close it on the device, "
        "and everything produced since is accumulating into one bucket.",
        section=S),
    "changeover_hour": Term(
        "Changeover hour",
        "An hour in which more than one part ran.",
        None,
        "Why are two products on one row?",
        "The device reports every part that ran in the hour, joined by a slash. The order is the device's and is "
        "not guaranteed chronological.",
        section=S),
    "implausible": Term(
        "Implausible duration",
        "A stop far longer than anything normal for its category.",
        None,
        "Is this a real stop or a mislabelled parked line?",
        "A 56-hour \"down\" or a 260-hour \"maintenance\" distorts every average it touches. These are excluded from "
        "the charts but kept in the tables, which is why a table and a chart can rank the same reason differently.",
        section=S),
    "config_epoch": Term(
        "Config epoch",
        "A period during which a device's configuration was unchanged.",
        None,
        "Are these numbers comparable with those?",
        "Each device recorded the wrong timezone when first installed, which shifts shift and production-day "
        "boundaries. Those periods are kept but excluded by default rather than silently dropped.",
        see=("production_day",), section=S),
    # ----------------------------------------------- measures deliberately absent
    "quality": Term(
        "Quality",
        status="Withheld.",
        why="Reject count is derived as infeed minus good count rather than measured, and the reject-reason log is "
            "empty across the entire history on all four machines. Where the good-count sensor reads higher than "
            "infeed, rejects go negative. Quality exceeds 100% in places, which is not possible.",
        see=("piece", "oee"), section=A),
    "oee": Term(
        "OEE",
        status="Withheld.",
        why="OEE contains Quality, so it inherits the fault above. Availability and Performance are published "
            "separately because they derive from time, which reconciles.",
        see=("quality", "available", "performance"), section=A),
    "labour": Term(
        "Labour",
        status="Not available from the devices.",
        why="Labour is calculated from a crew size entered by an operator at shift start. Across 1,803 shifts on "
            "all four lines, crew size has never been entered once. Labour hours must come from SAP.",
        section=A),
}

ABSENT = ("quality", "oee", "labour")
NOT_IN_GLOSSARY_MD: tuple[str, ...] = ()


def definition(key: str) -> Term:
    return DEFS[key]


def with_references(keys: list[str]) -> list[str]:
    """The keys, then every entry they refer to (and those refer to), each once, in first-seen order: a
    cross-reference must find its target in the page's Definitions list."""
    out, todo = [], list(keys)
    while todo:
        k = todo.pop(0)
        if k not in out:
            out.append(k)
            todo += [s for s in DEFS[k].see if s not in out]
    return out
