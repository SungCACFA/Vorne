"""Flask app over the local cache.

    python -m webapp                 http://127.0.0.1:5000
    python -m webapp --port 8080

Reads cache/xl_cache.db (path from config.yaml) read-only on every request,
so a refresh shows up on the next page load. Nothing here contacts the
devices or any outside service.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import yaml
from flask import Flask, abort, render_template, request
from markupsafe import Markup, escape

from . import data as D

ROOT = Path(__file__).resolve().parent.parent

# performance_impact -> colour key in the stylesheet
IMPACT = {"run": "run", "unplanned_stop": "unp", "planned_stop": "pln", "not_scheduled": "nsc"}


@dataclass
class Window:
    """What the controls selected: granularity, period, and which days count."""

    gran: str
    widen: list[str]
    fleet_from: int | None
    included: dict          # (device, day) -> Totals, inside the window
    periods: list[tuple[str, str]]
    period: str | None
    period_label: str

    def in_period(self, day: int) -> bool:
        return D.period_key(self.gran, day) == self.period

    def days(self, dev: str | None = None) -> list[int]:
        return sorted({d for (v, d) in self.included if (dev is None or v == dev) and self.in_period(d)})

    def query(self, **override) -> str:
        """Query string preserving the current selection, for nav links."""
        q = [("g", self.gran)] + ([("p", self.period)] if self.period else [])
        q += [("widen", w) for w in self.widen]
        q += list(override.items())
        return "?" + urlencode(q)


def select_window(snap: D.Snapshot, args) -> Window:
    gran = args.get("g", "month")
    if gran not in dict(D.GRANULARITIES):
        gran = "month"
    widen = [d for d in args.getlist("widen") if d in snap.devices]
    fleet_from = snap.fleet_from_day
    start = {dev: (snap.history[dev].first_usable_day if dev in widen or fleet_from is None else fleet_from)
             for dev in snap.devices}
    included = {k: v for k, v in snap.daily.items() if k[1] >= (start[k[0]] or 0)}
    days = sorted({day for _, day in included})
    if gran == "ytd":
        keys = [D.period_key("ytd", days[-1])] if days else []
    else:
        keys = sorted({D.period_key(gran, d) for d in days}, reverse=True)
    period = args.get("p")
    if period not in keys:
        period = keys[0] if keys else None
    return Window(gran, widen, fleet_from, included,
                  [(k, D.period_label(gran, k)) for k in keys], period,
                  D.period_label(gran, period) if period else "no data")


def totals(win: Window, dev: str) -> tuple[D.Totals, list[tuple[int, float | None]]]:
    t, spark = D.Totals(), []
    for day in win.days(dev):
        v = win.included[(dev, day)]
        t.add(v)
        spark.append((day, v.availability))
    return t, spark


def create_app(config_path: str | Path = ROOT / "config.yaml") -> Flask:
    app = Flask(__name__)
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    cache = Path(cfg.get("cache_path", "cache/xl_cache.db"))
    app.config["CACHE_PATH"] = cache if cache.is_absolute() else ROOT / cache

    @app.template_filter("pc")
    def pc(v):
        return "—" if v is None else f"{v * 100:.1f}%"

    @app.template_filter("hrs")
    def hrs(seconds):
        h = (seconds or 0) / 3600
        return f"{h:,.0f}" if h >= 100 else f"{h:.1f}"

    @app.template_filter("mins")
    def mins(seconds):
        return f"{(seconds or 0) / 60:.0f}"

    @app.template_filter("local")
    def local(ts):
        if not ts or not hasattr(ts, "astimezone"):
            return "—"
        t = ts.astimezone(D.TZ)
        return f"{D.nice(t.date())} {t:%H:%M}"

    @app.template_filter("clock")
    def clock(ts):
        return ts.astimezone(D.TZ).strftime("%H:%M") if ts else "—"

    @app.template_filter("pday")
    def pday(n):
        return D.nice(D.production_day(n)) if n is not None else "—"

    @app.template_filter("words")
    def words(key):
        return (key or "—").replace("_", " ")

    @app.template_filter("multi")
    def multi(value, kind="changeover"):
        """A record spanning a changeover carries every value, comma-joined
        in the device's order (not guaranteed chronological). Show each one,
        and say what it means."""
        if not value or "," not in value:
            return escape(value or "—")
        parts = [escape(p.replace("_", " ") if kind == "shift change" else p) for p in value.split(",")]
        return Markup(f'<span class=multi title="Several values in one record: {kind}">'
                      + '<span class=sep> / </span>'.join(parts)
                      + f'<small>{kind}</small></span>')

    @app.context_processor
    def nav():
        return {"impact_colour": IMPACT}

    def common(snap: D.Snapshot, win: Window, page: str, panel_devices: list[str]) -> dict:
        return {"snap": snap, "win": win, "page": page, "panel_devices": panel_devices,
                "grans": D.GRANULARITIES}

    @app.route("/")
    def overview():
        snap = D.load(app.config["CACHE_PATH"])
        win = select_window(snap, request.args)
        rows, fleet, sparks = [], D.Totals(), {}
        for dev in snap.devices:
            t, spark = totals(win, dev)
            if t.records:
                rows.append((dev, t))
                fleet.add(t)
                sparks[dev] = spark
        rows.sort(key=lambda r: -1 if r[1].availability is None else r[1].availability, reverse=True)
        return render_template("overview.html", **common(snap, win, "overview", snap.devices),
                               rows=rows, fleet=fleet, sparks=sparks, period_days=win.days())

    @app.route("/line/<dev>")
    def line(dev: str):
        snap = D.load(app.config["CACHE_PATH"])
        if dev not in snap.devices:
            abort(404)
        win = select_window(snap, request.args)
        t, spark = totals(win, dev)
        days = win.days(dev)
        ctx = {"dev": dev, "t": t, "day_spark": spark, "period_days": days}
        if days:
            lo, hi, path = days[0], days[-1], app.config["CACHE_PATH"]
            acts = D.activities(path, dev, lo, hi)
            ctx.update(
                acts=acts, acts_total=sum(a["seconds"] for a in acts) or 1.0,
                unplanned=D.reasons(path, dev, lo, hi, "unplanned_stop"),
                planned=D.reasons(path, dev, lo, hi, "planned_stop"),
                hours=D.hours(path, dev, lo, hi),
            )
            ctx["changeovers"] = sum(1 for h in ctx["hours"] if h["part"] and "," in h["part"])
            ctx["bad_perf"] = sum(1 for h in ctx["hours"] if h["performance"] is not None and h["performance"] > 1)
        return render_template("line.html", **common(snap, win, dev, [dev]), **ctx)

    return app
