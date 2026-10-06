-- Danbro packaging - Vorne XL data platform
-- Target: Azure Database for PostgreSQL Flexible Server, PG 14+
--
--   psql "host=<server>.postgres.database.azure.com dbname=<db> user=<admin> sslmode=require" \
--        -v ON_ERROR_STOP=1 -f schema.sql
--
-- Safe to re-run: every object is CREATE ... IF NOT EXISTS or CREATE OR
-- REPLACE. Re-running does NOT alter an existing table's columns; schema
-- changes after first install are migrations (see MAINTENANCE at the end).
--
-- Design notes
-- ------------
-- * Source of truth is the device. Each XL stores about a year of closed
--   channel events; the sync service copies them incrementally. Nothing here
--   is reconstructed from samples.
--
-- * Natural keys are the device's own event / record ids, never timestamps.
--   Split Down Event rewrites event boundaries, so a start_time key would
--   turn an edit into a duplicate.
--
-- * Every load is an UPSERT. Reasons are assigned after the fact, so a
--   DO NOTHING load would freeze "Missing Reason" into the Pareto forever.
--   The sync service's upsert is expected to take this form:
--
--     INSERT INTO <fact> (...) VALUES (...)
--     ON CONFLICT (device_id, <event_id>) DO UPDATE SET
--         <every device field> = EXCLUDED.<field>, ...,
--         deleted_at = NULL,
--         updated_at = now()
--     WHERE <fact>.sync_id IS NULL OR EXCLUDED.sync_id >= <fact>.sync_id;
--
--   The WHERE stops an older replay overwriting a newer edit.
--
-- * Deletes on the device are FLAGGED here (deleted_at), not removed. The
--   sync compares the device's record set over a trailing window with ours
--   and stamps anything that has gone. Every view filters them out.
--
-- * Counts are stored verbatim (BRIEF section 4). There are deliberately NO
--   validating CHECK constraints on fact tables: a CHECK failure is treated
--   by the sync as schema drift and halts every load. Bad data belongs in
--   the data_quality view, where it is visible instead of fatal.
--
-- * Whether counts may be shown is decided per configuration epoch, not per
--   device, so history from before a configuration fix stays untrusted after
--   the fix. See device_config_epoch.
--
-- * Not partitioned. ~35k hour_record rows/year across four devices; plain
--   indexes are enough and there is no partition maintenance to forget.
--
-- * All timestamps are timestamptz (stored UTC). Reporting timezone is
--   America/Toronto. The reporting day key is production_day_number - label
--   it "production day" in any UI, never "date": it starts at 07:00, not
--   midnight.

BEGIN;

-- ================================================================ helpers

CREATE OR REPLACE FUNCTION plant_tz() RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT 'America/Toronto' $$;

COMMENT ON FUNCTION plant_tz() IS
    'Reporting timezone. The one place to change it.';

-- production_day_number is days since 1970-01-01 in the device's production
-- calendar. This turns it into a date for display - but call it a
-- "production day", not a date.
CREATE OR REPLACE FUNCTION production_day(n int) RETURNS date
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT date '1970-01-01' + n $$;

-- ===================================================== dimension / control

CREATE TABLE IF NOT EXISTS device (
    device_id    text PRIMARY KEY,          -- our stable name: FR8, AreaB, AreaD, AreaE
    host         text NOT NULL,             -- LAN address, e.g. 10.15.22.19
    asset_name   text,                      -- api/v0/device asset_name.value
    asset_uuid   uuid,                      -- meta.asset_id
    mac          text,                      -- meta.device_id
    model        text,
    firmware     text,
    data_version text,
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz
);

COMMENT ON COLUMN device.asset_uuid IS
    'Changes when a unit is physically replaced unless the Replace Device '
    'endpoint is used. Informational only - everything keys on device_id.';

-- One row per configuration era per device. Epoch 1 is created automatically
-- for every device, effective from the beginning of time, counts untrusted.
-- When Vorne corrects a device's part configuration, INSERT the next epoch;
-- every fact row is re-stamped from its own start_time automatically.
CREATE TABLE IF NOT EXISTS device_config_epoch (
    device_id      text        NOT NULL REFERENCES device (device_id),
    config_epoch   int         NOT NULL CHECK (config_epoch >= 1),
    effective_from timestamptz NOT NULL,
    counts_trusted boolean     NOT NULL DEFAULT false,
    note           text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, config_epoch),
    UNIQUE (device_id, effective_from)
);

COMMENT ON TABLE device_config_epoch IS
    'Configuration eras. Rows are assigned an epoch by their own start_time, '
    'not by load time, so a backfill lands in the right era. counts_trusted '
    'gates every count-based figure in the app.';

-- Resumption state. One row per device per channel.
CREATE TABLE IF NOT EXISTS sync_state (
    device_id             text        NOT NULL REFERENCES device (device_id),
    channel               text        NOT NULL,
    -- Which high-water mark drives incremental pulls. 'sync_id' is the
    -- intended mode (it advances on create, update and delete);
    -- 'record_order' is the fallback if sync_id[gt] filtering proves not to
    -- work on a channel. Either way, the trailing re-sync window runs.
    resume_key            text        NOT NULL DEFAULT 'sync_id'
                                      CHECK (resume_key IN ('sync_id', 'record_order')),
    last_sync_id          bigint,
    last_record_order     bigint,
    backfill_completed_at timestamptz,         -- NULL until the first full pull finishes
    last_synced_at        timestamptz,         -- last attempt
    last_success_at       timestamptz,
    last_window_from      timestamptz,         -- start of the last reconciled re-sync window
    rows_loaded           bigint      NOT NULL DEFAULT 0,
    rows_flagged_deleted  bigint      NOT NULL DEFAULT 0,
    last_error            text,
    last_error_at         timestamptz,
    PRIMARY KEY (device_id, channel)
);

