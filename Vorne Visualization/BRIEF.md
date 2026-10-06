# Danbro packaging — Vorne XL data platform: project brief

Save as `BRIEF.md` in the project root and point Claude Code at it.

Everything in sections 2–4 was validated against the live devices in an
exploratory notebook. Trust it over vendor documentation where they disagree.

---

## 1. What we are building

Replace a working-but-manual notebook pipeline with a scheduled service and a
Flask app, so reporting happens without anyone running cells.

```
  PLANT FLOOR (10.15.22.0/24)          │  AZURE
                                       │
  [FR8]    10.15.22.19  ──┐            │
  [AreaB]  10.15.22.83  ──┼─→ sync ────┼──→ Postgres ──→ Flask app
  [AreaD]  10.15.22.95  ──┤   service  │                 + emailed reports
  [AreaE]  10.15.22.119 ──┘            │
                                       │
          outbound 5432 only ──────────┘
```

Azure never reaches into the plant. All connections outbound from the sync
service. No inbound rule, no VPN.

Timezone for all reporting: **America/Toronto**. Store UTC, convert on display.

### What already exists and works

A notebook that pulls full history from all four devices, reconciles time
accounting, aggregates downtime reasons, derives ideal cycle times, and renders
a self-contained interactive HTML report (`danbro.html`) with overview,
per-line, and products pages. **Read that report's structure before designing
the Flask app — it is the specification for what the app must do.** The job is
to make it scheduled and queryable, not to redesign it.

---

## 2. Architecture decision: incremental event sync, not live sampling

**Each XL device is already a historian.** It stores roughly a year of
historical production data in its own embedded database. The channel endpoints
expose it directly.

So the sync service does **not** poll a live feed into storage. It pulls
**closed events** incrementally:

- **First run** backfills the entire history on each device — roughly a year.
- **Every run after** pulls only records newer than the last one stored.
- Figures are the device's own closed totals, so they tie out exactly against
  XL's built-in reports. No reconstruction from samples, no end-of-hour
  truncation error.

Volume, measured: ~96 `shift_hour` rows/day across four devices, plus a few
hundred `process_state` events and a handful of part and job runs. Call it
**~1,000 rows/day**, under 100 MB/year total. A sync every five minutes is
ample; nothing here changes faster than hourly.

**Live current-state is a separate concern and needs no storage.** The Flask
app queries `api/v0/real_time/data/...` directly when someone opens a live
view. If the plant link is down, that one panel degrades and nothing else does.

### Resumability

Channel records carry `record_order` (monotonic) and `sync_id` (a signed
integer incremented on every create, update or delete in the channel —
effectively a transaction log). Track the high-water mark per device per
channel and filter forward:

```
GET api/v0/channels/shift_hour/events
      ?fields=...&sort=record_order&record_order[gt]=<last_seen>
```

Note: oldest archive records are pruned once the device's retention horizon is
exceeded, so `sync_id` gaps at the old end are expected and are not data loss.

---

## 3. Verified environment

| | |
|---|---|
| Devices | FR8 `10.15.22.19`, AreaB `10.15.22.83`, AreaD `10.15.22.95`, AreaE `10.15.22.119` |
| Firmware | 2.22.14.2 on all four |
| Data model | 1.6 |
| Model | xlhd (XL HD) |
| Auth | none — plain HTTP GET on the LAN |
| Metric channels | `shift_hour`, `shift`, `part` |
| Metrics | 58 stored, 94 derived |

Discovery note: a sweep of `10.15.22.1-254` on `api/v0/device` found exactly
these four. Re-run that if machines are added.

### API shape — validated

```
GET api/v0/channels                        list channels
GET api/v0/channels/{channel}              field schema for a channel
GET api/v0/channels/{channel}/events       historical records  ← primary
GET api/v0/dimensions/{name}               key → display_name lookups
GET api/v0/real_time/data/{section}        current state only, not stored
GET rest/cpe/attributes                    identity, no envelope
GET api/v0/device                          identity, values wrapped
```

