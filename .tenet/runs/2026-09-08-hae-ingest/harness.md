# Harness: 2026-09-08-hae-ingest

Derived from spec.md + `.tenet/project/testing.md`.

## Formatting & Linting
- `ruff check src/ tests/` must pass with zero findings (config in `pyproject.toml`; line length 100).
- `ruff format --check src/ tests/` (or consistent formatting per repo convention).
- Python 3.12+; type hints on all public functions; no `Any` in public signatures.

## Testing
- Framework: pytest. Commands: `uv run pytest` (all), `uv run pytest -k unit` etc.
- Integration tests MUST run against a real PostgreSQL container (postgres:18), NOT SQLite, NOT mocks — SQL semantics (ON CONFLICT on partitioned tables, generated columns, timestamptz) must be exercised. Skip-if-no-docker markers with explicit reason; never silent-skip.
- MCP e2e: official SDK `Client` against locally-served streamable-http endpoint.
- Coverage targets: normalizer/units/day-logic ≥ 95% unit; ingest HTTP paths (auth/quarantine/duplicate/oversize) fully branch-covered via TestClient; SQL layer covered by integration tests (not line-counted).
- Fixtures: slice-1 captured real HAE payloads (from `data/archive/`) are the canonical fixtures; synthetic minimal payloads for unit tests. Never commit real health data into the repo (fixtures under `tests/fixtures/` must be synthetic or scrubbed).
- `INTEGRATION_TEST_DB` env provides the test DB URL; tests create/drop their own schema (`test_<random>`), never touch `health_app` prod DB.

## Architecture Rules
- Package layout: `src/health_db/` (service code), `migrations/` (versioned SQL), `tests/`, `data/` (runtime archives — gitignored), single uv package.
- Plain SQL via psycopg3 — no ORM. SQL lives in dedicated modules (`db/`), not string-concatenated in route handlers.
- FastAPI app is thin: routes → services (normalize, archive, ingest) → db. MCP tools call the same query layer as CLI.
- Config from env only (pydantic-settings or stdlib os.environ — implementer's choice, documented); no hardcoded secrets; `.env` gitignored, `.env.example` tracked.
- Archive-first is an iron law: disk archive happens BEFORE any parse/validation outcome; quarantine is part of the archive tree.
- Partition maintenance is idempotent and safe to call concurrently (advisory lock).
- Timezone: store timestamptz UTC; KST day is a generated column with FIXED +9h offset — never compute day with a named timezone in SQL, never use system-local time.

## Code Principles
- stdlib-first, then pinned deps (fastapi, uvicorn, psycopg[binary,pool], mcp>=2,<3, pytest, ruff, httpx test client). Anything else needs a spec-referenced reason.
- JSON errors to clients are actionable; never leak stack traces (500 handlers return generic message + log id).
- Every SQL migration has a paired test asserting applied schema state.
- Idempotency everywhere: re-running ingest, replay, or migration is always safe (latest-wins / no-op).

## Danger Zones (never modify)
- `.tenet/project/**` (doctrine — propose via run journal only)
- `.tenet/runs/2026-09-08-hae-ingest/interview.md` (interview record)
- `.env` (user-owned secrets)
- `data/archive/**` (verbatim payload archive — the replay corpus; never edited/deleted by code)
- predecessor repo (sibling; out of run scope)

## Iron Laws
1. No credential (INGEST_SECRET, PSQL_PASSWORD) may ever appear in logs, error messages, test output, or committed files.
2. Archive-before-anything: a payload is on disk before any processing outcome is decided; DB failure must never lose a payload.
3. Auth checks use constant-time comparison.
4. Never store non-canonical-unit samples; never convert anything except kJ→kcal ÷ 4.184.
5. day_kst semantics never change (fixed UTC+9); rollup boundaries are KST days.
6. Upserts are latest-wins on (metric_id, source_id, ts) — no double counts, ever.
7. Tests must never run against the server production DB (<server-host>); integration tests use disposable containers only.

## Project Doctrine Boundary
Normal implementation jobs must not edit `.tenet/project/**`. Doctrine drift found during a job → note in `.tenet/runs/2026-09-08-hae-ingest/journal/` (via tenet journal or a written note in the journal dir); consolidation happens at run completion.