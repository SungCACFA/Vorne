"""Configuration epochs: rows from an era with a mis-set device clock are
kept (flagged), and are actually absent from every aggregate - not just
moved behind a later default date.

Literals derived independently from the raw fixtures: FR8 has 70 hour
records before its clock moved to Eastern (2026-02-23 14:00 UTC); the last
production day they touch is 20507, which holds 23 records in all, some
from after the fix.
"""

import sqlite3

import pytest
from werkzeug.datastructures import MultiDict

from webapp import data as D
from webapp import select_window, totals

FR8_EPOCH1_ROWS = 70
FR8_TAINTED_DAY = 20507
FR8_TAINTED_DAY_ROWS = 23
FR8_FEB_USABLE_ROWS = 97          # production days 20508..20512 (24-28 Feb)


@pytest.fixture(scope="module")
def snap(replayed_cache):
    return D.load(replayed_cache)


def test_epoch1_rows_are_kept_and_flagged_not_deleted(replayed_cache):
    db = sqlite3.connect(replayed_cache)
    n, deleted = db.execute("SELECT count(*), sum(deleted_at IS NOT NULL) FROM hour_record "
                            "WHERE device_id='FR8' AND config_epoch=1").fetchone()
    assert (n, deleted) == (FR8_EPOCH1_ROWS, 0)


def test_tainted_day_is_dropped_whole(replayed_cache, snap):
    """A production day touched by an unusable era is excluded entirely,
    including its rows from after the fix."""
    db = sqlite3.connect(replayed_cache)
    assert db.execute("SELECT count(*) FROM hour_record WHERE device_id='FR8' AND production_day_number=?",
                      (FR8_TAINTED_DAY,)).fetchone()[0] == FR8_TAINTED_DAY_ROWS
    assert ("FR8", FR8_TAINTED_DAY) not in snap.daily
    assert not [day for (dev, day) in snap.daily if dev == "FR8" and day <= FR8_TAINTED_DAY]
    assert snap.history["FR8"].first_usable_day == FR8_TAINTED_DAY + 1


@pytest.mark.parametrize("dev", ["AreaB", "AreaD", "AreaE", "FR8"])
def test_no_aggregate_day_holds_an_unusable_row(replayed_cache, snap, dev):
    db = sqlite3.connect(replayed_cache)
    bad_days = {r[0] for r in db.execute(
        "SELECT DISTINCT production_day_number FROM hour_record h JOIN device_config_epoch e "
        "USING (device_id, config_epoch) WHERE h.device_id=? AND e.usable=0", (dev,))}
    assert bad_days, "every device has an unusable first era in this recording"
    assert not bad_days & {day for (d, day) in snap.daily if d == dev}


def test_widened_february_counts_only_usable_rows(snap):
    """Widening FR8 shows its own earlier usable days - and still not the
    excluded era. February: only the 97 records after the tainted day."""
    win = select_window(snap, MultiDict({"g": "month", "p": "2026-02", "widen": "FR8"}))
    t, _ = totals(win, "FR8")
    assert t.records == FR8_FEB_USABLE_ROWS


def test_february_is_not_offered_without_widening(snap):
    win = select_window(snap, MultiDict({"g": "month"}))
    assert "2026-02" not in [k for k, _ in win.periods]


def test_default_window_starts_at_the_fleet_window(snap):
    win = select_window(snap, MultiDict({"g": "day"}))
    first = min(day for (_, day) in win.included)
    assert D.production_day(first).isoformat() == "2026-04-02"
    assert D.production_day(snap.fleet_from_day).isoformat() == "2026-04-02"
