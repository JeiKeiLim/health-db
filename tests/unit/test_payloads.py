"""Unit tests for health_db.payloads — HAE JSON v2 parse + validate.

Covers decomposition job slice-1-payloads acceptance: every documented
data-point shape, unknown-metric acceptance, wrong-shape rejection,
counting, data_type classification, unknown_formats reporting, plus the
hardening fixes from the slice-1-payloads code-critic review:

- invalid UTF-8 / NaN / Infinity / deep nesting / non-finite floats →
  MalformedPayload (never RecursionError or inf leakage),
- full-key-set shape recognition (partial overlap → unknown_formats),
- data_type stays within the 'metrics' | 'workouts' contract enum.
"""

from __future__ import annotations

import json

import pytest

from health_db.payloads import MalformedPayload, PayloadReport, parse


def _recursion_limit() -> int:
    import sys

    return sys.getrecursionlimit()


# ---------------------------------------------------------------------------
# Body builders
# ---------------------------------------------------------------------------


def wrap(metrics: list | None = None, workouts: list | None = None) -> bytes:
    data: dict = {}
    if metrics is not None:
        data["metrics"] = metrics
    if workouts is not None:
        data["workouts"] = workouts
    return json.dumps({"data": data}).encode()


def metric(name: str, points: list, units: str = "count") -> dict:
    return {"name": name, "units": units, "data": points}


# ---------------------------------------------------------------------------
# Wrong top-level shape → MalformedPayload (API layer turns this into 400)
# ---------------------------------------------------------------------------


class TestWrongShapeRejected:
    @pytest.mark.parametrize(
        "body",
        [
            b"{broken",
            b"garbage-not-json",
            b"",
            b"null",
            b"42",
            b'"just a string"',
            b"[1, 2, 3]",
            b'{"nope": 1}',
            b'{"data": "not-an-object"}',
            b'{"data": {"metrics": "not-a-list", "workouts": []}}',
            b'{"data": {"metrics": [], "workouts": 7}}',
        ],
    )
    def test_malformed_body_raises(self, body: bytes):
        with pytest.raises(MalformedPayload):
            parse(body)

    def test_missing_data_key(self):
        with pytest.raises(MalformedPayload, match="data"):
            parse(json.dumps({"metrics": [], "workouts": []}).encode())

    def test_empty_object(self):
        with pytest.raises(MalformedPayload):
            parse(b"{}")

    def test_json_decode_error_message_actionable(self):
        with pytest.raises(MalformedPayload, match="not valid JSON"):
            parse(b"{broken")

    def test_str_body_accepted_like_bytes(self):
        report = parse('{"data": {"metrics": [{"qty": 1, "date": "d"}], "workouts": []}}')
        assert report.metrics == 1

    def test_invalid_utf8_body_raises(self):
        with pytest.raises(MalformedPayload, match="not valid JSON"):
            parse(b"\xff\xfe\x80bad-utf8")

    def test_nan_literal_rejected(self):
        with pytest.raises(MalformedPayload, match="not valid JSON"):
            parse(b'{"data": {"metrics": [{"qty": NaN, "date": "d"}], "workouts": []}}')

    def test_infinity_literal_rejected(self):
        with pytest.raises(MalformedPayload, match="not valid JSON"):
            parse(b'{"data": {"metrics": [{"qty": Infinity, "date": "d"}], "workouts": []}}')

    def test_negative_infinity_literal_rejected(self):
        with pytest.raises(MalformedPayload, match="not valid JSON"):
            parse(b'{"data": {"metrics": [{"qty": -Infinity, "date": "d"}], "workouts": []}}')

    def test_non_finite_float_overflow_rejected(self):
        # 1e400 parses to inf; syntactically valid JSON, semantically rejected
        with pytest.raises(MalformedPayload, match="not valid JSON"):
            parse(b'{"data": {"metrics": [{"qty": 1e400, "date": "d"}], "workouts": []}}')

    def test_deeply_nested_body_raises_malformed_not_recursionerror(self):
        # 10x the interpreter's recursion limit guarantees RecursionError in
        # json.loads; parse() must classify it as MalformedPayload (a ValueError).
        depth = 10 * _recursion_limit()
        body = b'{"data": ' * depth + b"{}" + b"}" * depth
        with pytest.raises(MalformedPayload, match="nesting is too deep"):
            parse(body)

    def test_deeply_nested_str_body_raises_malformed(self):
        depth = 10 * _recursion_limit()
        body = "[" * depth + "]" * depth
        with pytest.raises(MalformedPayload):
            parse(body)

    def test_empty_payload_rejected(self):
        with pytest.raises(MalformedPayload, match="no metrics and no workouts"):
            parse(wrap([], []))


