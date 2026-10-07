"""Chart tooltips and measure definitions.

Literals: FR8's production day Fri 3 Jul 2026 begins at 23:00 on Thu 2 Jul,
a planned-stop hour (test_charts pins the cell's dominance); AreaB year to
date needs 17 of 53 reasons for 80% of its lost hours (test_rhythm_pareto).
"""

import json
import re
import shutil
import subprocess
from html import unescape
from pathlib import Path

import pytest

from webapp import glossary as GL

ASKED_FOR = ("available", "performance", "calendar_use", "earned_h", "variance_h", "derived_ict", "actual_spc",
             "run_h", "stopped_h", "small_stops", "scheduled", "elapsed", "production_day", "open_run")


# ------------------------------------------------------------ the module

def test_every_measure_asked_for_is_defined_with_its_caution():
    for key in ASKED_FOR:
        t = GL.DEFS[key]
        assert t.means and t.answers and len(t.caution) > 40, key       # the formula only where there is one


def test_the_page_shows_the_modules_wording_and_nothing_else(app_client):
    h = app_client.get("/?g=month&p=2026-09").get_data(as_text=True)
    t = GL.DEFS["calendar_use"]
    block = h[h.index('id="def-calendar_use"'):]
    block = unescape(re.sub(r"<[^>]+>", " ", block[:block.index("</div>")]))
    for part in (t.label, t.means, t.formula.strip("`"), t.answers, t.caution):
        assert part in re.sub(r"\s+", " ", block), part


def test_an_unknown_term_fails_loudly(app_client):
    from flask import render_template_string

    with app_client.application.test_request_context("/"):
        with pytest.raises(KeyError):
            render_template_string("{{ term('not_a_measure') }}")


@pytest.mark.parametrize("url,keys", [
    ("/?g=month&p=2026-09", ("available", "performance", "calendar_use", "scheduled", "down", "production_day")),
    ("/line/AreaB?g=month&p=2026-09", ("available", "scheduled", "down", "production_day", "run_h", "planned",
                                       "open_run")),
    ("/products?g=month&p=2026-09", ("derived_ict", "actual_spc", "run_h", "earned_h", "variance_h", "elapsed",
                                     "stopped_h", "small_stops")),
    ("/product/OTRIVIN?g=ytd&p=2026", ("run_h", "down", "down_per_run", "planned", "available", "performance")),
])
def test_measures_are_marked_and_defined_on_the_page(app_client, url, keys):
    h = app_client.get(url).get_data(as_text=True)
    for k in keys:
        assert f'<a class=term href="#def-{k}" data-def="{k}">' in h, k
        assert f'<div class=def id="def-{k}" data-def-body="{k}">' in h, k
    used = re.findall(r'data-def="(\w+)"', h)
    listed = re.findall(r'data-def-body="(\w+)"', h)
    assert sorted(set(used)) == sorted(listed)                 # the footer lists exactly what the page named


def test_headers_carry_the_term_not_a_hover_only_title(app_client):
    h = app_client.get("/products?g=month&p=2026-09").get_data(as_text=True)
    assert 'title="actual − earned"' not in h and 'title="median ideal_time' not in h


# ------------------------------------------------------------ the tooltips

def titles(svg):
    return [unescape(t) for t in re.findall(r"<title>(.*?)</title>", svg, re.S)]


def test_heatmap_cell_names_the_hour_what_ran_all_four_buckets_and_the_state(app_client):
    h = app_client.get("/line/FR8?g=day&p=2026-07-03").get_data(as_text=True)
    first = titles(h[h.index("<svg class=heat"):])[0].split("\n")
    assert first[0] == "Thu 2 Jul 23:00–00:00 (production day Fri 3 Jul)"   # the hour's own date
    assert first[1].startswith("Ran ")
    assert re.fullmatch(r"Running \d+ min · Unplanned stop \d+ min · Planned stop \d+ min · Not scheduled \d+ min",
                        first[2])
    assert first[3].startswith("Mostly planned stop")


