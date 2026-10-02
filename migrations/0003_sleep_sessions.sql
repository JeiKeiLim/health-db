-- 0003_sleep_sessions.sql — sleep-session detail lane (job-1; run 2026-09-28-mcp-analysis-parity, spec §Database Schema)
--
-- Additive-only: CREATE TABLE for the sleep-session lane ("MCP analysis
-- parity" spec §1/§Database Schema). No ALTER/DROP on existing tables
-- (anti-scenario 5: 0001/0002 are frozen; changes land only as a NEW
-- migration file). One transaction — the generic applier (db/migrations.py)
-- runs each migration file inside a transaction that also records its
-- version, so a failure aborts startup with no partial state.
--
-- Conventions carried from 0001/0002:
-- - timestamps timestamptz UTC; day_kst = KST calendar day via the FIXED
--   UTC+9 expression copied verbatim from 0001_init.sql:50-51 (iron law 4:
--   fixed offset, never a named timezone — 'Asia/Seoul' would not be
--   immutable and the generated column would be rejected). sleep_sessions
--   day_kst buckets on record_date (payload `date`), matching the KST day
--   bucketing of the existing sleep_analysis rows.
-- - latest-wins upserts (iron law 2): UNIQUE (source_name, sleep_start) is
--   the ON CONFLICT target for the pipeline's DO UPDATE SQL (issued by
--   db/helpers.py, job-3 — not here). Re-ingest is fully idempotent: two
--   consecutive replay runs leave row counts unchanged (spec success
--   criterion 2).
-- - session lanes (like workouts/ecgs/heart_rate_notifications):
--   intentionally OUT of daily_rollups and OUT of raw_samples partitioning;
--   no partition DDL needed (anti-scenario 6: no rollup contamination).
-- - payload-truth semantics: asleep = 0 on Watch rows is a known sentinel
--   stored verbatim, never re-derived; rows lacking a parseable sleep_start
--   are skipped and counted under skipped.sleep_session at ingest time —
--   zero silent drops (iron law 8).
-- - ingested_at DEFAULT now() for audit provenance, same as 0002 lanes.

-- --------------------------------------------------------------- sleep_sessions
CREATE TABLE sleep_sessions (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_name  text NOT NULL,
    record_date  timestamptz NOT NULL,
    day_kst      date GENERATED ALWAYS AS
                     (((record_date AT TIME ZONE 'UTC') + interval '9 hours')::date) STORED,
    sleep_start  timestamptz NOT NULL,
    sleep_end    timestamptz,
    in_bed_start timestamptz,
    in_bed_end   timestamptz,
    total_sleep  double precision,
    asleep       double precision,
    deep         double precision,
    core         double precision,
    rem          double precision,
    awake        double precision,
    in_bed       double precision,
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_name, sleep_start)
);

-- Day-range filter support for list_sleep_sessions (from/to KST-day filter,
-- same house convention as raw_samples_day_kst_idx, 0001_init.sql:146).
CREATE INDEX sleep_sessions_day_kst_idx ON sleep_sessions (day_kst);