# slice-1-archive retry: cold-start scan indexes full corpus (dedup bug)

type: journal
source_job: bba1e3ce-3ae0-41f5-8d27-61e8a9d6f248
job_name: Verbatim archive store (iron law)
created: 2026-09-08T10:09:48.444Z

## Findings

- **acceptance**: uv run pytest -k archive green (24 unit tests; 20 prior + 4 new regression), tests/unit 147 passed, ruff check src/ tests/ clean, ruff format clean. Pre-existing 9 acceptance ERRORs unchanged (ModuleNotFoundError: health_db.main — slice-1-api not yet dispatched; same error set as prior journal).
- **bug**: code_critic finding: _scan_for (archive.py:165-184) marked every file _scanned but only content-indexed files whose stem contained the sha8 token. After a process restart, store() of an already-archived payload saved under a foreign/renamed name (no sha8 stem) wrote a duplicate byte-identical copy — polluting the corpus and violating documented idempotency.
- **fix**: Cold-start scan now content-hashes and indexes EVERY non-quarantine *.json file on first encounter (direct assignment upgrades mark_seen-only sha->None entries to real paths); target-sha match still returns the path. OSError on read → skip (retry next scan). Quarantine files remain excluded (never-accepted content must re-quarantine).
- **regression_tests**: test_restart_store_of_foreign_named_payload_is_duplicate (foreign.json seeded, fresh store, re-post identical bytes → returns existing path, exactly 1 file); test_cold_scan_indexes_all_files_then_dedupes_across_lookups (full-miss lookup must still index scanned corpus); test_cold_scan_skips_quarantine_files (quarantine excluded; re-post stored in dated tree); test_cold_scan_ignores_non_json_files (only .json scanned, no crash).
- **smoke**: Real-process check outside pytest: seeded foreign.json, fresh ArchiveStore, store(same bytes) returned foreign.json with exactly 1 file in tree.
- **verified_kept**: 20 prior archive tests unchanged and green (atomicity, quarantine, KST layout, live-config binding, concurrency).
