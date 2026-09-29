"""Ingest SQL helpers shared by :mod:`health_db.pipeline` and (future) replay.

Plain SQL via psycopg3 (harness: no ORM, SQL lives in ``db/``). The helper
calls are transaction-shaped: they run on an open connection/transaction
owned by the caller and never commit themselves.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

# Unknown-metric/source auto-registration. is_cumulative is seeded from the
# canonical unit (spec §metric_dim): True iff the unit is in CUMULATIVE_UNITS.
# display_name falls back to CamelCase of the snake_case name.
UPSERT_METRIC = """
INSERT INTO metric_dim (name, display_name, canonical_unit, is_cumulative)
VALUES (%s, %s, %s, %s)
ON CONFLICT (name) DO UPDATE SET display_name = EXCLUDED.display_name
RETURNING metric_id
""".strip()

UPSERT_SOURCE = """
INSERT INTO source_dim (name) VALUES (%s)
ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
RETURNING source_id
""".strip()

UPSERT_SAMPLE = """
INSERT INTO raw_samples (metric_id, source_id, ts, qty, ingested_at)
VALUES (%s, %s, %s, %s, now())
ON CONFLICT (metric_id, source_id, ts)
DO UPDATE SET qty = EXCLUDED.qty, ingested_at = EXCLUDED.ingested_at
""".strip()

UPSERT_WORKOUT = """
INSERT INTO workouts (id, name, start_ts, end_ts, day_kst, duration_sec,
                      active_energy_kcal, total_energy, basal_energy, mets,
                      temperature, humidity, is_indoor, location, intensity,
                      distance_km, walking_running_distance_km, step_count,
                      step_cadence, speed, avg_heart_rate, max_heart_rate,
                      heart_rate_recovery, ingested_at)
VALUES (%s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s, %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s, %s,
        %s, now())
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    start_ts = EXCLUDED.start_ts,
    end_ts = EXCLUDED.end_ts,
    day_kst = EXCLUDED.day_kst,
    duration_sec = EXCLUDED.duration_sec,
    active_energy_kcal = EXCLUDED.active_energy_kcal,
    total_energy = EXCLUDED.total_energy,
    basal_energy = EXCLUDED.basal_energy,
    mets = EXCLUDED.mets,
    temperature = EXCLUDED.temperature,
    humidity = EXCLUDED.humidity,
    is_indoor = EXCLUDED.is_indoor,
    location = EXCLUDED.location,
    intensity = EXCLUDED.intensity,
    distance_km = EXCLUDED.distance_km,
    walking_running_distance_km = EXCLUDED.walking_running_distance_km,
    step_count = EXCLUDED.step_count,
    step_cadence = EXCLUDED.step_cadence,
    speed = EXCLUDED.speed,
    avg_heart_rate = EXCLUDED.avg_heart_rate,
    max_heart_rate = EXCLUDED.max_heart_rate,
    heart_rate_recovery = EXCLUDED.heart_rate_recovery,
    ingested_at = EXCLUDED.ingested_at
""".strip()

UPSERT_HR = """
INSERT INTO workout_heart_rate (workout_id, ts, heart_rate)
VALUES (%s, %s, %s)
ON CONFLICT (workout_id, ts)
DO UPDATE SET heart_rate = EXCLUDED.heart_rate
""".strip()

UPSERT_ROUTE = """
INSERT INTO routes (workout_id, ts, latitude, longitude,
                    course, speed, elevation, h_acc, v_acc)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (workout_id, ts) DO UPDATE SET
    course = EXCLUDED.course,
    speed = EXCLUDED.speed,
    elevation = EXCLUDED.elevation,
    h_acc = EXCLUDED.h_acc,
    v_acc = EXCLUDED.v_acc