# ---------------------------------------------------------------------------
# Documented shape: common {qty, date, source?}
# ---------------------------------------------------------------------------


DATE = "2026-09-08 09:00:00 +0900"


class TestCommonShape:
    def test_common_shape_with_source(self):
        point = {"qty": 8500, "date": DATE, "source": "Apple Watch"}
        report = parse(wrap([metric("step_count", [point])]))
        assert report.unknown_formats == ()
        assert report.samples == 1

    def test_common_shape_source_omitted_ios27(self):
        report = parse(wrap([metric("step_count", [{"qty": 120, "date": DATE}])]))
        assert report.unknown_formats == ()
        assert report.samples == 1

    def test_cumulative_and_heart_rate_common_points(self):
        report = parse(
            wrap(
                [
                    metric("walking_heart_rate_average", [{"qty": 65, "date": DATE}], "count/min"),
                    metric("body_mass", [{"qty": 72.5, "date": DATE, "source": "iPhone"}], "kg"),
                ]
            )
        )
        assert report.unknown_formats == ()
        assert report.samples == 2


# ---------------------------------------------------------------------------
# Documented shape: heart_rate {Min, Avg, Max}
# ---------------------------------------------------------------------------


class TestHeartRateShape:
    def test_heart_rate_min_avg_max(self):
        report = parse(
            wrap(
                [
                    metric(
                        "heart_rate",
                        [{"date": "2026-09-08 09:00:00 +0900", "Min": 65, "Avg": 72, "Max": 85}],
                        "count/min",
                    )
                ]
            )
        )
        assert report.unknown_formats == ()
        assert report.samples == 1
        assert report.metrics == 1


# ---------------------------------------------------------------------------
# Documented shape: blood_pressure {systolic, diastolic}
# ---------------------------------------------------------------------------


class TestBloodPressureShape:
    def test_blood_pressure_systolic_diastolic(self):
        report = parse(
            wrap(
                [
                    metric(
                        "blood_pressure",
                        [
                            {"systolic": 118, "diastolic": 76, "date": "2026-09-08 09:00:00 +0900"},
                        ],
                        "mmHg",
                    )
                ]
            )
        )
        assert report.unknown_formats == ()
        assert report.samples == 1


# ---------------------------------------------------------------------------
# Documented shape: sleep_analysis aggregated + unaggregated
# ---------------------------------------------------------------------------


class TestSleepAnalysisShapes:
    def test_sleep_analysis_aggregated(self):
        report = parse(
            wrap(
                [
                    metric(
                        "sleep_analysis",
                        [
                            {
                                "date": "2026-09-08",
                                "totalSleep": 7.5,
                                "asleep": 7.0,
                                "core": 3.5,
                                "deep": 1.5,
                                "rem": 2.0,
                                "sleepStart": "2026-09-07 23:00:00 +0900",
                                "sleepEnd": "2026-09-08 06:30:00 +0900",
                                "inBed": 8.0,
                                "inBedStart": "2026-09-07 22:45:00 +0900",
                                "inBedEnd": "2026-09-08 06:45:00 +0900",
                            }
                        ],
                        "hr",
                    )
                ]
            )
        )
        assert report.unknown_formats == ()
        assert report.samples == 1

    def test_sleep_analysis_unaggregated_qty_value(self):
        report = parse(
            wrap(
                [
                    metric(
                        "sleep_analysis",
                        [
                            {
                                "startDate": "2026-09-07 23:00:00 +0900",
                                "endDate": "2026-09-08 00:30:00 +0900",
                                "qty": 1.5,
                                "value": "Deep",
                                "sleepStart": "2026-09-07 23:00:00 +0900",
                                "sleepEnd": "2026-09-08 06:30:00 +0900",
                            }
                        ],
                        "hr",
                    )
                ]
            )
        )
        assert report.unknown_formats == ()
        assert report.samples == 1


# ---------------------------------------------------------------------------
# Unknown metric: accepted and counted, never rejected
# ---------------------------------------------------------------------------


