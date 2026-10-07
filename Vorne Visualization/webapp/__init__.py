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
import csv
import io
import math

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup, escape

from . import charts as C
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

    def link(self, **changes) -> str:
        """Query string with some selections replaced (not appended)."""
        q = {"g": self.gran, "p": self.period, **changes}
        pairs = [(k, v) for k, v in q.items() if v is not None] + [("widen", w) for w in self.widen]
        return "?" + urlencode(pairs)

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
    charts_cfg = cfg.get("charts") or {}
    app.config["CHARTS"] = {
        "frequent_per_day": float(charts_cfg.get("frequent_stops_per_production_day", C.FREQUENT_STOPS_PER_DAY)),
        "long_minutes": float(charts_cfg.get("long_stop_minutes", C.LONG_STOP_MINUTES)),
    }
    app.config["RHYTHM"] = {
        "min_shift": float(charts_cfg.get("rhythm_change_min_shift", C.RHYTHM_MIN_SHIFT)),
        "min_t": float(charts_cfg.get("rhythm_change_min_t", C.RHYTHM_MIN_T)),
    }
    live_cfg = cfg.get("live") or {}
    interval = float(live_cfg.get("interval_seconds", 5))
    app.config["LIVE"] = {"interval": interval,
                          # 20 s by default: four writer cycles, so 5 s writer / 5 s poll jitter never trips it
                          "stale_after": float(live_cfg.get("stale_after_seconds", 4 * interval)),
                          "poll": float(live_cfg.get("poll_seconds", interval)),
                          "devices": [d["id"] for d in cfg.get("devices") or []]}
    app.config["ICT_TOLERANCE"] = float(charts_cfg.get("ict_change_tolerance", D.ICT_TOLERANCE))
    default_rule = D.ImplausibleRule()
    app.config["IMPLAUSIBLE"] = D.ImplausibleRule(
        cap_hours=float(charts_cfg.get("implausible_cap_hours", default_rule.cap_hours)),
        floor_hours=float(charts_cfg.get("implausible_floor_hours", default_rule.floor_hours)),
        multiple=float(charts_cfg.get("implausible_times_median", default_rule.multiple)),
    )

    @app.template_filter("pc")
    def pc(v):
        return "—" if v is None else f"{v * 100:.1f}%"

    # Durations are clamped at 0 for display only. A handful of state events
    # end before they start (suspected clock corrections, listed in
    # data_quality); "-0.0 h" would be noise, not information.
    @app.template_filter("hrs")
    def hrs(seconds):
        h = max(0.0, (seconds or 0) / 3600)
        return f"{h:,.0f}" if h >= 100 else f"{h:.1f}"

    # One decimal at every size, for tables where readers subtract columns
    # (runs: elapsed - run - stopped = not scheduled).
    @app.template_filter("h1")
    def h1(seconds):
        return f"{max(0.0, (seconds or 0) / 3600):,.1f}"

    # Days to scan, hours to subtract: "42.2 d" with "1,015.1 h" beneath.
    @app.template_filter("elapsed")
    def elapsed(seconds):
        s = max(0.0, seconds or 0)
        return Markup(f'{s / 86400:,.1f} d<small class=f>{s / 3600:,.1f} h</small>')

    @app.template_filter("mins")
    def mins(seconds):
        return f"{max(0.0, (seconds or 0) / 60):.0f}"

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

    @app.template_filter("isoday")
    def isoday(n):
        return D.production_day(n).isoformat() if n is not None else ""

    @app.template_filter("weekday_label")
    def weekday_label(iso):
        d = D.date.fromisoformat(iso) if isinstance(iso, str) else iso
        return D.nice(d)

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

    def _part_link(p: str) -> str:
        q = request.args
        keep = [(k, v) for k in ("g", "p") if (v := q.get(k))] + [("widen", w) for w in q.getlist("widen")]
        return (f'<a class=lnk href="{escape(url_for("product", part=p) + ("?" + urlencode(keep) if keep else ""))}">'
                f'{escape(p)}</a>')

    @app.template_filter("partlink")
    def partlink(p):
        """A part's name, linked to its product page (with the current period)."""
        if not p or p in ("—", "null"):
            return escape(p or "—")
        return Markup(_part_link(p))

    @app.template_filter("parts")
    def parts(value):
        """Like multi, for the part column: each part named links to its page."""
        if not value or "," not in value:
            return partlink(value)
        names = [_part_link(p) for p in value.split(",")]
        return Markup('<span class=multi title="Several values in one record: changeover">'
                      + '<span class=sep> / </span>'.join(names) + '<small>changeover</small></span>')

    # ------------------------------------------------------- definitions
    from flask import g

    from . import glossary as GL

    def term(key: str, label: str | None = None) -> Markup:
        """A measure's name, linked to its one definition (webapp/glossary.py). Records the term so
        the page's footer lists it; without scripts the link jumps there."""
        t = GL.definition(key)          # KeyError on an unknown term: fail loudly, never render a dead link
        used = g.setdefault("terms_used", [])
        if key not in used:
            used.append(key)
        return Markup(f'<a class=term href="#def-{key}" data-def="{key}">{escape(label or t.label)}</a>')

    def terms_used() -> list:
        return [(k, GL.DEFS[k]) for k in g.get("terms_used", [])]

    app.jinja_env.globals.update(term=term, terms_used=terms_used, glossary_source=GL.SOURCE)
    # highlight keys: the same helpers the charts use, so a row and its chart element always match
    app.jinja_env.filters.update(hourkey=D.hour_key, reasonkey=D.reason_key)

    @app.context_processor
    def nav():
        # export=1 renders read-only fragments for the static HTML export.
        return {"impact_colour": IMPACT, "export": request.args.get("_export") == "1"}

    def common(snap: D.Snapshot, win: Window, page: str, panel_devices: list[str]) -> dict:
        flagged = D.merge_part_job([r for r in D.open_runs(app.config["CACHE_PATH"])
                                    if r.reasons and r.device_id in panel_devices])
        return {"snap": snap, "win": win, "page": page, "panel_devices": panel_devices,
                "grans": D.GRANULARITIES, "open_alerts": flagged}

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
        path = app.config["CACHE_PATH"]
        all_days = win.days(dev)
        # One product at a time: hour records naming the part (a changeover hour counts for both),
        # and the state events the device itself attributes to it.
        part = (request.args.get("part") or "").strip() or None
        options = D.line_parts(path, dev, all_days[0], all_days[-1]) if all_days else []
        if part:
            daily = D.part_daily(path, dev, all_days, part)
            t, spark = D.Totals(), []
            for day in sorted(daily):
                t.add(daily[day])
                spark.append((day, daily[day].availability))
            days = sorted(daily)
        else:
            t, spark = totals(win, dev)
            days = all_days
        ctx = {"dev": dev, "t": t, "day_spark": spark, "period_days": all_days, "part": part,
               "part_options": options}
        if days:
            lo, hi = days[0], days[-1]
            acts = D.activities(path, dev, lo, hi, part)
            ctx.update(
                acts=acts, acts_total=sum(a["seconds"] for a in acts) or 1.0,
                unplanned=D.reasons(path, dev, lo, hi, "unplanned_stop", part=part),
                planned=D.reasons(path, dev, lo, hi, "planned_stop", part=part),
                hours=D.hours(path, dev, lo, hi, part),
            )
            day_url = lambda n: url_for("line", dev=dev) + win.link(g="day", p=D.production_day(n).isoformat(), part=part)  # noqa: E731
            # Every production day in the period, not just days with hour records: the device files
            # a long idle stretch under the day it began (AreaB: one 232 h record from 11 to 21 Sep),
            # and dropping those rows would hide exactly what this chart is for.
            mine = [d for (v, d) in win.included if v == dev]
            heat_days = [d for d in range(min(mine), max(mine) + 1) if win.in_period(d)]
            ctx["heatmap"] = C.heatmap(D.hour_slots(path, dev, heat_days, part), heat_days, day_url)
            # Implausible durations: flagged over all history (medians need it), shown for this period.
            every_flag = D.implausible(path, app.config["IMPLAUSIBLE"], dev)
            flagged = [r for r in every_flag if r["day"] is not None and lo <= r["day"] <= hi
                       and (part is None or r["part"] == part)]
            flag_ids = {r["event_id"] for r in every_flag}
            # The charts leave flagged events out - one 56 h "down" turned AreaB's Pick & Place 2 from a
            # 55-stop, 8-minute nuisance into an apparent 68-minute breakdown. The tables keep everything.
            unp_flags = [r for r in flagged if r["impact"] == "unplanned_stop"]
            ctx["chart_excluded"] = (len(unp_flags), sum(r["seconds"] for r in unp_flags))
            clean = D.reasons(path, dev, lo, hi, "unplanned_stop", flag_ids, part)
            ctx["quadrant"] = C.quadrant([dict(r) for r in clean], len(days), **app.config["CHARTS"])
            ctx["pareto"] = C.pareto([dict(r) for r in clean])
            ctx["pareto_summary"] = C.pareto_summary([dict(r) for r in clean])
            ctx["implausible"] = flagged
            ctx["rule"] = app.config["IMPLAUSIBLE"]
            if flagged:
                fp = sum(r["seconds"] for r in flagged if r["impact"] == "planned_stop")
                fu = sum(r["seconds"] for r in flagged if r["impact"] == "unplanned_stop")
                sched, elapsed = t.sched, t.elapsed
                ctx["what_if"] = {
                    "planned_h": fp, "unplanned_h": fu,
                    "planned_share": fp / t.pln if t.pln else None,
                    "calendar_now": t.utilization,
                    "calendar_if": (sched - fp - fu) / elapsed if elapsed else None,
                    "avail_now": t.run / sched if sched else None,
                    "avail_if": t.run / (sched - fp - fu) if sched - fp - fu > 0 else None,
                }

            # Run/stop rhythm by week: the period's weeks, or the 12 weeks to its end when it is shorter.
            r_lo = lo if hi - lo >= 20 else max(min(d for (v, d) in win.included if v == dev), hi - 83)
            ctx["rhythm_span"] = (r_lo, hi, r_lo != lo)
            weeks = D.weekly_rhythm(path, dev, r_lo, hi, flag_ids, part)
            ctx["change"] = C.change_point(weeks, **app.config["RHYTHM"])
            ctx["change_stop"] = C.change_point(weeks, "stop_min", **app.config["RHYTHM"])
            ctx["rhythm"] = C.rhythm(weeks, ctx["change"])
            r_flags = [r for r in every_flag if r["impact"] == "unplanned_stop" and r["day"] is not None
                       and r_lo <= r["day"] <= hi and (part is None or r["part"] == part)]
            ctx["rhythm_excluded"] = (len(r_flags), sum(r["seconds"] for r in r_flags))

            # Changeovers, leaving out every implausible event on this line.
            changes = D.changeovers(path, dev, lo, hi, {r["event_id"] for r in every_flag})
            if part:
                changes = [c for c in changes if part in (c["from"], c["to"])]
            ctx["changes"] = changes
            ctx["comatrix"] = C.changeover_matrix(changes, lambda p: url_for("product", part=p) + win.query())
            ctx["changeovers"] = sum(1 for h in ctx["hours"] if h["part"] and "," in h["part"])
            over = [h for h in ctx["hours"] if h["performance"] is not None and h["performance"] > 1]
            ctx["bad_perf"] = len(over)
            ctx["bad_perf_short"] = sum(1 for h in over if h["run"] < 300)
        return render_template("line.html", **common(snap, win, dev, [dev]), **ctx)

    # ------------------------------------------------------- product pages

    @app.route("/product/<path:part>")
    def product(part: str):
        snap = D.load(app.config["CACHE_PATH"])
        path = app.config["CACHE_PATH"]
        ran = D.part_days(path, part)
        if not ran:
            abort(404)
        win = select_window(snap, request.args)
        href = lambda p: url_for("product", part=p) + win.query()  # noqa: E731
        opens = [o for o in D.merge_part_job(D.open_runs(path)) if o.part == part and o.channel != "job"]
        lines, spans, excluded = [], [], [0, 0.0]
        for dev in snap.devices:
            days = win.days(dev)
            if not days or not any(days[0] <= d <= days[-1] for d in ran.get(dev, ())):
                continue      # cheap exit: most periods of most parts have nothing to show
            lo, hi = days[0], days[-1]
            every_flag = D.implausible(path, app.config["IMPLAUSIBLE"], dev)
            flag_ids = {r["event_id"] for r in every_flag}
            all_runs = D.part_runs(path, dev, lo, hi, flag_ids)
            mine = [r for r in all_runs if r["part"] == part]
            if not mine:
                continue
            l = {"dev": dev, "runs": mine}
            for k in ("run_s", "unp_s", "pln_s", "ideal_s", "down_s", "flagged_s", "flagged_n"):
                l[k] = sum(r[k] for r in mine)
            excluded[0] += l["flagged_n"]
            excluded[1] += l["flagged_s"]
            # Like for like: every part this line ran in the period, with an hour or more of running.
            per: dict[str, dict] = {}
            for r in all_runs:
                if r["part"]:
                    a = per.setdefault(r["part"], {"part": r["part"], "runs": 0, "run_s": 0.0, "down_s": 0.0})
                    a["runs"] += 1
                    a["run_s"] += r["run_s"]
                    a["down_s"] += r["down_s"]
            l["compare"] = C.part_compare([dict(a, ratio=a["down_s"] / a["run_s"]) for a in per.values()
                                           if a["run_s"] >= 3600], part, href)
            reasons: dict[str, list] = {}
            for r in mine:
                for name, (n, sec) in r["reasons"].items():
                    x = reasons.setdefault(name, [0, 0.0])
                    x[0] += n
                    x[1] += sec
            rs = [{"reason": k, "events": n, "seconds": sec} for k, (n, sec) in reasons.items()]
            l["pareto"] = C.pareto([dict(r) for r in rs])
            l["pareto_summary"] = C.pareto_summary(rs)
            # Levels from all usable history (a reconfiguration needs runs either side); drawn for the period.
            hist = D.ict_history(path, dev, part)
            l["ict_levels"] = D.ict_levels(hist, app.config["ICT_TOLERANCE"])
            t_lo, t_hi = D.production_day_window(lo)[0], D.production_day_window(hi)[1][-1]
            for c in l["ict_levels"]["changes"]:
                c["in_period"] = t_lo <= c["start"] < t_hi
            l["ict_points"] = [p for p in hist if mine[0]["start"] <= p["start"] <= mine[-1]["start"]]
            l["inout"] = C.changeover_inout(D.changeovers(path, dev, lo, hi, flag_ids), part, href)
            spans += [mine[0]["start"], max(r["end"] for r in mine)]
            lines.append(l)

        ctx = {"part": part, "lines": lines, "open_here": opens, "excluded": excluded,
               "period_days": win.days(), "last_ran": None, "mixed": False}
        if lines:
            span = (min(spans), max(spans))
            ctx["timeline"] = C.run_timeline({l["dev"]: l["runs"] for l in lines}, span)
            ctx["ict"] = C.ict_chart({l["dev"]: {"points": l["ict_points"], "levels": l["ict_levels"]}
                                      for l in lines}, span)
            ctx["mixed"] = any(l["ict_levels"]["changes"] for l in lines)
        else:
            ctx["last_ran"] = max(((d, max(v)) for d, v in ran.items()), key=lambda dv: dv[1])
        return render_template("product.html", **common(snap, win, "products", snap.devices), **ctx)

    # ----------------------------------------------------------- products

    @app.route("/products")
    def products():
        snap = D.load(app.config["CACHE_PATH"])
        win = select_window(snap, request.args)
        unit = request.args.get("u", "s_per_piece")
        if unit not in dict(D.SAP_UNITS):
            unit = "s_per_piece"
        path = app.config["CACHE_PATH"]
        std = D.standards(path)
        ict = D.device_ict(path)
        reconf = {k: D.ict_levels(D.ict_history(path, *k), app.config["ICT_TOLERANCE"])["changes"] for k in ict}
        opens = D.open_runs(path)
        open_by = {(o.device_id, o.part) for o in opens if o.channel == "part"}

        rows, part_runs, job_runs = [], [], []
        for dev in snap.devices:
            days = win.days(dev)
            if not days:
                continue
            pr = D.runs(path, "part_run", dev, days[0], days[-1])
            part_runs += pr
            job_runs += D.runs(path, "job_run", dev, days[0], days[-1])
            agg: dict[str, dict] = {}
            for r in pr:
                a = agg.setdefault(r["part"] or "—", {"runs": 0, "run": 0.0, "ideal": 0.0, "cycles": 0.0,
                                                       "stopped": 0.0, "t_runs": 0, "t_run": 0.0, "t_good": 0.0})
                a["runs"] += 1
                a["run"] += r["run_time"] or 0
                a["ideal"] += r["ideal_time"] or 0
                a["cycles"] += r["run_cycles"] or 0
                a["stopped"] += (r["unplanned_stop_time"] or 0) + (r["planned_stop_time"] or 0)
                if r["counts_trusted"]:
                    a["t_runs"] += 1
                    a["t_run"] += r["run_time"] or 0
                    a["t_good"] += r["good_count"] or 0
            for part, a in agg.items():
                st = std.get(part, {})
                d_ict = ict.get((dev, part), {})
                epc = st.get("expected_count_per_cycle")
                spp = st.get("sap_s_per_piece")
                spc = spp * epc if spp and epc and epc > 0 else None
                actual = a["run"] / a["cycles"] if a["cycles"] else None
                rows.append({
                    "part": part, "dev": dev, **a, "ict": d_ict.get("ict"), "ict_sd": d_ict.get("sd"),
                    "ict_n": d_ict.get("n", 0), "ict_changes": reconf.get((dev, part), []), "epc": epc, "sap_display": D.from_s_per_piece(spp, unit),
                    "sap_spc": spc, "actual_spc": actual,
                    "vs_ict": actual / d_ict["ict"] if actual and d_ict.get("ict") else None,
                    "vs_sap": actual / spc if actual and spc else None,
                    "earned_sap": a["cycles"] * spc if spc else None,
                    "sap_state": ("no SAP standard" if spp is None else
                                  "pieces/cycle not configured" if not epc or epc <= 0 else None),
                    "actual_spp": a["t_run"] / a["t_good"] if a["t_runs"] and a["t_good"] > 0 else None,
                    "open": (dev, part) in open_by,
                })
        rows.sort(key=lambda r: r["run"], reverse=True)
        part_runs.sort(key=lambda r: r["run_time"] or 0, reverse=True)
        job_runs.sort(key=lambda r: r["run_time"] or 0, reverse=True)
        return render_template(
            "products.html", **common(snap, win, "products", snap.devices),
            rows=rows, part_runs=part_runs, job_runs=job_runs, ict=ict, unit=unit, units=D.SAP_UNITS,
            opens=opens, period_days=win.days(),
            imported=request.args.get("imported"), import_errors=request.args.getlist("err"),
        )

    def _number(raw) -> float | None:
        """Blank clears; anything else must be a finite positive number."""
        if raw is None or str(raw).strip() == "":
            return None
        try:
            v = float(str(raw).strip())
        except ValueError:
            raise ValueError(f"{str(raw).strip()!r} is not a number") from None
        if not math.isfinite(v) or v <= 0:
            raise ValueError("must be a positive number")
        return v

    @app.post("/api/part_standard")
    def set_standard():
        body = request.get_json(silent=True) or {}
        part, fld = (body.get("part") or "").strip(), body.get("field")
        if not part or fld not in ("sap", "epc"):
            return jsonify(ok=False, error="part and field (sap|epc) required"), 400
        try:
            v = _number(body.get("value"))
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        if fld == "sap":
            unit = body.get("unit")
            if unit not in dict(D.SAP_UNITS):
                return jsonify(ok=False, error="unknown unit"), 400
            D.save_standard(app.config["CACHE_PATH"], part, sap=v, unit=unit)
        else:
            D.save_standard(app.config["CACHE_PATH"], part, epc=v)
        return jsonify(ok=True)

    # ---------------------------------------------------------------- live

    def live_context(devices: list[str]) -> dict:
        """What the /live page and /api/live both show: the current_state table only."""
        from datetime import datetime, timezone

        from . import live as L

        cfg = app.config["LIVE"]
        now = datetime.now(timezone.utc)
        rows = L.readings(app.config["CACHE_PATH"], cfg["devices"] or devices, now, cfg["stale_after"])
        read = [r for r in rows or [] if r["age_s"] is not None]
        # link a part only where it has a product page (one closed run at least)
        known = {r["part"] for r in rows or [] if r.get("part") and D.part_days(app.config["CACHE_PATH"], r["part"])}
        return {"rows": rows, "now": now, "known_parts": known,
                "oldest": max(read, key=lambda r: r["age_s"], default=None),
                "never": [r["device_id"] for r in rows or [] if r["age_s"] is None],
                "stale_after": cfg["stale_after"], "interval": cfg["interval"], "poll": cfg["poll"],
                "impact_name": C.LABEL, "tz": D.TZ}

    @app.route("/live")
    def live_view():
        """Current state, read from the current_state table only. Never calls a device."""
        snap = D.load(app.config["CACHE_PATH"])
        return render_template("live.html", snap=snap, win=select_window(snap, request.args), page="live",
                               **live_context(snap.devices))

    @app.get("/api/live")
    def live_api():
        """The current_state rows as JSON, each with its panel rendered by the page's own template, so
        the page can update in place. Reads the table only."""
        from datetime import datetime

        ctx = live_context([])
        rows = []
        for r in ctx["rows"] or []:
            raw = {k: v for k, v in r.items() if not isinstance(v, datetime)}
            rows.append({**raw, "html": render_template("_live_panel.html", r=r, **ctx)})
        return jsonify(never_run=ctx["rows"] is None, server_time=ctx["now"].isoformat(),
                       stale_after=ctx["stale_after"], rows=rows,
                       top_html=render_template("_live_top.html", **ctx) if ctx["rows"] is not None else "")

    @app.get("/products/standards.csv")
    def export_standards():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["part", "sap_standard", "sap_unit", "sap_s_per_piece", "expected_count_per_cycle",
                    "derived_ideal_cycle_time_s_per_cycle", "ict_stddev", "sample_runs"])
        for part, r in sorted(D.standards(app.config["CACHE_PATH"]).items()):
            w.writerow([part, r["sap_standard"], r["sap_unit"], r["sap_s_per_piece"], r["expected_count_per_cycle"],
                        r["ideal_cycle_time"], r["ict_stddev"], r["sample_runs"]])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=part_standards.csv"})

    @app.post("/products/import")
    def import_standards():
        """CSV with a part column and any of: sap_standard (or value), sap_unit
        (or unit; default the page's unit), expected_count_per_cycle. The
        export file round-trips."""
        f = request.files.get("file")
        back = dict(request.args)
        if not f or not f.filename:
            return redirect(url_for("products", **back, err="no file chosen"))
        unit_default = request.form.get("u", "s_per_piece")
        text = f.read().decode("utf-8-sig", errors="replace")
        n, errs = 0, []
        for i, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            part = row.get("part")
            if not part:
                errs.append(f"line {i}: no part")
                continue
            try:
                kw = {}
                if "sap_standard" in row or "value" in row:
                    unit = row.get("sap_unit") or row.get("unit") or unit_default
                    if unit not in dict(D.SAP_UNITS):
                        raise ValueError(f"unknown unit {unit!r}")
                    kw.update(sap=_number(row.get("sap_standard", row.get("value"))), unit=unit)
                if "expected_count_per_cycle" in row:
                    kw["epc"] = _number(row["expected_count_per_cycle"])
                if kw:
                    D.save_standard(app.config["CACHE_PATH"], part, who="csv import", **kw)
                    n += 1
            except ValueError as exc:
                errs.append(f"line {i} ({part}): {exc}")
        return redirect(url_for("products", **back, imported=n, err=errs[:20]))

    return app