COMMENT ON COLUMN sync_state.last_sync_id IS
    'Never advanced past a still-open event: an open record is skipped and '
    'its sync_id is not counted, so it is picked up once it closes.';

-- Heartbeat. Without it "the line was idle" and "the sync died" look
-- identical in a chart.
CREATE TABLE IF NOT EXISTS sync_heartbeat (
    instance_id          text PRIMARY KEY,     -- e.g. hostname
    hostname             text,
    version              text,
    last_heartbeat       timestamptz NOT NULL,
    last_run_started_at  timestamptz,
    last_run_finished_at timestamptz,
    last_run_ok          boolean,
    devices_ok           int,
    devices_total        int,
    rows_upserted        int,                  -- in the last run
    last_error           text,
    last_error_at        timestamptz
);

-- Rows the database rejected as malformed. The high-water mark advances past
-- them; the trailing re-sync window retries them every run, and a retry that
-- succeeds stamps resolved_at. Open rows raise an alert in sync_alerts.
CREATE TABLE IF NOT EXISTS sync_quarantine (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id     text        NOT NULL,
    channel       text        NOT NULL,
    natural_key   text        NOT NULL,        -- the event / record id, as text
    sync_id       bigint,
    payload       jsonb       NOT NULL,        -- the record as the device sent it (NaN -> null)
    error         text        NOT NULL,
    sqlstate      text,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    attempts      int         NOT NULL DEFAULT 1,
    resolved_at   timestamptz
);

-- At most one open entry per record; a repeat failure bumps attempts.
CREATE UNIQUE INDEX IF NOT EXISTS sync_quarantine_open
    ON sync_quarantine (device_id, channel, natural_key)
    WHERE resolved_at IS NULL;

-- Key -> display name lookups from api/v0/dimensions/{name}, per device
-- because configurations differ between lines.
CREATE TABLE IF NOT EXISTS dimension_value (
    device_id           text    NOT NULL REFERENCES device (device_id),
    dimension           text    NOT NULL,      -- part, process_state, shift, ...
    key                 text    NOT NULL,
    display_name        text,
    is_in_configuration boolean,
    synced_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, dimension, key)
);

-- Live panel source. The app runs in Azure and cannot reach the plant, so
-- the sync service writes one row per device from api/v0/real_time/data on
-- every run. Up to one sync interval stale, by design.
CREATE TABLE IF NOT EXISTS current_state (
    device_id              text PRIMARY KEY REFERENCES device (device_id),
    observed_at            timestamptz,        -- last successful read
    attempted_at           timestamptz,        -- last attempt, successful or not
    last_error             text,
    process_state          text,
    process_state_label    text,
    process_state_reason   text,
    process_state_reason_display_name text,
    performance_impact     text,
    production_phase       text,
    part                   text,
    part_label             text,
    job                    text,
    shift                  text,
    shift_label            text,
    process_state_event_id bigint,
    shift_event_id         bigint,
    -- Current shift so far (shift channel). Counts are shown only where the
    -- current epoch is counts_trusted.
    shift_run_time            double precision,
    shift_unplanned_stop_time double precision,
    shift_planned_stop_time   double precision,
    shift_not_scheduled_time  double precision,
    shift_ideal_time          double precision,
    shift_in_count            double precision,
    shift_good_count          double precision,
    shift_reject_count        double precision,
    shift_target_count        double precision,
    categories             jsonb,              -- raw, for debugging
    category_display_names jsonb
);

-- Ideal cycle time per part. The derived columns are refreshed by
-- refresh_part_standard() at the end of each sync and are writable only by
-- the sync role. The sap_* columns are writable only by the app role.
CREATE TABLE IF NOT EXISTS part_standard (
    part             text PRIMARY KEY,
    ideal_cycle_time double precision,          -- s/cycle: median ideal_time/run_cycles
    ict_stddev       double precision,
    sample_runs      int,
    ict_unstable     boolean GENERATED ALWAYS AS (ict_stddev > 0.05) STORED,
    ict_updated_at   timestamptz,
    -- Pieces per cycle, from the device's part configuration (latest run on
    -- each device). Converts per-cycle figures to per-piece. 0.0 or NULL
    -- means not configured - the same root cause as the count defect.
    expected_count_per_cycle double precision,
    epc_inconsistent boolean,                    -- devices disagree on it for this part
    sap_standard     double precision,
    sap_unit         text CHECK (sap_unit IN ('s_per_piece', 'pieces_per_hour', 'hours_per_thousand')),
    -- SAP standard normalised to seconds per piece, whatever unit was entered.
    sap_s_per_piece  double precision GENERATED ALWAYS AS (
        CASE sap_unit
            WHEN 's_per_piece'        THEN sap_standard
            WHEN 'pieces_per_hour'    THEN 3600.0 / NULLIF(sap_standard, 0)
            WHEN 'hours_per_thousand' THEN sap_standard * 3.6
        END) STORED,
    sap_updated_at   timestamptz,
    sap_updated_by   text,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN part_standard.ideal_cycle_time IS
    'Seconds per machine CYCLE, not per piece. SAP standards are per piece; '
    'comparing them needs pieces per cycle, which needs trusted counts.';

