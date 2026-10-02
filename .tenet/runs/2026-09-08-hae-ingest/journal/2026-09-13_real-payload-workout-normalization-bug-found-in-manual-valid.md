# real-payload workout normalization bug found in manual validation

type: journal
source_job: 9bb0cc11-33ae-4314-bb71-15b6e49156c5
job_name: Integration: slice 2 full acceptance + perf
created: 2026-09-13T22:31:36.688Z

## Findings

- **confidence**: implemented-not-tested
- **evidence**: normalizer reproduced: workout_rows() on real archived payload export-a.json yields active_energy_kcal=None, total=None, distance=None, avg_hr=None, max_hr=None, steps=None, hr_rows=0 (3145 route rows OK). DB confirms: workouts table all-scalar NULL for real workouts; workout_heart_rate has 1 row total (fixture-only) vs 19,000 routes.
- **impact**: Real HAE v2 workout payloads persist scalar fields as NULL and no per-minute HR rows; affects workouts table, get_workout_detail MCP tool, health workouts CLI, any workout analytics.
- **note_1**: root cause: normalizer expects simplified fixture shapes (activeEnergy scalar float, heartRateData [{date,bpm}], walkingRunningDistance scalar) but real HAE v2 uses {qty,units} dicts (activeEnergyBurned, distance, avgHeartRate, maxHeartRate, totalEnergy, temperature, humidity, intensity, speed), heartRateData entries {date,Min,Avg,Max}, stepCount/walkingAndRunningDistance as point lists, activeEnergy/basalEnergy as point lists
- **note_2**: the acceptance suite did not catch this because all workout fixtures use the simplified shape; slice-1 corpus replay was the first real-payload write path exercised
- **note_3**: sleep_analysis: real samples carry asleep=0 with core+deep+rem>0; spec asleep-preference yields rollup 0 (parity with predecessor TASK-005 behavior, inherited not a porting bug) — flagged separately for design review
- **recommendation**: add real-corpus workout fixture (sanitized) to acceptance and fix workout_rows key/shape handling
- **subject**: manual validation finding (post-run)
