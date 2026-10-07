"""Line-page charts: the hour-by-hour heatmap and the stop-reason quadrant.

Facts pinned here were checked by hand against the cache:
  * The device reports an interrupted state as one event spanning the
    interruption; its duration already excludes it. "Latest-started event
    owns the time" reproduces the device's duration for 13,855 of 13,858
    events (the 3 exceptions are the FR8 clock-correction events).
  * AreaB has one 232 h not-scheduled record from Fri 11 Sep 15:00 to
    Mon 21 Sep 07:00, filed under 11 Sep: 12-20 Sep have no hour records of
    their own but must still appear as rows.
  * AreaB year to date: Bottle Jam 260 stops x 12.3 min (a chronic nuisance),
    Pick & Place 2 56 stops x 68.1 min (a major breakdown), over 127
    production days with records.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

from webapp import charts as C
from webapp import data as D

T0 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def ev(start_min, end_min, impact, state="s"):
    return (T0 + timedelta(minutes=start_min), T0 + timedelta(minutes=end_min), impact, state, None)


# --------------------------------------------------------------- the rule

def test_interrupting_event_owns_its_time():
    """A 60-min 'down' interrupted by a 20-min 'break' in the middle: the
    down owns 40 min, the break 20 - as the device's own durations say."""
    events = [ev(0, 60, "unplanned_stop", "down"), ev(20, 40, "not_scheduled", "break")]
    secs, what = D.paint(events, T0, T0 + timedelta(minutes=60))
    assert secs["unplanned_stop"] == 40 * 60
    assert secs["not_scheduled"] == 20 * 60
    assert sum(secs.values()) == 3600


def test_nested_interruptions_and_gaps():
    events = [ev(0, 50, "planned_stop", "changeover"), ev(10, 30, "not_scheduled", "no_production"),
              ev(15, 20, "run", "running")]
    secs, _ = D.paint(events, T0, T0 + timedelta(minutes=60))
    assert secs == {"run": 300.0, "not_scheduled": 900.0, "planned_stop": 1800.0, "unplanned_stop": 0.0}
    # minutes 50-60 have no state: not counted, not invented


def test_slice_boundaries_split_an_event():
    secs, _ = D.paint([ev(-30, 30, "run")], T0, T0 + timedelta(minutes=60))
    assert secs["run"] == 1800


# ------------------------------------------------------------- heatmap

def test_every_day_covers_24_hours_on_the_recording(replayed_cache):
    days = list(range(20700, 20710))
    slots = D.hour_slots(replayed_cache, "FR8", days)
    for n in days:
        covered = sum(c["covered"] for c in slots[n])
        assert covered == pytest.approx(86400, abs=60), D.production_day(n)
        assert all(c["covered"] <= 3600 + 1 for c in slots[n])


def test_three_july_reads_as_the_hour_table_does(replayed_cache):
    """FR8 3 Jul 2026: planned stops overnight, a long planned stop from
    09:00, running from 18:00 to the end of the day."""
    cells = D.hour_slots(replayed_cache, "FR8", [20637])[20637]
    dom = [c["dominant"] for c in cells]        # cell 0 is 23:00 on 2 Jul
    assert dom[0] == "planned_stop"
    assert dom[10:17] == ["planned_stop"] * 7    # 09:00-16:00
    assert dom[19:] == ["run"] * 5               # 18:00-23:00


def test_idle_stretch_without_records_still_gets_rows(app_client):
    html = app_client.get("/line/AreaB?g=month&p=2026-09").get_data(as_text=True)
    heat = html[html.index('<svg class=heat'):html.index('</svg>', html.index('<svg class=heat'))]
    assert heat.count("class=heatrow") == 30
    for d in range(12, 21):
        row = re.search(rf'data-filter-day="2026-09-{d:02d}" class=heatrow>(.*?)</a>', heat, re.S).group(1)
        assert row.count('fill="var(--nsc)"') == 24, d       # idle all day: every hour not scheduled


