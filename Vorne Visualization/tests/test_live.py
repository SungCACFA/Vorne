"""The live view: devices -> writer -> current_state -> /live.

Fixtures in tests/fixtures/live/ are one real reading of each device, taken
2026-10-07 09:00 (GET api/v0/real_time/data and the open process_state
event). Literals below were read off the raw JSON, not through the code:
  FR8   No Production (not_scheduled), Planned Unscheduled, ADVIL / 3044146
  AreaB Maintenance (planned_stop), Preventative Maintenance, OTRIVIN / 3044066,
        in it since 2 Oct 10:27:45, 167,556 s counted; shift planned 7,221 s
  AreaD Down (unplanned_stop), Waiting - Bulk, OTRIVIN / 3043984
  AreaE Changeover (planned_stop), Setup, ROGAINE_5b_SINGLES; shift run 814 s,
        unplanned 1,541 s, planned 4,866 s, ideal 114.6 s
Every shift is "Day Shift" from 07:00 (the devices' own names disagree:
AreaB's 07:00 shift is "second_shift").
"""

import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from xlpipe import live as W
from webapp import live as V

FIX = Path(__file__).parent / "fixtures" / "live"
DEVICES = ("FR8", "AreaB", "AreaD", "AreaE")


def reading(dev):
    rt = json.loads((FIX / f"{dev}_real_time.json").read_text(encoding="utf-8"))
    from xlpipe.metrics import positional_events
    st = positional_events(json.loads((FIX / f"{dev}_state.json").read_text(encoding="utf-8")), W.STATE_FIELDS)
    return W.from_real_time(rt, st)


class FakeClient:
    """Serves a device's recorded responses; optionally slow or failing."""

    def __init__(self, dev, delay=0.0, fail=False):
        self.dev, self.delay, self.fail = dev, delay, fail

    def get_json(self, path, params=None):
        time.sleep(self.delay)
        if self.fail:
            raise TimeoutError("timed out after 3 s")
        name = "real_time" if path == "api/v0/real_time/data" else "state"
        return json.loads((FIX / f"{self.dev}_{name}.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------ the reading

@pytest.mark.parametrize("dev,state,label,impact,reason,part,job", [
    ("FR8", "no_production", "No Production", "not_scheduled", "Planned Unscheduled", "ADVIL", "3044146"),
    ("AreaB", "maintenance", "Maintenance", "planned_stop", "Preventative Maintenance", "OTRIVIN", "3044066"),
    ("AreaD", "down", "Down", "unplanned_stop", "Waiting - Bulk", "OTRIVIN", "3043984"),
    ("AreaE", "changeover", "Changeover", "planned_stop", "Setup", "ROGAINE_5b_SINGLES", "3044229"),
])
def test_each_device_reads_as_recorded(dev, state, label, impact, reason, part, job):
    r = reading(dev)
    assert (r["process_state"], r["process_state_label"], r["performance_impact"],
            r["process_state_reason_display_name"], r["part"], r["job"]) == (state, label, impact, reason, part, job)
    assert r["shift_label"] == "Day Shift" and r["shift_start"] == "2026-10-07T07:00:00.000000-04:00"


def test_shift_and_state_times_are_the_devices_own():
    b = reading("AreaB")
    assert b["state_start"] == "2026-10-02T10:27:45.000000-04:00" and round(b["state_elapsed_s"]) == 167556
    assert round(b["shift_planned_stop_time"]) == 7221 and b["shift_run_time"] == 0
    e = reading("AreaE")
    assert [round(e[k]) for k in ("shift_run_time", "shift_unplanned_stop_time", "shift_planned_stop_time")]         == [814, 1541, 4866]
    assert round(e["shift_ideal_time"], 1) == 114.6


def test_a_state_start_from_another_event_is_not_used():
    rt = json.loads((FIX / "AreaB_real_time.json").read_text(encoding="utf-8"))
    r = W.from_real_time(rt, [{"process_state_event_id": 1, "start_time": "2026-01-01T00:00:00-05:00"}])
    assert r["state_start"] is None


def test_no_piece_counts_anywhere():
    for word in ("count", "reject", "good", "piece"):
        assert word not in W.DDL.lower()
        assert not any(word in k for k in reading("AreaE"))


# -------------------------------------------------------------- the writer

def test_a_failed_read_keeps_the_last_good_values(tmp_path):
    db_path = tmp_path / "c.db"
    assert W.poll_device(db_path, "AreaB", FakeClient("AreaB")) is True
    db = sqlite3.connect(db_path)
    first = db.execute("SELECT observed_at, process_state_label, last_error FROM current_state").fetchone()
    assert first[1] == "Maintenance" and first[2] is None
    assert W.poll_device(db_path, "AreaB", FakeClient("AreaB", fail=True)) is False
    obs, label, err, att = db.execute(
        "SELECT observed_at, process_state_label, last_error, attempted_at FROM current_state").fetchone()
    assert (obs, label) == (first[0], "Maintenance")           # values and their age kept
    assert err == "TimeoutError: timed out after 3 s" and att > obs
    W.poll_device(db_path, "AreaB", FakeClient("AreaB"))
    assert db.execute("SELECT last_error FROM current_state").fetchone()[0] is None


def test_one_slow_device_does_not_delay_the_others(tmp_path):
    db_path = tmp_path / "c.db"
    devices = [{"id": d, "host": d} for d in DEVICES]
    stop, threads = W.start(db_path, devices, interval=60,
                            client_factory=lambda h: FakeClient(h, delay=3.0 if h == "AreaD" else 0.0))
    try:
        time.sleep(1.0)
        got = {r[0] for r in sqlite3.connect(db_path).execute(
            "SELECT device_id FROM current_state WHERE observed_at IS NOT NULL")}
        assert got == {"FR8", "AreaB", "AreaE"}                  # AreaD still reading; the rest written
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)


def test_create_app_never_starts_the_writer(tmp_path):
    from webapp import create_app

    create_app()
    assert not any(t.name.startswith("live-") for t in threading.enumerate())


def test_only_one_writer_at_a_time(tmp_path):
    from xlpipe.__main__ import AlreadyRunning, live_lock

    cfg = {"cache_path": str(tmp_path / "c.db")}
    with live_lock(cfg):
        with pytest.raises(AlreadyRunning):
            live_lock(cfg).__enter__()


# ---------------------------------------------------------------- the page

@pytest.fixture
def live_app(tmp_path, monkeypatch):
    """An app whose cache holds a current_state table we write directly. Any
    attempt to reach a device fails the test."""
    import xlpipe.client
    from webapp import create_app

    def no_device(*a, **k):
        raise AssertionError("the page must never call a device")
    monkeypatch.setattr(xlpipe.client.XLClient, "__init__", no_device)
    monkeypatch.setattr(W, "read_device", no_device)
    db_path = tmp_path / "c.db"
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"cache_path: {db_path.as_posix()}\nlive:\n  interval_seconds: 5\n  stale_after_seconds: 15\n"
                   "devices:\n" + "".join(f"  - {{id: {d}, host: x}}\n" for d in DEVICES), encoding="utf-8")
    from xlpipe.cache import SQLiteCache
    SQLiteCache(db_path).close()                                 # the rest of the cache, empty
    return create_app(cfg).test_client(), db_path


