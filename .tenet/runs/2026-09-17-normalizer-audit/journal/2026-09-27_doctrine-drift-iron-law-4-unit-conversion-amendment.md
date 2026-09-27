# doctrine drift: iron-law-4 unit conversion amendment

type: journal
source_job: job-9 (fixtures, census lock, docs + doctrine note)
run: 2026-09-17-normalizer-audit
created: 2026-09-27T15:04:56.434Z

## Findings

- **current_claim**: non-canonical units are skipped
- **doctrine_file**: project/design.md
- **observed_reality**: exact-factor conversion implemented (lb/st/degF) and shipped with census-locked tests
- **proposed_change**: amend iron-law-4 to convert-with-exact-factor-else-skip-visibly

### doctrine-drift: project/design.md

- doctrine_file: project/design.md
- current_claim: non-canonical units are skipped (never stored next to canonical rows; kJ→kcal was the only conversion).
- observed_reality: run 2026-09-17-normalizer-audit (user-approved iron-law-4 amendment, spec §4) implements exact-factor conversion in `src/health_db/normalize.py` — lb→kg ÷ 2.20462, st→kg ÷ 0.157473, °F→°C (°F−32)×5/9 (wrist temperature), kJ→kcal ÷ 4.184 (pre-existing). Unconvertible units are still skipped but VISIBLE: counted in the ingest response `skipped.unit_unconvertible` (zero-silent-drops contract). Locked by tests: unit matrix in tests/unit/test_normalize.py (160 lb → 72.57 kg; 11.8 st → 74.93 kg; 95.5 °F → 35.2777 °C; fabricated 'gal' → visible skip count) and the census lock in tests/unit/test_census_lock.py; README iron-law-4 wording amended in commit 04596cf.
- proposed_change: amend iron-law-4 in `.tenet/project/design.md` (and the matching wording in project/architecture.md / project/overview.md if it repeats the old rule) to: "non-canonical units are CONVERTED with an exact factor when one exists (lb→kg ÷2.20462, st→kg ÷0.157473, °F→°C (°F−32)×5/9, kJ→kcal ÷4.184); unconvertible units are skipped VISIBLY (counted in the ingest response `skipped.unit_unconvertible`), never stored next to canonical rows." Apply via the sanctioned doctrine-maintenance path at run end; normal jobs must not edit .tenet/project/** directly.
