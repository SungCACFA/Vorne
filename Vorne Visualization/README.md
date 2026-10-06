# XL to Azure Postgres collector

Runs on a machine inside the plant network. Polls each XL device, buffers to
local disk, uploads to Azure Postgres in batches.

```
  [XL devices] --LAN--> [collector + spool.db] --HTTPS/5432--> [Azure Postgres]
```

The connection is outbound only. Nothing in Azure ever reaches into the plant.

## Setup order

**1. Preflight, from the plant network.** Do this before anything else.

```bash
pip install "psycopg[binary]"
export PGHOST=myserver.postgres.database.azure.com
export PGUSER=... PGPASSWORD=... PGDATABASE=postgres
python preflight.py
```

It tests DNS, TCP, TLS, auth, write permission and latency as separate steps,
so a failure tells you which layer broke. Two likely stumbles:

- **TCP 5432 times out.** The corporate firewall blocks outbound 5432. Ask IT
  to allow it to that one hostname. If they won't, the fallback is to push over
  HTTPS to a small API in Azure instead of connecting to Postgres directly —
  more moving parts, but 443 is never blocked.
- **"no pg_hba entry" on connect.** Azure's firewall doesn't know this site.
  Add the plant's public egress IP in the portal under Networking. Note this is
  the *public* IP the plant NATs out through, not the collector's LAN address.

**2. Create the schema.**

```bash
psql "host=$PGHOST user=$PGUSER dbname=xl sslmode=require" -f schema.sql
```

Then create the two roles at the bottom of `schema.sql` — a write-only one for
the collector and a read-only one for the Flask app. Don't let the web app
share the collector's credentials.

**3. Configure and run.**

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml    # device IPs
cp .env.example .env                  # DB credentials
python run_collector.py
```

Watch the log for `state -> Running` lines. Those mean it's working.

## What gets stored

**`state_event`** is the table that matters. One row per production state with
start, end, duration, reason, and the part and job it happened under. Downtime
Pareto, MTBF and reason analysis all come from here. Written the moment a state
changes, keyed on the device's own event id so a replay upserts instead of
duplicating.

**`metric_sample`** holds periodic rollups — by default one row per device per
channel per minute. Roughly 43,000 rows/day for ten devices, which Postgres
won't notice. Partitioned monthly so retention is `DROP TABLE` rather than a
`DELETE` that bloats the table.

The fifteen metrics you'll actually aggregate on get typed columns. Everything
else goes in `extra` as JSONB, so a firmware update that renames or adds a
metric never loses data and never needs a migration.

**`collector_health`** is a heartbeat. Without it you can't distinguish "the
line was idle" from "the collector died", and those look identical in a chart.

## Why one row per minute and not per second

The XL accumulates its own totals per hour, shift and part. Sampling every
second would write about 26 million rows per device per day and tell you
nothing a one-minute sample doesn't. State transitions *are* captured at full
fidelity, because that's where the information actually is.

## Reading the data

Counters are cumulative within their channel and reset when the channel event
rolls over. Take differences only within a constant `*_event_id`:

```sql
-- Output per hour for one device today
SELECT date_trunc('hour', ts) AS hour,
       max(good_count) - min(good_count) AS produced
FROM metric_sample
WHERE device_id = 'press-1'
  AND channel = 'shift_hour'
  AND ts >= current_date
GROUP BY 1 ORDER BY 1;
```

```sql
-- Worst downtime reasons this week
SELECT reason, stops, round(total_seconds/60) AS minutes
FROM downtime_by_reason
WHERE device_id = 'press-1' AND day >= date_trunc('week', now())
ORDER BY total_seconds DESC LIMIT 10;
```

```sql
-- Is the collector alive?
SELECT * FROM collector_status;
```

The `current_metrics` view gives the latest sample per device and channel —
cheap enough to drive a live tile without touching the devices.

## Failure behaviour

- **WAN drops.** Readings queue in `spool.db` and upload when it returns.
  Outage costs latency, not data.
- **Spool fills up.** Oldest metric samples are shed first; state events are
  never dropped. A missing rollup is a gap in a chart, a missing downtime
  event is a hole in the analysis you built this for.
- **One malformed row.** The batch is retried row by row, the bad one is
  logged and discarded, the rest go through. One bad record can't wedge the
  queue behind it.
- **Device unreachable.** That device backs off to a 60s retry and the others
  carry on.
- **Collector restarts.** The spool is on disk, so whatever was queued uploads
  on next start. Inserts are idempotent, so replays are harmless.

## Running it as a service

```ini
# /etc/systemd/system/xl-collector.service
[Unit]
Description=XL to Postgres collector
After=network-online.target

[Service]
WorkingDirectory=/opt/xl-collector
ExecStart=/opt/xl-collector/.venv/bin/python run_collector.py
Restart=always
RestartSec=10
User=xlcollector

[Install]
WantedBy=multi-user.target
```

On Windows, use Task Scheduler with "run whether user is logged on or not"
and a restart-on-failure action.

Two things to set up at the same time:

- **A heartbeat alert.** Query `collector_status` on a schedule and alert when
  health isn't `ok`. A silent collector is worse than no collector, because the
  charts keep looking plausible.
- **Backups.** Azure Postgres takes automatic backups, but confirm the
  retention window matches what you'd actually want to restore.

## Untested paths

This hasn't run against real hardware or a real Azure server. The SQL is
conventional and the Python compiles, but expect to spend a session on the
first live run — most likely on metric names that differ from the docs. Send me
the collector log and I'll correct the mapping.