def put(db_path, dev, *, age_s=None, error=None, error_age_s=0.0):
    now = datetime.now(timezone.utc)
    with sqlite3.connect(db_path) as db:
        db.executescript(W.DDL)
        if age_s is not None:
            W.write_ok(db, dev, reading(dev), (now - timedelta(seconds=age_s)).isoformat())
        if error:
            W.write_failed(db, dev, error, (now - timedelta(seconds=error_age_s)).isoformat())


def test_never_run_says_so(live_app):
    client, _ = live_app
    h = client.get("/live").get_data(as_text=True)
    assert_never_run_shown(h)
    assert "python -m xlpipe live" in h


def assert_never_run_shown(h):
    """The message is always in the page (so polling can reveal or hide it); what matters is that it is
    shown, the status bar hidden, and no panel drawn."""
    assert re.search(r"<div class=flag role=alert data-never\s*>", h)        # not hidden
    assert re.search(r"<div class=liveline data-bar data-oldest[^>]* hidden>", h)
    assert '<section class="lpanel' not in h


def test_an_empty_table_also_means_never_run(live_app):
    client, db_path = live_app
    with sqlite3.connect(db_path) as db:
        db.executescript(W.DDL)
    assert_never_run_shown(client.get("/live").get_data(as_text=True))
    j = client.get("/api/live").get_json()
    assert j["never_run"] is True and j["rows"] == []


def test_with_readings_the_never_run_message_is_hidden(live_app):
    client, db_path = live_app
    put(db_path, "FR8", age_s=1)
    h = client.get("/live").get_data(as_text=True)
    assert re.search(r"<div class=flag role=alert data-never hidden>", h)
    assert not re.search(r"<div class=liveline data-bar data-oldest[^>]* hidden>", h)


