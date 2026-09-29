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
  declared unit does not match its metric's canonical unit is CONVERTED
  when an exact factor exists (run 2026-09-17-normalizer-audit §4 — the
  user-approved iron-law-4 amendment): lb→kg ÷ 2.20462, st→kg ÷ 0.157473,
  °F→°C (°F−32)×5/9 for ``apple_sleeping_wrist_temperature``, and the
  pre-existing kJ→kcal ÷ 4.184. Converted rows store the canonical unit
  string. Units with no exact factor are still SKIPPED — but counted as
  ``unit_unconvertible`` (spec §5 skip accounting), never silent and
  never stored next to canonical rows.
- Metrics absent from the canonical map are pass-through: any declared
  unit is accepted, and ``is_cumulative`` seeds as
  ``unit in CUMULATIVE_UNITS`` (predecessor ``constants.py`` parity).
- Special shapes: heart_rate → qty = ``Avg``; blood_pressure → two rows
  (``blood_pressure_systolic`` / ``blood_pressure_diastolic``);
  sleep_analysis aggregated → one row per day, value precedence
  ``totalSleep>0`` → ``asleep>0`` → ``core+deep+rem`` → inBed>0 → a
  ``sleep_in_bed`` row → skip (run 2026-09-17-normalizer-audit: the REST
  Watch sentinel is ``asleep: 0`` on 1,150/1,200 backfill nights while
  49/50 mixed-stage nights carry a REAL asleep stage with
  ``totalSleep = core+deep+rem+asleep`` exactly, so ``totalSleep`` has
  absolute precedence and ``asleep: 0`` is never a value — see run
  research/predecessor-inventory.md); sleep_analysis unaggregated → one
  row per segment.
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

Session lane (run 2026-09-17-normalizer-audit §2): ``data.ecg`` → ``ecgs`` +
``ecg_voltages`` (:func:`_ecg_rows`; session rows like workouts — OUT of
``raw_samples``/``daily_rollups``). Voltage timestamps are UNIX EPOCH
SECONDS (``datetime.fromtimestamp(..., tz=UTC)`` — verified 42/42 real
backfill sessions: first voltage == session start ±2s under this reading;
CF-absolute would yield year-2056 values, and the predecessor CF epoch
applies only to the retired .hae lane, never REST ECG). Dropped sessions and
dropped voltage samples are COUNTED (``ecg`` skip-reason, spec §5), never
silent.

Notification lane (run 2026-09-17-normalizer-audit §3):
``data.heartRateNotifications`` → ``heart_rate_notifications`` +
``heart_rate_notification_samples`` (:func:`_hrn_rows`; session rows like
workouts — OUT of ``raw_samples``/``daily_rollups``). Event start/end are
ISO-8601 Z strings (:func:`_parse_ts`); each sample's timestamp is its
``timestamp.start``. Value/unit keys are pinned from the real backfill
corpus: HR reads ``heartRate``/``hrUnits``, HRV reads ``hrv``/``units``;
sample ``kind`` is ``'hr'`` vs ``'hrv'`` (a single ts may carry both, so
kind joins the samples PK, spec §6). Dropped events and dropped samples
are COUNTED (``notification`` skip-reason, spec §5), never silent.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from math import isfinite

KJ_PER_KCAL = 4.184

# Exact conversion factors (run 2026-09-17-normalizer-audit §4 — iron-law-4
# amendment, user-approved): canonical-unit metrics declaring one of these
# units are CONVERTED to the canonical unit, never skipped. Factors are
# exact by definition (1 lb = 0.45359237 kg, 1 st = 6.35029318 kg; °F→°C is
# the exact linear scale transform). kJ→kcal ÷ KJ_PER_KCAL predates the
# amendment and is unchanged.
LB_PER_KG = 2.20462
ST_PER_KG = 0.157473

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

# Energy metrics whose canonical unit is kcal — converted from kJ
# (kJ → kcal ÷ 4.184). Non-kJ, non-kcal declarations are skipped.
ENERGY_METRICS = frozenset({"active_energy", "basal_energy_burned", "dietary_energy"})

# Skip-reason key (run spec §5): a canonical-unit metric declared in a unit
# with NO exact conversion factor is dropped, and the drop is VISIBLE —
# counted here, surfaced by job-4's PayloadReport.skipped ingest-response
# accounting. Zero silent drops: every such sample increments this counter.
UNIT_UNCONVERTIBLE = "unit_unconvertible"

# Cumulative units (predecessor constants.py parity): metrics whose
# canonical/declared unit is in this set aggregate with SUM in trends;
# everything else aggregates with AVG.
CUMULATIVE_UNITS = frozenset({"count", "km", "kcal", "kJ", "min", "hr", "mg", "g", "mcg"})

# Blood-pressure derived metrics (spec: NEW behavior, no predecessor port).
BLOOD_PRESSURE_SYSTOLIC = "blood_pressure_systolic"
BLOOD_PRESSURE_DIASTOLIC = "blood_pressure_diastolic"
BP_DISPLAY_SYSTOLIC = "Blood Pressure Systolic"
BP_DISPLAY_DIASTOLIC = "Blood Pressure Diastolic"