-- =========================================================== fact tables
--
-- Common columns on every fact table:
--   record_order, sync_id      device bookkeeping; sync_id drives resumption
--   production_day_number      the reporting day key
--   config_epoch               set by trigger from start_time; never by hand
--   deleted_at                 set when the record vanished from the device
--   first_loaded_at, updated_at
--
-- Channel event-id column names (shift_hour_event_id etc.) follow the
-- device's naming; the sync service confirms each against
-- api/v0/channels/{channel} before first load.

-- shift_hour channel: the reporting backbone.
CREATE TABLE IF NOT EXISTS hour_record (
    device_id             text   NOT NULL REFERENCES device (device_id),
    shift_hour_event_id   bigint NOT NULL,
    record_order          bigint,
    sync_id               bigint,
    production_day_number int,
    start_time            timestamptz,
    end_time              timestamptz,
    shift                 text,
    shift_hour            text,
    part                  text,
    job                   text,
    in_count              double precision,
    good_count            double precision,
    reject_count          double precision,
    run_time              double precision,
    unplanned_stop_time   double precision,
    planned_stop_time     double precision,
    not_scheduled_time    double precision,
    ideal_time            double precision,
    target_count          double precision,
    -- Rates are only computed over full hours; partial records at the ends
    -- of the history skew them.
    is_full_hour          boolean GENERATED ALWAYS AS
                              (end_time - start_time = interval '1 hour') STORED,
    -- Time-accounting residual: elapsed minus the four time buckets. ~0 on a
    -- healthy full hour. Stored so drift is monitorable, not invisible.
    acct_gap_s            double precision GENERATED ALWAYS AS (
                              EXTRACT(EPOCH FROM (end_time - start_time))::double precision
                              - (run_time + unplanned_stop_time + planned_stop_time + not_scheduled_time)
                          ) STORED,
    -- BRIEF section 4 signature: reject == -good with no run time. Looks
    -- like a post-hoc count adjustment rather than production.
    is_count_adjustment   boolean GENERATED ALWAYS AS (
                              reject_count = -good_count AND run_time = 0 AND good_count > 0
                          ) STORED,
    config_epoch          int    NOT NULL DEFAULT 1,
    deleted_at            timestamptz,
    first_loaded_at       timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, shift_hour_event_id)
);

-- shift channel. Same shape, minus is_full_hour (meaningless for a shift).
CREATE TABLE IF NOT EXISTS shift_record (
    device_id             text   NOT NULL REFERENCES device (device_id),
    shift_event_id        bigint NOT NULL,
    record_order          bigint,
    sync_id               bigint,
    production_day_number int,
    production_day_number_raw text,              -- as sent, when it was a list or span; production_day_number holds the first day
    start_time            timestamptz,
    end_time              timestamptz,
    shift                 text,
    shift_hour            text,
    part                  text,
    job                   text,
    in_count              double precision,
    good_count            double precision,
    reject_count          double precision,
    run_time              double precision,
    unplanned_stop_time   double precision,
    planned_stop_time     double precision,
    not_scheduled_time    double precision,
    ideal_time            double precision,
    target_count          double precision,
    acct_gap_s            double precision GENERATED ALWAYS AS (
                              EXTRACT(EPOCH FROM (end_time - start_time))::double precision
                              - (run_time + unplanned_stop_time + planned_stop_time + not_scheduled_time)
                          ) STORED,
    is_count_adjustment   boolean GENERATED ALWAYS AS (
                              reject_count = -good_count AND run_time = 0 AND good_count > 0
                          ) STORED,
    config_epoch          int    NOT NULL DEFAULT 1,
    deleted_at            timestamptz,
    first_loaded_at       timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, shift_event_id)
);

-- process_state channel: one row per production state. The Pareto source.
CREATE TABLE IF NOT EXISTS state_event (
    device_id                         text   NOT NULL REFERENCES device (device_id),
    process_state_event_id            bigint NOT NULL,
    record_order                      bigint,
    sync_id                           bigint,
    production_day_number             int,
    production_day_number_raw         text,              -- as sent, when it was a list or span; production_day_number holds the first day
    start_time                        timestamptz,
    end_time                          timestamptz,
    duration_s                        double precision,   -- device field: duration
    process_state                     text,
    process_state_reason              text,               -- raw key
    process_state_reason_display_name text,
    performance_impact                text,               -- unplanned_stop, planned_stop, ...
    production_phase                  text,
    part                              text,
    shift                             text,
    config_epoch                      int    NOT NULL DEFAULT 1,
    deleted_at                        timestamptz,
    first_loaded_at                   timestamptz NOT NULL DEFAULT now(),
    updated_at                        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, process_state_event_id)
);

