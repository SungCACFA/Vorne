"""Static HTML export: every page, every period, in one self-contained file.

    python -m webapp export                      -> exports/danbro_<date>_<time>.html
    python -m webapp export --out report.html

Every fragment is rendered by the live app's own routes and templates (in
read-only export mode), so the snapshot cannot disagree with the app. The
fragments are split into chunks, each distinct chunk stored once, then
gzipped and embedded; the browser decompresses natively (DecompressionStream)
and rebuilds a page's HTML only when it is shown. See pack().
No external assets: no fonts, scripts or stylesheets are fetched.

Fixed at export time: the default fleet window (no per-line widening), the
SAP unit (seconds per piece), and every value shown. Every product page is
included and reached from the part names; the line page's product filter is
live-app only (one line x product x period each would multiply the file).
"""

from __future__ import annotations

import base64
import copy
import gzip
import json
import re
import sqlite3
import time
from contextlib import closing
from urllib.parse import quote
from datetime import datetime
from pathlib import Path

from werkzeug.datastructures import MultiDict

from . import ROOT, create_app, select_window
from . import data as D

PARTS = ("sub", "head", "panel", "body", "foot")

# Chunk boundaries: after a newline, a table row, a group, a link, or any
# closed SVG element. gzip only looks back 32 KB and a line page's body is
# bigger than that, so the same hour rows and chart pieces were compressed
# again in every day, week, month and year view (6.1 MB). Storing each chunk
# once roughly halves the file and drops nothing.
CHUNK = re.compile(r"(?<=\n)|(?<=</tr>)|(?<=</g>)|(?<=</a>)|(?<=</rect>)|(?<=</circle>)|(?<=</text>)|(?<=/>)")


def pack(strings: list[str]) -> tuple[list[str], list[list[int]]]:
    """Each string as a sequence of chunk ids into one table of distinct
    chunks. A run of consecutive ids (new chunks are numbered in order) is
    written as its first id then minus the number that follow:
    [7, -3] = chunks 7, 8, 9, 10. unpack() is the inverse; the shell's
    expand() is the same in JavaScript."""
    table: list[str] = []
    index: dict[str, int] = {}
    seqs = []
    for text in strings:
        ids = []
        for c in CHUNK.split(text):
            if c:
                if c not in index:
                    index[c] = len(table)
                    table.append(c)
                ids.append(index[c])
        seq, i = [], 0
        while i < len(ids):
            j = i
            while j + 1 < len(ids) and ids[j + 1] == ids[j] + 1:
                j += 1
            seq += [ids[i]] if j == i else [ids[i], -(j - i)]
            i = j + 1
        seqs.append(seq)
    return table, seqs


def unpack(table: list[str], seq: list[int]) -> str:
    out, prev = [], 0
    for v in seq:
        if v < 0:
            out += table[prev + 1:prev + 1 - v]
        else:
            out.append(table[v])
            prev = v
    return "".join(out)
MARK = {k: re.compile(rf"<!--{k}-->(.*?)<!--/{k}-->", re.S) for k in PARTS}


def _freeze_data_layer(snap: D.Snapshot, opens: list) -> dict:
    """Serve one snapshot and one open-run reading to all ~1,300 renders, so
    every page describes the same moment. Returns the originals to restore."""
    saved = {"load": D.load, "open_runs": D.open_runs, "implausible": D.implausible}
    D.load = lambda _path: snap
    # Copies: merge_part_job relabels the objects it is given.
    D.open_runs = lambda _path, now=None: [copy.copy(r) for r in opens]
    # The flags depend only on the cache and the rule, and every line and product page asks again.
    memo: dict = {}
    original = saved["implausible"]

    def implausible(path, rule=D.ImplausibleRule(), dev=None):
        if (rule, dev) not in memo:
            memo[(rule, dev)] = original(path, rule, dev)
        return [dict(r) for r in memo[(rule, dev)]]
    D.implausible = implausible
    return saved


