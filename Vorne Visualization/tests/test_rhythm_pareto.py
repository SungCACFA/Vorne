"""The rhythm change-point, the Pareto verdict, and the availability
convention note.

Literals, checked against the cache: AreaB's mean run burst steps from 59 to
23 minutes from the week of 11 May 2026 (-60%, Welch t 6.5). AreaD (-40%,
t 2.6), AreaE (t 1.8) and FR8 (t 1.4) do not clear the bar. AreaB needs 17 of
53 reasons for 80% of its lost hours; AreaD in September 2026, 3.
"""

import re

import pytest

from webapp import charts as C


def weeks(values):
    return [{"week": f"2026-{1 + i // 4:02d}-{1 + 7 * (i % 4):02d}", "run_bursts": 10, "stops": 10,
             "run_min": v, "stop_min": 10.0} for i, v in enumerate(values)]


# --------------------------------------------------------- change point

def test_a_clear_step_is_found_at_the_right_week():
    w = weeks([60, 62, 58, 61, 59, 25, 23, 24, 26, 22])
    c = C.change_point(w)
    assert c["index"] == 5
    assert round(c["before"]) == 60 and round(c["after"]) == 24
    assert c["shift"] < -0.5 and c["t"] > 10


def test_noise_is_not_marked():
    assert C.change_point(weeks([30, 34, 27, 31, 29, 33, 28, 32, 30, 31])) is None


def test_a_small_step_is_not_marked_however_clean():
    assert C.change_point(weeks([30, 30, 30, 30, 26, 26, 26, 26])) is None      # -13%: under 25%


def test_too_few_weeks_either_side():
    assert C.change_point(weeks([60, 60, 20, 20])) is None


@pytest.mark.parametrize("dev,marked", [("AreaB", True), ("AreaD", False), ("AreaE", False), ("FR8", False)])
def test_only_areab_is_marked_on_the_recording(app_client, dev, marked):
    h = app_client.get(f"/line/{dev}?g=ytd&p=2026").get_data(as_text=True)
    assert ("class=changelab>" in h) == marked


def test_areab_change_point_values(app_client):
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    assert "class=changelab>From the week of 11 May, run bursts shortened from 59 to 23 min (-60%)</text>" in h
    assert "while stop length showed no step of its own" in h


def test_rhythm_sits_straight_after_the_heatmap(app_client):
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    order = [h.index(s) for s in ("<h2>Hour by hour</h2>", "<h2>Run and stop rhythm", "<h2>Hours by activity",
                                  "<h2>Where the lost hours go")]
    assert order == sorted(order)


def test_change_thresholds_come_from_config(replayed_cache, tmp_path):
    from webapp import create_app

    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"cache_path: {replayed_cache.as_posix()}\ncharts:\n  rhythm_change_min_t: 7\n", encoding="utf-8")
    h = create_app(cfg).test_client().get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    assert "class=changelab>" not in h          # AreaB's t is 6.5: under a bar of 7


# --------------------------------------------------------- Pareto verdict

def test_areab_has_no_short_list(app_client):
    h = app_client.get("/line/AreaB?g=ytd&p=2026").get_data(as_text=True)
    assert "No short list: 80% of the lost hours takes\n17 of 53 reasons." in h
    assert ">no small set of fixes</text>" in h


def test_aread_september_has_a_short_list(app_client):
    h = app_client.get("/line/AreaD?g=month&p=2026-09").get_data(as_text=True)
    assert "A short list:\n3 of" in h
    assert "class=verdict>a short list: fixing these recovers most of it<" in h


@pytest.mark.parametrize("need_of,short", [((3, 40), True), ((5, 40), True), ((6, 20), False), ((6, 40), True),
                                           ((17, 53), False)])
def test_short_list_rule(need_of, short):
    need, n = need_of
    # n reasons where the first `need` hold just over 80% between them
    big = 80.5 / need
    small = 19.5 / (n - need)
    rs = [{"reason": f"r{i}", "events": 1, "seconds": big if i < need else small} for i in range(n)]
    s = C.pareto_summary(rs)
    assert s["need"] == need and s["short_list"] is short


# ------------------------------------------------- availability convention

def test_convention_is_named_on_the_panel_and_in_the_footer(app_client):
    h = app_client.get("/line/AreaB?g=month&p=2026-07").get_data(as_text=True)
    panel = re.sub(r"\s+", " ", h[h.index("far too long"):h.index("<h2>Hour by hour")])
    assert "Which availability convention." in panel
    assert "The common OEE convention leaves planned stops out" in panel
    foot = h[h.index("<footer>"):]
    assert "unlike the common\nOEE convention" in foot
