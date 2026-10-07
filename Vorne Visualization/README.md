# Danbro packaging: Vorne XL reporting

Time accounting, downtime reasons and product standards for the four packaging lines, read from the Vorne XL
devices and served as a web app. It replaces the manual notebook report (`danbro.html`).

> **Before you delete or rebuild the cache, export the SAP standards.** SAP values and pieces per cycle are typed
> in by people and live **only** in `cache/xl_cache.db`. Everything else in that file can be pulled again from the
> devices; those values cannot. Products page → **Export CSV** first, **Load CSV** afterwards.

## What runs where, today

```
  [FR8   10.15.22.19 ]──┐
  [AreaB 10.15.22.83 ]──┤   python -m xlpipe refresh  →  cache/xl_cache.db  →  python -m webapp
  [AreaD 10.15.22.95 ]──┤   (closed events, every 5 min)  (SQLite)              (Flask, read-mostly)
  [AreaE 10.15.22.119]──┘
```

Everything runs on one machine on the plant LAN. The Azure Postgres target in `BRIEF.md` is **parked**: outbound
5432 from the plant is blocked by a firewall whose public egress IP we can't yet determine. That is an IT
dependency. The schema (`schema.sql`) and the connectivity check (`preflight.py`) are written and waiting; the sync
writes through a storage interface (`xlpipe/store.py`), so moving from the SQLite cache to Postgres (direct, or HTTPS
to an endpoint in Azure) swaps one class and nothing upstream.

## Setup

Windows, Python 3.11 or later.

```powershell
cd "C:\...\Vorne Visualization"
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m xlpipe refresh        # first run backfills each device's full history: about 30 s
.venv\Scripts\python -m webapp                # http://127.0.0.1:5000
```

Devices, channels and configuration epochs are in `config.yaml`. There are no credentials until a database exists;
when there are, they go in `.env`, which is git-ignored.

## Commands

| | |
|---|---|
| `python -m xlpipe refresh` | Pull closed events from all four devices into the cache. Single instance: a second run exits immediately (code 3). |
| `python -m xlpipe refresh --device FR8 --channel shift_hour` | Narrow it down. Both repeatable. |
| `python -m xlpipe refresh --record DIR` | Also save every raw device response. This is how the test fixtures were made. |
| `python -m xlpipe report` | What the cache holds: rows, date ranges, epochs, the brief's claims checked. |
| `python verify.py` | Is the sync *correct*? Cache totals against the device's own aggregates, plus the data checks below. Exit 1 on any failure. |
| `python -m webapp [--port N]` | The app. Reads the cache on every request, so a refresh shows on the next page load. Also runs the live writer for `/live` (`--no-live-writer` to leave it to a separate process). |
| `python -m xlpipe live [--interval S]` | The live writer on its own: every device's real-time data into `current_state`, every 5 s. One writer at a time. |
| `python -m webapp export [--out FILE] [--keep]` | A self-contained, read-only HTML snapshot of every page, product page and period (≈3 min, ≈2.6 MB). Deletes earlier exports in the same folder unless `--keep`. See below. |
| `python -m pytest` | 344 tests, no device needed (≈4–7 min, most of it the export tests). |
| `python tests/mutation_check.py` | Would the tests notice if the code were wrong? See Tests. |
| `python preflight.py` | When there is a database path: DNS, TCP 5432, TLS, auth, permissions, latency, as separate steps. |

**Schedule the refresh** every five minutes with Task Scheduler: action
`.venv\Scripts\python.exe -m xlpipe refresh`, start in the project folder, "run whether user is logged on or not",
"do not start a new instance" if one is running. Nothing on the devices changes faster than hourly; five minutes is
ample, and the lock makes overlap harmless.

## How the sync works

The XL devices are historians: each keeps its whole history (back to installation, January–April 2026). The sync
copies **closed** events incrementally from the channel endpoints and never samples live values.