""".strip()

# ECG + heart-rate-notification lanes (run 2026-09-17-normalizer-audit §2/§3/
# §6): session tables upsert latest-wins on the UNIQUE (source_name, start_ts)
# natural keys RETURNING the DB id — the pipeline resolves the normalizer's
# array-position ``ref`` to that id before inserting child rows (voltages /
# samples), so re-posted sessions attach children to the SAME parent row and
# re-appends never happen (iron law 2).
UPSERT_ECG = """
INSERT INTO ecgs (source_name, start_ts, end_ts, classification,
                  sampling_frequency, average_heart_rate, voltage_count)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (source_name, start_ts) DO UPDATE SET
    end_ts = EXCLUDED.end_ts,
    classification = EXCLUDED.classification,
    sampling_frequency = EXCLUDED.sampling_frequency,
    average_heart_rate = EXCLUDED.average_heart_rate,
    voltage_count = EXCLUDED.voltage_count,
    ingested_at = now()
RETURNING id
""".strip()

UPSERT_ECG_VOLTAGE = """
INSERT INTO ecg_voltages (ecg_id, seq, ts, voltage, units)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (ecg_id, seq) DO UPDATE SET
    ts = EXCLUDED.ts,
    voltage = EXCLUDED.voltage,
    units = EXCLUDED.units
""".strip()

UPSERT_HRN = """
INSERT INTO heart_rate_notifications (source_name, start_ts, end_ts, kind, threshold)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (source_name, start_ts) DO UPDATE SET
    end_ts = EXCLUDED.end_ts,
    kind = EXCLUDED.kind,
    threshold = EXCLUDED.threshold,
    ingested_at = now()
RETURNING id
""".strip()

UPSERT_HRN_SAMPLE = """
INSERT INTO heart_rate_notification_samples (notification_id, ts, kind, value, units)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (notification_id, ts, kind) DO UPDATE SET
    value = EXCLUDED.value,
    units = EXCLUDED.units
""".strip()

# Sleep-session lane (run 2026-09-28-mcp-analysis-parity §1): same latest-wins
# upsert pattern as UPSERT_ECG/UPSERT_HRN on the UNIQUE (source_name, sleep_start)
# natural key RETURNING the DB id. No child rows (executemany is fine in the
# pipeline lane). Column order matches SleepSessionRow (normalize.py); durations
# stay hours verbatim (iron law 3). in_bed columns follow asleep…awake in the
# spec's field order. record_date = payload `date`, day_kst is DB-generated.
UPSERT_SLEEP_SESSION = """
INSERT INTO sleep_sessions (source_name, record_date, sleep_start, sleep_end,
                            in_bed_start, in_bed_end, total_sleep, asleep,
                            deep, core, rem, awake, in_bed)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (source_name, sleep_start) DO UPDATE SET
    record_date = EXCLUDED.record_date,
    sleep_end = EXCLUDED.sleep_end,
    in_bed_start = EXCLUDED.in_bed_start,
    in_bed_end = EXCLUDED.in_bed_end,
    total_sleep = EXCLUDED.total_sleep,
    asleep = EXCLUDED.asleep,
    deep = EXCLUDED.deep,
    core = EXCLUDED.core,
    rem = EXCLUDED.rem,
    awake = EXCLUDED.awake,
    in_bed = EXCLUDED.in_bed,
    ingested_at = now()
RETURNING id
""".strip()

DELETE_ROLLUPS = "DELETE FROM daily_rollups WHERE day_kst = ANY(%s)"

RECOMPUTE_ROLLUPS = """
INSERT INTO daily_rollups (metric_id, day_kst, sum_qty, avg_qty, min_qty,
                           max_qty, sample_count, updated_at)
SELECT metric_id, day_kst,
       sum(qty ORDER BY source_id, ts),
       avg(qty ORDER BY source_id, ts),
       min(qty), max(qty),
       count(*)::bigint, now()
FROM raw_samples
WHERE day_kst = ANY(%s)
GROUP BY metric_id, day_kst
""".strip()

INSERT_RAW_INGEST = """
INSERT INTO raw_ingest (sha256, received_at, automation_name, automation_id,
                        session_id, data_type, payload, outcome)
VALUES (%s, now(), %s, %s, %s, %s, %s, %s)
""".strip()

# Repair-path counterpart: re-processing a sha that already has a raw_ingest
# row must not violate the UNIQUE constraint, so the audit row is updated in
# place (received_at preserved; payload/outcome refreshed).
UPSERT_RAW_INGEST = """
INSERT INTO raw_ingest (sha256, received_at, automation_name, automation_id,
                        session_id, data_type, payload, outcome)
