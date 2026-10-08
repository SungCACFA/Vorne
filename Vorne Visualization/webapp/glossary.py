"""Every measure's definition, once. Every page pulls from here.

Templates mark a measure with term('available') (or term('available',
'Availability') for other wording). That renders a link to the definition,
with a dotted underline; with scripts on it opens a small panel, without them
it jumps to the page's Definitions list in the footer, which is built from
the terms the page used (and the entries those refer to).

The wording is GLOSSARY.md's, word for word, including where each definition
comes from (source); tests/test_explain.py parses that file and fails if any
entry here differs from it. Text keeps its markup: `code` renders as code,
**words** bold, *words* as emphasis - or as a link, where they name another
entry (*Small stops*) - and paragraphs stay paragraphs. Change the wording in
GLOSSARY.md first, then here.
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


DEFS: dict[str, Term] = {
    # ----------------------------------------------- time measures
    'available': Term(
        'Available',
        means='Of the time the line was scheduled for production, how much of it was actually running.',
        formula='`run time ÷ production time`, where production time = run + unplanned stop + planned stop',
        answers='Did it stay up?',
        source="Vorne Vocab — matches XL's own Availability exactly.",
        caution="Planned stops are in the denominator, so changeover and maintenance pull this down. That is Vorne's "
            "definition and our figure ties out against XL's. Note it differs from the textbook OEE convention, "
            'which excludes planned downtime from the denominator and therefore reads higher. Uses complete '
            'one-hour records only, because partial records distort a rate.',
        see=('planned', 'availability_loss', 'oee'),
        section='Time measures'),
    'performance': Term(
        'Performance',
        means='While running, how close the line came to its configured rate.',
        formula='`ideal time ÷ run time`',
        answers='Did it run fast?',
        source="Vorne Vocab — matches XL's own Performance exactly.",
        caution='Measured against the ideal cycle time configured on the device. Vorne intends that to be set per '
            'part; on these four lines it is mostly one nameplate speed copied across every part, so treat it as '
            'a relative measure between products on the same line rather than distance from an achievable target.'
            ' See *Ideal cycle time*.',
        see=('derived_ict', 'performance_loss', 'oee'),
        section='Time measures'),
    'calendar_use': Term(
        'Calendar use',
        means='Of all clock time, how much was scheduled for production at all.',
        formula='`production time ÷ all time`',
        answers='Are we using the asset?',
        source="Vorne Vocab — this is XL's **Utilization** metric, renamed here.",
        caution='Renamed from "Utilised" because the previous in-house report used that label for a different '
            'calculation — it counted complete hours only, which excluded the long unscheduled overnight blocks, '
            'the exact denominator this measure exists to capture. It was reporting schedule adherence, not asset'
            " use. Our figure matches Vorne's Utilization; the old one did not.",
        see=('scheduled', 'elapsed', 'teep'),
        section='Time measures'),
    'scheduled': Term(
        'Scheduled (production time)',
        means='Time the line was meant to be producing, whether running or stopped.',
        formula='`run + unplanned stop + planned stop`',
        answers='How much time did we intend to produce in?',
        source='Vorne Vocab calls this **Production Time**.',
        caution='Excludes unscheduled time. Most disagreements between two reports come down to one using production '
            'time and the other all time. Vorne also defines **Manufacturing Time** = run + down, which excludes '
            'planned stops — a third denominator to watch for.',
        see=('elapsed',),
        section='Time measures'),
    'elapsed': Term(
        'Elapsed (all time)',
        means='All clock time in the period, scheduled or not.',
        formula='`production time + not scheduled time`',
        answers='How much wall-clock time are we talking about?',
        source='Vorne Vocab calls this **All Time**.',
        caution='On a part run, elapsed minus run minus stopped leaves unscheduled time, usually the largest share. '
            'The run tables do not show it, because a part run is not a scheduling window.',
        see=('scheduled', 'not_scheduled'),
        section='Time measures'),
    'running': Term(
        'Running',
        means='The machine was cycling and producing.',
        answers='How much of the time was it actually making something?',
        source='Vorne Vocab — the Run impact state.',
        caution='Cycles slower than ideal still count as running time; the excess becomes a performance loss rather '
            'than downtime. Only once a cycle passes the down threshold does it become an availability loss. See '
            '*Small stops*.',
        see=('small_stops',),
        section='Time measures'),
    'down': Term(
        'Unplanned stop (down)',
        means='Down when it should have been running — breakdowns, jams, waiting on components.',
        answers='What went wrong?',
        source='Vorne Vocab — Down Time and Unplanned Stop Time are the same metric in XL.',
        caution='Reason assignment often happens after the fact, so recent stops may show unassigned and fill in '
            'later. The sync picks up those edits.',
        see=('mtbf', 'mttr'),
        section='Time measures'),
    'planned': Term(
        'Planned stop',
        means='Not producing, but intended — changeover, setup, scheduled maintenance, breaks.',
        answers='What did we choose not to produce through?',
        source='Vorne Vocab — the Planned Stop impact state.',
        caution='Some long events on these lines are labelled maintenance but look like a parked line, which inflates'
            ' planned stop and pulls availability down. Those are flagged as implausible durations. *(Danbro '
            'device data)*',
        see=('implausible',),
        section='Time measures'),
    'not_scheduled': Term(
        'Not scheduled',
        means='No shift was running. Nights, weekends, idle lines.',
        answers='When was nobody there?',
        source='Vorne Vocab — the Not Scheduled impact state.',
        caution="Usually the largest of the four buckets. Excluding it is what made the previous report's utilisation"
            ' figure look so much better than reality.',
        see=('calendar_use',),
        section='Time measures'),
    'mtbf': Term(
        'MTBF',
        means='How long the line runs on average before a fault stops it.',
        formula='`run time ÷ down events`',
        answers='How reliable is it between failures?',
        source='Vorne Vocab.',
        see=('down', 'running'),
        section='Time measures'),
    'mttr': Term(
        'MTTR',
        means='How long it takes on average to get running again after a fault.',
        formula='`down time ÷ down events`',
        answers='How quickly do we recover?',
        source='Vorne Vocab.',
        see=('down',),
        section='Time measures'),
    # --------------------------------------------------------- oee
    'oee': Term(
        'OEE',
        means='The share of scheduled production time that was fully productive — running, at full speed, making '
            'only good pieces.',
        formula='`availability × performance × quality`',
        answers='How much of our scheduled time actually turned into good product?',
        source='Vorne Vocab. 100% OEE means perfect production: always running, as fast as possible, no rejects.',
        caution='Currently withheld on these four lines, because Quality cannot be computed — see *Quality*. The '
            'fault inflates quality rather than depressing it, so an OEE figure taken from these machines reads '
            'better than reality.'
            '\n\nOEE has been reported from these devices in the past, and those figures were not reliable. This is '
            'recoverable: once reject scanning and the count inputs are configured, Quality becomes real and OEE '
            'returns, per line, from the date it is fixed.'
            '\n\nAvailability and Performance are published on their own in the meantime, because both derive from '
            'time, which reconciles to the second.'
            '\n\nNote that OEE multiplies its three factors, so it is unforgiving. A quality figure above 1.0 does not '
            'make OEE slightly wrong; it makes it meaningless. It also cannot be partially published — two of '
            'three factors is not an OEE.',
        see=('available', 'performance', 'quality', 'oee_loss'),
        section='OEE'),
    'availability_loss': Term(
        'Availability loss',
        means='The share of scheduled time lost because the line was not running at all.',
        formula='`100% − availability`',
        answers='How much did stoppages cost us?',
        source='Vorne Vocab. Accounts for both planned and unplanned stops.',
        caution='Includes changeover and maintenance, because Vorne puts planned stops inside production time. A line'
            ' with heavy changeover will show a large availability loss without anything being broken.',
        see=('available', 'oee_loss'),
        section='OEE'),
    'performance_loss': Term(
        'Performance loss',
        means='The share of scheduled time lost to running slower than the ideal cycle time.',
        formula='`(100% − performance) × availability`',
        answers='How much did slow running cost us?',
        source='Vorne Vocab.',
        caution='Not simply `1 − performance`. It is scaled by availability, because you can only lose speed during '
            'time you were actually running. This catches people out: a line with 50% performance and 50% '
            'availability has a 25% performance loss, not 50%. The three OEE losses are built this way so they '
            'add up with OEE to exactly 100% of scheduled time.',
        see=('performance', 'available', 'oee_loss'),
        section='OEE'),
    'quality_loss': Term(
        'Quality loss',
        means='The share of scheduled time lost to making pieces that did not meet standard.',
        formula='`(100% − quality) × availability × performance`',
        answers='How much did rejects cost us?',
        source='Vorne Vocab. Includes anything needing rework.',
        caution='Withheld here along with Quality. Scaled by both availability and performance for the same reason as'
            ' performance loss — you can only make a reject during time you were running and producing.',
        see=('quality', 'oee_loss'),
        section='OEE'),
    'oee_loss': Term(
        'OEE loss',
        means='The share of scheduled production time that was not productive.',
        formula='`100% − OEE`, equivalently availability loss + performance loss + quality loss',
        answers='How much of our scheduled time did we not get value from?',
        source='Vorne Vocab.',
        caution='The three component losses are deliberately constructed so that they plus OEE total 100% of '
            'scheduled time. That is what makes them comparable against each other, and it is why each is scaled '
            'by the factors ahead of it.',
        see=('availability_loss', 'performance_loss', 'quality_loss'),
        section='OEE'),
    'fully_productive_time': Term(
        'Fully productive time',
        means='The time it would have taken to make only the good pieces, at full speed, with no stoppages.',
        formula='`percent good × ideal time`',
        answers='What was the genuinely productive core of our time?',
        source='Vorne Vocab.',
        caution='Depends on piece counts, so withheld here. It is OEE expressed in hours rather than as a percentage,'
            ' which many people find easier to act on.',
        see=('oee',),
        section='OEE'),
    'hidden_factory_time': Term(
        'Hidden factory time',
        means='Untapped capacity — the additional production available without buying anything.',
        formula='`OEE lost time + schedule lost time`',
        answers='How much more could we make with what we already own?',
        source='Vorne Vocab.',
        caution='Represents around-the-clock perfect production, so it is a ceiling rather than a target. Useful for '
            'framing capacity arguments, not for setting goals. Depends on OEE, so withheld here; Calendar use '
            'shows the scheduling half of it on its own.',
        see=('oee', 'calendar_use'),
        section='OEE'),
    'six_big_losses': Term(
        'Six big losses',
        means="Vorne's breakdown of where production time goes: down events, planned stops, cycle losses, small "
            'stops, startup rejects and production rejects.',
        answers='Which category of loss is costing us most?',
        source='Vorne Vocab.',
        caution='The first four are published here, as they derive from time and cycles. The two reject categories '
            'depend on counts and are withheld. Note that cycle loss and small stop loss both count only the time'
            ' in **excess** of ideal cycle time, not the whole cycle.',
        see=('down', 'planned', 'small_stops', 'variance_h'),
        section='OEE'),
    'efficiency': Term(
        'Efficiency',
        means='How production compares against the takt-based target.',
        formula='`good count ÷ target count`',
        answers='Are we hitting the plan?',
        source='Vorne Vocab. Identical to its Rate Efficiency metric. 100% or above means the target was met.',
        caution='A different measure from Performance, and frequently confused with it. Performance compares against '
            'the ideal cycle time with no losses budgeted; Efficiency compares against takt time, which budgets '
            'for expected downtime, slow cycles and rejects. Withheld here because it depends on good count.',
        see=('performance', 'takt_time'),
        section='OEE'),
    # ----------------------------------- cycle and output measures
    'cycle': Term(
        'Cycle',
        means='One stroke of the machine.',
        answers='What is the unit the device counts?',
        source='Vorne Vocab.',
        caution='A cycle is not a piece. Vorne splits cycles three ways by duration: **run cycles** (up to the '
            'small-stop threshold, default 500% of ideal cycle time), **small stops** (between that and the down '
            'threshold, default 3 minutes), and **partial cycles** (one per run event, covering the boundaries). '
            'Total Cycles is all three added together. *(TN000406)*',
        see=('run_cycles', 'small_stops', 'total_cycles'),
        section='Cycle and output measures'),
    'piece': Term(
        'Piece',
        means='One sellable unit.',
        answers='How many did we actually make?',
        source='Vorne Vocab.',
        caution='Not reliably counted on these machines. See *Quality* and *Reject count* under measures deliberately'
            ' absent. Cycle counts are sound; piece counts are not.',
        see=('cycle', 'quality'),
        section='Cycle and output measures'),
    'derived_ict': Term(
        'Ideal cycle time (ICT)',
        means='The theoretical fastest time to complete one cycle for the part being run.',
        formula='`ideal time ÷ total cycles`',
        answers='What should one stroke take?',
        source="Vorne Vocab — configured per part on the device; our figure is Vorne's **Avg. Ideal Cycle Time**, "
            "recovered from the device's own ideal time.",
        caution='This is a configured value read back, not a measurement. Vorne intends it per part. On these four '
            'lines it is largely a single nameplate speed copied across every part — all four ADVIL variants '
            "return 0.54545455 s, which is 6/11 s: a rate of 6,600 cycles per hour, entered once as the line's "
            'nameplate speed. OTRIVIN differs between AreaB and AreaD because the standard tracks the machine '
            'rather than the product. By total cycles every run of a part returns the identical value — FR8 '
            'ESTRADIOL is exactly 0.375 s across every one of its runs, 88 when checked — which confirms it is a '
            'configured constant rather than a measurement. Every variance figure is only as good as this '
            'number. *(Danbro device data)*',
        see=('total_cycles', 'variance_h'),
        section='Cycle and output measures'),
    'actual_spc': Term(
        'Actual s/cycle',
        means='What one cycle really took, averaged over the period.',
        formula='`run hours × 3600 ÷ total cycles`',
        answers='How long per stroke, in practice?',
        source='Vorne Vocab — this is **Avg. Cycle Time**, total cycle time over total cycles.',
        caution='Running time only. Stopped time is excluded, so this measures speed rather than uptime.',
        see=('derived_ict', 'vs_derived'),
        section='Cycle and output measures'),
    'vs_derived': Term(
        'vs derived',
        means='How far actual cycle time sits from the configured standard.',
        formula='`actual s/cycle ÷ ideal cycle time`',
        answers='How far off standard?',
        source='Derived here; it is Performance inverted.',
        caution='1.00× is on standard, above is slower. A 2.00× ratio is 50% performance. Inherits the standards '
            'problem above, so compare products on the same line rather than reading the absolute number.',
        see=('performance', 'derived_ict'),
        section='Cycle and output measures'),
    'earned_h': Term(
        'Earned hours',
        means='The hours the output should have taken at standard.',
        formula='`total cycles × ideal cycle time ÷ 3600`',
        answers='What was the output worth, in hours?',
        source='Vorne Vocab — this is **Ideal Time** expressed in hours.',
        caution='Think of a quoted job: quoted at three hours, took eight — the mechanic earned three and burned '
            'eight. Measures speed only; stoppages are not in it. Note Vorne also has an **Earned Labor** metric,'
            ' which is a different thing entirely and unavailable here. See *Labour*.',
        see=('variance_h', 'labour'),
        section='Cycle and output measures'),
    'variance_h': Term(
        'Variance hours',
        means='Hours lost to running slower than standard.',
        formula='`actual run hours − earned hours`',
        answers='How much capacity did slow running cost?',
        source="Derived here. Vorne's nearest equivalents are **Cycle Lost Time** and **Small Stop Lost Time**, two "
            'of its Six Big Losses.',
        caution='Positive means slower than standard. Does not include stopped time at all — a part can show large '
            'variance while the line barely stopped, or none while it stood still for days. Variance is '
            'Performance; stopped hours are Availability. Vorne notes that a *negative* lost time means the ideal'
            ' cycle time is set too high, which is a useful check in the other direction.',
        see=('performance', 'available', 'stopped_h'),
        section='Cycle and output measures'),
    'run_h': Term(
        'Run h',
        means='Hours the machine was cycling.',
        answers='How long was it actually producing?',
        source='Vorne Vocab — **Run Time**.',
        caution='Excludes stopped and unscheduled time, so it will not reconcile against elapsed time on its own. On '
            'a part run, elapsed minus run minus stopped leaves unscheduled time, which is usually the largest '
            'share.',
        see=('running', 'elapsed'),
        section='Cycle and output measures'),
    'stopped_h': Term(
        'Stopped h',
        means='Hours scheduled but not running — unplanned and planned stops together.',
        answers='How long was it down while staffed?',
        source="Derived here from Vorne's Down Time and Planned Stop Time.",
        caution='Not the same as Vorne\'s **Down Time**, which is unplanned only. Where a column is headed "Down h" it'
            ' means unplanned stops alone.',
        see=('down', 'planned', 'elapsed'),
        section='Cycle and output measures'),
    'down_per_run': Term(
        'Unplanned h per run h',
        means='Unplanned stop hours for every hour the line spent running this product.',
        formula='`unplanned stop hours ÷ run hours`',
        answers='Does this product break down more than the others on the same machine?',
        source='Derived here.',
        caution='Counts unplanned stops only — setup and changeover are planned stops and are not in it. Only '
            'meaningful between products on the same line, where the machine, crew and stop reasons are shared; '
            'comparing across lines compares different equipment. Implausible stops are excluded.',
        see=('down', 'run_h', 'implausible'),
        section='Cycle and output measures'),
    'small_stops': Term(
        'Small stops',
        means='Cycles longer than the small-stop threshold but shorter than the down threshold.',
        answers='Is it hesitating rather than stopping?',
        source='Vorne Vocab and TN000406. Default thresholds are 500% of ideal cycle time, and 3 minutes, both '
            'configurable per part on the device.',
        caution='These never appear as downtime — the excess over ideal cycle time becomes a performance loss '
            'instead, which is why a line can look slow with no stop reason explaining it. Compare per thousand '
            'cycles rather than as a raw count.',
        see=('running', 'performance'),
        section='Cycle and output measures'),
    'run_cycles': Term(
        'Run cycles',
        means='Cycles completed within the small-stop threshold — the machine running normally.',
        answers='How much did it do at proper speed?',
        source='Vorne Vocab.',
        caution='Not the same as Total Cycles, which adds small stops and partial cycles. Vorne computes ideal time '
            'from **total** cycles, so any cycle-time figure must use the same denominator or it comes out '
            'inflated.',
        see=('total_cycles', 'cycle'),
        section='Cycle and output measures'),
    'total_cycles': Term(
        'Total cycles',
        means='Every cycle, however long it took.',
        formula='`run cycles + small stops + partial cycles`',
        answers='How many strokes in total?',
        source='Vorne Vocab.',
        caution="The correct denominator for any average cycle time, because Vorne's ideal time is ideal cycle time ×"
            ' total cycles.',
        see=('run_cycles', 'small_stops', 'derived_ict'),
        section='Cycle and output measures'),
    'equipment_cycles': Term(
        'Equipment cycles',
        means='Every cycle across all production states, not only while running.',
        answers='How much wear has the machine taken?',
        source='Vorne Vocab — intended for cycle-based maintenance programmes.',
        caution='All other cycle metrics count only during run events. This one does not, so it will be higher.',
        see=('cycle',),
        section='Cycle and output measures'),
    'takt_time': Term(
        'Takt time',
        means='The expected pace of production for the part, including budgeted losses.',
        answers='What pace are we meant to hold?',
        source='Vorne Vocab — set per part, drives the target counter.',
        caution='Not the same as ideal cycle time. Takt time budgets for downtime, slow cycles and rejects; ideal '
            "cycle time is the theoretical best with no losses. Takt drives Vorne's **Efficiency** metric (good "
            'count over target count), which is a different measure from Performance.',
        see=('derived_ict', 'efficiency'),
        section='Cycle and output measures'),
    # --------------------------------------- structure and context
    'production_day': Term(
        'Production day',
        means="The plant's own day, starting with its first shift.",
        answers='Which day does this belong to?',
        source='Vorne Vocab — "production days are aligned to shift boundaries so that shifts crossing midnight stay'
            ' within one day."',
        caution='Starts at 23:00 the previous evening where a third shift runs, otherwise 07:00. An unscheduled '
            'overnight block stays with the day before, so a production day can exceed 24 hours. Never equals a '
            "midnight-to-midnight calendar day. Vorne's production weeks always run Monday to Sunday. *(Danbro "
            'device data for the shift times)*',
        section='Structure and context'),
    'open_run': Term(
        'Open run',
        means='A part or job run the device has not closed yet.',
        answers='Is this still accumulating?',
        source='Derived here.',
        caution='Excluded from every total, because partial hours against partial cycles give meaningless ratios. A '
            "run open far longer than that part's normal duration usually means someone forgot to close it on the"
            ' device, and everything produced since is accumulating into one bucket. *(Danbro device data)*',
        section='Structure and context'),
    'changeover_hour': Term(
        'Changeover hour',
        means='An hour in which more than one part ran.',
        answers='Why are two products on one row?',
        source="Derived here from the device's own reporting.",
        caution="The device reports every part that ran in the hour, joined by a slash. The order is the device's and"
            ' is not guaranteed chronological.',
        section='Structure and context'),
    'production_phase': Term(
        'Production phase',
        means='Which stage of a part run an event falls in — changeover, startup, or steady state.',
        answers='Was this a settled run or still ramping?',
        source='Vorne Vocab.',
        caution='Vorne notes these phases have different loss characteristics, which is why startup rejects are '
            'tracked separately from production rejects.',
        section='Structure and context'),
    'implausible': Term(
        'Implausible duration',
        means='A stop far longer than anything normal for its category.',
        answers='Is this a real stop or a mislabelled parked line?',
        source='Derived here.',
        caution='A 56-hour "down" or a 260-hour "maintenance" distorts every average it touches. These are excluded '
            'from the charts but kept in the tables, which is why a table and a chart can rank the same reason '
            'differently. *(Danbro device data)*',
        section='Structure and context'),
    'config_epoch': Term(
        'Config epoch',
        means="A period during which a device's configuration was unchanged.",
        answers='Are these numbers comparable with those?',
        source='Derived here.',
        caution='Each device recorded the wrong timezone when first installed, which shifts shift and production-day '
            'boundaries. Those periods are kept but excluded by default rather than silently dropped. *(Danbro '
            'device data)*',
        see=('production_day',),
        section='Structure and context'),
    # -------------------------------- measures deliberately absent
    'quality': Term(
        'Quality',
        source='Vorne Vocab — `good count ÷ total count`, where total count = good count + reject count.',
        status='Withheld.',
        why='The reject sensor is not wired on any of the four lines, and no reject reason has ever been scanned '
            "in the entire history. In its place `reject count` is returning the same value as Vorne's **WIP "
            'Count** (in count − good count), which we confirmed holds on every row. That makes total count equal'
            ' to in count, so Quality is computing good ÷ infeed rather than good ÷ produced, and exceeds 100% '
            'whenever the good-count sensor leads the infeed sensor. Negative rejects appear on more than '
            '900 hourly records on FR8 and roughly 400 on each of the other three lines.'
            "\n\nXL's arithmetic is correct. Its count inputs are miswired. *(Danbro device data)*",
        see=('piece', 'oee'),
        section='Measures deliberately absent'),
    'oee_withheld': Term(
        'OEE',
        status='Withheld. See *OEE* in the main glossary for the definition and why it is currently withheld.',
        see=('oee',),
        section='Measures deliberately absent'),
    'teep': Term(
        'TEEP',
        status='Withheld. TEEP is `OEE × Utilization` (Vorne Vocab); see *OEE* in the main glossary for why it is '
            'currently withheld.',
        see=('oee', 'calendar_use'),
        section='Measures deliberately absent'),
    'labour': Term(
        'Labour',
        source='Vorne Vocab — Total Labor accumulates each second of production time multiplied by **Current Team '
            'Size**, entered by an operator. Labor Efficiency is earned labor ÷ total labor.',
        status='Not available from the devices.',
        why='Across more than 1,800 shifts on all four lines, crew size has never been entered once, so total '
            'labor is zero'
            ' everywhere and labor efficiency cannot be computed. Labour standards do exist on many parts — '
            'earned labor is populated on around 840 shifts — so the expectation is configured but no actuals are being '
            'captured. Labour hours must come from SAP. *(Danbro device data)*',
        section='Measures deliberately absent'),
}

ABSENT = ("quality", "oee", "teep", "labour")   # footer: the full OEE entry, not the pointer
NOT_IN_GLOSSARY_MD: tuple[str, ...] = ()


def _plain(label: str) -> str:
    return label.split(" (")[0].lower()


# an entry named in italics in another entry's text (*Small stops*, *Quality*) links to it; the full OEE entry,
# not the absent section's pointer, is the one "OEE" means
LABEL_KEY = {_plain(t.label): k for k, t in reversed(list(DEFS.items()))}


def key_for(words: str) -> str | None:
    return LABEL_KEY.get(_plain(words))


def _named(t: Term) -> tuple[str, ...]:
    import re

    text = " ".join(x for x in (t.means, t.formula, t.answers, t.source, t.caution, t.status, t.why) if x)
    return tuple(k for w in re.findall(r"(?<!\*)\*([^*]+)\*(?!\*)", text) if (k := key_for(w)))


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
            todo += [s for s in (*DEFS[k].see, *_named(DEFS[k])) if s not in out]
    return out
