"""How the sync behaves when things go wrong: a row the store rejects,
errors that must hold rather than quarantine, transport retries, NaN in a
quarantined payload, an unreachable device, and the record_order fallback
with an open event in the middle of the sequence."""

import json

import pytest
import requests

import xlpipe.sync as S
from fakes import FakeDevice
from xlpipe.cache import SQLiteCache
from xlpipe.channels import SPECS
from xlpipe.reader import DeviceReader
from xlpipe.store import QuarantineItem, RowRejected, SystemicError, TransportError, classify_sqlstate
from xlpipe.sync import ChannelSync, SyncSettings, sync_all

SPEC = SPECS["shift_hour"]
TODAY = 20732


class RejectingCache(SQLiteCache):
    """Behaves like a database that rejects any batch containing a bad row."""

    def upsert(self, spec, rows):
        if any(r.get("part") == "BAD" for r in rows):
            raise RowRejected('value too long for type character varying(20)', "22001")
        return super().upsert(spec, rows)


class BrokenSchemaCache(SQLiteCache):
    def upsert(self, spec, rows):
        raise SystemicError('column "acct_gap_s" does not exist', "42703")


def device(n=4):
    d = FakeDevice()
    for k in range(1, n + 1):
        d.add(k, day=TODAY - 1)
    d.add(n + 1, day=TODAY, open_=True)
    return d


def keys(store):
    return [r[0] for r in store.db.execute("SELECT shift_hour_event_id FROM hour_record ORDER BY 1")]


def test_one_rejected_row_is_quarantined_and_the_rest_load(tmp_path):
    store = RejectingCache(tmp_path / "c.db")
    dev = device()
    dev.edit(2, part="BAD")
    r = ChannelSync(DeviceReader(dev, "FR8"), store, SPEC, SyncSettings()).run()
    assert r.error is None
    assert keys(store) == [1, 3, 4]
    q = store.db.execute("SELECT natural_key, sqlstate FROM sync_quarantine WHERE resolved_at IS NULL").fetchall()
    assert [tuple(x) for x in q] == [("2", "22001")]


def test_schema_drift_holds_the_run_and_quarantines_nothing(tmp_path):
    store = BrokenSchemaCache(tmp_path / "c.db")
    with pytest.raises(SystemicError):
        ChannelSync(DeviceReader(device(), "FR8"), store, SPEC, SyncSettings()).run()
    assert store.db.execute("SELECT count(*) FROM sync_quarantine").fetchone()[0] == 0
    assert store.load_state("FR8", "shift_hour").last_sync_id is None     # nothing advanced


@pytest.mark.parametrize("sqlstate,cls", [("08006", TransportError), ("57P01", TransportError),
                                          ("40001", TransportError), ("22P02", RowRejected),
                                          ("23502", RowRejected), ("42703", SystemicError),
                                          ("23514", SystemicError), ("42501", SystemicError),
                                          ("XX000", SystemicError), (None, SystemicError)])
def test_sqlstate_classification(sqlstate, cls):
    """Schema drift and permissions hold; only data errors quarantine; the
    unknown holds, because holding is recoverable and draining is not."""
    assert classify_sqlstate(sqlstate) is cls


def test_transport_errors_retry_with_backoff_to_a_60s_ceiling(monkeypatch):
    sleeps = []
    monkeypatch.setattr(S.time, "sleep", sleeps.append)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 4:
            raise TransportError("connection reset")
        return "ok"

    assert S._with_retry(flaky, "test") == "ok"
    assert sleeps == [1.0, 2.0, 4.0]

    sleeps.clear()
    with pytest.raises(TransportError):
        S._with_retry(lambda: (_ for _ in ()).throw(TransportError("down")), "test")
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert max(sleeps) <= 60


def test_nan_in_a_quarantined_payload_is_stored_as_null(tmp_path):
    store = SQLiteCache(tmp_path / "c.db")
    store.quarantine("FR8", "shift_hour", [QuarantineItem("7", "bad", {"run_time": float("nan"),
                                                                        "nested": [float("inf"), 1.0]})])
    text = store.db.execute("SELECT payload FROM sync_quarantine").fetchone()[0]
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == {"run_time": None, "nested": [None, 1.0]}


def test_record_order_mode_never_moves_past_an_open_event_mid_sequence(tmp_path):
    """Fallback mode sorts by record_order, so an open event can sit between
    closed ones. The mark must stop before it; the closed records after it
    still load, and come round again harmlessly."""
    store = SQLiteCache(tmp_path / "c.db")
    dev = FakeDevice(honour_sync_filter=False)
    dev.add(1, day=TODAY - 1)
    dev.add(2, day=TODAY - 1)
    dev.add(3, day=TODAY - 1, open_=True)
    dev.add(4, day=TODAY - 1)
    ChannelSync(DeviceReader(dev, "FR8"), store, SPEC, SyncSettings()).run()
    st = store.load_state("FR8", "shift_hour")
    assert st.resume_key == "record_order"
    assert st.last_record_order == 2000
    assert keys(store) == [1, 2, 4]


def test_an_unreachable_device_does_not_stop_the_others(tmp_path):
    class Dead:
        host = "10.0.0.99"

        def get_json(self, *a, **k):
            raise requests.ConnectionError("no route to host")

    good = device()
    store = SQLiteCache(tmp_path / "c.db")
    results = sync_all([{"id": "DEAD", "host": "10.0.0.99"}, {"id": "OK", "host": "fake"}], ["shift_hour"], store,
                       SyncSettings(), client_factory=lambda h: Dead() if h == "10.0.0.99" else good)
    by = {r.device_id: r for r in results}
    assert by["DEAD"].error.startswith("unreachable")
    assert by["OK"].error is None and not any(c.error for c in by["OK"].channels)
    assert store.db.execute("SELECT count(*) FROM hour_record WHERE device_id='OK'").fetchone()[0] == 4
    hb = store.db.execute("SELECT last_run_ok, devices_ok, devices_total FROM sync_heartbeat").fetchone()
    assert tuple(hb) == (0, 1, 2)
