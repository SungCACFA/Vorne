"""Products page arithmetic: derived ICT, ratios, SAP units, per-piece gating,
editing and CSV. Expected values are literals derived independently from
the raw fixtures (see test_counts_preserved for the method).

FR8 ESTRADIOL, runs starting in September 2026 production days: 5 runs,
run 232,733.922768 s, 211,627 cycles, ideal 79,883.625 s, good 395,213.
Its derived ICT on FR8: median 0.37701477289352836 over 88 runs of >200
cycles (mean would be 0.37717...). OTRIVIN across the fleet: median
0.9301384408908945, mean 1.487008... - so a mean-for-median swap is
unmistakable there.
"""

import csv
import io
import re
import sqlite3

import pytest

from conftest import client_for
from webapp import data as D

ACTUAL_S_PER_CYCLE = 232733.92276800002 / 211627.0       # 1.0997364...
FR8_ESTRADIOL_ICT = 0.37701477289352836


def standards_row(html: str, part: str, dev: str) -> list[str]:
    table = html[html.index("<h2>Standards and variance"):html.index("<h2>Part runs")]
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
        th = re.search(r"<th>(.*?)</th>", row, re.S)
        if not th or re.sub(r"<[^>]+>", " ", th.group(1)).split()[0] != part:
            continue
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if cells[0] == dev:
            return cells
    raise AssertionError(f"no row for {part} on {dev}")


# --------------------------------------------------------------- derived ICT

def test_device_ict_is_the_median_not_the_mean(replayed_cache):
    got = D.device_ict(replayed_cache)[("FR8", "ESTRADIOL")]
    assert got["ict"] == pytest.approx(FR8_ESTRADIOL_ICT, rel=1e-12)
    assert got["n"] == 88
    assert got["sd"] == pytest.approx(0.0012064973917539441, rel=1e-9)


def test_part_standard_ict_is_the_median_not_the_mean(replayed_cache):
    v = sqlite3.connect(replayed_cache).execute(
        "SELECT ideal_cycle_time, sample_runs FROM part_standard WHERE part='OTRIVIN'").fetchone()
    assert v[0] == pytest.approx(0.9301384408908945, rel=1e-12)     # the mean would be 1.487
    assert v[1] == 11


def test_unstable_flag_follows_the_spread(replayed_cache):
    std = D.standards(replayed_cache)
    assert std["OTRIVIN"]["ict_unstable"] == 1        # sd ~0.67 across lines
    assert std["ESTRADIOL"]["ict_unstable"] == 0      # sd 0.0012


# ------------------------------------------------------------ the page row

def test_estradiol_fr8_september_row(app_client):
    html = app_client.get("/products?g=month&p=2026-09").get_data(as_text=True)
    cells = standards_row(html, "ESTRADIOL", "FR8")
    line, runs, ict, epc, sap, actual, vs_ict, vs_sap, run_h, earned_h, variance, per_piece = cells
    assert (line, runs) == ("FR8", "5")
    assert ict == "0.377"
    assert actual == "1.100"
    assert vs_ict == "2.92×"            # actual / derived; inverted would read 0.34×
    assert run_h == "64.6" and earned_h == "22.2"
    assert variance == "42.5"           # (run - ideal) / 3600
    assert vs_sap == "no SAP standard"
    assert per_piece == "counts untrusted"
    assert "not set" in html            # pieces per cycle placeholder, live mode


def test_vs_derived_ratio_direction(app_client):
    """Slower than standard reads above 1 and is marked 'over'."""
    html = app_client.get("/products?g=month&p=2026-09").get_data(as_text=True)
    assert f"{ACTUAL_S_PER_CYCLE / FR8_ESTRADIOL_ICT:.2f}×" == "2.92×"
    assert '<span class="over">2.92×</span>' in html


# -------------------------------------------------------------- SAP units

@pytest.mark.parametrize("unit,value", [("s_per_piece", 1.5), ("pieces_per_hour", 2400.0),
                                        ("hours_per_thousand", 1.5 / 3.6)])
