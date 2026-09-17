"""Slice-2 ingest pipeline: parsed HAE payload → PostgreSQL (decomposition
job slice-2-ingest-pipeline).

:func:`ingest_payload` runs the whole store stage in ONE transaction per
payload (all-or-nothing per the interface contract):

1. ``ensure_partitions`` — current + next month exist for the samples being
   inserted (idempotent, advisory-locked; historical months come from
   :func:`health_db.db.partitions.ensure_backfill_range`).
2. Dimension lookup/auto-register — unknown metrics and sources upsert into
   ``metric_dim`` / ``source_dim`` (unknown metric = accepted, pass-through).
   ``metric_dim.canonical_unit`` seeds from :mod:`health_db.normalize`'s
   canonical map; pass-through metrics register with their declared unit.
3. ``raw_samples`` upserts (latest-wins ``ON CONFLICT (metric_id, source_id,
   ts) DO UPDATE SET qty/ingested_at`` — iron law 6: overlapping re-sends
   update rows, they never append; non-canonical-unit rows were already
   skipped by the normalizer, iron law 4 — visibly, in the result's
   ``skipped`` accounting).
4. Day-scoped rollup recompute: ``DELETE FROM daily_rollups WHERE day_kst =
   ANY(days)`` then re-``INSERT ... SELECT ... GROUP BY`` from raw_samples —
   parity with predecessor ingester.py:237-253 (corrected values always
   recompute from the full sample set of the day).
5. Workouts upsert by UUID (latest-wins on re-send) + ``workout_heart_rate``
   + ``routes`` (kJ→kcal conversions already applied by the normalizer).
6. ``raw_ingest`` row (sha256 UNIQUE, headers, verbatim payload jsonb,
   ``outcome='stored'``) — the DB-side dedup signal for replay.

Failure semantics:

- :class:`DbUnavailable` is raised when the pool cannot deliver a connection
  or the server is unreachable. The route maps it to ``503 + Retry-After:
  300`` and the payload is written to the out-of-archive pending queue
  (iron law 2: a DB outage never loses a payload; the payload is already on
  disk in the dated archive before this stage ever runs).
- Any other psycopg error rolls the transaction back (via the pool
  connection context) and propagates as :class:`DbUnavailable` with the
  error redacted to its class name (iron law 1: psycopg messages can embed
  connection settings).

Dedup: the caller (``/api/ingest``) consults the disk-archive sha256 index
first (slice-1 dedup, still the fast path). Inside the store stage the
``raw_ingest`` ``sha256`` UNIQUE constraint is the DB-level guard:
:func:`ingest_payload` treats an existing row as ``outcome='duplicate'``
(no reprocessing) so the response is idempotent even across replays and
process restarts with a cold archive index.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from health_db.db.helpers import _Ids, _parse_headers, recompute_rollups
from health_db.db.partitions import ensure_backfill_range, ensure_partitions
from health_db.normalize import SampleRow, workout_rows
from health_db.payloads import empty_skipped, merge_skipped

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date

    from psycopg.abc import ConnectionType
    from psycopg_pool import ConnectionPool

OUTCOME_STORED = "stored"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_REPLAYED = "replayed"
OUTCOME_REPAIRED = "repaired"

_RAW_INGEST_UNIQUE = "raw_ingest_sha256_key"


class DbUnavailable(Exception):
    """Postgres is unreachable or the transaction failed; payload stays on
    disk (dated archive + pending queue). Never carries connection detail
    (iron law 1) — ``str()`` is a static, safe message."""

    MESSAGE = "database unavailable; payload kept on disk for replay"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.MESSAGE)


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of one stored payload (route-level response contract).

    ``skipped`` is the run-2026-09-17-normalizer-audit §5 visible-skip
    accounting (spec §5 keys, all defaulting to 0, always present): the
    payload's drop census — unit-unconvertible samples, all-zero sleep
    points, unrecognized shapes, dropped ECG/notification rows — merged
    from the normalizer + payloads report by :func:`ingest_payload`.
    Informational only; never blocks the store.
    """

    outcome: str
    metrics: int
    samples: int
    workouts: int
    skipped: dict[str, int] = field(default_factory=empty_skipped)