Quirks that cost time to find:

- Responses are wrapped `{"data": ..., "meta": ...}`. `rest/cpe/attributes` is
  not.
- `api/v0/device` wraps **every value** as `{"metadata": {...}, "value": ...}`.
  Reading `.get("asset_name")` returns a dict, not the name. Unwrap twice.
- `/events` requires `fields` and returns **positional arrays** in exactly the
  order requested. Zip back to names; never assume a default order.
- Filters use bracket operators: `production_day_number[gt]=20700`,
  `event_id[neq]=9`. Operators: `lt gt lte gte eq neq`.
- `sort=-record_order` for descending.
- Channels available: `quality_event, production_metric, shift, process_state,
  production_phase, instant, calendar_day, diagnostic, team, production_day,
  part, month, job, hour, shift_hour, week, quarter, year, system, quality`.
  `instant` is deprecated — do not collect it.
- Table endpoints exist but Vorne recommends against them for integration.
  Use channels.

### Field sets in use — all validated

`shift_hour` and `shift` (the reporting backbone):

```
start_time, end_time, shift, shift_hour, part, job,
in_count, good_count, reject_count,
run_time, unplanned_stop_time, planned_stop_time, not_scheduled_time,
ideal_time, target_count
```

`process_state` (downtime reasons — the Pareto source):

```
start_time, end_time, duration, process_state, process_state_reason,
process_state_reason_display_name, performance_impact, production_phase,
part, shift, production_day_number
```

`part` and `job` (run-level performance):

```
start_time, end_time, part, job, duration,
run_time, unplanned_stop_time, planned_stop_time,
in_count, good_count, ideal_time, ideal_cycle_time,
target_count, target_cycles, run_cycles, small_stops
```

`quality_event` (currently empty — see section 4):

```
production_day_number, reject_reason, reject_reason_display_name,
count, record_order
```

### Validated derivations

Time accounting reconciles: for a full-hour record,
`(end_time − start_time) − (run_time + unplanned_stop_time +
planned_stop_time + not_scheduled_time) ≈ 0`. That reconciliation is what
makes the time-based rates trustworthy, and it should be asserted in tests.

```
availability = run_time / (run_time + unplanned_stop_time + planned_stop_time)
performance  = ideal_time / run_time
utilization  = (run + unplanned + planned) / (run + unplanned + planned + not_scheduled)
ideal_cycle_time = ideal_time / run_cycles        # matches device config to <0.5%
```

Filter to `end_time − start_time == 3600` before computing hourly rates;
partial records at the ends of the history skew them.

---

## 4. Count data is unreliable — report time, withhold counts

This is established, not suspected. Do not try to fix it in code.

**Findings:**

- `reject_count` equals `in_count − good_count` exactly, for every row.
- The `quality_event` channel is **empty across the whole history on all four
  devices**. No reject reasons are ever scanned, so rejects are inferred from a
  sensor disagreement rather than measured.
- Where the good-count sensor registers more pieces than infeed saw, rejects go
  negative.
- Negative rejects persist at hour, shift **and** day grain with unchanged net,
  so this is a measurement fault, not a timing artefact.
- `expected_count_per_cycle` and `utilized_count_per_cycle` are `0.0`, and
  `pack_out_count` is `0.0` — the per-cycle reconciliation XL needs was never
  configured.
- There is a distinct signature worth isolating in the loader: rows where
  `reject_count == -good_count` **and** `run_time == 0`. These look like
  post-hoc count adjustments rather than production.

**Requirements:**

- **Store counts verbatim.** Never clamp, absolute-value, or recompute. A
  negative reject count is real information about a real problem.
- **Do not publish `quality`, `oee`, or `teep`** anywhere in the app.
  Availability, performance and utilization are sound because they derive from
  time, which reconciles.
