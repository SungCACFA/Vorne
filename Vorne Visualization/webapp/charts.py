"""Inline SVG charts, generated server-side.

No chart library and nothing fetched at runtime, so every chart works the
same in the live app, the static export and print. Each mark carries its own
<title> tooltip (which also works where page scripts don't run, as in the
export's fragments), and every chart has its table view directly beneath it
on the page.

Colours are the design system's four performance-impact colours, validated
with the dataviz palette checks: colourblind separation passes for every pair
(worst deltaE 9.9, red/green, deuteranopia) and so does the normal-vision floor
(19.4). Amber and grey are under 3:1 against the page, which is why both charts
keep a legend, tooltips and a table view, and why marks are never the only
place a value lives.
"""

from __future__ import annotations

import math
from datetime import date
from html import escape

from markupsafe import Markup

from . import data as D

INK, MUTE, RULE, PANEL = "#15191B", "#5A6368", "#DADFE1", "#F4F6F6"
COLOUR = {"run": "var(--run)", "unplanned_stop": "var(--unp)", "planned_stop": "var(--pln)",
          "not_scheduled": "var(--nsc)", "other": "#FFFFFF"}
LABEL = {"run": "Running", "unplanned_stop": "Unplanned stop", "planned_stop": "Planned stop",
         "not_scheduled": "Not scheduled", "other": "Another product"}


def _h(seconds: float) -> str:
    h = max(0.0, seconds) / 3600
    return f"{h:,.0f}" if h >= 100 else f"{h:.1f}"


def _m(seconds: float) -> str:
    return f"{max(0.0, seconds) / 60:.0f}"


# ---------------------------------------------------------------- heatmap

def heatmap(slots: dict[int, list[dict]], days: list[int], href_for_day) -> Markup:
    """One row per production day (23:00 to 23:00), one cell per hour,
    coloured by the impact that took most of that hour."""
    if not days:
        return Markup("")
    n = len(days)
    row_h = 16 if n <= 35 else 9 if n <= 100 else 5
    gap = 2 if row_h >= 9 else 1
    label_w, top, cell_w = 104, 30, 36
    width = label_w + 24 * cell_w
    height = top + n * row_h + 26
    label_every = 1 if row_h >= 16 else 7      # dense views: label Mondays only
    out = [f'<svg class=heat viewBox="0 0 {width} {height}" width="{width}" height="{height}" role=img '
           f'aria-label="Hour by hour, {n} production days, coloured by what took most of each hour">']
    # Hour labels every 2 h, and the shift changes (07:00, 15:00, 23:00) as hairlines.
    for h in range(24):
        clock = (23 + h) % 24
        x = label_w + h * cell_w
        if h % 2 == 0:
            out.append(f'<text x="{x + 2}" y="{top - 10}" class=tick>{clock:02d}</text>')
        if clock in (7, 15):
            out.append(f'<line x1="{x - 1}" x2="{x - 1}" y1="{top - 6}" y2="{height - 6}" class=shift />')
    out.append(f'<text x="{label_w + 8 * cell_w}" y="{top - 22}" class=tick>first shift</text>')
    out.append(f'<text x="{label_w + 16 * cell_w}" y="{top - 22}" class=tick>second shift</text>')
    out.append(f'<text x="{label_w}" y="{top - 22}" class=tick>third shift</text>')
    for i, day in enumerate(days):
        y = top + i * row_h
        d = D.production_day(day)
        cells = slots.get(day) or []
        href = escape(href_for_day(day))
        iso = d.isoformat()
        out.append(f'<a href="{href}" data-filter-day="{iso}" class=heatrow>')
        # dense views: Mondays only, plus the first row unless a Monday follows within 3 rows
        first_ok = i == 0 and all(D.production_day(x).weekday() != 0 for x in days[1:4])
        if label_every == 1 or d.weekday() == 0 or first_ok:
            out.append(f'<text x="0" y="{y + row_h - 3 if row_h >= 9 else y + row_h}" class=day>'
                       f'{d:%a} {d.day} {d:%b}</text>')
        for h, c in enumerate(cells):
            x = label_w + h * cell_w
            if not c["dominant"]:
                fill, title = PANEL, f"{c['start']:%H:%M} · no state recorded"
            else:
                fill = COLOUR[c["dominant"]]
                if c["dominant"] == "other":
                    out.append(f'<rect x="{x}" y="{y}" width="{cell_w - gap}" height="{row_h - gap}" rx="1" '
                               f'class=other><title>{d:%a} {d.day} {d:%b} {c["start"]:%H:%M} · mostly another '
                               f'product</title></rect>')
                    continue
                # the hour; what ran; all four buckets, zeros included; the dominant state and its reason
                split = " · ".join(f"{LABEL[k]} {_m(c['seconds'].get(k, 0.0))} min" for k in D.IMPACTS)
                what = (c["top_state"] or "").replace("_", " ")
                if c["top_reason"] and c["top_reason"] != c["top_state"]:
                    what += f": {c['top_reason']}"
                end = c.get("end")
                st = c["start"]           # the hour's own date: 23:00 belongs to the evening before
                title = "\n".join([
                    f"{st:%a} {st.day} {st:%b} {st:%H:%M}" + (f"–{end:%H:%M}" if end else "")
                    + (f" (production day {d:%a} {d.day} {d:%b})" if st.date() != d else ""),
                    "Ran " + (" / ".join(c.get("parts") or []) or "no part recorded"),
                    split,
                    f"Mostly {LABEL[c['dominant']].lower()}"
                    + (f" — {what}" if what and what.lower() != LABEL[c["dominant"]].lower() else ""),
                ])
            out.append(f'<rect x="{x}" y="{y}" width="{cell_w - gap}" height="{row_h - gap}" rx="1" fill="{fill}">'
                       f'<title>{escape(title)}</title></rect>')
        out.append("</a>")
    out.append(f'<text x="{label_w}" y="{height - 6}" class=tick>Rows are fixed 23:00-23:00 windows. The device can '
               f'file an unscheduled night with the day before, so its own production day - and that day\'s totals '
               f'elsewhere on this page - can run past 24 hours. Nothing is dropped or counted twice.</text>')
    out.append("</svg>")
    return Markup("".join(out))