def test_quadrant_says_what_the_zone_implies(app_client):
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    t = [x for x in titles(h[h.index("<svg class=quad"):h.index("</svg>", h.index("<svg class=quad"))])
         if x.startswith("Bottle Jam")][0]
    assert t.endswith("· Chronic nuisance\nFrequent, short: process fix by the line team")


def test_pareto_bar_gives_mean_minutes_and_how_many_reasons_reach_80(app_client):
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    ts = titles(h[h.index("<svg class=pareto"):h.index("</svg>", h.index("<svg class=pareto"))])
    assert all(re.search(r"stops · \d+ min each\nrunning total \d+% · 80% of lost hours takes 17 of 53 reasons$", t)
               for t in ts)


def test_rhythm_point_gives_the_week_both_means_and_the_change():
    from webapp import charts as C

    weeks = [{"week": "2026-09-07", "run_bursts": 10, "stops": 8, "run_min": 30.0, "stop_min": 5.0},
             {"week": "2026-09-14", "run_bursts": 12, "stops": 9, "run_min": 24.0, "stop_min": 7.0},
             {"week": "2026-09-21", "run_bursts": 11, "stops": 7, "run_min": 26.0, "stop_min": 6.0}]
    ts = titles(str(C.rhythm(weeks)))
    assert ("Week of 14 Sep 2026\nMean run burst 24 min (-6 min on the week before) · 12 run bursts\n"
            "Mean unplanned stop 7 min (+2 min on the week before) · 9 stops") in ts
    assert ts[0].startswith("Week of 7 Sep 2026\nMean run burst 30 min (first week shown)")


def test_sparkline_bar_names_the_production_day_and_its_availability(app_client):
    h = app_client.get("/?g=month&p=2026-09").get_data(as_text=True)
    assert re.search(r'<i title="Production day 1 Sep 2026&#10;Available \d+\.\d%"', h)


