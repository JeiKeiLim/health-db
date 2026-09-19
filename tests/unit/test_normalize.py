"""Unit tests for health_db.normalize — pure HAE v2 normalization.

Covers decomposition job slice-2-normalize acceptance: every documented
data-point shape (plain qty, heart_rate Avg, blood-pressure split, sleep
aggregated totalSleep-precedence, sleep unaggregated), the unit policy
(canonical match, kJ→kcal ÷ 4.184, non-canonical skip, pass-through),
KST day computation (fixed +9h, midnight boundary), and workout
normalization (kJ→kcal energies, heartRateData, route rows).

Run 2026-09-17-normalizer-audit additions: the sleep value rule matrix
(Watch asleep:0 sentinel → totalSleep, mixed-stage nights, AutoSleep naps,
in-bed split to sleep_in_bed, loosened no-totalSleep recognition, all-zero
skip accounting), fixture-locked to the census in
tests/acceptance/parse_everything.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from health_db.normalize import (
    BLOOD_PRESSURE_DIASTOLIC,
    BLOOD_PRESSURE_SYSTOLIC,
    CUMULATIVE_UNITS,
    ECG_SKIP,
    HRN_KIND_HR,
    HRN_KIND_HRV,
    KJ_PER_KCAL,
    LB_PER_KG,
    NOTIFICATION_SKIP,
    SLEEP_IN_BED,
    SLEEP_NO_VALUE,
    ST_PER_KG,
    UNIT_UNCONVERTIBLE,
    EcgRow,
    EcgVoltageRow,
    HrnRow,
    HrnSampleRow,
    SampleRow,
    WorkoutRow,
    _ecg_rows,
    _hrn_rows,
    _qty_for_unit,
    _qty_of,
    _sum_qty_of,
    canonical_unit_of,
    day_kst,
    display_name,
    is_cumulative_unit,
    metric_rows,
    metric_rows_detailed,
    workout_rows,
)

KST = ZoneInfo("Asia/Seoul")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def doc(metrics: list | None = None, workouts: list | None = None) -> dict:
    data: dict = {}
    if metrics is not None:
        data["metrics"] = metrics
    if workouts is not None:
        data["workouts"] = workouts
    return {"data": data}


def metric(name: str, points: list, units: str = "count") -> dict:
    return {"name": name, "units": units, "data": points}


def sample_rows(rows: list[SampleRow]) -> list[tuple[str, str, float, str]]:
    return [(r.name, r.source, r.qty, r.unit) for r in rows]


# ---------------------------------------------------------------------------
# day_kst — fixed UTC+9 (iron law 5)
# ---------------------------------------------------------------------------


class TestDayKst:
    def test_evening_kst_same_day(self):
        ts = datetime(2026, 9, 8, 22, 59, tzinfo=KST)
        assert day_kst(ts).isoformat() == "2026-09-08"

    def test_one_minute_before_and_after_kst_midnight(self):
        # 23:59 KST is same day; 00:01 KST next day rolls over (fixed +9h).
        assert day_kst(datetime(2026, 9, 8, 23, 59, tzinfo=KST)).isoformat() == "2026-09-08"
        assert day_kst(datetime(2026, 9, 9, 0, 1, tzinfo=KST)).isoformat() == "2026-09-09"

    def test_utc_2359_kst_boundary(self):
        # 14:59 UTC == 23:59 KST same day
        assert day_kst(datetime(2026, 9, 8, 14, 59, tzinfo=UTC)).isoformat() == "2026-09-08"
        # 15:01 UTC == 00:01 KST next day
        assert day_kst(datetime(2026, 9, 8, 15, 1, tzinfo=UTC)).isoformat() == "2026-09-09"

    def test_kst_has_no_dst(self):
        # Fixed +9h: identical results in January (no DST shift either way)
        jan = day_kst(datetime(2026, 1, 15, 15, 1, tzinfo=UTC))
        assert jan.isoformat() == "2026-01-16"


# ---------------------------------------------------------------------------
# Unit policy constants
# ---------------------------------------------------------------------------


class TestUnitPolicy:
    def test_canonical_map_parity_with_predecessor_map(self):
        assert canonical_unit_of("weight_body_mass") == "kg"
        assert canonical_unit_of("body_fat_percentage") == "%"
        assert canonical_unit_of("height") == "m"
        assert canonical_unit_of("walking_running_distance") == "km"
        assert canonical_unit_of("walking_step_length") == "cm"
        assert canonical_unit_of("walking_speed") == "km/hr"
        assert canonical_unit_of("running_speed") == "km/hr"
        assert canonical_unit_of("stair_speed_up") == "m/s"
        assert canonical_unit_of("stair_speed_down") == "m/s"
        assert canonical_unit_of("active_energy") == "kcal"
        assert canonical_unit_of("basal_energy_burned") == "kcal"
        assert canonical_unit_of("dietary_energy") == "kcal"
        assert canonical_unit_of("apple_sleeping_wrist_temperature") == "degC"
        assert canonical_unit_of("lean_body_mass") == "kg"

    def test_pass_through_metrics_have_no_canonical(self):
        assert canonical_unit_of("step_count") is None
        assert canonical_unit_of("heart_rate") is None

    def test_cumulative_units_parity(self):
        assert CUMULATIVE_UNITS == {
            "count",
            "km",
            "kcal",
            "kJ",
            "min",
            "hr",
            "mg",
            "g",
            "mcg",
        }

    @pytest.mark.parametrize(
        ("unit", "expected"),
        [("count", True), ("km", True), ("kcal", True), ("bpm", False), ("%", False)],
    )
    def test_is_cumulative_unit(self, unit: str, expected: bool):
        assert is_cumulative_unit(unit) is expected


class TestDisplayName:
    def test_known_mapping(self):
        assert display_name("step_count") == "Step Count"
        assert display_name("walking_running_distance") == "Walking + Running Distance"
        assert display_name("weight_body_mass") == "Weight & Body Mass"

    def test_blood_pressure_splits(self):
        assert display_name(BLOOD_PRESSURE_SYSTOLIC) == "Blood Pressure Systolic"
        assert display_name(BLOOD_PRESSURE_DIASTOLIC) == "Blood Pressure Diastolic"

    def test_unknown_falls_back_to_camelcase(self):
        assert display_name("some_new_metric") == "Some New Metric"


# ---------------------------------------------------------------------------
# Plain {qty, date} shape + unit policy gates
# ---------------------------------------------------------------------------


class TestPlainQty:
    def test_simple_sample(self):
        rows = metric_rows(
            doc([metric("step_count", [{"qty": 100, "date": "2026-09-08 10:00:00 +0900"}])])
        )
        assert len(rows) == 1
        assert sample_rows(rows) == [("step_count", "unknown", 100.0, "count")]
        assert rows[0].ts == datetime(2026, 9, 8, 10, 0, tzinfo=KST)
        assert day_kst(rows[0].ts).isoformat() == "2026-09-08"

    def test_point_source_wins_over_group_source(self):
        group = {
            "name": "step_count",
            "units": "count",
            "source": "iPhone",
            "data": [{"qty": 5, "date": "2026-09-08 10:00:00 +0900", "source": "Watch"}],
        }
        rows = metric_rows(doc([group]))
        assert rows[0].source == "Watch"

    def test_group_source_used_when_point_has_none(self):
        group = {
            "name": "step_count",
            "units": "count",
            "source": "iPhone",
            "data": [{"qty": 5, "date": "2026-09-08 10:00:00 +0900"}],
        }
        assert metric_rows(doc([group]))[0].source == "iPhone"

    def test_missing_source_becomes_unknown(self):
        rows = metric_rows(
            doc([metric("step_count", [{"qty": 1, "date": "2026-09-08 10:00:00 +0900"}])])
        )
        assert rows[0].source == "unknown"


class TestCanonicalUnitConversion:
    """Iron-law-4 amendment (run 2026-09-17-normalizer-audit §4, user-approved):
    canonical metrics with a non-canonical declared unit are CONVERTED when
    an exact factor exists; only units with no factor are skipped — visibly."""

    def test_lb_converts_to_kg_exact_factor(self):
        decision = _qty_for_unit("weight_body_mass", "lb", 160.0)
        assert decision.skip_reason is None
        # 160 / 2.20462 = 72.5748655097… (spec §4 factor ÷ 2.20462).
        assert decision.row == pytest.approx((72.5748655, "kg"), abs=1e-6)
        rows = metric_rows(
            doc(
                [
                    metric(
                        "weight_body_mass",
                        [{"qty": 160, "date": "2026-09-08 10:00:00 +0900"}],
                        units="lb",
                    )
                ]
            )
        )
        assert rows[0].qty == pytest.approx(72.5748655)
        assert rows[0].unit == "kg"

    def test_st_converts_to_kg_exact_factor(self):
        decision = _qty_for_unit("weight_body_mass", "st", 11.8)
        assert decision.skip_reason is None
        # 11.8 / 0.157473 = 74.9334806602… (spec §4 factor ÷ 0.157473).
        assert decision.row == pytest.approx((74.9334807, "kg"), abs=1e-6)
        rows = metric_rows(
            doc(
                [
                    metric(
                        "weight_body_mass",
                        [{"qty": 11.8, "date": "2026-09-08 10:00:00 +0900"}],
                        units="st",
                    )
                ]
            )
        )
        assert rows[0].qty == pytest.approx(74.9334807)
        assert rows[0].unit == "kg"

    def test_degf_converts_to_degc_wrist_temperature_only(self):
        decision = _qty_for_unit("apple_sleeping_wrist_temperature", "degF", 95.5)
        assert decision.skip_reason is None
        assert decision.row == pytest.approx((35.27777777777778, "degC"), abs=1e-9)
        rows = metric_rows(
            doc(
                [
                    metric(
                        "apple_sleeping_wrist_temperature",
                        [{"qty": 95.5, "date": "2026-09-08 10:00:00 +0900"}],
                        units="degF",
                    )
                ]
            )
        )
        assert rows[0].qty == pytest.approx(35.27777777777778)
        assert rows[0].unit == "degC"

    def test_degf_not_converted_for_other_metrics(self):
        # °F factor is pinned to the wrist-temperature metric only.
        rows, skips = metric_rows_detailed(
            doc(
                [
                    metric(
                        "body_fat_percentage",
                        [{"qty": 95.5, "date": "2026-09-08 10:00:00 +0900"}],
                        units="degF",
                    )
                ]
            )
        )
        assert rows == []
        assert skips == {UNIT_UNCONVERTIBLE: 1}

    def test_lean_body_mass_lb_converts(self):
        rows = metric_rows(
            doc(
                [
                    metric(
                        "lean_body_mass",
                        [{"qty": 100, "date": "2026-09-08 10:00:00 +0900"}],
                        units="lb",
                    )
                ]
            )
        )
        assert rows[0].qty == pytest.approx(100 / 2.20462)
        assert rows[0].unit == "kg"

    def test_constants_are_exact_factors(self):
        assert LB_PER_KG == 2.20462
        assert ST_PER_KG == 0.157473

    def test_weight_in_kg_passes(self):
        rows = metric_rows(
            doc(
                [
                    metric(
                        "weight_body_mass",
                        [{"qty": 68.0, "date": "2026-09-08 10:00:00 +0900"}],
                        units="kg",
                    )
                ]
            )
        )
        assert sample_rows(rows) == [("weight_body_mass", "unknown", 68.0, "kg")]

    def test_unconvertible_unit_skipped_and_counted(self):
        # A fabricated unit with no exact factor: no row, but the drop is
        # VISIBLE in the skip census (zero-silent-drops contract, spec §5).
        rows, skips = metric_rows_detailed(
            doc(
                [
                    metric(
                        "weight_body_mass",
                        [{"qty": 5, "date": "2026-09-08 10:00:00 +0900"}],
                        units="gal",
                    )
                ]
            )
        )
        assert rows == []
        assert skips == {UNIT_UNCONVERTIBLE: 1}

    def test_unconvertible_unit_direct_gate_reason(self):
        decision = _qty_for_unit("weight_body_mass", "gal", 5.0)
        assert decision.row is None
        assert decision.skip_reason == UNIT_UNCONVERTIBLE

    def test_bad_timestamp_not_counted_as_unit_skip(self):
        rows, skips = metric_rows_detailed(
            doc(
                [
                    metric(
                        "weight_body_mass",
                        [{"qty": 5, "date": "not-a-date"}],
                        units="gal",
                    )
                ]
            )
        )
        assert rows == []
        assert skips == {}

    def test_distance_in_mi_still_skipped(self):
        # mi has no exact factor in this run: skipped but visibly counted.
        rows, skips = metric_rows_detailed(
            doc(
                [
                    metric(
                        "walking_running_distance",
                        [{"qty": 3, "date": "2026-09-08 10:00:00 +0900"}],
                        units="mi",
                    )
                ]
            )
        )
        assert rows == []
        assert skips == {UNIT_UNCONVERTIBLE: 1}

    def test_pass_through_metric_any_unit_stored(self):
        rows, skips = metric_rows_detailed(
            doc(
                [
                    metric(
                        "biotin",
                        [{"qty": 5, "date": "2026-09-08 10:00:00 +0900"}],
                        units="weird",
                    )
                ]
            )
        )
        assert sample_rows(rows) == [("biotin", "unknown", 5.0, "weird")]
        assert skips == {}


class TestEnergyKjConversion:
    def test_active_energy_kj_converted_to_kcal(self):
        rows = metric_rows(
            doc(
                [
                    metric(
                        "active_energy",
                        [{"qty": 418.4, "date": "2026-09-08 10:00:00 +0900"}],
                        units="kJ",
                    )
                ]
            )
        )
        assert len(rows) == 1
        assert rows[0].qty == pytest.approx(100.0)
        assert rows[0].unit == "kcal"

    def test_active_energy_kcal_passes_unchanged(self):
        rows = metric_rows(
            doc(
                [
                    metric(
                        "active_energy",
                        [{"qty": 100, "date": "2026-09-08 10:00:00 +0900"}],
                        units="kcal",
                    )
                ]
            )
        )
        assert rows[0].qty == 100.0
        assert rows[0].unit == "kcal"

    def test_active_energy_cal_skipped_and_counted(self):
        rows, skips = metric_rows_detailed(
            doc(
                [
                    metric(
                        "active_energy",
                        [{"qty": 100, "date": "2026-09-08 10:00:00 +0900"}],
                        units="cal",
                    )
                ]
            )
        )
        assert rows == []
        assert skips == {UNIT_UNCONVERTIBLE: 1}

    def test_conversion_factor_parity(self):
        assert KJ_PER_KCAL == 4.184


# ---------------------------------------------------------------------------
# Special shape: heart_rate {Min, Avg, Max}
# ---------------------------------------------------------------------------


class TestHeartRateAvg:
    def test_qty_is_avg(self):
        point = {"Min": 55, "Avg": 62.5, "Max": 120, "date": "2026-09-08 10:30:00 +0900"}
        rows = metric_rows(doc([metric("heart_rate", [point], units="bpm")]))
        assert len(rows) == 1
        assert rows[0].qty == 62.5
        assert rows[0].unit == "bpm"
        assert rows[0].name == "heart_rate"

    def test_missing_avg_skips(self):
        point = {"Min": 55, "Max": 120, "date": "2026-09-08 10:30:00 +0900"}
        assert metric_rows(doc([metric("heart_rate", [point], units="bpm")])) == []


# ---------------------------------------------------------------------------
# Special shape: blood_pressure {systolic, diastolic} → two rows
# ---------------------------------------------------------------------------


class TestBloodPressureSplit:
    def test_splits_into_two_derived_metrics(self):
        point = {"systolic": 120, "diastolic": 80, "date": "2026-09-08 08:00:00 +0900"}
        rows = metric_rows(doc([metric("blood_pressure", [point], units="mmHg")]))
        assert len(rows) == 2
        assert sample_rows(rows) == [
            (BLOOD_PRESSURE_SYSTOLIC, "unknown", 120.0, "mmHg"),
            (BLOOD_PRESSURE_DIASTOLIC, "unknown", 80.0, "mmHg"),
        ]

    def test_display_names(self):
        assert display_name("blood_pressure_systolic") == "Blood Pressure Systolic"

    def test_non_mmhg_skipped(self):
        point = {"systolic": 120, "diastolic": 80, "date": "2026-09-08 08:00:00 +0900"}
        rows = metric_rows(doc([metric("blood_pressure", [point], units="kPa")]))
        assert rows == []

    def test_missing_diastolic_yields_systolic_only(self):
        point = {"systolic": 120, "diastolic": None, "date": "2026-09-08 08:00:00 +0900"}
        rows = metric_rows(doc([metric("blood_pressure", [point], units="mmHg")]))
        assert sample_rows(rows) == [(BLOOD_PRESSURE_SYSTOLIC, "unknown", 120.0, "mmHg")]


# ---------------------------------------------------------------------------
# Special shape: sleep_analysis aggregated (totalSleep-precedence, run
# 2026-09-17-normalizer-audit) + in-bed split + skip accounting
# ---------------------------------------------------------------------------


def sleep_agg(**overrides: object) -> dict:
    base = {
        "date": "2026-09-08 00:00:00 +0900",
        "totalSleep": 7.5,
        "asleep": 6.8,
        "core": 3.0,
        "deep": 1.2,
        "rem": 1.5,
    }
    base.update(overrides)
    return base


# Fixture matrix mirrored verbatim from tests/acceptance/parse_everything.py
# (census-locked real-corpus subsets; scenario numbers refer to that file).
WATCH_NIGHT = {
    "date": "2026-09-17 00:00:00 +0900",
    "source": "Jongkuk의 Apple Watch",
    "asleep": 0,
    "inBed": 0,
    "awake": 0.05,
    "core": 3.5891,
    "deep": 1.0473,
    "rem": 1.7382,
    "totalSleep": 6.3746,
    "sleepStart": "2026-09-16 22:11:12 +0900",
    "sleepEnd": "2026-09-17 04:58:30 +0900",
}

MIXED_STAGE_NIGHT = {
    "date": "2024-10-09 00:00:00 +0900",
    "source": "Jongkuk의 Apple Watch",
    "asleep": 2.05,
    "inBed": 0,
    "awake": 0.4167,
    "core": 3.1875,
    "deep": 1.0938,
    "rem": 1.9375,
    "totalSleep": 8.268799999999999,
    "sleepStart": "2024-10-08 21:50:00 +0900",
    "sleepEnd": "2024-10-09 06:20:00 +0900",
}

AUTOSLEEP_NAP = {
    "date": "2026-09-17 00:00:00 +0900",
    "source": "AutoSleep",
    "asleep": 0.5833333333333334,
    "inBed": 0.5833333333333334,
    "awake": 0,
    "core": 0,
    "deep": 0,
    "rem": 0,
    "totalSleep": 0.5833333333333334,
    "sleepStart": "2026-09-16 17:35:00 +0900",
    "sleepEnd": "2026-09-16 18:10:00 +0900",
}

INBED_ONLY = {
    "date": "2018-04-02 00:00:00 +0900",
    "source": "시계",
    "asleep": 0,
    "inBed": 7.332222222222223,
    "awake": 0,
    "core": 0,
    "deep": 0,
    "rem": 0,
    "totalSleep": 0,
    "sleepStart": "2018-04-02 00:15:08 +0900",
    "sleepEnd": "2018-04-02 07:35:04 +0900",
}

NO_TOTALSLEEP_SHAPE = {
    "date": "2026-09-08",
    "asleep": 7.0,
    "core": 3.5,
    "deep": 1.5,
    "rem": 2.0,
}


class TestSleepAggregated:
    def test_totalsleep_wins_over_asleep(self):
        # Re-derived (run spec §1): totalSleep has absolute precedence when
        # > 0 — the old asleep-preference test (7.5 total + 6.8 asleep →
        # 6.8) is superseded; 49/50 mixed-stage backfill nights have
        # totalSleep == core+deep+rem+asleep exactly.
        rows = metric_rows(doc([metric("sleep_analysis", [sleep_agg()], units="hr")]))
        assert len(rows) == 1
        assert rows[0].qty == 7.5
        assert rows[0].ts == datetime(2026, 9, 8, 0, 0, tzinfo=KST)

    def test_asleep_wins_when_totalsleep_absent(self):
        # The no-totalSleep shape (scenario 9): asleep carries the value.
        rows = metric_rows(
            doc([metric("sleep_analysis", [sleep_agg(totalSleep=None)], units="hr")])
        )
        assert rows[0].qty == 6.8

    def test_core_deep_rem_sum_when_asleep_and_totalsleep_zero(self):
        rows = metric_rows(
            doc(
                [
                    metric(
                        "sleep_analysis",
                        [sleep_agg(totalSleep=0, asleep=0)],
                        units="hr",
                    )
                ]
            )
        )
        assert rows[0].qty == pytest.approx(5.7)

    def test_missing_rem_skips_when_no_other_value(self):
        rows = metric_rows(
            doc(
                [
                    metric(
                        "sleep_analysis",
                        [sleep_agg(totalSleep=None, asleep=None, rem=None)],
                        units="hr",
                    )
                ]
            )
        )
        assert rows == []

    def test_unparseable_date_skips(self):
        rows = metric_rows(doc([metric("sleep_analysis", [sleep_agg(date="nope")], units="hr")]))
        assert rows == []

    def test_missing_date_skips(self):
        # Loosened dispatch still requires `date`.
        rows = metric_rows(
            doc([metric("sleep_analysis", [sleep_agg(date=None)], units="hr")])
        )
        assert rows == []


class TestSleepFixtureMatrix:
    """The 7 real-corpus fixture shapes (census-locked acceptance parity).

    Mirrors TestSleepValueRule / TestSleepInBed in
    tests/acceptance/parse_everything.py at the pure-normalizer level.
    """

    def test_watch_night_asleep_sentinel_stores_totalsleep(self):
        # Scenario 1: asleep:0 is the REST Watch sentinel (1,150/1,200
        # backfill nights) — it must never be stored as the value.
        rows, skips = metric_rows_detailed(
            doc([metric("sleep_analysis", [WATCH_NIGHT], units="hr")])
        )
        assert [(r.name, r.qty) for r in rows] == [
            ("sleep_analysis", 6.3746)
        ]
        assert skips == {}

    def test_mixed_stage_night_totalsleep_not_stage_sum(self):
        # Scenario 2: totalSleep 8.2687… wins; core+deep+rem (6.2188) would
        # lose the real asleep stage (2.05).
        rows, _ = metric_rows_detailed(
            doc([metric("sleep_analysis", [MIXED_STAGE_NIGHT], units="hr")])
        )
        assert [(r.name, r.qty) for r in rows] == [("sleep_analysis", 8.268799999999999)]

    def test_autosleep_nap_asleep_value(self):
        # Scenario 3: AutoSleep nap → asleep 0.5833…. The nap row also
        # carries inBed == asleep (real AutoSleep shape): the in-bed split
        # emits BOTH rows (in-bed time is independent of stage totals).
        rows, _ = metric_rows_detailed(
            doc([metric("sleep_analysis", [AUTOSLEEP_NAP], units="hr")])
        )
        assert [(r.name, r.qty) for r in rows] == [
            ("sleep_analysis", 0.5833333333333334),
            (SLEEP_IN_BED, 0.5833333333333334),
        ]

    def test_inbed_only_becomes_sleep_in_bed(self):
        # Scenario 4: zero sleep_analysis rows; one sleep_in_bed row.
        rows, skips = metric_rows_detailed(
            doc([metric("sleep_analysis", [INBED_ONLY], units="hr")])
        )
        assert [(r.name, r.qty) for r in rows] == [(SLEEP_IN_BED, 7.332222222222223)]
        assert skips == {}, "in-bed split is a legitimate row, not a skipped point"

    def test_no_totalsleep_shape_recognized(self):
        # Scenario 9: {date, asleep, core, deep, rem} without totalSleep —
        # loosened dispatch recognizes it; asleep wins (7.0).
        rows, _ = metric_rows_detailed(
            doc([metric("sleep_analysis", [NO_TOTALSLEEP_SHAPE], units="hr")])
        )
        assert [(r.name, r.qty) for r in rows] == [("sleep_analysis", 7.0)]

    def test_all_zero_point_skipped_and_counted(self):
        # Anti-scenario: every sleep field 0 and inBed 0 → no row, and the
        # skip must be visible for job-4's response accounting.
        point = {
            "date": "2026-09-08 00:00:00 +0900",
            "asleep": 0,
            "inBed": 0,
            "core": 0,
            "deep": 0,
            "rem": 0,
            "totalSleep": 0,
        }
        rows, skips = metric_rows_detailed(doc([metric("sleep_analysis", [point], units="hr")]))
        assert rows == []
        assert skips == {SLEEP_NO_VALUE: 1}

    def test_inbed_plus_stages_emits_both_rows(self):
        # In-bed time is independent of stage totals: both rows emitted.
        point = {**WATCH_NIGHT, "inBed": 7.5}
        rows, _ = metric_rows_detailed(doc([metric("sleep_analysis", [point], units="hr")]))
        assert [(r.name, r.qty) for r in rows] == [
            ("sleep_analysis", 6.3746),
            (SLEEP_IN_BED, 7.5),
        ]

    def test_nap_inbed_matches_asleep_emits_both(self):
        # AutoSleep nap rows carry inBed == asleep: sleep_analysis keeps the
        # nap value AND the in-bed row is emitted (independent surfaces).
        rows, _ = metric_rows_detailed(
            doc([metric("sleep_analysis", [AUTOSLEEP_NAP], units="hr")])
        )
        assert [(r.name, r.qty) for r in rows] == [
            ("sleep_analysis", 0.5833333333333334),
            (SLEEP_IN_BED, 0.5833333333333334),
        ]


class TestSleepInBedRegistration:
    def test_display_name(self):
        assert display_name(SLEEP_IN_BED) == "Sleep In-Bed"

    def test_not_in_canonical_units(self):
        # Pass-through unit policy: sleep_in_bed must NOT join
        # CANONICAL_UNITS (spec §1; declared unit 'hr' passes through).
        assert canonical_unit_of(SLEEP_IN_BED) is None

    def test_in_bed_row_carries_declared_unit(self):
        rows, _ = metric_rows_detailed(doc([metric("sleep_analysis", [INBED_ONLY], units="hr")]))
        assert rows[0].unit == "hr"

    def test_is_cumulative_seeds_from_hr(self):
        assert is_cumulative_unit("hr") is True


class TestMetricRowsDetailedSkips:
    """The job-4 seam: skip counts surface without changing the row contract."""

    def test_metric_rows_unchanged_contract(self):
        rows = metric_rows(doc([metric("sleep_analysis", [WATCH_NIGHT], units="hr")]))
        assert [(r.name, r.qty) for r in rows] == [("sleep_analysis", 6.3746)]

    def test_detailed_returns_always_present_dict(self):
        _, skips = metric_rows_detailed(doc([metric("step_count", [])]))
        assert skips == {}

    def test_unrecognized_shape_not_counted_here(self):
        # Partial-overlap shapes are payloads.py's unknown_formats concern;
        # this counter only owns recognized-but-valueless sleep points.
        _, skips = metric_rows_detailed(doc([metric("blood_pressure", [{"systolic": 120}])]))
        assert skips == {}

    def test_mixed_points_counted_individually(self):
        points = [WATCH_NIGHT, INBED_ONLY, {"date": "2026-09-08", "totalSleep": 0, "asleep": 0}]
        _, skips = metric_rows_detailed(doc([metric("sleep_analysis", points, units="hr")]))
        assert skips == {SLEEP_NO_VALUE: 1}


# ---------------------------------------------------------------------------
# Special shape: sleep_analysis unaggregated (one row per segment)
# ---------------------------------------------------------------------------


class TestSleepUnaggregated:
    def test_one_row_per_segment(self):
        points = [
            {
                "startDate": "2026-09-07 23:00:00 +0900",
                "endDate": "2026-09-07 23:30:00 +0900",
                "qty": 0.5,
                "value": "Awake",
            },
            {
                "startDate": "2026-09-07 23:30:00 +0900",
                "endDate": "2026-09-08 07:00:00 +0900",
                "qty": 7.5,
                "value": "Asleep",
            },
        ]
        rows = metric_rows(doc([metric("sleep_analysis", points, units="hr")]))
        assert len(rows) == 2
        assert [r.qty for r in rows] == [0.5, 7.5]
        assert rows[0].ts == datetime(2026, 9, 7, 23, 0, tzinfo=KST)

    def test_segment_missing_qty_skipped(self):
        points = [
            {
                "startDate": "2026-09-07 23:00:00 +0900",
                "endDate": "2026-09-07 23:30:00 +0900",
                "value": "Awake",
            }
        ]
        assert metric_rows(doc([metric("sleep_analysis", points, units="hr")])) == []


# ---------------------------------------------------------------------------
# Robustness: bad groups, bad points, non-strict envelope
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_non_dict_document(self):
        assert metric_rows(None) == []
        assert metric_rows([]) == []

    def test_missing_data_or_metrics(self):
        assert metric_rows({}) == []
        assert metric_rows({"data": {}}) == []
        assert metric_rows({"data": {"metrics": "nope"}}) == []

    def test_bad_group_skipped(self):
        rows = metric_rows(doc(["not-a-group", {"name": "", "data": []}, metric("step_count", [])]))
        assert rows == []

    def test_bad_point_skipped(self):
        rows = metric_rows(doc([metric("step_count", ["nope", {"qty": "x", "date": None}])]))
        assert rows == []

    def test_unrecognized_shape_dropped(self):
        # {systolic} alone (partial key overlap) is unrecognized upstream;
        # normalization drops it.
        rows = metric_rows(doc([metric("blood_pressure", [{"systolic": 120}], units="mmHg")]))
        assert rows == []

    def test_bad_timestamp_skips_sample(self):
        rows = metric_rows(doc([metric("step_count", [{"qty": 5, "date": "not-a-date"}])]))
        assert rows == []

    def test_naive_timestamp_treated_as_utc(self):
        rows = metric_rows(doc([metric("step_count", [{"qty": 5, "date": "2026-09-08 10:00:00"}])]))
        assert rows[0].ts.tzinfo is UTC

    def test_bool_qty_rejected(self):
        rows = metric_rows(
            doc([metric("step_count", [{"qty": True, "date": "2026-09-08 10:00:00 +0900"}])])
        )
        assert rows == []


# ---------------------------------------------------------------------------
# Workouts: kJ→kcal + heartRateData + route
# ---------------------------------------------------------------------------


def base_workout(**overrides: object) -> dict:
    base = {
        "id": "A1B2C3D4-0000-1111-2222-333344445555",
        "name": "Outdoor Run",
        "start": "2026-09-08 06:30:00 +0900",
        "end": "2026-09-08 07:10:00 +0900",
        "duration": 40.0,
        "activeEnergy": 1464.4,  # kJ → 350 kcal
        "totalEnergy": 1673.6,  # kJ → 400 kcal
        "basalEnergy": 209.2,  # kJ → 50 kcal
        "distance": 8.0,
        "avgHeartRate": 145.0,
        "maxHeartRate": 172.0,
    }
    base.update(overrides)
    return base


class TestWorkoutRows:
    def test_energy_kj_converted(self):
        (row, hr, route) = workout_rows(doc(workouts=[base_workout()]))[0]
        assert row.active_energy_kcal == pytest.approx(350.0)
        assert row.total_energy == pytest.approx(400.0)
        assert row.basal_energy == pytest.approx(50.0)
        assert hr == []
        assert route == []

    def test_core_fields_and_day_kst(self):
        row = workout_rows(doc(workouts=[base_workout()]))[0][0]
        assert isinstance(row, WorkoutRow)
        assert row.name == "Outdoor Run"
        assert row.start_ts == datetime(2026, 9, 8, 6, 30, tzinfo=KST)
        assert row.end_ts == datetime(2026, 9, 8, 7, 10, tzinfo=KST)
        assert row.duration_sec == 40.0
        assert row.day_kst.isoformat() == "2026-09-08"
        assert row.distance_km == 8.0
        assert row.avg_heart_rate == 145.0
        assert row.max_heart_rate == 172.0

    def test_optional_fields(self):
        workout = base_workout(
            isIndoor=True,
            location="Seoul",
            intensity=5.0,
            mets=9.5,
            temperature=21.0,
            humidity=55.0,
            stepCount={"value": 8000},
            stepCadence=160.0,
            speed=12.0,
            heartRateRecovery={"heartRateRecoveryValue": 25},
        )
        row = workout_rows(doc(workouts=[workout]))[0][0]
        assert row.is_indoor is True
        assert row.location == "Seoul"
        assert row.intensity == 5.0
        assert row.mets == 9.5
        assert row.temperature == 21.0
        assert row.humidity == 55.0
        assert row.step_count == 8000
        assert row.step_cadence == 160.0
        assert row.speed == 12.0
        assert row.heart_rate_recovery == {"heartRateRecoveryValue": 25}

    def test_mets_camelcase_key(self):
        row = workout_rows(doc(workouts=[base_workout(METs=7.0)]))[0][0]
        assert row.mets == 7.0

    def test_step_count_scalar(self):
        row = workout_rows(doc(workouts=[base_workout(stepCount=6500)]))[0][0]
        assert row.step_count == 6500

    def test_heart_rate_rows(self):
        workout = base_workout(
            heartRateData=[
                {"date": "2026-09-08 06:30:00 +0900", "bpm": 120},
                {"date": "2026-09-08 06:31:00 +0900", "bpm": 125},
            ]
        )
        (_, hr, _) = workout_rows(doc(workouts=[workout]))[0]
        assert len(hr) == 2
        assert hr[0].workout_id == "A1B2C3D4-0000-1111-2222-333344445555"
        assert hr[0].heart_rate == 120
        assert hr[1].ts == datetime(2026, 9, 8, 6, 31, tzinfo=KST)

    def test_route_rows(self):
        workout = base_workout(
            route=[
                {
                    "timestamp": "2026-09-08 06:30:10 +0900",
                    "latitude": 37.5665,
                    "longitude": 126.9780,
                    "course": 45.0,
                    "speed": 3.2,
                    "altitude": 38.0,
                    "horizontalAccuracy": 5.0,
                    "verticalAccuracy": 3.0,
                }
            ]
        )
        (_, _, route) = workout_rows(doc(workouts=[workout]))[0]
        assert len(route) == 1
        point = route[0]
        assert (point.latitude, point.longitude) == (37.5665, 126.9780)
        assert point.course == 45.0
        assert point.speed == 3.2
        assert point.elevation == 38.0
        assert point.h_acc == 5.0
        assert point.v_acc == 3.0
        assert point.ts == datetime(2026, 9, 8, 6, 30, 10, tzinfo=KST)

    def test_route_point_missing_longitude_skipped(self):
        workout = base_workout(
            route=[
                {"timestamp": "2026-09-08 06:30:10 +0900", "latitude": 37.5665, "longitude": None},
                {
                    "timestamp": "2026-09-08 06:30:20 +0900",
                    "latitude": 37.5666,
                    "longitude": 126.9780,
                },
            ]
        )
        (_, _, route) = workout_rows(doc(workouts=[workout]))[0]
        assert len(route) == 1
        assert route[0].latitude == 37.5666

    def test_hr_bad_point_skipped_individually(self):
        workout = base_workout(
            heartRateData=[
                {"date": "2026-09-08 06:30:00 +0900", "bpm": None},
                {"date": "2026-09-08 06:31:00 +0900", "bpm": 125},
            ]
        )
        (_, hr, _) = workout_rows(doc(workouts=[workout]))[0]
        assert [r.heart_rate for r in hr] == [125]

    @pytest.mark.parametrize(
        "overrides",
        [
            {"id": ""},
            {"start": "bogus"},
            {"end": "bogus"},
            {"duration": None},
        ],
    )
    def test_unusable_workout_skipped(self, overrides: dict):
        assert workout_rows(doc(workouts=[base_workout(**overrides)])) == []

    def test_bad_shapes_skipped(self):
        assert workout_rows(None) == []
        assert workout_rows({}) == []
        assert workout_rows({"data": {"workouts": "nope"}}) == []
        assert workout_rows(doc(workouts=["nope", 42])) == []

    def test_mixed_metrics_and_workouts(self):
        document = doc(
            metrics=[metric("step_count", [{"qty": 10, "date": "2026-09-08 10:00:00 +0900"}])],
            workouts=[base_workout()],
        )
        assert len(metric_rows(document)) == 1
        assert len(workout_rows(document)) == 1


# ---------------------------------------------------------------------------
# Real HAE v2 workout shapes (the shapes the simplified fixtures never used)
# ---------------------------------------------------------------------------


def real_workout(**overrides: object) -> dict:
    """A real-shape workout: {qty,units} dicts + per-minute point lists.

    Values are deliberately synthetic (clearly-fake 2023-01-15 date, round
    scalars, 12.34/56.78 grid) while every real payload SHAPE is preserved —
    that shape coverage is what this fixture exists to guard.
    """
    base = {
        "id": "A1B2C3D4-0000-1111-2222-333344445555",
        "name": "Outdoor Run",
        "start": "2023-01-15 18:57:52 +0900",
        "end": "2023-01-15 18:46:55 +0900",
        "duration": 2700.0,
        "activeEnergyBurned": {"qty": 2100.0, "units": "kJ"},
        "activeEnergy": [
            {"date": "2023-01-15 18:57:52 +0900", "qty": 30.0, "units": "kJ"},
            {"date": "2023-01-15 18:58:52 +0900", "qty": 40.0, "units": "kJ"},
        ],
        "totalEnergy": {"qty": 2400.0, "units": "kJ"},
        "basalEnergy": [
            {"date": "2023-01-15 18:57:52 +0900", "qty": 5.0, "units": "kJ"},
            {"date": "2023-01-15 18:58:52 +0900", "qty": 6.0, "units": "kJ"},
        ],
        "distance": {"qty": 6.5, "units": "km"},
        "avgHeartRate": {"qty": 150.0, "units": "count/min"},
        "maxHeartRate": {"qty": 180, "units": "count/min"},
        "temperature": {"qty": 22.0, "units": "degC"},
        "humidity": {"qty": 50, "units": "%"},
        "intensity": {"qty": 9.0, "units": "kcal/hr·kg"},
        "stepCadence": {"qty": 170.0, "units": "count/min"},
        "speed": {"qty": 8.0, "units": "km/hr"},
        "stepCount": [
            {"date": "2023-01-15 18:57:52 +0900", "qty": 170.0, "units": "count"},
            {"date": "2023-01-15 18:58:52 +0900", "qty": 171.0, "units": "count"},
        ],
        "walkingAndRunningDistance": [
            {"date": "2023-01-15 18:57:52 +0900", "qty": 0.15, "units": "km"},
            {"date": "2023-01-15 18:58:52 +0900", "qty": 0.16, "units": "km"},
        ],
        "heartRateData": [
            {"date": "2023-01-15 18:57:00 +0900", "Avg": 105, "Min": 105, "Max": 105},
            {"date": "2023-01-15 18:58:00 +0900", "Avg": 114, "Min": 101, "Max": 124},
            {"date": "2023-01-15 18:59:00 +0900", "Avg": 132.75, "Min": 128, "Max": 134},
        ],
        "heartRateRecovery": [
            {"date": "2023-01-15 18:46:55 +0900", "Avg": 165, "Min": 165, "Max": 165},
            {"date": "2023-01-15 18:47:00 +0900", "Avg": 166, "Min": 166, "Max": 166},
        ],
        "route": [
            {
                "timestamp": "2023-01-15 18:57:52 +0900",
                "latitude": 12.34,
                "longitude": 56.78,
                "altitude": 40.0,
            }
        ],
    }
    base.update(overrides)
    return base


class TestRealWorkoutShapes:
    def test_all_scalar_dict_fields_are_read(self):
        row = workout_rows(doc(workouts=[real_workout()]))[0][0]
        assert row.active_energy_kcal == pytest.approx(2100.0 / KJ_PER_KCAL)
        assert row.distance_km == pytest.approx(6.5)
        assert row.avg_heart_rate == pytest.approx(150.0)
        assert row.max_heart_rate == 180
        assert row.temperature == pytest.approx(22.0)
        assert row.humidity == 50
        assert row.intensity == pytest.approx(9.0)
        assert row.step_cadence == pytest.approx(170.0)
        assert row.speed == pytest.approx(8.0)

    def test_energy_lists_summed_then_converted(self):
        row = workout_rows(doc(workouts=[real_workout()]))[0][0]
        assert row.total_energy == pytest.approx(2400.0 / KJ_PER_KCAL)
        assert row.basal_energy == pytest.approx((5.0 + 6.0) / KJ_PER_KCAL)

    def test_active_energy_burned_preferred_over_active_energy_list(self):
        row = workout_rows(doc(workouts=[real_workout()]))[0][0]
        assert row.active_energy_kcal == pytest.approx(2100.0 / KJ_PER_KCAL)
        assert row.active_energy_kcal != pytest.approx((30.0 + 40.0) / KJ_PER_KCAL)

    def test_active_energy_burned_scalar_fallback(self):
        row = workout_rows(doc(workouts=[real_workout(activeEnergyBurned=418.4)]))[0][0]
        assert row.active_energy_kcal == pytest.approx(100.0)

    def test_active_energy_list_fallback_when_burned_absent(self):
        workout = real_workout()
        del workout["activeEnergyBurned"]
        row = workout_rows(doc(workouts=[workout]))[0][0]
        assert row.active_energy_kcal == pytest.approx((30.0 + 40.0) / KJ_PER_KCAL)

    def test_step_count_list_summed(self):
        row = workout_rows(doc(workouts=[real_workout()]))[0][0]
        assert row.step_count == int(170.0 + 171.0)

    def test_walking_and_running_distance_list_summed(self):
        row = workout_rows(doc(workouts=[real_workout()]))[0][0]
        assert row.walking_running_distance_km == pytest.approx(0.15 + 0.16)

    def test_walking_running_distance_legacy_key_alias(self):
        workout = real_workout()
        del workout["walkingAndRunningDistance"]
        workout["walkingRunningDistance"] = [
            {"qty": 1.5, "units": "km"},
            {"qty": 2.5, "units": "km"},
        ]
        row = workout_rows(doc(workouts=[workout]))[0][0]
        assert row.walking_running_distance_km == pytest.approx(4.0)

    def test_hr_rows_use_avg_rounded(self):
        (_, hr, _) = workout_rows(doc(workouts=[real_workout()]))[0]
        assert [r.heart_rate for r in hr] == [105, 114, 133]
        assert hr[0].ts == datetime(2023, 1, 15, 18, 57, tzinfo=KST)

    def test_recovery_list_kept_verbatim(self):
        row = workout_rows(doc(workouts=[real_workout()]))[0][0]
        assert isinstance(row.heart_rate_recovery, list)
        assert row.heart_rate_recovery[0]["Avg"] == 165
        assert len(row.heart_rate_recovery) == 2

    def test_route_rows_real_shape(self):
        (_, _, route) = workout_rows(doc(workouts=[real_workout()]))[0]
        assert len(route) == 1
        assert route[0].latitude == pytest.approx(12.34)


class TestRecomputeRollupsSqlContract:
    """The production recompute SQL must pin the float8 accumulation order.

    ``sum``/``avg`` over float8 are order-sensitive; PostgreSQL's parallel and
    hash aggregation may visit rows in an arbitrary physical order, which a
    repair re-run changes (every row is re-UPDATE'd). Pinning
    ``ORDER BY source_id, ts`` (the ``raw_samples`` PK) makes the rollup values
    reproducible across replays.

    This is an explicit SQL-contract assertion against the *real* production
    constant, so it fails the instant that clause is dropped. A content-level
    md5 snapshot across repairs cannot provide this guarantee: a small,
    plan-stable table returns identical sums for both accumulation orders.
    """

    def test_recompute_rollups_sql_is_order_deterministic(self):
        from health_db.db.helpers import RECOMPUTE_ROLLUPS

        assert "sum(qty ORDER BY source_id, ts)" in RECOMPUTE_ROLLUPS
        assert "avg(qty ORDER BY source_id, ts)" in RECOMPUTE_ROLLUPS

    def test_recompute_rollups_sql_has_no_unpinned_float8_aggregate(self):
        from health_db.db.helpers import RECOMPUTE_ROLLUPS

        assert "sum(qty)" not in RECOMPUTE_ROLLUPS
        assert "avg(qty)" not in RECOMPUTE_ROLLUPS


class TestQtyHelpers:
    """The generic scalar/{qty|value}/list extractor used by workout fields."""

    def test_scalar(self):
        assert _qty_of(5) == 5.0
        assert _qty_of(5.5) == 5.5

    def test_bool_rejected(self):
        assert _qty_of(True) is None
        assert _qty_of(False) is None

    def test_qty_dict(self):
        assert _qty_of({"qty": 3.5, "units": "km"}) == 3.5

    def test_value_dict(self):
        assert _qty_of({"value": 7}) == 7.0

    def test_list_summed(self):
        assert _qty_of([{"qty": 1}, {"qty": 2}]) == 3.0

    def test_empty_list_zero(self):
        assert _qty_of([]) == 0.0

    def test_none_and_unrecognized(self):
        assert _qty_of(None) is None
        assert _qty_of("nope") is None
        assert _qty_of({"other": 1}) is None

    def test_sum_qty_of_non_list_delegates(self):
        assert _sum_qty_of(4) == 4.0
        assert _sum_qty_of({"qty": 4}) == 4.0
        assert _sum_qty_of([{"qty": 1}, 2, {"value": 3}]) == 6.0


# ---------------------------------------------------------------------------
# ECG session lane (job-5, run 2026-09-17-normalizer-audit §2)
# ---------------------------------------------------------------------------


# Fixture mirrored verbatim from tests/acceptance/parse_everything.py
# (scenario 5 — trimmed real backfill ECG shape).
ECG_SESSION = {
    "source": "심전도",
    "start": "2025-10-12 10:45:06 +0900",
    "end": "2025-10-12 10:45:36 +0900",
    "samplingFrequency": 512,
    "numberOfVoltageMeasurements": 3,
    "classification": "정상 동율 리듬",
    "averageHeartRate": 68,
    "voltageMeasurements": [
        {"voltage": -10.528610229492188, "date": 1760233506.862702, "units": "mcV"},
        {"voltage": -8.956151962280273, "date": 1760233506.864655, "units": "mcV"},
        {"voltage": -7.150754928777866, "date": 1760233506.8666081, "units": "mcV"},
    ],
}


def ecg_doc(sessions: list | None = None) -> dict:
    data: dict = {}
    if sessions is not None:
        data["ecg"] = sessions
    return {"data": data}


def good_ts() -> datetime:
    return datetime(2025, 10, 12, 1, 45, 6, 862702, tzinfo=UTC)


class TestEcgRows:
    def test_session_fixture_fields(self):
        sessions, voltages, skips = _ecg_rows(ecg_doc([ECG_SESSION]))
        assert skips == {}
        assert len(sessions) == 1
        row = sessions[0]
        assert isinstance(row, EcgRow)
        assert row.ref == 0
        assert row.source_name == "심전도"
        assert row.start_ts == datetime(2025, 10, 12, 10, 45, 6, tzinfo=KST)
        assert row.end_ts == datetime(2025, 10, 12, 10, 45, 36, tzinfo=KST)
        assert row.classification == "정상 동율 리듬"
        assert row.sampling_frequency == 512
        assert row.average_heart_rate == 68
        assert row.voltage_count == 3

    def test_three_voltages_with_unix_epoch_ts(self):
        # VERIFIED (42/42 real backfill sessions): voltage `date` is UNIX
        # epoch seconds — first voltage == session start ±2s under this
        # reading (2025-10-12 01:45:06.862702 UTC == 10:45:06 KST). The
        # predecessor CF-absolute reading would yield year-2056 values.
        _, voltages, _ = _ecg_rows(ecg_doc([ECG_SESSION]))
        assert len(voltages) == 3
        first = voltages[0]
        assert isinstance(first, EcgVoltageRow)
        assert first.ts == datetime(2025, 10, 12, 1, 45, 6, 862702, tzinfo=UTC), (
            "voltage ts must be UNIX-epoch converted (never CF-absolute)"
        )
        assert [v.seq for v in voltages] == [0, 1, 2]
        assert [v.ecg_ref for v in voltages] == [0, 0, 0]
        assert voltages[0].voltage == pytest.approx(-10.528610229492188)
        assert voltages[1].voltage == pytest.approx(-8.956151962280273)
        assert voltages[2].voltage == pytest.approx(-7.150754928777866)
        assert {v.units for v in voltages} == {"mcV"}

    def test_first_voltage_within_2s_of_session_start(self):
        # The verification invariant from the spec, asserted directly:
        # session start (KST 10:45:06 == 01:45:06 UTC) vs first voltage ts.
        sessions, voltages, _ = _ecg_rows(ecg_doc([ECG_SESSION]))
        delta = abs((voltages[0].ts - sessions[0].start_ts).total_seconds())
        assert delta <= 2.0

    def test_ref_links_sessions_to_their_voltages(self):
        # Two sessions: each voltage row's ecg_ref must track its session's
        # index (pipeline resolves ref → DB id; job-7).
        second = {
            **ECG_SESSION,
            "start": "2025-10-13 10:45:06 +0900",
            "end": "2025-10-13 10:45:36 +0900",
        }
        sessions, voltages, _ = _ecg_rows(ecg_doc([ECG_SESSION, second]))
        assert [s.ref for s in sessions] == [0, 1]
        assert {v.ecg_ref for v in voltages} == {0, 1}
        assert len([v for v in voltages if v.ecg_ref == 0]) == 3
        assert len([v for v in voltages if v.ecg_ref == 1]) == 3

    def test_unparseable_start_session_skipped_and_counted(self):
        bad = {**ECG_SESSION, "start": "bogus"}
        sessions, voltages, skips = _ecg_rows(ecg_doc([bad, ECG_SESSION]))
        assert len(sessions) == 1 and sessions[0].ref == 1
        assert len(voltages) == 3 and all(v.ecg_ref == 1 for v in voltages)
        assert skips == {ECG_SKIP: 1}

    def test_missing_start_session_skipped(self):
        bad = {k: v for k, v in ECG_SESSION.items() if k != "start"}
        sessions, _, skips = _ecg_rows(ecg_doc([bad]))
        assert sessions == []
        assert skips == {ECG_SKIP: 1}

    def test_unparseable_end_session_skipped(self):
        # end_ts is NOT NULL (spec §6): an unparseable end cannot store.
        bad = {**ECG_SESSION, "end": "bogus"}
        sessions, _, skips = _ecg_rows(ecg_doc([bad]))
        assert sessions == []
        assert skips == {ECG_SKIP: 1}

    def test_empty_voltage_measurements_session_skipped(self):
        bad = {**ECG_SESSION, "voltageMeasurements": []}
        sessions, voltages, skips = _ecg_rows(ecg_doc([bad]))
        assert sessions == [] and voltages == []
        assert skips == {ECG_SKIP: 1}

    def test_missing_voltage_measurements_session_skipped(self):
        bad = {k: v for k, v in ECG_SESSION.items() if k != "voltageMeasurements"}
        sessions, _, skips = _ecg_rows(ecg_doc([bad]))
        assert sessions == []
        assert skips == {ECG_SKIP: 1}

    def test_non_string_source_session_skipped(self):
        # source is the natural key's first half — a non-string cannot upsert.
        bad = {**ECG_SESSION, "source": 42}
        sessions, _, skips = _ecg_rows(ecg_doc([bad]))
        assert sessions == []
        assert skips == {ECG_SKIP: 1}

    def test_malformed_voltage_point_dropped_individually(self):
        # A partially-corrupt session keeps its good samples; bad points
        # are counted, never silent (zero-silent-drops contract).
        bad_measurements = [
            {"voltage": -1.0, "date": 1760233506.862702, "units": "mcV"},
            {"voltage": None, "date": 1760233506.864655, "units": "mcV"},
            {"voltage": -2.0, "date": "not-a-number", "units": "mcV"},
            {"date": 1760233506.866608, "units": "mcV"},
            "not-a-dict",
        ]
        bad = {**ECG_SESSION, "voltageMeasurements": bad_measurements}
        sessions, voltages, skips = _ecg_rows(ecg_doc([bad]))
        assert len(sessions) == 1, "session itself is still usable"
        assert len(voltages) == 1 and voltages[0].seq == 0
        assert skips == {ECG_SKIP: 4}

    def test_voltage_epoch_out_of_range_dropped_and_counted(self):
        # JSON-legal but unrepresentable epoch (1e300): the point is dropped
        # and counted under 'ecg' — the session and its good sibling survive
        # (never-crash discipline: OverflowError/OSError must not poison the
        # full-history backfill payload).
        good = {"voltage": -1.0, "date": 1760233506.862702, "units": "mcV"}
        bad_point = {"voltage": -2.0, "date": 1e300, "units": "mcV"}
        bad = {**ECG_SESSION, "voltageMeasurements": [good, bad_point]}
        sessions, voltages, skips = _ecg_rows(ecg_doc([bad]))
        assert len(sessions) == 1, "session itself is still usable"
        assert len(voltages) == 1 and voltages[0].ts == good_ts()
        assert skips == {ECG_SKIP: 1}

    def test_voltage_epoch_nan_dropped_and_counted(self):
        # float('nan') passes the numeric-type gate but cannot be converted.
        bad_point = {"voltage": -1.0, "date": float("nan"), "units": "mcV"}
        bad = {**ECG_SESSION, "voltageMeasurements": [bad_point]}
        sessions, voltages, skips = _ecg_rows(ecg_doc([bad]))
        assert sessions == [] and voltages == []
        # 1 dropped point + 1 session (empty-after-drops, matching the
        # empty_voltage_measurements skip semantics).
        assert skips == {ECG_SKIP: 2}

    def test_voltage_epoch_negative_huge_dropped_and_counted(self):
        # -1e300 is finite but far below any representable epoch.
        bad_point = {"voltage": -1.0, "date": -1e300, "units": "mcV"}
        bad = {**ECG_SESSION, "voltageMeasurements": [bad_point]}
        sessions, voltages, skips = _ecg_rows(ecg_doc([bad]))
        assert sessions == [] and voltages == []
        assert skips == {ECG_SKIP: 2}

    def test_valid_voltage_survives_alongside_out_of_range_point(self):
        # A corrupt point must not poison its good siblings in the SAME
        # session (partial-corruption discipline, mirrored from the
        # malformed-point test above).
        good = {"voltage": 3.5, "date": 1760233506.862702, "units": "mcV"}
        mixed = [
            {"voltage": -2.0, "date": float("inf"), "units": "mcV"},
            good,
            {"voltage": -2.0, "date": float("-inf"), "units": "mcV"},
        ]
        bad = {**ECG_SESSION, "voltageMeasurements": mixed}
        sessions, voltages, skips = _ecg_rows(ecg_doc([bad]))
        assert len(sessions) == 1
        assert len(voltages) == 1 and voltages[0].seq == 1
        assert voltages[0].voltage == pytest.approx(3.5)
        assert voltages[0].ts == good_ts()
        assert skips == {ECG_SKIP: 2}

    def test_optional_session_scalars_default_to_none(self):
        # spec §6: classification / sampling_frequency / average_heart_rate
        # are nullable; numberOfVoltageMeasurements falls back to the parsed
        # array length when absent.
        minimal = {
            "source": "심전도",
            "start": "2025-10-12 10:45:06 +0900",
            "end": "2025-10-12 10:45:36 +0900",
            "voltageMeasurements": ECG_SESSION["voltageMeasurements"],
        }
        sessions, voltages, _ = _ecg_rows(ecg_doc([minimal]))
        row = sessions[0]
        assert row.classification is None
        assert row.sampling_frequency is None
        assert row.average_heart_rate is None
        assert row.voltage_count == 3, "declared count absent → parsed length"
        assert len(voltages) == 3

    def test_non_dict_session_skipped(self):
        sessions, _, skips = _ecg_rows(ecg_doc(["nope", 42, ECG_SESSION]))
        assert [s.ref for s in sessions] == [2]
        assert skips == {ECG_SKIP: 2}

    def test_absent_or_non_list_lane_yields_nothing_no_skips(self):
        # An absent lane is not a drop; a malformed lane value likewise
        # yields nothing (defensive parity with metric_rows/workout_rows).
        assert _ecg_rows(ecg_doc(None)) == ([], [], {})
        assert _ecg_rows(ecg_doc([])) == ([], [], {})
        assert _ecg_rows({}) == ([], [], {})
        assert _ecg_rows({"data": {"ecg": "nope"}}) == ([], [], {})
        assert _ecg_rows(None) == ([], [], {})

    def test_lane_absent_from_metrics_payload_no_skips(self):
        # A normal metrics payload carries no `ecg` key: zero skips.
        document = doc([metric("step_count", [{"qty": 5, "date": "2026-09-08 10:00:00 +0900"}])])
        assert _ecg_rows(document) == ([], [], {})

    def test_frozen_dataclasses(self):
        sessions, voltages, _ = _ecg_rows(ecg_doc([ECG_SESSION]))
        with pytest.raises(Exception):
            sessions[0].voltage_count = 9  # type: ignore[misc]
        with pytest.raises(Exception):
            voltages[0].voltage = 0.0  # type: ignore[misc]


class TestEcgVoltageBatchConstant:
    def test_batch_cap_is_10k(self):
        # spec §8: ECG voltage inserts are batched ≤10k per executemany
        # (600,000 voltages on the real backfill); the constant lives next
        # to the normalizer so the pipeline imports one shared value.
        from health_db.normalize import ECG_VOLTAGE_BATCH

        assert ECG_VOLTAGE_BATCH == 10_000


# ---------------------------------------------------------------------------
# Heart-rate-notification lane (job-6, run 2026-09-17-normalizer-audit §3)
# ---------------------------------------------------------------------------


# Fixture mirrored verbatim from tests/acceptance/parse_everything.py
# (scenario 6 — trimmed real backfill notification shape).
HR_NOTIFICATION = {
    "start": "2026-06-16T04:39:20Z",
    "end": "2026-06-16T04:39:20Z",
    "threshold": 118,
    "heartNotification": "high_heart_rate_notification",
    "source": {"name": "Jongkuk의 Apple Watch", "identifier": "com.apple.health.A1B2C3D4"},
    "heartRateData": [
        {
            "hrUnits": "count/min",
            "heartRate": 132,
            "timestamp": {
                "measurementDuration": 0,
                "end": "2026-06-16T04:40:29Z",
                "units": "s",
                "start": "2026-06-16T04:40:29Z",
            },
        },
    ],
    "hrvData": [
        {
            "units": "ms",
            "hrv": 27.413754219876544,
            "timestamp": {
                "measurementDuration": 59.5,
                "end": "2026-06-16T04:31:05Z",
                "units": "s",
                "start": "2026-06-16T04:30:05Z",
            },
        },
    ],
}


def hrn_doc(events: list | None = None) -> dict:
    data: dict = {}
    if events is not None:
        data["heartRateNotifications"] = events
    return {"data": data}


class TestHrnRows:
    def test_event_fixture_fields(self):
        events, samples, skips = _hrn_rows(hrn_doc([HR_NOTIFICATION]))
        assert skips == {}
        assert len(events) == 1
        row = events[0]
        assert isinstance(row, HrnRow)
        assert row.ref == 0
        assert row.source_name == "Jongkuk의 Apple Watch"
        # ISO-8601 Z strings via _parse_ts (never the HAE %z space format).
        assert row.start_ts == datetime(2026, 6, 16, 4, 39, 20, tzinfo=UTC)
        assert row.end_ts == datetime(2026, 6, 16, 4, 39, 20, tzinfo=UTC)
        assert row.kind == "high_heart_rate_notification"
        assert row.threshold == 118

    def test_samples_hr_and_hrv_with_pinned_keys(self):
        # Value/unit keys are pinned from the real corpus: HR reads
        # heartRate/hrUnits, HRV reads hrv/units; ts = timestamp.start.
        events, samples, _ = _hrn_rows(hrn_doc([HR_NOTIFICATION]))
        assert len(samples) == 2
        hr = samples[0]
        assert isinstance(hr, HrnSampleRow)
        assert hr.hrn_ref == 0
        assert hr.kind == HRN_KIND_HR
        assert hr.value == 132.0
        assert hr.units == "count/min"
        assert hr.ts == datetime(2026, 6, 16, 4, 40, 29, tzinfo=UTC)
        hrv = samples[1]
        assert hrv.kind == HRN_KIND_HRV
        assert hrv.value == pytest.approx(27.413754219876544)
        assert hrv.units == "ms"
        assert hrv.ts == datetime(2026, 6, 16, 4, 30, 5, tzinfo=UTC)

    def test_kind_values_are_hr_and_hrv(self):
        _, samples, _ = _hrn_rows(hrn_doc([HR_NOTIFICATION]))
        assert {s.kind for s in samples} == {"hr", "hrv"}

    def test_ref_links_events_to_their_samples(self):
        # Two events: each sample's hrn_ref must track its event's index
        # (pipeline resolves ref → DB id; job-7).
        second = {
            **HR_NOTIFICATION,
            "start": "2026-06-17T04:39:20Z",
            "end": "2026-06-17T04:39:20Z",
        }
        events, samples, _ = _hrn_rows(hrn_doc([HR_NOTIFICATION, second]))
        assert [e.ref for e in events] == [0, 1]
        assert {s.hrn_ref for s in samples} == {0, 1}
        assert len([s for s in samples if s.hrn_ref == 0]) == 2
        assert len([s for s in samples if s.hrn_ref == 1]) == 2

    def test_event_without_samples_still_stored(self):
        # Unlike ECG (empty voltages → session skipped), an event with no
        # sample lists still carries its own data (kind/threshold/window) —
        # heartRateData/hrvData are optional (spec §3 shape).
        minimal = {
            "start": "2026-06-16T04:39:20Z",
            "end": "2026-06-16T04:39:20Z",
            "heartNotification": "high_heart_rate_notification",
            "source": {"name": "Watch"},
        }
        events, samples, skips = _hrn_rows(hrn_doc([minimal]))
        assert len(events) == 1 and samples == []
        assert skips == {}

    def test_unparseable_start_event_skipped_and_counted(self):
        bad = {**HR_NOTIFICATION, "start": "bogus"}
        events, samples, skips = _hrn_rows(hrn_doc([bad, HR_NOTIFICATION]))
        assert len(events) == 1 and events[0].ref == 1
        assert len(samples) == 2 and all(s.hrn_ref == 1 for s in samples)
        assert skips == {NOTIFICATION_SKIP: 1}

    def test_missing_start_event_skipped(self):
        bad = {k: v for k, v in HR_NOTIFICATION.items() if k != "start"}
        events, _, skips = _hrn_rows(hrn_doc([bad]))
        assert events == []
        assert skips == {NOTIFICATION_SKIP: 1}

    def test_unparseable_end_event_skipped(self):
        # end_ts is NOT NULL (spec §6): an unparseable end cannot store.
        bad = {**HR_NOTIFICATION, "end": "bogus"}
        events, _, skips = _hrn_rows(hrn_doc([bad]))
        assert events == []
        assert skips == {NOTIFICATION_SKIP: 1}

    def test_missing_kind_event_skipped(self):
        # kind is NOT NULL (spec §6): a missing heartNotification cannot store.
        bad = {k: v for k, v in HR_NOTIFICATION.items() if k != "heartNotification"}
        events, _, skips = _hrn_rows(hrn_doc([bad]))
        assert events == []
        assert skips == {NOTIFICATION_SKIP: 1}

    def test_non_string_kind_event_skipped(self):
        bad = {**HR_NOTIFICATION, "heartNotification": 42}
        events, _, skips = _hrn_rows(hrn_doc([bad]))
        assert events == []
        assert skips == {NOTIFICATION_SKIP: 1}

    def test_missing_source_name_event_skipped(self):
        # source.name is the natural key's first half — a non-string cannot
        # upsert latest-wins (real corpus shape: {name, identifier} dict).
        bad = {**HR_NOTIFICATION, "source": {"identifier": "com.apple.health"}}
        events, _, skips = _hrn_rows(hrn_doc([bad]))
        assert events == []
        assert skips == {NOTIFICATION_SKIP: 1}

    def test_non_dict_source_event_skipped(self):
        bad = {**HR_NOTIFICATION, "source": "Jongkuk의 Apple Watch"}
        events, _, skips = _hrn_rows(hrn_doc([bad]))
        assert events == []
        assert skips == {NOTIFICATION_SKIP: 1}

    def test_malformed_sample_dropped_individually(self):
        # A partially-corrupt event keeps its good samples; bad points are
        # counted, never silent (zero-silent-drops contract).
        bad_hr_data = [
            {
                "hrUnits": "count/min",
                "heartRate": 132,
                "timestamp": {"start": "2026-06-16T04:40:29Z"},
            },
            {
                "hrUnits": "count/min",
                "heartRate": None,
                "timestamp": {"start": "2026-06-16T04:51:29Z"},
            },
            {"hrUnits": "count/min", "timestamp": {"start": "2026-06-16T04:52:29Z"}},
            {"hrUnits": "count/min", "heartRate": 130, "timestamp": {"start": "not-a-date"}},
            {"hrUnits": "count/min", "heartRate": 131},
            "not-a-dict",
        ]
        bad = {**HR_NOTIFICATION, "heartRateData": bad_hr_data}
        events, samples, skips = _hrn_rows(hrn_doc([bad]))
        assert len(events) == 1, "event itself is still usable"
        hr_samples = [s for s in samples if s.kind == HRN_KIND_HR]
        assert len(hr_samples) == 1 and hr_samples[0].value == 132.0
        assert skips == {NOTIFICATION_SKIP: 5}

    def test_missing_timestamp_object_dropped_and_counted(self):
        bad = {
            **HR_NOTIFICATION,
            "heartRateData": [{"hrUnits": "count/min", "heartRate": 132}],
            "hrvData": [],
        }
        events, samples, skips = _hrn_rows(hrn_doc([bad]))
        assert len(events) == 1
        assert samples == []
        assert skips == {NOTIFICATION_SKIP: 1}

    def test_timestamp_end_used_only_via_start(self):
        # The sample ts comes from timestamp.start — NOT timestamp.end
        # (corpus shape: HRV start 04:30:05 ≠ end 04:31:05).
        _, samples, _ = _hrn_rows(hrn_doc([HR_NOTIFICATION]))
        hrv = next(s for s in samples if s.kind == HRN_KIND_HRV)
        assert hrv.ts == datetime(2026, 6, 16, 4, 30, 5, tzinfo=UTC)

    def test_units_missing_defaults_to_none(self):
        # units column is nullable (spec §6): a missing unit string → None,
        # not a crash and not a skip.
        event = {
            **HR_NOTIFICATION,
            "heartRateData": [
                {"heartRate": 132, "timestamp": {"start": "2026-06-16T04:40:29Z"}}
            ],
            "hrvData": [],
        }
        _, samples, skips = _hrn_rows(hrn_doc([event]))
        assert len(samples) == 1
        assert samples[0].units is None
        assert skips == {}

    def test_threshold_missing_defaults_to_none(self):
        # threshold is nullable (spec §6).
        event = {k: v for k, v in HR_NOTIFICATION.items() if k != "threshold"}
        events, _, skips = _hrn_rows(hrn_doc([event]))
        assert events[0].threshold is None
        assert skips == {}

    def test_non_dict_event_skipped(self):
        events, _, skips = _hrn_rows(hrn_doc(["nope", 42, HR_NOTIFICATION]))
        assert [e.ref for e in events] == [2]
        assert skips == {NOTIFICATION_SKIP: 2}

    def test_absent_or_non_list_lane_yields_nothing_no_skips(self):
        # An absent lane is not a drop; a malformed lane value likewise
        # yields nothing (defensive parity with _ecg_rows/metric_rows).
        assert _hrn_rows(hrn_doc(None)) == ([], [], {})
        assert _hrn_rows(hrn_doc([])) == ([], [], {})
        assert _hrn_rows({}) == ([], [], {})
        assert _hrn_rows({"data": {"heartRateNotifications": "nope"}}) == ([], [], {})
        assert _hrn_rows(None) == ([], [], {})

    def test_lane_absent_from_metrics_payload_no_skips(self):
        # A normal metrics payload carries no heartRateNotifications key.
        document = doc([metric("step_count", [{"qty": 5, "date": "2026-09-08 10:00:00 +0900"}])])
        assert _hrn_rows(document) == ([], [], {})

    def test_frozen_dataclasses(self):
        events, samples, _ = _hrn_rows(hrn_doc([HR_NOTIFICATION]))
        with pytest.raises(Exception):
            events[0].threshold = 9  # type: ignore[misc]
        with pytest.raises(Exception):
            samples[0].value = 0.0  # type: ignore[misc]