# The export's own file names. Only these are ever pruned.
EXPORT_NAME = re.compile(r"danbro_\d{4}-\d{2}-\d{2}_\d{4}\.html")


def prune(keep: Path, log=print) -> list[Path]:
    """Delete every earlier export beside `keep`, so the folder only ever
    holds the current snapshot and an old one cannot be the one attached to
    an email. Only files named like an export are touched."""
    gone = [f for f in keep.parent.iterdir()
            if f.is_file() and f != keep and EXPORT_NAME.fullmatch(f.name)]
    for f in gone:
        f.unlink()
        log(f"deleted the earlier export {f.name}")
    return gone


def build(config_path: str | Path | None = None, out: str | Path | None = None, log=print,
          keep: bool = False) -> Path:
    app = create_app(config_path) if config_path else create_app()
    cache = app.config["CACHE_PATH"]
    snap = D.load(cache)
    exported_at = datetime.now(D.TZ)
    saved = _freeze_data_layer(snap, D.open_runs(cache, now=exported_at))
    try:
        client = app.test_client()
        pages = [("overview", "Overview", "/")] + [(d, d, f"/line/{d}") for d in snap.devices] + \
                [("products", "Products", "/products")]
        with closing(sqlite3.connect(f"file:{cache}?mode=ro", uri=True)) as db:
            names = [r[0] for r in db.execute("SELECT DISTINCT part FROM part_run WHERE part IS NOT NULL "
                                              "AND deleted_at IS NULL ORDER BY part")]
        products = [("part:" + n, n, "/product/" + quote(n)) for n in names if D.part_days(cache, n)]
        periods: dict[str, list] = {}
        for gran, _ in D.GRANULARITIES:
            periods[gran] = select_window(snap, MultiDict({"g": gran})).periods

        strings: list[str] = []
        index: dict[str, int] = {}

        def intern(s: str) -> int:
            if s not in index:
                index[s] = len(strings)
                strings.append(s)
            return index[s]

        frags: dict = {}
        t0, n = time.monotonic(), 0
        for page, _, url in pages + products:
            frags[page] = {}
            for gran, _ in D.GRANULARITIES:
                frags[page][gran] = {}
                for key, _label in periods[gran]:
                    resp = client.get(url, query_string={"g": gran, "p": key, "_export": "1"})
                    if resp.status_code != 200:
                        raise RuntimeError(f"{url} {gran} {key}: HTTP {resp.status_code}")
                    html = resp.get_data(as_text=True)
                    found = {k: MARK[k].search(html) for k in PARTS}
                    missing = [k for k, m in found.items() if m is None]
                    if missing:
                        raise RuntimeError(f"{url}: template markers missing: {missing}")
                    win = select_window(snap, MultiDict({"g": gran, "p": key}))
                    ndays = len(win.days(page if page in snap.devices else None))
                    frags[page][gran][key] = [intern(found[k].group(1)) for k in PARTS] + [ndays]
                    n += 1
            if not page.startswith("part:"):
                log(f"  {page:9} rendered ({n} fragments, {time.monotonic() - t0:.0f}s)")
        log(f"  {len(products)} product pages rendered ({n} fragments, {time.monotonic() - t0:.0f}s)")
    finally:
        D.load, D.open_runs, D.implausible = saved["load"], saved["open_runs"], saved["implausible"]

    chunks, seqs = pack(strings)
    bad = [i for i, text in enumerate(strings) if unpack(chunks, seqs[i]) != text]
    if bad:     # never ship a snapshot that would not rebuild its own pages
        raise RuntimeError(f"{len(bad)} fragments do not survive packing, first #{bad[0]}")
    last_day = snap.last_day
    bundle = {
        "pages": [[p, label] for p, label, _ in pages],
        "products": [[p, label] for p, label, _ in products],
        "grans": [list(g) for g in D.GRANULARITIES],
        "periods": {g: [list(p) for p in ps] for g, ps in periods.items()},
        "chunks": chunks,
        "seqs": seqs,
        "frags": frags,
        "stamp": {
            "exported": f"{D.nice(exported_at.date())} {exported_at:%H:%M}",
            "refreshed": (f"{D.nice(snap.last_refresh.astimezone(D.TZ).date())} "
                          f"{snap.last_refresh.astimezone(D.TZ):%H:%M}") if snap.last_refresh else "unknown",
            "from": D.nice(D.production_day(snap.fleet_from_day)) if snap.fleet_from_day else "—",
            "to": D.nice(D.production_day(last_day)) if last_day is not None else "—",
        },
    }
    raw = json.dumps(bundle, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    packed = base64.b64encode(gzip.compress(raw, compresslevel=9)).decode("ascii")
    css = (ROOT / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
    sticky = (ROOT / "webapp" / "templates" / "_sticky.html").read_text(encoding="utf-8")
    html = (SHELL.replace("/*CSS*/", css).replace("/*STICKY*/", sticky)
                 .replace("__BUNDLE__", packed)
                 .replace("__EXPORTED__", bundle["stamp"]["exported"]))

    out = Path(out) if out else ROOT / "exports" / f"danbro_{exported_at:%Y-%m-%d_%H%M}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB; {len(raw) / 1e6:.1f} MB before compression, "
        f"{len(strings)} distinct fragments, {len(chunks):,} distinct chunks)")
    if not keep:     # only once the new file is safely written
        prune(out, log)
    return out


# The shell. Styles are the app's own stylesheet, inlined. The font stack
# falls back to the system UI face: Libre Franklin is not embedded, because
# fetching it would be an external asset.
SHELL = r"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Danbro packaging · snapshot __EXPORTED__</title>
<style>/*CSS*/
.stamp{font-size:13px;color:var(--mute);margin:-20px 0 22px}
.stamp strong{color:var(--ink);font-weight:600}
#loading{color:var(--mute);padding:28px 0}
nav button{background:none;border:0;border-bottom:2px solid transparent;margin-bottom:-1px;
padding:8px 12px;font:inherit;font-weight:600;color:var(--mute);cursor:pointer}
nav button[aria-current=page]{color:var(--ink);border-bottom-color:var(--ink)}
@media print{.ctrl,nav{display:none}}
</style>
<div class=wrap>
<h1>Danbro packaging</h1>
<p class=sub id=sub></p>
<p class=stamp id=stamp></p>
<nav id=nav></nav>
<div class=ctrl data-bar>
  <div class=seg role=group aria-label=Granularity id=gran></div>
  <label class=sr for=period>Period</label><select id=period></select>
  <span id=ndays></span>
</div>
<main id=main><p id=loading>Opening the snapshot…</p></main>
<footer id=foot></footer>
</div>
/*STICKY*/
<script>
const PACKED = "__BUNDLE__";
let B = null, page = 'overview', gran = 'month', period = null;

async function unpack() {
  if (!('DecompressionStream' in window)) throw new Error('needs a browser with DecompressionStream (Edge/Chrome 80+, Firefox 113+, Safari 16.4+)');
  const bytes = Uint8Array.from(atob(PACKED), c => c.charCodeAt(0));
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
  return JSON.parse(await new Response(stream).text());
}

// A fragment's HTML from its chunk ids; see pack() in export.py. Built when
// first shown and kept, so opening the file never builds every page at once.
const built = new Map();
function text(n) {
  if (!built.has(n)) {
    let o = '', prev = 0;
    for (const v of B.seqs[n]) {
      if (v < 0) { for (let k = 1; k <= -v; k++) o += B.chunks[prev + k]; }
      else { o += B.chunks[v]; prev = v; }
    }
    built.set(n, o);
  }
  return built.get(n);
}

function fromHash() {
  const [p, g, k] = location.hash.slice(1).split('/').map(decodeURIComponent);
  if (B.frags[p]) page = p;
  if (B.periods[g]) gran = g;
  period = k || null;
}

function render() {
  const ps = B.periods[gran];
  if (!ps.some(([k]) => k === period)) period = ps.length ? ps[0][0] : null;
  document.getElementById('nav').innerHTML = B.pages.map(([k, l]) =>
    `<button data-page="${k}"${k === page || (k === 'products' && page.startsWith('part:')) ? ' aria-current=page' : ''}>${l}</button>`).join('');
  document.getElementById('gran').innerHTML = B.grans.map(([k, l]) =>
    `<button data-gran="${k}" aria-pressed="${k === gran}">${l}</button>`).join('');
  document.getElementById('period').innerHTML = ps.map(([k, l]) =>
    `<option value="${k}"${k === period ? ' selected' : ''}>${l}</option>`).join('');
  const f = period && B.frags[page][gran][period];
  if (!f) { document.getElementById('main').innerHTML = '<p class=empty>No data.</p>'; return; }
  const s = i => text(f[i]);
  document.getElementById('sub').innerHTML = s(0);
  document.getElementById('main').innerHTML = s(1) + s(2) + s(3);
  document.getElementById('foot').innerHTML = s(4);
  document.getElementById('ndays').textContent = f[5] + ' production day' + (f[5] === 1 ? '' : 's');
  const st = B.stamp;
  document.getElementById('stamp').innerHTML =
    `Snapshot exported <strong>${st.exported}</strong> · cache refreshed ${st.refreshed} · ` +
    `production days ${st.from} – ${st.to} · read-only`;
  document.title = 'Danbro packaging · ' + document.getElementById('sub').textContent + ' · snapshot ' + st.exported;
  history.replaceState(null, '', '#' + [page, gran, period].map(encodeURIComponent).join('/'));
}

document.addEventListener('click', ev => {
  const row = ev.target.closest('[data-filter-day]'), clear = ev.target.closest('[data-filter-clear]');
  if (!row && !clear) return;
  ev.preventDefault(); ev.stopPropagation();
  const day = row ? row.dataset.filterDay : null;
  const table = document.querySelector('table.hours'), bar = document.querySelector('.filterbar');
  if (!table || !bar) return;
  let n = 0;
  table.classList.toggle('filtered', !!day);
  for (const tr of table.tBodies[0].rows) { const m = !!day && tr.dataset.day === day; tr.classList.toggle('match', m); n += m; }
  bar.hidden = !day;
  if (day) { bar.querySelector('[data-filter-label]').textContent = day + ' (' + n + ' scheduled hour' + (n === 1 ? '' : 's') + ')';
             document.getElementById('hours').scrollIntoView({behavior: 'smooth'}); }
}, true);
document.addEventListener('click', ev => {
  const p = ev.target.closest('[data-page]'), g = ev.target.closest('[data-gran]');
  if (p) { page = p.dataset.page; render(); return; }
  if (g) { gran = g.dataset.gran; period = null; render(); return; }
  // Links inside the pages point at the live app's routes; follow them here.
  const a = ev.target.closest('a[href^="/"]');
  if (!a) return;
  ev.preventDefault();
  const u = new URL(a.getAttribute('href'), 'http://x');
  page = u.pathname.startsWith('/line/') ? decodeURIComponent(u.pathname.slice(6))
       : u.pathname.startsWith('/product/') ? 'part:' + decodeURIComponent(u.pathname.slice(9))
       : u.pathname.startsWith('/products') ? 'products' : 'overview';
  if (!B.frags[page]) page = 'products';
  if (u.searchParams.get('g')) gran = u.searchParams.get('g');
  if (u.searchParams.get('p')) period = u.searchParams.get('p');
  render(); window.scrollTo(0, 0);
});
document.getElementById('period').addEventListener('change', e => { period = e.target.value; render(); });

unpack().then(b => { B = b; fromHash(); render(); })
  .catch(e => { document.getElementById('main').innerHTML = '<p class=empty>Could not open this snapshot: ' + e.message + '</p>'; });
</script>
</html>
"""
