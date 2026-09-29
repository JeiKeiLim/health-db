"""Unit tests for the sleep_sessions normalizer lane (mcp-analysis-parity job-2).

``_sleep_session_rows`` is a PURE parallel lane over ``data.metrics``: one
``SleepSessionRow`` per recognized aggregated-sleep record, fields verbatim
from the payload, no merge, no nap filtering, no re-derivation. The 5 shapes
come from tests/fixtures/parse_everything/sleep_shape_matrix.json (FROZEN —
the census lock's source of truth); the two real records are the spec §1
live values. The existing sleep_analysis/sleep_in_bed emissions must be
untouched — tests/unit/test_census_lock.py runs unchanged elsewhere.

Behavioral contract per test (observable outcomes, not internals):

- watch_sentinel_night → one row, asleep stored as 0 VERBATIM.
- mixed_stage_night → one row with the real asleep stage.
- autosleep_nap → one distinct row (asleep == in_bed == 0.5833…), never
  merged into a Watch row on the same day.
- inbed_only_row → one row (in_bed 7.4 verbatim).
- no_totalsleep_shape → NO row + skipped['sleep_session'] == 1.
- Spec §1 real records (night 2026-09-28, night + nap 2026-09-17) survive
  ingest exactly, including the asleep: 0 sentinel.
- Earlier-precedence shapes ({qty, date}, {startDate, endDate, qty}) yield
  NO session rows — recognition is identical to the aggregated-sleep branch.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from health_db.normalize import (
    SLEEP_SESSION_SKIP,
    SleepSessionRow,
    _sleep_session_rows,
    metric_rows_detailed,
)

KST = ZoneInfo("Asia/Seoul")

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "parse_everything"


def doc(metrics: list | None = None, workouts: list | None = None) -> dict:
    data: dict = {}
    if metrics is not None:
        data["metrics"] = metrics
    if workouts is not None:
        data["workouts"] = workouts
    return {"data": data}


def metric(name: str, points: list, units: str = "count") -> dict:
    return {"name": name, "units": units, "data": points}


def matrix_points() -> list[tuple[str, dict]]:
    """(id, point) from the FROZEN sleep_shape_matrix.json fixtures."""
    payload = json.loads((FIXTURE_DIR / "sleep_shape_matrix.json").read_text(encoding="utf-8"))
    return [(entry["id"], entry["point"]) for entry in payload["fixtures"]]


def ids_by_start(rows: list[SleepSessionRow]) -> dict[datetime, SleepSessionRow]:
    """Index rows by sleep_start (the natural key's second half)."""
    return {row.sleep_start: row for row in rows}


# ---------------------------------------------------------------------------
# Spec §1 real records (verbatim consumer-spec values)
# ---------------------------------------------------------------------------

# Night 2026-09-27→28 (Watch) — spec §1 verbatim excerpt.
NIGHT_2026_09_28 = {
    "date": "2026-09-28 00:00:00 +0900",
    "source": "Jongkuk의 Apple Watch",
    "sleepStart": "2026-09-27 23:05:35 +0900",
    "sleepEnd": "2026-09-28 05:49:54 +0900",
    "inBedStart": "2026-09-27 23:05:35 +0900",
    "inBedEnd": "2026-09-28 05:49:54 +0900",
    "totalSleep": 6.2614,
    "asleep": 0,
    "inBed": 0,
    "awake": 0.262,
    "core": 3.7234,
    "deep": 1.043,
    "rem": 1.495,
}

# Night 2026-09-16→17 (Watch) — spec §1 verbatim excerpt.
NIGHT_2026_09_17 = {
    "date": "2026-09-17 00:00:00 +0900",
    "source": "Jongkuk의 Apple Watch",
    "sleepStart": "2026-09-16 22:11:12 +0900",
    "sleepEnd": "2026-09-17 04:58:30 +0900",
    "inBedStart": "2026-09-16 22:11:12 +0900",
    "inBedEnd": "2026-09-17 04:58:30 +0900",
    "totalSleep": 6.3746,
    "asleep": 0,
    "inBed": 0,
    "awake": 0.05,
    "core": 3.5891,
    "deep": 1.0473,
    "rem": 1.7382,
}

# AutoSleep nap 2026-09-16 (same record_date day) — spec §1 verbatim excerpt.
AUTOSLEEP_NAP_2026_09_17 = {
    "date": "2026-09-17 00:00:00 +0900",
    "source": "AutoSleep",
    "sleepStart": "2026-09-16 17:35:00 +0900",
    "sleepEnd": "2026-09-16 18:10:00 +0900",
    "inBedStart": "2026-09-16 17:35:00 +0900",
    "inBedEnd": "2026-09-16 18:10:00 +0900",
    "totalSleep": 0.5833333333333334,
    "asleep": 0.5833333333333334,
    "inBed": 0.5833333333333334,
    "awake": 0,
    "core": 0,
    "deep": 0,
    "rem": 0,
}


class TestSpec1RealRecords:
    """The two spec §1 real payload excerpts must survive ingest exactly."""

    def test_night_2026_09_28_full_fields(self):
        rows, skips = _sleep_session_rows(
            doc([metric("sleep_analysis", [NIGHT_2026_09_28], units="hr")])
        )
        assert skips == {}
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, SleepSessionRow)
        assert row.source_name == "Jongkuk의 Apple Watch"
        assert row.record_date == datetime(2026, 9, 28, 0, 0, tzinfo=KST)
        assert row.sleep_start == datetime(2026, 9, 27, 23, 5, 35, tzinfo=KST)
        assert row.sleep_end == datetime(2026, 9, 28, 5, 49, 54, tzinfo=KST)
        assert row.in_bed_start == datetime(2026, 9, 27, 23, 5, 35, tzinfo=KST)
        assert row.in_bed_end == datetime(2026, 9, 28, 5, 49, 54, tzinfo=KST)
        assert row.total_sleep == pytest.approx(6.2614)
        assert row.asleep == 0.0  # sentinel verbatim — never re-derived
        assert row.in_bed == 0.0
        assert row.awake == pytest.approx(0.262)
        assert row.core == pytest.approx(3.7234)
        assert row.deep == pytest.approx(1.043)
        assert row.rem == pytest.approx(1.495)

    def test_night_and_nap_2026_09_17_two_distinct_rows(self):
        rows, skips = _sleep_session_rows(
            doc(
                [
                    metric(
                        "sleep_analysis",
                        [NIGHT_2026_09_17, AUTOSLEEP_NAP_2026_09_17],
                        units="hr",
                    )
                ]
            )
        )
        assert skips == {}
        assert len(rows) == 2, "never merged: distinct (source, sleep_start)"
        by_start = ids_by_start(rows)
        night = by_start[datetime(2026, 9, 16, 22, 11, 12, tzinfo=KST)]
        assert night.source_name == "Jongkuk의 Apple Watch"
        assert night.total_sleep == pytest.approx(6.3746)
        assert night.deep == pytest.approx(1.0473)
        assert night.core == pytest.approx(3.5891)
        assert night.rem == pytest.approx(1.7382)
        assert night.awake == pytest.approx(0.05)
        assert night.asleep == 0.0
        nap = by_start[datetime(2026, 9, 16, 17, 35, 0, tzinfo=KST)]
        assert nap.source_name == "AutoSleep"
        assert nap.total_sleep == pytest.approx(0.5833333333333334)
        assert nap.asleep == pytest.approx(0.5833333333333334)
        assert nap.in_bed == pytest.approx(0.5833333333333334)
        assert nap.sleep_end == datetime(2026, 9, 16, 18, 10, 0, tzinfo=KST)
        assert nap.awake == 0.0


