-- 0001_init.sql — full HAE ingest schema (job slice-2-schema; spec § Database Schema)
--
-- Conventions (spec): all timestamps timestamptz UTC; day_kst = KST calendar
-- day (fixed UTC+9 — KST has no DST); FKs enforced; plain SQL applied by the
-- versioned applier (health-migrate) inside one transaction per migration.
--
-- Notes carried from research/postgres18-schema.md:
-- - raw_samples is PARTITION BY RANGE (ts); the PK (metric_id, source_id, ts)
--   includes the partition key, which is required and satisfied.
-- - day_kst is a STORED generated column over the FIXED-offset expression
--   ((ts AT TIME ZONE 'UTC') + interval '9 hours')::date. `AT TIME ZONE 'UTC'`
--   on timestamptz is immutable, so the generated column is accepted by PG 18
--   (a named zone like 'Asia/Seoul' would NOT be immutable here). Iron law 5:
--   day semantics are fixed UTC+9, never a named timezone, never server-local.
-- - Upserts are latest-wins (iron law 6): raw_samples ON CONFLICT
--   (metric_id, source_id, ts) DO UPDATE (SQL issued by the pipeline, not
--   here); workouts upsert by UUID.
-- - kJ→kcal (÷ 4.184) happens at normalize time (slice-2-normalize) — stored
--   energy columns are already kcal; intensity is numeric (fixes the old
--   SQLite TEXT/numeric mismatch).
-- - No partition DDL here: partitions are created on demand by
--   health_db.db.partitions.ensure_partitions (advisory-lock, idempotent).
--   A base CHECK constraint (ts IS NOT NULL) guards inserts until a partition
--   exists; without any partition an insert fails cleanly (research note 3:
--   declarative partitioning never auto-creates).

-- ---------------------------------------------------------------- metric_dim
CREATE TABLE metric_dim (
    metric_id      smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name           text NOT NULL UNIQUE,
    display_name   text NOT NULL,
    canonical_unit text NOT NULL,
    is_cumulative  boolean NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- source_dim
CREATE TABLE source_dim (
    source_id  smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------- raw_samples (parent)
CREATE TABLE raw_samples (
    metric_id   smallint NOT NULL REFERENCES metric_dim (metric_id),
    source_id   smallint NOT NULL REFERENCES source_dim (source_id),
    ts          timestamptz NOT NULL,
    qty         double precision NOT NULL,
    day_kst     date GENERATED ALWAYS AS
                    (((ts AT TIME ZONE 'UTC') + interval '9 hours')::date) STORED,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (metric_id, source_id, ts)
) PARTITION BY RANGE (ts);

-- ---------------------------------------------------------------- daily_rollups
CREATE TABLE daily_rollups (
    metric_id    smallint NOT NULL REFERENCES metric_dim (metric_id),
    day_kst      date NOT NULL,
    sum_qty      double precision NOT NULL,
    avg_qty      double precision NOT NULL,
    min_qty      double precision NOT NULL,
    max_qty      double precision NOT NULL,
    sample_count bigint NOT NULL,
    updated_at   timestamptz NOT NULL,
    PRIMARY KEY (metric_id, day_kst)
);

-- ------------------------------------------------------------------- workouts
CREATE TABLE workouts (
    id                          uuid PRIMARY KEY,
    name                        text NOT NULL,
    start_ts                    timestamptz NOT NULL,
    end_ts                      timestamptz NOT NULL,
    day_kst                     date NOT NULL,
    duration_sec                double precision NOT NULL,
    active_energy_kcal          double precision,
    total_energy                double precision,
    basal_energy                double precision,
    mets                        double precision,
    temperature                 double precision,
    humidity                    double precision,
    is_indoor                   boolean,
    location                    text,
    intensity                   double precision,
    distance_km                 double precision,
    walking_running_distance_km double precision,
    step_count                  bigint,
    step_cadence                double precision,
    speed                       double precision,
    avg_heart_rate              double precision,
    max_heart_rate              double precision,
    heart_rate_recovery         jsonb,
    ingested_at                 timestamptz NOT NULL
);

-- -------------------------------------------------------- workout_heart_rate
CREATE TABLE workout_heart_rate (
    workout_id uuid NOT NULL REFERENCES workouts (id) ON DELETE CASCADE,
    ts         timestamptz NOT NULL,
    heart_rate integer NOT NULL,
    PRIMARY KEY (workout_id, ts)
);

-- --------------------------------------------------------------------- routes
CREATE TABLE routes (
    workout_id uuid NOT NULL REFERENCES workouts (id) ON DELETE CASCADE,
    ts         timestamptz NOT NULL,
    latitude   double precision NOT NULL,
    longitude  double precision NOT NULL,
    course     double precision,
    speed      double precision,
    elevation  double precision,
    h_acc      double precision,
    v_acc      double precision,
    PRIMARY KEY (workout_id, ts)
);

-- ------------------------------------------------------------------ raw_ingest
CREATE TABLE raw_ingest (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sha256          text NOT NULL UNIQUE,
    received_at     timestamptz NOT NULL,
    automation_name text,
    automation_id   text,
    session_id      text,
    data_type       text,
    payload         jsonb NOT NULL,
    outcome         text NOT NULL,
    error_note      text
);

-- ------------------------------------------------------------------- auth_log
CREATE TABLE auth_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts         timestamptz NOT NULL,
    ip         text,
    user_agent text,
    reason     text NOT NULL
);

-- Useful for the stats endpoint (recent 401s) without a seq scan.
CREATE INDEX auth_log_ts_idx ON auth_log (ts DESC);

-- Helpful for freshness/coverage reads on the parent (per-partition index).
CREATE INDEX raw_samples_day_kst_idx ON raw_samples (day_kst);