- **Resumes on `sync_id`**, which the device advances on every create, update and delete. Verified that
  `sync_id[gt]` filters whole events, so an incremental pull returns full totals. `record_order` is a fallback
  only, used if a channel fails the `sync_id` self-test; it reflects an event's *first* slice and cannot see edits.
- **Never stores an open event.** The device marks the current event `sync_id = -1`. The high-water mark never moves
  past an open event. (A bug here once left the mark NULL for every channel that fitted in one page, so they
  re-backfilled every run. `tests/test_sync.py` has the regression test.)
- **Re-pulls the last 7 production days every run** (`sync.window_days`). This catches reasons assigned after the
  fact — without it the downtime Pareto would be stuck on "Missing Reason" — retries anything quarantined, and
  reconciles deletes: anything we hold that the device no longer has is **flagged** `deleted_at`, never removed. A
  safety valve refuses to flag more than 20% of the window at once.
- **Upserts are guarded by `sync_id`**: a row is only overwritten by a strictly newer version, so a replay can never
  undo an edit.
- **Captures each channel's open event** separately (`open_event`), so the app can mark it and warn about it.
- **Malformed rows are quarantined** with the raw payload and the reason, and the rest load. Errors that would affect
  every row (schema drift, permissions) hold the run instead of quarantining row by row.
- A device that cannot be reached is skipped; the others carry on.

## What the cache holds

`cache/xl_cache.db` (SQLite, ~10 MB). Fact tables match `schema.sql` column for column.

| Table | From | Key | Notes |
|---|---|---|---|
| `hour_record` | `shift_hour` | `shift_hour_event_id` | The reporting backbone. Generated `is_full_hour`, `acct_gap_s`, `is_count_adjustment`. |
| `shift_record` | `shift` | `shift_event_id` | |
| `state_event` | `process_state` | `process_state_event_id` | Downtime reasons; the Pareto source. |
| `part_run`, `job_run` | `part`, `job` | `part_event_id`, `job_event_id` | |
| `quality_event` | `quality_event` | `record_id` | Empty on all four devices. Modelled so it fills the day reject scanning is set up. |
| `open_event` | each channel's `events/current` | device, channel | Replaced on every refresh. |
| `device_config_epoch` | `config.yaml` | device, epoch | Which eras are usable, and whether their counts are trusted. |
| `part_standard` | **people** | `part` | Derived ideal cycle time (refreshed by the sync), plus **SAP standard and pieces per cycle, entered by hand.** |
| `sync_state`, `sync_quarantine`, `sync_heartbeat`, `channel_field` | the sync | | Resumption marks, rejected rows, last run, what each channel's schema offered. |

**`part_standard` is the one table that is not a cache.** Its SAP and pieces-per-cycle columns exist nowhere else
until Postgres does. Deleting or rebuilding `xl_cache.db` deletes them. Export the CSV from the products page before
any rebuild and load it back afterwards. The export round-trips.

Natural keys are always the device's own ids, never timestamps: Split Down Event rewrites event boundaries, so a
start-time key would turn an edit into a duplicate.

## Known data-quality issues, and why each check exists

Every one of these was found in the real data. Each has a check, so that if it recurs, or a fix appears to work,
the data says so rather than someone noticing in a meeting.

**1. Piece counts are unreliable. Counts are withheld; time-based rates are shown.** `reject_count` is derived as
`in_count − good_count`, the reject log (`quality_event`) is empty everywhere, and where the good-count sensor sees
more than infeed, rejects go negative (hundreds of hours per line). The device was never told how many pieces come
out per cycle (`expected_count_per_cycle` = 0.0 on every unit). So quality, OEE and TEEP are not published.
Availability, performance and calendar use derive from time, which reconciles. *Check:* counts are stored exactly as
sent — `tests/test_counts_preserved.py` fails if anyone clamps, recomputes or `abs()`es them, and was proven to catch
that by trying it. Counts are only shown for configuration epochs marked `counts_trusted` (none yet).

