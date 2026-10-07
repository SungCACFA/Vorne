"""Priority 4: the September 2026 overview, locked in.

These are the figures verified by hand against direct SQL on the cache
(Available / Performance / Calendar use / Scheduled h / Down h), here at
full precision so a refactor cannot move them silently - not even in a
decimal place the page does not show.

They are tied to the recording in tests/fixtures. Re-recording means
re-verifying these against SQL before updating them.
"""

import re

import pytest
from werkzeug.datastructures import MultiDict

from webapp import data as D
from webapp import select_window, totals

# device: availability, performance, calendar use, scheduled s, unplanned s, records, full hours
SEPTEMBER = {
    "AreaB": (0.4040687776156672, 0.6420227412778946, 0.2825214946716629, 732342.629803, 278377.593243, 281, 264),
    "AreaD": (0.26025204507937283, 0.722945447864832, 0.3196154345787037, 828443.206428, 151620.627836, 374, 352),
    "AreaE": (0.33545557232272516, 0.36074468800473347, 0.40104777393683366, 1039581.582359, 386938.477972, 374, 352),
    "FR8": (0.3257748819011626, 0.4430834908018003, 0.40169768113530296, 1041273.627845, 329398.721953, 374, 352),
}
FLEET = (0.32873484718647417, 0.519614524107052, 0.35122117129539776, 3641641.046435, 1146335.421004)
# What the old report's full-hours-only method gives for the same period.
FLEET_OLD_UTILISATION = 0.7502884877461077

# As displayed, and as checked by hand against SQL.
SEPTEMBER_DISPLAY = {
    "AreaB": ("40.4%", "64.2%", "28.3%", "203", "77.3"),
    "AreaD": ("26.0%", "72.3%", "32.0%", "230", "42.1"),
    "AreaE": ("33.5%", "36.1%", "40.1%", "289", "107"),
    "FR8": ("32.6%", "44.3%", "40.2%", "289", "91.5"),
}
FLEET_DISPLAY = ("32.9%", "52.0%", "35.1%", "1,012", "318")


@pytest.fixture(scope="module")
def september(replayed_cache):
    snap = D.load(replayed_cache)
    win = select_window(snap, MultiDict({"g": "month", "p": "2026-09"}))
    return snap, win


@pytest.mark.parametrize("dev", sorted(SEPTEMBER))
def test_september_per_line(september, dev):
    _, win = september
    t, _ = totals(win, dev)
    got = (t.availability, t.performance, t.utilization, t.sched, t.unp, t.records, t.full_hours)
    exp = SEPTEMBER[dev]
    assert got[5:] == exp[5:]
    assert got[:5] == pytest.approx(exp[:5], rel=1e-12)


def test_september_fleet(september):
    snap, win = september
    fleet = D.Totals()
    for dev in snap.devices:
        fleet.add(totals(win, dev)[0])
    got = (fleet.availability, fleet.performance, fleet.utilization, fleet.sched, fleet.unp)
    assert got == pytest.approx(FLEET, rel=1e-12)
    assert fleet.utilization_full_hours_only == pytest.approx(FLEET_OLD_UTILISATION, rel=1e-12)


def test_september_page_shows_the_hand_checked_figures(app_client):
    html = app_client.get("/?g=month&p=2026-09").get_data(as_text=True)
    for dev, exp in SEPTEMBER_DISPLAY.items():
        row = re.search(rf'>{dev}</a></th><td class=bar>.*?</td>(.*?)</tr>', html, re.S).group(1)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td>(.*?)</td>", row, re.S)]
        assert tuple(cells) == exp, dev
    foot = html.split("<tfoot>")[1].split("</tfoot>")[0]
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td>(.*?)</td>", foot, re.S)]
    assert tuple(cells) == FLEET_DISPLAY
    assert "calendar use is 35.1%; the old method gives\n75.0%" in html.replace("  ", " ")


def test_default_window_is_the_fleet_window(september):
    snap, _ = september
    assert D.production_day(snap.fleet_from_day).isoformat() == "2026-04-02"