-- part channel: one row per part run.
CREATE TABLE IF NOT EXISTS part_run (
    device_id             text   NOT NULL REFERENCES device (device_id),
    part_event_id         bigint NOT NULL,
    record_order          bigint,
    sync_id               bigint,
    production_day_number int,
    production_day_number_raw text,              -- as sent, when it was a list or span; production_day_number holds the first day
    start_time            timestamptz,
    end_time              timestamptz,
    part                  text,
    job                   text,
    duration_s            double precision,
    run_time              double precision,
    unplanned_stop_time   double precision,
    planned_stop_time     double precision,
    in_count              double precision,
    good_count            double precision,
    ideal_time            double precision,
    ideal_cycle_time      double precision,   -- as configured on the device
    target_count          double precision,
    target_cycles         double precision,
    run_cycles            double precision,
    small_stops           double precision,
    expected_count_per_cycle double precision,   -- device setting: pieces per cycle. 0.0 = unconfigured
    config_epoch          int    NOT NULL DEFAULT 1,
    deleted_at            timestamptz,
    first_loaded_at       timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, part_event_id)
);

-- job channel. Same shape as part_run.
CREATE TABLE IF NOT EXISTS job_run (
    device_id             text   NOT NULL REFERENCES device (device_id),
    job_event_id          bigint NOT NULL,
    record_order          bigint,
    sync_id               bigint,
    production_day_number int,
    production_day_number_raw text,              -- as sent, when it was a list or span; production_day_number holds the first day
    start_time            timestamptz,
    end_time              timestamptz,
    part                  text,
    job                   text,
    duration_s            double precision,
    run_time              double precision,
    unplanned_stop_time   double precision,
    planned_stop_time     double precision,
    in_count              double precision,
    good_count            double precision,
    ideal_time            double precision,
    ideal_cycle_time      double precision,
    target_count          double precision,
    target_cycles         double precision,
    run_cycles            double precision,
    small_stops           double precision,
    expected_count_per_cycle double precision,   -- device setting: pieces per cycle. 0.0 = unconfigured
    config_epoch          int    NOT NULL DEFAULT 1,
    deleted_at            timestamptz,
    first_loaded_at       timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, job_event_id)
);

-- quality_event channel. Empty on all four devices today; modelled so it
-- starts populating the moment reject scanning is configured. Keyed on the
-- device's record_id, which this channel exposes.
CREATE TABLE IF NOT EXISTS quality_event (
    device_id                  text   NOT NULL REFERENCES device (device_id),
    record_id                  bigint NOT NULL,
    event_id                   bigint,
    record_order               bigint,
    slice_order                bigint,
    sync_id                    bigint,
    production_day_number      int,
    reject_reason              text,
    reject_reason_display_name text,
    count                      double precision,   -- device field name, kept as-is
    config_epoch               int    NOT NULL DEFAULT 1,
    deleted_at                 timestamptz,
    first_loaded_at            timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, record_id)
);

-- Indexes: by time and by production day on every fact table.
CREATE INDEX IF NOT EXISTS hour_record_time   ON hour_record   (device_id, start_time);
CREATE INDEX IF NOT EXISTS hour_record_day    ON hour_record   (device_id, production_day_number);
CREATE INDEX IF NOT EXISTS shift_record_time  ON shift_record  (device_id, start_time);
CREATE INDEX IF NOT EXISTS shift_record_day   ON shift_record  (device_id, production_day_number);
CREATE INDEX IF NOT EXISTS state_event_time   ON state_event   (device_id, start_time);
CREATE INDEX IF NOT EXISTS state_event_day    ON state_event   (device_id, production_day_number);
CREATE INDEX IF NOT EXISTS part_run_time      ON part_run      (device_id, start_time);
CREATE INDEX IF NOT EXISTS part_run_day       ON part_run      (device_id, production_day_number);
CREATE INDEX IF NOT EXISTS part_run_part      ON part_run      (part);
CREATE INDEX IF NOT EXISTS job_run_time       ON job_run       (device_id, start_time);
CREATE INDEX IF NOT EXISTS job_run_day        ON job_run       (device_id, production_day_number);
CREATE INDEX IF NOT EXISTS quality_event_day  ON quality_event (device_id, production_day_number);

-- ===================================================== config-epoch triggers

-- Epoch in force for a device at a moment. Falls back to 1 so a missing
-- epoch row can never fail an insert.
CREATE OR REPLACE FUNCTION config_epoch_at(p_device text, p_at timestamptz)
RETURNS int LANGUAGE sql STABLE AS $$
    SELECT COALESCE((
        SELECT config_epoch FROM device_config_epoch
        WHERE device_id = p_device AND effective_from <= p_at
        ORDER BY effective_from DESC LIMIT 1
    ), 1)
$$;

-- quality_event has no start_time; place it by the start of its production
-- day (midnight plant time - close enough except on the changeover day).
CREATE OR REPLACE FUNCTION production_day_start(n int) RETURNS timestamptz
LANGUAGE sql STABLE AS $$
    SELECT (production_day(n)::timestamp AT TIME ZONE plant_tz())
$$;

CREATE OR REPLACE FUNCTION stamp_config_epoch() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.config_epoch := config_epoch_at(NEW.device_id, NEW.start_time);
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION stamp_config_epoch_by_day() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.config_epoch := config_epoch_at(NEW.device_id, production_day_start(NEW.production_day_number));
    RETURN NEW;
END $$;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['hour_record', 'shift_record', 'state_event', 'part_run', 'job_run'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', t || '_epoch', t);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE INSERT OR UPDATE OF start_time, device_id ON %I '
            'FOR EACH ROW EXECUTE FUNCTION stamp_config_epoch()', t || '_epoch', t);
    END LOOP;
    DROP TRIGGER IF EXISTS quality_event_epoch ON quality_event;
    CREATE TRIGGER quality_event_epoch
        BEFORE INSERT OR UPDATE OF production_day_number, device_id ON quality_event
        FOR EACH ROW EXECUTE FUNCTION stamp_config_epoch_by_day();
