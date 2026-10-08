"""Total cycles: the denominator of every per-cycle figure.

Vorne: Ideal Time = Ideal Cycle Time x Total Cycles, Total Cycles = run cycles + small stops + partial
cycles. Dividing by run cycles alone inflated the derived ICT (13% on KWELLADA_100ML, 1.6% on
ROGAINE_5b_TRIPLES, read from the devices on 2026-10-08); by total cycles the configured value comes back
exactly - FR8 ESTRADIOL is 0.375 s on all 88 runs. These tests cover the plumbing that makes that true:
the new columns, the backfill of rows cached before them, the fallbacks, and the replay of fixtures that
predate the fields.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from webapp import data as D
from xlpipe.cache import SQLiteCache
from xlpipe.channels import SPECS


def part_row(k, sync_id, **extra):
    st = datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(days=k)
    return {"device_id": "L1", "part_event_id": k, "sync_id": sync_id, "record_order": k,
            "production_day_number": 20697 + k, "start_time": st.isoformat(),
            "end_time": (st + timedelta(hours=8)).isoformat(), "part": "P", "run_time": 3600.0,
            "ideal_time": 600.0, "run_cycles": 900.0, "small_stops": 80.0, "partial_cycles": 20.0, **extra}


# ------------------------------------------------- a cache from before the fields

def test_an_old_cache_gains_the_columns_and_re_pulls_those_channels(tmp_path):
    db_path = tmp_path / "old.db"
    SQLiteCache(db_path).close()
    db = sqlite3.connect(db_path)
    for t in ("part_run", "job_run"):
        db.execute(f"ALTER TABLE {t} DROP COLUMN total_cycles")
        db.execute(f"ALTER TABLE {t} DROP COLUMN partial_cycles")
    for ch in ("part", "job", "shift_hour"):
        db.execute("INSERT INTO sync_state (device_id, channel, last_sync_id) VALUES ('L1', ?, 500)", (ch,))
    db.commit()
    db.close()
    cache = SQLiteCache(db_path)
    assert cache.added_columns == {"part_run": ["total_cycles", "partial_cycles"],
                                   "job_run": ["total_cycles", "partial_cycles"]}
    cache.close()
    state = dict(sqlite3.connect(db_path).execute("SELECT channel, last_sync_id FROM sync_state"))
    assert state == {"part": None, "job": None, "shift_hour": 500}      # only the changed channels re-pull
    assert SQLiteCache(db_path).added_columns == {}                     # and only once


# ------------------------------------------------------------- the backfill rule

@pytest.fixture
def cache(tmp_path):
    c = SQLiteCache(tmp_path / "c.db")
    yield c, tmp_path / "c.db"
    c.close()


def total(db_path, k=1):
    return sqlite3.connect(db_path).execute("SELECT total_cycles FROM part_run WHERE part_event_id=?", (k,)).fetchone()[0]


def test_a_re_pull_fills_the_new_column(cache):
    c, path = cache
    c.upsert(SPECS["part"], [part_row(1, 5)])                          # cached before the field existed
    assert total(path) is None
    c.upsert(SPECS["part"], [part_row(1, 5, total_cycles=1000.0)])     # same version, re-pulled
    assert total(path) == 1000.0


def test_the_fill_never_overwrites_and_never_takes_an_older_version(cache):
    c, path = cache
    c.upsert(SPECS["part"], [part_row(1, 5, total_cycles=1000.0)])
    c.upsert(SPECS["part"], [part_row(1, 5, total_cycles=7.0)])        # same version, other value: kept
    assert total(path) == 1000.0
    c.upsert(SPECS["part"], [part_row(2, 9)])
    c.upsert(SPECS["part"], [part_row(2, 4, total_cycles=7.0)])        # an older version: never fills
    assert total(path, 2) is None


# --------------------------------------------------------------- the fallbacks

def test_device_total_preferred_then_computed_then_without_partial(cache):
    c, path = cache
    c.upsert(SPECS["part"], [
        part_row(1, 1, total_cycles=1200.0),                     # the device's own (deliberately != the sum)
        part_row(2, 2),                                          # no total: run + small + partial = 1000
        part_row(3, 3, partial_cycles=None),                     # nor partial: run + small = 980
    ])
    c.close()
    rows = {r["part_event_id"]: (r["cycles_total"], r["cycles_basis"]) for r in D.runs(path, "part_run", "L1", 0, 99999)}
    assert rows == {1: (1200.0, "device"), 2: (1000.0, "computed"), 3: (980.0, "without partial cycles")}


def test_a_cache_without_the_columns_still_reads(tmp_path):
    """The app reads the cache and never migrates it; until a refresh it must still work, and say so."""
    path = tmp_path / "old.db"
    c = SQLiteCache(path)
    c.upsert(SPECS["part"], [part_row(1, 1)])
    c.close()
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE part_run DROP COLUMN total_cycles")
    db.execute("ALTER TABLE part_run DROP COLUMN partial_cycles")
    db.commit()
    db.close()
    (r,) = D.runs(path, "part_run", "L1", 0, 99999)
    assert (r["cycles_total"], r["cycles_basis"]) == (980.0, "without partial cycles")
    assert D.device_ict(path)[("L1", "P")]["ict"] == pytest.approx(600 / 980)


# --------------------------------------------- fixtures that predate the fields

def test_replay_fills_whole_runs_and_never_a_clipped_slice():
    """FR8's recording holds run 291 twice at the same version: whole in a history page, and clipped to the
    window (from 25 Sep 07:00, 108,107 of its 122,727 run cycles) in the re-sync request."""
    import json
    from pathlib import Path

    from xlpipe.client import DeviceHTTPError, ReplayClient
    from xlpipe.metrics import positional_events

    rc = ReplayClient(Path(__file__).parent / "fixtures" / "devices" / "10.15.22.19")
    seen = {}
    for f in rc.dir.glob("api_v0_channels_part_events__*.json"):
        rec = json.loads(f.read_text(encoding="utf-8"))
        params = dict(rec["request"]["params"])
        if rec["status"] != 200 or "run_cycles" not in params.get("fields", ""):
            continue
        fields = params["fields"].split(",") + ["total_cycles", "partial_cycles"]
        for r in positional_events(rc.get_json(f.name and "api/v0/channels/part/events",
                                               {**params, "fields": ",".join(fields)}), fields):
            if r["part_event_id"] == 291:
                seen[r["run_cycles"]] = r["total_cycles"]
            if r["sync_id"] != -1 and r["total_cycles"] is not None:      # every filled row is a whole run
                assert r["total_cycles"] == r["run_cycles"] + r["small_stops"] + r["partial_cycles"]
    assert seen == {122727.0: 123469.0, 108107.0: None}
    with pytest.raises(DeviceHTTPError):                                # any other unrecorded request still fails
        rc.get_json("api/v0/channels/part/events", {"fields": "part_event_id,not_a_field"})


def test_the_products_page_says_where_its_total_cycles_came_from(app_client):
    h = app_client.get("/products?g=ytd&p=2026").get_data(as_text=True)
    assert "Total cycles\nhere are the device's own, on all" in h


def test_a_clipped_window_slice_never_fills_a_whole_run(cache):
    """A production-day-filtered request returns a run clipped to the window: same sync_id, fewer cycles
    (FR8 run 291: 108,107 run cycles in the window, 122,727 whole). Its totals must not land on the run."""
    c, path = cache
    c.upsert(SPECS["part"], [part_row(1, 5)])                                       # whole run, 900 run cycles
    c.upsert(SPECS["part"], [part_row(1, 5, run_cycles=700.0, total_cycles=760.0)])  # the clipped slice
    assert total(path) is None
    c.upsert(SPECS["part"], [part_row(1, 5, total_cycles=1000.0)])                  # the whole run, re-pulled
    assert total(path) == 1000.0
