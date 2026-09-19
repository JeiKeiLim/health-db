"""HAE JSON v2 payload parsing and validation (pure functions, no I/O).

Slice-1 job ``slice-1-payloads``: parse Health Auto Export JSON v2 REST
payloads into a :class:`PayloadReport` — counts and format classification
only; slice 1 stores nothing (normalization/upsert lands in slice 2).

Envelope (research/hae-json-v2-format.md): ``{"data": {"metrics": [...],
"workouts": [...]}}``. A body is rejected with :class:`MalformedPayload`
(the API layer turns this into a 400 + quarantine) when it:

- is not valid JSON — including invalid UTF-8 bytes, ``NaN``/``Infinity``
  literals, non-finite numbers (e.g. ``1e400``), or nesting deeper than the
  interpreter's recursion limit (``RecursionError`` is caught and classified);
- lacks the ``{"data": {...}}`` envelope or has mistyped ``metrics`` /
  ``workouts`` arrays;
- carries neither metrics nor workouts (empty payload).

Everything else is *reported*, never rejected:

- Unknown metric **names** are always accepted (auto-registered in
  ``metric_dim`` in slice 2); slice 1 keeps no name registry.
- Unknown metric data **shapes** are flagged in ``unknown_formats`` (e.g. a
  future HAE format change) while the payload is still accepted and counted,
  and mirrored into the ``skipped`` accounting's ``unrecognized_shape``
  counter (run 2026-09-17-normalizer-audit §5).

Recognized data-point shapes (shape-based, not name-based, so renamed or
future metrics with a documented shape still parse). Recognition requires
the FULL key set of a shape — partial key overlap (e.g. ``{systolic}``
alone or ``{Avg}`` alone) does NOT count and is reported as unknown:

- common: ``{qty, date, ...}`` (source/mealTime/reason/value extras allowed)
  — also covers blood_glucose + mealTime, insulin_delivery + reason,
  handwashing/toothbrushing + value, sexual_activity.
- sleep_analysis unaggregated: ``{startDate, endDate, qty, value, ...}``.
- heart_rate: ``{Min, Avg, Max, ...}`` (no qty; Avg is the canonical value).
- blood_pressure: ``{systolic, diastolic, ...}`` (no qty).
- sleep_analysis aggregated: ``{date, ...sleep fields...}`` where ANY of
  ``totalSleep/asleep/core/deep/rem/inBed/sleepStart`` alongside ``date``
  suffices (day-level, no qty; run 2026-09-17-normalizer-audit §1 —
  totalSleep is no longer required, the archive corpus carries
  ``{date, asleep, core, deep, rem}`` rows without it).

Counting semantics (receipt counts, per decomposition job slice-1-payloads):

- ``metrics``  = number of metric group entries in ``data.metrics``.
- ``samples``  = total data points across all groups' ``data`` arrays
  (one point = one sample regardless of shape).
- ``workouts`` = number of entries in ``data.workouts``.

``data_type`` stays within the ``raw_ingest.data_type`` contract enum
(``'metrics' | 'workouts'``): ``"metrics"`` whenever any metric groups are
present (metrics take precedence when both arrays are non-empty), else
``"workouts"``. Empty payloads are rejected as malformed, so ``"mixed"`` /
``"empty"`` values never occur.

This module performs no I/O and holds no state: every function is pure and
safe to call concurrently.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

DataType = Literal["metrics", "workouts"]

# Visible-skip accounting keys (run spec §5 — zero-silent-drops contract).
# Frozen set: every dropped data point increments exactly ONE of these
# counters; the object is always present in the ingest response with every
# key defaulting to 0. Counters are informational and never block ingest.
# Producers: unit_unconvertible + sleep_no_value come from the metric
# normalizer's skip census (normalize.metric_rows_detailed); ecg +
# notification from the lane normalizers (jobs 5/6); unrecognized_shape
# mirrors unknown_formats here (count only — the reason strings stay in
# unknown_formats).
SKIPPED_KEYS: tuple[str, ...] = (
    "unit_unconvertible",
    "unrecognized_shape",
    "ecg",
    "notification",
    "sleep_no_value",
)
_SKIPPED_ZERO: dict[str, int] = dict.fromkeys(SKIPPED_KEYS, 0)


def empty_skipped() -> dict[str, int]:
    """A fresh all-zero skip counter with exactly the spec §5 keys.

    Returns a mutable dict — callers own their copy (the shared zero
    template is copied per call, never handed out).
    """
    return dict(_SKIPPED_ZERO)


def merge_skipped(target: dict[str, int], source: Mapping[str, int]) -> dict[str, int]:
    """Fold one skip census into ``target`` (in place, returns ``target``).

    Unknown keys are IGNORED defensively (a malformed census must never
    inject foreign keys into the response accounting) while every known key
    absent from ``source`` stays at its current value. Never raises.
    """
    for key in SKIPPED_KEYS:
        value = source.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            target[key] += value
    return target

# FULL key sets for shape recognition (research/hae-json-v2-format.md). A
# data point matches a shape only when every key of the set is present —
# partial overlap ({systolic} alone, {Avg} alone, ...) is NOT recognition
# and is reported in unknown_formats instead.
_COMMON_KEYS = frozenset({"qty", "date"})
_SLEEP_UNAGGREGATED_KEYS = frozenset({"qty", "value", "startDate", "endDate"})
_BLOOD_PRESSURE_KEYS = frozenset({"systolic", "diastolic"})
_HEART_RATE_KEYS = frozenset({"Min", "Avg", "Max"})
# Sufficient-key-set rule (run 2026-09-17-normalizer-audit §1): recognition =
# keys ⊇ {date} ∧ ∩ sleep field set ≠ ∅ (the unaggregated/common shapes keep
# their earlier precedence). totalSleep alone is no longer required.
_SLEEP_FIELD_KEYS = frozenset(
    {"totalSleep", "asleep", "core", "deep", "rem", "inBed", "sleepStart"}
)


class MalformedPayload(ValueError):
    """Payload is not valid JSON, lacks the HAE v2 ``{"data": {...}}`` envelope, or is empty."""


@dataclass(frozen=True, slots=True)
class PayloadReport:
    """Parse outcome for one HAE JSON v2 payload (report-only; nothing stored).

    ``data_type`` is always ``"metrics"`` or ``"workouts"`` (the
    ``raw_ingest.data_type`` contract enum). ``unknown_formats`` entries are
    JSON-ready dicts with ``name``/``units``/``reason`` keys describing
    metric groups or workouts that deviate from the documented shapes.
    Anomalies never reject the payload.

    ``skipped`` carries the run-2026-09-17-normalizer-audit §5 visible-skip
    accounting with the frozen keys of :data:`SKIPPED_KEYS` (all zero by
    default, always present): every dropped data point increments exactly
    one counter. At parse time only ``unrecognized_shape`` is populated
    (mirroring ``unknown_formats``); the metric-normalizer/lane counters
    merge in downstream (pipeline) so the ingest response reports the
    payload's full skip census. Informational only — never blocks ingest.
    """

    data_type: DataType
    metrics: int
    samples: int
    workouts: int
    unknown_formats: tuple[dict[str, str], ...] = ()
    skipped: dict[str, int] = field(default_factory=empty_skipped)


def _point_shape(point: object) -> str | None:
    """Shape name for one metric data point, or None when unrecognized.

    Recognition requires the FULL key set of a documented shape; a point
    that only partially overlaps (e.g. ``{systolic}`` alone or ``{Avg}``
    alone) is unrecognized and reported in ``unknown_formats``.
    """
    if not isinstance(point, dict):
        return None
    keys = point.keys()
    if _COMMON_KEYS <= keys:
        return "common"
    if _SLEEP_UNAGGREGATED_KEYS <= keys:
        return "sleep_analysis_unaggregated"
    if _BLOOD_PRESSURE_KEYS <= keys:
        return "blood_pressure"
    if _HEART_RATE_KEYS <= keys:
        return "heart_rate"
    if "date" in keys and keys & _SLEEP_FIELD_KEYS:
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


def _classify_data_type(n_metric_groups: int, n_workouts: int) -> DataType:
    """Contract enum only (``'metrics' | 'workouts'``); metrics take precedence.

    Both arrays non-empty (not the per-automation norm, but tolerated) classifies
    as ``"metrics"``; empty payloads never reach here (rejected as malformed).
    """
    if n_metric_groups:
        return "metrics"
    return "workouts"


# Lane keys (run 2026-09-17-normalizer-audit §2/§3): an envelope that would
# otherwise be "empty" (no metrics AND no workouts) is still accepted when it
# carries ECG/notification lane data — a lane-only payload stores real rows
# (ecgs/heart_rate_notifications), so rejecting it would silently drop a
# documented data surface (anti-scenario 1). No real corpus file is
# lane-only, but the door stays open for one.
_LANE_KEYS = ("ecg", "heartRateNotifications")


def _has_lane_data(data: dict[str, object]) -> bool:
    """True when the v2 ``data`` object carries at least one non-empty lane."""
    return any(isinstance(data.get(key), list) and data.get(key) for key in _LANE_KEYS)


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
    if not metrics and not workouts and not _has_lane_data(data):
        raise MalformedPayload("payload carries no metrics and no workouts (empty v2 envelope)")

    unknown: list[dict[str, str]] = []
    skipped = empty_skipped()
    samples = 0
    for group in metrics:
        if isinstance(group, dict) and isinstance(group.get("data"), list):
            samples += len(group["data"])
        anomaly = _inspect_metric_group(group)
        if anomaly is not None:
            unknown.append(anomaly)
            # Mirror into the skipped accounting (count only; the reason
            # strings stay in unknown_formats). spec §5: a dropped point
            # must be visible in exactly one counter.
            skipped["unrecognized_shape"] += 1
    for workout in workouts:
        anomaly = _inspect_workout(workout)
        if anomaly is not None:
            unknown.append(anomaly)
            skipped["unrecognized_shape"] += 1

    return PayloadReport(
        data_type=_classify_data_type(len(metrics), len(workouts)),
        metrics=len(metrics),
        samples=samples,
        workouts=len(workouts),
        unknown_formats=tuple(unknown),
        skipped=skipped,
    )


def _reject_non_finite_constant(token: str) -> float:
    """``json.loads`` parse_constant hook: NaN/Infinity literals are rejected."""
    raise ValueError(f"non-finite number {token} is not allowed")


def _parse_finite_float(token: str) -> float:
    """``json.loads`` parse_float hook: reject floats outside the finite range.

    Catches overflow literals such as ``1e400`` that parse to ``inf`` while
    being syntactically valid JSON.
    """
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"non-finite number {token} is not allowed")
    return value


def parse(body: bytes | str) -> PayloadReport:
    """Parse one HAE JSON v2 payload body into a :class:`PayloadReport`.

    Pure function: no filesystem, database, or network access. Raises
    :class:`MalformedPayload` when ``body`` is not valid JSON (invalid UTF-8,
    ``NaN``/``Infinity``/non-finite numbers, recursion-limit-exceeding
    nesting), lacks the ``{"data": {...}}`` envelope, or carries no metrics
    and no workouts; all other anomalies are reported in ``unknown_formats``
    and never reject the payload.
    """
    try:
        document = json.loads(
            body, parse_constant=_reject_non_finite_constant, parse_float=_parse_finite_float
        )
    except RecursionError as exc:
        raise MalformedPayload("payload JSON nesting is too deep to parse") from exc
    except ValueError as exc:  # JSONDecodeError, UnicodeDecodeError, non-finite guards
        raise MalformedPayload(f"payload is not valid JSON: {exc}") from exc
    return _parse_document(document)