END $$;

-- When an epoch is added, moved or removed, re-stamp that device's history.
CREATE OR REPLACE FUNCTION restamp_config_epoch() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    dev text := COALESCE(NEW.device_id, OLD.device_id);
    t   text;
BEGIN
    FOREACH t IN ARRAY ARRAY['hour_record', 'shift_record', 'state_event', 'part_run', 'job_run'] LOOP
        EXECUTE format(
            'UPDATE %I SET config_epoch = config_epoch_at(device_id, start_time) '
            'WHERE device_id = $1 AND config_epoch IS DISTINCT FROM config_epoch_at(device_id, start_time)', t)
        USING dev;
    END LOOP;
    UPDATE quality_event
       SET config_epoch = config_epoch_at(device_id, production_day_start(production_day_number))
     WHERE device_id = dev
       AND config_epoch IS DISTINCT FROM config_epoch_at(device_id, production_day_start(production_day_number));
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS device_config_epoch_restamp ON device_config_epoch;
CREATE TRIGGER device_config_epoch_restamp
    AFTER INSERT OR UPDATE OR DELETE ON device_config_epoch
    FOR EACH ROW EXECUTE FUNCTION restamp_config_epoch();

-- Every new device gets epoch 1 from the beginning of time, counts
-- untrusted. SECURITY DEFINER so the sync role can register a device
-- without being able to write epochs itself.
CREATE OR REPLACE FUNCTION seed_config_epoch() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    INSERT INTO device_config_epoch (device_id, config_epoch, effective_from, counts_trusted, note)
    VALUES (NEW.device_id, 1, '-infinity', false, 'initial configuration')
    ON CONFLICT DO NOTHING;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS device_seed_epoch ON device;
CREATE TRIGGER device_seed_epoch
    AFTER INSERT ON device
    FOR EACH ROW EXECUTE FUNCTION seed_config_epoch();

-- ========================================================= part standards

-- Derived ideal cycle time per part, over runs with run_cycles > 200, and
-- the device's pieces-per-cycle setting from the latest run of the part on
-- each device. Called by the sync at the end of each run. Never touches the
-- sap_* columns.
CREATE OR REPLACE FUNCTION refresh_part_standard() RETURNS int
LANGUAGE sql AS $$
    WITH derived AS (
        SELECT part,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY ideal_time / run_cycles) AS ict,
               stddev_samp(ideal_time / run_cycles)                                AS sd,
               count(*)::int                                                       AS n
          FROM part_run
         WHERE deleted_at IS NULL AND part IS NOT NULL AND run_cycles > 200
         GROUP BY part
    ), latest AS (
        SELECT DISTINCT ON (part, device_id) part, device_id, expected_count_per_cycle AS epc
          FROM part_run
         WHERE deleted_at IS NULL AND part IS NOT NULL
         ORDER BY part, device_id, end_time DESC NULLS LAST
    ), epc AS (
        SELECT part,
               CASE WHEN min(epc) = max(epc) THEN min(epc) END AS epc,
               min(epc) IS DISTINCT FROM max(epc)              AS inconsistent
          FROM latest GROUP BY part
    ), merged AS (
        SELECT part, d.ict, d.sd, d.n, e.epc, e.inconsistent
          FROM derived d FULL JOIN epc e USING (part)
    ), up AS (
        INSERT INTO part_standard AS ps
               (part, ideal_cycle_time, ict_stddev, sample_runs,
                expected_count_per_cycle, epc_inconsistent, ict_updated_at)
        SELECT part, ict, sd, n, epc, inconsistent, now() FROM merged
        ON CONFLICT (part) DO UPDATE SET
            ideal_cycle_time         = EXCLUDED.ideal_cycle_time,
            ict_stddev               = EXCLUDED.ict_stddev,
            sample_runs              = EXCLUDED.sample_runs,
            expected_count_per_cycle = EXCLUDED.expected_count_per_cycle,
            epc_inconsistent         = EXCLUDED.epc_inconsistent,
            ict_updated_at           = EXCLUDED.ict_updated_at,
            updated_at               = now()
        WHERE (ps.ideal_cycle_time, ps.ict_stddev, ps.sample_runs,
               ps.expected_count_per_cycle, ps.epc_inconsistent)
              IS DISTINCT FROM
              (EXCLUDED.ideal_cycle_time, EXCLUDED.ict_stddev, EXCLUDED.sample_runs,
               EXCLUDED.expected_count_per_cycle, EXCLUDED.epc_inconsistent)
        RETURNING 1
    )
    SELECT count(*)::int FROM up
$$;

-- ================================================================== views
--
-- Every view excludes deleted_at rows. Rates are computed from summed
-- times, never averaged ratios.

