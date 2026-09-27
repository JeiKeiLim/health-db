# e2e-1 acceptance and unit gate green with exploratory probes

type: journal
source_job: a6fdffe2-25cc-4087-87f4-13371852f595
job_name: interaction_e2e for f8cf6f19
created: 2026-09-27T14:22:01.878Z

## Findings

- **agent_brain_probes**: {"cli":"health --help exit 0; unknown subcommand exit 2 with actionable choice list; ecgs --limit 0 exit 1 (non-500); ecgs --limit 99999 exit 1 with actionable 'database unavailable' hint (pool resolution precedes limit validation — minor, not a defect); no stack traces leaked","hrn_lane":"bogus-ts event, missing kind, missing source.name each drop + count under notification; garbage list items counted; absent lane = no skips","sleep_rule":"all-zero -> sleep_no_value counted; negative -> skipped (counted via unrecognized/sleep_no_value); string-typed values -> skipped visibly; huge 1e308 stored as-is (no overflow crash); bool True treated as falsy-value -> skipped visibly; missing date/empty point -> unrecognized_shape counted (zero silent drops holds); unicode source stored","unit_gate":"lb 160 -> 72.57 kg; st 11.8 -> 74.93 kg; degF 95.5 -> 35.2778 degC (wrist-temp only, per spec); 'gal' -> skip_reason=unit_unconvertible visible; kJ 418.4 -> 100 kcal; lb 0.0 -> 0.0 kg kept"}
- **deliverables**: {"docker":"29.8.0","parse_everything_acceptance":"24/24 PASSED in ~38-43s (disposable postgres:18 via docker)","python":"3.12.7","uv":"0.8.10"}
- **exploratory**: {"ruff":"clean (src/ tests/)","sibling_acceptance":"parse_everything_migration.py 21/21 PASSED in 2.99s; slice_1+slice_2 acceptance (101 tests incl. perf gate) PASSED in 89.66s","unit_suite":"776/776 PASSED in 168.46s"}
- **job**: e2e-1
- **notes**: °F glyph probe was tester error: real corpus unit string is 'degF' (code + tests pin degF correctly, scoped to apple_sleeping_wrist_temperature per spec §4). Not a defect.
- **outcome**: PASS
- **per_class**: {"TestAntiScenarios":"2/2","TestCensusLock":"1/1","TestEcgLane":"4/4","TestMcpTools":"4/4","TestNotificationLane":"2/2","TestRepairAndRecognition":"2/2","TestSleepInBed":"2/2","TestSleepValueRule":"4/4","TestUnitConversion":"3/3"}
- **report_only**: true
- **run**: 2026-09-17-normalizer-audit
- **scope**: report-only e2e-1: parse_everything acceptance + full unit gate; no code changes; no doctrine edits; no blocking findings
