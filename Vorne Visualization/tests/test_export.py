"""The static HTML export: self-contained, complete, read-only, and showing
the same figures as the app. (~3 min: it renders every page, including
40 product pages, for every period.)"""

import base64
import gzip
import json
import re

import pytest

from test_aggregation import FLEET_DISPLAY


@pytest.fixture(scope="module")
def export(replayed_cache, tmp_path_factory):
    from webapp.export import build

    d = tmp_path_factory.mktemp("export")
    cfg = d / "config.yaml"
    cfg.write_text(f"cache_path: {replayed_cache.as_posix()}\n", encoding="utf-8")
    earlier = d / "danbro_2026-01-01_0000.html"       # an older snapshot in the same folder
    earlier.write_text("old", encoding="utf-8")
    out = build(cfg, d / "danbro_2026-10-07_0000.html", log=lambda *_: None)
    html = out.read_text(encoding="utf-8")
    packed = re.search(r'const PACKED = "([^"]+)"', html).group(1)
    bundle = json.loads(gzip.decompress(base64.b64decode(packed)))
    # Rebuild every fragment from its chunks, as the shell does: every check below then also
    # checks the packing round trip (build() refuses to write a file that fails it).
    from webapp.export import unpack
    bundle["strings"] = [unpack(bundle["chunks"], s) for s in bundle["seqs"]]
    bundle["_path"] = out
    bundle["_earlier_left"] = earlier.exists()
    return html, bundle


def fragment(bundle, page, gran, period):
    f = bundle["frags"][page][gran][period]
    return [bundle["strings"][i] for i in f[:5]], f[5]


def test_no_external_assets(export):
    html, bundle = export
    shell = re.sub(r'const PACKED = "[^"]+"', "", html)
    assert not re.search(r'(src|href)\s*=\s*"?https?://', shell)
    assert "@import" not in shell and "fonts.googleapis" not in html
    assert not any(re.search(r'(src|href)\s*=\s*"?https?://', s) for s in bundle["strings"])


def test_every_page_and_period_is_present(export):
    _, b = export
    assert [p for p, _ in b["pages"]] == ["overview", "AreaB", "AreaD", "AreaE", "FR8", "products"]
    for page, _ in b["pages"]:
        for gran, _ in b["grans"]:
            assert set(b["frags"][page][gran]) == {k for k, _ in b["periods"][gran]}


def test_september_matches_the_locked_figures(export):
    _, b = export
    (_, _, _, body, foot), ndays = fragment(b, "overview", "month", "2026-09")
    cells = [re.sub(r"<[^>]+>", "", c).strip()
             for c in re.findall(r"<td>(.*?)</td>", body.split("<tfoot>")[1].split("</tfoot>")[0], re.S)]
    assert tuple(cells) == FLEET_DISPLAY
    assert ndays == 22
    assert "Calendar use is a different measure" in foot


def test_read_only(export):
    _, b = export
    assert not any("<input" in s or "fetch(" in s for s in b["strings"])
    (_, _, _, body, _), _ = fragment(b, "products", "month", "2026-09")
    assert "Read-only snapshot" in body and "not configured" in body


def test_every_quality_panel_survives(export):
    _, b = export
    (_, head, panel, body, _), _ = fragment(b, "overview", "month", "2026-09")
    assert "run open for" in re.sub(r"<[^>]+>", "", head)   # open-run alert ("run open" is a definition link)
    assert "Production days from" in panel and "live app" in panel   # exclusion, snapshot wording
    assert "Piece counts are not yet reliable" in body
    (_, _, _, line, _), _ = fragment(b, "FR8", "day", "2026-07-03")
    assert "over 100% on a" in line and "CHANGEOVER" in line.upper()


def test_stamped(export):
    html, b = export
    assert b["stamp"]["exported"] and b["stamp"]["refreshed"] != "unknown"
    assert "Snapshot exported" in html


