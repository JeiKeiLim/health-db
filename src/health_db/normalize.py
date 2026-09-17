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
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

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