VALUES (%s, now(), %s, %s, %s, %s, %s, %s)
ON CONFLICT (sha256) DO UPDATE SET
    payload = EXCLUDED.payload,
    data_type = EXCLUDED.data_type,
    outcome = EXCLUDED.outcome
""".strip()

# DB-side replay dedup: one round-trip per payload, before any reprocessing.
SELECT_RAW_INGEST_SHA = "SELECT outcome FROM raw_ingest WHERE sha256 = %s"


class _Ids:
    """Per-transaction cache of metric/source ids (dim lookups repeat).

    The lane caches (``ecg_ids``/``hrn_ids``) map the normalizers'
    array-position ``ref`` to the DB ``id`` RETURNed by the parent upsert:
    the ref is unique within one payload's transaction, and children
    (voltages / samples) resolve through it so re-posted payloads attach to
    the same parent row instead of re-appending.
    """

    def __init__(self) -> None:
        self._metrics: dict[str, int] = {}
        self._sources: dict[str, int] = {}
        self.ecg_ids: dict[int, int] = {}
        self.hrn_ids: dict[int, int] = {}

    def metric_id(self, conn: object, name: str, declared_unit: str = "") -> int:
        found = self._metrics.get(name)
        if found is None:
            from health_db.normalize import canonical_unit_of, display_name, is_cumulative_unit

            # Seed rule (spec §metric_dim): mapped metrics take their canonical
            # unit; pass-through metrics seed from the declared HAE unit.
            unit = canonical_unit_of(name)
            if unit is None:
                unit = declared_unit
                cumulative = is_cumulative_unit(declared_unit)
            else:
                cumulative = is_cumulative_unit(unit)
            row = conn.execute(
                UPSERT_METRIC,
                (name, display_name(name), unit, cumulative),
            ).fetchone()
            found = int(row[0])
            self._metrics[name] = found
        return found

    def source_id(self, conn: object, name: str) -> int:
        found = self._sources.get(name)
        if found is None:
            row = conn.execute(UPSERT_SOURCE, (name,)).fetchone()
            found = int(row[0])
            self._sources[name] = found
        return found


def recompute_rollups(conn: object, days: frozenset[date]) -> tuple[int, int]:
    """Day-scoped rollup recompute (predecessor ingester.py:237-253 parity).

    DELETE then re-INSERT every affected KST day inside the caller's
    transaction — the rollup always reflects the full raw_sample set for the
    day after latest-wins corrections, never a partial increment. Returns
    ``(deleted, inserted)`` row counts.

    ``sum``/``avg`` are computed with an explicit ``ORDER BY source_id, ts``
    (the raw_samples PK) so the float8 accumulation order is deterministic.
    Without it PostgreSQL's parallel/hash aggregation order varies with the
    physical row order, which a repair re-run changes (every row is
    re-UPDATE'd → new tuple versions → different heap scan order), making the
    rollup values drift at ULP level between identical replays. Pinning the
    order makes force-repair byte-reproducible (rollup md5 stable across
    consecutive runs) while ``min``/``max``/``count`` are order-insensitive.
    """
    if not days:
        return 0, 0
    ordered = sorted(days)
    deleted = conn.execute(DELETE_ROLLUPS, (ordered,)).rowcount
    inserted = conn.execute(RECOMPUTE_ROLLUPS, (ordered,)).rowcount
    return deleted, inserted


def _parse_headers(headers: Mapping[str, str | None]) -> tuple[str | None, str | None, str | None]:
    """Extract (automation_name, automation_id, session_id) from HAE headers.

    Accepts any string-keyed mapping (case-insensitive HTTP headers or plain
    dicts from tests/replay); values are stripped, blanks become None.
    """

    def grab(name: str) -> str | None:
        for key, value in headers.items():
            if key.lower() == name and value is not None:
                text = value.strip()
                if text:
                    return text
        return None

    return (
        grab("automation-name"),
        grab("automation-id"),
        grab("session-id"),
    )