# --------------------------------------------------------------- quadrant

ZONES = {
    # (frequent?, long?): (name, action)
    (True, True): ("Critical", "frequent and long: fix first"),
    (True, False): ("Chronic nuisance", "frequent, short: process fix by the line team"),
    (False, True): ("Major breakdown", "rare, long: maintenance root cause"),
    (False, False): ("Minor", "rare, short: monitor"),
}


def zone_of(events: int, mean_s: float, split_events: float, split_mean: float) -> tuple[str, str]:
    return ZONES[(events > split_events, mean_s > split_mean)]


# Defaults only: the live values come from config.yaml (charts:), read once
# in create_app. Fixed lines, never derived from the data - see quadrant().
FREQUENT_STOPS_PER_DAY = 1.0
LONG_STOP_MINUTES = 30.0


def quadrant(reasons: list[dict], n_days: int, label_top: int = 7, *,
             frequent_per_day: float = FREQUENT_STOPS_PER_DAY,
             long_minutes: float = LONG_STOP_MINUTES) -> Markup:
    """Unplanned-stop reasons: events (x, log) against mean minutes (y, log),
    bubble area proportional to hours lost.

    Zones split at fixed, plain-language lines: frequent = more than one stop
    per production day in the period, on average; long = over 30 minutes per
    stop. Splitting at the median reason was tried and rejected: dozens of
    trivial reasons dragged both lines down (to ~10 stops and ~9 min on AreaB)
    and every reason that mattered landed in "critical". A weighted median
    was unstable between periods."""
    rs = [r for r in reasons if r["events"] > 0 and r["seconds"] > 0]
    if len(rs) < 4:
        return Markup('<p class=empty>Too few stop reasons in this period to compare.</p>')
    for r in rs:
        r["mean_s"] = r["seconds"] / r["events"]
    split_e = max(1.0, frequent_per_day * n_days)
    split_m = long_minutes * 60

    W, H, L, R, T, B = 960, 470, 64, 24, 16, 52
    pw, ph = W - L - R, H - T - B
    def ext(vals, lo_floor):
        lo, hi = max(min(vals), lo_floor), max(vals)
        lo, hi = 10 ** math.floor(math.log10(lo)), 10 ** math.ceil(math.log10(hi))
        return lo, hi if hi > lo else lo * 10
    ex = ext([r["events"] for r in rs], 1)
    ey = ext([r["mean_s"] / 60 for r in rs], 0.1)
    def X(v):
        v = min(max(v, ex[0]), ex[1])
        return L + pw * (math.log10(v) - math.log10(ex[0])) / (math.log10(ex[1]) - math.log10(ex[0]))

    def Y(v):   # values below the axis floor sit on it; the tooltip keeps the true value
        v = min(max(v, ey[0]), ey[1])
        return T + ph - ph * (math.log10(v) - math.log10(ey[0])) / (math.log10(ey[1]) - math.log10(ey[0]))
    max_h = max(r["seconds"] for r in rs)
    rad = lambda s: max(4.0, 30 * math.sqrt(s / max_h))  # noqa: E731   area proportional to hours

    o = [f'<svg class=quad viewBox="0 0 {W} {H}" width="{W}" height="{H}" role=img '
         f'aria-label="Unplanned stop reasons by frequency and length">']
    # recessive grid at each decade
    for exp in range(int(math.log10(ex[0])), int(math.log10(ex[1])) + 1):
        v = 10 ** exp
        o.append(f'<line x1="{X(v):.1f}" x2="{X(v):.1f}" y1="{T}" y2="{T + ph}" class=grid />'
                 f'<text x="{X(v):.1f}" y="{T + ph + 18}" class="tick mid">{v:,}</text>')
    for exp in range(int(math.floor(math.log10(ey[0]))), int(math.log10(ey[1])) + 1):
        v = 10 ** exp
        lab = f"{v:g}"
        o.append(f'<line x1="{L}" x2="{L + pw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class=grid />'
                 f'<text x="{L - 8}" y="{Y(v) + 4:.1f}" class="tick end">{lab}</text>')
    o.append(f'<text x="{L + pw / 2}" y="{H - 8}" class="axis mid">stops in the period (log scale) →</text>'
             f'<text x="16" y="{T + ph / 2}" class="axis mid" transform="rotate(-90 16 {T + ph / 2})">'
             f'mean minutes per stop (log scale) →</text>')
    # the median split, and the zone names with their action, in the corners
    sx, sy = X(split_e), Y(split_m / 60)
    sx = min(max(sx, L), L + pw)
    sy = min(max(sy, T), T + ph)
    o.append(f'<line x1="{sx:.1f}" x2="{sx:.1f}" y1="{T}" y2="{T + ph}" class=split />'
             f'<line x1="{L}" x2="{L + pw}" y1="{sy:.1f}" y2="{sy:.1f}" class=split />'
             f'<text x="{sx:.1f}" y="{T + ph + 32}" class="tick mid">{split_e:,.0f} stops: '
             f'{frequent_per_day:g} per production day with records</text>'
             f'<text x="{L + pw - 4}" y="{sy - 5:.1f}" class="tick end">{long_minutes:g} min per stop</text>')
    corners = {(False, True): (L + 10, T + 18, "start"), (True, True): (L + pw - 10, T + 18, "end"),
               (False, False): (L + 10, T + ph - 24, "start"), (True, False): (L + pw - 10, T + ph - 24, "end")}
    for key, (x, y, anchor) in corners.items():
        name, action = ZONES[key]
        o.append(f'<text x="{x}" y="{y}" class="zone {anchor}">{name}</text>'
                 f'<text x="{x}" y="{y + 15}" class="zonenote {anchor}">{action}</text>')
    # bubbles, biggest first so small ones sit on top
    ranked = sorted(rs, key=lambda r: -r["seconds"])
    labelled = {id(r) for r in ranked[:label_top]}
    for r in ranked:
        cx, cy, rr = X(r["events"]), Y(r["mean_s"] / 60), rad(r["seconds"])
        name, action = zone_of(r["events"], r["mean_s"], split_e, split_m)
        title = (f"{r['reason']} · {_h(r['seconds'])} h lost · {r['events']:,} stops · "
                 f"{_m(r['mean_s'])} min each · {name}\n{action[0].upper() + action[1:]}")
        o.append(f'<g class=bubble><circle cx="{cx:.1f}" cy="{cy:.1f}" r="{rr:.1f}" class=dot />'
                 f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{max(rr, 12):.1f}" class=hit>'
                 f'<title>{escape(title)}</title></circle></g>')
    # Direct labels for the biggest reasons, placed only where they collide
    # with nothing: no stacking, no nudging. A label that finds no clear spot
    # is left to its tooltip and the table beneath.
    placed: list[tuple[float, float, float, float]] = []
    discs = [(X(r["events"]), Y(r["mean_s"] / 60), rad(r["seconds"])) for r in ranked]
    def clear(box):
        x0, y0, x1, y1 = box
        if x0 < L or x1 > L + pw or y0 < T or y1 > T + ph:
            return False
        if any(not (x1 < a or x0 > c or y1 < b or y0 > d) for a, b, c, d in placed):
            return False
        for dx, dy, dr in discs:      # any bubble's disc, not just its own
            nx, ny = min(max(dx, x0), x1), min(max(dy, y0), y1)
            if (nx - dx) ** 2 + (ny - dy) ** 2 < (dr + 1) ** 2:
                return False
        return True
    for r in ranked:
        if id(r) not in labelled:
            continue
        cx, cy, rr = X(r["events"]), Y(r["mean_s"] / 60), rad(r["seconds"])
        text = f"{r['reason']} {_h(r['seconds'])} h"
        w, hgt = 6.4 * len(text), 13
        for anchor, tx, ty in (("start", cx + rr + 5, cy + 4), ("end", cx - rr - 5, cy + 4),
                               ("middle", cx, cy - rr - 5), ("middle", cx, cy + rr + 14)):
            x0 = tx if anchor == "start" else tx - w if anchor == "end" else tx - w / 2
            box = (x0, ty - hgt + 3, x0 + w, ty + 3)
            if clear(box):
                placed.append(box)
                cls = {"start": "start", "end": "end", "middle": "mid"}[anchor]
                o.append(f'<text x="{tx:.1f}" y="{ty:.1f}" class="lab {cls}">'
                         f'{escape(r["reason"])} <tspan class=labval>{_h(r["seconds"])} h</tspan></text>')
                break
    o.append("</svg>")
    # bubble size key, outside the plot so its circles cannot be mistaken for data
    r1, r2 = rad(max_h), rad(max_h * 0.25)
    kh = int(2 * r1 + 8)
    o.append(f'<svg class=quadkey viewBox="0 0 420 {kh}" width="420" height="{kh}" role=img aria-label="Bubble size key">'
             f'<text x="0" y="{kh / 2 + 4:.0f}" class=tick>Bubble area = hours lost:</text>'
             f'<circle cx="{150 + r1:.1f}" cy="{kh / 2:.1f}" r="{r1:.1f}" class="dot key" />'
             f'<text x="{150 + 2 * r1 + 6:.1f}" y="{kh / 2 + 4:.0f}" class=tick>{_h(max_h)} h</text>'
             f'<circle cx="{260 + r2:.1f}" cy="{kh / 2:.1f}" r="{r2:.1f}" class="dot key" />'
             f'<text x="{260 + 2 * r2 + 6:.1f}" y="{kh / 2 + 4:.0f}" class=tick>{_h(max_h * 0.25)} h</text></svg>')
    return Markup("".join(o))