**2. Six hours look like manual count entry.** Everywhere else `reject = in − good`. In six hours it doesn't: three have
`in` of 0 or 1 beside thousands good, two have reject at exactly half of `in − good`, all six have near-zero run time.
Hypothesis, unconfirmed: counts keyed in by hand (Vorne has been asked whether XL logs adjustments). *Check:*
`data_quality` category `suspected_manual_count_entry`; `verify.py` lists them; the tests pin all six and fail if a
seventh appears.

| Line | Hour (Toronto) | In | Good | Reject |
|---|---|---|---|---|
| AreaB | Thu 20 Aug 2026 13:00 | 0 | 3,316 | −2,379 |
| AreaB | Fri 25 Sep 2026 16:00 | 1 | 237 | 0 |
| FR8 | Wed 24 Jun 2026 14:00 | 2,928 | 115 | 2,378 |
| FR8 | Thu 6 Aug 2026 21:00 | 744 | 0 | 372 |
| FR8 | Tue 22 Sep 2026 08:00 | 0 | 3,278 | −1,657 |
| FR8 | Tue 22 Sep 2026 10:00 | 588 | 4 | 292 |

**3. Count adjustments.** Rows where `reject = −good` with no run time look like after-the-fact corrections rather
than production (193 on FR8). *Check:* generated column `is_count_adjustment`; `data_quality` category.

**4. Device clocks were on the wrong timezone at first.** FR8 recorded Central time (−06:00) until 23 Feb 2026; the
other three spent their first hours on −05:00. Stored times are correct (UTC), but shift and production-day
boundaries in those eras are an hour off on a Toronto clock. *Check:* configuration epoch 1 on each device is marked
unusable with its reason; every page defaults to the **fleet window** (from production day 2 Apr 2026, when all four
are usable) and says so in a visible panel; each line can be widened to its own usable history.

**5. Runs left open on the device.** AreaD's part/job run (OTRIVIN, job 3043984) was opened on 25 Aug 2026 and never
closed: six weeks, 101 run-hours, all accumulating into one run, with nothing watching. While the alert was being
built, it found a **second** case the same day: AreaB OTRIVIN crossing seven days. This is why the check exists.
*Check:* a red alert at the top of the overview, line and products pages for any run open more than **7 days**, or
more than **3× that part's median run on that line**. The median uses only runs of more than 200 cycles: lines record
short fragments (10 of FR8's 22 ADVIL runs), and counting them would make an ordinary run read as "41× median"
until nobody trusted the alert. Open runs are listed first and marked on the products page, and are never folded
into totals.

**6. Performance above 100%.** That means the ideal cycle time configured for the part is wrong, not that the machine
ran fast. Of 23 such hours, only 6 had at least five minutes of running and were at least 1% over; the other 17
were either very short runs (12 under five minutes, typically 100.1% on 1–3 minutes, where one cycle's rounding tips
it over) or within 1% of 100%. *Check:* every one is flagged in red; substantial ones say "wrong
ideal cycle time", short ones say "over 100% on a 3-min run", so the six real signals aren't buried.

**7. Negative durations.** Five FR8 `not_monitored` state events end before they start (worst −34 s), most likely
the device clock being corrected mid-event. Kept as reported; displayed as 0, never "−0.0". *Check:* `data_quality`
category `negative_duration_suspected_clock_correction`, across every fact table; `verify.py` lists them.

**8. Changeover hours carry several values.** An hour (or shift, or state) spanning a changeover returns `part`, `job`,
`shift` as a list. Hundreds of rows. Stored comma-joined in the device's order (`SOFDRA,OTRIVIN`; not guaranteed
chronological) and shown on the line page as a tinted, labelled changeover row. *Check:* `tests/test_reader.py`.

**9. The device skips event ids.** AreaB has no `shift_hour_event_id` 2759 or 2811, with no gap in time. *Check:*
`verify.py` compares our ids with the device's own list, so a hole is a failure only when the device has the record
and we don't.