# In-bed sleep metric (run 2026-09-17-normalizer-audit): in-bed hours with no
# stage data (2018-era 시계/iPhone rows, 520/2,500 backfill rows) land here
# instead of polluting sleep_analysis with zero rows. Plain pass-through
# metric — deliberately NOT in CANONICAL_UNITS.
SLEEP_IN_BED = "sleep_in_bed"
SLEEP_IN_BED_DISPLAY = "Sleep In-Bed"

# Skip reason for sleep-shaped points that carry no sleep value at all
# (every field 0/absent and inBed 0/absent). Counted by job-4's
# `skipped.sleep_no_value` ingest-response accounting (run spec §5) — the
# zero-silent-drops contract. Never counted as `unrecognized_shape`: the
# shape IS recognized; the point simply carries no value.
SLEEP_NO_VALUE = "sleep_no_value"

# Sleep-field key sets (run 2026-09-17-normalizer-audit §1): a point is
# sleep-shaped when it carries `date` plus ANY sleep field. totalSleep is no
# longer required — the archive corpus contains {date, asleep, core, deep,
# rem} rows without it (scenario 9). sleepStart alone (no values) is also a
# recognized sleep shape; it yields no row and counts as sleep_no_value.
_SLEEP_FIELD_KEYS = frozenset(
    {"totalSleep", "asleep", "core", "deep", "rem", "inBed", "sleepStart"}
)

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
    "sleep_in_bed": "Sleep In-Bed",
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


@dataclass(frozen=True, slots=True)
class EcgRow:
    """One ``ecgs`` row (upsert key = (source_name, start_ts), UNIQUE).

    ``ref`` is the session's index within ``data.ecg`` (the stable
    array-position reference linking this row to its voltage rows) —
    job-7's pipeline resolves it to the DB ``id`` after the session upsert.
    ``voltage_count`` is the number of voltage points the session declared
    via ``numberOfVoltageMeasurements`` (NOT the parsed array length: the
    real corpus keeps them equal, and the pipeline must detect any drift
    rather than silently re-counting).
    """

    ref: int
    source_name: str
    start_ts: datetime
    end_ts: datetime
    classification: str | None = None
    sampling_frequency: int | None = None
    average_heart_rate: int | None = None
    voltage_count: int = 0


@dataclass(frozen=True, slots=True)
class EcgVoltageRow:
    """One ``ecg_voltages`` row (PK (ecg_id, seq); ``ecg_id`` = parent ``ref``).

    ``ts`` is UNIX-epoch converted (see module docstring — verified 42/42
    against the backfill corpus); ``voltage`` stays mcV pass-through; ``seq``
    is the index in the payload's ``voltageMeasurements`` array.
    """

    ecg_ref: int
    seq: int
    ts: datetime
    voltage: float
    units: str


@dataclass(frozen=True, slots=True)
class HrnRow:
    """One ``heart_rate_notifications`` row (upsert key = (source_name,
    start_ts), UNIQUE).

    ``ref`` is the event's index within ``data.heartRateNotifications``
    (the stable array-position reference linking this row to its sample
    rows) — job-7's pipeline resolves it to the DB ``id`` after the event
    upsert. ``kind`` is the raw ``heartNotification`` string (real corpus:
    ``high_heart_rate_notification``); ``threshold`` is the nullable bpm
    trigger (spec §6: nullable column).
    """

    ref: int
    source_name: str
    start_ts: datetime
    end_ts: datetime
    kind: str
    threshold: int | None = None


@dataclass(frozen=True, slots=True)
class HrnSampleRow:
    """One ``heart_rate_notification_samples`` row (PK (notification_id, ts,
    kind); ``notification_id`` = parent ``ref``).

    ``kind`` is ``'hr'`` or ``'hrv'`` — a single ts may carry both kinds
    (spec §6), so kind joins the PK. Value/unit keys are pinned from the
    real corpus: HR ``heartRate``/``hrUnits``, HRV ``hrv``/``units``; ``ts``
    is the sample's ``timestamp.start``.
    """

    hrn_ref: int
    ts: datetime
    kind: str
    value: float
    units: str | None = None


@dataclass(frozen=True, slots=True)
class SleepSessionRow:
    """One ``sleep_sessions`` row (upsert key = (source_name, sleep_start),
    UNIQUE; run 2026-09-28-mcp-analysis-parity spec §1).

    One row per recognized aggregated-sleep record — every payload point the
    existing branch yields a ``sleep_analysis`` or ``sleep_in_bed`` row for
    (Watch nights, AutoSleep naps/dozes, in-bed-era rows). Per-source rows
    are NEVER merged and no nap filtering happens server-side (the consumer
    applies its own 16:00–18:59 exclusion). ALL fields are payload truth
    VERBATIM (durations in hours): the ``asleep: 0`` Watch sentinel (1,150 /
    1,200 backfill nights) stores 0.0, mixed-stage nights store their real
    ``asleep`` — no re-derivation, no zero-filling in either direction.
    Durations come from ``_num``; timestamps from ``_parse_ts``.
    """

    source_name: str
    record_date: datetime
    sleep_start: datetime
    sleep_end: datetime | None = None
    in_bed_start: datetime | None = None
    in_bed_end: datetime | None = None
    total_sleep: float | None = None
    asleep: float | None = None
    deep: float | None = None
    core: float | None = None
    rem: float | None = None
    awake: float | None = None
    in_bed: float | None = None