def _canonical_unit(name: str, declared: str) -> tuple[str, bool]:
    """``(canonical_unit, is_cumulative)`` for a metric_dim registration.

    Mapped metrics get their canonical unit (spec §metric_dim seed rule);
    pass-through metrics (no canonical map entry) seed from their declared
    HAE ``units`` value.
    """
    from health_db.normalize import canonical_unit_of, is_cumulative_unit

    mapped = canonical_unit_of(name)
    if mapped is None:
        return declared, is_cumulative_unit(declared)
    return mapped, is_cumulative_unit(mapped)


def _sample_days(rows: list[SampleRow]) -> frozenset[date]:
    """KST days touched by the sample rows (fixed UTC+9 semantics)."""
    from health_db.normalize import day_kst

    return frozenset(day_kst(row.ts) for row in rows)


def _upsert_samples(conn: ConnectionType, ids: _Ids, rows: list[SampleRow]) -> int:
    """executemany latest-wins upsert; returns the number of rows offered."""
    if rows:
        data = [
            (
                ids.metric_id(conn, row.name),
                ids.source_id(conn, row.source),
                row.ts,
                row.qty,
            )
            for row in rows
        ]
        from health_db.db.helpers import UPSERT_SAMPLE

        with conn.cursor() as cur:
            cur.executemany(UPSERT_SAMPLE, data)
    return len(rows)


def _upsert_workouts(
    conn: ConnectionType,
    parsed: object,
) -> int:
    """Workout upserts by UUID + HR + route rows (latest-wins re-sends)."""
    from psycopg.types.json import Jsonb

    from health_db.db.helpers import UPSERT_HR, UPSERT_ROUTE, UPSERT_WORKOUT

    grouped = workout_rows(parsed)
    for row, hr_rows, route_rows in grouped:
        # psycopg3 cannot adapt a plain dict/list to a jsonb parameter
        # (ProgrammingError 'cannot adapt type dict'): wrap the parsed
        # recovery object explicitly so a non-empty heartRateRecovery list
        # stores as JSON instead of crashing the workout transaction. None
        # stays SQL NULL.
        recovery = Jsonb(row.heart_rate_recovery) if row.heart_rate_recovery is not None else None
        conn.execute(
            UPSERT_WORKOUT,
            (
                row.id,
                row.name,
                row.start_ts,
                row.end_ts,
                row.day_kst,
                row.duration_sec,
                row.active_energy_kcal,
                row.total_energy,
                row.basal_energy,
                row.mets,
                row.temperature,
                row.humidity,
                row.is_indoor,
                row.location,
                row.intensity,
                row.distance_km,
                row.walking_running_distance_km,
                row.step_count,
                row.step_cadence,
                row.speed,
                row.avg_heart_rate,
                row.max_heart_rate,
                recovery,
            ),
        )
        if hr_rows:
            with conn.cursor() as cur:
                cur.executemany(UPSERT_HR, ((row.id, h.ts, h.heart_rate) for h in hr_rows))
        if route_rows:
            with conn.cursor() as cur:
                cur.executemany(
                    UPSERT_ROUTE,
                    (
                        (
                            row.id,
                            r.ts,
                            r.latitude,
                            r.longitude,
                            r.course,
                            r.speed,
                            r.elevation,
                            r.h_acc,
                            r.v_acc,
                        )
                        for r in route_rows
                    ),
                )
    return len(grouped)