-- Time accounting per device per production day.
--
-- Two populations, named so they cannot be confused:
--   *_all              every closed record, including the long overnight
--                      not-scheduled blocks. Hours and utilization use
--                      these: utilization is scheduled / elapsed time, so
--                      dropping not-scheduled blocks would remove exactly
--                      the denominator it exists to measure.
--   *_full_hours_only  full one-hour records only. Availability and
--                      performance are rates over scheduled time, where
--                      partial records distort rather than inform.
--
-- The notebook report computed everything from full hours only, so its
-- utilization reads higher than this view. That is the old report's flaw,
-- fixed here; the app's methodology footer must say so.
CREATE OR REPLACE VIEW daily_time_accounting AS
SELECT
    device_id,
    production_day_number,
    production_day(production_day_number)                          AS production_day,
    count(*)                                                       AS records_all,
    count(*) FILTER (WHERE is_full_hour)                           AS full_hours,
    sum(run_time)                / 3600.0                          AS run_hours_all,
    sum(unplanned_stop_time)     / 3600.0                          AS unplanned_hours_all,
    sum(planned_stop_time)       / 3600.0                          AS planned_hours_all,
    sum(not_scheduled_time)      / 3600.0                          AS not_scheduled_hours_all,
    sum(run_time + unplanned_stop_time + planned_stop_time) / 3600.0
                                                                   AS scheduled_hours_all,
    sum(ideal_time)              / 3600.0                          AS ideal_hours_all,
    sum(run_time + unplanned_stop_time + planned_stop_time)
        / NULLIF(sum(run_time + unplanned_stop_time + planned_stop_time + not_scheduled_time), 0)
                                                                   AS utilization_all,
    sum(run_time) FILTER (WHERE is_full_hour)
        / NULLIF(sum(run_time + unplanned_stop_time + planned_stop_time) FILTER (WHERE is_full_hour), 0)
                                                                   AS availability_full_hours_only,
    sum(ideal_time) FILTER (WHERE is_full_hour)
        / NULLIF(sum(run_time) FILTER (WHERE is_full_hour), 0)     AS performance_full_hours_only
FROM hour_record
WHERE deleted_at IS NULL
GROUP BY device_id, production_day_number;

COMMENT ON VIEW daily_time_accounting IS
    'Hours and utilization over all closed records; availability and '
    'performance over full hours only. Utilization will read lower than the '
    'notebook report, which excluded not-scheduled blocks. quality, oee and '
    'teep are deliberately absent: they depend on counts (BRIEF section 4).';

-- Downtime Pareto source. Both stop types, so the line page can show
-- unplanned reasons and planned stops as separate tables. The cumulative
-- column depends on the selected period, so the app computes it.
CREATE OR REPLACE VIEW downtime_by_reason AS
SELECT
    device_id,
    production_day_number,
    performance_impact,
    process_state_reason,
    COALESCE(process_state_reason_display_name, process_state_reason, 'Unassigned') AS reason,
    count(*)                 AS events,
    sum(duration_s) / 3600.0 AS hours,
    avg(duration_s) / 60.0   AS mean_minutes
FROM state_event
WHERE deleted_at IS NULL
  AND performance_impact IN ('unplanned_stop', 'planned_stop')
GROUP BY device_id, production_day_number, performance_impact,
         process_state_reason, process_state_reason_display_name;

-- "Hours by activity" on the line page.
CREATE OR REPLACE VIEW activity_hours AS
SELECT
    device_id,
    production_day_number,
    process_state,
    performance_impact,
    count(*)                 AS events,
    sum(duration_s) / 3600.0 AS hours
FROM state_event
WHERE deleted_at IS NULL
GROUP BY device_id, production_day_number, process_state, performance_impact;

-- Products page. Per-cycle figures are the trustworthy primary.
--
-- Per-cycle vs per-piece is a units problem: the device's
-- expected_count_per_cycle converts one to the other, and it is currently
-- 0.0 (unconfigured) - the same root cause as the count defect. So:
--   sap_s_per_cycle        SAP's per-piece standard converted with that
--                          setting; NULL until it is configured
--   *_per_piece, good_count  from counts, so only from counts_trusted epochs
--   sap_comparison_status  drives the UI state. For
--                          'pieces_per_cycle_not_configured' show "SAP
--                          comparison unavailable: pieces-per-cycle not
--                          configured on the device".
CREATE OR REPLACE VIEW part_performance AS
WITH runs AS (
    SELECT r.*, COALESCE(e.counts_trusted, false) AS counts_trusted
      FROM part_run r
      LEFT JOIN device_config_epoch e USING (device_id, config_epoch)
     WHERE r.deleted_at IS NULL AND r.part IS NOT NULL
)
SELECT
    r.part,
    count(*)                                              AS runs,
    sum(r.run_time)   / 3600.0                            AS actual_h,
    sum(r.ideal_time) / 3600.0                            AS earned_h,
    sum(r.ideal_time) / NULLIF(sum(r.run_time), 0)        AS efficiency,
    (sum(r.ideal_time) - sum(r.run_time)) / 3600.0        AS variance_h,
    sum(r.run_cycles)                                     AS run_cycles,
    sum(r.run_time) / NULLIF(sum(r.run_cycles), 0)        AS actual_s_per_cycle,
    ps.ideal_cycle_time                                   AS derived_ict_s_per_cycle,
    ps.ict_stddev,
    ps.sample_runs                                        AS ict_sample_runs,
    ps.ict_unstable,
    ps.expected_count_per_cycle,
    ps.epc_inconsistent,
    ps.sap_standard,
    ps.sap_unit,
    ps.sap_s_per_piece,
    CASE WHEN ps.expected_count_per_cycle > 0 AND NOT ps.epc_inconsistent
         THEN ps.sap_s_per_piece * ps.expected_count_per_cycle END     AS sap_s_per_cycle,
    CASE
        WHEN ps.sap_s_per_piece IS NULL                        THEN 'no_sap_standard'
        WHEN ps.epc_inconsistent                               THEN 'pieces_per_cycle_inconsistent'
        WHEN COALESCE(ps.expected_count_per_cycle, 0) <= 0     THEN 'pieces_per_cycle_not_configured'
        ELSE 'available'
    END                                                   AS sap_comparison_status,
    count(*) FILTER (WHERE r.counts_trusted)              AS runs_with_trusted_counts,
    sum(r.good_count) FILTER (WHERE r.counts_trusted)     AS good_count,
    sum(r.run_time) FILTER (WHERE r.counts_trusted)
        / NULLIF(sum(r.good_count) FILTER (WHERE r.counts_trusted), 0)   AS actual_s_per_piece,
    sum(r.ideal_time) FILTER (WHERE r.counts_trusted)
        / NULLIF(sum(r.good_count) FILTER (WHERE r.counts_trusted), 0)   AS ideal_s_per_piece