# Skip-reason key (run spec §5): an ECG session dropped for an unparseable
# start (cannot upsert latest-wins without (source, start)) or an empty
# voltageMeasurements array (a session without samples carries no data).
# Counted by job-4's `skipped.ecg` ingest-response accounting — never silent.
ECG_SKIP = "ecg"

# Skip-reason key (run spec §5): a notification dropped for an unparseable
# start (the (source_name, start_ts) natural key needs it — cannot upsert
# latest-wins). Counted by job-4's `skipped.notification` ingest-response
# accounting — never silent.
NOTIFICATION_SKIP = "notification"

# Sample kinds (spec §3): HR and HRV sample lists hang off each event; a
# single ts may carry both, so kind joins the samples PK (spec §6).
HRN_KIND_HR = "hr"
HRN_KIND_HRV = "hrv"

# INTEGER column bounds (spec §6): threshold is a Postgres integer (4
# bytes, signed range [-2147483648, 2147483647]). JSON-legal finite floats
# like 1e308 convert to 309-digit ints that cannot fit; NaN converts
# to int only to raise ValueError — _hrn_event_fields drops+counts those
# events instead of letting the job-7 upsert fail the whole transaction.
HRN_INT_FIELD_MIN = -(2**31)
HRN_INT_FIELD_MAX = 2**31 - 1


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


@dataclass(frozen=True, slots=True)
class UnitDecision:
    """Outcome of the unit-policy gate for one sample (pure, no I/O).

    ``row`` is a ``(qty, unit)`` tuple to store (unit is always the
    canonical string when a conversion happened); it is None exactly when
    ``skip_reason`` is set. ``skip_reason`` values are job-4 accounting
    keys (spec §5) — currently only :data:`UNIT_UNCONVERTIBLE`.
    """

    row: tuple[float, str] | None
    skip_reason: str | None = None


_UNIT_CONVERSIONS: dict[str, dict[str, tuple[str, float]]] = {
    # metric → declared unit → (canonical unit, exact factor)
    "weight_body_mass": {"lb": ("kg", LB_PER_KG), "st": ("kg", ST_PER_KG)},
    "lean_body_mass": {"lb": ("kg", LB_PER_KG), "st": ("kg", ST_PER_KG)},
    "apple_sleeping_wrist_temperature": {"degF": ("degC", None)},
}


