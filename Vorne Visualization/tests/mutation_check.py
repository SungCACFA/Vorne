#!/usr/bin/env python3
"""Mutation check: does the test suite notice when the code is wrong?

Each entry below is a deliberate, plausible bug - the kind a refactor or a
"helpful" fix could introduce. For each one: apply it, run the suite, put
the original back. A mutation the suite does not catch is a gap: the code
can be wrong and every test still passes.

    python tests/mutation_check.py              all mutations
    python tests/mutation_check.py 3 7          just these numbers
    python tests/mutation_check.py --list
    python tests/mutation_check.py --check      verify every pin, run nothing
    python tests/mutation_check.py --pins       print the pins that no longer match

Not collected by pytest (the file name does not start with test_). The
export test is skipped while mutating (it takes ~40 s and renders the same
templates the page tests cover); run the full suite separately.

Safety: the original bytes are restored and re-checked after every run, and
bytecode caching is disabled. (An earlier version restored by file move;
a mutation of identical length, written in the same second, left Python
running the mutated .pyc after the source was restored.)

Pins. A mutation that stops applying tests nothing, silently, and that
happened three times: Windows line endings, a stale .pyc, and a refactor that
moved #42's target line. So every entry is pinned (PINS, at the bottom) to a
hash of the whole source line(s) its original text sits on. Before anything
is mutated, every entry is checked: its text must occur exactly once, and its
line must hash to its pin. Any failure refuses the whole run (exit 2). The
same check runs inside the normal suite (test_mutation_pins.py), so a stale
mutation fails `pytest` without anyone running this. When you change a
pinned line on purpose: re-read the mutation, make sure it still describes a
real bug in the new code, then take the new pin from --pins.

One run at a time, and nothing edited while it runs. A run holds
.mutation_run.lock (created exclusively; a second run refuses to start) and
keeps the original bytes of the file it has mutated in .mutation_run.original.
It hashes every source file before the first mutation and checks them before
and after each one. An edit made during a run would otherwise be wiped by the
restore, or bake the mutation in, or change what later mutations test; so on
any change the run stops, saves the edited file beside the original as
<file>.edited-during-mutation, restores, and exits 3 with no verdict. If the
lock is there and no run is active, a run was killed: --check shows whether a
file was left mutated, and .mutation_run.original holds its original bytes.

When you add a feature, add the mutation that would break it. When a
mutation is missed, either write the test that catches it or record here
why it is acceptable.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (what the bug would do, file, original text, mutated text)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # --- the original seventeen (first run: 7 caught, 10 missed)
    ("unusable-epoch days included in aggregates", "webapp/data.py",
     "            if not r[14]:", "            if False:"),
    ("perf >100% short-run wording dropped (all say 'wrong ICT')", "webapp/templates/_macros.html",
     "run is not none and run < 300", "run is not none and run < 0"),
    ("SAP pieces/hour display conversion wrong", "webapp/data.py",
     '"pieces_per_hour": 3600 / v if v else None', '"pieces_per_hour": v'),
    ("negative-duration display clamp removed", "webapp/__init__.py",
     "h = max(0.0, (seconds or 0) / 3600)", "h = (seconds or 0) / 3600"),
    ("changeover arrays stored as Python repr", "xlpipe/rows.py",
     'v = ",".join(parts) if parts else None', "v = str(raw)"),
    ("open-run median multiple 3x -> 30x", "webapp/data.py",
     "OPEN_MEDIAN_MULTIPLE = 3", "OPEN_MEDIAN_MULTIPLE = 30"),
    ("sync_id upsert guard removed", "xlpipe/cache.py",
     'f"WHERE {t}.sync_id IS NULL OR excluded.sync_id > {t}.sync_id OR {t}.deleted_at IS NOT NULL"', 'f"WHERE 1"'),
    ("epoch re-stamping disabled", "xlpipe/cache.py",
     "        with self._lock:\n            for spec in SPECS.values():\n                col =",
     "        with self._lock:\n            for spec in []:\n                col ="),
    ("delete reconciliation disabled", "xlpipe/sync.py", "                elif gone:", "                elif False:"),
    ("part_standard ICT median -> mean", "xlpipe/cache.py",
     "(part, statistics.median(xs),", "(part, statistics.mean(xs),"),
    ("products vs-derived ratio inverted", "webapp/__init__.py",
     '"vs_ict": actual / d_ict["ict"] if', '"vs_ict": d_ict["ict"] / actual if'),
    ("store row rejection: no row-by-row fallback", "xlpipe/sync.py",
     "            except RowRejected:\n", "            except ZeroDivisionError:\n"),
    ("weeks start Sunday instead of Monday", "webapp/data.py",
     "(d - timedelta(days=d.weekday()))", "(d - timedelta(days=(d.weekday() + 1) % 7))"),
    ("calendar use computed the old way (full hours only)", "webapp/data.py",
     "        return self.sched / self.elapsed if self.elapsed else None",
     "        return self.utilization_full_hours_only"),
    ("per-piece figures shown for untrusted epochs", "webapp/__init__.py",
     '"actual_spp": a["t_run"] / a["t_good"] if a["t_runs"] and a["t_good"] > 0 else None',
     '"actual_spp": a["run"] / 1000.0'),
    ("NaN reaches the quarantine payload (json_safe skipped)", "xlpipe/cache.py",
     "                payload, _ = json_safe(it.payload)", "                payload = it.payload"),
    ("transport retry gives up immediately", "xlpipe/sync.py",
     "    for attempt in range(6):", "    for attempt in range(1):"),
    # --- added with the second round of tests
    ("device ICT median -> mean", "webapp/data.py",
     '{"ict": statistics.median(v),', '{"ict": statistics.mean(v),'),
    # (A first version weakened the day-level backstop, `day <= tainted` -> `<`: an equivalent mutant,
    # because the per-day min(usable) filter already drops that day. This one hits the filter itself.)
    ("tainted production day kept when any of its rows are usable", "webapp/data.py",
     "       min(COALESCE(e.usable, 1))", "       max(COALESCE(e.usable, 1))"),
    ("earned-at-SAP hours use run time instead of cycles", "webapp/__init__.py",
     '"earned_sap": a["cycles"] * spc if spc else None', '"earned_sap": a["run"] * spc if spc else None'),
    ("edit API accepts zero", "webapp/__init__.py",
     "        if not math.isfinite(v) or v <= 0:", "        if not math.isfinite(v) or v < 0:"),
    ("CSV import ignores the unit column", "webapp/__init__.py",
     'unit = row.get("sap_unit") or row.get("unit") or unit_default', "unit = unit_default"),
    ("record_order mode moves past an open event", "xlpipe/sync.py",
     "                        break  # never move past an open event", "                        continue"),
    ("schema drift recorded and carried on, not held", "xlpipe/sync.py",
     "        except SystemicError:\n            raise", "        except ZeroDivisionError:\n            raise"),
    ("data errors classified as systemic (would hold forever)", "xlpipe/store.py",
     '    if s.startswith("22") or s == "23502":\n        return RowRejected',
     '    if s.startswith("22") or s == "23502":\n        return SystemicError'),
    ("epoch picked earliest instead of latest", "xlpipe/cache.py",
     'ORDER BY {order} DESC LIMIT 1', 'ORDER BY {order} ASC LIMIT 1'),
    ("fleet window uses the earliest device, not the latest", "webapp/data.py",
     "        return max(firsts) if len(firsts) == len(self.history) and firsts else None",
     "        return min(firsts) if len(firsts) == len(self.history) and firsts else None"),
    ("open-run 7-day limit -> 70 days", "webapp/data.py", "OPEN_DAYS_LIMIT = 7", "OPEN_DAYS_LIMIT = 70"),
    ("200-cycle filter dropped from the open-run median", "webapp/data.py",
     '                f"AND run_cycles > 200",', '                f"",'),
    ("performance > 100% not flagged at all", "webapp/templates/_macros.html",
     "{%- elif v > 1 -%}<span class=bad", "{%- elif v > 9 -%}<span class=bad"),
    ("NaN stored as a number (guard removed)", "xlpipe/metrics.py",
     "    if not math.isfinite(out):\n        return None\n    return out", "    return out"),
    ("'null' category string stored literally", "xlpipe/metrics.py",
     'return None if out == DEVICE_NULL or out == "" else out', 'return None if out == "" else out'),
    # --- added with the line-page charts
    ("heatmap: interrupted event keeps its time (earliest-started wins)", "webapp/data.py",
     "        ev = max(live, key=lambda ev: ev[0])", "        ev = min(live, key=lambda ev: ev[0])"),
    ("heatmap: rows only for days with hour records (idle stretch vanishes)", "webapp/__init__.py",
     "            heat_days = [d for d in range(min(mine), max(mine) + 1) if win.in_period(d)]",
     "            heat_days = days"),
    ("quadrant: frequency split no longer scaled by production days", "webapp/charts.py",
     "    split_e = max(1.0, frequent_per_day * n_days)", "    split_e = 10.0"),
    ("quadrant: 'long' line ignores the configured minutes", "webapp/charts.py",
     "    split_m = long_minutes * 60", "    split_m = 10 * 60"),
    ("quadrant thresholds in config.yaml silently ignored", "webapp/__init__.py",
     '"long_minutes": float(charts_cfg.get("long_stop_minutes", C.LONG_STOP_MINUTES)),',
     '"long_minutes": float(C.LONG_STOP_MINUTES * 2),'),
    ("quadrant: labels placed without the collision check", "webapp/charts.py",
     "            if clear(box):", "            if True:"),
    # --- added with the Pareto, rhythm, changeover matrix and implausible flag
    ("implausible: the 24 h cap ignored", "webapp/data.py",
     "        if dur > rule.cap_hours * 3600 or (", "        if ("),
    ("implausible: median multiple effectively disabled", "webapp/data.py",
     "dur > rule.multiple * m):", "dur > rule.multiple * m * 1000):"),
    ("quadrant and Pareto include implausible events", "webapp/__init__.py",
     'clean = D.reasons(path, dev, lo, hi, "unplanned_stop", flag_ids, part)',
     'clean = D.reasons(path, dev, lo, hi, "unplanned_stop", set(), part)'),
    ("rhythm includes implausible events", "webapp/__init__.py",
     "weeks = D.weekly_rhythm(path, dev, r_lo, hi, flag_ids, part)", "weeks = D.weekly_rhythm(path, dev, r_lo, hi, (), part)"),
    ("changeovers: a stop charged to every overlapping change", "webapp/data.py",
     'if imp == "planned_stop" and s >= last_run and eid not in charged:',
     'if imp == "planned_stop" and s >= last_run:'),
    ("changeovers: breakdowns counted as changeover cost", "webapp/data.py",
     'if imp == "planned_stop" and s >= last_run and eid not in charged:',
     'if imp in ("planned_stop", "unplanned_stop") and s >= last_run and eid not in charged:'),
    ("Pareto: running total over the shown reasons only", "webapp/charts.py",
     '    total = sum(r["seconds"] for r in rs)\n    rows =', '    total = sum(r["seconds"] for r in rs[:top])\n    rows ='),
    ("changeover colour bins shifted", "webapp/charts.py", "CO_BINS = (1, 2, 4, 8)", "CO_BINS = (2, 4, 8, 16)"),
    ("implausible panel: availability what-if not adjusted", "webapp/__init__.py",
     '"avail_if": t.run / (sched - fp - fu) if sched - fp - fu > 0 else None,',
     '"avail_if": t.run / sched if sched else None,'),
    ("rhythm: colliding end labels drawn anyway", "webapp/charts.py",
     "        if pts and not clash:", "        if pts:"),
    # --- added with the change-point marker, the Pareto verdict and the convention note
    ("Pareto: short-list rule loosened", "webapp/charts.py",
     "SHORT_LIST_MAX = 5 ", "SHORT_LIST_MAX = 50 "),
    ("change point: noise test ignored", "webapp/charts.py",
     "    if shift is None or abs(shift) < min_shift or t < min_t:", "    if shift is None or abs(shift) < min_shift:"),
    ("change point: size test ignored", "webapp/charts.py",
     "    if shift is None or abs(shift) < min_shift or t < min_t:", "    if shift is None or t < min_t:"),
    ("change-point thresholds in config.yaml ignored", "webapp/__init__.py",
     '"min_t": float(charts_cfg.get("rhythm_change_min_t", C.RHYTHM_MIN_T)),', '"min_t": float(C.RHYTHM_MIN_T),'),
    ("availability convention note dropped from the panel", "webapp/templates/line.html",
     "<p><strong>Which availability convention.</strong>", "<p><strong></strong>"),
    # --- product pages and the line page's product filter
    ("product: implausible stops kept in the chart down hours", "webapp/data.py",
     "        if eid in excl:\n", "        if False:\n"),
    ("product: a stop after a run's end charged to that run", "webapp/data.py",
     'if i < 0 or s >= out[i]["end"]:', "if i < 0:"),
    ("ICT: an unconfirmed jump starts a new level", "webapp/data.py",
     "elif i + 1 < len(vals) and abs(vals[i + 1] / vals[i] - 1) <= tolerance:", "elif True:"),
    ("ICT tolerance in config.yaml silently ignored", "webapp/__init__.py",
     'float(charts_cfg.get("ict_change_tolerance", D.ICT_TOLERANCE))', "D.ICT_TOLERANCE"),
    ("product compare uses the device's down hours, implausible included", "webapp/__init__.py",
     'a["down_s"] += r["down_s"]', 'a["down_s"] += r["unp_s"]'),
    ("product changeovers: in and out swapped", "webapp/charts.py",
     '        if c["to"] == part:\n            into', '        if c["from"] == part:\n            into'),
    ("line filter: charts' stop reasons ignore the product", "webapp/__init__.py",
     'D.reasons(path, dev, lo, hi, "unplanned_stop", flag_ids, part)',
     'D.reasons(path, dev, lo, hi, "unplanned_stop", flag_ids)'),
    ("line filter: hour table ignores the product", "webapp/data.py",
     "extra, args = _of_part(part, hour_record=True)", "extra, args = _of_part(None, hour_record=True)"),
    ("line filter: heatmap paints other products as the chosen one", "webapp/data.py",
     'kind = "other" if part and ev_part != part', 'kind = "other" if False'),
    ("line filter: heatmap day links drop the product", "webapp/__init__.py",
     "p=D.production_day(n).isoformat(), part=part)", "p=D.production_day(n).isoformat())"),
    ("part names no longer link to their product page", "webapp/__init__.py",
     "        return Markup(_part_link(p))", "        return escape(p)"),
    # --- the guards themselves: the runner and the export. The runner's own targets are written as
    # two adjacent strings, so this list does not contain the line it mutates (one match, not two).
    ("runner: the pin check skipped", "tests/mutation_check.py",
     "        elif pin_of(rel, old) " "!= PINS[name]:", "        elif False:"),
    ("runner: a second run can start (lock not exclusive)", "tests/mutation_check.py",
     "fd = os.open(LOCK, os.O_CREAT " "| os.O_EXCL | os.O_WRONLY)", "fd = os.open(LOCK, os.O_CREAT | os.O_WRONLY)"),
    ("runner: an edit to the mutated file is wiped by the restore", "tests/mutation_check.py",
     "            if written and " "path.read_bytes() != mutated:", "            if False:"),
    ("runner: edits to other files mid-run go unnoticed", "tests/mutation_check.py",
     "        if edited := changed_since(" "before, except_for=None):", "        if edited := []:"),
    ("export: prune deletes files that are not exports", "webapp/export.py",
     "if f.is_file() and f != keep and EXPORT_NAME.fullmatch(f.name)]", "if f.is_file() and f != keep]"),
    ("export: --keep not passed through", "webapp/__main__.py",
     "    build(out=args.out, keep=args.keep)", "    build(out=args.out)"),
    # --- the live view
    ("live: a failed read moves observed_at, so stale values look fresh", "xlpipe/live.py",
     '"last_error = excluded.last_error", (device_id, attempted_at, error[:500]))',
     '"last_error = excluded.last_error, observed_at = excluded.attempted_at", (device_id, attempted_at, error[:500]))'),
    ("live: an old reading is not marked stale on the server", "webapp/live.py",
     'r["stale"] = r["never_read"] or r["failing"] or r["age_s"] > stale_after',
     'r["stale"] = r["never_read"] or r["failing"]'),
    ("live: a failing device is not flagged", "webapp/live.py",
     'r["failing"] = bool(r.get("last_error")) and', 'r["failing"] = False and'),
    ("live: the browser never marks an ageing reading stale", "webapp/templates/live.html",
     "const stale = p.dataset.failing === '1' || age > parseFloat(p.dataset.staleAfter);",
     "const stale = p.dataset.failing === '1';"),
    ("live: a state start from a different event is trusted", "xlpipe/live.py",
     'if st.get("process_state_event_id") == ids.get("process_state_event_id") else None,',
     "if True else None,"),
    ("live: an empty table is rendered as panels", "webapp/live.py",
     "    if not got:\n        return None", "    if False:\n        return None"),
    ("live: availability leaves planned stops out", "webapp/live.py",
     'r["availability"] = run / (run + unp + pln) if run + unp + pln > 0 else None',
     'r["availability"] = run / (run + unp) if run + unp > 0 else None'),
    # --- live auto-refresh
    ("live: updates reload the whole page instead of updating in place", "webapp/templates/live.html",
     "      apply(j);\n", "      location.reload();\n"),
    ("live: a failed poll resets the ages, so a page cut off from the server looks current",
     "webapp/templates/live.html",
     "      S.failing = true;                          // keep the last values",
     "      S.failing = true; S.t0 = performance.now(); // keep the last values"),
    ("live: the marker never shows that updates have stopped", "webapp/templates/live.html",
     "    dot.classList.toggle('down', S.failing);", "    dot.classList.toggle('down', false);"),
    ("live: polling carries on while the tab is hidden", "webapp/templates/live.html",
     "    if (document.hidden) return;                 // resumes on visibilitychange",
     "    ;                                            // resumes on visibilitychange"),
    ("live: showing the tab again does not read at once", "webapp/templates/live.html",
     "document.addEventListener('visibilitychange', () => { document.hidden ? clearTimeout(S.timer) : poll(); });",
     "document.addEventListener('visibilitychange', () => { clearTimeout(S.timer); });"),
    ("live: stale threshold default back to three intervals", "webapp/__init__.py",
     '"stale_after": float(live_cfg.get("stale_after_seconds", 4 * interval)),',
     '"stale_after": float(live_cfg.get("stale_after_seconds", 3 * interval)),'),
    ("live: a long state shows only the counted time", "webapp/templates/_live_panel.html",
     "wall - r.state_elapsed_s > 600 %}", "wall - r.state_elapsed_s > 10 ** 9 %}"),
    ("live: days ago truncated (Friday to Wednesday reads 4 days)", "webapp/templates/_live_macros.html",
     "{{ (h / 24)|round|int }} days ago", "{{ (h / 24)|int }} days ago"),
    # --- the sticky band
    ("sticky: the nav scrolls away", "webapp/static/style.css",
     "nav{position:sticky;top:0;z-index:30;", "nav{position:static;top:0;z-index:30;"),
    ("sticky: the bar sticks at the top, under the nav", "webapp/static/style.css",
     "[data-bar]{position:sticky;top:var(--navh);", "[data-bar]{position:sticky;top:0;"),
    ("sticky: on a phone the bar sticks too", "webapp/static/style.css",
     "  [data-bar]{position:static;box-shadow:none}", "  [data-bar]{box-shadow:none}"),
    ("sticky: in-page jumps land under the band", "webapp/static/style.css",
     "html{scroll-padding-top:calc(var(--navh) + var(--barh) + 12px)}", "html{}"),
    ("sticky: the band never shows it is floating", "webapp/templates/_sticky.html",
     "sentinel.getBoundingClientRect().top < 0)", "sentinel.getBoundingClientRect().top < -1e9)"),
    ("sticky: the nav's real height is never measured", "webapp/templates/_sticky.html",
     "    root.style.setProperty('--navh', nav.offsetHeight + 'px');", "    ;"),
    # --- tooltips and definitions
    ("definitions: the footer list is never rendered", "webapp/templates/base.html",
     '{% include "_glossary.html" %}', ""),
    ("definitions: terms used are not recorded, so the page lists none", "webapp/__init__.py",
     "            used.append(key)", "            pass"),
    ("tooltip: no flip at the right edge", "webapp/templates/_interact.html",
     "    if (left + w > innerWidth - 8) left = x - gap - w;", "    ;"),
    ("tooltip: the browser's own title tooltip is left on top", "webapp/templates/_interact.html",
     "      if (t) { n.dataset.tip = t.textContent; t.remove(); return [n, n.dataset.tip]; }",
     "      if (t) { n.dataset.tip = t.textContent; return [n, n.dataset.tip]; }"),
    ("tooltip: a second tap does not close it", "webapp/templates/_interact.html",
     "    if (tipFor === got[0]) { hideTip(); return; }", "    ;"),
    ("definitions: Escape does not return focus to the term", "webapp/templates/_interact.html",
     "    if (ev.key === 'Escape') { if (!pop.hidden) closeDef(true); hideTip(); }",
     "    if (ev.key === 'Escape') { if (!pop.hidden) closeDef(false); hideTip(); }"),
    ("heatmap tooltip: the first hour dated by its production day", "webapp/charts.py",
     '                    f"{st:%a} {st.day} {st:%b} {st:%H:%M}"', '                    f"{d:%a} {d.day} {d:%b} {st:%H:%M}"'),
    ("heatmap tooltip: only the buckets that ran are listed", "webapp/charts.py",
     "for k in D.IMPACTS)", "for k in D.IMPACTS if c['seconds'].get(k))"),
    ("Pareto tooltip: the 80% count is of the shown bars", "webapp/charts.py",
     "f'{need} of {len(rs)} reasons</title></rect></g>')", "f'{n80} of {len(rs)} reasons</title></rect></g>')"),
    ("rhythm tooltip: change from the previous week inverted", "webapp/charts.py",
     '({w[key] - prev:+.0f} min on the week before)', '({prev - w[key]:+.0f} min on the week before)'),
    ("quadrant tooltip: the zone's implication dropped", "webapp/charts.py",
     "{name}\\n{action[0].upper() + action[1:]}", "{name}\\n{name}"),
]


def _lines(rel: str, old: str) -> tuple[str | None, int]:
    """The whole line(s) holding `old` in `rel`, line endings normalised,
    and how many times `old` occurs there."""
    path = ROOT / rel
    if not path.exists():
        return None, 0
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    n = text.count(old)
    if n != 1:
        return None, n
    i = text.index(old)
    a = text.rfind("\n", 0, i) + 1
    b = text.find("\n", i + len(old))
    return text[a:len(text) if b < 0 else b], 1


def pin_of(rel: str, old: str) -> str | None:
    lines, _ = _lines(rel, old)
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()[:12] if lines is not None else None


def _loads(rel: str, old: str, new: str) -> str | None:
    """None if the mutated file still compiles (Python) or parses (Jinja);
    otherwise the error. A mutant that cannot load fails every test, which
    says nothing about the tests."""
    text = (ROOT / rel).read_bytes().decode("utf-8").replace("\r\n", "\n").replace(old, new)
    try:
        if rel.endswith(".py"):
            compile(text, rel, "exec")
        elif rel.endswith(".html"):
            import jinja2
            jinja2.Environment().parse(text)
    except Exception as exc:  # noqa: BLE001 - any failure to load is the finding
        return f"{type(exc).__name__}: {exc}".splitlines()[0]
    return None


def verify() -> list[str]:
    """Every mutation, checked against its pin. Empty means all would apply."""
    problems = []
    names = [m[0] for m in MUTATIONS]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"duplicate name {dup!r}: pins are keyed by name")
    for i, (name, rel, old, new) in enumerate(MUTATIONS, 1):
        lines, n = _lines(rel, old)
        if not (ROOT / rel).exists():
            problems.append(f"{i:3}  {name}: {rel} does not exist")
        elif n != 1:
            problems.append(f"{i:3}  {name}: original text found {n} times in {rel}, not once")
        elif old == new:
            problems.append(f"{i:3}  {name}: mutated text is the original")
        elif name not in PINS:
            problems.append(f"{i:3}  {name}: no pin (new entry? take it from --pins)")
        elif pin_of(rel, old) != PINS[name]:
            problems.append(f"{i:3}  {name}: the target line in {rel} changed since it was pinned")
        elif (err := _loads(rel, old, new)) is not None:
            problems.append(f"{i:3}  {name}: the mutated file does not load ({err}), so the suite would "
                            "catch it for the wrong reason")
    for name in sorted(set(PINS) - set(names)):
        problems.append(f"pin for {name!r} has no mutation")
    return problems


def run_suite() -> tuple[int, int]:
    # MUTATION_CHECK_RUNNING tells test_mutation_pins.py to stand aside: with a file mutated, its
    # pin no longer matches, and the suite would "catch" every mutation for the wrong reason.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "MUTATION_CHECK_RUNNING": "1"}
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x",
                          "--deselect", "tests/test_export.py", "tests"],
                         cwd=ROOT, capture_output=True, text=True, env=env).stdout
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", tail)) else 0
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", tail)) else 0
    return failed, passed


LOCK = ROOT / ".mutation_run.lock"
BACKUP = ROOT / ".mutation_run.original"
WATCHED = ("webapp", "xlpipe", "tests")


class EditedDuringRun(RuntimeError):
    pass


def snapshot() -> dict[Path, str]:
    """Hash of every source file the suite could depend on."""
    files = [ROOT / "config.yaml", ROOT / "verify.py", ROOT / "schema.sql"]
    for d in WATCHED:
        files += [f for f in (ROOT / d).rglob("*") if f.is_file() and f.suffix in (".py", ".html", ".css", ".yaml")
                  and "__pycache__" not in f.parts and "fixtures" not in f.parts]
    return {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files if f.exists()}


def changed_since(before: dict[Path, str], *, except_for: Path | None = None) -> list[str]:
    now = snapshot()
    return sorted(str(f.relative_to(ROOT)) for f in set(before) | set(now)
                  if f != except_for and before.get(f) != now.get(f))


def take_lock() -> bool:
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"pid {os.getpid()}, started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    return True


def note_in_lock(line: str) -> None:
    with open(LOCK, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def clear_bytecode() -> None:
    for d in ROOT.glob("**/__pycache__"):
        if ".venv" not in d.parts:
            shutil.rmtree(d, ignore_errors=True)


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for i, (name, f, *_ ) in enumerate(MUTATIONS, 1):
            print(f"{i:3}  {name}  ({f})")
        return 0
    if "--pins" in argv:
        for i, (name, rel, old, _) in enumerate(MUTATIONS, 1):
            pin = pin_of(rel, old)
            if pin is None or PINS.get(name) != pin:
                lines, _ = _lines(rel, old)
                print(f"{i:3}  {name}\n     line now: {lines!r}\n     pin:      {name!r}: {pin!r},")
        return 0
    problems = verify()
    if problems or "--check" in argv:
        for line in problems:
            print("STALE " + line)
        if problems:
            print(f"\nrefusing to run: {len(problems)} mutation(s) would not test what they say. "
                  "Fix the entries (see --pins), then run again.")
            return 2
        print(f"all {len(MUTATIONS)} mutations pinned and applicable")
        return 0
    chosen = [int(a) for a in argv if a.isdigit()] or list(range(1, len(MUTATIONS) + 1))
    if not take_lock():
        print(f"refusing to run: {LOCK.name} exists, so another mutation run is active or one was killed.\n"
              + LOCK.read_text(encoding="utf-8")
              + "If no run is active: run --check (a killed run can leave a file mutated; the original bytes "
                f"are in {BACKUP.name}), put that file right, then delete {LOCK.name}.")
        return 2
    try:
        return _run(chosen)
    except EditedDuringRun as exc:
        print(f"\nSTOPPED: {exc}\nNo verdict: the results above were run against code that was changing.")
        return 3
    finally:
        BACKUP.unlink(missing_ok=True)
        LOCK.unlink(missing_ok=True)


def _run(chosen: list[int]) -> int:
    clear_bytecode()
    base_failed, _ = run_suite()
    if base_failed:
        print("the suite fails without any mutation; fix that first")
        return 2
    caught = 0
    before = snapshot()
    for i in chosen:
        name, rel, old, new = MUTATIONS[i - 1]
        path = ROOT / rel
        if edited := changed_since(before):
            raise EditedDuringRun(f"source changed during the run, before #{i}: {', '.join(edited)}")
        original = path.read_bytes()
        text = original.decode("utf-8")
        crlf = (old.replace(chr(10), chr(13) + chr(10)), new.replace(chr(10), chr(13) + chr(10)))
        if text.count(old) != 1 and text.count(crlf[0]) == 1:
            old, new = crlf   # the file has Windows line endings
        if text.count(old) != 1:     # verify() passed, so the file changed during this run
            raise RuntimeError(f"#{i} {name}: {rel} changed while mutating; stop and look")
        written = False
        try:
            mutated = text.replace(old, new).encode("utf-8")
            assert mutated != original, f"#{i} {name}: mutation changed nothing"
            BACKUP.write_bytes(original)
            note_in_lock(f"#{i} applied to {rel}")
            path.write_bytes(mutated)
            written = True
            clear_bytecode()
            failed, _ = run_suite()
        finally:
            if written and path.read_bytes() != mutated:      # someone saved this file while it was mutated
                kept = path.with_name(path.name + ".edited-during-mutation")
                kept.write_bytes(path.read_bytes())
                path.write_bytes(original)
                os.utime(path, None)
                clear_bytecode()
                raise EditedDuringRun(f"{rel} was edited while #{i} was applied. The original is restored; "
                                      f"the edited version (mutation #{i} included) is in {kept.name}")
            path.write_bytes(original)
            os.utime(path, None)
            clear_bytecode()
            assert path.read_bytes() == original, f"could not restore {rel}"
        note_in_lock(f"#{i} restored")
        if edited := changed_since(before, except_for=None):
            raise EditedDuringRun(f"source changed while #{i} ran: {', '.join(edited)}")
        caught += bool(failed)
        print(f"{i:3}  {'caught' if failed else 'MISSED'}  {name}", flush=True)
    print(f"\n{caught} of {len(chosen)} mutations caught")
    return 0 if caught == len(chosen) else 1


# Hash of each mutation's target line(s), as pinned after its last reviewed run. See the docstring.
PINS: dict[str, str] = {
    'unusable-epoch days included in aggregates': 'e7df5be5d38c',
    "perf >100% short-run wording dropped (all say 'wrong ICT')": '0b37acf73485',
    'SAP pieces/hour display conversion wrong': '42451cfcf21a',
    'negative-duration display clamp removed': '3deb940b79cc',
    'changeover arrays stored as Python repr': '2929049171a6',
    'open-run median multiple 3x -> 30x': 'cfd24caf9f0e',
    'sync_id upsert guard removed': '00749bff8d64',
    'epoch re-stamping disabled': '5eea92b2afe2',
    'delete reconciliation disabled': '7fdf3116ac28',
    'part_standard ICT median -> mean': '3e7e7e62c60f',
    'products vs-derived ratio inverted': 'c6d4d2cc1f76',
    'store row rejection: no row-by-row fallback': 'e657a0e3578e',
    'weeks start Sunday instead of Monday': '3888a6f204f7',
    'calendar use computed the old way (full hours only)': '81558d4da521',
    'per-piece figures shown for untrusted epochs': '856bfca326ff',
    'NaN reaches the quarantine payload (json_safe skipped)': '7cdaed42472c',
    'transport retry gives up immediately': 'ba938d00b2da',
    'device ICT median -> mean': 'd08a6ba1f6fe',
    'tainted production day kept when any of its rows are usable': '73d71b8446cc',
    'earned-at-SAP hours use run time instead of cycles': 'b468894d5248',
    'edit API accepts zero': 'bd7d0971535a',
    'CSV import ignores the unit column': '2a39b85da914',
    'record_order mode moves past an open event': 'ad0a5a6850a3',
    'schema drift recorded and carried on, not held': '12e808586ab2',
    'data errors classified as systemic (would hold forever)': '34957c15451f',
    'epoch picked earliest instead of latest': 'fa77c07c1f09',
    'fleet window uses the earliest device, not the latest': '3dfa45d52470',
    'open-run 7-day limit -> 70 days': '92f7b3e18211',
    '200-cycle filter dropped from the open-run median': '2b15601ff57c',
    'performance > 100% not flagged at all': '13d75c99ecf6',
    'NaN stored as a number (guard removed)': '034b0377bc87',
    "'null' category string stored literally": 'c8ea718ad816',
    'heatmap: interrupted event keeps its time (earliest-started wins)': '96df0e79ed53',
    'heatmap: rows only for days with hour records (idle stretch vanishes)': 'e1dfa8fed046',
    'quadrant: frequency split no longer scaled by production days': '14bc71e17bc3',
    "quadrant: 'long' line ignores the configured minutes": '74fb78b80650',
    'quadrant thresholds in config.yaml silently ignored': '7f215f3ecfed',
    'quadrant: labels placed without the collision check': '09a8c43757d5',
    'implausible: the 24 h cap ignored': 'aa45d122b59b',
    'implausible: median multiple effectively disabled': 'aa45d122b59b',
    'quadrant and Pareto include implausible events': 'd982dbd2b3d8',
    'rhythm includes implausible events': 'b279917bc8d8',
    'changeovers: a stop charged to every overlapping change': 'fdef39d39b0c',
    'changeovers: breakdowns counted as changeover cost': 'fdef39d39b0c',
    'Pareto: running total over the shown reasons only': 'f8c645686784',
    'changeover colour bins shifted': '6081b377f5bb',
    'implausible panel: availability what-if not adjusted': '0fa9048f1705',
    'rhythm: colliding end labels drawn anyway': 'e456d562f8b1',
    'Pareto: short-list rule loosened': 'fea39a7cbe03',
    'change point: noise test ignored': 'fc2855b6a1ad',
    'change point: size test ignored': 'fc2855b6a1ad',
    'change-point thresholds in config.yaml ignored': '0ecae7f4e04c',
    'availability convention note dropped from the panel': '9a6564576c53',
    'product: implausible stops kept in the chart down hours': 'e163352d481c',
    "product: a stop after a run's end charged to that run": '425599a32ca5',
    'ICT: an unconfirmed jump starts a new level': 'd83d4047fb3a',
    'ICT tolerance in config.yaml silently ignored': '8637e87e900b',
    "product compare uses the device's down hours, implausible included": '5b2599c60223',
    'product changeovers: in and out swapped': '7a88bb0983e7',
    "line filter: charts' stop reasons ignore the product": 'd982dbd2b3d8',
    'line filter: hour table ignores the product': '470430517561',
    'line filter: heatmap paints other products as the chosen one': '7883f99cfbc3',
    'line filter: heatmap day links drop the product': '8650b32ead95',
    'part names no longer link to their product page': 'b0e63485aed9',
    'runner: the pin check skipped': '7740469144e3',
    'runner: a second run can start (lock not exclusive)': '1d375222c0bf',
    'runner: an edit to the mutated file is wiped by the restore': 'b1d833c07690',
    'runner: edits to other files mid-run go unnoticed': '32ef1c447fbf',
    'export: prune deletes files that are not exports': 'a32f03418071',
    'export: --keep not passed through': '0b7165be6e10',
    'live: a failed read moves observed_at, so stale values look fresh': 'cd59109bb519',
    'live: an old reading is not marked stale on the server': 'c03266c004ad',
    'live: a failing device is not flagged': 'f48a75f91b31',
    'live: the browser never marks an ageing reading stale': '78cae1df1b99',
    'live: a state start from a different event is trusted': 'fa1eea2c9df5',
    'live: an empty table is rendered as panels': '52c92d458650',
    'live: availability leaves planned stops out': '9b2669783896',
    'live: updates reload the whole page instead of updating in place': 'db89b4f0e6f0',
    'live: a failed poll resets the ages, so a page cut off from the server looks current': '867cda0b4ba1',
    'live: the marker never shows that updates have stopped': '2fe9d2451ba8',
    'live: polling carries on while the tab is hidden': 'f28f2449a1b0',
    'live: showing the tab again does not read at once': '2e2748186089',
    'live: stale threshold default back to three intervals': 'c953fae5df8c',
    'live: a long state shows only the counted time': '1dacdab8bebe',
    'live: days ago truncated (Friday to Wednesday reads 4 days)': 'a7f0be74c227',
    'sticky: the nav scrolls away': 'a001190b3817',
    'sticky: the bar sticks at the top, under the nav': '0ca1f87afa6b',
    'sticky: on a phone the bar sticks too': '12b266adde35',
    'sticky: in-page jumps land under the band': '99188d275640',
    'sticky: the band never shows it is floating': '14e69f76903f',
    "sticky: the nav's real height is never measured": 'ddecfa9fb0d8',
    'definitions: the footer list is never rendered': '5e36062f6d2e',
    'definitions: terms used are not recorded, so the page lists none': '577eade92899',
    'tooltip: no flip at the right edge': 'b9c1211778e7',
    "tooltip: the browser's own title tooltip is left on top": 'bd50d9ddfca0',
    'tooltip: a second tap does not close it': 'f1ad84306812',
    'definitions: Escape does not return focus to the term': 'd533dfe06526',
    'heatmap tooltip: the first hour dated by its production day': 'f757ee1d904a',
    'heatmap tooltip: only the buckets that ran are listed': '162e36e73873',
    'Pareto tooltip: the 80% count is of the shown bars': 'bcc97935524a',
    'rhythm tooltip: change from the previous week inverted': '78d39adefd2c',
    "quadrant tooltip: the zone's implication dropped": '851c37b6eeeb',
}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
