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

def test_every_measure_asked_for_is_defined_in_full():
    for key in ASKED_FOR:
        t = GL.DEFS[key]
        assert all(len(v) > 10 for v in (t.means, t.formula, t.answers, t.caution)), key


def test_the_page_shows_the_modules_wording_and_nothing_else(app_client):
    h = app_client.get("/?g=month&p=2026-09").get_data(as_text=True)
    t = GL.DEFS["calendar_use"]
    block = h[h.index('id="def-calendar_use"'):]
    block = unescape(re.sub(r"<[^>]+>", " ", block[:block.index("</div>")]))
    for part in (t.label, t.means, t.formula, t.answers, t.caution):
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
                 "secondTapHides": True, "opens": True, "focusMoves": True, "escapeCloses": True,
                 "clickOutsideCloses": True}