# ---------------------------------------------------------------------------
# The 5 frozen sleep_shape_matrix.json shapes → session row or skip
# ---------------------------------------------------------------------------


class TestMatrixShapes:
    """Each matrix shape produces the expected session row or skip."""

    def test_watch_sentinel_night_row_with_asleep_zero_verbatim(self):
        (shape_id, point) = next(p for p in matrix_points() if p[0] == "watch_sentinel_night")
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert skips == {}
        assert len(rows) == 1
        row = rows[0]
        assert row.source_name == "Apple Watch"
        assert row.total_sleep == pytest.approx(6.4)
        assert row.asleep == 0.0, "asleep: 0 sentinel stored verbatim, never derived"
        assert row.deep == pytest.approx(1.0)
        assert row.sleep_start == datetime(2026, 9, 16, 22, 11, 12, tzinfo=KST)
        assert row.sleep_end == datetime(2026, 9, 17, 4, 58, 30, tzinfo=KST)
        assert shape_id == "watch_sentinel_night"  # guard against fixture drift

    def test_mixed_stage_night_asleep_kept_verbatim(self):
        point = next(p[1] for p in matrix_points() if p[0] == "mixed_stage_night")
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert skips == {}
        row = rows[0]
        assert row.asleep == pytest.approx(1.9), "real 4th stage stored verbatim"
        assert row.total_sleep == pytest.approx(8.2)

    def test_autosleep_nap_row(self):
        point = next(p[1] for p in matrix_points() if p[0] == "autosleep_nap")
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert skips == {}
        row = rows[0]
        assert row.source_name == "AutoSleep"
        assert row.total_sleep == pytest.approx(0.58)
        assert row.asleep == pytest.approx(0.58)
        assert row.in_bed == pytest.approx(0.58)

    def test_inbed_only_row(self):
        point = next(p[1] for p in matrix_points() if p[0] == "inbed_only_row")
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert skips == {}
        row = rows[0]
        assert row.source_name == "watch"
        assert row.in_bed == pytest.approx(7.3)
        assert row.total_sleep == 0.0  # payload truth: totalSleep is 0
        assert row.asleep == 0.0

    def test_no_totalsleep_shape_no_row_and_skipped(self):
        point = next(p[1] for p in matrix_points() if p[0] == "no_totalsleep_shape")
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert rows == []
        assert skips == {SLEEP_SESSION_SKIP: 1}

    def test_all_five_shapes_classify(self):
        """Full matrix walk: 4 rows + 1 visible skip, nothing silent."""
        points = [p for _id, p in matrix_points()]
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", points, units="hr")]))
        assert len(rows) == 4
        assert skips == {SLEEP_SESSION_SKIP: 1}
        # Exactly one row per (source, sleep_start) — no merges.
        keys = {(r.source_name, r.sleep_start) for r in rows}
        assert len(keys) == 4