# ----------------------------------------------------------------- pareto

def pareto(reasons: list[dict], top: int = 15) -> Markup:
    """Stop reasons ranked by hours lost, each as a share of the period's
    unplanned hours, with the running total and the 80% mark.

    Drawn horizontally so the reason names read without rotation. One axis
    (0-100% of lost hours) carries both the bars and the running total: a
    Pareto in hours with a separate percentage scale would be a two-axis
    chart, which invents correlations."""
    rs = sorted((r for r in reasons if r["seconds"] > 0), key=lambda r: -r["seconds"])
    if len(rs) < 3:
        return Markup('<p class=empty>Too few stop reasons in this period for a Pareto.</p>')
    total = sum(r["seconds"] for r in rs)
    rows = [(r["reason"], r["seconds"], r["events"]) for r in rs[:top]]
    rest = rs[top:]
    if rest:
        rows.append((f"All other {len(rest)} reasons", sum(r["seconds"] for r in rest),
                     sum(r["events"] for r in rest)))
    cum, cums = 0.0, []
    for _, s, _ in rows:
        cum += s
        cums.append(cum / total)
    n80 = next(i + 1 for i, c in enumerate(cums) if c >= 0.8 - 1e-9)
    # How many individual reasons 80% really takes, even when it is past the grouped tail.
    run, need = 0.0, len(rs)
    for k, r in enumerate(rs, 1):
        run += r["seconds"]
        if run / total >= 0.8 - 1e-9:
            need = k
            break
    if rest and n80 == len(rows):
        n80 = None       # crossed inside the grouped tail: label that point with the real count instead

    label_w, W, row_h, T = 230, 960, 22, 30
    pw = W - label_w - 230
    H = T + len(rows) * row_h + 34

    def X(frac):
        return label_w + pw * frac

    o = [f'<svg class=pareto viewBox="0 0 {W} {H}" width="{W}" height="{H}" role=img '
         f'aria-label="Stop reasons ranked by share of unplanned hours, with the running total">']
    for frac in (0, .2, .4, .6, .8, 1.0):
        o.append(f'<line x1="{X(frac):.1f}" x2="{X(frac):.1f}" y1="{T - 4}" y2="{H - 30}" class=grid />'
                 f'<text x="{X(frac):.1f}" y="{H - 14}" class="tick mid">{frac:.0%}</text>')
    o.append(f'<line x1="{X(.8):.1f}" x2="{X(.8):.1f}" y1="{T - 16}" y2="{H - 30}" class=mark80 />'
             f'<text x="{X(.8) + 4:.1f}" y="{T - 10}" class=tick>80% of lost hours</text>')
    pts = []
    for i, ((name, s, n), c) in enumerate(zip(rows, cums)):
        y = T + i * row_h
        other = name.startswith("All other ")
        o.append(f'<g class=pbar><text x="{label_w - 8}" y="{y + 15}" class="end {"tick" if other else "lab"}">'
                 f'{escape(name)}</text>'
                 f'<rect x="{label_w}" y="{y + 5}" width="{max(1.0, pw * s / total):.1f}" height="{row_h - 9}" '
                 f'rx="2" class="{"bar other" if other else "bar"}" />'
                 f'<rect x="0" y="{y}" width="{W}" height="{row_h}" class=hitrow>'
                 f'<title>{escape(name)} · {_h(s)} h ({s / total:.0%} of lost hours) · {n:,} stops · '
                 f'{_m(s / n) if n else "—"} min each\nrunning total {c:.0%} · 80% of lost hours takes '
                 f'{need} of {len(rs)} reasons</title></rect></g>')
        pts.append((X(c), y + row_h / 2))
    o.append('<polyline class=cum points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '" />')
    for i, (x, y) in enumerate(pts):
        o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" class=cumdot />')
        summary = pareto_summary(reasons)
        verdict = ("a short list: fixing these recovers most of it" if summary and summary["short_list"]
                   else "no small set of fixes recovers most of this")
        if n80 is not None and i + 1 == n80:
            # The curve comes in from the upper left and leaves to the lower right, and the bars this
            # far down are short: below-left of the point, and left of the 80% mark, is the clear spot.
            lx = min(x - 14, X(.8) - 6)
            o.append(f'<text x="{lx:.1f}" y="{y + 18:.1f}" text-anchor=end class=lab>{n80} '
                     f'reason{"s" if n80 > 1 else ""} = {cums[i]:.0%} of lost hours</text>'
                     f'<text x="{lx:.1f}" y="{y + 33:.1f}" text-anchor=end class=verdict>{verdict}</text>')
        elif n80 is None and i + 1 == len(rows):
            # 80% is crossed inside the grouped tail: the label goes in the clear space right of the
            # 100% point, as a short stack, so it crosses neither the line nor the 80% mark.
            lines = [("lab", "80% of lost hours takes"), ("lab", f"{need} of {len(rs)} reasons:")]
            lines += [("verdict", "no small set of fixes"), ("verdict", "recovers most of this")]                 if not (summary and summary["short_list"]) else [("verdict", "a short list")]
            for j, (cls, txt) in enumerate(lines):
                o.append(f'<text x="{x + 12:.1f}" y="{y - 46 + 15 * j:.1f}" class={cls}>{txt}</text>')
    o.append("</svg>")
    key = ('<div class=key><span><i style="background:var(--unp)"></i>Share of the period&#39;s unplanned hours</span>'
           '<span><i style="background:var(--ink);height:2px;vertical-align:middle"></i>Running total</span></div>')
    return Markup("".join(o) + key)


# ---------------------------------------------------------------- rhythm

def _week_tip(ws: list[dict], i: int) -> str:
    """One week of the rhythm chart: both means, each with its change from the week before."""
    w = ws[i]
    d = date.fromisoformat(w["week"])
    lines = [f"Week of {D.nice(d)}"]
    for key, label, n in (("run_min", "Mean run burst", f"{w['run_bursts']:,} run bursts"),
                          ("stop_min", "Mean unplanned stop", f"{w['stops']:,} stops")):
        if w[key] is None:
            lines.append(f"{label}: none")
            continue
        prev = ws[i - 1][key] if i > 0 else None
        delta = f" ({w[key] - prev:+.0f} min on the week before)" if prev is not None else " (first week shown)"
        lines.append(f"{label} {w[key]:.0f} min{delta} · {n}")
    return "\n".join(lines)


def rhythm(weeks: list[dict], change: dict | None = None) -> Markup:
    """Mean run-burst minutes and mean unplanned-stop minutes by week, on one
    axis (minutes). `change` (from change_point) marks the week the run burst
    stepped to a new level, with each side's mean drawn as a level."""
    ws = [w for w in weeks if w["run_min"] is not None or w["stop_min"] is not None]
    if len(ws) < 3:
        return Markup('<p class=empty>Fewer than three weeks in this period: pick a month or the year to date '
                      'to see the rhythm.</p>')
    W, H, L, R, T, B = 960, 300, 56, 190, 20, 44
    pw, ph = W - L - R, H - T - B
    top = max(v for w in ws for v in (w["run_min"] or 0, w["stop_min"] or 0))
    step = next(s for s in (5, 10, 15, 20, 30, 60, 120, 240, 480, 960) if top / s <= 6)
    ymax = step * math.ceil(top / step)

    def X(i):
        return L + pw * i / (len(ws) - 1)

    def Y(v):
        return T + ph - ph * v / ymax

    o = [f'<svg class=rhythm viewBox="0 0 {W} {H}" width="{W}" height="{H}" role=img '
         f'aria-label="Mean run burst and mean unplanned stop by week">']
    v = 0
    while v <= ymax:
        o.append(f'<line x1="{L}" x2="{L + pw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class=grid />'
                 f'<text x="{L - 8}" y="{Y(v) + 4:.1f}" class="tick end">{v:g}</text>')
        v += step
    o.append(f'<text x="14" y="{T + ph / 2}" class="axis mid" transform="rotate(-90 14 {T + ph / 2})">minutes</text>')
    every = max(1, round(len(ws) / 10))
    for i, w in enumerate(ws):
        if i % every == 0 or i == len(ws) - 1:
            d = date.fromisoformat(w["week"])
            o.append(f'<text x="{X(i):.1f}" y="{T + ph + 18}" class="tick mid">{d.day} {d:%b}</text>')
    o.append(f'<text x="{L + pw / 2}" y="{H - 6}" class="axis mid">week starting</text>')
    if change is not None and 0 < change["index"] < len(ws):
        i0 = change["index"]
        cx = (X(i0 - 1) + X(i0)) / 2
        o.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{T}" y2="{T + ph}" class=change />')
        o.append(f'<line x1="{L:.1f}" x2="{cx:.1f}" y1="{Y(change["before"]):.1f}" y2="{Y(change["before"]):.1f}" '
                 f'class=level />'
                 f'<line x1="{cx:.1f}" x2="{L + pw:.1f}" y1="{Y(change["after"]):.1f}" y2="{Y(change["after"]):.1f}" '
                 f'class=level />')
        d = date.fromisoformat(change["week"])
        word = "shortened" if change["shift"] < 0 else "lengthened"
        o.append(f'<text x="{cx + 6:.1f}" y="{T + 12}" class=changelab>From the week of {d.day} {d:%b}, run bursts '
                 f'{word} from {change["before"]:.0f} to {change["after"]:.0f} min ({change["shift"]:+.0%})</text>')
    ends = {}
    for key in ("run_min", "stop_min"):
        last = [w[key] for w in ws if w[key] is not None]
        ends[key] = Y(last[-1]) if last else None
    clash = None not in ends.values() and abs(ends["run_min"] - ends["stop_min"]) < 16
    for key, cls, label in (("run_min", "run", "Mean run burst"), ("stop_min", "unp", "Mean unplanned stop")):
        series = [(i, w) for i, w in enumerate(ws) if w[key] is not None]
        pts = [(X(i), Y(w[key])) for i, w in series]
        o.append(f'<polyline class="line {cls}" points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '" />')
        for (x, y), (i, w) in zip(pts, series):
            o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" class="pt {cls}" />'
                     f'<circle cx="{x:.1f}" cy="{y:.1f}" r="12" class=hit><title>{escape(_week_tip(ws, i))}'
                     f'</title></circle>')
        if pts and not clash:      # colliding end labels are dropped, never stacked
            (lx, ly), last = pts[-1], series[-1][1][key]
            o.append(f'<text x="{lx + 10:.1f}" y="{ly + 4:.1f}" class=lab>{label} '
                     f'<tspan class=labval>{last:.0f} min</tspan></text>')
    o.append("</svg>")
    key = ('<div class=key><span><i style="background:var(--run);height:2px;vertical-align:middle"></i>'
           'Mean run burst: minutes the line ran before it stopped</span>'
           '<span><i style="background:var(--unp);height:2px;vertical-align:middle"></i>'
           'Mean unplanned stop, minutes</span></div>')
    return Markup("".join(o) + key)


# ------------------------------------------------------ changeover matrix

# Sequential amber-to-brown ramp, validated with the dataviz ordinal checks
# (monotone lightness, visible gaps, light end >= 2:1 on white). Fixed bins,
# so a cell's colour means the same thing in every period.
CO_RAMP = ("#D5A441", "#C98A0B", "#966507", "#704A03", "#4A2E00")
CO_BINS = (1, 2, 4, 8)            # mean hours: <1, 1-2, 2-4, 4-8, 8+
CO_BIN_LABELS = ("under 1 h", "1-2 h", "2-4 h", "4-8 h", "8 h or more")


def co_bin(hours: float) -> int:
    return next((i for i, b in enumerate(CO_BINS) if hours < b), len(CO_BINS))


def ink_for(hex_colour: str) -> str:
    """White or ink inside a fill, by the fill's luminance."""
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lum = 0.2126 * r ** 2.2 + 0.7152 * g ** 2.2 + 0.0722 * b ** 2.2
    return INK if lum > 0.28 else "#FFFFFF"


def changeover_matrix(changes: list[dict], part_href) -> Markup:
    """From-part (rows) by to-part (columns): mean planned-stop hours per
    change, with the count in the cell. An HTML table, so it prints and reads
    as a table without the colours."""
    if not changes:
        return Markup('<p class=empty>No part or job changes started in this period.</p>')
    cells: dict[tuple, list[dict]] = {}
    for c in changes:
        cells.setdefault((c["from"], c["to"]), []).append(c)
    froms = sorted({c["from"] for c in changes})
    tos = sorted({c["to"] for c in changes})
    o = ['<div class=scroll style="max-height:none"><table class=comatrix><thead><tr><th>From, to</th>']
    o += [f'<td class=colhead><a class=lnk href="{escape(part_href(t))}">{escape(t)}</a></td>' for t in tos]
    o.append("</tr></thead><tbody>")
    for f in froms:
        o.append(f'<tr><th><a class=lnk href="{escape(part_href(f))}">{escape(f)}</a></th>')
        for t in tos:
            cs = cells.get((f, t))
            if not cs:
                o.append("<td class=nochange></td>")
                continue
            total = sum(c["seconds"] for c in cs)
            mean_h = total / len(cs) / 3600
            fill = CO_RAMP[co_bin(mean_h)]
            excl = sum(c["excluded_seconds"] for c in cs)
            title = (f"{f} to {t}{' (same part, new job)' if f == t else ''} · {len(cs)} "
                     f"change{'s' if len(cs) > 1 else ''} · mean {mean_h:.1f} h, total {total / 3600:.1f} h"
                     + (f" · {excl / 3600:.0f} h of implausible events left out" if excl else ""))
            o.append(f'<td style="background:{fill};color:{ink_for(fill)}" title="{escape(title)}"'
                     f'{" class=diag" if f == t else ""}>{len(cs)}<small>{mean_h:.1f} h</small></td>')
        o.append("</tr>")
    o.append("</tbody></table></div>")
    o.append("<div class=key>" + "".join(f'<span><i style="background:{c}"></i>{lab}</span>'
                                         for c, lab in zip(CO_RAMP, CO_BIN_LABELS))
             + "<span>Mean planned-stop hours per change; the large number is how many changes.</span></div>")
    return Markup("".join(o))


# ------------------------------------------------- Pareto: short list or not

SHORT_LIST_MAX = 5          # at most this many reasons for 80% of lost hours...
SHORT_LIST_SHARE = 0.20     # ...or at most this share of all reasons: a short list


def pareto_summary(reasons: list[dict]) -> dict | None:
    """How many reasons 80% of lost hours takes, and whether that is a short
    list (a few fixes recover most of it) or losses spread thin (no small set
    of fixes will)."""
    rs = sorted((r for r in reasons if r["seconds"] > 0), key=lambda r: -r["seconds"])
    if len(rs) < 3:
        return None
    total = sum(r["seconds"] for r in rs)
    run, need = 0.0, len(rs)
    for k, r in enumerate(rs, 1):
        run += r["seconds"]
        if run / total >= 0.8 - 1e-9:
            need = k
            break
    share = sum(r["seconds"] for r in rs[:need]) / total
    return {"need": need, "of": len(rs), "share": share,
            "short_list": need <= SHORT_LIST_MAX or need <= SHORT_LIST_SHARE * len(rs)}


# ------------------------------------------------- rhythm: when it changed

RHYTHM_MIN_SHIFT = 0.25     # a step must move the mean run burst by at least 25%...
RHYTHM_MIN_T = 3.0          # ...and stand at least 3 standard errors clear of the week-to-week noise
RHYTHM_MIN_WEEKS = 3        # with at least this many weeks either side


def change_point(weeks: list[dict], key: str = "run_min", *, min_shift: float = RHYTHM_MIN_SHIFT,
                 min_t: float = RHYTHM_MIN_T, min_weeks: int = RHYTHM_MIN_WEEKS) -> dict | None:
    """The single week where the series steps from one level to another, if
    there is one worth marking.

    Two-segment fit: the split that minimises the squared error around each
    side's mean. A split always exists, so it is only reported when the step
    is large (min_shift) and clear of the noise (Welch t >= min_t); otherwise
    None, and the chart marks nothing."""
    import statistics

    pts = [(i, w[key]) for i, w in enumerate(weeks) if w[key] is not None]
    if len(pts) < 2 * min_weeks:
        return None
    xs = [v for _, v in pts]
    best = None
    for k in range(min_weeks, len(xs) - min_weeks + 1):
        a, b = xs[:k], xs[k:]
        ma, mb = statistics.mean(a), statistics.mean(b)
        sse = sum((x - ma) ** 2 for x in a) + sum((x - mb) ** 2 for x in b)
        if best is None or sse < best[0]:
            best = (sse, k)
    k = best[1]
    a, b = xs[:k], xs[k:]
    ma, mb = statistics.mean(a), statistics.mean(b)
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    t = abs(ma - mb) / se if se else float("inf")
    shift = (mb - ma) / ma if ma else None
    if shift is None or abs(shift) < min_shift or t < min_t:
        return None
    return {"index": pts[k][0], "week": weeks[pts[k][0]]["week"], "before": ma, "after": mb,
            "weeks_before": len(a), "weeks_after": len(b), "shift": shift, "t": t}


# ---------------------------------------------------------- product pages

def _time_ticks(t0, t1) -> list[tuple]:
    """Tick positions for a local-time axis from t0 to t1: hours for a day or
    two, Mondays up to ~10 weeks, month starts beyond."""
    from datetime import datetime, timedelta

    a, b = t0.astimezone(D.TZ), t1.astimezone(D.TZ)
    span = (b - a).total_seconds() / 86400
    out = []
    if span <= 2.5:
        t = a.replace(minute=0, second=0, microsecond=0)
        step = 3 if span <= 1.2 else 6
        while t <= b:
            if t >= a and t.hour % step == 0:
                out.append((t, f"{t:%H}:00" if t.hour else f"{t.day} {t:%b}"))
            t = (t.replace(tzinfo=None) + timedelta(hours=1)).replace(tzinfo=D.TZ)
    elif span <= 75:
        d = a.date() + timedelta(days=(7 - a.weekday()) % 7)
        every = 1 if span <= 35 else 2
        k = 0
        while d <= b.date():
            if k % every == 0:
                out.append((datetime(d.year, d.month, d.day, tzinfo=D.TZ), f"{d.day} {d:%b}"))
            d += timedelta(days=7)
            k += 1
    else:
        y, m = a.year, a.month + 1
        if m > 12:
            y, m = y + 1, 1
        while datetime(y, m, 1, tzinfo=D.TZ) <= b:
            out.append((datetime(y, m, 1, tzinfo=D.TZ), D.MONTHS[m - 1][:3]))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _nice_ceiling(v: float) -> float:
    for s in (0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100):
        if v <= s:
            return s
    return 10 ** math.ceil(math.log10(v))


def run_timeline(by_line: dict[str, list[dict]], span: tuple) -> Markup:
    """Every run of one part, one panel per line on a shared time axis: down
    hours per run hour (y) at the run's start (x), dot area = run hours.

    The y axis is capped so one run of a few minutes cannot flatten the rest;
    a run above the cap sits on it as a triangle, with its true value in the
    tooltip. The dashed line is the part's ratio over all its runs on that
    line in the period - the same figure as the table above."""
    lines = [(dev, rs) for dev, rs in by_line.items() if rs]
    if not lines:
        return Markup('<p class=empty>No closed runs of this part in this period.</p>')
    t0, t1 = span
    if t1 <= t0:
        from datetime import timedelta
        t1 = t0 + timedelta(hours=1)
    ratios = sorted(r["ratio"] for _, rs in lines for r in rs if r["ratio"] is not None)
    # cap at the run-hour-weighted 95th percentile, so the axis follows the runs that matter
    weighted = sorted(((r["ratio"], r["run_s"]) for _, rs in lines for r in rs if r["ratio"] is not None))
    total = sum(w for _, w in weighted) or 1.0
    acc, p95 = 0.0, (ratios[-1] if ratios else 1.0)
    for v, w in weighted:
        acc += w
        if acc / total >= 0.95:
            p95 = v
            break
    refs = [l for _, rs in lines for l in [_ratio(rs)] if l is not None]
    ymax = _nice_ceiling(max([p95 * 1.1, *refs, 0.05]))
    max_run = max(r["run_s"] for _, rs in lines for r in rs) or 1.0

    W, L, R, PH, GAP, T = 960, 64, 150, 120, 34, 8
    pw = W - L - R
    H = T + len(lines) * (PH + GAP) + 30
    secs = (t1 - t0).total_seconds() or 1.0

    def X(t):
        return L + pw * min(max((t - t0).total_seconds() / secs, 0.0), 1.0)

    o = [f'<svg class=runs viewBox="0 0 {W} {H}" width="{W}" height="{H}" role=img '
         f'aria-label="Every run of this part: down hours per run hour over time, one panel per line">']
    ticks = _time_ticks(t0, t1)
    for k, (dev, rs) in enumerate(lines):
        top = T + k * (PH + GAP) + 16

        def Y(v):
            return top + PH - PH * min(v, ymax) / ymax
        o.append(f'<text x="{L}" y="{top - 6}" class=lab>{escape(dev)}</text>')
        for frac in (0, 0.5, 1):
            v = ymax * frac
            o.append(f'<line x1="{L}" x2="{L + pw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class=grid />'
                     f'<text x="{L - 8}" y="{Y(v) + 4:.1f}" class="tick end">{v:g}</text>')
        for t, lab in ticks:
            o.append(f'<line x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{top + PH}" y2="{top + PH + 4}" class=grid />')
            if k == len(lines) - 1:
                o.append(f'<text x="{X(t):.1f}" y="{top + PH + 18}" class="tick mid">{lab}</text>')
        ref = _ratio(rs)
        if ref is not None:
            o.append(f'<line x1="{L}" x2="{L + pw}" y1="{Y(ref):.1f}" y2="{Y(ref):.1f}" class=refline />'
                     f'<text x="{L + pw + 8}" y="{Y(ref) + 4:.1f}" class=tick>all runs {ref:.2f}</text>')
        for r in sorted(rs, key=lambda r: -r["run_s"]):
            if r["ratio"] is None:
                continue
            cx, cy = X(r["start"]), Y(r["ratio"])
            rad = max(3.0, 11 * math.sqrt(r["run_s"] / max_run))
            title = (f"{D.nice(r['start'].astimezone(D.TZ).date())} {r['start'].astimezone(D.TZ):%H:%M}"
                     f"{' · job ' + r['job'] if r.get('job') else ''} · {_h(r['run_s'])} run h · "
                     f"{_h(r['down_s'])} down h · {r['ratio']:.2f} down h per run h")
            if r.get("flagged_n"):
                title += (f" · {r['flagged_n']} implausible stop{'s' if r['flagged_n'] > 1 else ''} "
                          f"({_h(r['flagged_s'])} h) left out")
            if r["ratio"] > ymax:
                o.append(f'<path d="M{cx - 6:.1f},{cy + 5:.1f} L{cx + 6:.1f},{cy + 5:.1f} L{cx:.1f},{cy - 5:.1f} Z" '
                         f'class="dot capped" />')
                title += " (above the axis)"
            else:
                o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{rad:.1f}" class="dot runpt" />')
            o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{max(rad, 10):.1f}" class=hit>'
                     f'<title>{escape(title)}</title></circle>')
    o.append(f'<text x="14" y="{T + (H - T - 30) / 2}" class="axis mid" '
             f'transform="rotate(-90 14 {T + (H - T - 30) / 2})">down h per run h</text>')
    o.append("</svg>")
    key = ('<div class=key><span><i style="background:var(--unp);border-radius:50%"></i>One run; area = run hours</span>'
           '<span><i style="background:none;border-top:2px dashed var(--mute);height:0;vertical-align:middle"></i>'
           'All runs on that line</span><span>▲ above the axis: true value in the tooltip</span></div>')
    return Markup("".join(o) + key)


