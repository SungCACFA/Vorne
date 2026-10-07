"""Priority 1: reading the device correctly.

Small payloads are lifted from the recorded fixtures so the shapes are the
device's own, not what the docs say they should be.
"""

import math

import pytest

import json

from conftest import FIXTURES, HOST, fixture_file
from xlpipe.channels import SPECS
from xlpipe.metrics import (
    DeviceError, event_id, num, parse_ts, positional_events, text, unwrap, unwrap_values,
)
from xlpipe.rows import MALFORMED, OK, OPEN, build_row, convert, parse_day
from datetime import datetime, timezone

NOW = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------- envelopes

def test_unwrap_splits_data_and_meta():
    data, meta = unwrap({"data": {"x": 1}, "meta": {"asset_id": "abc", "device_id": "3C:E4"}})
    assert data == {"x": 1}
    assert meta["asset_id"] == "abc"


def test_unwrap_passes_bare_objects_through():
    # rest/cpe/attributes has no envelope
    attrs = fixture_file(HOST["FR8"], "rest_cpe_attributes.json")["response"]
    data, meta = unwrap(attrs)
    assert data is attrs and meta == {}
    assert data["asset_name"] == "FR8"


def test_unwrap_raises_on_device_error():
    # The shape the device sends for a bad request (seen live: HTTP 400 "Internal error")
    with pytest.raises(DeviceError, match="Internal error"):
        unwrap({"data": {}, "meta": {"error": {"detail": "Internal error", "status": "Bad Request"}}})


def test_api_v0_device_is_double_wrapped():
    payload = fixture_file(HOST["FR8"], "api_v0_device.json")["response"]
    # A naive .get returns the wrapper, not the value - the trap the brief warns about
    assert isinstance(payload["data"]["asset_name"], dict)
    values = unwrap_values(payload)
    assert values["asset_name"] == "FR8"
    assert values["firmware_version"] == "2.22.14.2"
    assert values["device_id"] == "3C:E4:B0:53:9D:EC"


# --------------------------------------------------------- positional arrays

def test_positional_events_zips_in_request_order():
    payload = {"data": {"events": [[3659, "2026-10-06T11:00:00.000000-04:00", 2881.7], [3660, None, 0.0]]}}
    got = positional_events(payload, ["shift_hour_event_id", "start_time", "run_time"])
    assert got[0] == {"shift_hour_event_id": 3659, "start_time": "2026-10-06T11:00:00.000000-04:00",
                      "run_time": 2881.7}
    assert got[1]["start_time"] is None


def test_positional_events_without_envelope():
    # events/current and /previous are documented without the data wrapper
    assert positional_events({"events": [[1, 2]]}, ["a", "b"]) == [{"a": 1, "b": 2}]


@pytest.mark.parametrize("row", [[1], [1, 2, 3]])
def test_positional_events_raises_on_length_mismatch(row):
    with pytest.raises(ValueError, match="requested fields"):
        positional_events({"data": {"events": [row]}}, ["a", "b"])


def test_positional_events_on_a_recorded_page():
    """A real backfill page: every row zips to exactly the requested fields."""
    pages = [json.loads(f.read_text(encoding="utf-8"))
             for f in (FIXTURES / HOST["FR8"]).glob("api_v0_channels_shift_hour_events__*.json")]
    page = max(pages, key=lambda r: len(str(r["response"])))  # the biggest backfill page
    fields = page["request"]["params"]["fields"].split(",")
    rows = positional_events(page["response"], fields)
    assert len(rows) > 100
    assert all(list(r) == fields for r in rows)


# ------------------------------------------------------------------ values

@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_become_none(raw):
    assert num(raw) is None


def test_negative_and_zero_numbers_are_kept():
    assert num(-16996.0) == -16996.0
    assert num(0.0) == 0.0
    assert num("-290") == -290.0


@pytest.mark.parametrize("raw", ["null", None, ""])
def test_device_null_string_becomes_none(raw):
    assert text(raw) is None
    assert num(raw) is None