# --------------------------------------------------- in a browser (Edge)

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
CSS = (Path(__file__).parent.parent / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
PROBE = r"""<script>
function at(el, type, kind, dx) {
  const r = el.getBoundingClientRect(), x = r.left + (dx ?? r.width / 2), y = r.top + r.height / 2;
  el.dispatchEvent(new PointerEvent(type, {bubbles: true, clientX: x, clientY: y, pointerType: kind}));
  return x;
}
addEventListener('load', () => setTimeout(() => {
  const o = {}, tip = document.getElementById('tip'), pop = document.getElementById('defpop');
  const cells = document.querySelectorAll('svg.heat rect[fill]');
  const x = at(cells[2], 'pointermove', 'mouse');
  o.hover = !tip.hidden && tip.textContent.includes('\n') && tip.getBoundingClientRect().left > x;
  o.nativeTitleGone = !cells[2].querySelector('title');
  const x2 = at(cells[23], 'pointermove', 'mouse', cells[23].getBoundingClientRect().width - 1);
  o.flips = tip.getBoundingClientRect().right <= x2;
  at(document.querySelector('h1'), 'pointermove', 'mouse');
  o.hidesOffChart = tip.hidden;
  at(cells[5], 'pointerdown', 'touch');
  o.tapShows = !tip.hidden;
  at(cells[5], 'pointerdown', 'touch');
  o.secondTapHides = tip.hidden;
  const term = document.querySelector('a.term[data-def="available"]');
  term.focus();
  term.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
  term.click();                                   // what Enter on a focused link does
  o.opens = !pop.hidden && pop.querySelector('h3').textContent === 'Available';
  o.focusMoves = pop.contains(document.activeElement);
  const where = pop.style.left + pop.style.top, see = pop.querySelector('.see a.term[data-def="planned"]');
  see.click();                                    // a cross-reference inside the open panel
  o.seeAlsoInPlace = !pop.hidden && pop.querySelector('h3').textContent === 'Planned stop'
                     && pop.style.left + pop.style.top === where;
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  o.escapeCloses = pop.hidden && document.activeElement === term;
  term.click();
  document.querySelector('h1').click();
  o.clickOutsideCloses = pop.hidden;
  document.body.dataset.probe = JSON.stringify(o);
}, 300));
</script>"""


def run_probe(html, tmp_path):
    if not EDGE.exists() and not shutil.which("msedge"):
        pytest.skip("no Edge on this machine to run the page's scripts")
    html = html.replace('<link rel=stylesheet href="/static/style.css">', f"<style>{CSS}</style>")
    f = tmp_path / "p.html"
    f.write_text(re.sub(r'<link href="https://fonts[^>]+>', "", html) + PROBE, encoding="utf-8")
    dom = subprocess.run([str(EDGE), "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'edge'}",
                          "--window-size=1280,900", "--virtual-time-budget=4000", "--dump-dom", f.as_uri()],
                         capture_output=True, text=True, encoding="utf-8", timeout=120).stdout
    return json.loads(unescape(re.search(r'data-probe="([^"]+)"', dom).group(1)))


def test_tooltips_and_definitions_in_a_browser(app_client, tmp_path):
    o = run_probe(app_client.get("/line/AreaB?g=month&p=2026-09").get_data(as_text=True), tmp_path)
    assert o == {"hover": True, "nativeTitleGone": True, "flips": True, "hidesOffChart": True, "tapShows": True,
                 "secondTapHides": True, "opens": True, "focusMoves": True, "seeAlsoInPlace": True, "escapeCloses": True,
                 "clickOutsideCloses": True}


# ------------------------------------------------------------ highlighting

def test_each_reason_links_its_bubble_bar_and_row(app_client):
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    quad = h[h.index("<svg class=quad"):h.index("</svg>", h.index("<svg class=quad"))]
    par = h[h.index("<svg class=pareto"):h.index("</svg>", h.index("<svg class=pareto"))]
    why = h[h.index("<h2>Why it stopped"):h.index("<h2>Planned stops")]
    assert '<g class=bubble data-hl="unp:Bottle Jam">' in quad
    assert '<g class=pbar data-hl="unp:Bottle Jam">' in par
    assert '<tr data-hl="unp:Bottle Jam"><th>Bottle Jam</th>' in why
    planned = h[h.index("<h2>Planned stops"):]
    assert "data-hl=\"unp:" not in planned[:planned.index("</table>")]      # planned reasons stay out of it
    assert 'All other' in par and not re.search(r'<g class=pbar data-hl="unp:All other', par)


def test_every_hour_row_points_at_the_cell_that_draws_its_hour(app_client):
    """Cells carry no key; a row's key names the heatmap row (window) and cell. Check each against the
    window boundaries the heatmap itself is drawn from."""
    from datetime import date

    from webapp import data as D

    h = app_client.get("/line/AreaB?g=month&p=2026-09").get_data(as_text=True)
    windows = set(re.findall(r'data-filter-day="([\d-]+)" class=heatrow>', h))
    rows = re.findall(r'<tr data-day="[\d-]+" data-hl="h:([\d-]+)/(\d+)"[^>]*>.*?<td>(\d\d):\d\d–', h, re.S)
    assert rows and len(rows) == h.count("<tr data-day=")                # every row in the table checked
    for window, i, hour in rows:
        assert window in windows
        bounds = D.production_day_window((date.fromisoformat(window) - D.EPOCH).days)[1]
        assert bounds[int(i)].hour == int(hour), (window, i, hour)       # that cell starts at the row's hour
    assert '<rect' in h and not re.search(r'<rect [^>]*data-hl=', h)    # nothing on the cells themselves
    # ...and each key, worked out another way: the window and cell of (start + 1 h) on the local clock
    # (a 23:00 hour belongs to the next day's row, at cell 0)
    from datetime import timedelta

    recs = D.hours(app_client.application.config["CACHE_PATH"], "AreaB", 20697, 20726)   # 1-30 Sep 2026
    expected = []
    for r in recs:
        t = r["start"].astimezone(D.TZ) + timedelta(hours=1)
        expected.append(f"h:{t.date().isoformat()}/{t.hour}")
    got = re.findall(r'<tr data-day="[\d-]+" data-hl="(h:[^"]+)"', h)
    assert got == expected
    assert any(k.endswith("/0") for k in got)                          # a 23:00 hour is among them


def test_overview_rows_link_their_sparkline(app_client):
    h = app_client.get("/?g=month&p=2026-09").get_data(as_text=True)
    for dev in ("AreaB", "AreaD", "AreaE", "FR8"):
        assert f'<tr data-hl="line:{dev}">' in h and f'<div class=strip data-hl="line:{dev}">' in h


HL_PROBE = r"""<script>
function over(el) { el.dispatchEvent(new PointerEvent('pointerover', {bubbles: true, pointerType: 'mouse'})); }
const lit = sel => [...document.querySelectorAll(sel)].map(e => e.classList.contains('hl'));
addEventListener('load', () => setTimeout(() => {
  const o = {};
  over(document.querySelector('tr[data-hl="unp:Bottle Jam"] th'));
  o.fromRow = lit('[data-hl="unp:Bottle Jam"]');
  o.othersDark = document.querySelectorAll('.hl').length;
  over(document.querySelector('g.bubble[data-hl="unp:Bottle Jam"] circle'));
  o.fromBubble = lit('[data-hl="unp:Bottle Jam"]');
  const row = document.querySelector('tr[data-hl^="h:"]');
  over(row.querySelector('td'));
  const [, day, idx] = /^h:([\d-]+)\/(\d+)$/.exec(row.dataset.hl);
  const cell = document.querySelectorAll(`a.heatrow[data-filter-day="${day}"] rect`)[+idx];
  o.hourCell = [cell.classList.contains('hl')];
  over(document.querySelector('h1'));
  over(cell);                                    // and the other way: the cell lights its rows
  o.cellLightsRow = row.classList.contains('hl') && cell.classList.contains('hl');
  over(document.querySelector('h1'));
  o.cleared = document.querySelectorAll('.hl').length;
  document.body.dataset.probe = JSON.stringify(o);
}, 300));
</script>"""


def test_pointing_at_either_side_lights_the_other(app_client, tmp_path):
    if not EDGE.exists() and not shutil.which("msedge"):
        pytest.skip("no Edge on this machine to run the page's scripts")
    html = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    html = html.replace('<link rel=stylesheet href="/static/style.css">', f"<style>{CSS}</style>")
    f = tmp_path / "hl.html"
    f.write_text(re.sub(r'<link href="https://fonts[^>]+>', "", html) + HL_PROBE, encoding="utf-8")
    dom = subprocess.run([str(EDGE), "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'edge'}",
                          "--window-size=1280,900", "--virtual-time-budget=6000", "--dump-dom", f.as_uri()],
                         capture_output=True, text=True, encoding="utf-8", timeout=180).stdout
    o = json.loads(unescape(re.search(r'data-probe="([^"]+)"', dom).group(1)))
    assert o == {"fromRow": [True, True, True], "othersDark": 3, "fromBubble": [True, True, True],
                 "hourCell": [True], "cellLightsRow": True, "cleared": 0}


# --------------------------------------------- the wording is GLOSSARY.md's

FIELDS = {"definition": "means", "formula": "formula", "answers": "answers", "caution": "caution",
          "status": "status", "why": "why"}


def glossary_md() -> dict[str, dict[str, str]]:
    """GLOSSARY.md, parsed on its own terms: each ### heading, and its **field:** paragraphs."""
    text = (Path(__file__).parent.parent / "GLOSSARY.md").read_text(encoding="utf-8")
    out = {}
    for block in re.split(r"^### ", text, flags=re.M)[1:]:
        head, _, body = block.partition("\n")
        body = re.split(r"\n---|\n## ", body)[0]
        fields = re.findall(r"\*\*(\w+):\*\*\s*(.*?)(?=\n\s*\*\*\w+:\*\*|\Z)", body, re.S)
        out[head.strip()] = {FIELDS[k]: " ".join(v.split()) for k, v in fields}
    return out


def test_every_glossary_md_entry_is_defined_word_for_word():
    md = glossary_md()
    assert len(md) == 28                                                # 25 measures + 3 deliberately absent
    by_label = {t.label: (k, t) for k, t in GL.DEFS.items() if k not in GL.NOT_IN_GLOSSARY_MD}
    assert set(by_label) == set(md)                                     # nothing dropped, nothing added
    for label, fields in md.items():
        _, t = by_label[label]
        for name in ("means", "formula", "answers", "caution", "status", "why"):
            assert (getattr(t, name) or None) == fields.get(name), (label, name)


def test_the_cautions_survive():
    """The cautions are the point. Spot-check that the findings they record are there in full."""
    assert "ADVIL in three different carton sizes shares one cycle time" in GL.DEFS["derived_ict"].caution
    assert "Across 1,803 shifts on all four lines, crew size has never been entered once" in GL.DEFS["labour"].why
    assert "It was reporting schedule adherence, not asset use." in GL.DEFS["calendar_use"].caution


def test_absent_measures_are_reachable_from_every_page(app_client):
    for url in ("/?g=month&p=2026-09", "/line/FR8?g=month&p=2026-09", "/products?g=month&p=2026-09",
                "/product/OTRIVIN?g=ytd&p=2026", "/live"):
        h = app_client.get(url).get_data(as_text=True)
        foot = h[h.index("<footer>"):]
        for k in GL.ABSENT:
            assert f'<a class=term href="#def-{k}" data-def="{k}">' in foot, (url, k)
            assert f'data-def-body="{k}"' in foot, (url, k)
        assert "Withheld." in foot and "Not available from the devices." in foot


def test_oee_is_linked_where_the_word_appears(app_client):
    h = app_client.get("/?g=month&p=2026-09").get_data(as_text=True)
    assert ('<a class=term href="#def-quality" data-def="quality">Quality</a> and '
            '<a class=term href="#def-oee" data-def="oee">OEE</a> are withheld') in h


def test_cross_references_link_and_their_targets_are_on_the_page(app_client):
    h = app_client.get("/products?g=month&p=2026-09").get_data(as_text=True)
    var = h[h.index('data-def-body="variance_h"'):]
    var = var[:var.index("</div>")]
    assert '<a class=term href="#def-performance" data-def="performance">Performance</a>' in var
    assert 'data-def-body="performance"' in h                           # the target is there to open
    run = h[h.index('data-def-body="running"'):]
    assert '<em>small stops</em>' in run[:run.index("</div>")]          # GLOSSARY.md's emphasis kept


def test_formulas_render_as_code_and_absent_parts_are_not_invented(app_client):
    h = app_client.get("/products?g=month&p=2026-09").get_data(as_text=True)
    ict = h[h.index('data-def-body="derived_ict"'):]
    ict = ict[:ict.index("</div>")]
    assert "<code>median of (ideal time ÷ run cycles)</code> across runs over 200 cycles" in ict
    run = h[h.index('data-def-body="run_h"'):]
    assert "Formula" not in run[:run.index("</div>")]                    # GLOSSARY.md gives Run h no formula


def test_no_provisional_note(app_client):
    h = app_client.get("/?g=month&p=2026-09").get_data(as_text=True)
    assert "provisional" not in h.lower() and "to be replaced" not in h
