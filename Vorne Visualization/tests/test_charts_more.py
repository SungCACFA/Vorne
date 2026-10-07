"""Pareto, run/stop rhythm, changeover matrix and the implausible-duration
flag. Literals were checked against the cache with direct SQL where the code
under test could otherwise confirm itself (e.g. 19 stops over 24 h, fleet).

Key facts:
  * Default rule (24 h; or 12 h and 10x the line's median for that state)
    flags 42 events: FR8 16, AreaB 8, AreaD 10, AreaE 8. AreaB's include the
    260.4 h "maintenance" from 13 Jul and a 56.3 h "down" (Pick & Place 2).
  * Leaving them out of the charts: Pick & Place 2 is 55 stops x 8 min (it
    looked like 56 x 68 min); the AreaB week of 11 May has a mean stop of
    3.8 min, not 260.
  * AreaB changeovers, year to date: 48 changes, 7 between different parts;
    222.0 h charged, 301.0 h of implausible planned stop left out - exactly
    AreaB's flagged planned-stop hours, so nothing is counted twice.
"""

import re
import sqlite3

import pytest

from conftest import client_for
from webapp import charts as C
from webapp import data as D


@pytest.fixture(scope="module")
def flags(replayed_cache):
    return D.implausible(replayed_cache)


# ------------------------------------------------------------- implausible

def test_default_rule_flags_42_events(flags):
    by = {d: sum(r["device_id"] == d for r in flags) for d in ("FR8", "AreaB", "AreaD", "AreaE")}
    assert by == {"FR8": 16, "AreaB": 8, "AreaD": 10, "AreaE": 8}


def test_the_july_maintenance_and_the_56h_down_are_flagged(flags):
    b = [r for r in flags if r["device_id"] == "AreaB"]
    assert any(r["state"] == "maintenance" and round(r["seconds"] / 3600, 1) == 260.4
               and r["start"].date().isoformat() == "2026-07-13" for r in b)
    assert any(r["state"] == "down" and round(r["seconds"] / 3600, 1) == 56.3
               and r["reason"] == "Pick & Place 2" for r in b)


def test_every_stop_over_24h_is_flagged(replayed_cache, flags):
    """Cross-check against SQL that does not use the rule's code."""
    n = sqlite3.connect(replayed_cache).execute(
        "SELECT count(*) FROM state_event s JOIN device_config_epoch e USING (device_id, config_epoch) "
        "WHERE e.usable = 1 AND deleted_at IS NULL AND performance_impact IN ('planned_stop', 'unplanned_stop') "
        "AND duration_s > 86400").fetchone()[0]
    assert n == 19 == sum(r["seconds"] > 86400 for r in flags)


def test_rule_comes_from_config(replayed_cache, tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"cache_path: {replayed_cache.as_posix()}\ncharts:\n  implausible_cap_hours: 1000\n"
                   "  implausible_floor_hours: 1000\n", encoding="utf-8")
    from webapp import create_app

    html = create_app(cfg).test_client().get("/line/AreaB?g=month&p=2026-07").get_data(as_text=True)
    assert "far too long" not in html


def test_july_panel_says_what_the_events_do(app_client):
    html = app_client.get("/line/AreaB?g=month&p=2026-07").get_data(as_text=True)
    panel = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html[html.index("far too long") - 60:html.index("<h2>Hour by hour")]))
    assert " 3 state events in this period are far too long" in panel
    assert "260 h, 117 times the usual 2.2 h" in panel
    assert "28.8 h, 579 times the usual 3 min" in panel           # sub-hour medians read in minutes
    assert "92.1% of this period's" in panel
    assert "lowers availability and inflates calendar use" in panel
    assert "calendar use would be 6.1% rather than 47.7%" in panel
    assert "Nothing on this page has been changed for them" in panel


def test_charts_leave_flagged_events_out_and_say_so(app_client):
    html = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    notes = re.findall(r'<p class="note leftout">(.*?)</p>', html, re.S)
    assert len(notes) == 3                                          # quadrant, Pareto, rhythm
    assert all("6 implausible unplanned stops (173 h)" in re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", n))
               for n in notes)                                       # "implausible" is a definition link
    # ...while the table beneath keeps them: Pick & Place 2 is still 63.5 h there
    why = html[html.index("<h2>Why it stopped"):html.index("<h2>Planned stops")]
    assert re.search(r"<th>Pick &amp; Place 2</th>.*?<td>63.5</td><td>56</td>", why, re.S)


