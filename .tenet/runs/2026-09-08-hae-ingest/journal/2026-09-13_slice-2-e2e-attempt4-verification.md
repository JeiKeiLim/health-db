# slice-2-e2e — attempt 4 verification (test critic)

Commit under test: `b1d7d9d` · Mode: report-only, verify-only (no src/tests edits).

## Environment notes (this cloud-synced workspace)

- `.venv` `.pth` files (`health_db.pth`, `_virtualenv.pth`) carry the macOS
  "hidden" xattr (private cloud storage sync churn) — CPython `site.py` prints
  `Skipping hidden .pth file` and the editable install is not processed, so
  `import health_db` fails in fresh shells.
- **Workaround (no repo change)**: prefix pytest/ruff invocations with
  `PYTHONPATH=src`. All suites pass with it. Future runs on this machine
  should use `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -B -m pytest ...`
  (the job-prompt recipe without PYTHONPATH reproduces ModuleNotFoundError
  in schema tests).
- Stale `uv run`/pytest processes killed first per job rules; postgres:18
  container `health-db-postgres-1` up on host port 55432 (verified via `docker ps`).

## Independent verification results (this attempt)

| Suite | Command | Result |
|---|---|---|
| Acceptance | `pytest tests/acceptance -m "slice1 or slice2"` | **90/90 passed** (90 collected, all dots, no skips) |
| Unit | `pytest tests -m "not acceptance"` | **517/517 passed** (517 collected) |
| Lint | `ruff check src/ tests/` | **All checks passed** (exit 0) |
| Perf (isolated re-run) | `pytest tests/acceptance/slice_2_perf.py -v` | **2/2 passed**; 91-day range, budget 1.0s asserted on best-of-5 real query path |

Matches the worker's claimed counts exactly (90/90, 517/517).

## Sufficiency spot-checks (test critic)

- MCP e2e: real official SDK `Client` over `streamable_http_client` with Bearer
  header (`tests/acceptance/conftest.py:557-582`); unauthenticated client without
  Authorization header exercises 401 path (`conftest.py:615-619`); resources read
  via `health://metrics` (`slice_2_db_mcp.py:240`); unknown-metric error is
  actionable (`"list_metrics" in err`, `slice_2_db_mcp.py:279`).
- DB outage: 503 + payload archived + pending queue (`slice_2_db_mcp.py:183-187`),
  replay-after-outage drains queue once, second replay drains nothing, outcome
  `replayed`, rollups recomputed (`slice_2_db_mcp.py:189-210`) — anti-scenario 2/5 covered.
- Replay idempotency: sha256 dedup via raw_ingest, `outcome == "replayed"`
  (`slice_2_db_mcp.py:297-303`) — success criterion 2 covered.
- Schema: latest-wins ON CONFLICT on partitioned raw_samples
  (`slice_2_schema.py:13,331`), ensure_partitions idempotent second call is a
  no-op (`slice_2_schema.py:391`), FK/cascade/HR dedupe tests present.
- Perf test asserts a real budget (1.0s) on the real queries path with correct
  shape/sum verification — not a vacuous timing assertion.
- No silent skips in either run (zero `s` in output; docker was up so
  requires_pg tests executed).

Verdict: worker output consistent with independent evidence; tests sufficient
for this job's scope (slice-2 full acceptance + perf verification).