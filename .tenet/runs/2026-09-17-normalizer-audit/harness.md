# Harness: 2026-09-17-normalizer-audit

Run-specific quality contract, derived from spec + `.tenet/project/testing.md`.

## Formatting & Linting

- `uv run ruff check src/ tests/` — must be clean (project gate).
- Type hints on new public functions (repo convention; dataclass `slots` style as in normalize.py).

## Testing

- `uv run pytest` (unit) green; `uv run pytest -m requires_pg` (acceptance, disposable postgres:18 container) green.
- Framework: pytest, fixtures under `tests/fixtures/` (sanitized real payloads — PII scrubbed, same discipline as the slice-1 corpus sanitization in commit b97a3bb).
- New-code coverage: every new branch of the sleep rule, the in-bed split, the loosened recognition, both new lanes, and the conversion path must have at least one unit test each; acceptance must cover: ingest → query round-trip per new table, idempotent re-post, force-replay re-normalization, perf non-regression.
- Fixture census lock: a unit test asserts the exact census numbers from the research (137 kept + 1 in-bed from the 130-daily sample subset committed as fixture; 2,200 + 520 from the backfill sleep fixture) — drift here means the rule regressed.

## Architecture Rules

- Pure functions stay pure: normalize.py/payloads.py hold no I/O (module docstring contract, lines 1-54).
- Plain SQL in `db/helpers.py` + migration files only — no ORM, SQL stays in `db/` (helpers.py:1-6 contract).
- New ingest lanes flow through the SAME pipeline transaction shape (`ingest_payload`, pipeline.py:203): one transaction per payload, all-or-nothing, archive-first preserved.
- MCP tools go through the shared `queries.py` read layer (mcp_server + cli both wrap it — never ad-hoc SQL in the tool layer).
- Migration 0002 is additive-only CREATE TABLE; never ALTER an existing table; existing migrations (0001) untouched.

## Code Principles

- Data-plane change: correctness over convenience. Every precedence rule in the sleep branch must cite its evidence (census numbers) in a comment.
- No comments except policy/evidence citations (repo convention: dense policy docstrings, sparse inline comments).
- Constants for conversion factors sit next to KJ_PER_KCAL (normalize.py:59).

## Danger Zones

- `data/archive/**` — verbatim archive, NEVER modified by code or agent (iron law 2).
- `data/pending/**` — outage queue; only the documented replay path drains it.
- `migrations/0001_init.sql` — frozen; changes only via a NEW migration.
- `.tenet/project/**` — doctrine; normal jobs never edit (drift notes go to run journal).
- `src/health_db/archive.py`, `pending.py` — archive/queue mechanics out of scope this run.
- `.hae` corpus under the synced `Documents/` folder — read-only reference material; never copied into the repo.

## Iron Laws (run-scoped restatement + amendment)

1. Archive-first: every authorized payload hits disk before processing (unchanged).
2. Idempotency: sha256 dedup + latest-wins upserts; re-posting never changes row counts (unchanged; new tables follow the same contract).
3. **Amended (user-approved this run)**: canonical-unit policy — non-canonical units with an exact factor are CONVERTED (lb→kg ÷2.20462, st→kg ÷0.157473, °F→°C, kJ→kcal ÷4.184); unconvertible units are skipped but VISIBLE in the response `skipped` accounting. Never store a non-canonical unit next to canonical rows.
4. KST days: fixed UTC+9 generated columns; never named zones, never server-local (new tables reuse the same generated expression).
5. Rollups recompute transactionally per affected day (new lanes intentionally OUT of rollups).
6. Verbatim raw_ingest payload + disk archive stay the audit truth; repair never deletes raw_ingest rows.
7. Iron law 1 (secrets): no secret ever in logs/responses/errors — unchanged.

## Project Doctrine Boundary

Normal implementation jobs must not edit `.tenet/project/**`. The iron-law-4 amendment is recorded as a doctrine-drift note in the run journal; a doctrine-maintenance job applies it to `.tenet/project/architecture.md`/`overview.md` only via the sanctioned path at run end.