def _qty_for_unit(name: str, declared_unit: str, qty: float) -> UnitDecision:
    """Unit-policy gate (iron law 4, amended run 2026-09-17-normalizer-audit).

    Precedence: canonical match keeps the value as-is; a non-canonical
    declared unit is CONVERTED when an exact factor exists — lb→kg ÷ 2.20462
    and st→kg ÷ 0.157473 for the mass metrics, °F→°C (°F−32)×5/9 for
    ``apple_sleeping_wrist_temperature`` only, and the pre-existing
    kJ→kcal ÷ 4.184 for the energy metrics. Converted rows store the
    canonical unit string. Units with no exact factor are skipped VISIBLY:
    the returned decision carries ``skip_reason='unit_unconvertible'`` so
    the caller can count it (spec §5) instead of dropping silently.
    Pure: no I/O, no state.
    """
    canonical = CANONICAL_UNITS.get(name)
    if canonical is None:
        return UnitDecision(row=(qty, declared_unit))
    if declared_unit == canonical:
        return UnitDecision(row=(qty, canonical))
    if name in ENERGY_METRICS and declared_unit == "kJ":
        return UnitDecision(row=(qty / KJ_PER_KCAL, "kcal"))
    conversion = _UNIT_CONVERSIONS.get(name, {}).get(declared_unit)
    if conversion is not None:
        target, factor = conversion
        if factor is None:
            # °F→°C: exact linear scale transform, wrist temperature only.
            return UnitDecision(row=((qty - 32) * 5 / 9, target))
        return UnitDecision(row=(qty / factor, target))
    return UnitDecision(row=None, skip_reason=UNIT_UNCONVERTIBLE)


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
        decision = _qty_for_unit(name, declared_unit, qty)
        if decision.row is not None:
            yield SampleRow(name, source, ts, decision.row[0], decision.row[1])
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

    if "date" in keys and keys & _SLEEP_FIELD_KEYS:
        # Sleep aggregated (day-level), run 2026-09-17-normalizer-audit §1.
        # Dispatch requires only `date` + ANY sleep field — totalSleep is no
        # longer mandatory (scenario 9: {date, asleep, core, deep, rem} rows
        # exist in the archive corpus without it). Common {qty, date} and
        # unaggregated {startDate, endDate, qty} keep their earlier
        # precedence above.
        ts = _parse_ts(point.get("date"))
        if ts is None:
            return
        # Value precedence (research/predecessor-inventory.md — universal
        # rule, verified outcomes cited per clause):
        #   totalSleep>0 wins absolutely: 1,150/1,200 Watch nights carry the
        #   asleep:0 sentinel (storing asleep would yield 0.0) and 50/50
        #   mixed-stage nights have totalSleep == core+deep+rem+asleep
        #   exactly, so asleep is a REAL STAGE there — it must neither
        #   override totalSleep (would double count) nor be summed with the
        #   stages (loses it).
        total_sleep = _num(point.get("totalSleep"))
        if total_sleep is not None and total_sleep > 0:
            yield SampleRow(name, source, ts, total_sleep, declared_unit)
        else:
            # asleep>0 next: AutoSleep nap rows carry asleep only (nap
            # 0.5833…). The 0-sentinel on Watch rows never wins.
            asleep = _num(point.get("asleep"))
            if asleep is not None and asleep > 0:
                yield SampleRow(name, source, ts, asleep, declared_unit)
            else:
                # core+deep+rem last: all three present and summing > 0.
                parts = [_num(point.get(part)) for part in ("core", "deep", "rem")]
                if all(part is not None for part in parts) and sum(parts) > 0:
                    yield SampleRow(name, source, ts, sum(parts), declared_unit)  # type: ignore[arg-type]
        # In-bed split (independent of the value rule above): inBed hours
        # are wall-clock bed time, never sleep, so they NEVER land in
        # sleep_analysis — a stage/total value above may coexist with an
        # inBed>0 row (test_inbed_plus_stages_emits_both). 520/2,500
        # backfill rows + 1 recent row are in-bed-only: they emit ONLY the
        # sleep_in_bed row below and zero sleep_analysis rows.
        in_bed = _num(point.get("inBed"))
        if in_bed is not None and in_bed > 0:
            yield SampleRow(SLEEP_IN_BED, source, ts, in_bed, declared_unit)
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
    return metric_rows_detailed(document)[0]


def metric_rows_detailed(
    document: object,
) -> tuple[list[SampleRow], dict[str, int]]:
    """Normalize ``data.metrics`` into SampleRows + a skip-reason census.

    The returned dict counts every dropped point under its reason key
    (run spec §5 — zero-silent-drops contract). Key ``sleep_no_value``
    counts sleep-shaped points whose every sleep field is 0/absent AND
    whose inBed is 0/absent: the point is recognized but carries no sleep
    value at all, so no row exists for it. The dict is always present
    (empty when nothing was skipped); keys are frozen by spec §5.
    This function reports ``sleep_no_value`` (its own lane) and
    ``unit_unconvertible`` (from the unit-policy gate's decisions);
    ``unrecognized_shape / ecg / notification`` are counted by their own
    jobs. Pure: no I/O, no state.
    """
    skips: dict[str, int] = {}
    if not isinstance(document, dict):
        return [], skips
    data = document.get("data")
    if not isinstance(data, dict):
        return [], skips
    metrics = data.get("metrics")
    if not isinstance(metrics, list):
        return [], skips

    rows: list[SampleRow] = []
    for group in metrics:
        if not isinstance(group, dict):
            continue
        points = group.get("data")
        if not isinstance(points, list):
            continue
        name = group.get("name")
        units = group.get("units")
        declared_unit = units if isinstance(units, str) else str(units)
        for point in points:
            if isinstance(point, dict):
                emitted = list(_rows_for_point(group, point))
                if not emitted and "qty" in point.keys() and "date" in point.keys():
                    # qty/date shape produced no row: bad qty/ts yield
                    # nothing observable (pre-existing), but a canonical
                    # metric in an unconvertible unit must be dropped
                    # VISIBLY — the reason comes from the gate itself
                    # (spec §5 zero-silent-drops contract).
                    qty = _num(point.get("qty"))
                    if qty is not None and _parse_ts(point.get("date")) is not None:
                        gate_name = name if isinstance(name, str) else ""
                        decision = _qty_for_unit(gate_name, declared_unit, qty)
                        if decision.skip_reason is not None:
                            skips[decision.skip_reason] = skips.get(decision.skip_reason, 0) + 1
                if _is_sleep_point(point) and not any(r.name != SLEEP_IN_BED for r in emitted):
                    # No sleep_analysis row came out of a recognized sleep
                    # point: either it is in-bed-only (a legit split, no
                    # skip) or it carried no value at all (counted).
                    if not any(r.name == SLEEP_IN_BED for r in emitted):
                        skips[SLEEP_NO_VALUE] = skips.get(SLEEP_NO_VALUE, 0) + 1
                rows.extend(emitted)
    return rows, skips