# ---------------------------------------------------------------------------
# Recognition parity with the aggregated-sleep branch
# ---------------------------------------------------------------------------


class TestRecognitionParity:
    """Earlier-precedence shapes are NOT session records; guard mirrors the
    existing aggregated-sleep dispatch (date + ∩ sleep fields ≠ ∅)."""

    def test_common_qty_date_shape_not_a_session(self):
        point = {"qty": 5, "date": "2026-09-08 10:00:00 +0900", "asleep": 1.0}
        rows, skips = _sleep_session_rows(doc([metric("step_count", [point], units="count")]))
        assert rows == []
        assert skips == {}

    def test_unaggregated_sleep_shape_not_a_session(self):
        point = {
            "startDate": "2026-09-07 23:00:00 +0900",
            "endDate": "2026-09-08 07:00:00 +0900",
            "qty": 7.5,
            "value": "Asleep",
        }
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert rows == []
        assert skips == {}

    def test_plain_point_without_date_or_sleep_fields_ignored(self):
        point = {"systolic": 120, "diastolic": 80}
        rows, skips = _sleep_session_rows(doc([metric("blood_pressure", [point], units="mmHg")]))
        assert rows == []
        assert skips == {}

    def test_point_source_wins_over_group_source(self):
        point = {
            "date": "2026-09-17 00:00:00 +0900",
            "sleepStart": "2026-09-16 22:11:12 +0900",
            "source": "Apple Watch",
        }
        group = {
            "name": "sleep_analysis",
            "units": "hr",
            "source": "iPhone",
            "data": [point],
        }
        rows, _ = _sleep_session_rows(doc([group]))
        assert rows[0].source_name == "Apple Watch"

    def test_group_source_used_when_point_has_none(self):
        group = {
            "name": "sleep_analysis",
            "units": "hr",
            "source": "iPhone",
            "data": [
                {
                    "date": "2026-09-17 00:00:00 +0900",
                    "sleepStart": "2026-09-16 22:11:12 +0900",
                }
            ],
        }
        rows, _ = _sleep_session_rows(doc([group]))
        assert rows[0].source_name == "iPhone"

    def test_missing_source_becomes_unknown(self):
        point = {"date": "2026-09-17 00:00:00 +0900", "sleepStart": "2026-09-16 22:11:12 +0900"}
        rows, _ = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert rows[0].source_name == "unknown"


