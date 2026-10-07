"""Priority 6: open-run detection.

A run is flagged when open more than 7 days, or more than 3x that part's
median closed run on that device - the median over runs of more than 200
cycles only, because lines record short fragments that would drag it to
hours (FR8 ADVIL: 22 runs, median 0.09 days with fragments, 1.01 without).
"""

from datetime import datetime, timedelta, timezone

import pytest

from webapp import data as D
from xlpipe.cache import SQLiteCache
from xlpipe.channels import SPECS

NOW = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)
SPEC = SPECS["part"]


@pytest.fixture
def cache(tmp_path):
    path = tmp_path / "cache.db"
    store = SQLiteCache(path)
    store.upsert_device({"device_id": "L1", "host": "x", "asset_name": None, "asset_uuid": None, "mac": None,
                         "model": None, "firmware": None, "data_version": None})
    yield path, store
    store.close()


def _run(key, start, days, cycles, part="P"):
    st = NOW - timedelta(days=start)
    return {"device_id": "L1", "part_event_id": key, "sync_id": key, "record_order": key, "production_day_number": 1,
            "start_time": st.isoformat(), "end_time": (st + timedelta(days=days)).isoformat(), "part": part,
            "job": "J", "run_time": 100.0, "run_cycles": float(cycles), "ideal_time": 50.0}


def _open(store, days_open, part="P"):
    store.record_open_event(SPEC, "L1", {"part_event_id": 999, "part": part, "job": "J",
                                         "start_time": (NOW - timedelta(days=days_open)).isoformat(),
                                         "production_day_number": 1, "run_time": 3600.0})


def flagged(path):
    return [r for r in D.open_runs(path, now=NOW) if r.reasons]


def test_open_over_seven_days_is_flagged(cache):
    path, store = cache
    _open(store, 8)
    (r,) = flagged(path)
    assert "7-day limit" in r.reasons[0]


def test_open_under_seven_days_with_no_history_is_fine(cache):
    path, store = cache
    _open(store, 6)
    assert flagged(path) == []


def test_over_three_times_the_median_is_flagged(cache):
    path, store = cache
    store.upsert(SPEC, [_run(k, 30 + k, 1.0, 5000) for k in range(1, 6)])     # median 1 day
    _open(store, 3.5)
    (r,) = flagged(path)
    assert r.median_seconds == pytest.approx(86400)
    assert "3.5x" in r.reasons[0]


def test_under_three_times_the_median_is_fine(cache):
    path, store = cache
    store.upsert(SPEC, [_run(k, 30 + k, 2.0, 5000) for k in range(1, 6)])     # median 2 days
    _open(store, 5)
    assert flagged(path) == []


def test_short_fragments_do_not_drag_the_median_down(cache):
    """The FR8 ADVIL case: without the 200-cycle filter, ten fragments make
    the median an hour and a normal 2-day run reads as dozens of times over."""
    path, store = cache
    fragments = [_run(k, 40 + k, 0.04, 50) for k in range(1, 11)]
    real = [_run(k, 60 + k, 1.0, 5000) for k in range(11, 16)]
    store.upsert(SPEC, fragments + real)
    _open(store, 2)
    assert flagged(path) == []
    (r,) = D.open_runs(path, now=NOW)
    assert r.median_runs == 5 and r.median_seconds == pytest.approx(86400)


def test_too_few_substantial_runs_disables_the_median_rule(cache):
    path, store = cache
    store.upsert(SPEC, [_run(k, 30 + k, 0.5, 5000) for k in range(1, 3)])     # only 2 runs
    _open(store, 4)
    assert flagged(path) == []


def test_both_rules_can_fire_together(cache):
    path, store = cache
    store.upsert(SPEC, [_run(k, 60 + k, 1.0, 5000) for k in range(1, 6)])
    _open(store, 42)
    (r,) = flagged(path)
    assert len(r.reasons) == 2


def test_part_and_job_alerts_merge_when_they_open_together():
    start = NOW - timedelta(days=10)
    runs = [D.OpenRun("L1", "part", "P", "J", start, 1, 864000, None, 0, ["x"]),
            D.OpenRun("L1", "job", "P", "J", start, 1, 864000, None, 0, ["x"])]
    (m,) = D.merge_part_job(runs)
    assert m.channel == "part and job"


# ------------------------------------------------ the real recorded case

def test_aread_open_since_august_is_flagged_on_the_recording(replayed_cache, recorded_at):
    """The case that prompted the check: AreaD job 3043984 open from
    25 August 2026, with nothing watching."""
    runs = {(r.device_id, r.channel): r for r in D.open_runs(replayed_cache, now=recorded_at)}
    r = runs[("AreaD", "part")]
    assert (r.part, r.job) == ("OTRIVIN", "3043984")
    assert r.start.date().isoformat() == "2026-08-25"
    assert len(r.reasons) == 2                     # over 7 days, and ~10x the median
    assert runs[("AreaE", "part")].reasons == []   # opened the same morning: fine


def test_areab_crossing_seven_days_is_flagged_on_the_recording(replayed_cache, recorded_at):
    """The second instance, found by this check the day it was written."""
    r = {(r.device_id, r.channel): r for r in D.open_runs(replayed_cache, now=recorded_at)}[("AreaB", "part")]
    assert r.open_days > 7 and "7-day limit" in r.reasons[0]