def test_product_pages_are_in_the_export(export):
    _, b = export
    keys = [k for k, _ in b["products"]]
    assert len(keys) == 40 and "part:OTRIVIN" in keys and "part:ESTRADIOL" in keys
    assert all(set(b["frags"][k]["month"]) == {p for p, _ in b["periods"]["month"]} for k in keys)
    (sub, _, _, body, _), _ = fragment(b, "part:OTRIVIN", "ytd", "2026")
    assert sub.startswith("OTRIVIN")
    assert "<svg class=runs" in body and "<svg class=ict" in body and "Into it, from" in body
    (_, _, _, line, _), _ = fragment(b, "AreaB", "ytd", "2026")
    assert "<select id=part" not in line                      # the product filter is live-app only
    assert 'href="/product/OTRIVIN?' in line


def test_export_follows_product_links(export):
    html, _ = export
    assert "u.pathname.startsWith('/product/') ? 'part:' + decodeURIComponent(u.pathname.slice(9))" in html


# ------------------------------------------------------------ the packing

@pytest.mark.parametrize("text", ["", "no boundary at all", "<tr><td>1</td></tr><tr><td>1</td></tr>\n",
                                  "<svg><rect x=1 /><rect x=1 /><text>a</text></svg>\n\n<g></g></a>"])
def test_pack_round_trips_awkward_strings(text):
    from webapp.export import pack, unpack

    table, seqs = pack([text, text + "x", "y" + text])
    assert [unpack(table, s) for s in seqs] == [text, text + "x", "y" + text]


def test_repeated_rows_are_stored_once():
    from webapp.export import pack

    row = "<tr><td>OMNARIS</td><td>12.5</td></tr>"
    table, seqs = pack([row * 3, "<table>" + row * 2])
    assert table.count(row) == 1


def test_snapshot_fits_an_email(export):
    """6.1 MB before chunk packing; the recording packs to well under 3 MB.
    Email attachments grow by a third in transit (MIME base64)."""
    path = export[1]["_path"]
    assert path.stat().st_size < 3_000_000


def test_the_browser_rebuilds_pages_as_python_does(export, tmp_path):
    """The JavaScript decoder runs only in a browser. Open the snapshot in
    headless Edge at a line page and a product page, and compare what it
    drew with the fragment unpacked here."""
    import shutil
    import subprocess
    from pathlib import Path

    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not edge.exists() and not shutil.which("msedge"):
        pytest.skip("no Edge on this machine to run the snapshot's JavaScript")
    _, b = export
    for page, gran, period in (("AreaB", "ytd", "2026"), ("part:OTRIVIN", "month", "2026-06")):
        dom = subprocess.run(
            [str(edge), "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'edge'}",
             "--virtual-time-budget=15000", "--dump-dom",
             b["_path"].as_uri() + "#" + "/".join(x.replace(":", "%3A") for x in (page, gran, period))],
            capture_output=True, text=True, encoding="utf-8", timeout=120).stdout
        main = dom[dom.index('<main id="main">'):dom.index("</main>")]
        (_, head, panel, body, _), _ = fragment(b, page, gran, period)
        src = head + panel + body
        for tag in ("<tr", "<rect", "<circle", "<text", "<h2"):
            assert main.count(tag) == src.count(tag), (page, tag)
        assert main.count("<rect") > 50                   # a real page, not an empty one


def test_the_build_deleted_the_earlier_export(export):
    """The fixture put an earlier export beside the new one; build() removed it."""
    _, b = export
    assert b["_earlier_left"] is False
    assert b["_path"].exists()


def test_the_export_has_the_same_sticky_band(export, tmp_path):
    """Opened at a line page, the snapshot's nav and period bar stick together under 90 px, as in the app."""
    from test_sticky import measure

    html, _ = export
    assert html.count("// The sticky band") == 1
    m = measure(html.replace("location.hash.slice(1)", "'AreaB/month/2026-09'"), 1280, tmp_path)
    assert m["navTop"] == 0 and abs(m["barTop"] - m["navH"]) < 1 and m["band"] < 90 and m["scrolled"] is True


def test_the_export_carries_tooltips_and_definitions(export):
    html, b = export
    assert html.count("// Chart tooltips and measure definitions.") == 1
    (_, _, _, body, foot), _ = fragment(b, "AreaB", "month", "2026-09")
    assert '<a class=term href="#def-available" data-def="available">' in body
    assert '<div class=def id="def-available" data-def-body="available">' in foot     # the panel's text travels with the page
