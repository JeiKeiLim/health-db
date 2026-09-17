# slice-1-archive complete: verbatim atomic archive store

type: journal
source_job: bba1e3ce-3ae0-41f5-8d27-61e8a9d6f248
job_name: Verbatim archive store (iron law)
created: 2026-09-08T09:54:59.201Z

## Findings

- **acceptance**: uv run pytest -k archive green (22 passed); ruff check src/ tests/ clean; ruff format clean; pre-existing 9 acceptance errors unchanged (module health_db.main absent — slice-1-api not yet implemented, verified identical error set via stash diff)
- **archive_root_binding**: root=None follows config.ARCHIVE_ROOT live (acceptance client monkeypatch contract); explicit root param for unit tests
- **atomicity**: mkstemp in same dir + fsync(file) + os.replace + best-effort dir fsync; BaseException path unlinks temp — no partial file ever at archive path (tested with injected OSErrors)
- **commit**: d01dfd4
- **delivered**: src/health_db/archive.py + tests/unit/test_archive.py
- **design_choices**: KST (fixed UTC+9, no DST) filenames/day-dirs mirroring day_kst semantics — never system-local; quarantine lane never dedupes against stored index (quarantined content was never accepted, re-post re-quarantines and a later valid store works); find_by_sha256 does incremental disk scan (restart resilience) honoring only sha8-prefixed stems and skipping quarantine; _now_kst module-level seam for tests; same-second name collisions disambiguated via content check then -N suffix
- **interface**: ArchiveStore per decomposition contract: store(body, *, quarantine=False) -> Path, sha256_of, seen, mark_seen, find_by_sha256; store is idempotent on sha256 (duplicate returns existing path, no second file)
- **iron_law_2**: store() writes byte-identical payload before returning; module has no parse/auth knowledge — API layer orders auth→archive→parse (untrusted 401 bodies never reach the store)
