# slice-1-config retry #1 — uniform blank normalization + fail-closed secret policy

type: journal
dag_id: slice-1-config
created: 2026-09-08

## Scope

Retry addressing code_critic findings from the prior attempt:
1. `from_env()` normalized whitespace/blank values inconsistently across fields.
2. Dead test helper `environ_of()` at tests/unit/test_config.py:20.

## Changes

- **Uniform policy** applied to all 5 env vars: strip surrounding whitespace; blank-after-strip == absent.
  - `ARCHIVE_ROOT` / `APP_VERSION` / `INGEST_HEADER`: absent → built-in default (blank header now falls back instead of raising).
  - `MAX_PAYLOAD_BYTES`: absent / invalid-int / non-positive → default + `UserWarning` (a malformed limit can never crash ingest startup).
  - `INGEST_SECRET`: unset → `None`; set-but-blank → `""` + `UserWarning` (fail-closed).
- **Fail-closed policy documented** in module docstring: missing or empty `INGEST_SECRET` never disables auth — the auth layer (slice-1-auth) MUST reject every request while the secret is `None` or `""`. Blank means deny-all, never allow-all. Warning never contains the secret value.
- **Dead helper removed**: `environ_of()` deleted; tests use explicit dicts / monkeypatch consistently.

## Acceptance

- `uv run pytest -k config` → **26 passed** (was 19; +7 normalization/warning tests, incl. a `simplefilter("error")` guard asserting no spurious warnings when env vars are fully absent).
- `uv run ruff check src/ tests/` clean; `ruff format` clean; `ConfigError` now fully removed (no references in src or tests).
- No literal secrets (`rg` verified); repr/redaction unchanged and still masking.

## Notes for downstream

- Pre-existing acceptance errors are out of scope: `tests/acceptance/slice_1_ingest.py` errors at fixture setup on `ModuleNotFoundError: health_db.main` (downstream slice-1-api job, not yet implemented); `slice_2_db_mcp.py` requires a PG container. Unit config suite is this job's acceptance surface.
- slice-1-auth implementers: honor the fail-closed contract above; `Settings.ingest_secret` is `None` (unset) or `""` (blank) — both must deny.