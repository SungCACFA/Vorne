"""Grouping production days into periods. Weeks start on Monday, as in the
old report. Production day n is date(1970,1,1) + n days."""

from datetime import date

import pytest

from webapp import data as D


def pdn(y, m, d):
    return (date(y, m, d) - date(1970, 1, 1)).days


@pytest.mark.parametrize("day,week", [
    (pdn(2026, 9, 28), "2026-09-28"),   # Monday
    (pdn(2026, 9, 30), "2026-09-28"),   # Wednesday
    (pdn(2026, 10, 3), "2026-09-28"),   # Saturday
    (pdn(2026, 10, 4), "2026-09-28"),   # Sunday belongs to the week before
    (pdn(2026, 10, 5), "2026-10-05"),   # next Monday
    (pdn(2026, 3, 1), "2026-02-23"),    # across a month boundary
])
def test_weeks_start_on_monday(day, week):
    assert D.period_key("week", day) == week


def test_production_day_number_to_date():
    assert D.production_day(20732) == date(2026, 10, 6)
    assert D.production_day(20545) == date(2026, 4, 2)


@pytest.mark.parametrize("gran,day,key,label", [
    ("day", pdn(2026, 7, 3), "2026-07-03", "Production day 3 Jul 2026"),
    ("week", pdn(2026, 9, 30), "2026-09-28", "Week of 28 Sep 2026"),
    ("month", pdn(2026, 9, 30), "2026-09", "September 2026"),
    ("ytd", pdn(2026, 9, 30), "2026", "2026 to date"),
])
def test_keys_and_labels(gran, day, key, label):
    assert D.period_key(gran, day) == key
    assert D.period_label(gran, key) == label


def test_a_week_holds_monday_to_sunday_only(replayed_cache):
    """The week of 28 Sep 2026 has production days Monday to Friday; the
    unscheduled weekend belongs to Friday's production day, so Saturday and
    Sunday have no records of their own."""
    from werkzeug.datastructures import MultiDict
    from webapp import select_window

    snap = D.load(replayed_cache)
    days = select_window(snap, MultiDict({"g": "week", "p": "2026-09-28"})).days()
    assert [D.production_day(d).isoformat() for d in days] == [
        "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]
    before = select_window(snap, MultiDict({"g": "week", "p": "2026-09-21"})).days()
    assert max(before) < pdn(2026, 9, 28)
