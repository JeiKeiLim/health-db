# slice-1-quarantine complete: failure semantics module + 23 tests

type: journal
source_job: 2fa9f9a3-577b-4dd7-a41e-780a1b8876d1
job_name: Malformed/oversize failure semantics
created: 2026-09-08T11:16:03.725Z

## Findings

- **acceptance**: uv run pytest tests/unit/test_failures.py → 23 passed; uv run pytest tests/unit → 202 passed; uv run ruff check src/ tests/ → clean; ruff format --check on both files clean. uv run pytest -k failures → 23 passed + 6 pre-existing collection errors (tests/acceptance/slice_1_ingest.py needs health_db.main, the still-pending slice-1-api job; verified pre-existing via git stash).
- **constraints_respected**: No DB import (grep-able slice-1 constraint); secrets never in responses/notes (iron law 1); quarantine lane stays outside the dedup index so re-posted bad bytes are never treated as stored duplicates; dated tree receives nothing on any failure path.
- **design_choices**: 1) Response contract: 400 {error:'malformed', quarantine:<archive-relative id>, reason:<parser message>}; id is posix-relative (quarantine/HHMMSS-sha8.json) so operators can locate the file. 2) Error note written as sibling <stem>.error.txt (reason text only, no body bytes/secrets — iron law 1), best-effort: note-write failure does not fail the quarantine. 3) Iron-law ordering enforced: quarantine write happens BEFORE the 400 response is built; a failed write propagates (never a 400 claiming a quarantine id for a payload not on disk). 4) check_size reads config.MAX_PAYLOAD_BYTES live at call time (test-monkeypatchable); exactly-at-cap passes (strictly-greater fails). 5) Oversize response carries error/limit_bytes/received_bytes; spec's Retry-After:300 belongs to the 503 DB-unavailable path, NOT 413 (spec failure table). 6) Fallback store: module-cached ArchiveStore singleton for direct/scripted use, bound to live config.ARCHIVE_ROOT; slice-1-api passes its pipeline store explicitly. 7) Same-second re-post of identical bad bytes shares one quarantine file (store content-disambiguation) — documented, tested, nothing lost either way.
- **http_413_note**: starlette deprecated HTTP_413_REQUEST_ENTITY_TOO_LARGE in favor of HTTP_413_CONTENT_TOO_LARGE; failures.py uses a plain int 413 (HTTP_413 = 413) to avoid alias churn.
- **job**: slice-1-quarantine
- **lessons**: fastapi re-exports JSONResponse from fastapi.responses only in this version (fastapi 0.141.1) — import from fastapi.responses. Unit tests calling handle_malformed must pass an explicit store bound to tmp (autouse fixture resets config.ARCHIVE_ROOT + the _FALLBACK_STORE cache) or the fallback store writes into the repo's real data/archive tree (caught and cleaned: gitignored data/ dir, no repo damage).
- **smoke_check**: Minimal FastAPI app wired like slice-1-api (check_size → 413 path; handle_malformed → 400 path) via TestClient: broken JSON → 400 {error:malformed, quarantine id, reason}; {'nope':1} → 400; 2KiB vs 1KiB cap → 413 {payload_too_large}; 2 quarantined files byte-identical; dated tree empty; no stray writes outside tmp root.
- **test_count**: 23