# ------------------------------------------------------------------ Pareto

def test_pareto_eighty_percent(app_client):
    h = app_client.get("/line/AreaD?g=month&p=2026-09").get_data(as_text=True)
    assert ">3 reasons = 84% of lost hours<" in h
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    assert ">80% of lost hours takes</text>" in h and ">17 of 53 reasons:</text>" in h   # stacked label


def test_pareto_has_one_axis():
    rs = [{"reason": f"r{i}", "events": 1, "seconds": float(100 - i)} for i in range(5)]
    svg = str(C.pareto(rs))
    ticks = re.findall(r'class="tick mid">(\d+%)</text>', svg)
    assert ticks == ["0%", "20%", "40%", "60%", "80%", "100%"]       # bars and running total share it
    pts = [float(x.split(",")[0]) for x in re.search(r'class=cum points="([^"]+)"', svg).group(1).split()]
    assert pts == sorted(pts)                                         # running total only rises


def test_pareto_running_total_is_over_all_reasons_not_the_shown_ones():
    rs = [{"reason": f"r{i}", "events": 1, "seconds": 10.0} for i in range(20)]   # 15 shown + 5 grouped
    svg = str(C.pareto(rs, top=15))
    assert "All other 5 reasons" in svg
    assert "running total 75%" in svg and "running total 100%" in svg


# ------------------------------------------------------------------ rhythm

def test_rhythm_drops_a_week_set_by_one_stop(replayed_cache, flags):
    ids = {r["event_id"] for r in flags if r["device_id"] == "AreaB"}
    raw = {w["week"]: w for w in D.weekly_rhythm(replayed_cache, "AreaB", 20545, 20732)}
    clean = {w["week"]: w for w in D.weekly_rhythm(replayed_cache, "AreaB", 20545, 20732, ids)}
    assert len(raw) == 23
    assert round(raw["2026-05-11"]["stop_min"]) == 260
    assert round(clean["2026-05-11"]["stop_min"], 1) == 3.8


def test_rhythm_short_period_shows_twelve_weeks(app_client):
    h = app_client.get("/line/FR8?g=week&p=2026-09-28").get_data(as_text=True)
    assert "This period is short, so the chart shows the 12 weeks to" in h
    assert "<svg class=rhythm" in h


def test_rhythm_end_labels_never_stack():
    weeks = [{"week": f"2026-09-{d:02d}", "run_bursts": 1, "stops": 1, "run_min": 20.0, "stop_min": 19.0}
             for d in (7, 14, 21)]
    svg = str(C.rhythm(weeks))
    assert "Mean run burst <tspan" not in svg                        # the legend carries them instead


# ------------------------------------------------------------ changeovers

def test_changeovers_charge_each_stop_once(replayed_cache, flags):
    ids = {r["event_id"] for r in flags if r["device_id"] == "AreaB"}
    ch = D.changeovers(replayed_cache, "AreaB", 20545, 20732, ids)
    assert len(ch) == 48 and sum(not c["same_part"] for c in ch) == 7
    assert round(sum(c["seconds"] for c in ch) / 3600, 1) == 222.0
    flagged_planned = sum(r["seconds"] for r in flags if r["device_id"] == "AreaB" and r["impact"] == "planned_stop")
    assert sum(c["excluded_seconds"] for c in ch) == pytest.approx(flagged_planned)   # 301.0 h, not more


def test_changeover_matrix_cells(app_client):
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    m = h[h.index("<table class=comatrix>"):h.index("</table>", h.index("<table class=comatrix>"))]
    assert re.search(r'title="OTRIVIN to DICLOFENAC · 1 change · mean 26.5 h[^"]*">1<small>26.5 h</small>', m)
    assert re.search(r'title="OMNARIS to OMNARIS \(same part, new job\) · 18 changes · mean 4.5 h', m)
    assert "same part, new job" in m and "class=diag" in m


@pytest.mark.parametrize("hours,b", [(0.5, 0), (1.0, 1), (3.9, 2), (4.0, 3), (7.99, 3), (8.0, 4), (26.5, 4)])
def test_changeover_bins_are_fixed(hours, b):
    assert C.co_bin(hours) == b
    assert C.CO_RAMP == ("#D5A441", "#C98A0B", "#966507", "#704A03", "#4A2E00")


