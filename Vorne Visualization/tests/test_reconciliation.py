"""Priority 3: time accounting, as WE compute it.

For a full hour, elapsed minus the four buckets (run, unplanned, planned,
not scheduled) should be ~0. The device's records reconcile; what needs
testing is that our generated columns (is_full_hour, acct_gap_s) and our
checks compute it correctly. Per the agreed rule: assert the median, report
the spread, flag |gap| > 180 s rather than fail, assert nothing on partial
records.
"""

import sqlite3
from datetime import datetime

import pytest

import verify
from conftest import HOST, recorded_events
from xlpipe.cache import SQLiteCache
from xlpipe.channels import SPECS
from xlpipe.rows import convert

SPEC = SPECS["shift_hour"]


def _record(key, start, end, run=0.0, unp=0.0, pln=0.0, nsc=0.0):
    return {"shift_hour_event_id": key, "sync_id": key, "record_order": key, "production_day_number": 20700,
            "start_time": start, "end_time": end, "run_time": run, "unplanned_stop_time": unp,
            "planned_stop_time": pln, "not_scheduled_time": nsc, "ideal_time": 0.0}


# Hand-written records with hand-computed expected values.
CASES = [
    # key, start, end, buckets, expected is_full_hour, expected acct_gap_s
    (1, "2026-09-01T13:00:00.000000-04:00", "2026-09-01T14:00:00.000000-04:00", (3000.0, 400.0, 200.0, 0.0), 1, 0.0),
    (2, "2026-09-01T14:00:00.000000-04:00", "2026-09-01T15:00:00.000000-04:00", (2826.5, 400.0, 200.0, 0.0), 1, 173.5),
    (3, "2026-09-01T15:00:00.000000-04:00", "2026-09-01T16:00:00.000000-04:00", (3000.0, 400.0, 300.0, 0.0), 1, -100.0),
    # partial: 47 min 22.970334 s, buckets add up
    (4, "2026-10-06T09:00:00.000000-04:00", "2026-10-06T09:47:22.970334-04:00", (2842.970334, 0.0, 0.0, 0.0), 0, 0.0),
    # the overnight not-scheduled block: 8 h, not a full hour, reconciles
    (5, "2026-10-01T23:00:00.000000-04:00", "2026-10-02T07:00:00.000000-04:00", (0.0, 0.0, 0.0, 28800.0), 0, 0.0),
    # a full hour across a UTC offset written differently
    (6, "2026-09-01T17:00:00+00:00", "2026-09-01T14:00:00.000000-04:00", (3600.0, 0.0, 0.0, 0.0), 1, 0.0),
]


@pytest.fixture
def store(tmp_path):
    s = SQLiteCache(tmp_path / "c.db")
    yield s
    s.close()


def _load(store, records):
    rows = []
    for r in records:
        row, problems = convert(SPEC, "FR8", r)
        assert not problems
        rows.append(row)
    store.upsert(SPEC, rows)


@pytest.mark.parametrize("key,start,end,buckets,full,gap", CASES)
def test_generated_columns_on_hand_written_records(store, key, start, end, buckets, full, gap):
    _load(store, [_record(key, start, end, *buckets)])
    got = store.db.execute("SELECT is_full_hour, acct_gap_s FROM hour_record WHERE shift_hour_event_id=?",
                           (key,)).fetchone()
    assert got[0] == full
    assert got[1] == pytest.approx(gap, abs=1e-3)


def test_missing_bucket_gives_null_gap_not_a_fake_number(store):
    rec = _record(9, "2026-09-01T13:00:00-04:00", "2026-09-01T14:00:00-04:00", 3600.0)
    rec["not_scheduled_time"] = None
    _load(store, [rec])
    assert store.db.execute("SELECT acct_gap_s FROM hour_record WHERE shift_hour_event_id=9").fetchone()[0] is None


# ----------------------------------------------------- our checks on our data

def _accounting_result(db_path, dev):
    verify.results.clear()
    verify.check_accounting(sqlite3.connect(db_path), dev)
    return verify.results[-1][0]


@pytest.mark.parametrize("dev", sorted(HOST))
def test_verify_accounting_passes_on_the_recording(replayed_cache, dev):
    """Our check, over our generated column, on the real data: median near
    zero and nothing beyond 180 s (a fact about this recording; if a
    re-recording breaks it, look at those hours before loosening it)."""
    assert _accounting_result(replayed_cache, dev) == verify.OK


def test_verify_flags_a_full_hour_beyond_180_s(store, tmp_path):
    _load(store, [_record(k, f"2026-09-01T{k:02d}:00:00+00:00", f"2026-09-01T{k + 1:02d}:00:00+00:00", 3600.0)
                  for k in range(1, 6)])
    _load(store, [_record(7, "2026-09-02T01:00:00+00:00", "2026-09-02T02:00:00+00:00", 3400.0)])   # 200 s gap
    assert _accounting_result(tmp_path / "c.db", "FR8") == verify.FLAG


def test_verify_fails_when_the_median_is_off(store, tmp_path):
    _load(store, [_record(k, f"2026-09-01T{k:02d}:00:00+00:00", f"2026-09-01T{k + 1:02d}:00:00+00:00", 3590.0)
                  for k in range(1, 6)])                                                         # 10 s every hour
    assert _accounting_result(tmp_path / "c.db", "FR8") == verify.FAIL


@pytest.mark.parametrize("dev", sorted(HOST))
def test_cached_gap_matches_an_independent_calculation(replayed_cache, dev):
    """Row for row against the raw records, read by an independent parser
    (conftest.recorded_events shares no code with xlpipe)."""
    raw = {}
    for e in recorded_events(HOST[dev], "shift_hour"):
        elapsed = (datetime.fromisoformat(e["end_time"]) - datetime.fromisoformat(e["start_time"])).total_seconds()
        raw[e["shift_hour_event_id"]] = (elapsed, elapsed - (e["run_time"] + e["unplanned_stop_time"]
                                                             + e["planned_stop_time"] + e["not_scheduled_time"]))
    rows = sqlite3.connect(replayed_cache).execute(
        "SELECT shift_hour_event_id, is_full_hour, acct_gap_s FROM hour_record WHERE device_id=?", (dev,)).fetchall()
    assert len(rows) == len(raw)
    for key, full, gap in rows:
        elapsed, expected = raw[key]
        assert bool(full) == (abs(elapsed - 3600) < 1e-3), key
        assert gap == pytest.approx(expected, abs=2e-3), key   # unixepoch('subsec') is millisecond-precise