def test_display_conversion_from_seconds_per_piece(unit, value):
    assert D.from_s_per_piece(1.5, unit) == pytest.approx(value)


@pytest.mark.parametrize("unit,value", [("s_per_piece", 1.5), ("pieces_per_hour", 2400.0),
                                        ("hours_per_thousand", 0.4166666666666667)])
def test_stored_value_normalises_to_seconds_per_piece(cache_copy, unit, value):
    D.save_standard(cache_copy, "ESTRADIOL", sap=value, unit=unit)
    got = sqlite3.connect(cache_copy).execute(
        "SELECT sap_standard, sap_unit, sap_s_per_piece FROM part_standard WHERE part='ESTRADIOL'").fetchone()
    assert got[:2] == (value, unit)
    assert got[2] == pytest.approx(1.5)


def test_sap_comparison_end_to_end(cache_copy, tmp_path):
    c = client_for(cache_copy, tmp_path)
    url = "/products?g=month&p=2026-09"
    assert c.post("/api/part_standard", json={"part": "ESTRADIOL", "field": "sap", "value": "2400",
                                              "unit": "pieces_per_hour"}).get_json() == {"ok": True}
    cells = standards_row(c.get(url).get_data(as_text=True), "ESTRADIOL", "FR8")
    assert cells[7] == "pieces/cycle not configured"       # SAP set, pieces per cycle not
    assert c.post("/api/part_standard", json={"part": "ESTRADIOL", "field": "epc", "value": "2"}).get_json()["ok"]
    html = c.get(url).get_data(as_text=True)
    cells = standards_row(html, "ESTRADIOL", "FR8")
    # SAP 2400/h = 1.5 s/piece; x 2 pieces/cycle = 3.0 s/cycle; actual 1.0997 -> 0.37x
    assert 'class=epc data-part="ESTRADIOL" value="2"' in html
    assert 'class=sap data-part="ESTRADIOL" value="1.5"' in html     # shown in the page unit, s/piece
    assert cells[7] == "0.37×"
    # earned at SAP: 211,627 cycles x 3.0 s = 176.4 h; the page shows >=100 h without decimals
    assert "Earned h, SAP" in html and cells[10] == "176"


# --------------------------------------------------------- per-piece gating

def test_per_piece_appears_only_for_trusted_epochs(cache_copy, tmp_path):
    db = sqlite3.connect(cache_copy)
    db.execute("UPDATE device_config_epoch SET counts_trusted=1 WHERE device_id='FR8' AND config_epoch=2")
    db.commit()
    html = client_for(cache_copy, tmp_path).get("/products?g=month&p=2026-09").get_data(as_text=True)
    assert standards_row(html, "ESTRADIOL", "FR8")[-1] == f"{232733.92276800002 / 395213.0:.3f}"   # 0.589
    assert standards_row(html, "ROGAINE_5b_TRIPLES", "AreaE")[-1] == "counts untrusted"


# ------------------------------------------------------- edit API checks

@pytest.mark.parametrize("body,status", [
    ({"part": "ESTRADIOL", "field": "sap", "value": "-3", "unit": "s_per_piece"}, 400),
    ({"part": "ESTRADIOL", "field": "sap", "value": "abc", "unit": "s_per_piece"}, 400),
    ({"part": "ESTRADIOL", "field": "sap", "value": "inf", "unit": "s_per_piece"}, 400),
    ({"part": "ESTRADIOL", "field": "sap", "value": "1", "unit": "furlongs"}, 400),
    ({"part": "ESTRADIOL", "field": "colour", "value": "1"}, 400),
    ({"part": "", "field": "sap", "value": "1", "unit": "s_per_piece"}, 400),
    ({"part": "ESTRADIOL", "field": "epc", "value": "0"}, 400),
])
def test_api_rejects_bad_input(cache_copy, tmp_path, body, status):
    r = client_for(cache_copy, tmp_path).post("/api/part_standard", json=body)
    assert r.status_code == status and r.get_json()["ok"] is False
    assert sqlite3.connect(cache_copy).execute(
        "SELECT sap_standard, expected_count_per_cycle FROM part_standard WHERE part='ESTRADIOL'").fetchone() == (None, None)