class TestSessionSkips:
    """Unusable points drop VISIBLY under skipped.sleep_session (spec §1)."""

    def test_missing_sleepstart_skipped_and_counted(self):
        point = {"date": "2026-09-08 00:00:00 +0900", "asleep": 7.0, "core": 3.5}
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert rows == []
        assert skips == {SLEEP_SESSION_SKIP: 1}

    def test_unparseable_sleepstart_skipped_and_counted(self):
        point = {
            "date": "2026-09-08 00:00:00 +0900",
            "sleepStart": "not-a-date",
            "asleep": 7.0,
        }
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert rows == []
        assert skips == {SLEEP_SESSION_SKIP: 1}

    def test_unparseable_record_date_skipped_and_counted(self):
        # record_date is NOT NULL: an unparseable payload `date` cannot store.
        point = {"date": "nope", "sleepStart": "2026-09-16 22:11:12 +0900"}
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert rows == []
        assert skips == {SLEEP_SESSION_SKIP: 1}

    def test_missing_date_not_a_sleep_shape(self):
        # Without `date` the point is NOT a recognized aggregated-sleep
        # shape at all (recognition = date + ∩ sleep fields) — it is
        # unknown_formats territory upstream, not a sleep_session skip.
        point = {"sleepStart": "2026-09-16 22:11:12 +0900", "asleep": 7.0}
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert rows == []
        assert skips == {}

    def test_mixed_points_counted_individually(self):
        good = {"date": "2026-09-17 00:00:00 +0900", "sleepStart": "2026-09-16 22:11:12 +0900"}
        bad = {"date": "2026-09-08 00:00:00 +0900", "asleep": 7.0}
        rows, skips = _sleep_session_rows(
            doc([metric("sleep_analysis", [good, bad, good], units="hr")])
        )
        assert len(rows) == 2
        assert skips == {SLEEP_SESSION_SKIP: 1}

    def test_no_sleep_fields_in_lane_no_skips(self):
        # A normal non-sleep payload yields nothing with no skips.
        rows, skips = _sleep_session_rows(
            doc([metric("step_count", [{"qty": 5, "date": "2026-09-08 10:00:00 +0900"}])])
        )
        assert rows == []
        assert skips == {}

    def test_absent_or_malformed_document_yields_nothing(self):
        assert _sleep_session_rows(None) == ([], {})
        assert _sleep_session_rows({}) == ([], {})
        assert _sleep_session_rows({"data": {}}) == ([], {})
        assert _sleep_session_rows({"data": {"metrics": "nope"}}) == ([], {})


