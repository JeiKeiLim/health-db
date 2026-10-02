# slice-1-api retry #2: test-critic fixes — automation-header wiring tests + 8-key response contract

## Findings

- **critic**: test_critic FAIL from retry #1 — (a) no behavioral HTTP test for the HAE automation-header → `stats.last_automation` wiring, (b) stored-response shape asserted 6/8 keys instead of all 8.
- **fix (a)**: `tests/acceptance/slice_1_ingest.py::TestAutomationHeadersToStats` — 5 behavioral HTTP tests over POST /api/ingest → GET /api/ingest/stats:
  - automation-name/id/session-id headers → `last_automation` contains `automation-name=metrics-all`, the automation-id, and the session-id
  - second labeled automation overwrites (most recent wins)
  - missing headers on a later payload leave `last_automation` **unchanged** (kept, not cleared)
  - never-labeled payloads keep `last_automation` **None** while stored/received still count
  - partial headers label only present ones in fixed order (`"automation-name=workouts; session-id=s-123"` exact match)
- **fix (b)**: `assert_stored_shape()` helper asserts the exact 8-key contract (`outcome, data_type, metrics, samples, workouts, unknown_formats, archive_path, sha256` — no missing, no extra keys) plus type/value checks (data_type enum, non-negative ints, unknown_formats entries are `{name, units, reason}`, sha256 is a 64-hex string, archive_path exists on disk). Applied to all three stored-outcome acceptance tests (metrics, workouts, unknown-metric).
- **implementation unchanged**: main.py / routes.py / stats.py verified correct by the code critic in retry #1 — this retry only adds/tightens tests.
- **acceptance**: `uv run pytest tests/acceptance -m slice1` → **20 passed** (was 15; +5 automation-header tests); `uv run pytest -k stats` → **36 passed**; `uv run pytest` → **222 passed** (+26 pre-existing slice-2 collection errors: mcp/psycopg not yet dependencies, out of slice-1 scope); `uv run ruff check src/ tests/` → clean; `ruff format --check` clean.
- **live smoke**: uvicorn on :8973 — health ok; POST with automation headers → 200 stored with all 8 keys; stats showed `"last_automation": "automation-name=metrics-all; automation-id=aid-1; session-id=sid-9"`; bad token → 401; payload archived verbatim under `archive/2026/09/08/`.
- **files**: ["tests/acceptance/slice_1_ingest.py"]
- **job**: slice-1-api (retry #2, test-critic remediation)