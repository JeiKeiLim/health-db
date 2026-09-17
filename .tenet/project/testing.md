# Project Testing

## Strategy

- **pytest** unit tests + acceptance tests, mirroring the predecessor discipline (96 unit + 36 acceptance there). New code lands with tests in the same change.
- **Fixtures**: real HAE payloads captured during slice 1 (verbatim archive) become the replay fixtures for slice 2's parser/upsert tests — the single most valuable test asset of this project.
- **Local database**: development runs postgres in a local Docker container (not the server instance); tests run against a disposable postgres, not SQLite/mocks, so SQL semantics (ON CONFLICT, partitioning, generated columns, timezones) are actually exercised.
- **ruff** for lint; Python type hints checked in CI-relevant commands.

## Quality Gates

- All tests green (`uv run pytest`), lint clean (`uv run ruff check src/ tests/`)
- Slice acceptance:
  - Slice 1: real HAE app payloads accepted (200, < 2s), archived verbatim, inspectable; quarantine path demonstrated with a malformed payload; auth rejection demonstrated with a bad token
  - Slice 2: replay of captured payloads into PG with correct rollups; full-history backfill ≤ 1 day; hourly delta syncs land within 5 min of the hour; MCP rollup query < 1s for 90-day range; idempotency proven by double-posting (no double counts)

## Verification Gaps (acknowledged)

- Week-long "zero unexplained 4xx/5xx" validation is operational (real phone), not a pytest
- Rollup parity vs predecessor during overlap period is a manual/CLI comparison exercise