def test_panels_read_the_table_only(live_app):
    client, db_path = live_app
    for d in DEVICES:
        put(db_path, d, age_s=2)
    h = client.get("/live").get_data(as_text=True)
    assert h.count('<section class="lpanel') == 4 and h.count("lpanel stale") == 0
    for text in ("No Production", "Maintenance", "Down", "Changeover", "Waiting - Bulk", "Preventative Maintenance"):
        assert text in h
    assert 'class="lstate imp-unp"' in h and 'class="lstate imp-nsc"' in h and h.count('class="lstate imp-pln"') == 2
    # AreaE's shift: availability 814 / (814 + 1541 + 4866) = 11.3%; performance 114.6 / 814 = 14.1%
    assert "11.3%" in h and "14.1%" in h
    assert "All" in h and "data-oldest" in h and ">Refresh</button>" in h


def test_ages_are_of_the_reading_not_the_page(live_app):
    client, db_path = live_app
    put(db_path, "FR8", age_s=40)
    put(db_path, "AreaB", age_s=3)
    h = client.get("/live").get_data(as_text=True)
    ages = [float(a) for a in __import__("re").findall(r'data-panel data-device="\w+"\s+data-age0="([\d.]+)"', h)]
    assert len(ages) == 2 and 39.5 < max(ages) < 45 and min(ages) < 8
    assert "Oldest reading: <strong>FR8</strong>" in h and "AreaD, AreaE</strong> never read" in h


def test_an_old_reading_is_stale_without_any_error(live_app):
    client, db_path = live_app
    put(db_path, "FR8", age_s=60)                                # writer stopped a minute ago
    h = client.get("/live").get_data(as_text=True)
    assert '<section class="lpanel stale"' in h and "Read failed" not in h


def test_a_failing_device_keeps_its_last_values_marked_stale(live_app):
    client, db_path = live_app
    put(db_path, "AreaD", age_s=120, error="ConnectTimeout: timed out", error_age_s=2)
    h = client.get("/live").get_data(as_text=True)
    panel = h[h.index("<h2>AreaD</h2>") - 300:]
    assert 'class="lpanel stale"' in panel[:300]
    assert "Read failed at" in panel and "ConnectTimeout: timed out" in panel and "last good values" in panel
    assert "Waiting - Bulk" in panel                              # kept, not blanked


def test_a_device_never_read_shows_no_numbers(live_app):
    client, db_path = live_app
    put(db_path, "FR8", age_s=1)
    put(db_path, "AreaE", error="ConnectionError: no route to host")
    h = client.get("/live").get_data(as_text=True)
    panel = h[h.index("<h2>AreaE</h2>"):h.index("</section>", h.index("<h2>AreaE</h2>"))]
    assert "No reading yet from AreaE" in panel and "no route to host" in panel
    assert "lstate" not in panel and " h</strong>" not in panel and "%" not in panel


def test_no_counts_on_the_page(live_app):
    client, db_path = live_app
    for d in DEVICES:
        put(db_path, d, age_s=1)
    body = client.get("/live").get_data(as_text=True)
    body = body[body.index("<div class=livegrid data-grid>"):body.index("<script>", body.index("<div class=livegrid"))]
    assert not re.search(r"counts?|pieces?|reject", body, re.I)


def test_live_is_in_the_nav_but_not_the_export(app_client):
    h = app_client.get("/line/FR8?g=month&p=2026-09").get_data(as_text=True)
    nav = h[h.index("<nav>"):h.index("</nav>")]
    assert '<a href="/live">Live</a>' in nav
    from webapp import export
    assert "/live" not in export.SHELL


def test_the_browser_marks_a_reading_stale_as_it_ages(live_app, tmp_path):
    """The ticker runs only in a browser. A reading 10 s old against a 15 s
    limit: current when the page opens, stale by itself 8 s later, with the
    age counted up - no reload, no new data."""
    import shutil
    import subprocess

    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not edge.exists() and not shutil.which("msedge"):
        pytest.skip("no Edge on this machine to run the page's JavaScript")
    client, db_path = live_app
    put(db_path, "FR8", age_s=10)
    page = tmp_path / "live.html"
    page.write_text(client.get("/live").get_data(as_text=True), encoding="utf-8")
    seen = []
    for budget in (1000, 8000):
        dom = subprocess.run([str(edge), "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'edge'}",
                              f"--virtual-time-budget={budget}", "--dump-dom", page.as_uri()],
                             capture_output=True, text=True, encoding="utf-8", timeout=120).stdout
        cls = re.search(r'<section class="([^"]+)"', dom).group(1)
        age = int(re.search(r'class="lread">.*?<span data-age="">(\d+) s</span>', dom, re.S).group(1))
        seen.append((cls, age, re.search(r'data-verdict="">([^<]*)<', dom).group(1)))
    (c1, a1, v1), (c2, a2, v2) = seen
    assert "stale" not in c1 and 10 <= a1 <= 13 and v1 == "3 of 4 lines stale"    # the other three never read
    assert "stale" in c2 and a2 >= 17 and v2 == "4 of 4 lines stale"
    # opened from a file, every poll fails: the values stay, the ages climb, and the marker says so
    assert re.search(r'class="lpoll[^"]*down[^"]*"[^>]*>no update for \d+ s<', dom)
    assert ">No Production</p>" in dom                              # last values kept, not blanked


