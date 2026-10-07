"""Product pages and the line page's product filter.

Literals were computed with plain SQL and the implausible rule re-implemented
from scratch (scratchpad literals.py), not with the code under test:
  * Year to date from the fleet window (2 Apr 2026):
      AreaB OMNARIS   20 runs, 369.8 run h, 349.7 down h (device), 133.3 planned h;
                      239.1 down h once 3 implausible stops are left out: 0.65 per run h.
      AreaB OTRIVIN    8 runs,  52.4 run h, 28.0 down h, 15.1 clean.
      AreaD OTRIVIN   11 runs, 132.0 run h, 88.9 down h, 51.6 clean; open since 25 Aug.
      AreaB parts by clean down h per run h: DICLOFENAC 3.84, OMNARIS 0.65,
      OTRIVIN 0.29, OTRIVIN_PUMPi_20ML 0.21, OMNARIS_120P 0.20.
  * OMNARIS on AreaB: Bottle Jam 202 stops, 47.2 h, first of 47 reasons.
  * Ideal cycle time: AreaB OTRIVIN reconfigured 19 Jun (0.929 -> 0.889,
    -4%); AreaE REGAINE_5b_60ML_SINGLES 20 Apr (-11%); FR8 ABREVAi_3G 13 Mar
    (-10%, before the fleet window), with three single-run outliers.
  * Summing state events by time inside each closed run reproduces part_run's
    own down hours to the second on every run but one (13 s, FR8); the event's part field does not (it
    also carries the open run: 233.3 run h for AreaD OTRIVIN against 132.0).
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from webapp import charts as C
from webapp import data as D


def text(html, start, end):
    seg = html[html.index(start):html.index(end, html.index(start))] if end else html[html.index(start):]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", seg))


# ------------------------------------------------------------ the data

def test_events_inside_runs_reproduce_part_run_exactly(replayed_cache):
    db = sqlite3.connect(replayed_cache)
    devs = [r[0] for r in db.execute("SELECT device_id FROM device")]
    for dev in devs:
        runs = D.part_runs(replayed_cache, dev, 0, 99999)
        assert runs
        for r in runs:
            n = db.execute("SELECT coalesce(sum(duration_s), 0) FROM state_event WHERE device_id=? AND deleted_at IS NULL "
                           "AND performance_impact='unplanned_stop' AND start_time>=? AND start_time<?",
                           (dev, r["start"].isoformat(), r["end"].isoformat())).fetchone()[0]
            assert r["down_s"] == pytest.approx(n)
            # ...and the device's own run total, to the second, on every run but one: FR8's ESTRADIOL
            # from 21 Sep carries 13 s more in part_run than its events (nothing straddles the run).
            if (dev, r["start"].isoformat()) == ("FR8", "2026-09-21T17:52:58+00:00"):
                assert round(r["unp_s"] - r["down_s"]) == 13
            else:
                assert r["down_s"] == pytest.approx(r["unp_s"], abs=0.5), (dev, r["start"])


def test_flagged_stops_leave_the_chart_figure_only(replayed_cache):
    ids = {r["event_id"] for r in D.implausible(replayed_cache, dev="AreaB")}
    om = [r for r in D.part_runs(replayed_cache, "AreaB", 20545, 20732, ids) if r["part"] == "OMNARIS"]
    assert len(om) == 20
    assert round(sum(r["run_s"] for r in om) / 3600, 1) == 369.8
    assert round(sum(r["unp_s"] for r in om) / 3600, 1) == 349.7          # verbatim, unchanged
    assert round(sum(r["down_s"] for r in om) / 3600, 1) == 239.1
    assert sum(r["flagged_n"] for r in om) == 3


def pts(values, start=datetime(2026, 5, 1, tzinfo=timezone.utc)):
    return [{"start": start + timedelta(days=i), "ict": v, "cycles": 1000} for i, v in enumerate(values)]


def test_a_held_step_is_a_reconfiguration():
    lv = D.ict_levels(pts([0.930, 0.928, 0.885, 0.884, 0.896, 0.893]))
    assert [round(l["value"], 3) for l in lv["levels"]] == [0.929, 0.889]
    assert len(lv["changes"]) == 1 and lv["changes"][0]["start"].day == 3 and not lv["outliers"]


def test_one_run_off_is_an_outlier_not_a_reconfiguration():
    lv = D.ict_levels(pts([2.172, 2.202, 2.839, 2.187]))       # AreaD ELYXYB_4k8ML_TRADE
    assert lv["changes"] == [] and lv["outliers"] == {2}


def test_a_lone_first_run_does_not_make_a_level():
    lv = D.ict_levels(pts([0.75, 0.61, 0.61, 0.612, 0.609]))
    assert lv["changes"] == [] and lv["outliers"] == {0}


def test_noise_within_three_percent_is_one_level():
    lv = D.ict_levels(pts([0.375, 0.382, 0.378, 0.376, 0.381]))
    assert len(lv["levels"]) == 1 and not lv["outliers"]


def test_reconfigurations_on_the_recording(replayed_cache):
    found = {}
    for dev, part in (("AreaB", "OTRIVIN"), ("AreaE", "REGAINE_5b_60ML_SINGLES"), ("FR8", "ABREVAi_3G"),
                      ("FR8", "ESTRADIOL"), ("AreaD", "ELYXYB_4k8ML_TRADE")):
        lv = D.ict_levels(D.ict_history(replayed_cache, dev, part))
        found[part] = [(c["start"].date().isoformat(), f"{c['shift']:+.0%}") for c in lv["changes"]], len(lv["outliers"])
    assert found == {"OTRIVIN": ([("2026-06-19", "-4%")], 0), "REGAINE_5b_60ML_SINGLES": ([("2026-04-20", "-11%")], 0),
                     "ABREVAi_3G": ([("2026-03-13", "-10%")], 3), "ESTRADIOL": ([], 0), "ELYXYB_4k8ML_TRADE": ([], 1)}


# ------------------------------------------------------------ the page

def test_where_it_runs(app_client):
    h = app_client.get("/product/OTRIVIN?g=ytd&p=2026").get_data(as_text=True)
    t = text(h, "<h2>Where it runs", "<h2>Every run")
    assert re.search(r"AreaB 8 52\.4 28\.0 0\.53 34\.5", t)
    assert re.search(r"AreaD 11 132 88\.9 0\.67 93\.5", t)
    assert "reconfigured" in t                               # AreaB, 19 Jun


def test_open_run_is_named_and_left_out(app_client):
    h = app_client.get("/product/OTRIVIN?g=ytd&p=2026").get_data(as_text=True)
    assert re.search(r"A run of OTRIVIN on\s+AreaD has been open since 25 Aug 2026", h)
    assert "It is in none of the figures below." in h


def test_compare_highlights_this_part_with_clean_figures(app_client):
    h = app_client.get("/product/OMNARIS?g=ytd&p=2026").get_data(as_text=True)
    svg = h[h.index("<svg class=compare"):h.index("</svg>", h.index("<svg class=compare"))]
    order = re.findall(r"<title>([A-Za-z0-9_]+) · ([\d.]+) down h per run h", svg)
    assert order == [("DICLOFENAC", "3.83"), ("OMNARIS", "0.65"), ("OTRIVIN", "0.29"),
                     ("OTRIVIN_PUMPi_20ML", "0.21"), ("OMNARIS_120P", "0.20")]
    assert svg.count('class="bar"') == 1                     # only OMNARIS wears the colour
    assert 'href="/product/OTRIVIN?' in svg


def test_timeline_has_every_run(app_client):
    h = app_client.get("/product/OMNARIS?g=ytd&p=2026").get_data(as_text=True)
    svg = h[h.index("<svg class=runs"):h.index("</svg>", h.index("<svg class=runs"))]
    assert svg.count("class=hit>") == 14                     # 20 runs, 6 of which never cycled
    assert "6 runs never cycled (no run hours), so have no dot" in re.sub(r"\s+", " ", h)
    assert ">all runs 0.65</text>" in svg
    assert "3 implausible unplanned stops" in h


def test_reasons_are_this_parts(app_client):
    h = app_client.get("/product/OMNARIS?g=ytd&p=2026").get_data(as_text=True)
    svg = h[h.index("<svg class=pareto"):h.index("</svg>", h.index("<svg class=pareto"))]
    first = re.search(r"<title>([^<]+?) · ([\d.]+) h \((\d+)% of lost hours\) · ([\d,]+) stops", svg)
    assert first.groups() == ("Bottle Jam", "47.2", "20", "202")


def test_ict_reconfiguration_is_marked_and_explained(app_client):
    h = app_client.get("/product/OTRIVIN?g=ytd&p=2026").get_data(as_text=True)
    assert "class=changelab>reconfigured 19 Jun: 0.929 to 0.889 s (-4%)</text>" in h
    assert re.search(r"AreaB: reconfigured on\s+19 Jun 2026", h)
    assert "mixes both values" in h


def test_reconfiguration_before_the_period_is_said_so(app_client):
    h = app_client.get("/product/ABREVAi_3G?g=ytd&p=2026&widen=FR8").get_data(as_text=True)
    assert "FR8: reconfigured on" in h
    h = app_client.get("/product/REGAINE_5b_60ML_SINGLES?g=month&p=2026-09").get_data(as_text=True)
    assert "No closed run of REGAINE_5b_60ML_SINGLES started in this period" in h


def test_changeover_in_and_out(app_client):
    h = app_client.get("/product/OTRIVIN?g=ytd&p=2026").get_data(as_text=True)
    t = text(h, "<h2>What changing to and from it costs", "<h2>Piece counts")
    assert "Into it, from" in t and "Out of it, to" in t
    assert re.search(r"Out of it, to Part Changes Mean h Total h (?:\S+ \d+ [\d.]+ [\d.]+ )*DICLOFENAC 1 26\.5", t)


def test_unknown_part_is_404(app_client):
    assert app_client.get("/product/NOT_A_PART").status_code == 404


def test_part_names_link_to_their_page(app_client):
    h = app_client.get("/products?g=ytd&p=2026").get_data(as_text=True)
    assert '<a class=lnk href="/product/ESTRADIOL?g=ytd&amp;p=2026">ESTRADIOL</a>' in h
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    m = h[h.index("<table class=comatrix>"):]
    assert 'href="/product/OTRIVIN?g=ytd&amp;p=2026"' in m
    hours = h[h.index('<table class=hours>'):]
    assert re.search(r'<td><a class=lnk href="/product/OMNARIS\?g=ytd&amp;p=2026">OMNARIS</a></td>', hours)


def test_period_controls_apply(app_client):
    h = app_client.get("/product/OMNARIS?g=month&p=2026-08").get_data(as_text=True)
    t = text(h, "<h2>Where it runs", "<h2>Every run")
    m = re.search(r"AreaB (\d+) ", t)
    assert m and int(m.group(1)) < 20


# --------------------------------------------- line page, one product at a time

def test_line_filter_narrows_every_section(app_client):
    full = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    h = app_client.get("/line/AreaB?g=ytd&p=2026&part=OMNARIS").get_data(as_text=True)
    assert "Showing OMNARIS only" in h
    hours = h[h.index('<table class=hours>'):]
    # every hour row names OMNARIS
    rows = re.findall(r"<tr data-day=[^>]*>(.*?)</tr>", hours, re.S)
    assert rows and all("OMNARIS" in r for r in rows)
    # stop reasons are OMNARIS's: Bottle Jam drops from 260 stops line-wide
    assert re.search(r"<title>Bottle Jam · [\d.]+ h lost · 260 stops", full)
    assert not re.search(r"<title>Bottle Jam · [\d.]+ h lost · 260 stops", h)
    # the heatmap greys out other products' time
    assert 'class=other' in h and "Another product" in h


def test_line_filter_options_and_links_keep_it(app_client):
    h = app_client.get("/line/AreaB?g=ytd&p=2026&part=OMNARIS").get_data(as_text=True)
    sel = h[h.index('<select id=part'):h.index('</select>', h.index('<select id=part'))]
    assert '<option value="OMNARIS" selected>' in sel and '<option value="">All products</option>' in sel
    assert 'name=part value="OMNARIS"' in h                   # period control keeps the filter
    assert re.search(r'data-filter-day="[\d-]+"', h)
    assert re.search(r'href="/line/AreaB\?g=day&amp;p=[\d-]+&amp;part=OMNARIS"', h)   # day links keep it
    nav = h[h.index("<nav>"):h.index("</nav>")]
    assert "part=" not in nav                                 # other lines open unfiltered


def test_line_filter_hidden_in_export_mode(app_client):
    h = app_client.get("/line/AreaB?g=ytd&p=2026&_export=1").get_data(as_text=True)
    assert "<select id=part" not in h


def test_ict_tolerance_comes_from_config(replayed_cache, tmp_path):
    from webapp import create_app

    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"cache_path: {replayed_cache.as_posix()}\ncharts:\n  ict_change_tolerance: 0.05\n", encoding="utf-8")
    h = create_app(cfg).test_client().get("/product/OTRIVIN?g=ytd&p=2026").get_data(as_text=True)
    assert "class=changelab>reconfigured" not in h            # AreaB's step is 4%: inside a 5% band


# ---------------------------------------- gaps found by the mutation check

def test_a_stop_between_runs_belongs_to_neither(tmp_path):
    """On the recording one part run starts the moment the last ends, so no
    stop ever falls between two runs. Build a line where one does."""
    from xlpipe.cache import SQLiteCache
    from xlpipe.channels import SPECS

    store = SQLiteCache(tmp_path / "c.db")
    store.upsert_device({"device_id": "L1", "host": "x", "asset_name": None, "asset_uuid": None, "mac": None,
                         "model": None, "firmware": None, "data_version": None})
    t0 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    at = lambda h: (t0 + timedelta(hours=h)).isoformat()  # noqa: E731
    store.upsert(SPECS["part"], [
        {"device_id": "L1", "part_event_id": k, "sync_id": k, "record_order": k, "production_day_number": 20697,
         "start_time": at(a), "end_time": at(b), "part": p, "run_time": 3600.0, "unplanned_stop_time": 600.0,
         "planned_stop_time": 0.0, "ideal_time": 1800.0, "run_cycles": 100.0}
        for k, (a, b, p) in enumerate([(0, 2, "A"), (3, 5, "B")], start=1)])
    store.upsert(SPECS["process_state"], [
        {"device_id": "L1", "process_state_event_id": k, "sync_id": k, "record_order": k,
         "production_day_number": 20697, "start_time": at(h), "end_time": at(h + 1 / 6), "duration_s": 600.0,
         "process_state": "down", "performance_impact": "unplanned_stop"}
        for k, h in enumerate([1, 2.5, 4], start=1)])           # in A, between the runs, in B
    store.close()
    runs = D.part_runs(tmp_path / "c.db", "L1", 20697, 20697)
    assert [(r["part"], r["down_s"]) for r in runs] == [("A", 600.0), ("B", 600.0)]


def test_line_filter_reasons_are_the_products(app_client):
    """The chart reasons under the filter are OMNARIS's own: 202 Bottle Jam
    stops, 47.2 h, a fifth of its 239.1 clean down hours (independent SQL)."""
    h = app_client.get("/line/AreaB?g=ytd&p=2026&part=OMNARIS").get_data(as_text=True)
    svg = h[h.index("<svg class=pareto"):h.index("</svg>", h.index("<svg class=pareto"))]
    first = re.search(r"<title>([^<]+?) · ([\d.]+) h \((\d+)% of lost hours\) · ([\d,]+) stops", svg)
    assert first.groups() == ("Bottle Jam", "47.2", "20", "202")
    assert re.search(r"<title>Bottle Jam · 47.2 h lost · 202 stops", h)          # the quadrant too