def _ratio(rs: list[dict]) -> float | None:
    run = sum(r["run_s"] for r in rs)
    return sum(r["down_s"] for r in rs) / run if run else None


def part_compare(rows: list[dict], highlight: str, href) -> Markup:
    """Down hours per run hour for every part on one line, this part
    highlighted: the like-for-like comparison, since the same line's other
    products share its crew, its machine and its reasons."""
    rs = sorted(rows, key=lambda r: -r["ratio"])
    if len(rs) < 2:
        return Markup('<p class=empty>No other part ran on this line in this period, so there is nothing to '
                      'compare with.</p>')
    label_w, W, row_h, T = 230, 960, 22, 8
    pw = W - label_w - 230
    H = T + len(rs) * row_h + 34
    xmax = _nice_ceiling(max(r["ratio"] for r in rs) or 0.1)

    def X(v):
        return label_w + pw * v / xmax
    o = [f'<svg class=compare viewBox="0 0 {W} {H}" width="{W}" height="{H}" role=img '
         f'aria-label="Down hours per run hour by part on this line">']
    for frac in (0, .25, .5, .75, 1.0):
        o.append(f'<line x1="{X(xmax * frac):.1f}" x2="{X(xmax * frac):.1f}" y1="{T - 4}" y2="{H - 30}" class=grid />'
                 f'<text x="{X(xmax * frac):.1f}" y="{H - 14}" class="tick mid">{xmax * frac:g}</text>')
    for i, r in enumerate(rs):
        y = T + i * row_h
        me = r["part"] == highlight
        name = escape(r["part"])
        lab = (f'<text x="{label_w - 8}" y="{y + 15}" class="end lab{" me" if me else ""}">{name}</text>' if me else
               f'<a href="{escape(href(r["part"]))}"><text x="{label_w - 8}" y="{y + 15}" class="end tick">{name}</text></a>')
        o.append(f'<g class=pbar>{lab}'
                 f'<rect x="{label_w}" y="{y + 5}" width="{max(1.0, X(r["ratio"]) - label_w):.1f}" height="{row_h - 9}" '
                 f'rx="2" class="{"bar" if me else "bar other"}" />'
                 f'<text x="{X(r["ratio"]) + 6:.1f}" y="{y + 15}" class="{"lab" if me else "tick"}">{r["ratio"]:.2f}'
                 f'<tspan class=labval> · {_h(r["run_s"])} run h</tspan></text>'
                 f'<rect x="0" y="{y}" width="{W}" height="{row_h}" class=hitrow>'
                 f'<title>{name} · {r["ratio"]:.2f} down h per run h · {_h(r["down_s"])} down h over '
                 f'{_h(r["run_s"])} run h · {r["runs"]} run{"s" if r["runs"] != 1 else ""}</title></rect></g>')
    o.append("</svg>")
    return Markup("".join(o))