def ingest_payload(
    pool: ConnectionPool,
    parsed: object,
    *,
    sha256: str,
    body: bytes,
    headers: Mapping[str, str | None] | None = None,
    outcome: str = OUTCOME_STORED,
    force: bool = False,
) -> IngestResult:
    """Store one parsed payload in a single all-or-nothing transaction.

    ``parsed`` is the decoded JSON document (the same object
    :func:`health_db.payloads.parse` produced). ``sha256`` / ``body`` feed
    the ``raw_ingest`` archive row. ``outcome`` is the value recorded in
    ``raw_ingest.outcome`` for a fresh store (the ingest route records
    ``'stored'``; the replay tools record ``'replayed'`` so a backfill run
    is distinguishable from live syncs in the raw_ingest audit). Returns
    the response-contract outcome; raises :class:`DbUnavailable` when the
    database cannot serve the write.

    ``force`` (repair path): reprocess a payload whose ``sha256`` already
    exists in ``raw_ingest`` instead of short-circuiting to duplicate. The
    normalization + latest-wins upserts are idempotent, so re-running repair
    over an already-repaired payload rewrites the same values and the
    ``raw_ingest`` audit row is updated in place (received_at preserved) with
    outcome ``'repaired'`` — metrics/samples/rollups are untouched.
    """
    from health_db.db.helpers import INSERT_RAW_INGEST, UPSERT_RAW_INGEST

    try:
        with pool.connection(timeout=30.0) as conn:
            existing = conn.execute(
                "SELECT outcome FROM raw_ingest WHERE sha256 = %s", (sha256,)
            ).fetchone()
            if existing is not None and not force:
                # Idempotent re-delivery (replay, restart): report duplicate
                # without reprocessing (spec failure table: "no reprocessing").
                return IngestResult(
                    outcome=existing[0] if isinstance(existing[0], str) else OUTCOME_DUPLICATE,
                    metrics=0,
                    samples=0,
                    workouts=0,
                    skipped=empty_skipped(),
                )

            document = parsed if isinstance(parsed, dict) else {}
            data = document.get("data") if isinstance(document.get("data"), dict) else {}
            metric_groups = data.get("metrics") if isinstance(data.get("metrics"), list) else []
            workout_entries = data.get("workouts") if isinstance(data.get("workouts"), list) else []

            _upsert_workouts(conn, parsed)

            from health_db.normalize import metric_rows_detailed

            # Skip census (spec §5): the metric normalizer reports its own
            # drops (unit_unconvertible, sleep_no_value) alongside the rows;
            # the payloads-parse counters (unrecognized_shape) and the lane
            # counters (ecg, notification) merge in from jobs 5/6's
            # normalizers via the report. Zero silent drops: every dropped
            # point lands in exactly one counter of this dict.
            rows, metric_skips = metric_rows_detailed(parsed)
            skipped = empty_skipped()
            merge_skipped(skipped, metric_skips)
            ids = _Ids()
            for row in rows:
                # register dims before partition create; declared unit
                # seeds pass-through metric_dim rows (spec §metric_dim)
                ids.metric_id(conn, row.name, declared_unit=row.unit)
                ids.source_id(conn, row.source)

            days = _sample_days(rows)
            if days:
                ensure_backfill_range(conn, first_day=min(days), last_day=max(days))
            ensure_partitions(conn)
            stored = _upsert_samples(conn, ids, rows)

            _deleted, _inserted = recompute_rollups(conn, days)

            automation_name, automation_id, session_id = _parse_headers(headers or {})
            recorded_outcome = OUTCOME_REPAIRED if (force and existing is not None) else outcome
            statement = UPSERT_RAW_INGEST if force else INSERT_RAW_INGEST
            conn.execute(
                statement,
                (
                    sha256,
                    automation_name,
                    automation_id,
                    session_id,
                    "metrics" if metric_groups or not workout_entries else "workouts",
                    body.decode("utf-8"),
                    recorded_outcome,
                ),
            )

            return IngestResult(
                outcome=recorded_outcome,
                metrics=len(metric_groups),
                samples=stored,
                workouts=len(workout_entries),
                skipped=skipped,
            )
    except DbUnavailable:
        raise
    except Exception as exc:
        # Pool borrow timeout / server down / SQL failure → 503 path. The
        # message is static (never the exception text — iron law 1); the
        # exception class name is the only observable detail.
        detail = type(exc).__name__
        raise DbUnavailable(f"database unavailable ({detail}); payload kept on disk") from exc


def parse_raw_ingest_unique_violation(exc: Exception) -> bool:
    """True when ``exc`` is the raw_ingest sha256 UNIQUE violation (concurrent
    duplicate) — the caller can then respond idempotently."""
    return _RAW_INGEST_UNIQUE in str(exc)  # pragma: no cover - race-path helper
