"""Shared fixtures. No live device: everything replays responses recorded with

    python -m xlpipe refresh --record tests/fixtures/devices

against an empty cache, i.e. exactly the requests a first-run backfill makes.
Re-record when the device data needs refreshing; the expected values in
test_aggregation.py are tied to the recording and must be re-verified then.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xlpipe.cache import SQLiteCache  # noqa: E402
from xlpipe.client import ReplayClient  # noqa: E402
from xlpipe.sync import SyncSettings, sync_all  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "devices"
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
DEVICES = CONFIG["devices"]
HOST = {d["id"]: d["host"] for d in DEVICES}


def fixture_file(host: str, name: str) -> dict:
    """One recorded response, by its file name (see client.fixture_name)."""
    return json.loads((FIXTURES / host / name).read_text(encoding="utf-8"))


def recorded_events(host: str, channel: str) -> list[dict]:
    """Every closed record the device sent for a channel during the backfill,
    straight from the raw fixture files.

    Deliberately shares no code with xlpipe - it indexes the positional
    arrays itself - so tests comparing the cache with this are not comparing
    our zipping code with itself."""
    key = {"shift_hour": "shift_hour_event_id", "shift": "shift_event_id", "process_state": "process_state_event_id",
           "part": "part_event_id", "job": "job_event_id"}[channel]
    best: dict = {}
    for f in (FIXTURES / host).glob(f"api_v0_channels_{channel}_events__*.json"):
        rec = json.loads(f.read_text(encoding="utf-8"))
        params = rec["request"]["params"]
        if rec["status"] != 200 or "group" in params:
            continue
        fields = params["fields"].split(",")
        body = rec["response"]
        rows = body["data"]["events"] if "data" in body else body["events"]
        ki, si = fields.index(key), fields.index("sync_id")
        for row in rows:
            if row[si] == -1:
                continue
            ev = {fields[i]: row[i] for i in range(len(fields))}
            # Probe requests ask for only the key and sync_id; keep the full record.
            if len(ev) >= len(best.get(row[ki], {})):
                best[row[ki]] = ev
    return list(best.values())


@pytest.fixture(scope="session")
def recorded_at() -> datetime:
    """When the fixtures were captured, from the device's own clock."""
    rec = fixture_file(HOST["FR8"], "api_v0_device.json")
    from xlpipe.metrics import unwrap_values

    return datetime.fromisoformat(unwrap_values(rec["response"])["time"])


@pytest.fixture(scope="session")
def replayed_cache(tmp_path_factory) -> Path:
    """A cache built by the real sync from the recorded responses."""
    path = tmp_path_factory.mktemp("replay") / "cache.db"
    store = SQLiteCache(path)
    try:
        store.apply_epochs(CONFIG.get("epochs") or {})
        results = sync_all(DEVICES, CONFIG["channels"], store, SyncSettings(**CONFIG["sync"]),
                           client_factory=lambda host: ReplayClient(FIXTURES / host, host=host))
        errors = [(d.device_id, c.channel, c.error) for d in results for c in d.channels if c.error]
        assert not errors, f"replay diverged from the recording: {errors}"
        assert all(not d.error for d in results)
        store.restamp_epochs()
        store.refresh_part_standard()
    finally:
        store.close()
    return path


@pytest.fixture
def cache_copy(replayed_cache, tmp_path) -> Path:
    """A private copy of the replayed cache, for tests that write to it."""
    import sqlite3

    dst = tmp_path / "copy.db"
    src = sqlite3.connect(replayed_cache)
    out = sqlite3.connect(dst)
    src.backup(out)
    out.close()
    src.close()
    return dst


def client_for(cache: Path, tmp_path: Path):
    from webapp import create_app

    cfg = tmp_path / "copy_config.yaml"
    cfg.write_text(f"cache_path: {cache.as_posix()}\n", encoding="utf-8")
    return create_app(cfg).test_client()


@pytest.fixture(scope="session")
def app_client(replayed_cache, tmp_path_factory):
    from webapp import create_app

    cfg = tmp_path_factory.mktemp("cfg") / "config.yaml"
    cfg.write_text(f"cache_path: {replayed_cache.as_posix()}\n", encoding="utf-8")
    return create_app(cfg).test_client()
