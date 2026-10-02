# fix-workout-normalizer complete — all critics pass

type: journal
source_job: 34b9c6a3-f427-4bc8-8e6a-f4af1e13758e
job_name: fix-workout-normalizer
created: 2026-09-14T02:31:32.645Z

## Findings

- **commits**: ["dc4b065 — real HAE v2 workout shapes ({qty,units} dicts, HR Avg, list stepCount/walkingAndRunningDistance, kcal energies) + Jsonb wrap + force/repair replay + workout_v2_sample fixture + coverage","a7a0a48 — deterministic rollup aggregates (sum/avg ORDER BY source_id, ts) + metric-bearing repair idempotency guards","57621d8 — sanitized fixture PII (synthetic 2023-01-15/12.34/56.78) + sql-contract determinism guard with proven teeth"]
- **critics**: FINAL ROUND ALL PASS: code, test (unit micro + teeth micro), interaction_e2e, security, data_integrity
- **evidence**: {"acceptance":"targeted repair/workout subset 11 passed; unit 85 passed (test_normalize), full unit 452 passed clean-env","teeth_proof":"revert sum(qty ORDER BY source_id, ts)->sum(qty): guard FAILS; restore: PASSES; helpers.py byte-identical after","verified_live":"27/27 real workouts repaired (active_energy_kcal, distance, HR, steps), workout_heart_rate 1->735 rows, raw_samples 60000 / daily_rollups 400 / routes 19000 / raw_ingest 20 all unchanged, consecutive force-replays byte-identical"}
- **lesson_1**: HAE v2 real payloads differ from official docs: workout scalars are {qty,units} dicts, HR points use Avg, stepCount/walkingAndRunningDistance are point lists, activeEnergyBurned is the aggregate key. Fixtures must mirror REAL shapes or tests pass while production data silently NULLs.
- **lesson_2**: A behavioral snapshot test cannot detect a plan-stable ORDER BY regression; the honest guard is an SQL-contract assertion on the imported production constant, with revert-fail proof.
- **lesson_3**: cloud-synced workspace: venv .pth hidden-flag recurrences break imports (chflags -R nohidden .venv), and reviewer output truncation is systemic — scope critics tightly and require early rubric emission.
- **verdict**: fix-workout-normalizer COMPLETE — all critics pass
- **workflow_note**: dev job retries: 4 total; 2 remediation rounds (commit discipline + guard teeth); critics recovered via scope splits after repeated truncations
