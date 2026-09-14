"""Pure normalization of HAE JSON v2 payloads into storage rows.

Slice-2 job ``slice-2-normalize``: converts a parsed HAE JSON v2 document
(the dict produced by ``json.loads`` — slice-1 ``payloads.parse`` already
validated the envelope upstream) into flat row dataclasses ready for
PostgreSQL upsert. Pure functions only: no filesystem, no database, no
network — the pipeline job (``slice-2-ingest-pipeline``) wires these rows
into transactions.

Policies (ported from predecessor; iron laws 4/5):

- Canonical units (predecessor ``constants.py:104-119``, re-keyed to
  the snake_case metric names stored in ``metric_dim``): a sample whose
  declared unit does not match its metric's canonical unit is SKIPPED,
  never converted. The single exception is energy kJ→kcal ÷ 4.184.
- Metrics absent from the canonical map are pass-through: any declared
  unit is accepted, and ``is_cumulative`` seeds as
  ``unit in CUMULATIVE_UNITS`` (predecessor ``constants.py`` parity).
- Special shapes: heart_rate → qty = ``Avg``; blood_pressure → two rows
  (``blood_pressure_systolic`` / ``blood_pressure_diastolic``);
  sleep_analysis aggregated → one row per day, qty = ``asleep`` when
  present else ``core+deep+rem`` (AutoSleep-wins parity with TASK-005);
  sleep_analysis unaggregated → one row per segment.
- ``day_kst`` uses a FIXED UTC+9 offset (KST has no DST) — never a named
  timezone, never server-local time.

Workout field shapes (HAE v2 real payloads vs simplified fixtures):

- Real HAE v2 workouts wrap most scalars in ``{qty, units}`` objects
  (``activeEnergyBurned``, ``totalEnergy``, ``distance``, ``avgHeartRate``,
  ``maxHeartRate``, ``temperature``, ``humidity``, ``intensity``,
  ``stepCadence``, ``speed``) and present several fields as per-minute point
  LISTS (``activeEnergy``, ``basalEnergy``, ``stepCount``,
  ``walkingAndRunningDistance``). The generic :func:`_qty_of` /
  :func:`_sum_qty_of` helpers accept scalar, ``{qty|value}`` dict, and (where
  summing is meaningful) list-of-points shapes, so both the real corpus and
  the simplified test fixtures normalize identically.
- ``activeEnergyBurned`` (dict or scalar) is preferred for active energy;
  a scalar ``activeEnergy`` remains the fixture fallback. Total/basal energy
  and the walking+running distance list are summed before conversion.
- ALL workout energies are stored in **kcal**: ``active_energy_kcal``,
  ``total_energy`` and ``basal_energy`` convert the declared kJ value with
  kJ → kcal ÷ 4.184 (the single allowed conversion). The predecessor stored
  ``total_energy``/``basal_energy`` raw kJ; this module deliberately unifies
  on kcal.
- ``heartRateData`` entries carry ``{date, Avg, Max, Min, units}`` in real
  payloads (and ``{date, bpm}`` in fixtures): the stored ``heart_rate`` is
  ``int(round(Avg))`` with a ``bpm`` fallback.
- ``heartRateRecovery`` is a real point LIST, kept verbatim as a JSON array
  in the ``heart_rate_recovery`` jsonb column.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

KJ_PER_KCAL = 4.184

# Canonical unit per metric (predecessor constants.py:104-119, keyed by
# the snake_case REST names stored in metric_dim). Metrics not listed here
# are pass-through: any declared unit is accepted.
CANONICAL_UNITS: dict[str, str] = {
    "weight_body_mass": "kg",
    "lean_body_mass": "kg",
    "body_fat_percentage": "%",
    "height": "m",
    "walking_running_distance": "km",
    "walking_step_length": "cm",
    "walking_speed": "km/hr",
    "running_speed": "km/hr",
    "stair_speed_up": "m/s",
    "stair_speed_down": "m/s",
    "active_energy": "kcal",
    "basal_energy_burned": "kcal",
    "dietary_energy": "kcal",
    "apple_sleeping_wrist_temperature": "degC",
}

# Energy metrics whose canonical unit is kcal — the only metrics ever
# converted (kJ → kcal ÷ 4.184). Non-kJ, non-kcal declarations are skipped.
ENERGY_METRICS = frozenset({"active_energy", "basal_energy_burned", "dietary_energy"})

# Cumulative units (predecessor constants.py parity): metrics whose
# canonical/declared unit is in this set aggregate with SUM in trends;
# everything else aggregates with AVG.
CUMULATIVE_UNITS = frozenset({"count", "km", "kcal", "kJ", "min", "hr", "mg", "g", "mcg"})

# Blood-pressure derived metrics (spec: NEW behavior, no predecessor port).
BLOOD_PRESSURE_SYSTOLIC = "blood_pressure_systolic"
BLOOD_PRESSURE_DIASTOLIC = "blood_pressure_diastolic"
BP_DISPLAY_SYSTOLIC = "Blood Pressure Systolic"
BP_DISPLAY_DIASTOLIC = "Blood Pressure Diastolic"

# Human-readable display names (predecessor constants.py:21-102 port;
# snake_case → CamelCase). Extended with the blood-pressure splits.
DISPLAY_NAMES: dict[str, str] = {
    "active_energy": "Active Energy",
    "alcohol_consumption": "Alcohol Consumption",
    "apple_exercise_time": "Apple Exercise Time",
    "apple_sleeping_wrist_temperature": "Apple Sleeping Wrist Temperature",
    "apple_stand_hour": "Apple Stand Hour",
    "apple_stand_time": "Apple Stand Time",
    "basal_energy_burned": "Basal Energy Burned",
    "biotin": "Biotin",
    "blood_oxygen_saturation": "Blood Oxygen Saturation",
    "body_fat_percentage": "Body Fat Percentage",
    "body_mass_index": "Body Mass Index",
    "breathing_disturbances": "Breathing Disturbances",
    "caffeine": "Caffeine",
    "calcium": "Calcium",
    "carbohydrates": "Carbohydrates",
    "cardio_recovery": "Cardio Recovery",
    "chloride": "Chloride",
    "cholesterol": "Cholesterol",
    "chromium": "Chromium",
    "copper": "Copper",
    "dietary_energy": "Dietary Energy",
    "dietary_sugar": "Dietary Sugar",
    "environmental_audio_exposure": "Environmental Audio Exposure",
    "fiber": "Fiber",
    "flights_climbed": "Flights Climbed",
    "folate": "Folate",
    "headphone_audio_exposure": "Headphone Audio Exposure",
    "heart_rate": "Heart Rate",
    "heart_rate_variability": "Heart Rate Variability",
    "height": "Height",
    "iodine": "Iodine",
    "iron": "Iron",
    "lean_body_mass": "Lean Body Mass",
    "magnesium": "Magnesium",
    "manganese": "Manganese",
    "mindful_minutes": "Mindful Minutes",
    "molybdenum": "Molybdenum",
    "monounsaturated_fat": "Monounsaturated Fat",
    "niacin": "Niacin",
    "pantothenic_acid": "Pantothenic Acid",
    "phosphorus": "Phosphorus",
    "physical_effort": "Physical Effort",
    "polyunsaturated_fat": "Polyunsaturated Fat",
    "potassium": "Potassium",
    "protein": "Protein",
    "respiratory_rate": "Respiratory Rate",
    "resting_heart_rate": "Resting Heart Rate",
    "riboflavin": "Riboflavin",
    "running_ground_contact_time": "Running Ground Contact Time",
    "running_power": "Running Power",
    "running_speed": "Running Speed",
    "running_stride_length": "Running Stride Length",
    "running_vertical_oscillation": "Running Vertical Oscillation",
    "saturated_fat": "Saturated Fat",
    "selenium": "Selenium",
    "six_minute_walking_test_distance": "Six-Minute Walking Test Distance",
    "sleep_analysis": "Sleep Analysis",
    "sodium": "Sodium",
    "stair_speed_down": "Stair Speed: Down",
    "stair_speed_up": "Stair Speed: Up",
    "step_count": "Step Count",
    "thiamin": "Thiamin",
    "time_in_daylight": "Time in Daylight",
    "total_fat": "Total Fat",
    "vitamin_a": "Vitamin A",
    "vitamin_b12": "Vitamin B12",
    "vitamin_b6": "Vitamin B6",
    "vitamin_c": "Vitamin C",
    "vitamin_d": "Vitamin D",
    "vitamin_e": "Vitamin E",
    "vitamin_k": "Vitamin K",
    "vo2_max": "VO2 Max",
    "walking_asymmetry_percentage": "Walking Asymmetry Percentage",
    "walking_double_support_percentage": "Walking Double Support Percentage",
    "walking_heart_rate_average": "Walking Heart Rate Average",
    "walking_running_distance": "Walking + Running Distance",
    "walking_speed": "Walking Speed",
    "walking_step_length": "Walking Step Length",
    "weight_body_mass": "Weight & Body Mass",
    "zinc": "Zinc",
    BLOOD_PRESSURE_SYSTOLIC: BP_DISPLAY_SYSTOLIC,
    BLOOD_PRESSURE_DIASTOLIC: BP_DISPLAY_DIASTOLIC,
}


def display_name(name: str) -> str:
    """Human-readable display name; unknown names fall back to CamelCase."""
    known = DISPLAY_NAMES.get(name)
    if known is not None:
        return known
    return " ".join(part.capitalize() for part in name.split("_"))


def canonical_unit_of(name: str) -> str | None:
    """Canonical unit for a metric, or None when the metric is pass-through."""
    return CANONICAL_UNITS.get(name)


def is_cumulative_unit(unit: str) -> bool:
    """Seed rule for ``metric_dim.is_cumulative`` (CUMULATIVE_UNITS parity)."""
    return unit in CUMULATIVE_UNITS


def day_kst(ts: datetime) -> date:
    """KST calendar day of ``ts`` — fixed UTC+9, never a named zone.

    Matches the SQL generated column ``((ts AT TIME ZONE 'UTC') + interval
    '9 hours')::date``: the instant is first normalized to UTC (so tz-aware
    inputs in any zone behave identically), then shifted +9h (KST has no
    DST), then the date is taken.
    """
    return (ts.astimezone(timezone.utc) + timedelta(hours=9)).date()


@dataclass(frozen=True, slots=True)
class SampleRow:
    """One ``raw_samples`` row: (metric, source, ts) latest-wins upsert key."""

    name: str
    source: str
    ts: datetime
    qty: float
    unit: str


@dataclass(frozen=True, slots=True)
class WorkoutRow:
    """One ``workouts`` row (upsert key = HAE workout UUID)."""

    id: str
    name: str
    start_ts: datetime
    end_ts: datetime
    day_kst: date
    duration_sec: float
    active_energy_kcal: float | None = None
    total_energy: float | None = None
    basal_energy: float | None = None
    mets: float | None = None
    temperature: float | None = None
    humidity: float | None = None
    is_indoor: bool | None = None
    location: str | None = None
    intensity: float | None = None
    distance_km: float | None = None
    walking_running_distance_km: float | None = None
    step_count: int | None = None
    step_cadence: float | None = None
    speed: float | None = None
    avg_heart_rate: float | None = None
    max_heart_rate: float | None = None
    heart_rate_recovery: object | None = None


@dataclass(frozen=True, slots=True)
class HrRow:
    """One ``workout_heart_rate`` row (PK workout_id + ts)."""

    workout_id: str
    ts: datetime
    heart_rate: int


@dataclass(frozen=True, slots=True)
class RouteRow:
    """One ``routes`` row (PK workout_id + ts)."""

    workout_id: str
    ts: datetime
    latitude: float
    longitude: float
    course: float | None = None
    speed: float | None = None
    elevation: float | None = None
    h_acc: float | None = None
    v_acc: float | None = None


def _num(value: object) -> float | None:
    """Coerce a JSON scalar to float; bools/strings/None are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _qty_of(value: object) -> float | None:
    """Extract a numeric quantity from any HAE v2 workout field shape.

    Accepts a scalar int/float (bools rejected), a ``{qty|value: ...}``
    object (HAE v2 wraps most workout scalars this way), and — where summing
    is meaningful — delegates lists to :func:`_sum_qty_of` so a call site
    cannot accidentally read a per-minute point list as a scalar. Returns
    None for anything unrecognized.
    """
    if isinstance(value, list):
        return _sum_qty_of(value)
    if isinstance(value, dict):
        return _num(value.get("qty", value.get("value")))
    return _num(value)