FROM runs r
LEFT JOIN part_standard ps USING (part)
GROUP BY r.part, ps.part;

-- Everything suspicious, one row per record per issue. Shown in the app,
-- reported by verify.py, never fatal to a load.
CREATE OR REPLACE VIEW data_quality AS
WITH facts AS (
    SELECT 'hour_record' AS source, device_id, shift_hour_event_id AS event_id,
           start_time, end_time, production_day_number, config_epoch,
           in_count, good_count, reject_count, run_time, ideal_time,
           acct_gap_s, is_full_hour, is_count_adjustment
      FROM hour_record WHERE deleted_at IS NULL
    UNION ALL
    SELECT 'shift_record', device_id, shift_event_id,
           start_time, end_time, production_day_number, config_epoch,
           in_count, good_count, reject_count, run_time, ideal_time,
           acct_gap_s, NULL, is_count_adjustment
      FROM shift_record WHERE deleted_at IS NULL
    UNION ALL
    SELECT 'part_run', device_id, part_event_id,
           start_time, end_time, production_day_number, config_epoch,
           in_count, good_count, NULL, run_time, ideal_time,
           NULL, NULL, NULL
      FROM part_run WHERE deleted_at IS NULL
    UNION ALL
    SELECT 'job_run', device_id, job_event_id,
           start_time, end_time, production_day_number, config_epoch,
           in_count, good_count, NULL, run_time, ideal_time,
           NULL, NULL, NULL
      FROM job_run WHERE deleted_at IS NULL
)
SELECT f.source, f.device_id, f.event_id, f.start_time, f.production_day_number,
       f.config_epoch, i.issue, i.detail
FROM facts f
CROSS JOIN LATERAL (VALUES
    ('negative_reject',  f.reject_count < 0,
         format('reject_count %s', f.reject_count)),
    ('good_exceeds_in',  f.good_count > f.in_count,
         format('good %s > in %s', f.good_count, f.in_count)),
    ('performance_gt_1', f.ideal_time > f.run_time AND f.run_time > 0,
         format('performance %s - wrong ideal cycle time for this part', round((f.ideal_time / NULLIF(f.run_time, 0))::numeric, 3))),
    ('count_adjustment', f.is_count_adjustment,
         format('reject %s = -good, run_time 0', f.reject_count)),
    ('acct_gap',         f.is_full_hour AND abs(f.acct_gap_s) > 180,
         format('%s s unaccounted in a full hour', round(f.acct_gap_s::numeric, 1))),
    ('missing_times',    f.start_time IS NULL OR f.end_time IS NULL,
         'start_time or end_time missing')
) AS i (issue, hit, detail)
WHERE i.hit;

-- Per device per channel: is the sync keeping up?
CREATE OR REPLACE VIEW sync_status AS
SELECT
    s.device_id,
    s.channel,
    s.resume_key,
    s.last_sync_id,
    s.last_record_order,
    s.backfill_completed_at,
    s.last_success_at,
    now() - s.last_success_at AS staleness,
    CASE
        WHEN s.backfill_completed_at IS NULL                     THEN 'backfilling'
        WHEN s.last_error_at > COALESCE(s.last_success_at, '-infinity') THEN 'error'
        WHEN now() - s.last_success_at > interval '30 minutes'   THEN 'stale'
        ELSE 'ok'
    END AS health,
    s.rows_loaded,
    s.rows_flagged_deleted,
    (SELECT count(*) FROM sync_quarantine q
      WHERE q.device_id = s.device_id AND q.channel = s.channel
        AND q.resolved_at IS NULL)                              AS quarantined_open,
    s.last_error,
    s.last_error_at
FROM sync_state s;

-- Is the sync service alive? Runs every 5 minutes, so 15 minutes of silence
-- is two missed runs.
CREATE OR REPLACE VIEW collector_status AS
SELECT
    h.instance_id,
    h.hostname,
    h.version,
    h.last_heartbeat,
    now() - h.last_heartbeat AS staleness,
    CASE
        WHEN now() - h.last_heartbeat < interval '15 minutes' THEN 'ok'
        WHEN now() - h.last_heartbeat < interval '60 minutes' THEN 'lagging'
        ELSE 'down'
    END AS health,
    h.last_run_ok,
    h.devices_ok,
    h.devices_total,
    h.rows_upserted,
    (SELECT count(*) FROM sync_quarantine WHERE resolved_at IS NULL) AS quarantined_open,
    h.last_error,
    h.last_error_at
FROM sync_heartbeat h;

