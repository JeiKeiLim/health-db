-- 0002_parse_everything.sql — ECG + heart-rate-notification lanes (job-1; run 2026-09-17-normalizer-audit, spec §6)
--
-- Additive-only: CREATE TABLE for the two new ingest lanes ("parse
-- everything" spec §2/§3). No ALTER/DROP on existing tables (anti-scenario 6:
-- 0001 is frozen; changes land only as a NEW migration file).
--
-- Conventions carried from 0001_init.sql:
-- - timestamps timestamptz UTC; day_kst = KST calendar day via the FIXED
--   UTC+9 expression copied verbatim from 0001_init.sql:50-51 (iron law 4:
--   fixed offset, never a named timezone — 'Asia/Seoul' would not be
--   immutable and the generated column would be rejected).
-- - latest-wins upserts (iron law 2): the UNIQUE (source_name, start_ts)
--   keys on the parent tables are the ON CONFLICT targets for the
--   pipeline's DO UPDATE SQL (issued by db/helpers.py, job-7 — not here).
-- - session lanes (like workouts): intentionally OUT of daily_rollups and
--   OUT of raw_samples partitioning; no partition DDL needed.
-- - child tables cascade on parent delete (workout_heart_rate pattern,
--   0001_init.sql:98-103); voltage PK (ecg_id, seq) keeps the array index —
--   re-ingest replaces the full sample set, never re-appends.
-- - ingested_at DEFAULT now() for audit provenance, same as workouts.

-- ------------------------------------------------------------------------- ecgs
CREATE TABLE ecgs (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_name        text NOT NULL,
    start_ts           timestamptz NOT NULL,
    end_ts             timestamptz NOT NULL,
    day_kst            date GENERATED ALWAYS AS
                           (((start_ts AT TIME ZONE 'UTC') + interval '9 hours')::date) STORED,
    classification     text,
    sampling_frequency integer,
    average_heart_rate integer,
    voltage_count      integer NOT NULL,
    ingested_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_name, start_ts)
);

-- ---------------------------------------------------------------- ecg_voltages
CREATE TABLE ecg_voltages (
    ecg_id  bigint NOT NULL REFERENCES ecgs (id) ON DELETE CASCADE,
    seq     integer NOT NULL,
    ts      timestamptz NOT NULL,
    voltage double precision NOT NULL,
    units   text NOT NULL,
    PRIMARY KEY (ecg_id, seq)
);

-- --------------------------------------------------- heart_rate_notifications
CREATE TABLE heart_rate_notifications (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_name text NOT NULL,
    start_ts   timestamptz NOT NULL,
    end_ts     timestamptz NOT NULL,
    day_kst    date GENERATED ALWAYS AS
                   (((start_ts AT TIME ZONE 'UTC') + interval '9 hours')::date) STORED,
    kind       text NOT NULL,
    threshold  integer,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_name, start_ts)
);

-- ------------------------------------------ heart_rate_notification_samples
CREATE TABLE heart_rate_notification_samples (
    notification_id bigint NOT NULL REFERENCES heart_rate_notifications (id) ON DELETE CASCADE,
    ts              timestamptz NOT NULL,
    kind            text NOT NULL,  -- 'hr' or 'hrv': one ts may carry both
    value           double precision NOT NULL,
    units           text,
    PRIMARY KEY (notification_id, ts, kind)
);