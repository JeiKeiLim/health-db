# interaction-e2e job-10 — probe-cleanup ops surface: live psql + replay-ordering probes, pass

type: journal
source_job: job-10
run: 2026-09-28-mcp-analysis-parity

## Findings

- **surface**: cli/ops (psql script + documented `docker exec ... psql -f -` invocation). No browser; layer2 not_applicable. Verified LIVE, not just via the author's tests: disposable postgres:18.6 container (iron law 7), real uvicorn ingest (:8000) + MCP (:8100) servers, real HTTP `/api/ingest` posts, real psql stdin execution of `scripts/probe_cleanup.sql`.
- **scripted**: unit `tests/unit/test_probe_cleanup_script.py` 7/7; acceptance `uv run pytest tests/acceptance/mcp_analysis_parity.py -m requires_pg` 26/26 (76 s); `uv run ruff check src/ tests/` clean.
- **live e2e chain (deploy-sequence simulation)**: ingest `__probe_metric` (1 sample), `__probe_metric2` (2 samples), `step_count` (2 samples) → all 200 `stored`, auto-registered 3 metrics, rollups+raws landed → `health-replay --dir ... --force` processed 3 payloads → cleanup `DELETE 3/3/2` + COMMIT → idempotent re-run `DELETE 0/0/0` → `list_metrics` count 1, only `step_count`; `freshness_check` has no probe entries; probe data gone while `step_count` rows/rollups survive exactly (sum 12700 = 8500+4200, `query_day` intact).
- **anti-scenario 8 confirmed LIVE**: re-running `health-replay --force` after cleanup resurrects `__probe_metric`/`__probe_metric2` from archived payloads (exactly the documented hazard); re-running the cleanup removes them again (DELETE 4/4 counting + pre-existing rollups? no — 3/3/2 then 0/0/0 pattern held). This validates the header/README ordering rule with observed evidence, not just doc-strings.
- **transactionality edge**: hand-broken variant (syntax error mid-transaction after the two rollups/raws deletes) → psql ROLLBACKs, rows survive; exit 0 with ON_ERROR_STOP unset (psql semantics, worth a nit — see below). Real script + `--set=ON_ERROR_STOP=1` and repeated runs all clean.
- **precision probes**: exact-name match only — `__PROBE_METRIC` (case), `__probe_metricX` (prefix/substring) SURVIVE cleanup (DELETE 0 while present); only the two exact names deleted. No LIKE/pattern over-reach.
- **concurrency**: cleanup ran against live uvicorn pool while `/api/health` polled concurrently — no deadlocks, correct deletes, servers unaffected.
- **MCP parity (criterion 7 through the real /mcp wire)**: `list_metrics` count 1 / no probes; `freshness_check` no probes; `health://freshness` no probe mentions; `health://schema` contains `sleep_sessions`; deleted metric on `get_metric_series('__probe_metric')` → isError with actionable "not found — call list_metrics" message (no crash); unknown tool → isError.
- **resurrection is by-design documented**: spec §5 Out of Scope — payloads stay archived; a post-cleanup replay re-creating probe metrics is acknowledged/acceptable ("purge is a possible follow-up"), not a bug.

## Nits (non-blocking, no code change required)

1. **Env var name mismatch between README and script header**: README.md:239 says `docker exec -i health-db psql "$DATABASE_URL" -f - < ...`, the script header says `$PSQL_DATABASE_URL`. Neither variable exists anywhere in the project (env keys are PSQL_URL/PSQL_PORT/PSQL_USER/PSQL_TABLE/PSQL_PASSWORD) and the image does not ship a psql client (python:3.12-slim image → `NO_PSQL`). So the documented one-liner works only if run against a psql-capable container/host with a connection URI the operator defines ad hoc. Functionally the file itself is correct (psql -U/-d or a URI both fine); recommend unifying on one concrete documented invocation in a follow-up.
2. **psql default (ON_ERROR_STOP unset) exits 0 even when a statement fails and rolls back** (observed with the broken variant). For an ops runbook, recommending `psql -v ON_ERROR_STOP=1 -f` would make failures non-zero in scripts/cron. Minor docs hardening only; the delivered script itself cannot fail its own statements (three plain DELETEs, no params).

## Verdict

passed