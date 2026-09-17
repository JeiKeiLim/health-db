# slice-1-auth — retry #1, all-green (evidence corrected)

**When:** 2026-09-08 09:18 UTC
**Trigger:** code_critic `evidence_mismatch` finding on previous attempt — final report misattributed the 41 pre-existing pytest errors as ruff lint errors.

## What was verified this attempt (re-run, exact numbers)

### `uv run pytest -k auth` — GREEN
`34 passed, 130 deselected` (tests/unit/test_auth.py: 34 tests).

### `uv run pytest tests/acceptance -m slice1` — 0 passed, 0 failed, 15 errors, all at fixture/setup
- **All 15 errors are in `tests/acceptance/slice_1_ingest.py`**, root cause identical for every one: `ModuleNotFoundError: No module named 'health_db.main'` at the `client` fixture (slice_1_ingest.py:117) — `src/health_db/main.py` is the slice-1-api job, not yet implemented.
- Slice 1 expected 0 passing here: acceptance is the definition of done for the *slice*, gated on slice-1-api. Auth itself is covered by the 34 unit tests above.
- Slice-1 e2e job (`slice-1-e2e`) is downstream of slice-1-api.

### Full suite context — 41 pre-existing errors, two disjoint causes (NOT lint)
| File | Errors | Cause | Owner |
|---|---|---|---|
| tests/acceptance/slice_1_ingest.py | 15 | `ModuleNotFoundError: No module named 'health_db.main'` (fixture at slice_1_ingest.py:117) | slice-1-api (pending) |
| tests/acceptance/slice_2_db_mcp.py | 26 | missing fixtures: `pg` (13), `mcp` (10), `api` (3) — DB/MCP fixtures not yet implemented | slice-2 (out of current scope) |

Plus the unimplemented-module contribution inside slice_1: stats/api modules don't exist yet (`health_db.stats`, `health_db.archive` likewise absent — their tests are among the 15).

- `uv run pytest` overall: **123 passed** (unit: auth 34 + config 26 + payloads 63), 41 errors (breakdown above), 0 failures.
- **`uv run ruff check src/ tests/` → "All checks passed!"** — ruff was never failing; the previous report was wrong to lump errors into lint.

## Kept verified work (unchanged this attempt)
- Constant-time compare over SHA-256 digests (`hmac.compare_digest` of digests — length-safe, non-ASCII-safe).
- Fail-closed on unset/blank secret (`auth_not_configured` → deny all).
- Generic 401 body, single shape across all failure modes (`UNAUTHORIZED_DETAIL`).
- Frozen `AuditEntry` (ts/ip/ua/reason only); bounded thread-safe `AuditLog`.
- Router-level `Depends(require_token)` wiring proven on POST-style probe route.
- 34 auth tests green; ruff clean.

## Evidence-breakdown lesson (for final-report discipline)
Error categories must be counted and attributed separately: (a) pytest collection/setup errors by file and root cause, (b) ruff findings, (c) failures. Never collapse (a) into (b).