**10. Part and job runs match 1:1, today.** Every part run has a job run with identical boundaries, on every line.
That is a configuration artefact, not a guarantee: a changeover mid-job would break it. *Check:* `verify.py` flags
the day it stops holding; the app does not assume it.

**11. Calendar use is not the old report's "Utilised".** The old column was computed from full hours only, which
dropped the overnight not-scheduled blocks — the very denominator it measures. September 2026 reads **35.1%** calendar
use; the old method gives 75.0%. A different name for a different measure; every page's footer explains it.

**12. Production days are not calendar dates.** A production day starts with its first shift: 23:00 the evening
before when a third shift runs, otherwise 07:00. An unscheduled night stays with the day before. Labelled
"production day" everywhere.

## Device API behaviours worth knowing

Verified on firmware 2.22.14.2. Trust these over the vendor PDF.

- `/events` needs `fields` and returns positional arrays in that order; a length mismatch is an error, never a guess.
- `api/v0/device` wraps every value as `{"metadata": …, "value": …}`; unwrap twice.
- The device writes "nothing" as the **string** `"null"`. Stored as SQL NULL.
- `sync_id` is one counter shared by every channel on a device — effectively a transaction log.
- **`group=none` ignores the `sync_id` filter** and folds the open event into the aggregate. `verify.py` filters on
  "event id below the current event" instead. Anything aggregating on the device must do the same.
- `expected_count_per_cycle` is `ephemeral`: only the open event carries a value; history returns NULL.
- History starts at installation (FR8 20 Jan 2026, the others late March / 1 Apr); nothing has been pruned yet.

## Charts, product pages and the product filter

All charts are inline SVG drawn on the server (`webapp/charts.py`): nothing is fetched, so they work the same in the
app, the export and print. Every mark has a tooltip, every chart has a one-line note on how to read it and what to
do, and every chart follows the granularity and period controls. Thresholds live in `config.yaml` under `charts:`.

**Line page**, in order: hour-by-hour heatmap (one row per production day, 23:00 to 23:00; above the hour table),
run/stop rhythm by week with the change point marked when the run burst steps by ≥25% at Welch t ≥ 3, hours by
activity, the stop-reason quadrant (fixed lines: 1 stop per production day, 30 minutes), the Pareto with a verdict
(a "short list" is ≤5 reasons or ≤20% of reasons for 80% of lost hours), the reason tables, the changeover matrix.

