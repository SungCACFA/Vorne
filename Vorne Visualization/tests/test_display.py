"""What the pages say: the graduated performance flag, the duration clamp,
changeover rendering, and the line page's figures for one known day.

FR8 July 2026: 5 hours above 100% performance, 3 of them on runs under five
minutes (labelled as such) and 2 substantial ones (labelled as a wrong
ideal cycle time). 3 Jul 2026 holds three of the short ones and one
changeover hour.
"""

import re

import pytest

from webapp import create_app


@pytest.fixture(scope="module")
def macros(app_client):
    app = app_client.application
    with app.test_request_context():
        yield app.jinja_env.get_template("_macros.html").module, app.jinja_env.filters


def test_short_run_over_100_is_not_called_a_wrong_ict(macros):
    m, _ = macros
    out = str(m.perf(1.001, 60.0))
    label = re.search(r"<small>(.*?)</small>", out).group(1)        # the visible label, not the tooltip
    assert label == "over 100% on a 1-min run"
    assert "class=bad" in out          # still flagged, still red


def test_substantial_run_over_100_is_called_a_wrong_ict(macros):
    m, _ = macros
    out = str(m.perf(1.012, 3500.0))
    assert re.search(r"<small>(.*?)</small>", out).group(1) == "wrong ideal cycle time" and "class=bad" in out


def test_aggregate_over_100_without_run_time_is_called_a_wrong_ict(macros):
    m, _ = macros
    assert "wrong ideal cycle time" in str(m.perf(1.05))


def test_under_100_is_not_flagged(macros):
    m, _ = macros
    assert str(m.perf(0.776)) == "77.6%"


@pytest.mark.parametrize("filt,seconds,expected", [("hrs", -34.487024, "0.0"), ("mins", -34.487024, "0"),
                                                   ("h1", -9.46, "0.0"), ("hrs", 0.0, "0.0")])
def test_negative_durations_display_as_zero(macros, filt, seconds, expected):
    _, f = macros
    assert f[filt](seconds) == expected


def test_line_page_july_counts_the_two_kinds_of_flag(app_client):
    html = app_client.get("/line/FR8?g=month&p=2026-07").get_data(as_text=True)
    assert html.count("-min run</small>") == 3
    assert html.count("wrong ideal cycle time</small>") == 2
    assert "3 of these ran under five minutes" in html


def test_no_minus_zero_anywhere_on_the_july_line_page(app_client):
    html = app_client.get("/line/FR8?g=month&p=2026-07").get_data(as_text=True)
    assert not re.search(r">-0(\.0)?<", html)


def test_changeover_hour_is_marked(app_client):
    html = app_client.get("/line/FR8?g=day&p=2026-07-03").get_data(as_text=True)
    rows = re.findall(r'<tr data-day="2026-07-03" class="changeover">.*?</tr>', html, re.S)
    assert len(rows) == 1
    # each part named links to its product page, in the device's order
    assert ('<a class=lnk href="/product/PREMARINi_30G?g=day&amp;p=2026-07-03">PREMARINi_30G</a><span class=sep> / '
            '</span><a class=lnk href="/product/PREMARINi_4G?g=day&amp;p=2026-07-03">PREMARINi_4G</a>') in rows[0]
    assert "<small>changeover</small>" in rows[0]


def test_line_page_down_events_and_top_reason(app_client):
    """FR8 September: 240 unplanned stop events, 91.5 h; top reason
    'Waiting Components!' 18.7 h over 2 events (checked against SQL)."""
    html = app_client.get("/line/FR8?g=month&p=2026-09").get_data(as_text=True)
    glance = html[html.index("at a glance"):html.index("<h2>Hours by activity")]
    assert re.findall(r"<td>([^<]*)</td>", glance)[-2:] == ["91.5", "240"]
    why = html[html.index("<h2>Why it stopped"):html.index("<h2>Planned stops")]
    first = re.search(r"<tbody>\s*<tr><th>([^<]+)</th>.*?<td>([\d.,]+)</td><td>([\d,]+)</td>", why, re.S).groups()
    assert first == ("Waiting Components!", "18.7", "2")