class TestUnknownMetricAccepted:
    def test_unknown_metric_name_accepted(self):
        payload = {
            "data": {
                "metrics": [metric("some_future_metric", [{"qty": 3, "date": DATE}])],
                "workouts": [],
            }
        }
        report = parse(json.dumps(payload).encode())
        assert report.metrics == 1
        assert report.samples == 1
        assert report.unknown_formats == ()

    def test_unknown_metric_with_documented_shape_still_accepted(self):
        # A renamed/unknown metric with a documented shape parses fine —
        # shapes are keyed on structure, not on a name registry.
        payload = {
            "data": {
                "metrics": [
                    metric("alien_metric", [{"date": DATE, "Min": 1, "Avg": 2, "Max": 3}]),
                ],
                "workouts": [],
            }
        }
        report = parse(json.dumps(payload).encode())
        assert report.unknown_formats == ()
        assert report.samples == 1


# ---------------------------------------------------------------------------
# Unknown formats: reported, payload still accepted
# ---------------------------------------------------------------------------


class TestUnknownFormatDetection:
    def test_unrecognized_point_shape_reported_not_rejected(self):
        payload = {
            "data": {
                "metrics": [metric("mystery_metric", [{"foo": 1, "bar": 2}], "count")],
                "workouts": [],
            }
        }
        report = parse(json.dumps(payload).encode())
        assert report.metrics == 1  # payload still counted/accepted
        assert len(report.unknown_formats) == 1
        entry = report.unknown_formats[0]
        assert entry["name"] == "mystery_metric"
        assert entry["units"] == "count"
        assert "foo" in entry["reason"]

    def test_non_object_point_reported(self):
        report = parse(wrap([metric("weird_metric", ["not-a-dict"])]))
        assert len(report.unknown_formats) == 1
        assert "not an object" in report.unknown_formats[0]["reason"]

    def test_group_missing_name_reported(self):
        report = parse(wrap([{"units": "count", "data": [{"qty": 1, "date": DATE}]}]))
        assert len(report.unknown_formats) == 1
        assert "name" in report.unknown_formats[0]["reason"]

    def test_group_missing_data_reported(self):
        report = parse(wrap([{"name": "no_data_metric", "units": "count"}]))
        assert len(report.unknown_formats) == 1
        assert report.unknown_formats[0]["name"] == "no_data_metric"

    def test_group_data_not_array_reported(self):
        report = parse(wrap([{"name": "bad_data_metric", "units": "count", "data": {"qty": 1}}]))
        assert len(report.unknown_formats) == 1
        assert report.metrics == 1  # still counted, still accepted

    def test_non_object_group_reported(self):
        report = parse(wrap(["not-a-group"]))
        assert len(report.unknown_formats) == 1

    def test_workout_missing_id_reported(self):
        workout = {
            "name": "Running",
            "start": "2026-09-08 07:02:00 +0900",
            "end": "2026-09-08 07:47:00 +0900",
        }
        report = parse(wrap([], [workout]))
        assert report.workouts == 1
        assert len(report.unknown_formats) == 1
        assert "id" in report.unknown_formats[0]["reason"]

    def test_non_object_workout_reported(self):
        report = parse(wrap([], ["not-a-workout"]))
        assert report.workouts == 1
        assert len(report.unknown_formats) == 1

    def test_valid_workout_with_optional_fields_accepted(self):
        workout = {
            "id": "CC8B17E2-9B4A-4C1D-8E2F-0A1B2C3D4E5F",
            "name": "Running",
            "start": "2026-09-08 07:02:00 +0900",
            "end": "2026-09-08 07:47:00 +0900",
            "duration": 2700,
            "activeEnergy": 1420.8,
            "heartRateData": [{"date": "2026-09-08 07:03:00 +0900", "bpm": 128}],
            "route": [
                {"timestamp": "2026-09-08 07:03:00 +0900", "latitude": 37.33, "longitude": 127.01},
            ],
        }
        report = parse(wrap([], [workout]))
        assert report.unknown_formats == ()
        assert report.workouts == 1


# ---------------------------------------------------------------------------
# Full-key-set shape recognition: partial overlap → unknown_formats
#
# Run 2026-09-17-normalizer-audit §1: the sleep aggregated shape is the one
# documented exception — it recognizes on a SUFFICIENT key set ({date} + any
# sleep field), not a full key set. Everything else keeps full-key-set
# recognition.
# ---------------------------------------------------------------------------