def test_text_keeps_real_values():
    assert text("first_shift") == "first_shift"


def test_event_id_tolerates_float_and_string():
    assert event_id(12.0) == 12 and event_id("12") == 12
    assert event_id(12.5) is None and event_id("null") is None


def test_parse_ts_keeps_offset_and_rejects_naive():
    ts = parse_ts("2026-01-20T07:00:00.000000-06:00")
    assert ts.utcoffset().total_seconds() == -6 * 3600
    assert parse_ts("2026-01-20T07:00:00") is None


# ------------------------------------------------------- row conversion

def _hour(**over):
    base = {"shift_hour_event_id": 2459, "sync_id": 8925, "record_order": 1, "production_day_number": 20685,
            "start_time": "2026-08-20T13:00:00.000000-04:00", "end_time": "2026-08-20T14:00:00.000000-04:00",
            "shift": "second_shift", "shift_hour": "7", "part": "OMNARIS", "job": "3043900",
            "in_count": 0.0, "good_count": 3316.0, "reject_count": -2379.0, "run_time": 0.0,
            "unplanned_stop_time": 2250.0, "planned_stop_time": 1350.0, "not_scheduled_time": 30.0,
            "ideal_time": 0.0, "target_count": 0.0}
    base.update(over)
    return base


def test_category_arrays_on_changeover_are_joined_not_repr():
    row, problems = convert(SPECS["shift_hour"], "AreaD", _hour(part=["SOFDRA", "OTRIVIN"], job=["1", "2"]))
    assert not problems
    assert row["part"] == "SOFDRA,OTRIVIN"
    assert row["job"] == "1,2"
    assert "[" not in row["part"]


def test_category_array_drops_null_members():
    row, _ = convert(SPECS["shift_hour"], "AreaD", _hour(part=["null", "OTRIVIN"]))
    assert row["part"] == "OTRIVIN"


def test_nan_in_a_record_becomes_null_and_is_reported():
    b = build_row(SPECS["shift_hour"], "FR8", _hour(ideal_time=float("nan")), current_key=None, now=NOW)
    assert b.status == OK
    assert b.row["ideal_time"] is None
    assert b.non_finite == ["ideal_time"]


def test_null_string_categories_become_null():
    b = build_row(SPECS["shift_hour"], "FR8", _hour(shift="null", shift_hour="null"), current_key=None, now=NOW)
    assert b.row["shift"] is None and b.row["shift_hour"] is None


def test_timestamps_stored_as_utc():
    b = build_row(SPECS["shift_hour"], "FR8", _hour(), current_key=None, now=NOW)
    assert b.row["start_time"] == "2026-08-20T17:00:00+00:00"


def test_non_numeric_value_is_malformed_not_silently_null():
    b = build_row(SPECS["shift_hour"], "FR8", _hour(run_time="abc"), current_key=None, now=NOW)
    assert b.status == MALFORMED and "run_time" in b.reason


def test_missing_key_is_malformed():
    b = build_row(SPECS["shift_hour"], "FR8", _hour(shift_hour_event_id=None), current_key=None, now=NOW)
    assert b.status == MALFORMED


@pytest.mark.parametrize("over,current", [({"sync_id": -1}, None), ({}, 2459),
                                          ({"end_time": "2026-12-01T00:00:00+00:00"}, None),
                                          ({"end_time": None}, None)])
def test_open_events_are_never_rows(over, current):
    b = build_row(SPECS["shift_hour"], "FR8", _hour(**over), current_key=current, now=NOW)
    assert b.status == OPEN and b.row is None


@pytest.mark.parametrize("raw,expected", [(20700, (20700, None)), ("20700", (20700, None)),
                                          ([20700, 20701], (20700, "20700,20701")),
                                          ("20700,20701", (20700, "20700,20701")), ("null", (None, None))])
def test_production_day_keeps_first_day_and_raw_span(raw, expected):
    assert parse_day(raw) == expected