def _is_sleep_point(point: dict[str, object]) -> bool:
    """True when a point has `date` plus at least one sleep field.

    Mirrors the aggregated-sleep dispatch guard in :func:`_rows_for_point`
    so skip accounting can classify points the branch recognized.
    """
    keys = point.keys()
    return "date" in keys and bool(keys & _SLEEP_FIELD_KEYS)


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


def _ecg_session_fields(
    session: dict[str, object],
) -> tuple[datetime, datetime, str | None, int | None, int | None, int] | None:
    """Extract the scalars of one ECG session, or None when it must be skipped.

    ``start``/``end`` are HAE ``yyyy-MM-dd HH:mm:ss Z`` strings parsed by
    :func:`_parse_ts`; a session missing a parseable start or end cannot
    upsert latest-wins (the (source_name, start_ts) natural key needs the
    start; end_ts is NOT NULL per spec §6), so it is dropped and counted
    (``skipped.ecg``, spec §5). ``samplingFrequency`` and
    ``averageHeartRate`` are optional scalars (spec §6: nullable columns);
    ``numberOfVoltageMeasurements`` falls back to 0 — the pipeline upsert
    stores the parsed array length as authoritative when the count is 0.
    """
    start = _parse_ts(session.get("start"))
    if start is None:
        return None
    end = _parse_ts(session.get("end"))
    if end is None:
        return None
    classification = session.get("classification")
    frequency = _num(session.get("samplingFrequency"))
    avg_hr = _num(session.get("averageHeartRate"))
    declared = _num(session.get("numberOfVoltageMeasurements"))
    return (
        start,
        end,
        classification if isinstance(classification, str) and classification else None,
        int(frequency) if frequency is not None else None,
        int(avg_hr) if avg_hr is not None else None,
        int(declared) if declared is not None else 0,
    )


