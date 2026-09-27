# run-complete normalizer-audit all jobs pass deployment handoff ready

type: journal
source_job: e0267826-c85f-4db6-8ca1-f2aa2749b3ae
job_name: Final E2E: full gate + deployment handoff doc
created: 2026-09-27T19:46:53.998Z

## Findings

- **deliverables**: Sleep value rule (totalSleep>asleep>c+d+r>sleep_in_bed>skip, census-locked 128+2 recent / 2200+300 backfill); loosened recognition ({date,asleep}/{date,sleepStart} shapes); iron-law-4 unit conversion (lb/st/degF exact factors, skip-visibly otherwise); skip accounting in PayloadReport + /api/ingest + replay stderr; ECG lane (42 sessions/600000 voltages, UNIX-epoch ts, overflow guards); HRN lane (350 events/1900 samples, INT32-bounded threshold); MCP tools list_ecgs/get_ecg_detail/list_heart_notifications + CLI; migration 0002 additive-only; census-lock tests mutation-verified; README deploy-before-backfill warning; doctrine-drift notes journaled
- **envelope_amendment**: Lane-only payloads accepted (orchestrator-ratified spec amendment, journaled); data_type reconciled via shared _classify_data_type
- **gate**: 781 passed / 0 failed / ruff clean (634 unit + 147 acceptance incl perf 2/2)
- **key_commits**: ["30563cc","4f67aa4","67f9722","25f7a8a","ee620f7","c42565e","2f23a7c","e60c081","c5a1d3f","5084dda","eea09bf","ee2d133","49d26d2","77c89d4","799aabd","04596cf","4ddcd77","60f0da6"]
- **run**: 2026-09-17-normalizer-audit
- **status**: COMPLETE