def test_blank_clears_a_value(cache_copy, tmp_path):
    c = client_for(cache_copy, tmp_path)
    c.post("/api/part_standard", json={"part": "ESTRADIOL", "field": "epc", "value": "3"})
    c.post("/api/part_standard", json={"part": "ESTRADIOL", "field": "epc", "value": ""})
    assert sqlite3.connect(cache_copy).execute(
        "SELECT expected_count_per_cycle FROM part_standard WHERE part='ESTRADIOL'").fetchone()[0] is None


def test_edits_never_touch_derived_columns(cache_copy, tmp_path):
    before = sqlite3.connect(cache_copy).execute(
        "SELECT ideal_cycle_time, ict_stddev, sample_runs FROM part_standard WHERE part='OTRIVIN'").fetchone()
    client_for(cache_copy, tmp_path).post("/api/part_standard", json={"part": "OTRIVIN", "field": "sap",
                                                                      "value": "2", "unit": "s_per_piece"})
    after = sqlite3.connect(cache_copy).execute(
        "SELECT ideal_cycle_time, ict_stddev, sample_runs FROM part_standard WHERE part='OTRIVIN'").fetchone()
    assert before == after


# ------------------------------------------------------------------- CSV

def test_csv_round_trip_restores_hand_entered_values(cache_copy, tmp_path):
    c = client_for(cache_copy, tmp_path)
    c.post("/api/part_standard", json={"part": "ESTRADIOL", "field": "sap", "value": "2400", "unit": "pieces_per_hour"})
    c.post("/api/part_standard", json={"part": "ESTRADIOL", "field": "epc", "value": "2"})
    exported = c.get("/products/standards.csv").get_data()
    db = sqlite3.connect(cache_copy)
    db.execute("UPDATE part_standard SET sap_standard=NULL, sap_unit=NULL, expected_count_per_cycle=NULL")
    db.commit()
    r = c.post("/products/import", data={"u": "s_per_piece", "file": (io.BytesIO(exported), "x.csv")},
               content_type="multipart/form-data")
    assert r.status_code == 302 and "err=" not in r.headers["Location"]
    got = sqlite3.connect(cache_copy).execute(
        "SELECT sap_standard, sap_unit, sap_s_per_piece, expected_count_per_cycle FROM part_standard "
        "WHERE part='ESTRADIOL'").fetchone()
    assert got == (2400.0, "pieces_per_hour", 1.5, 2.0)


def test_csv_import_reports_bad_lines_and_loads_the_rest(cache_copy, tmp_path):
    body = b"part,value,unit\nADVIL,0.5,s_per_piece\nSOFDRA,abc,s_per_piece\n,1,s_per_piece\nOMNARIS,2,parsecs\n"
    r = client_for(cache_copy, tmp_path).post(
        "/products/import", data={"u": "s_per_piece", "file": (io.BytesIO(body), "x.csv")},
        content_type="multipart/form-data")
    loc = r.headers["Location"]
    assert "imported=1" in loc
    assert "SOFDRA" in loc and "line+4" in loc and "parsecs" in loc
    db = sqlite3.connect(cache_copy)
    assert db.execute("SELECT sap_standard FROM part_standard WHERE part='ADVIL'").fetchone()[0] == 0.5
    assert db.execute("SELECT sap_standard FROM part_standard WHERE part='SOFDRA'").fetchone()[0] is None


def test_csv_export_columns(app_client):
    rows = list(csv.DictReader(io.StringIO(app_client.get("/products/standards.csv").get_data(as_text=True))))
    otrivin = next(r for r in rows if r["part"] == "OTRIVIN")
    assert float(otrivin["derived_ideal_cycle_time_s_per_cycle"]) == pytest.approx(0.9301384408908945)