def ict_chart(by_line: dict[str, dict], span: tuple) -> Markup:
    """Derived ideal cycle time (s per cycle) run by run, one panel per line,
    with each configuration level drawn as a line and every reconfiguration
    marked. Hollow rings are single runs off their level (a change made
    mid-run, or a counting glitch), which do not count as reconfigurations."""
    lines = [(dev, v) for dev, v in by_line.items() if v["points"]]
    if not lines:
        return Markup('<p class=empty>No run of this part over 200 cycles in this period, so no ideal cycle time '
                      'can be derived.</p>')
    t0, t1 = span
    W, L, R, PH, GAP, T = 960, 64, 150, 110, 40, 8
    pw = W - L - R
    H = T + len(lines) * (PH + GAP) + 30
    secs = (t1 - t0).total_seconds() or 1.0

    def X(t):
        return L + pw * min(max((t - t0).total_seconds() / secs, 0.0), 1.0)
    o = [f'<svg class=ict viewBox="0 0 {W} {H}" width="{W}" height="{H}" role=img '
         f'aria-label="Derived ideal cycle time by run, one panel per line">']
    ticks = _time_ticks(t0, t1)
    for k, (dev, v) in enumerate(lines):
        top = T + k * (PH + GAP) + 16
        pts, lv = v["points"], v["levels"]
        vals = [p["ict"] for p in pts] + [l["value"] for l in lv["levels"]]
        lo, hi = min(vals), max(vals)
        pad = max((hi - lo) * 0.15, hi * 0.02)
        lo, hi = lo - pad, hi + pad

        def Y(x):
            return top + PH - PH * (x - lo) / (hi - lo)
        o.append(f'<text x="{L}" y="{top - 6}" class=lab>{escape(dev)}</text>')
        for frac in (0, 0.5, 1):
            x = lo + (hi - lo) * frac
            o.append(f'<line x1="{L}" x2="{L + pw}" y1="{Y(x):.1f}" y2="{Y(x):.1f}" class=grid />'
                     f'<text x="{L - 8}" y="{Y(x) + 4:.1f}" class="tick end">{x:.3f}</text>')
        for t, lab in ticks:
            if k == len(lines) - 1:
                o.append(f'<text x="{X(t):.1f}" y="{top + PH + 18}" class="tick mid">{lab}</text>')
        for l in lv["levels"]:
            xa, xb = X(max(l["start"], t0)), X(min(l["end"], t1))
            if l["end"] < t0 or l["start"] > t1:
                continue
            o.append(f'<line x1="{xa:.1f}" x2="{max(xb, xa + 2):.1f}" y1="{Y(l["value"]):.1f}" '
                     f'y2="{Y(l["value"]):.1f}" class=level />')
        for c in lv["changes"]:
            if not t0 <= c["start"] <= t1:
                continue
            cx = X(c["start"])
            o.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{top}" y2="{top + PH}" class=change />'
                     f'<text x="{cx + 6:.1f}" y="{top + 10}" class=changelab>reconfigured '
                     f'{c["start"].astimezone(D.TZ).day} {c["start"].astimezone(D.TZ):%b}: {c["before"]:.3f} to '
                     f'{c["after"]:.3f} s ({c["shift"]:+.0%})</text>')
        for i, p in enumerate(pts):
            cx, cy = X(p["start"]), Y(p["ict"])
            off = p["outlier"]
            title = (f"{D.nice(p['start'].astimezone(D.TZ).date())} · {p['ict']:.3f} s per cycle · "
                     f"{p['cycles']:,.0f} cycles" + (" · off its level: one run, not a reconfiguration" if off else ""))
            o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" class="pt {"off" if off else "on"}" />'
                     f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="10" class=hit><title>{escape(title)}</title></circle>')
    o.append(f'<text x="14" y="{T + (H - T - 30) / 2}" class="axis mid" '
             f'transform="rotate(-90 14 {T + (H - T - 30) / 2})">seconds per cycle</text>')
    o.append("</svg>")
    key = ('<div class=key><span><i style="background:var(--ink);border-radius:50%"></i>One run over 200 cycles</span>'
           '<span><i style="background:none;border:2px solid var(--ink);border-radius:50%;width:6px;height:6px"></i>'
           'Off its level: a single run</span>'
           '<span><i style="background:var(--mute);height:2px;vertical-align:middle"></i>Configuration level</span>'
           '<span>The axis does not start at zero: it is drawn to show small shifts.</span></div>')
    return Markup("".join(o) + key)