def test_text_on_dark_cells_is_white():
    assert C.ink_for("#4A2E00") == "#FFFFFF" and C.ink_for("#D5A441") == C.INK


# ---------------------------------------- gaps found by the mutation check

def test_the_24h_cap_flags_even_when_the_median_is_long(tmp_path):
    """On the recording every stop over 24 h also clears "12 h and 10x the
    median", so the cap was untested. A state whose usual length is long
    (median 20 h) only gets caught by the cap."""
    from datetime import datetime, timedelta, timezone

    from xlpipe.cache import SQLiteCache
    from xlpipe.channels import SPECS

    store = SQLiteCache(tmp_path / "c.db")
    store.upsert_device({"device_id": "L1", "host": "x", "asset_name": None, "asset_uuid": None, "mac": None,
                         "model": None, "firmware": None, "data_version": None})
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = []
    for k, hours in enumerate([20, 20, 20, 30], start=1):     # median 20 h; one event of 30 h
        st = t0 + timedelta(days=3 * k)
        rows.append({"device_id": "L1", "process_state_event_id": k, "sync_id": k, "record_order": k,
                     "production_day_number": 20700 + k, "start_time": st.isoformat(),
                     "end_time": (st + timedelta(hours=hours)).isoformat(), "duration_s": hours * 3600.0,
                     "process_state": "maintenance", "performance_impact": "planned_stop"})
    store.upsert(SPECS["process_state"], rows)
    store.close()
    flagged = D.implausible(tmp_path / "c.db")
    assert [r["event_id"] for r in flagged] == [4]            # 30 h > 24 h, though only 1.5x the median


def test_rhythm_on_the_page_leaves_implausible_stops_out(app_client):
    """The data function excluding them is not enough: the page must pass them."""
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    assert re.search(r"<title>Week of 11 May 2026\nMean run burst \d+ min \([^)]*\) · [\d,]+ run bursts\n"
                     r"Mean unplanned stop 4 min \([+-]\d+ min on the week before\) · 2 stops</title>", h)


def test_july_panel_adjusts_availability_too(app_client):
    html = app_client.get("/line/AreaB?g=month&p=2026-07").get_data(as_text=True)
    panel = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html[html.index("far too long"):html.index("<h2>Hour by hour")]))
    assert "availability over all records 10.1% rather than 1.3%" in panel


# ------------------------------------- the table marks what the charts leave out

def flag_notes(html):
    why = html[html.index("<h2>Why it stopped"):html.index("<h2>Planned stops")]
    notes = re.findall(r"<tr class=flagrow[^>]*><td colspan=6>(.*?)</td></tr>", why, re.S)
    return why, [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", n)).replace("&amp;", "&") for n in notes]


def test_a_flagged_row_says_why_it_ranks_differently(app_client):
    """Pick & Place 2: the table's 63.5 h over 56 stops includes one 56.3 h "down"; the charts show
    7.3 h over 55 (test_charts pins the quadrant's 7.3 h / 55 independently)."""
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    why, notes = flag_notes(h)
    assert re.search(r"<th>Pick &amp; Place 2</th>.*?<td>63.5</td><td>56</td>", why, re.S)    # the complete record
    assert ("Includes 1 implausible stop (56.3 h), left out of the charts above: there, Pick & Place 2 is "
            "7.3 h over 55 stops.") in notes
    row = why[why.index("<th>Pick &amp; Place 2</th>"):]
    assert row.index("<tr class=flagrow") == row.index("<tr")            # the note sits directly beneath its row


def test_every_flagged_stop_is_noted_once(app_client):
    """Six implausible unplanned stops, 173 h - the same the charts' left-out note counts."""
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    _, notes = flag_notes(h)
    hours = [float(re.search(r"\(([\d.]+) h\)", n).group(1)) for n in notes]
    assert len(notes) == 6 and abs(sum(hours) - 173.6) < 0.05           # 56.3 + 28.8 + 21.2 + 30.2 + 12.9 + 24.2
    assert "6 implausible unplanned stops (173 h)" in re.sub(r"\s+", " ", h)


def test_the_note_lights_up_with_its_reason(app_client):
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    assert '<tr class=flagrow data-hl="unp:Pick &amp; Place 2"><td colspan=6>' in h


def test_no_note_without_a_flagged_stop(app_client):
    h = app_client.get("/line/FR8?g=day&p=2026-07-03").get_data(as_text=True)
    assert "<tr class=flagrow" not in h
