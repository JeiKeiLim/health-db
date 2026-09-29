-- scripts/probe_cleanup.sql — remove ingest probe-test artifacts from the production DB (ops-only).
--
-- Deletes the auto-registered test-acceptance metrics '__probe_metric' and
-- '__probe_metric2' (specs/2026-09-28-mcp-analysis-parity.md §5) from the three
-- data tables:
--
--   1. daily_rollups  (per-day aggregates)
--   2. raw_samples     (raw sample rows)
--   3. metric_dim      (catalog entry — delete LAST, children reference it)
--
-- Run inside the container (psql heredoc):
--   docker exec -i health-db psql "$PSQL_DATABASE_URL" -f - < scripts/probe_cleanup.sql
-- or docker compose run health-db psql ... depending on the deployment.
--
-- ⚠ ORDERING — MUST run AFTER the backfill replay:
--     docker exec health-db health-replay --dir data/archive --force
-- A full replay re-normalizes archived payloads verbatim; if the archived
-- probe payloads are replayed BEFORE this cleanup, the upsert path resurrects
-- the probe rows from raw_ingest and this script would have to run again.
-- Deploy sequence step 3 (after step 2's backfill replay) — never before.
--
-- Idempotency: deleting absent rows is a no-op; safe to re-run (the WHERE
-- subqueries simply match nothing). Payloads stay in the archive
-- (raw_ingest + disk, iron law 2) — probes in future replays are acceptable;
-- purging the matching archived payloads is a possible follow-up.
--
-- NOT a migration (data cleanup, not schema): never run under health-migrate;
-- forward-only scripts (migrations/) are frozen to additive-only changes.

BEGIN;

DELETE FROM daily_rollups WHERE metric_id IN
  (SELECT metric_id FROM metric_dim WHERE name IN ('__probe_metric', '__probe_metric2'));

DELETE FROM raw_samples WHERE metric_id IN
  (SELECT metric_id FROM metric_dim WHERE name IN ('__probe_metric', '__probe_metric2'));

DELETE FROM metric_dim WHERE name IN ('__probe_metric', '__probe_metric2');

COMMIT;