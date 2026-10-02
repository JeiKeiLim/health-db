# doctrine drift: spec envelope lane-only ratification

type: journal
source_job: 909384a9-f0d6-465d-866d-69a8b7c5dbc5
job_name: Pipeline wiring: lane upserts in one transaction
created: 2026-09-19T13:28:55.264Z

## Findings

- **affected_tests**: ["tests/unit/test_payloads.py lane-only acceptance tests","tests/acceptance/parse_everything.py TestEcgLane/TestNotificationLane"]
- **current_claim**: Spec §Envelope note: payloads carrying ONLY data.ecg/data.heartRateNotifications (no metrics/workouts) are rejected as an empty v2 envelope → 400 + quarantine (payloads.py envelope rule 'stays as-is').
- **doctrine_file**: .tenet/runs/2026-09-17-normalizer-audit/spec.md
- **observed_reality**: commit ee2d133 (job-7) loosened the envelope in payloads.py (_LANE_KEYS/_has_lane_data): lane-only payloads with valid lane data are ACCEPTED with 200 and their rows stored (ecgs/heart_rate_notifications + children); empty-list/non-list lane values are still rejected as an empty envelope. This was required by the run's own pre-written acceptance fixtures (tests/acceptance/parse_everything.py TestEcgLane/TestNotificationLane post doc_lanes() payloads through the real /api/ingest route and assert 200 + stored rows), and it is the only reading that satisfies the user's 'parse every data point' directive (zero silent drops) — a 400/quarantine path for a payload carrying storable rows would re-introduce the exact defect class the run exists to kill.
- **proposed_change**: Treat spec §Envelope lane-only line as superseded (orchestrator-ratified spec amendment): lane-only payloads with valid lane data are accepted with 200; empty-list/non-list lane values still rejected. The spec §Envelope note's 'the envelope rule stays as-is' claim should be read as amended; no doctrine file edit this run — normal jobs do not write .tenet/project/**, the doctrine-maintenance path applies it at run end.
- **run**: 2026-09-17-normalizer-audit
- **why_it_happened**: The spec's envelope note reasoned from 'no real corpus file is lane-only (0/212 verified)' and optimized for not weakening the empty-envelope guard, but it overlooked that the acceptance harness itself constructs trimmed lane-only fixtures to isolate lane behavior — acceptance-first doctrine wins over the spec's incidental envelope wording.