-- One query for monitoring: any row here means someone should look.
CREATE OR REPLACE VIEW sync_alerts AS
SELECT 'sync_' || health AS alert, NULL::text AS device_id,
       format('instance %s, last heartbeat %s ago', instance_id, date_trunc('second', staleness)) AS detail
  FROM collector_status WHERE health <> 'ok'
UNION ALL
SELECT 'no_heartbeat', NULL, 'sync_heartbeat is empty - the sync has never run'
 WHERE NOT EXISTS (SELECT 1 FROM sync_heartbeat)
UNION ALL
SELECT 'channel_' || health, device_id, format('%s: %s', channel, COALESCE(last_error, 'no success for ' || date_trunc('second', staleness)))
  FROM sync_status WHERE health IN ('error', 'stale')
UNION ALL
SELECT 'quarantine', device_id, format('%s open row(s) on %s, oldest %s', count(*), channel, min(first_seen_at))
  FROM sync_quarantine WHERE resolved_at IS NULL GROUP BY device_id, channel
UNION ALL
SELECT 'live_panel_stale', device_id, format('current_state last read %s', COALESCE(observed_at::text, 'never'))
  FROM current_state WHERE observed_at IS NULL OR now() - observed_at > interval '30 minutes';

-- ================================================================== roles
--
-- Group roles only; no passwords in this file. Create the login users by
-- hand (see MAINTENANCE). The sync role reads as well as writes, because its
-- upserts read existing rows. The meaningful boundary is that the app role
-- is read-only, except for the SAP standard columns.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xl_sync') THEN
        CREATE ROLE xl_sync NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xl_reader') THEN
        CREATE ROLE xl_reader NOLOGIN;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO xl_sync, xl_reader', current_database());
END $$;

GRANT USAGE ON SCHEMA public TO xl_sync, xl_reader;

-- Sync service.
GRANT SELECT, INSERT, UPDATE ON
    device, sync_state, sync_heartbeat, sync_quarantine, dimension_value, current_state,
    hour_record, shift_record, state_event, part_run, job_run, quality_event
    TO xl_sync;
GRANT SELECT ON device_config_epoch TO xl_sync;            -- epochs are set by a human
GRANT SELECT ON part_standard TO xl_sync;
GRANT INSERT (part, ideal_cycle_time, ict_stddev, sample_runs, expected_count_per_cycle,
              epc_inconsistent, ict_updated_at, updated_at)
    ON part_standard TO xl_sync;
GRANT UPDATE (ideal_cycle_time, ict_stddev, sample_runs, expected_count_per_cycle,
              epc_inconsistent, ict_updated_at, updated_at)
    ON part_standard TO xl_sync;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO xl_sync;
GRANT EXECUTE ON FUNCTION refresh_part_standard() TO xl_sync;

-- Flask app.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO xl_reader;   -- includes views
GRANT INSERT (part, sap_standard, sap_unit, sap_updated_at, sap_updated_by, updated_at)
    ON part_standard TO xl_reader;
GRANT UPDATE (sap_standard, sap_unit, sap_updated_at, sap_updated_by, updated_at)
    ON part_standard TO xl_reader;

-- ================================================================ devices

INSERT INTO device (device_id, host) VALUES
    ('FR8',   '10.15.22.19'),
    ('AreaB', '10.15.22.83'),
    ('AreaD', '10.15.22.95'),
    ('AreaE', '10.15.22.119')
ON CONFLICT (device_id) DO NOTHING;

COMMIT;

-- ============================================================ MAINTENANCE
--
-- Login users (run once, as the server admin; choose real passwords):
--
--   CREATE ROLE xl_sync_svc LOGIN PASSWORD '...' IN ROLE xl_sync;
--   CREATE ROLE xl_flask    LOGIN PASSWORD '...' IN ROLE xl_reader;
--
-- Configuration fixed on a device (e.g. Vorne corrects FR8's part setup):
--
--   INSERT INTO device_config_epoch (device_id, config_epoch, effective_from, counts_trusted, note)
--   VALUES ('FR8', 2, '2026-11-02 07:00 America/Toronto', true, 'part config corrected by Vorne, ticket #...');
--
--   Every FR8 fact row is re-stamped automatically by start_time. Rows
--   before that moment stay epoch 1 and untrusted. To check:
--
--   SELECT config_epoch, count(*), min(start_time), max(start_time)
--     FROM hour_record WHERE device_id = 'FR8' GROUP BY 1 ORDER BY 1;
--
-- Adding a device: INSERT INTO device (device_id, host) VALUES (...);
--   epoch 1 is created for it automatically. Then add it to config.yaml.
--
-- Quarantine: SELECT * FROM sync_quarantine WHERE resolved_at IS NULL;
--   The re-sync window retries these every run. If one persists, the error
--   column says why. Once fixed (or deliberately abandoned):
--   UPDATE sync_quarantine SET resolved_at = now() WHERE id = ...;
--
-- Force a full re-pull of one channel (e.g. after a firmware upgrade):
--   UPDATE sync_state SET last_sync_id = NULL, last_record_order = NULL,
--          backfill_completed_at = NULL WHERE device_id = 'FR8' AND channel = 'shift_hour';
--   Upserts make the overlap harmless.
--
-- Health, all in one place:
--   SELECT * FROM sync_alerts;
--
-- Schema changes after first install: write an ALTER TABLE migration. This
-- file's CREATE TABLE IF NOT EXISTS will not modify an existing table.
