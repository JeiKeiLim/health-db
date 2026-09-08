"""HAE JSON v2 payload parsing and validation (pure functions, no I/O).

Slice-1 job ``slice-1-payloads``: parse Health Auto Export JSON v2 REST
payloads into a :class:`PayloadReport` — counts and format classification
only; slice 1 stores nothing (normalization/upsert lands in slice 2).

Envelope (research/hae-json-v2-format.md): ``{"data": {"metrics": [...],
"workouts": [...]}}``. A body that is not valid JSON or lacks the v2
envelope raises :class:`MalformedPayload` (the API layer turns this into a
400 + quarantine). Everything else is *reported*, never rejected:

- Unknown metric **names** are always accepted (auto-registered in
  ``metric_dim`` in slice 2); slice 1 keeps no name registry.
- Unknown metric data **shapes** are flagged in ``unknown_formats`` (e.g. a
  future HAE format change) while the payload is still accepted and counted.

Recognized data-point shapes (shape-based, not name-based, so renamed or
future metrics with a documented shape still parse):

- common: any point with a ``qty`` key — ``{qty, date, source?}`` (source
  may be omitted on iOS 27+). Also covers sleep_analysis unaggregated
  ``{startDate, endDate, qty, value}`` and qty-bearing variants
  (blood_glucose + mealTime, insulin_delivery + reason, handwashing/toothbrushing
  + value, sexual_activity).
- heart_rate: ``{date, Min, Avg, Max}`` (no qty; Avg is the canonical value).
- blood_pressure: ``{date, systolic, diastolic}`` (no qty).
- sleep_analysis aggregated: day-level ``{date, totalSleep, asleep, core,
  deep, rem, sleepStart, sleepEnd, inBed, ...}`` (no qty).

Counting semantics (receipt counts, per decomposition job slice-1-payloads):

- ``metrics``  = number of metric group entries in ``data.metrics``.
- ``samples``  = total data points across all groups' ``data`` arrays
  (one point = one sample regardless of shape).
- ``workouts`` = number of entries in ``data.workouts``.

``data_type`` classification: metrics-only → ``"metrics"``; workouts-only →
``"workouts"``; both non-empty (Batch Requests may split/combine exports) →
``"mixed"``; both empty → ``"empty"``. The spec's ``raw_ingest.data_type``
enum (``'metrics' | 'workouts'``) describes the per-automation norm; slice 2
storage may normalize the mixed/empty cases when writing ``raw_ingest``.

This module performs no I/O and holds no state: every function is pure and
safe to call concurrently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Key sets for special-shape recognition (research/hae-json-v2-format.md).
_BLOOD_PRESSURE_KEYS = frozenset({"systolic", "diastolic"})
_HEART_RATE_KEYS = frozenset({"Min", "Avg", "Max"})
_SLEEP_AGGREGATED_KEYS = frozenset({"totalSleep", "asleep", "core", "deep", "rem"})


class MalformedPayload(ValueError):
    """Payload is not valid JSON or lacks the HAE v2 ``{"data": {...}}`` envelope."""


@dataclass(frozen=True, slots=True)
class PayloadReport:
    """Parse outcome for one HAE JSON v2 payload (report-only; nothing stored).

    ``unknown_formats`` entries are JSON-ready dicts with ``name``/``units``/
    ``reason`` keys describing metric groups or workouts that deviate from
    the documented shapes. Anomalies never reject the payload.
    """

    data_type: str
    metrics: int
    samples: int
    workouts: int
    unknown_formats: tuple[dict[str, str], ...] = ()


def _point_shape(point: object) -> str | None:
    """Shape name for one metric data point, or None when unrecognized."""
    if not isinstance(point, dict):
        return None
    if "qty" in point:
        return "common"
    if _BLOOD_PRESSURE_KEYS & point.keys():
        return "blood_pressure"
    if _HEART_RATE_KEYS & point.keys():
        return "heart_rate"
    if _SLEEP_AGGREGATED_KEYS & point.keys():
        return "sleep_analysis_aggregated"
    return None


def _units_of(group: dict[str, object]) -> str:
    units = group.get("units", "")
    return units if isinstance(units, str) else str(units)


def _unknown_format(name: str, units: str, reason: str) -> dict[str, str]:
    return {"name": name, "units": units, "reason": reason}


def _inspect_metric_group(group: object) -> dict[str, str] | None:
    """Unknown-format entry for a metric group deviating from documented shapes."""
    if not isinstance(group, dict):
        return _unknown_format("", "", "metric group is not an object")
    name = group.get("name")
    if not isinstance(name, str) or not name:
        return _unknown_format("", _units_of(group), "metric group missing 'name'")
    data = group.get("data")
    if data is None:
        return _unknown_format(name, _units_of(group), "metric group missing 'data' array")
    if not isinstance(data, list):
        return _unknown_format(name, _units_of(group), "metric group 'data' is not an array")
    for point in data:
        if _point_shape(point) is not None:
            continue
        if isinstance(point, dict):
            reason = f"unrecognized data-point shape (keys: {sorted(point)})"
        else:
            reason = "data point is not an object"
        return _unknown_format(name, _units_of(group), reason)
    return None


def _inspect_workout(workout: object) -> dict[str, str] | None:
    """Unknown-format entry for a workout entry deviating from the v2 shape."""
    if not isinstance(workout, dict):
        return _unknown_format("workout", "", "workout entry is not an object")
    workout_id = workout.get("id")
    if not isinstance(workout_id, str) or not workout_id:
        return _unknown_format("workout", "", "workout missing 'id' (required for upsert)")
    return None


def _classify_data_type(n_metric_groups: int, n_workouts: int) -> str:
    if n_metric_groups and n_workouts:
        return "mixed"
    if n_metric_groups:
        return "metrics"
    if n_workouts:
        return "workouts"
    return "empty"


def _parse_document(document: object) -> PayloadReport:
    if not isinstance(document, dict):
        raise MalformedPayload("top-level JSON value must be an object")
    data = document.get("data")
    if not isinstance(data, dict):
        raise MalformedPayload("missing or invalid 'data' object (expected HAE JSON v2 envelope)")
    metrics = data.get("metrics", [])
    workouts = data.get("workouts", [])
    if not isinstance(metrics, list):
        raise MalformedPayload("'data.metrics' must be an array")
    if not isinstance(workouts, list):
        raise MalformedPayload("'data.workouts' must be an array")

    unknown: list[dict[str, str]] = []
    samples = 0
    for group in metrics:
        if isinstance(group, dict) and isinstance(group.get("data"), list):
            samples += len(group["data"])
        anomaly = _inspect_metric_group(group)
        if anomaly is not None:
            unknown.append(anomaly)
    for workout in workouts:
        anomaly = _inspect_workout(workout)
        if anomaly is not None:
            unknown.append(anomaly)

    return PayloadReport(
        data_type=_classify_data_type(len(metrics), len(workouts)),
        metrics=len(metrics),
        samples=samples,
        workouts=len(workouts),
        unknown_formats=tuple(unknown),
    )


def parse(body: bytes | str) -> PayloadReport:
    """Parse one HAE JSON v2 payload body into a :class:`PayloadReport`.

    Pure function: no filesystem, database, or network access. Raises
    :class:`MalformedPayload` when ``body`` is not valid JSON or lacks the
    ``{"data": {...}}`` envelope; all other anomalies are reported in
    ``unknown_formats`` and never reject the payload.
    """
    try:
        document = json.loads(body)
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError subclass ValueError
        raise MalformedPayload(f"payload is not valid JSON: {exc}") from exc
    return _parse_document(document)