def changeover_inout(changes: list[dict], part: str, href) -> Markup:
    """Two tables: changes into this part (by the part before) and out of it
    (by the part after), each with count and mean planned-stop hours; the
    mean cell wears the changeover ramp, as on the line's matrix."""
    def table(title, rows):
        if not rows:
            return f'<div><h3>{title}</h3><p class=empty>None in this period.</p></div>'
        o = [f'<div><h3>{title}</h3><table class=inout><thead><tr><th>Part</th><td>Changes</td>'
             f'<td>Mean h</td><td>Total h</td></tr></thead><tbody>']
        for other, cs in sorted(rows.items(), key=lambda kv: -sum(c["seconds"] for c in kv[1]) / len(kv[1])):
            total = sum(c["seconds"] for c in cs)
            mean_h = total / len(cs) / 3600
            fill = CO_RAMP[co_bin(mean_h)]
            name = (f'{escape(other)} <small class=muted>same part, new job</small>' if other == part
                    else f'<a class=lnk href="{escape(href(other))}">{escape(other)}</a>')
            o.append(f'<tr><th>{name}</th><td>{len(cs)}</td>'
                     f'<td style="background:{fill};color:{ink_for(fill)}">{mean_h:.1f}</td>'
                     f'<td>{total / 3600:.1f}</td></tr>')
        o.append("</tbody></table></div>")
        return "".join(o)

    into: dict[str, list] = {}
    out: dict[str, list] = {}
    for c in changes:
        if c["to"] == part:
            into.setdefault(c["from"], []).append(c)
        elif c["from"] == part:
            out.setdefault(c["to"], []).append(c)
    if not into and not out:
        return Markup('<p class=empty>No changes into or out of this part in this period.</p>')
    return Markup('<div class=pair>' + table("Into it, from", into) + table("Out of it, to", out) + "</div>")
