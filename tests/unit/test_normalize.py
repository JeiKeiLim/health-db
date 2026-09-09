"""Unit tests for health_db.normalize — pure HAE v2 normalization.

Covers decomposition job slice-2-normalize acceptance: every documented
data-point shape (plain qty, heart_rate Avg, blood-pressure split, sleep
aggregated asleep-preference, sleep unaggregated), the unit policy
(canonical match, kJ→kcal ÷ 4.184, non-canonical skip, pass-through),
KST day computation (fixed +9h, midnight boundary), and workout
normalization (kJ→kcal energies, heartRateData, route rows).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from health_db.normalize import (
    BLOOD_PRESSURE_DIASTOLIC,
    BLOOD_PRESSURE_SYSTOLIC,
    CUMULATIVE_UNITS,
    KJ_PER_KCAL,
    SampleRow,
    WorkoutRow,
    canonical_unit_of,
    day_kst,
    display_name,
    is_cumulative_unit,
    metric_rows,
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


class TestCanonicalUnitSkip:
    def test_weight_in_lb_is_skipped_not_converted(self):
        rows = metric_rows(
            doc(
                [
                    metric(
                        "weight_body_mass",
                        [{"qty": 150, "date": "2026-09-08 10:00:00 +0900"}],
                        units="lb",
                    )
                ]
            )
        )
        assert rows == []

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

    @pytest.mark.parametrize("unit", ["st", "lb"])
    def test_non_canonical_variants_skipped(self, unit: str):
        rows = metric_rows(
            doc(
                [
                    metric(
                        "weight_body_mass",
                        [{"qty": 1, "date": "2026-09-08 10:00:00 +0900"}],
                        units=unit,
                    )
                ]
            )
        )
        assert rows == []

    def test_temperature_in_degf_skipped(self):
        rows = metric_rows(
            doc(
                [
                    metric(
                        "apple_sleeping_wrist_temperature",
                        [{"qty": 98.6, "date": "2026-09-08 10:00:00 +0900"}],
                        units="degF",
                    )
                ]
            )
        )
        assert rows == []

    def test_distance_in_mi_skipped(self):
        rows = metric_rows(
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

    def test_active_energy_cal_skipped(self):
        rows = metric_rows(
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
# Special shape: sleep_analysis aggregated (asleep-preference)
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


class TestSleepAggregated:
    def test_asleep_wins_when_present(self):
        rows = metric_rows(doc([metric("sleep_analysis", [sleep_agg()], units="hr")]))
        assert len(rows) == 1
        assert rows[0].qty == 6.8
        assert rows[0].ts == datetime(2026, 9, 8, 0, 0, tzinfo=KST)

    def test_core_deep_rem_sum_when_asleep_absent(self):
        rows = metric_rows(doc([metric("sleep_analysis", [sleep_agg(asleep=None)], units="hr")]))
        assert rows[0].qty == pytest.approx(5.7)

    def test_missing_rem_skips_when_no_asleep(self):
        rows = metric_rows(
            doc([metric("sleep_analysis", [sleep_agg(asleep=None, rem=None)], units="hr")])
        )
        assert rows == []

    def test_unparseable_date_skips(self):
        rows = metric_rows(doc([metric("sleep_analysis", [sleep_agg(date="nope")], units="hr")]))
        assert rows == []


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
                    "longitude": REDACTED,
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
        assert (point.latitude, point.longitude) == (37.5665, REDACTED)
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
                    "longitude": REDACTED,
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