- Carry a `counts_trusted boolean` on the device table, default false. Flip it
  per device once configuration is corrected, and have the app show count-based
  metrics only where it is true.
- Add `config_epoch int` default 1 on fact tables, bumped manually when a
  device's configuration changes, so pre- and post-fix rows stay
  distinguishable without guessing at timestamps.
- Surface a persistent, visible data-quality note anywhere counts appear — the
  existing report's wording is a good model.
- Expose a `data_quality` view: rows with negative rejects, `good > in`, or
  `performance > 1`.

---

## 5. Schema

Postgres 14+ on Azure Flexible Server. `sslmode=require` is mandatory.

### Dimension and control tables

- `device` — `device_id` PK (FR8, AreaB, AreaD, AreaE), `host`, `asset_uuid`,
  `mac`, `model`, `firmware`, `data_version`, `counts_trusted`, `first_seen`,
  `last_seen`
- `sync_state` — `(device_id, channel)` PK, `last_record_order`, `last_sync_id`,
  `last_synced_at`, `rows_loaded`, `last_error`. This is what makes the sync
  resumable; without it every run re-pulls a year.
- `part_standard` — `part` PK, `ideal_cycle_time` (derived, median),
  `ict_stddev`, `sample_runs`, `sap_standard`, `sap_unit`, `updated_at`. The
  notebook's SAP comparison currently lives in browser state and a CSV; it
  belongs here.

### Fact tables

- `hour_record` — from `shift_hour`. `(device_id, start_time)` PK. Partition
  monthly by `start_time`. All fields from the validated set, plus
  `is_full_hour boolean` and `acct_gap_s double precision` (the reconciliation
  residual, so drift is monitorable rather than invisible).
- `shift_record` — from `shift`, same shape.
- `state_event` — from `process_state`. `(device_id, start_time)` PK or a
  surrogate with a unique natural key. Include `duration_s`,
  `performance_impact`, both raw reason key and display name,
  `production_phase`.
- `part_run` / `job_run` — from `part` and `job` channels, including
  `run_cycles`, `ideal_cycle_time`, `target_cycles`, `small_stops`.
- `quality_event` — model it even though it is empty today, so it starts
  populating the moment reject scanning is configured.

All loads idempotent: `ON CONFLICT DO NOTHING` on the natural key, so a
re-pulled overlap is harmless.

### Views

`daily_time_accounting`, `downtime_by_reason`, `activity_hours`,
`part_performance`, `data_quality`, `sync_status`, `collector_status`.

### Roles

Write-only for the sync service, read-only for Flask. Separate credentials.

---

## 6. Sync service

Python. `requests`, `psycopg[binary]` v3, `PyYAML`.

- Four devices, pulled concurrently; a thread pool is plenty.
- Backfill on first run, incremental after, driven by `sync_state`.
- Page through large backfills using `limit` and `offset`; do not try to pull a
  year in one request.
- Batch inserts. Round-trip latency to Azure makes row-by-row pointless.
- Guard against NaN and infinity before insert — Postgres `double precision`
  rejects both and one bad value aborts an entire batch.
- On a batch rejection from a malformed row, retry row by row, quarantine the
  bad one with its error logged, let the rest through.
- Distinguish transport errors (retry with backoff to a 60 s ceiling) from
  permanent data errors (quarantine).
- A device being unreachable must not block the other three.
- Heartbeat table so a silent failure is detectable. Without it you cannot tell
  "the line was idle" from "the sync died", and those look identical in a chart.
- Local spool is **not** needed here, unlike a live sampler: the device retains
  the history, so a failed run simply retries later and catches up. Do not add
  one.
- TCP keepalives in the connection string — Azure drops idle connections.
- Credentials in `.env`, never committed. Device list in `config.yaml`.
- Run as a scheduled task or systemd timer every five minutes, single instance.
  Guard against overlapping runs with a lock.

---

## 7. Flask app