**Implausible stops.** A planned or unplanned stop over 24 h, or over 12 h and 10× that state's median on that line,
is flagged in a panel (42 on the recording; AreaB's 260 h "maintenance" in July). The **charts leave them out and say
how many**; the **tables keep everything**. Nothing is recomputed in the stored data.

**Product pages** (`/product/<part>`, linked wherever a part is named): which lines ran it and how (runs, run h,
down h, down h per run h, availability within its runs, performance, current ICT); every run on a timeline; the
part against the same line's other products, by down hours per run hour; its own stop-reason Pareto; ideal cycle
time run by run, with reconfigurations marked; changeover cost in and out of it.

- Runs are the device's own part runs; run, stop and ideal hours are part_run's, verbatim. Stops are assigned to
  the run they started in, by time. Re-summed from events, down hours match part_run to the second on every run
  but one (FR8 ESTRADIOL from 21 Sep 2026: the device's total is 13 s higher).
- The event's own `part` field is not used for runs: it also carries the still-open run (AreaD's OTRIVIN open since
  25 Aug reads 233 run h by that field against 132 h of closed runs). Open runs are named and counted nowhere.
- **ICT reconfiguration**: derived ICT = ideal time ÷ run cycles, runs over 200 cycles, all usable history. A run more
  than `ict_change_tolerance` (3%) off its level that the next run confirms starts a new level; one the next run
  does not confirm is a single outlier. On the recording: AreaB OTRIVIN 19 Jun (−4%), AreaE REGAINE_5b_60ML_SINGLES
  20 Apr (−11%), FR8 ABREVAi_3G 13 Mar (−10%), AreaB OMNARIS_120P 30 Mar (−53%). The products list's derived ICT
  is a median over all history, so it mixes both levels where a line was reconfigured; that row says so.

**Product filter on the line page** (`?part=`): hour records naming the part (a changeover hour counts for both
parts) and the state events the device attributes to it, open run included. The heatmap shows other products' time
in white. Calendar use under the filter is scheduled ÷ elapsed while that part was on the line. Live app only.

## The sticky band

On every page the nav stays at the top while scrolling, with the page's bar directly under it: the granularity and
period controls, or on `/live` the status line with the oldest reading. The masthead scrolls away. On a laptop the
band is 88-89 px (measured at 1024 and 1280 px wide); below 700 px only the nav sticks, on one row that scrolls
sideways (42 px). Once the band floats over content it gets a hairline and a soft shadow.

- Two sticky siblings rather than one sticky wrapper, so the bar can stop sticking on a phone while the nav keeps
  sticking. `_sticky.html` measures both into `--navh` / `--barh`; the bar sits at `top: var(--navh)`.
- In-page jumps (a heatmap day scrolling to the hour table) land below the band: `scroll-padding-top` on the page.
- The page tables have no sticky headers of their own: the `class=scroll` wrappers carry no styling, so nothing can
  hide under the band.
- The export uses its own shell, with the same classes, the inlined stylesheet and the same `_sticky.html`.
- The open-run alerts and the history panel now sit below the bar, since the bar has to be directly under the nav.

## Live view (`/live`)

What each line is doing now: state in large type in its impact colour, the reason, part and job, the shift and how far
into it, run / down / planned hours and availability / performance for the shift so far, and how long the line has
been in its present state. No piece counts. Live only: not in the static export.

    devices -> live writer -> current_state table -> /live

- **The writer** (`xlpipe/live.py`) reads `api/v0/real_time/data` and the open process-state event from each device,
  one thread per device, every `live.interval_seconds` (5), with a `timeout_seconds` (3) limit, so a slow or dead
  device only delays its own row. A good read replaces the row and stamps `observed_at` with when it was taken; a
  failed read changes only `attempted_at` and `last_error`, so the last good values keep their real age. It runs
  inside `python -m webapp` or alone as `python -m xlpipe live`; a lock beside the cache allows one at a time.
- **The page reads only `current_state`** and never calls a device: any number of viewers cost one write loop, and the
  table's twin in `schema.sql` means moving it to Postgres changes nothing in the view. No row at all: the page says
  the writer has never run, and keeps checking.
- **It updates itself in place.** Every `poll_seconds` (5) the browser reads `/api/live` - the rows as JSON, each with
  its panel rendered by the page's own template - and swaps the panels; no page reload, so no flicker and no lost
  scroll position. Each poll waits for the last (4 s limit). A failed poll changes nothing on screen: the values
  stay and their ages keep climbing into stale, and the marker beside Refresh turns red with how long since the last
  update, so a break between browser and server shows like any other. Polling pauses while the tab is hidden
  (background tabs throttle timers, which would show a false age) and reads at once when it is shown. Refresh reads
  immediately.
- **Ages are of the reading, not the page.** The server renders each reading's age; the browser counts it up every
  second on its own monotonic clock, so a wrong clock on the screen cannot make an old reading look fresh. A panel is
  stale once its reading is older than `stale_after_seconds` (20: four writer cycles, so 5 s writer / 5 s poll jitter
  never trips it) or its last read failed: greyed, with the failure and the age of the values shown. With updates
  running it appears only when something is wrong: the writer stopped, a device unreachable, or the server out of
  reach. A line never read shows no numbers at all. The line at the top gives the oldest reading, which lines were
  never read, and how many are stale.
- Availability and performance use the site's conventions (run ÷ (run + unplanned + planned); ideal ÷ run) from the
  device's own shift times, not the device's own percentages. Time in the present state is the device's own, which
  leaves out breaks and unscheduled time inside it. Where the wall clock since the state began is more than ten
  minutes past that, both are shown: "3 h 06 min counted · since Mon 08:40 (2 days ago)".
- Shift names are the devices' display names. The underlying codes disagree between lines (AreaB's 07:00 shift is
  `second_shift`, FR8's is `first_shift`), so never compare shifts across lines by code.

## Static HTML export

`python -m webapp export` writes `exports/danbro_<date>_<time>.html`: one file with every page (overview, the four
lines, products, and a page per product) for every day, week, month and year-to-date, and working granularity and period controls. It needs
no server and fetches nothing: open it in Edge, Chrome or Firefox, or attach it to an email.

- Every page is rendered by the live app's own templates, so the snapshot cannot disagree with the app.
- **Size: ≈2.6 MB**, so it emails comfortably (attachments grow by about a third in transit). The pages are split
  into chunks (table rows, chart elements, lines) and each distinct chunk is stored once, then gzipped. gzip alone
  only looks back 32 KB, and a line page is bigger than that, so the same rows and chart pieces were compressed again
  in every day, week, month and year view: 6.1 MB. The browser rebuilds a page only when it is shown. The export
  refuses to write a file whose pages do not rebuild exactly, and a test opens it in headless Edge (where installed)
  to check the browser draws what Python unpacks.
- **One export at a time.** Once the new file is written, every earlier export in the same folder is deleted, so an
  old snapshot cannot be the one attached to an email. Only files named like an export (`danbro_<date>_<time>.html`)
  are touched. `--keep` keeps them.
- It is **read-only**: SAP standards and pieces per cycle are shown as text, as they were at export time, and the CSV
  tools are absent.
- It always uses the fleet window; per-line widening and the line page's product filter are live-app features.
- It is stamped with the export time, the cache refresh time and the production days covered. The open-run alerts
  are as of the export time, so a run "open 42 days" in a snapshot was 42 days at that moment.
- Every data-quality panel, flag and methodology footer is kept.
- The typeface falls back to the system UI font, because embedding Libre Franklin would mean fetching it.

## Tests

`python -m pytest` — 344 tests, four to seven minutes (most of it the export tests), no device. Fixtures in `tests/fixtures/devices/` (16 MB) are a
recorded first-run backfill of all four devices; the end-to-end tests replay them through the real sync into a fresh
cache.

**Expected values are literals.** Wherever a test checks numbers from the recording, the expected value was derived
once from the raw fixture JSON by a script sharing no code with `xlpipe`, and written into the test. Where a test does
read the fixtures, it uses its own parser (`conftest.recorded_events`), never the code under test — otherwise a bug
in that code would be compared with itself and pass.

| File | What it pins |
|---|---|
| `test_reader.py` | Envelopes, positional zipping and its mismatch error, the `api/v0/device` double wrap, changeover arrays, NaN/∞ → NULL, `"null"` → NULL, open and malformed records. |
| `test_sync.py` | `sync_id` resumption on an in-memory fake device: backfill, incremental, paging, open events, edits, replays that must not undo edits, delete flagging and its safety valve, the `record_order` fallback, quarantine and retry. The regression test for the NULL high-water-mark bug. |
| `test_sync_errors.py` | A row the store rejects (quarantined, rest load); schema drift (holds, quarantines nothing); SQLSTATE classification; retry backoff to the 60 s ceiling; NaN in quarantined payloads; an open event mid-sequence in `record_order` mode; an unreachable device. |
| `test_reconciliation.py` | Our generated `is_full_hour` / `acct_gap_s` on hand-written records with hand-computed answers; `verify.py`'s accounting check passing, flagging and failing; the cache against an independent calculation row for row. |
| `test_counts_preserved.py` | Literal checksums of every count per line and channel, literal negative-reject rows, the six anomalies and that there are only six. |
| `test_epochs.py` | Unusable-era rows kept and flagged, and actually absent from every aggregate; the tainted production day dropped whole; widening still excludes the era. |
| `test_aggregation.py` | The September 2026 overview at full precision, and as displayed, against the figures verified by hand. |
| `test_products.py` | Derived ICT is the median (per line and fleet), the September ESTRADIOL row cell by cell, ratio direction, SAP unit conversion both ways, SAP comparison end to end, per-piece only for trusted epochs, edit API validation, CSV round trip and error reporting. |
| `test_periods.py` | Weeks start Monday, keys and labels for each granularity, what a week contains. |
| `test_display.py` | Graduated performance wording, the 0-clamp on durations, changeover rows, line-page figures for a known month. |
| `test_open_runs.py` | The 7-day and 3× median rules, the 200-cycle filter, and the real AreaD and AreaB cases. |
| `test_export.py` | The static export: no external assets, every page and period, the September figures, read-only, every quality panel, stamped; all 40 product pages present and their links followed. |
| `test_charts.py` | Heatmap painting (latest-started event owns the time), 24 h coverage, the AreaB idle stretch, quadrant zones and config thresholds, labels that never overlap. |
| `test_charts_more.py` | The implausible rule (42 events, the 24 h cap, config), charts leaving them out and tables keeping them, Pareto on one axis, rhythm weeks, changeovers charged once. |
| `test_rhythm_pareto.py` | The rhythm change point (AreaB 11 May, −60%, t 6.5; not the other lines), the Pareto verdict, the availability-convention note. |
| `test_sticky.py` | The bar directly under the nav on every page (the live status line on /live), the rules, and in headless Edge at 1280 and 390 px: band under 90 px, nav at 0 with the bar exactly under it, only the nav on a phone, the shadow only once floating, the hour-table jump landing below the band. The export's band is checked in `test_export.py`. |
| `test_live.py` | Each device's recorded reading, no counts, a failed read keeping the last values, a slow device not delaying the rest, one writer, the page reading only the table, never-run, stale by age and by failure, never-read panels, the JSON endpoint, counted vs wall-clock time; and in headless Edge: a reading turning stale as it ages, a failed poll keeping values with ages climbing, an in-place update with no reload, polling paused while hidden and resumed at once. |
| `test_product_pages.py` | Runs and their stops against part_run, ICT levels and outliers, the reconfigurations on the recording, every product-page section with literal figures, part links, the line page's product filter. |

### Mutation check

`python tests/mutation_check.py` applies, one at a time, 91 deliberate bugs of the kind a refactor or a "helpful" fix
could introduce — clamping a count, inverting a ratio, taking a mean for a median, starting weeks on Sunday — runs
the suite against each, and restores the original. A bug the suite does not notice is a gap. It takes several minutes;
`--list` shows the bugs, and numbers run a subset.

Passing tests only show the code agrees with the tests. This shows the tests would notice if the code were wrong.
When you add a feature, add the mutation that would break it.

**If you reuse or modify the runner: restore bytes and disable bytecode caching.** Python decides whether a cached
`.pyc` is still valid by comparing only the source file's size and modification time (whole seconds). An early
version restored files by moving a backup back into place. One mutation (`range(6)` → `range(1)`) was the same length
as the original and was written in the same second, so after the source was restored Python kept running the
**mutated bytecode** — the sync silently retried once instead of six times, and nothing errored. It surfaced only
when a new test failed against code that looked correct. The runner now writes the original bytes back, bumps the
modification time, deletes `__pycache__`, runs the suite with `PYTHONDONTWRITEBYTECODE=1`, and checks each restore.
If results ever look inconsistent, delete every `__pycache__` directory before trusting them.

**Every mutation is pinned.** A mutation whose target text no longer applies tests nothing, and that has happened
four times: Windows line endings, a stale `.pyc`, #42's line moving in a refactor, and #41 and #42 again when the line
page gained its product filter. Each entry is pinned (`PINS`, at the bottom of the runner) to a hash of the whole
source line(s) holding its original text, line endings normalised. Before anything is mutated the runner checks every
entry: the text occurs exactly once, the line hashes to its pin, the mutated file still compiles or parses (a mutant
that cannot load fails every test for the wrong reason). Any failure refuses the whole run, exit 2. The same check is
an ordinary test (`test_mutation_pins.py`), so a stale mutation fails `pytest` itself. `--check` runs only the check;
`--pins` prints each mismatched entry with its current line and new pin. When you change a pinned line on purpose,
re-read the mutation against the new code before taking the new pin: updating it blindly defeats the point.

**One run at a time, and nothing edited while it runs.** A run holds `.mutation_run.lock` (created exclusively; a
second run refuses to start, exit 2) and keeps the original bytes of the file it has mutated in
`.mutation_run.original`. It hashes every source file before the first mutation and checks them before and after each
one. An edit saved mid-run would otherwise be wiped by the restore, or keep the mutation, or change what later
mutations test; so on any change the run stops with no verdict (exit 3), restores the original, and keeps the edited
file as `<file>.edited-during-mutation`. A lock left behind means a run was killed: `--check` shows whether a file was
left mutated, `.mutation_run.original` holds its original bytes, then delete the lock. The runner's own guards are
among the 70 mutations.

**Re-recording** (`refresh --record tests/fixtures/devices` against an *empty* cache) changes the data under the
literal expectations. Re-derive them from the raw JSON, independently, before updating them.

## Runbook

**The red open-run alert is showing.** The named run has been open too long on the device. Ask the line whether that
job really is still running. If it has finished, it needs closing out on the XL; until it is, everything that line
makes accumulates into that one run and none of it reaches the product tables.

**A line shows stale data.** `python -m xlpipe report` shows the last refresh and any channel error. Check the
device answers at `http://<ip>` in a browser. One unreachable device does not stop the others.

**`verify.py` reports a FAIL.** Run a refresh and re-run it. If cache vs device totals still disagree, the sync is
dropping or double-counting; check `sync_quarantine` and `sync_state.last_error`.

**Something is quarantined.** `SELECT * FROM sync_quarantine WHERE resolved_at IS NULL` in the cache. The 7-day
window retries it each run; if it persists, the `error` column says why.

**Rebuilding the cache** (after a schema change, or if it is damaged): **Export CSV on the products page first.** Then
move `cache/` aside, run `python -m xlpipe refresh` (≈30 s for all four devices), and Load CSV to restore SAP values
and pieces per cycle.

**A device's configuration is corrected** (e.g. Vorne fixes pieces per cycle): add the next epoch for that device in
`config.yaml` with its start time, and `counts_trusted: true` if counts become reliable. The next refresh re-stamps
every row by its own start time; earlier rows keep their old epoch and stay untrusted.

**A device is added or replaced.** Add it to `config.yaml` (key on our own `device_id`; the device's `asset_uuid`
changes on physical replacement). Re-run the discovery sweep of `10.15.22.1-254` on `api/v0/device` to find new
units.

**When the database path opens up.** Run `preflight.py` from the plant machine: it names the failing layer, and
on a firewall rejection prints the public IP Azure sees, which is the one to allow in the portal (not a LAN address).
Apply `schema.sql`, create the login users from its maintenance notes, then add the Postgres (or HTTPS) store behind
`xlpipe/store.py`. Any HTTPS client must call `truststore.inject_into_ssl()`: this network runs Cisco Umbrella TLS
inspection, and Python's bundled CA list fails on it.

## Layout

```
xlpipe/        the sync: client, reader, rows, channels, sync, store interface, SQLite cache, report
webapp/        Flask app: data.py (queries), templates/, static/style.css (from danbro.html)
tests/         pytest suite and recorded fixtures
schema.sql     Postgres schema, views, roles (parked; parses, never applied)
preflight.py   Azure connectivity check (parked)
verify.py      correctness checks against the devices
config.yaml    devices, channels, sync settings, configuration epochs
BRIEF.md       the project brief
```
