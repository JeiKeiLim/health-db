# census lock pins qty against sentinel regression

type: journal
source_job: job-9 (eval retry #2 — code_critic test_bug fix)
run: 2026-09-17-normalizer-audit
created: 2026-09-28T01:10:00+09:00

## Findings

- **critic finding (test_bug)**: the census lock was count-based only — `_outcome` counted rows by name and never asserted qty. The asleep:0 sentinel regression (storing qty 0.0) still emits one sleep_analysis row per night, so all three census tests passed unchanged under the exact regression the docstring claimed to catch.
- **fix**: `tests/unit/test_census_lock.py` — `_outcome` now returns per-row-name `{count, qty_sum}`; `_assert_shape_outcomes` pins BOTH count and qty per emitted row name (expected table is now `(count, qty)` tuples, e.g. watch_sentinel_night sleep_analysis = (1, 6.4)); unexpected row names fail. New `TestSentinelQtyLock.test_no_zero_qty_on_value_carrying_shapes` asserts, for every shape whose fixture-declared `expected_qty` > 0, that the stored row exists with qty > 0 — the sentinel property as a standalone lock.
- **fixture**: `tests/fixtures/parse_everything/sleep_shape_matrix.json` gained `expected_qty` per shape ({sleep_analysis, sleep_in_bed}); census_note documents why counts alone cannot see the sentinel regression.
- **docstring**: now documents both regression classes — count/split drift (weighted parity) and sentinel qty regression (per-shape qty pin) — instead of claiming count parity catches the sentinel.
- **gate**: `uv run pytest tests/unit -q` → 634 passed (was 633; +1 new sentinel test); `uv run pytest tests/acceptance/parse_everything.py` → 25 passed; `uv run ruff check src/ tests/` clean.
- **commit**: 33dc371 (prior landings 799aabd / 04596cf / 4ddcd77 kept).
- **prose drift (e2e critic)**: journal doctrine-drift file corrected — 74.93 → 74.9334807 and 72.57 → 72.57. The code value (÷ 0.157473 / ÷ 2.20462) is the factor-true one (see 2026-09-17_job-3-unit-conversion-complete.md); the old literals came from the internally inconsistent task prompt. tests/acceptance/parse_everything.py:328 literal also aligned to 72.57 (abs=1e-4 tolerance was unaffected, now exact).