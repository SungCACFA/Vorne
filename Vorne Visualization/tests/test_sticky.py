"""The sticky band: the nav, and directly under it the page's bar (period
controls; on /live the status line). The masthead scrolls away; the band stays
under 90 px on a laptop; on a phone only the nav sticks; in-page jumps land
below it; the export behaves the same.

Geometry is measured in headless Edge (skipped where Edge is missing). In its
--dump-dom mode the browser renders no frames, so scroll events do not always
fire; the probe scrolls, then dispatches the scroll event a real browser would.
Layout queries (getBoundingClientRect) are real either way.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
CSS = (Path(__file__).parent.parent / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
PAGES = ["/?g=month&p=2026-09", "/line/AreaB?g=month&p=2026-09", "/products?g=month&p=2026-09",
         "/product/OTRIVIN?g=ytd&p=2026"]

PROBE = """<script>
addEventListener('load', () => setTimeout(() => {
  scrollTo(0, 1500); dispatchEvent(new Event('scroll'));
  setTimeout(() => {
    const nav = document.querySelector('nav'), bar = document.querySelector('[data-bar]');
    const n = nav.getBoundingClientRect(), b = bar.getBoundingClientRect();
    const sticky = getComputedStyle(bar).position === 'sticky';
    const out = {navTop: n.top, navH: n.height, barTop: b.top, barSticky: sticky,
                 band: sticky ? b.bottom : n.bottom,
                 scrolled: document.documentElement.classList.contains('scrolled')};
    const hours = document.getElementById('hours');
    if (hours) { hours.scrollIntoView(); out.jumpTop = hours.getBoundingClientRect().top; }
    scrollTo(0, 0); dispatchEvent(new Event('scroll'));
    out.scrolledAtTop = document.documentElement.classList.contains('scrolled');
    document.body.dataset.band = JSON.stringify(out);
  }, 800);
}, 300));
</script>"""


def measure(html: str, width: int, tmp_path: Path) -> dict:
    if not EDGE.exists() and not shutil.which("msedge"):
        pytest.skip("no Edge on this machine to lay the page out")
    html = html.replace('<link rel=stylesheet href="/static/style.css">', f"<style>{CSS}</style>")
    html = re.sub(r'<link href="https://fonts[^>]+>', "", html) + PROBE
    f = tmp_path / f"page{width}.html"
    f.write_text(html, encoding="utf-8")
    # Under heavy load headless Edge can hand back the page before the probe has run (once, in a suite
    # run 60% slower than usual). That - no measurement at all - is retried once with more time; a
    # measurement that is wrong is never retried.
    for budget in (5000, 15000):
        dom = subprocess.run([str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                              f"--user-data-dir={tmp_path / 'edge'}", f"--window-size={width},900",
                              f"--virtual-time-budget={budget}", "--dump-dom", f.as_uri()],
                             capture_output=True, text=True, encoding="utf-8", timeout=180).stdout
        m = re.search(r'data-band="([^"]+)"', dom)
        if m:
            return json.loads(m.group(1).replace("&quot;", '"'))
    pytest.fail("Edge returned the page without running the layout probe, twice")


# ------------------------------------------------------------- markup

@pytest.mark.parametrize("url", PAGES)
def test_the_bar_sits_directly_under_the_nav(app_client, url):
    h = app_client.get(url).get_data(as_text=True)
    assert re.search(r"</nav>\s*<form class=ctrl data-bar ", h)
    assert h.index("<form class=ctrl data-bar") < h.index("<!--head-->")       # alerts come after the band
    assert h.count('<script>\n// The sticky band') == 1


def test_live_sticks_its_status_line_instead(app_client):
    h = app_client.get("/live").get_data(as_text=True)
    assert re.search(r"</nav>\s*<div class=liveline data-bar data-oldest", h)
    assert "<form class=ctrl" not in h


def test_the_rules(app_client):
    assert re.search(r"nav\{position:sticky;top:0;", CSS)
    assert re.search(r"\[data-bar\]\{position:sticky;top:var\(--navh\);", CSS)
    assert "scroll-padding-top:calc(var(--navh) + var(--barh) + 12px)" in CSS
    narrow = CSS[CSS.index("@media(max-width:700px){\n  [data-bar]"):]
    assert narrow.startswith("@media(max-width:700px){\n  [data-bar]{position:static")


# ----------------------------------------------------------- geometry

@pytest.fixture
def live_client(replayed_cache, tmp_path):
    """/live with a reading from every line, on a copy of the recorded cache."""
    import sqlite3
    from datetime import datetime, timezone

    from test_live import DEVICES, reading
    from webapp import create_app
    from xlpipe import live as W

    db = tmp_path / "live.db"
    shutil.copy(replayed_cache, db)
    with sqlite3.connect(db) as c:
        c.executescript(W.DDL)
        for d in DEVICES:
            W.write_ok(c, d, reading(d), datetime.now(timezone.utc).isoformat())
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"cache_path: {db.as_posix()}\ndevices:\n" + "".join(f"  - {{id: {d}, host: x}}\n" for d in DEVICES),
                   encoding="utf-8")
    return create_app(cfg).test_client()


@pytest.mark.parametrize("url", PAGES + ["/live"])
def test_on_a_laptop_the_band_holds_nav_and_bar_under_90px(app_client, live_client, url, tmp_path):
    client = live_client if url == "/live" else app_client
    m = measure(client.get(url).get_data(as_text=True), 1280, tmp_path)
    assert m["navTop"] == 0 and m["barSticky"] and abs(m["barTop"] - m["navH"]) < 1   # bar directly under the nav
    assert m["band"] < 90, m
    assert m["scrolled"] is True and m["scrolledAtTop"] is False                      # shadow only once floating


@pytest.mark.parametrize("url", ["/line/AreaB?g=month&p=2026-09", "/live"])
def test_on_a_phone_only_the_nav_sticks(app_client, live_client, url, tmp_path):
    client = live_client if url == "/live" else app_client
    m = measure(client.get(url).get_data(as_text=True), 390, tmp_path)
    assert m["navTop"] == 0 and not m["barSticky"] and m["barTop"] < 0               # bar scrolled away
    assert m["band"] == m["navH"] < 50                                               # nav on one row
    assert m["scrolled"] is True


def test_an_in_page_jump_lands_below_the_band(app_client, tmp_path):
    """Clicking a heatmap day scrolls to the hour table; it must not hide under the band."""
    m = measure(app_client.get("/line/AreaB?g=month&p=2026-09").get_data(as_text=True), 1280, tmp_path)
    assert m["band"] <= m["jumpTop"] <= m["band"] + 20


def test_the_export_shell_has_the_same_band():
    from webapp import export

    assert "<nav id=nav></nav>\n<div class=ctrl data-bar>" in export.SHELL and "/*STICKY*/" in export.SHELL