def _sum_qty_of(value: object) -> float | None:
    """Sum a per-minute point list (``[{qty}, ...]``) or unwrap a single value.

    HAE v2 emits ``activeEnergy``, ``basalEnergy``, ``stepCount`` and
    ``walkingAndRunningDistance`` as lists of ``{date, qty, ...}`` points; the
    predecessor's ``_sum_qty_list`` semantics (empty list → 0.0, dict point →
    its ``qty``, scalar point → itself) are preserved so a field that sums
    meaningfully can never silently yield None. Non-list input delegates to
    :func:`_qty_of` (scalar/dict shapes stay supported for fixture parity).
    """
    if not isinstance(value, list):
        return _qty_of(value)
    total = 0.0
    for point in value:
        qty = _qty_of(point)
        if qty is not None:
            total += qty
    return total


def _parse_ts(value: object) -> datetime | None:
    """Parse an HAE timestamp (``yyyy-MM-dd HH:mm:ss Z`` or ISO 8601).

    Naive timestamps (should not occur in v2) are interpreted as UTC.
    Returns None when unparseable — the sample is then skipped.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _qty_for_unit(name: str, declared_unit: str, qty: float) -> tuple[float, str] | None:
    """Unit-policy gate: canonical match / kJ→kcal conversion / pass-through.

    Returns ``(qty, unit)`` to keep, or None when the sample must be SKIPPED
    (non-canonical unit — iron law 4: never converted, never stored).
    """
    canonical = CANONICAL_UNITS.get(name)
    if canonical is None:
        return qty, declared_unit
    if declared_unit == canonical:
        return qty, canonical
    if name in ENERGY_METRICS and declared_unit == "kJ":
        return qty / KJ_PER_KCAL, "kcal"
    return None


def _source_of(group: dict[str, object], point: dict[str, object]) -> str:
    """Sample source: per-point, else group-level, else ``'unknown'``."""
    for candidate in (point.get("source"), group.get("source")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return "unknown"


def _rows_for_point(group: dict[str, object], point: dict[str, object]) -> Iterable[SampleRow]:
    """Yield zero or more SampleRows for one metric data point.

    Shape dispatch mirrors payloads.py (full-key-set recognition); points
    with unparseable timestamps or unit-policy violations yield nothing.
    """
    name = group.get("name")
    if not isinstance(name, str) or not name:
        return
    units = group.get("units")
    declared_unit = units if isinstance(units, str) else str(units)
    source = _source_of(group, point)
    keys = point.keys()

    if "qty" in keys and "date" in keys:
        qty = _num(point.get("qty"))
        ts = _parse_ts(point.get("date"))
        if qty is None or ts is None:
            return
        gated = _qty_for_unit(name, declared_unit, qty)
        if gated is not None:
            yield SampleRow(name, source, ts, gated[0], gated[1])
        return

    if "systolic" in keys and "diastolic" in keys:
        # Blood pressure splits into two derived metrics (spec: mmHg only).
        ts = _parse_ts(point.get("date"))
        if ts is None or declared_unit != "mmHg":
            return
        systolic = _num(point.get("systolic"))
        diastolic = _num(point.get("diastolic"))
        if systolic is not None:
            yield SampleRow(BLOOD_PRESSURE_SYSTOLIC, source, ts, systolic, "mmHg")
        if diastolic is not None:
            yield SampleRow(BLOOD_PRESSURE_DIASTOLIC, source, ts, diastolic, "mmHg")
        return

    if "Min" in keys and "Avg" in keys and "Max" in keys:
        # Heart rate: Avg is the canonical value (research/hae-json-v2-format.md).
        qty = _num(point.get("Avg"))
        ts = _parse_ts(point.get("date"))
        if qty is None or ts is None:
            return
        yield SampleRow(name, source, ts, qty, declared_unit)
        return

    if "startDate" in keys and "endDate" in keys and "qty" in keys:
        # Sleep unaggregated: one row per segment; `value` is not stored
        # (documented edge case — aggregation ON is the recommended setting).
        qty = _num(point.get("qty"))
        ts = _parse_ts(point.get("startDate"))
        if qty is None or ts is None:
            return
        yield SampleRow(name, source, ts, qty, declared_unit)
        return

    if "totalSleep" in keys:
        # Sleep aggregated (day-level): asleep-preference, else core+deep+rem.
        ts = _parse_ts(point.get("date"))
        if ts is None:
            return
        asleep = _num(point.get("asleep"))
        if asleep is not None:
            yield SampleRow(name, source, ts, asleep, declared_unit)
            return
        parts = [_num(point.get(part)) for part in ("core", "deep", "rem")]
        if any(part is None for part in parts):
            return
        yield SampleRow(name, source, ts, sum(parts), declared_unit)  # type: ignore[arg-type]
        return

    # Unrecognized shape: reported upstream (payloads unknown_formats);
    # normalization drops it silently.
    return


def metric_rows(document: object) -> list[SampleRow]:
    """Normalize ``data.metrics`` of a parsed v2 document into SampleRows.

    Defensive by design: an invalid document or group yields no rows rather
    than raising — envelope validation already happened upstream, and a
    partially-bad payload must still normalize its good parts.
    """
    if not isinstance(document, dict):
        return []
    data = document.get("data")
    if not isinstance(data, dict):
        return []
    metrics = data.get("metrics")
    if not isinstance(metrics, list):
        return []

    rows: list[SampleRow] = []
    for group in metrics:
        if not isinstance(group, dict):
            continue
        points = group.get("data")
        if not isinstance(points, list):
            continue
        for point in points:
            if isinstance(point, dict):
                rows.extend(_rows_for_point(group, point))
    return rows


def _energy_kcal(value: object) -> float | None:
    """Workout energy field: kJ → kcal ÷ 4.184 (the only conversion).

    Accepts the real HAE v2 shapes via :func:`_qty_of` — a ``{qty, units:
    'kJ'}`` dict (``activeEnergyBurned``/``totalEnergy``) or a per-minute
    point list (``activeEnergy``/``basalEnergy``) — and sums list shapes
    before converting.
    """
    qty = _qty_of(value)
    return None if qty is None else qty / KJ_PER_KCAL


def _workout_ts(workout: dict[str, object], *keys: str) -> datetime | None:
    for key in keys:
        ts = _parse_ts(workout.get(key))
        if ts is not None:
            return ts
    return None


def _hr_rows(workout_id: str, heart_rate_data: object) -> list[HrRow]:
    """``heartRateData`` entries → HrRows (bad points skipped).

    Real HAE v2 points are ``{date, Avg, Max, Min, units}`` and store
    ``int(round(Avg))`` (predecessor parity); simplified fixtures use
    ``{date, bpm}`` and remain supported as the fallback.
    """
    if not isinstance(heart_rate_data, list):
        return []
    rows: list[HrRow] = []
    for point in heart_rate_data:
        if not isinstance(point, dict):
            continue
        bpm = _num(point.get("Avg"))
        if bpm is None:
            bpm = _num(point.get("bpm"))
        ts = _parse_ts(point.get("date"))
        if bpm is None or ts is None:
            continue
        rows.append(HrRow(workout_id, ts, int(round(bpm))))
    return rows


def _route_rows(workout_id: str, route: object) -> list[RouteRow]:
    """``route`` entries → RouteRows; latitude/longitude are NOT NULL —
    points missing either are skipped (schema parity)."""
    if not isinstance(route, list):
        return []
    rows: list[RouteRow] = []
    for point in route:
        if not isinstance(point, dict):
            continue
        ts = _parse_ts(point.get("timestamp"))
        latitude = _num(point.get("latitude"))
        longitude = _num(point.get("longitude"))
        if ts is None or latitude is None or longitude is None:
            continue
        rows.append(
            RouteRow(
                workout_id,
                ts,
                latitude,
                longitude,
                course=_num(point.get("course")),
                speed=_num(point.get("speed")),
                elevation=_num(point.get("altitude")),
                h_acc=_num(point.get("horizontalAccuracy")),
                v_acc=_num(point.get("verticalAccuracy")),
            )
        )
    return rows


def _step_count(step_count: object) -> int | None:
    """``stepCount`` may be a scalar, a ``{value|qty}`` object, or a point list.

    Real HAE v2 emits a per-minute list (``[{date, qty, source, units}, ...]``)
    which must be SUMMED (predecessor ``_sum_step_counts`` parity); the
    scalar/object shapes are kept for simplified fixtures.
    """
    qty = _sum_qty_of(step_count)
    return None if qty is None else int(qty)


def _mets(workout: dict[str, object]) -> float | None:
    for key in ("mets", "METs"):
        if key in workout:
            return _num(workout.get(key))
    return None


def _active_energy_kcal(workout: dict[str, object]) -> float | None:
    """Active workout energy in kcal.

    Prefers ``activeEnergyBurned`` (the real HAE v2 ``{qty, units: 'kJ'}``
    object, or a scalar); falls back to a scalar/list ``activeEnergy`` so the
    simplified test fixtures keep working (predecessor parity).
    """
    burned = _qty_of(workout.get("activeEnergyBurned"))
    if burned is not None:
        return burned / KJ_PER_KCAL
    return _energy_kcal(workout.get("activeEnergy"))


def _walking_running_distance(workout: dict[str, object]) -> float | None:
    """``walkingAndRunningDistance`` point list summed in km.

    Accepts the misspelled ``walkingRunningDistance`` key too (fixture alias).
    """
    for key in ("walkingAndRunningDistance", "walkingRunningDistance"):
        if key in workout:
            return _sum_qty_of(workout.get(key))
    return None


def _heart_rate_recovery(value: object) -> object | None:
    """Keep ``heartRateRecovery`` verbatim: real payloads send a JSON array of
    ``{date, Avg, Max, Min, units}`` points (also accept a legacy dict shape),
    and it round-trips through the ``jsonb`` column unchanged."""
    if isinstance(value, (dict, list)):
        return value
    return None


def workout_rows(
    document: object,
) -> list[tuple[WorkoutRow, list[HrRow], list[RouteRow]]]:
    """Normalize ``data.workouts`` into (WorkoutRow, hr_rows, route_rows).

    Workout energies convert kJ → kcal ÷ 4.184. Workouts missing ``id`` or
    a parseable start date are skipped (unparseable UUID-less rows cannot
    upsert latest-wins); nested HR/route points skip individually.
    """
    if not isinstance(document, dict):
        return []
    data = document.get("data")
    if not isinstance(data, dict):
        return []
    workouts = data.get("workouts")
    if not isinstance(workouts, list):
        return []

    result: list[tuple[WorkoutRow, list[HrRow], list[RouteRow]]] = []
    for workout in workouts:
        if not isinstance(workout, dict):
            continue
        workout_id = workout.get("id")
        name = workout.get("name")
        start_ts = _workout_ts(workout, "start", "startDate")
        end_ts = _workout_ts(workout, "end", "endDate")
        duration = _num(workout.get("duration"))
        if not isinstance(workout_id, str) or not workout_id:
            continue
        if not isinstance(name, str):
            name = ""
        if start_ts is None or end_ts is None or duration is None:
            continue
        row = WorkoutRow(
            id=workout_id,
            name=name,
            start_ts=start_ts,
            end_ts=end_ts,
            day_kst=day_kst(start_ts),
            duration_sec=duration,
            active_energy_kcal=_active_energy_kcal(workout),
            total_energy=_energy_kcal(workout.get("totalEnergy")),
            basal_energy=_energy_kcal(workout.get("basalEnergy")),
            mets=_mets(workout),
            temperature=_qty_of(workout.get("temperature")),
            humidity=_qty_of(workout.get("humidity")),
            is_indoor=workout.get("isIndoor")
            if isinstance(workout.get("isIndoor"), bool)
            else None,
            location=workout.get("location") if isinstance(workout.get("location"), str) else None,
            intensity=_qty_of(workout.get("intensity")),
            distance_km=_qty_of(workout.get("distance")),
            walking_running_distance_km=_walking_running_distance(workout),
            step_count=_step_count(workout.get("stepCount")),
            step_cadence=_qty_of(workout.get("stepCadence")),
            speed=_qty_of(workout.get("speed")),
            avg_heart_rate=_qty_of(workout.get("avgHeartRate")),
            max_heart_rate=_qty_of(workout.get("maxHeartRate")),
            heart_rate_recovery=_heart_rate_recovery(workout.get("heartRateRecovery")),
        )
        result.append(
            (
                row,
                _hr_rows(workout_id, workout.get("heartRateData")),
                _route_rows(workout_id, workout.get("route")),
            )
        )
    return result
