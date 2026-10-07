"""Priority 5: counts arrive exactly as the device reported them.

This is the test most likely to be "helpfully" broken later - someone
clamping negative rejects to zero, recomputing reject = in - good, or
abs()-ing a count. The count data is wrong on the device; storing it wrong
in a different way would hide the evidence.

Every expected value below is a literal, derived once from the raw fixture
JSON by a throwaway script that shared no code with xlpipe. Nothing here
runs the code under test to produce its own expectation.
"""

import sqlite3

import pytest

# (device, channel) -> rows, sum in, sum good, sum reject, rows with reject < 0, rows with good < 0
CHECKSUMS = {
    ("FR8", "shift_hour"): (3659, 3386951.0, 4129590.0, -742117.0, 923, 65),
    ("FR8", "shift"): (521, 3373463.0, 4109829.0, -735844.0, 181, 15),
    ("AreaB", "shift_hour"): (2925, 1824801.0, 1547392.0, 278582.0, 415, 31),
    ("AreaB", "shift"): (414, 1824801.0, 1547392.0, 278582.0, 62, 7),
    ("AreaD", "shift_hour"): (3079, 760525.0, 708697.0, 51828.0, 392, 0),
    ("AreaD", "shift"): (435, 760525.0, 708697.0, 51828.0, 49, 0),
    ("AreaE", "shift_hour"): (2981, 930564.0, 950845.0, -20281.0, 397, 61),
    ("AreaE", "shift"): (421, 930564.0, 950845.0, -20281.0, 77, 7),
}
TABLE = {"shift_hour": ("hour_record", "shift_hour_event_id"), "shift": ("shift_record", "shift_event_id")}

# The first three negative-reject hours on each line, as the device sent them.
NEGATIVE_REJECT_HOURS = [
    ("FR8", 390, 9.0, 36.0, -27.0), ("FR8", 391, 1.0, 39.0, -38.0), ("FR8", 396, 5208.0, 5281.0, -73.0),
    ("AreaB", 90, 1099.0, 1149.0, -50.0), ("AreaB", 100, 0.0, 914.0, -914.0), ("AreaB", 101, 0.0, 203.0, -203.0),
    ("AreaD", 205, 0.0, 1.0, -1.0), ("AreaD", 223, 0.0, 33.0, -33.0), ("AreaD", 224, 0.0, 688.0, -688.0),
    ("AreaE", 126, 3340.0, 3443.0, -103.0), ("AreaE", 127, 1691.0, 1869.0, -178.0), ("AreaE", 128, 3666.0, 4488.0, -822.0),
]

# The six hours where reject != in - good: suspected manual count entry.
ANOMALIES = [
    ("AreaB", 2459, 0.0, 3316.0, -2379.0),
    ("AreaB", 2813, 1.0, 237.0, 0.0),
    ("FR8", 2099, 2928.0, 115.0, 2378.0),
    ("FR8", 2864, 744.0, 0.0, 372.0),
    ("FR8", 3480, 0.0, 3278.0, -1657.0),
    ("FR8", 3482, 588.0, 4.0, 292.0),
]


@pytest.fixture(scope="module")
def db(replayed_cache):
    return sqlite3.connect(replayed_cache)


@pytest.mark.parametrize("dev,channel", sorted(CHECKSUMS))
def test_count_checksums_match_the_device(db, dev, channel):
    """Any clamp, abs(), recomputation or dropped row moves at least one of
    these: the sums carry the sign of every value."""
    table, _ = TABLE[channel]
    got = db.execute(
        f"SELECT count(*), sum(in_count), sum(good_count), sum(reject_count), "
        f"sum(reject_count < 0), sum(good_count < 0) FROM {table} WHERE device_id=?", (dev,)).fetchone()
    assert got == CHECKSUMS[(dev, channel)]


@pytest.mark.parametrize("dev,key,i,g,r", NEGATIVE_REJECT_HOURS)
def test_negative_reject_hours_are_stored_as_sent(db, dev, key, i, g, r):
    got = db.execute("SELECT in_count, good_count, reject_count FROM hour_record "
                     "WHERE device_id=? AND shift_hour_event_id=?", (dev, key)).fetchone()
    assert got == (i, g, r)


@pytest.mark.parametrize("dev,key,i,g,r", ANOMALIES)
def test_the_six_anomalies_are_unmodified(db, dev, key, i, g, r):
    got = db.execute("SELECT in_count, good_count, reject_count FROM hour_record "
                     "WHERE device_id=? AND shift_hour_event_id=?", (dev, key)).fetchone()
    assert got == (i, g, r)
    assert r != i - g      # the whole point: not "corrected" to in - good


def test_the_six_are_the_only_ones(db):
    got = db.execute("SELECT device_id, shift_hour_event_id FROM hour_record "
                     "WHERE abs(reject_count - (in_count - good_count)) > 1e-6 ORDER BY 1, 2").fetchall()
    assert got == [(d, k) for d, k, *_ in ANOMALIES]


def test_count_adjustment_signature_count_is_stable(db):
    """reject == -good with no run time: FR8 had 193 in the original notebook."""
    assert db.execute("SELECT count(*) FROM hour_record WHERE device_id='FR8' AND is_count_adjustment").fetchone()[0] == 193