# ------------------------------------------------------------ auto-refresh

def test_the_json_endpoint_returns_the_rows_and_their_panels(live_app):
    client, db_path = live_app
    put(db_path, "AreaD", age_s=3)
    put(db_path, "AreaE", error="ConnectionError: no route to host")
    j = client.get("/api/live").get_json()
    assert j["never_run"] is False and j["stale_after"] == 15
    by = {r["device_id"]: r for r in j["rows"]}
    assert set(by) == set(DEVICES)
    d = by["AreaD"]
    assert d["process_state_label"] == "Down" and d["process_state_reason_display_name"] == "Waiting - Bulk"
    assert 2 < d["age_s"] < 10 and d["stale"] is False and d["observed_at"]
    assert d["html"].startswith('<section class="lpanel" data-panel data-device="AreaD"')
    assert by["AreaE"]["never_read"] is True and "No reading yet from AreaE" in by["AreaE"]["html"]
    assert "AreaD</strong>, taken" in j["top_html"] and "FR8, AreaB, AreaE</strong> never read" in j["top_html"]
    assert not re.search(r"\bcounts?\b|_count|\bpieces?\b|reject|good_", json.dumps(j["rows"]), re.I)


def test_the_json_endpoint_says_when_the_writer_never_ran(live_app):
    client, _ = live_app
    j = client.get("/api/live").get_json()
    assert j["never_run"] is True and j["rows"] == [] and j["top_html"] == ""


def test_stale_threshold_defaults_to_twenty_seconds(tmp_path):
    from webapp import ROOT, create_app

    cfg = tmp_path / "c.yaml"
    cfg.write_text("cache_path: x.db\nlive:\n  interval_seconds: 5\n", encoding="utf-8")
    assert create_app(cfg).config["LIVE"]["stale_after"] == 20
    assert create_app(ROOT / "config.yaml").config["LIVE"]["stale_after"] == 20


def weekday_clock(ts):
    from webapp.data import TZ

    return ts.astimezone(TZ).strftime("%a %H:%M")


def test_a_long_state_shows_counted_time_and_wall_clock(live_app):
    """AreaD: "down" since Monday, three hours of it counted. Both, so it does not read as a bug."""
    client, db_path = live_app
    put(db_path, "AreaD", age_s=1)
    observed = datetime.now(timezone.utc) - timedelta(seconds=1)
    start = observed - timedelta(days=2, minutes=30)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE current_state SET state_start=?, state_elapsed_s=? WHERE device_id='AreaD'",
                   (start.isoformat(), 3 * 3600 + 6 * 60))
    h = re.sub(r"\s+", " ", client.get("/live").get_data(as_text=True))
    assert f"<strong>3 h 06 min</strong> counted · since {weekday_clock(start)} (2 days ago)" in h


def test_a_short_state_reads_simply(live_app):
    client, db_path = live_app
    put(db_path, "AreaE", age_s=1)
    observed = datetime.now(timezone.utc) - timedelta(seconds=1)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE current_state SET state_start=?, state_elapsed_s=? WHERE device_id='AreaE'",
                   ((observed - timedelta(minutes=90)).isoformat(), 90 * 60))
    h = re.sub(r"\s+", " ", client.get("/live").get_data(as_text=True))
    assert re.search(r"<strong>1 h 30 min</strong> in this state · since \d\d:\d\d", h) and "counted ·" not in h


def test_the_note_under_the_status_bar_is_one_line(live_app):
    client, db_path = live_app
    put(db_path, "FR8", age_s=1)
    h = client.get("/live").get_data(as_text=True)
    note = re.search(r'<p class=note title="[^"]+">(.*?)</p>', h, re.S).group(1)
    assert re.sub(r"\s+", " ", note).strip() == "Updates every 5 s · stale after 15 s · no piece counts. Details in the footer."


