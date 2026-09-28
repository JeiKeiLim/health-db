# Harness: 2026-09-28-mcp-analysis-parity

Run-specific quality contract, derived from spec.md + `.tenet/project/testing.md`.

## Formatting & Linting

- `uv run ruff check src/ tests/` — must be clean (project gate).
- Type hints on new public functions (repo convention).

## Testing

- `uv run pytest` (unit) green; `uv run pytest -m requires_pg` (acceptance, disposable postgres:18 container) green.
- Framework: pytest. Fixtures under `tests/fixtures/parse_everything/` — real payload shapes, source names genericized (house `_provenance` discipline, see sleep_shape_matrix.json).
- New-code coverage, every branch at least one test:
  - `_sleep_session_rows` normalizer: each of the 5 matrix shapes; the two demo full records; no-sleepStart skip + count; in-bed-era row; AutoSleep nap distinctness.
  - Pipeline `_upsert_sleep_session_lane`: ON CONFLICT latest-wins; skipped census merge.
  - `list_sleep_sessions`: day-range filter, limit clamp [1,500], empty result, past-end page.
  - Route paging: clamps, 4-page walk, `next_route_offset` None on last page, summary fields unchanged.
  - `latest_activity_day`: present metrics, missing metrics → None fallback behavior.
  - `list_workouts` pagination: boundaries, `next_offset` marker.
  - CLI: `health sleep` round trip; `health routes <id>` new flags.
- Acceptance (`requires_pg`): fixture ingest → `list_sleep_sessions` exact values; double replay idempotency (row counts unchanged); route pages sum 2873; perf gate (tests/acceptance/slice_2_perf.py) non-regression.

## Architecture Rules

- Pure functions stay pure: normalize.py/payloads.py hold no I/O (module docstring contract).
- Plain SQL in `db/helpers.py` + migration files only — no ORM.
- New lane flows through the SAME per-payload pipeline transaction (`ingest_payload`); archive-first preserved; OUT of rollups.
- MCP tools + CLI wrap the shared `queries.py` read layer — never ad-hoc SQL in the tool layer.
- Migration 0003 additive-only CREATE TABLE + UNIQUE + index; never ALTER existing tables; 0001/0002 untouched; single transaction (forward-only, no down path — house convention; failed migration aborts startup, no partial state).
- `day_kst` generated columns reuse the fixed UTC+9 expression family from 0001 — never named zones, never server-local.
- Additive response fields only — existing tool response shapes unchanged beyond documented additions (`skipped` object gains `sleep_session` key; `SKIPPED_KEYS` is the frozen contract, extend it exactly once).

## Code Principles

- Verbatim payload semantics: no re-derivation, no zero-filling, no sentinel "fixing" (data-plane correctness over convenience).
- Every sleep precedence/semantics comment cites its evidence (census numbers / run citations) — repo convention.
- No comments except policy/evidence citations.
- Constants sit next to their siblings (normalize.py KJ_PER_KCAL family; queries.py ECG_HRN_DEFAULT_LIMIT family).

## Danger Zones

- `data/archive/**` — verbatim archive, NEVER modified (iron law 2).
- `data/pending/**` — outage queue; only the documented replay path drains it.
- `migrations/0001_init.sql`, `migrations/0002_parse_everything.sql` — frozen.
- `tests/fixtures/parse_everything/sleep_shape_matrix.json` — FROZEN (census lock depends on it; spec §Test Strategy).
- `src/health_db/archive.py`, `pending.py` — out of scope.
- `.tenet/project/**` — doctrine; normal jobs never edit (drift notes go to run journal).
- Legacy predecessor corpus under the synced `Documents/` folder — read-only reference; never copied into the repo.

## Iron Laws (run-scoped restatement)

1. Archive-first: payload hits disk before processing (unchanged).
2. Idempotency: sha256 dedup + latest-wins upserts; re-posting never changes row counts — new table follows the same contract.
3. Canonical units: unchanged (no new conversions this run; sleep durations stay hours verbatim).
4. KST days: fixed UTC+9 generated columns; sleep_sessions.day_kst MUST bucket identically to existing sleep_analysis rows.
5. Rollups recompute transactionally per affected day — sleep_sessions intentionally OUT of rollups.
6. Verbatim raw_ingest payload + disk archive stay the audit truth; repair never deletes raw_ingest rows.
7. Secrets never in logs/responses/errors.
8. Zero silent drops: every dropped data point produces a `skipped` entry with a reason — the new `sleep_session` key extends this contract; the session lane is the last known gap.

## Project Doctrine Boundary

Normal implementation jobs must not edit `.tenet/project/**`. Proposed doctrine updates (e.g. MCP tool roster 8→9 in architecture.md integration contracts) go to the run journal as drift notes; a doctrine-maintenance job applies them at run end via the sanctioned path only.