class TestPartialKeyOverlapNotRecognized:
    """{systolic} alone or {Avg} alone must NOT count as a recognized shape."""

    def test_systolic_alone_is_unknown(self):
        report = parse(wrap([metric("blood_pressure", [{"systolic": 118, "date": DATE}])]))
        assert len(report.unknown_formats) == 1
        assert "systolic" in report.unknown_formats[0]["reason"]

    def test_diastolic_alone_is_unknown(self):
        report = parse(wrap([metric("blood_pressure", [{"diastolic": 76, "date": DATE}])]))
        assert len(report.unknown_formats) == 1

    def test_avg_alone_is_unknown(self):
        report = parse(wrap([metric("heart_rate", [{"Avg": 72, "date": DATE}])]))
        assert len(report.unknown_formats) == 1
        assert "Avg" in report.unknown_formats[0]["reason"]

    def test_min_max_without_avg_is_unknown(self):
        report = parse(wrap([metric("heart_rate", [{"Min": 60, "Max": 90, "date": DATE}])]))
        assert len(report.unknown_formats) == 1

    def test_sleep_aggregated_loosened_keys_recognized(self):
        # Run 2026-09-17-normalizer-audit §1: recognition loosened to the
        # sufficient-key-set rule — {date} + ANY sleep field is enough;
        # totalSleep is no longer required (scenario 9 archive shape).
        report = parse(wrap([metric("sleep_analysis", [{"date": "2026-09-08", "asleep": 7.0}])]))
        assert report.unknown_formats == ()
        assert report.samples == 1

    def test_sleep_date_plus_sleepstart_only_recognized(self):
        # sleepStart alone (no value fields) is a recognized sleep shape.
        report = parse(
            wrap(
                [
                    metric(
                        "sleep_analysis",
                        [{"date": "2026-09-08", "sleepStart": "2026-09-07 23:00:00 +0900"}],
                    )
                ]
            )
        )
        assert report.unknown_formats == ()

    def test_sleep_valueless_shape_still_counts_as_sample(self):
        # Recognition ≠ a row: {date, totalSleep: 0} counts as a sample and
        # normalize yields no row — skip accounting is the normalizer's job.
        report = parse(wrap([metric("sleep_analysis", [{"date": "2026-09-08", "totalSleep": 0}])]))
        assert report.unknown_formats == ()
        assert report.samples == 1

    def test_sleep_date_without_any_sleep_field_is_unknown(self):
        # {date} alone (no sleep-field overlap) stays unrecognized.
        report = parse(wrap([metric("sleep_analysis", [{"date": "2026-09-08"}])]))
        assert len(report.unknown_formats) == 1

    def test_sleep_unaggregated_missing_value_is_unknown(self):
        # {startDate, endDate, qty} without "value" partially overlaps the
        # sleep_analysis_unaggregated shape → must NOT be recognized.
        report = parse(
            wrap(
                [
                    metric(
                        "sleep_analysis_unagg",
                        [{"startDate": "s", "endDate": "e", "qty": 1.5}],
                    )
                ]
            )
        )
        assert len(report.unknown_formats) == 1
        entry = report.unknown_formats[0]
        assert "unrecognized data-point shape" in entry["reason"]
        assert "'endDate', 'qty', 'startDate'" in entry["reason"]
        assert "'value'" not in entry["reason"]

    def test_sleep_unaggregated_missing_qty_is_unknown(self):
        # {startDate, endDate, value} without "qty" partially overlaps the
        # sleep_analysis_unaggregated shape → must NOT be recognized.
        report = parse(
            wrap(
                [
                    metric(
                        "sleep_analysis_unagg",
                        [{"startDate": "s", "endDate": "e", "value": "Deep"}],
                    )
                ]
            )
        )
        assert len(report.unknown_formats) == 1
        entry = report.unknown_formats[0]
        assert "unrecognized data-point shape" in entry["reason"]
        assert "'endDate', 'startDate', 'value'" in entry["reason"]
        assert "'qty'" not in entry["reason"]

    def test_qty_without_date_is_unknown(self):
        report = parse(wrap([metric("step_count", [{"qty": 8500}])]))
        assert len(report.unknown_formats) == 1

    def test_partial_shapes_still_counted_and_accepted(self):
        # Unknown shapes are reported, never rejected.
        report = parse(
            wrap([metric("broken_metric", [{"systolic": 118}, {"Avg": 72}, {"qty": 1}])])
        )
        assert report.metrics == 1
        assert report.samples == 3
        assert len(report.unknown_formats) == 1

    def test_full_key_sets_still_recognized(self):
        report = parse(
            wrap(
                [
                    metric("blood_pressure", [{"systolic": 118, "diastolic": 76, "date": DATE}]),
                    metric("heart_rate", [{"Min": 60, "Avg": 70, "Max": 90, "date": DATE}]),
                    metric("sleep_analysis", [_sleep_aggregated_point()]),
                    metric(
                        "sleep_analysis_unagg",
                        [{"startDate": "s", "endDate": "e", "qty": 1.5, "value": "Deep"}],
                    ),
                ]
            )
        )
        assert report.unknown_formats == ()
        assert report.samples == 4