def test_the_page_updates_in_place_without_reloading(live_app, tmp_path):
    """A real server, a real browser. The page is handed over showing AreaD
    "Down"; the table already says "Running". After its first poll the page
    must show Running, current, with the marker alive - and the page itself
    must have been requested once only, so nothing was reloaded."""
    import shutil
    import subprocess
    from werkzeug.serving import make_server

    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not edge.exists() and not shutil.which("msedge"):
        pytest.skip("no Edge on this machine to run the page's JavaScript")
    client, db_path = live_app
    app = client.application
    put(db_path, "AreaD", age_s=1)
    first = client.get("/live").get_data(as_text=True)              # shows Down
    with sqlite3.connect(db_path) as db:                            # meanwhile the writer has moved on
        db.execute("UPDATE current_state SET process_state='running', process_state_label='Running', "
                   "performance_impact='run', process_state_reason=NULL, process_state_reason_display_name=NULL, "
                   "observed_at=? WHERE device_id='AreaD'", (datetime.now(timezone.utc).isoformat(),))
    served = {"page": 0}

    def wsgi(environ, start_response):
        if environ["PATH_INFO"] == "/page":
            served["page"] += 1
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [first.encode("utf-8")]
        return app(environ, start_response)
    server = make_server("127.0.0.1", 0, wsgi, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        dom = subprocess.run([str(edge), "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'edge'}",
                              "--virtual-time-budget=7000", "--dump-dom",
                              f"http://127.0.0.1:{server.server_port}/page"],
                             capture_output=True, text=True, encoding="utf-8", timeout=120).stdout
    finally:
        server.shutdown()
    panel = dom[dom.index('data-device="AreaD"') - 40:]
    panel = panel[:panel.index("</section>")]
    assert ">Running</p>" in panel and ">Down</p>" not in panel and "imp-run" in panel
    assert 'class="lpanel"' in panel                                 # current, not stale
    assert int(re.search(r'<body[^>]*data-polls="(\d+)"', dom).group(1)) >= 1
    assert re.search(r'class="lpoll[^"]*"[^>]*>updating<', dom)
    assert served["page"] == 1                                       # no reload


def test_polling_pauses_while_hidden_and_reads_at_once_when_shown(live_app, tmp_path):
    """Headless Edge is always "visible", so the test server injects a
    script that reports the tab hidden until 6 s, then shown. No poll may
    reach the server while hidden (the 5 s poll is skipped); one must arrive
    as soon as it is shown, without waiting for the next 5 s."""
    import shutil
    import subprocess
    from werkzeug.serving import make_server

    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not edge.exists() and not shutil.which("msedge"):
        pytest.skip("no Edge on this machine to run the page's JavaScript")
    client, db_path = live_app
    app = client.application
    put(db_path, "AreaD", age_s=1)
    hide = ("<script>window.__hidden = true;"
            "Object.defineProperty(document, 'hidden', {configurable: true, get: () => window.__hidden});"
            "setTimeout(() => { window.__hidden = false; document.dispatchEvent(new Event('visibilitychange')); }, 6000);"
            "</script>")
    page = client.get("/live").get_data(as_text=True).replace("<div class=wrap>", hide + "<div class=wrap>", 1)
    calls = []

    def wsgi(environ, start_response):
        if environ["PATH_INFO"] == "/page":
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [page.encode("utf-8")]
        if environ["PATH_INFO"] == "/api/live":
            calls.append(environ["PATH_INFO"])
        return app(environ, start_response)
    server = make_server("127.0.0.1", 0, wsgi, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        polled = []
        for budget in (5500, 7000):
            calls.clear()
            subprocess.run([str(edge), "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'edge'}",
                            f"--virtual-time-budget={budget}", "--dump-dom", f"http://127.0.0.1:{server.server_port}/page"],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
            polled.append(len(calls))
    finally:
        server.shutdown()
    assert polled[0] == 0                # hidden: the 5 s poll did not go out
    assert polled[1] >= 1                # shown at 6 s: read at once, before the next 5 s poll was due (10 s)


def test_days_ago_rounds_to_the_nearest_day(live_app):
    """AreaB, maintenance since Fri 10:27, read Wed 09:48: 4 days 23 h of wall clock reads "5 days ago"."""
    client, db_path = live_app
    put(db_path, "AreaB", age_s=1)
    observed = datetime.now(timezone.utc) - timedelta(seconds=1)
    start = observed - timedelta(days=4, hours=23)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE current_state SET state_start=?, state_elapsed_s=? WHERE device_id='AreaB'",
                   (start.isoformat(), 47 * 3600))
    h = re.sub(r"\s+", " ", client.get("/live").get_data(as_text=True))
    assert f"counted · since {weekday_clock(start)} (5 days ago)" in h
