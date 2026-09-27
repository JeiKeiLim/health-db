"""Census lock (anti-scenario 7) — rule + fixtures move together.

The audited corpora (research/predecessor-inventory.md §7) pin exact sleep
outcomes for run 2026-09-17-normalizer-audit:

- recent 130 daily exports → 128 sleep_analysis rows kept + 2 in-bed splits
- full-history backfill → 2,200 sleep rows kept (1,000 AutoSleep + 1,200 Watch)
  + 300 in-bed splits (2,500 sleep points total)

The committed fixtures are heavily trimmed (5 shapes, not 2,600 rows), so
each shape carries a ``census_weight``: the number of real corpus rows it
stands for. The lock asserts ``sum(weight × outcome) == census`` per lane.

Two regression classes are covered, and BOTH must break the lock:

1. Count/split drift — a rule change that stores a different number of
   sleep_analysis / sleep_in_bed rows (e.g. the in-bed split firing when it
   should not, or a recognized shape silently dropped). The weighted count
   parity above stops this.
2. Sentinel qty regression (anti-scenario 2) — the original defect stored
   qty 0.0 behind UNCHANGED row counts (one sleep_analysis row per night),
   so counts alone cannot see it. Each shape therefore also pins
   ``expected_qty`` per emitted row name; a value-carrying shape whose
   stored sleep_analysis qty is 0.0 (the ``asleep: 0`` sentinel winning
   again) fails the lock even though every count matches.

Drifting the rule or editing a fixture shape (counts or qty) without
re-deriving its weight breaks this test — the two must move together.

No docker, no I/O beyond reading the committed JSON fixtures: pure
normalizer in, outcome table out. Deterministic and fast by construction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from health_db.normalize import SLEEP_IN_BED, metric_rows_detailed

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "parse_everything"

# Audited census numbers (spec §1 universal-rule census; research §7).
# recent: 130 sleep points → 137 kept, 2 in-bed splits.
# backfill: 2,500 sleep points → 2,200 kept, 300 in-bed splits.
RECENT_SLEEP_KEPT, RECENT_IN_BED = 128, 2
BACKFILL_SLEEP_KEPT, BACKFILL_IN_BED = 2200, 300
RECENT_SLEEP_TOTAL, BACKFILL_SLEEP_TOTAL = 130, 2500


def _matrix() -> list[dict]:
    path = FIXTURE_DIR / "sleep_shape_matrix.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    fixtures = payload.get("fixtures")
    assert isinstance(fixtures, list) and fixtures, "fixture matrix must be non-empty"
    return fixtures


def _outcome(point: dict) -> dict[str, dict[str, float]]:
    """Run the sleep value rule over one point → per-row-name outcome.

    Returns ``{row_name: {"count": n, "qty_sum": total}}`` — the qty sum
    (one row per name per point here) is what pins the sentinel rule:
    the ``asleep: 0`` regression stores count 1 with qty 0.0, invisible
    to a count-only lock.
    """
    document = {
        "data": {"metrics": [{"name": "sleep_analysis", "units": "hr", "data": [point]}]}
    }
    rows, _skips = metric_rows_detailed(document)
    outcome: dict[str, dict[str, float]] = {}
    for row in rows:
        entry = outcome.setdefault(row.name, {"count": 0, "qty_sum": 0.0})
        entry["count"] += 1
        entry["qty_sum"] += row.qty
    return outcome


def _weights_by_shape_id(fixtures: list[dict]) -> dict[str, dict[str, int]]:
    """Per fixture id: {lane → census_weight} (explicit per-lane objects)."""
    weights: dict[str, dict[str, int]] = {}
    for entry in fixtures:
        weight = entry["census_weight"]
        assert isinstance(weight, dict) and set(weight) <= {"recent", "backfill"}, (
            f"{entry['id']}: census_weight must be a per-lane object"
        )
        weights[entry["id"]] = weight
    return weights


def _assert_shape_outcomes(fixtures: list[dict]) -> None:
    """Each shape must normalize to the documented outcome (per-row lock).

    The expected table asserts, per emitted row name: the row COUNT and
    the stored QTY. The qty pin is the sentinel guard (anti-scenario 2):
    the ``asleep: 0`` regression keeps every count identical (one
    sleep_analysis row per night) while storing 0.0 — only the qty
    assertion sees it. The in-bed column doubles as the co-emission lock
    (0 = no sleep_in_bed row may co-emit, >0 = the split fires with that
    qty; the census split numbers count points whose ONLY row is
    sleep_in_bed — co-emitted in-bed rows on value-carrying shapes are
    locked here per shape instead).
    """
    expected = {
        "watch_sentinel_night": {"sleep_analysis": (1, 6.4), SLEEP_IN_BED: (0, 0.0)},
        "mixed_stage_night": {"sleep_analysis": (1, 8.2), SLEEP_IN_BED: (0, 0.0)},
        "autosleep_nap": {"sleep_analysis": (1, 0.58), SLEEP_IN_BED: (1, 0.58)},
        "inbed_only_row": {"sleep_analysis": (0, 0.0), SLEEP_IN_BED: (1, 7.3)},
        "no_totalsleep_shape": {"sleep_analysis": (1, 7.0), SLEEP_IN_BED: (0, 0.0)},
    }
    for entry in fixtures:
        got = _outcome(entry["point"])
        want = dict(expected[entry["id"]])
        for row_name, (want_count, want_qty) in want.items():
            entry_outcome = got.get(row_name, {"count": 0, "qty_sum": 0.0})
            assert entry_outcome["count"] == want_count, (
                f"shape {entry['id']} drifted: {row_name} count "
                f"{entry_outcome['count']} != {want_count}"
            )
            assert entry_outcome["qty_sum"] == pytest.approx(want_qty), (
                f"shape {entry['id']} drifted: {row_name} qty "
                f"{entry_outcome['qty_sum']} != {want_qty}"
            )
        extra = set(got) - set(want)
        assert not extra, f"shape {entry['id']}: unexpected rows {extra}"


class TestSentinelQtyLock:
    """Anti-scenario 2 as a standalone property: a value-carrying sleep
    shape must never store qty 0.0 behind an unchanged row count."""

    def test_no_zero_qty_on_value_carrying_shapes(self):
        for entry in _matrix():
            got = _outcome(entry["point"])
            expected_qty = entry["expected_qty"]
            for row_name in ("sleep_analysis", SLEEP_IN_BED):
                if expected_qty.get(row_name, 0) > 0:
                    row_outcome = got.get(row_name, {"count": 0, "qty_sum": 0.0})
                    assert row_outcome["count"] > 0 and row_outcome["qty_sum"] > 0, (
                        f"sentinel regression on {entry['id']}: {row_name} "
                        f"stored {row_outcome} (qty must be {expected_qty[row_name]})"
                    )


class TestCensusLock:
    """Weighted census parity — fixtures × rule must reproduce §7 exactly."""

    def test_shapes_carry_weights_and_expected_outcomes(self):
        fixtures = _matrix()
        shape_ids = [entry["id"] for entry in fixtures]
        assert shape_ids == [
            "watch_sentinel_night",
            "mixed_stage_night",
            "autosleep_nap",
            "inbed_only_row",
            "no_totalsleep_shape",
        ], "fixture matrix shape set/order drifted"
        _assert_shape_outcomes(fixtures)

    def test_weights_sum_to_corpus_totals(self):
        # The weight table must reconstruct the audited corpus sizes:
        # 130 recent + 2,500 backfill sleep points (weights are per-lane).
        fixtures = _matrix()
        weights = _weights_by_shape_id(fixtures)
        recent = sum(w.get("recent", 0) for w in weights.values())
        backfill = sum(w.get("backfill", 0) for w in weights.values())
        assert recent == RECENT_SLEEP_TOTAL, (
            f"recent fixture weights sum to {recent}, census says {RECENT_SLEEP_TOTAL}"
        )
        assert backfill == BACKFILL_SLEEP_TOTAL, (
            f"backfill fixture weights sum to {backfill}, census says {BACKFILL_SLEEP_TOTAL}"
        )

    def test_weighted_rule_outcomes_match_census(self):
        # The lock proper: sum(weight × per-shape outcome) must equal the
        # audited kept/in-bed numbers for BOTH lanes. If the rule changes
        # (or a fixture shape changes) without updating the census, the
        # products no longer sum to §7's numbers.
        fixtures = _matrix()
        weights = _weights_by_shape_id(fixtures)
        recent_sleep = recent_inbed = backfill_sleep = backfill_inbed = 0
        for entry in fixtures:
            got = _outcome(entry["point"])
            sleep = got.get("sleep_analysis", {}).get("count", 0)
            # Census split definition (spec §1): points whose ONLY row is
            # sleep_in_bed — a value row plus a co-emitted in-bed row is
            # one kept sleep point, not an in-bed split.
            inbed = 1 if got and set(got) == {SLEEP_IN_BED} else 0
            w = weights[entry["id"]]
            recent_sleep += w.get("recent", 0) * sleep
            recent_inbed += w.get("recent", 0) * inbed
            backfill_sleep += w.get("backfill", 0) * sleep
            backfill_inbed += w.get("backfill", 0) * inbed
        assert recent_sleep == RECENT_SLEEP_KEPT, (
            f"recent: weighted kept = {recent_sleep}, census says {RECENT_SLEEP_KEPT}"
        )
        assert recent_inbed == RECENT_IN_BED, (
            f"recent: weighted in-bed = {recent_inbed}, census says {RECENT_IN_BED}"
        )
        assert backfill_sleep == BACKFILL_SLEEP_KEPT, (
            f"backfill: weighted kept = {backfill_sleep}, census says {BACKFILL_SLEEP_KEPT}"
        )
        assert backfill_inbed == BACKFILL_IN_BED, (
            f"backfill: weighted in-bed = {backfill_inbed}, census says {BACKFILL_IN_BED}"
        )