class TestOverflowGuard:
    """Huge-int duration literals drop VISIBLY, never crash the payload.

    JSON ints parse natively (bypassing payloads' parse_float guards), so
    a 10**400 literal reaches _num and float() raises OverflowError. The
    lane treats that point exactly like its sibling lanes do
    (_hrn_sample_rows / _hrn_event_fields): no row, skip counted under
    sleep_session — and good sibling points still make it through.
    """

    def test_huge_int_stage_field_drops_point_without_crash(self):
        point = {
            "date": "2026-09-17 00:00:00 +0900",
            "sleepStart": "2026-09-16 22:11:12 +0900",
            "sleepEnd": "2026-09-17 04:58:30 +0900",
            "totalSleep": 6.3746,
            "deep": 10**400,
        }
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert rows == [], "overflowed stage makes the record unusable verbatim"
        assert skips == {SLEEP_SESSION_SKIP: 1}

    def test_overflow_point_does_not_poison_good_siblings(self):
        good = {
            "date": "2026-09-17 00:00:00 +0900",
            "sleepStart": "2026-09-16 22:11:12 +0900",
            "totalSleep": 6.3746,
        }
        bad = {
            "date": "2026-09-18 00:00:00 +0900",
            "sleepStart": "2026-09-17 22:11:12 +0900",
            "asleep": 10**400,
        }
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [good, bad], units="hr")]))
        assert len(rows) == 1, "good sibling kept despite an overflowed neighbor"
        assert rows[0].sleep_start == datetime(2026, 9, 16, 22, 11, 12, tzinfo=KST)
        assert skips == {SLEEP_SESSION_SKIP: 1}

    def test_overflow_counted_individually_across_points(self):
        good = {"date": "2026-09-17 00:00:00 +0900", "sleepStart": "2026-09-16 22:11:12 +0900"}
        bad_core = {
            "date": "2026-09-18 00:00:00 +0900",
            "sleepStart": "2026-09-17 22:11:12 +0900",
            "core": 10**400,
        }
        bad_rem = {
            "date": "2026-09-19 00:00:00 +0900",
            "sleepStart": "2026-09-18 22:11:12 +0900",
            "rem": 10**400,
        }
        rows, skips = _sleep_session_rows(
            doc([metric("sleep_analysis", [bad_core, good, bad_rem], units="hr")])
        )
        assert len(rows) == 1
        assert skips == {SLEEP_SESSION_SKIP: 2}

    def test_huge_int_in_total_sleep_also_guarded(self):
        # Guard covers all seven duration fields, not just stage fields.
        point = {
            "date": "2026-09-17 00:00:00 +0900",
            "sleepStart": "2026-09-16 22:11:12 +0900",
            "totalSleep": 10**400,
            "deep": 1.1,
        }
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert rows == []
        assert skips == {SLEEP_SESSION_SKIP: 1}

    def test_finite_but_huge_duration_float_is_kept(self):
        # The sibling-lane guard is OverflowError-specific: a finite huge
        # float (1e308, within double range) parses fine and is kept —
        # only unrepresentable magnitudes (huge ints) drop.
        point = {
            "date": "2026-09-17 00:00:00 +0900",
            "sleepStart": "2026-09-16 22:11:12 +0900",
            "deep": 1e308,
        }
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert skips == {}
        assert rows[0].deep == float(1e308), "finite huge float is kept, not dropped"


# ---------------------------------------------------------------------------
# Parallel emission: session lane must NOT disturb existing rows
# ---------------------------------------------------------------------------


class TestParallelEmission:
    """The session lane is purely additive — census lock is the guard."""

    def test_existing_emissions_unchanged_for_matrix_shapes(self):
        # The existing aggregated-sleep branch keeps its exact outcomes when
        # the session lane exists (mirror of test_census_lock assertions at
        # the pure level for the same 5 points).
        points = [p for _id, p in matrix_points()]
        rows, _skips = metric_rows_detailed(doc([metric("sleep_analysis", points, units="hr")]))
        by_name: dict[str, list[float]] = {}
        for row in rows:
            by_name.setdefault(row.name, []).append(row.qty)
        assert sorted(by_name["sleep_analysis"]) == [0.58, 6.4, 7.0, 8.2], (
            "inbed_only_row emits only sleep_in_bed (census lock parity)"
        )
        assert sorted(by_name.get("sleep_in_bed", [])) == [0.58, 7.3]

    def test_session_rows_and_sample_rows_both_emitted(self):
        # One payload, both lanes: the same point yields a sleep_analysis
        # SampleRow AND a SleepSessionRow (spec scenario: parallel emission).
        document = doc([metric("sleep_analysis", [NIGHT_2026_09_28], units="hr")])
        sample_rows, _ = metric_rows_detailed(document)
        session_rows, _ = _sleep_session_rows(document)
        assert [(r.name, r.qty) for r in sample_rows] == [
            ("sleep_analysis", 6.2614)
        ]
        assert len(session_rows) == 1

    def test_frozen_dataclass(self):
        rows, _ = _sleep_session_rows(
            doc([metric("sleep_analysis", [NIGHT_2026_09_28], units="hr")])
        )
        with pytest.raises(Exception):
            rows[0].total_sleep = 9.0  # type: ignore[misc]

    def test_utc_ts_still_buckets_identically(self):
        # A record_date in a non-+0900 offset still parses to the same
        # instant (tz-aware) — KST bucketing happens downstream.
        point = {
            "date": "2026-09-16 15:00:00 +0000",  # == 2026-09-17 00:00 KST
            "sleepStart": "2026-09-16 13:45:00 +0000",
        }
        rows, skips = _sleep_session_rows(doc([metric("sleep_analysis", [point], units="hr")]))
        assert skips == {}
        assert rows[0].record_date == datetime(2026, 9, 16, 15, 0, tzinfo=UTC)