def _sleep_aggregated_point() -> dict:
    return {
        "date": "2026-09-08",
        "totalSleep": 7.5,
        "asleep": 7.0,
        "core": 3.5,
        "deep": 1.5,
        "rem": 2.0,
    }


# ---------------------------------------------------------------------------
# Counting + data_type classification
# ---------------------------------------------------------------------------


class TestCountingAndClassification:
    def test_both_non_empty_classifies_metrics(self):
        # data_type contract is 'metrics' | 'workouts' only; metrics win when
        # both arrays are non-empty (Batch Requests may split/combine exports).
        payload = {
            "data": {
                "metrics": [
                    metric("step_count", [{"qty": 8500, "date": "d1"}, {"qty": 120, "date": "d2"}]),
                    metric("heart_rate", [{"date": "d1", "Min": 65, "Avg": 72, "Max": 85}]),
                ],
                "workouts": [{"id": "A", "name": "Running"}, {"id": "B", "name": "Cycling"}],
            }
        }
        report = parse(json.dumps(payload).encode())
        assert report.metrics == 2
        assert report.samples == 3
        assert report.workouts == 2
        assert report.data_type == "metrics"

    def test_data_type_metrics(self):
        report = parse(wrap([metric("step_count", [{"qty": 1, "date": "d"}])], []))
        assert report.data_type == "metrics"

    def test_data_type_workouts(self):
        report = parse(wrap([], [{"id": "A", "name": "Running"}]))
        assert report.data_type == "workouts"

    def test_data_type_only_allows_contract_enum(self):
        report_m = parse(wrap([metric("x", [{"qty": 1, "date": "d"}])], []))
        report_w = parse(wrap([], [{"id": "A", "name": "Walk"}]))
        assert report_m.data_type in ("metrics", "workouts")
        assert report_w.data_type in ("metrics", "workouts")

    def test_metrics_only_envelope_without_workouts_key(self):
        report = parse(json.dumps({"data": {"metrics": [metric("x", [])]}}).encode())
        assert report.data_type == "metrics"
        assert report.workouts == 0

    def test_workouts_only_envelope_without_metrics_key(self):
        report = parse(json.dumps({"data": {"workouts": [{"id": "A", "name": "Walk"}]}}).encode())
        assert report.data_type == "workouts"
        assert report.metrics == 0

    def test_empty_data_arrays_rejected_as_malformed(self):
        with pytest.raises(MalformedPayload):
            parse(wrap([], []))

    def test_empty_data_arrays_with_extra_envelope_keys_rejected(self):
        body = json.dumps({"data": {"metrics": [], "workouts": [], "something_new": [1]}}).encode()
        with pytest.raises(MalformedPayload):
            parse(body)


# ---------------------------------------------------------------------------
# Purity + report type contract
# ---------------------------------------------------------------------------


class TestContract:
    def test_report_is_frozen_dataclass(self):
        report = parse(wrap([], [{"id": "A", "name": "Walk"}]))
        assert isinstance(report, PayloadReport)
        with pytest.raises(Exception):
            report.metrics = 99  # type: ignore[misc]

    def test_unknown_formats_is_tuple(self):
        report = parse(wrap([metric("m", ["bad"])]))
        assert isinstance(report.unknown_formats, tuple)

    def test_parse_is_stateless_and_repeatable(self):
        body = wrap([metric("step_count", [{"qty": 5, "date": "d"}])])
        first = parse(body)
        second = parse(body)
        assert first == second

    def test_malformed_payload_is_valueerror(self):
        # API layer catches ValueError-shaped MalformedPayload for 400 + quarantine.
        assert issubclass(MalformedPayload, ValueError)
