"""Priority 2: sync_id resumption, open events, edits and deletes."""

import sqlite3

import pytest

from fakes import FakeDevice
from xlpipe.cache import SQLiteCache
from xlpipe.channels import SPECS
from xlpipe.reader import DeviceReader
from xlpipe.sync import ChannelSync, SyncSettings

SPEC = SPECS["shift_hour"]
TODAY = 20732


@pytest.fixture
def store(tmp_path):
    s = SQLiteCache(tmp_path / "cache.db")
    yield s
    s.close()


def run(dev: FakeDevice, store, page_size=1000, **settings):
    reader = DeviceReader(dev, "FR8", page_size=page_size)
    result = ChannelSync(reader, store, SPEC, SyncSettings(page_size=page_size, **settings)).run()
    assert result.error is None, result.error
    return result


def keys(store) -> list[int]:
    return [r[0] for r in store.db.execute(
        "SELECT shift_hour_event_id FROM hour_record WHERE deleted_at IS NULL ORDER BY 1")]


def state(store):
    return store.load_state("FR8", "shift_hour")


def device(n_closed=3, open_=True, **kw) -> FakeDevice:
    d = FakeDevice(**kw)
    for k in range(1, n_closed + 1):
        d.add(k, day=TODAY - 1)
    if open_:
        d.add(n_closed + 1, day=TODAY, open_=True)
    return d


# -------------------------------------------------- the null high-water bug

def test_regression_single_page_backfill_sets_the_high_water_mark(store):
    """The bug: with no filter on the first page, the open event (sync_id -1)
    sorted first, the mark refused to move past it, and every channel whose
    history fit in one page kept last_sync_id = NULL - re-backfilling on
    every run, forever."""
    dev = device(n_closed=3)
    run(dev, store)
    assert state(store).last_sync_id == max(e["sync_id"] for e in dev.events.values())
    assert state(store).last_sync_id is not None


def test_backfill_never_requests_the_open_event(store):
    """The fix: a sync_id backfill starts at [gt]=-1, so the open event never
    arrives at all."""
    dev = device(n_closed=3)
    run(dev, store)
    backfill = [p for p in dev.event_requests() if p.get("sort") == "sync_id" and p.get("limit") == "1000"]
    assert backfill[0]["sync_id[gt]"] == "-1"


def test_second_run_is_incremental_only(store):
    dev = device(n_closed=3)
    run(dev, store)
    mark = state(store).last_sync_id
    dev.log.clear()
    r = run(dev, store)
    incremental = [p for p in dev.event_requests() if p.get("sort") == "sync_id"]
    assert incremental and incremental[0]["sync_id[gt]"] == str(mark)
    assert r.upserted == 0  # the window re-pull changes nothing that has not changed


# ------------------------------------------------------------ paging

def test_multi_page_backfill_loads_everything(store):
    dev = device(n_closed=7)
    run(dev, store, page_size=2)
    assert keys(store) == list(range(1, 8))
    assert state(store).backfill_completed_at is not None


# ------------------------------------------------------------ open events

def test_open_event_is_not_stored_until_it_closes(store):
    dev = device(n_closed=3)
    run(dev, store)
    assert 4 not in keys(store)
    dev.close(4)
    dev.add(5, day=TODAY, open_=True)
    run(dev, store)
    assert 4 in keys(store) and 5 not in keys(store)


def test_open_event_is_recorded_for_the_app(store):
    dev = device(n_closed=2)
    run(dev, store)
    got = store.db.execute("SELECT event_key FROM open_event WHERE device_id='FR8' AND channel='shift_hour'").fetchall()
    assert [tuple(r) for r in got] == [(3,)]


# ------------------------------------------------------------- edits

def test_edit_to_an_old_record_is_picked_up(store):
    """Reasons get assigned after the fact; with DO NOTHING the Pareto would
    stay "Missing Reason" forever."""
    dev = device(n_closed=3)
    run(dev, store)
    dev.edit(1, part="ESTRADIOL", run_time=1234.5)
    run(dev, store)
    part, rt = store.db.execute("SELECT part, run_time FROM hour_record WHERE shift_hour_event_id=1").fetchone()
    assert (part, rt) == ("ESTRADIOL", 1234.5)


def test_an_older_replay_cannot_undo_a_newer_edit(store):
    dev = device(n_closed=2)
    run(dev, store)
    old = dict(dev.events[1])
    dev.edit(1, part="NEW")
    run(dev, store)
    from xlpipe.rows import convert
    stale, _ = convert(SPEC, "FR8", old)          # the pre-edit version, lower sync_id
    store.upsert(SPEC, [stale])
    assert store.db.execute("SELECT part FROM hour_record WHERE shift_hour_event_id=1").fetchone()[0] == "NEW"


# ------------------------------------------------------------ deletes

def test_delete_on_the_device_is_flagged_not_removed(store):
    dev = device(n_closed=10)
    run(dev, store)
    dev.delete(2)
    r = run(dev, store)
    assert r.flagged_deleted == 1
    assert 2 not in keys(store)
    assert store.db.execute("SELECT deleted_at IS NOT NULL FROM hour_record WHERE shift_hour_event_id=2").fetchone()[0]


def test_empty_window_from_the_device_does_not_wipe_our_rows(store):
    """Safety valve: a device returning nothing must not flag a week deleted."""
    dev = device(n_closed=10)
    run(dev, store)
    for k in list(dev.events):
        if k <= 10:
            dev.delete(k)
    r = run(dev, store)
    assert r.flagged_deleted == 0
    assert len(keys(store)) == 10


# ------------------------------------------------------- fallback mode

def test_falls_back_to_record_order_when_sync_filter_is_ignored(store):
    dev = device(n_closed=5, honour_sync_filter=False)
    run(dev, store)
    st = state(store)
    assert st.resume_key == "record_order"
    assert keys(store) == [1, 2, 3, 4, 5]
    # the mark stops before the open event, which has a real record_order
    assert st.last_record_order == 5 * 1000


# --------------------------------------------------------- quarantine

def test_malformed_record_is_quarantined_then_retried_by_the_window(store):
    dev = device(n_closed=3)
    dev.edit(2, run_time="garbage")
    run(dev, store)
    assert 2 not in keys(store)
    q = store.db.execute("SELECT natural_key, resolved_at FROM sync_quarantine").fetchall()
    assert [tuple(r) for r in q] == [("2", None)]
    dev.edit(2, run_time=900.0)          # fixed on the device
    run(dev, store)
    assert 2 in keys(store)
    assert store.db.execute("SELECT resolved_at IS NOT NULL FROM sync_quarantine").fetchone()[0]