The existing `danbro.html` defines the requirement. Port its structure:

**Pages:** Overview (all four lines), one per line, Products.

**Controls:** granularity day / week / month / year-to-date, with a period
selector. These already work in the report and users will expect them.

**Overview:** time-accounting table with a stacked proportion bar per line,
availability / performance / utilization, scheduled and down hours, fleet
total row. Plus per-line daily availability sparklines.

**Line page:** headline rates, hours by activity with `performance_impact`,
downtime reasons ranked by hours lost with event counts, mean minutes and a
cumulative column, planned stops as a separate table, daily sparkline, and an
hour-by-hour table showing what ran in each scheduled hour.

**Products page:** derived ideal cycle time per part with a stability flag,
an editable SAP standard column with unit selection (seconds per piece, pieces
per hour, hours per thousand), actual versus both standards, earned versus
actual hours, variance. Persist SAP values to `part_standard` rather than
browser state. Keep CSV import and export.

**Design language, already established — inherit it, do not redesign:**

```
Typeface   Libre Franklin (400 / 600 / 800), tabular numerals
Ink        #15191B      Muted  #5A6368
Rule       #DADFE1      Panel  #F4F6F6
Running    #1F6F5C      Unplanned stop  #B3261E
Planned    #C98A0B      Not scheduled   #B9C0C4
```

Structure: left-aligned, max ~1080px, section headings with a 2px rule under
them, explanatory note under each heading in muted 14px, tables with
right-aligned numerics and left-aligned row headers, stacked proportion bars
inline in table cells, data-quality warnings as a panel with a red left border.
Corporate assets will be supplied — reconcile with the above, do not start over.

**Also needed:** read-only DB role with pooling, Entra ID auth if other
internal apps use it, a scheduled shift-summary email (this is the whole point
of the project — eliminating manual reporting), and a live current-state panel
querying devices directly.

---

## 8. Things that will bite you

- Counters are cumulative **within** a channel event and reset on rollover.
  Differencing across a rollover produces large negatives. Closed-event records
  sidestep this entirely, which is the main reason for the architecture in
  section 2.
- `performance > 1` means a wrong ideal cycle time for that part, not a fast
  machine. Flag it.
- Filter to full-hour records before computing rates.
- `asset_uuid` changes when a device is physically replaced unless the Replace
  Device endpoint is used. Key on your own `device_id`.
- Metric and field availability varies by firmware and part configuration.
  Resolve names at runtime against the channel schema; never hardcode without a
  fallback.
- Single sync instance. Concurrent runs will both backfill.
- `edges_since_power_on` on pins counts transitions, wraps at 2³²−1, resets on
  reboot. Halve and round up for activations; treat as a delta source only.

---

## 9. Deliverables in order

1. `schema.sql` — tables, partitions, views, roles
2. `preflight.py` — DNS, TCP 5432, TLS, auth, write permission, latency as
   **separate** checks so a failure names the layer. Outbound 5432 is often
   blocked by corporate firewalls; the fallback is pushing over HTTPS to an API
   in Azure. Confirm this before building anything else.
3. Sync service — client, channel loaders, `sync_state` resumption, backfill
4. `verify.py` — compare database aggregates against a direct device pull, and
   assert the time-accounting reconciliation holds. This is how we know the
   sync is correct rather than merely running.
5. Tests, using recorded device payloads as fixtures. No live device required.
   Cover: envelope unwrapping (including the double-wrapped `api/v0/device`),
   positional array zipping, time-accounting reconciliation, NaN guarding,
   resumption from `sync_state`, negative-reject preservation.
6. `README.md` — setup order, the Azure firewall requirement (allow the plant's
   **public egress IP** in the portal, not a LAN address), query examples,
   failure behaviour, runbook
7. Flask app — after data is landing and `verify.py` passes

Write the runbook as you go. Otherwise this becomes a machine one person
understands, and that person takes vacation.