def _ecg_voltage_rows(
    ecg_ref: int, measurements: object
) -> tuple[list[EcgVoltageRow], int]:
    """``voltageMeasurements`` → (EcgVoltageRows, dropped_count).

    Voltage ``date`` is a UNIX epoch SECONDS number (verified 42/42: first
    voltage == session start ±2s under this reading; CF-absolute would give
    year-2056 — the predecessor CF epoch belongs to the retired .hae lane,
    NOT REST ECG). Non-numeric/missing date or voltage values drop the
    individual point (counted) while the rest of the array is kept: a
    partially-corrupt session must not poison its good samples.
    """
    rows: list[EcgVoltageRow] = []
    dropped = 0
    if not isinstance(measurements, list):
        return [], 0
    for seq, point in enumerate(measurements):
        if not isinstance(point, dict):
            dropped += 1
            continue
        date_value = point.get("date")
        if isinstance(date_value, bool) or not isinstance(date_value, (int, float)):
            dropped += 1
            continue
        # Huge-int voltage literals (10**400 is JSON-legal) overflow float(),
        # and NaN/inf values are not storable sample magnitudes — guard
        # exactly like the sibling HRN value conversion (spec §5: drop the
        # point, count it, never crash the payload).
        try:
            voltage = _num(point.get("voltage"))
        except (OverflowError, OSError, ValueError):
            dropped += 1
            continue
        if voltage is None or not isfinite(voltage):
            dropped += 1
            continue
        units = point.get("units")
        # JSON-legal but unrepresentable epochs (NaN/inf/1e300/huge) must
        # drop the point, not crash the payload (zero-silent-drop: counted).
        try:
            ts = datetime.fromtimestamp(date_value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            dropped += 1
            continue
        rows.append(
            EcgVoltageRow(
                ecg_ref=ecg_ref,
                seq=seq,
                ts=ts,
                voltage=voltage,
                units=units if isinstance(units, str) and units else "mcV",
            )
        )
    return rows, dropped


def _ecg_rows(document: object) -> tuple[list[EcgRow], list[EcgVoltageRow], dict[str, int]]:
    """Normalize ``data.ecg`` into (EcgRows, EcgVoltageRows, skip census).

    Session lanes (like workouts): rows go to the ``ecgs``/``ecg_voltages``
    tables via job-7's upserts — OUT of ``raw_samples`` and
    ``daily_rollups``. ``ref`` is the session's index in ``data.ecg``; the
    pipeline resolves it to the DB ``id`` after the parent upsert, so
    voltage rows never need a DB round-trip here (pure, no I/O).

    Skips (counted under ``ecg``, spec §5): sessions with an unparseable
    start/end, sessions whose parsed voltage array is empty (nothing to
    store), and malformed voltage points. ``source`` is the string
    ``source`` field (real corpus: ``"심전도"``); a non-string source is
    skipped like any other unusable natural-key half. Empty/non-list
    ``data.ecg`` yields nothing with no skips — an absent lane is not a
    drop. Pure: no I/O, no state.
    """
    skips: dict[str, int] = {}
    if not isinstance(document, dict):
        return [], [], skips
    data = document.get("data")
    if not isinstance(data, dict):
        return [], [], skips
    sessions = data.get("ecg")
    if not isinstance(sessions, list):
        return [], [], skips

    session_rows: list[EcgRow] = []
    voltage_rows: list[EcgVoltageRow] = []
    dropped_total = 0
    for ref, session in enumerate(sessions):
        if not isinstance(session, dict):
            dropped_total += 1
            continue
        source_name = session.get("source")
        if not isinstance(source_name, str) or not source_name:
            dropped_total += 1
            continue
        fields = _ecg_session_fields(session)
        if fields is None:
            dropped_total += 1
            continue
        start, end, classification, frequency, avg_hr, declared_count = fields
        parsed_voltages, dropped_voltages = _ecg_voltage_rows(
            ref, session.get("voltageMeasurements")
        )
        if not parsed_voltages:
            # Empty (or fully malformed) voltageMeasurements: a session
            # without samples carries no data — skip the session row too.
            dropped_total += 1 + dropped_voltages
            continue
        dropped_total += dropped_voltages
        session_rows.append(
            EcgRow(
                ref=ref,
                source_name=source_name,
                start_ts=start,
                end_ts=end,
                classification=classification,
                sampling_frequency=frequency,
                average_heart_rate=avg_hr,
                # Declared count is authoritative on the real corpus (42/42
                # sessions carry it); the parsed length covers a payload
                # that omits the field.
                voltage_count=declared_count or len(parsed_voltages),
            )
        )
        voltage_rows.extend(parsed_voltages)
    if dropped_total:
        # Sparse census parity with metric_rows_detailed: zero drops →
        # empty dict; job-4's merge_skipped treats absent keys as 0.
        skips[ECG_SKIP] = dropped_total
    return session_rows, voltage_rows, skips


# Voltage insert batching cap (spec §8: executemany batches ≤10k rows per
# statement — the 42-session backfill carries 600,000 voltages; the
# pipeline (job-7) chunks with this limit).
ECG_VOLTAGE_BATCH = 10_000


def _hrn_sample_rows(
    hrn_ref: int,
    samples: object,
    *,
    kind: str,
    value_key: str,
    unit_key: str,
) -> tuple[list[HrnSampleRow], int]:
    """One event's sample list → (HrnSampleRows, dropped_count).

    ``kind`` distinguishes the two lists ('hr' vs 'hrv'); the value/unit
    keys are pinned per kind from the real backfill corpus: HR reads
    ``heartRate``/``hrUnits``, HRV reads ``hrv``/``units`` (spec §3). ``ts``
    is the sample's ``timestamp.start`` via :func:`_parse_ts`. Malformed
    points (non-dict, missing/None value, unparseable ts) drop the
    individual sample (counted) while good siblings are kept: a
    partially-corrupt event must not poison its good samples.

    Value coercion is overflow-guarded: JSON ints parse natively (bypassing
    payloads' parse_float/parse_constant guards), so a huge integer literal
    like ``10**400`` reaches :func:`_num` and ``float()`` raises
    OverflowError — the point drops + counts under ``notification``
    (mirroring :func:`_ecg_voltage_rows`' epoch guard) instead of crashing
    the payload (zero-silent-drops, never-crash discipline).
    """
    rows: list[HrnSampleRow] = []
    dropped = 0
    if not isinstance(samples, list):
        return [], 0
    for point in samples:
        if not isinstance(point, dict):
            dropped += 1
            continue
        try:
            value = _num(point.get(value_key))
        except OverflowError:
            dropped += 1
            continue
        if value is None:
            dropped += 1
            continue
        ts_payload = point.get("timestamp")
        ts_start = ts_payload.get("start") if isinstance(ts_payload, dict) else None
        ts = _parse_ts(ts_start)
        if ts is None:
            dropped += 1
            continue
        units = point.get(unit_key)
        rows.append(
            HrnSampleRow(
                hrn_ref=hrn_ref,
                ts=ts,
                kind=kind,
                value=value,
                units=units if isinstance(units, str) and units else None,
            )
        )
    return rows, dropped


def _hrn_event_fields(
    event: dict[str, object],
) -> tuple[datetime, datetime, str, int | None] | None:
    """Extract the scalars of one notification event, or None when skipped.

    ``start``/``end`` are ISO-8601 Z strings parsed by :func:`_parse_ts`
    (demo corpus: ``2026-06-16T04:39:20Z``). An event missing a parseable
    start or end cannot upsert latest-wins ((source_name, start_ts) needs
    the start; end_ts is NOT NULL per spec §6), so it is dropped and
    counted (``skipped.notification``, spec §5). ``kind`` is the raw
    ``heartNotification`` string — NOT NULL per spec §6, so a missing
    non-string kind drops the event. ``threshold`` is optional (nullable
    column) and magnitude-guarded: ints bypass the JSON parse guards, so
    a huge literal (``10**400``) overflows ``float()``, a finite ``1e308``
    widens to a 309-digit int that cannot fit the INTEGER threshold column
    (spec §6), and a NaN literal raises ``ValueError`` on ``int(nan)`` —
    either way the event drops + counts under ``notification`` (mirroring
    :func:`_ecg_voltage_rows`' epoch guard), keeping the job-7 transaction
    crash-free. The bound check is the SIGNED INT32 range, so a valid
    INT32_MIN threshold (-2147483648) is kept, not dropped.
    """
    start = _parse_ts(event.get("start"))
    if start is None:
        return None
    end = _parse_ts(event.get("end"))
    if end is None:
        return None
    kind = event.get("heartNotification")
    if not isinstance(kind, str) or not kind:
        return None
    # JSON-legal but unrepresentable magnitudes (huge int → OverflowError,
    # finite 1e308 → 309-digit int, NaN → ValueError on int(nan)) must
    # drop the event, not crash the payload or the later upsert
    # (zero-silent-drop: counted).
    try:
        threshold = _num(event.get("threshold"))
        threshold_int = int(threshold) if threshold is not None else None
    except (OverflowError, ValueError):
        return None
    if threshold_int is not None and not (
        HRN_INT_FIELD_MIN <= threshold_int <= HRN_INT_FIELD_MAX
    ):
        return None
    return start, end, kind, threshold_int


def _hrn_rows(document: object) -> tuple[list[HrnRow], list[HrnSampleRow], dict[str, int]]:
    """Normalize ``data.heartRateNotifications`` into (HrnRows, HrnSampleRows,
    skip census).

    Session lanes (like workouts): rows go to the
    ``heart_rate_notifications``/``heart_rate_notification_samples`` tables
    via job-7's upserts — OUT of ``raw_samples`` and ``daily_rollups``.
    ``ref`` is the event's index in ``data.heartRateNotifications``; the
    pipeline resolves it to the DB ``id`` after the parent upsert, so
    sample rows never need a DB round-trip here (pure, no I/O).

    Skips (counted under ``notification``, spec §5): events with an
    unparseable start/end, a missing/non-string ``heartNotification`` kind,
    and malformed individual samples (missing/None value, unparseable
    timestamp). ``source`` is the ``{name, identifier}`` object — ``name``
    feeds the natural key; a missing/non-string name is skipped like any
    other unusable natural-key half. Empty/non-list lane yields nothing
    with no skips — an absent lane is not a drop. Pure: no I/O, no state.
    """
    skips: dict[str, int] = {}
    if not isinstance(document, dict):
        return [], [], skips
    data = document.get("data")
    if not isinstance(data, dict):
        return [], [], skips
    events = data.get("heartRateNotifications")
    if not isinstance(events, list):
        return [], [], skips

    event_rows: list[HrnRow] = []
    sample_rows: list[HrnSampleRow] = []
    dropped_total = 0
    for ref, event in enumerate(events):
        if not isinstance(event, dict):
            dropped_total += 1
            continue
        source_payload = event.get("source")
        source_name = source_payload.get("name") if isinstance(source_payload, dict) else None
        if not isinstance(source_name, str) or not source_name:
            dropped_total += 1
            continue
        fields = _hrn_event_fields(event)
        if fields is None:
            dropped_total += 1
            continue
        start, end, kind, threshold = fields
        event_rows.append(
            HrnRow(
                ref=ref,
                source_name=source_name,
                start_ts=start,
                end_ts=end,
                kind=kind,
                threshold=threshold,
            )
        )
        hr_parsed, hr_dropped = _hrn_sample_rows(
            ref,
            event.get("heartRateData"),
            kind=HRN_KIND_HR,
            value_key="heartRate",
            unit_key="hrUnits",
        )
        hrv_parsed, hrv_dropped = _hrn_sample_rows(
            ref,
            event.get("hrvData"),
            kind=HRN_KIND_HRV,
            value_key="hrv",
            unit_key="units",
        )
        dropped_total += hr_dropped + hrv_dropped
        sample_rows.extend(hr_parsed)
        sample_rows.extend(hrv_parsed)
    if dropped_total:
        # Sparse census parity with metric_rows_detailed/_ecg_rows: zero
        # drops → empty dict; job-4's merge_skipped treats absent keys as 0.
        skips[NOTIFICATION_SKIP] = dropped_total
    return event_rows, sample_rows, skips


# Skip-reason key (mcp-analysis-parity spec §1): a recognized aggregated-
# sleep point without a parseable ``sleepStart`` cannot join the
# sleep_sessions lane's (source_name, sleep_start) latest-wins upsert key —
# dropped and counted under ``skipped.sleep_session`` (zero-silent-drops
# contract, iron law 8). The existing sleep_no_value / sleep_analysis /
# sleep_in_bed emissions are untouched (census lock is the guard).
SLEEP_SESSION_SKIP = "sleep_session"

# Payload keys read verbatim into SleepSessionRow (mcp-analysis-parity
# spec §1): timestamps via _parse_ts, durations via _num.
_SLEEP_SESSION_TS_KEYS = {
    "sleep_start": "sleepStart",
    "sleep_end": "sleepEnd",
    "in_bed_start": "inBedStart",
    "in_bed_end": "inBedEnd",
}
_SLEEP_SESSION_NUM_KEYS = {
    "total_sleep": "totalSleep",
    "asleep": "asleep",
    "deep": "deep",
    "core": "core",
    "rem": "rem",
    "awake": "awake",
    "in_bed": "inBed",
}


def _sleep_session_rows(document: object) -> tuple[list[SleepSessionRow], dict[str, int]]:
    """Normalize ``data.metrics`` sleep-shaped points into (SleepSessionRows,
    skip census) — the parallel session lane (mcp-analysis-parity spec §1).

    Recognition is IDENTICAL to the existing aggregated-sleep dispatch
    (``date`` key + ∩ :data:`_SLEEP_FIELD_KEYS` ≠ ∅, the guard
    ``payloads._point_shape`` / ``_rows_for_point`` use): points matching
    earlier-precedence shapes ({qty, date}, {startDate, endDate, qty}) are
    NOT session records and yield nothing here.

    One row per recognized record — per-source rows are NEVER merged and no
    nap filtering happens server-side. Fields are stored VERBATIM (no
    re-derivation, no zero-filling): the Watch ``asleep: 0`` sentinel stays
    0.0, mixed-stage nights keep their real ``asleep``. A point without a
    parseable ``sleepStart`` yields NO row and counts under
    ``skipped.sleep_session`` (spec §1 — the natural key needs the start).
    Duration coercion is overflow-guarded like the sibling lanes
    (:func:`_hrn_sample_rows`, :func:`_hrn_event_fields`): JSON ints parse
    natively (bypassing payloads' parse_float/parse_constant guards), so a
    huge integer literal (10**400) in a duration field raises OverflowError
    in :func:`_num`; the point drops + counts under ``sleep_session``
    instead of crashing the payload.
    Existing sleep_analysis/sleep_in_bed emissions are untouched; census
    parity lives in tests/unit/test_census_lock.py. Pure: no I/O, no state.
    """
    skips: dict[str, int] = {}
    if not isinstance(document, dict):
        return [], skips
    data = document.get("data")
    if not isinstance(data, dict):
        return [], skips
    metrics = data.get("metrics")
    if not isinstance(metrics, list):
        return [], skips

    rows: list[SleepSessionRow] = []
    for group in metrics:
        if not isinstance(group, dict):
            continue
        points = group.get("data")
        if not isinstance(points, list):
            continue
        for point in points:
            if not isinstance(point, dict):
                continue
            keys = point.keys()
            # Same recognition guard as _rows_for_point's aggregated-sleep
            # branch (and earlier shapes keep their precedence — a {qty,
            # date} point is not a session record even when a sleep field
            # rides along).
            if "qty" in keys and "date" in keys:
                continue
            if "startDate" in keys and "endDate" in keys and "qty" in keys:
                continue
            if not ("date" in keys and keys & _SLEEP_FIELD_KEYS):
                continue
            source = _source_of(group, point)
            record_date = _parse_ts(point.get("date"))
            sleep_start = _parse_ts(point.get("sleepStart"))
            if record_date is None or sleep_start is None:
                # record_date is NOT NULL and sleep_start is the upsert
                # key's second half — either missing means no row, and the
                # drop is VISIBLE (skipped.sleep_session, spec §1).
                skips[SLEEP_SESSION_SKIP] = skips.get(SLEEP_SESSION_SKIP, 0) + 1
                continue
            timestamps = {
                field: _parse_ts(point.get(key)) for field, key in _SLEEP_SESSION_TS_KEYS.items()
            }
            # Overflow-guarded coercion, mirroring _hrn_sample_rows /
            # _hrn_event_fields: JSON ints parse natively (bypassing
            # payloads' parse_float/parse_constant guards), so a huge
            # integer literal like 10**400 in a duration field reaches
            # _num and float() raises OverflowError — the point drops and
            # counts under skipped.sleep_session (zero-silent-drops,
            # never-crash discipline) instead of crashing the upsert.
            # No partial row: one corrupt stage makes the record unusable
            # verbatim (a NaN-like stage cannot be quietly repaired).
            try:
                durations = {
                    field: _num(point.get(key)) for field, key in _SLEEP_SESSION_NUM_KEYS.items()
                }
            except OverflowError:
                skips[SLEEP_SESSION_SKIP] = skips.get(SLEEP_SESSION_SKIP, 0) + 1
                continue
            rows.append(
                SleepSessionRow(
                    source_name=source,
                    record_date=record_date,
                    sleep_start=sleep_start,
                    sleep_end=timestamps["sleep_end"],
                    in_bed_start=timestamps["in_bed_start"],
                    in_bed_end=timestamps["in_bed_end"],
                    total_sleep=durations["total_sleep"],
                    asleep=durations["asleep"],
                    deep=durations["deep"],
                    core=durations["core"],
                    rem=durations["rem"],
                    awake=durations["awake"],
                    in_bed=durations["in_bed"],
                )
            )
    return rows, skips