def test_heatmap_rows_link_to_their_day(app_client):
    html = app_client.get("/line/AreaB?g=month&p=2026-09").get_data(as_text=True)
    assert 'href="/line/AreaB?g=day&amp;p=2026-09-07" data-filter-day="2026-09-07"' in html
    assert 'data-day="2026-09-07"' in html            # the table rows the click filters


# ------------------------------------------------------------- quadrant

@pytest.mark.parametrize("events,mean_min,zone", [
    (260, 12.3, "Chronic nuisance"), (56, 68.1, "Major breakdown"),
    (300, 45.0, "Critical"), (5, 4.0, "Minor"),
])
def test_zones_follow_the_per_day_and_30_minute_lines(events, mean_min, zone):
    assert C.zone_of(events, mean_min * 60, 127, 30 * 60)[0] == zone


def test_thresholds_come_from_config(replayed_cache, tmp_path):
    """One place to set them: config.yaml charts:. Bulk Replenishment (10
    stops, 94 min) is a major breakdown at the default 30-minute line and
    minor at a 120-minute one."""
    from webapp import create_app

    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"cache_path: {replayed_cache.as_posix()}\n"
                   "charts:\n  frequent_stops_per_production_day: 2\n  long_stop_minutes: 120\n", encoding="utf-8")
    html = create_app(cfg).test_client().get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    assert "254 stops: 2 per production day with records" in html
    assert ">120 min per stop<" in html
    assert re.search(r"<title>Bulk Replenishment · [\d.]+ h lost · 10 stops · 94 min each · Minor\n", html)


def test_default_thresholds_are_one_per_day_and_30_minutes():
    assert (C.FREQUENT_STOPS_PER_DAY, C.LONG_STOP_MINUTES) == (1.0, 30.0)


def test_quadrant_on_the_recording(app_client):
    html = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    quad = html[html.index('<svg class=quad'):html.index('</svg>', html.index('<svg class=quad'))]
    assert re.search(r"<title>Bottle Jam · [\d.]+ h lost · 260 stops · 12 min each · Chronic nuisance\n", quad)
    # Pick & Place 2 is 55 stops of 8 min once its one 56 h "down" (flagged implausible) is left out.
    assert re.search(r"<title>Pick &amp; Place 2 · 7.3 h lost · 55 stops · 8 min each · Minor\n", quad)
    assert re.search(r"<title>Bulk Replenishment · [\d.]+ h lost · 10 stops · 94 min each · Major breakdown\n", quad)
    assert "127 stops: 1 per production day with records" in quad
    for name in ("Critical", "Chronic nuisance", "Major breakdown", "Minor"):
        assert f">{name}</text>" in quad


def test_every_reason_is_a_bubble_with_a_tooltip(app_client):
    html = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    quad = html[html.index('<svg class=quad'):html.index('</svg>', html.index('<svg class=quad'))]
    why = html[html.index("<h2>Why it stopped"):html.index("<h2>Planned stops")]
    body = why[why.index("<tbody>"):]                       # not the header row
    n_reasons = len(re.findall(r"<tr><th>[^<]+</th><td class=bar>", body))
    more = re.search(r"(\d+) more reasons?</th>", why)
    total = n_reasons + (int(more.group(1)) if more else 0)
    assert quad.count("class=bubble") == total


def test_direct_labels_never_overlap(app_client):
    """Labels are placed only where they collide with nothing; check the
    boxes the layout reserved never intersect."""
    html = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    labels = re.findall(r'<text x="([\d.]+)" y="([\d.]+)" class="lab (start|end|mid)">(.*?)</text>', html)
    boxes = []
    for x, y, anchor, body in labels:
        text = re.sub(r"<[^>]+>", "", body)
        w = 6.4 * len(text)
        x0 = float(x) if anchor == "start" else float(x) - w if anchor == "end" else float(x) - w / 2
        boxes.append((x0, float(y) - 10, x0 + w, float(y) + 3))
    assert len(boxes) >= 3
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1], (a, b)


def test_too_few_reasons_says_so():
    out = str(C.quadrant([{"reason": "x", "events": 1, "seconds": 60.0}], 5))
    assert "Too few stop reasons" in out
