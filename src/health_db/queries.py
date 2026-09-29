"""Shared read layer for health-db (job slice-2-mcp-server).

Every read surface — the 8 MCP tools, the 3 MCP resources, and (slice-2-cli)
the thin ``health`` CLI wrapper — calls the functions in this module and
nothing else (decomposition interface contract: "queries — the single read
surface shared by MCP tools + CLI + resources"). All functions return plain
JSON-ready dicts (``date`` objects pre-isoformatted, floats/ints as stored)
so the MCP tool layer can publish them as structured content verbatim.

Concurrency model: tools run as *sync* functions (the SDK executes sync tool
functions on a worker thread via anyio, so a blocking psycopg borrow never
blocks the event loop). Each function borrows a pooled connection per call
(psycopg_pool handles reuse), runs its SQL, and returns dicts.

Pagination contract (spec §MCP): every unbounded series is bounded —
``raw_samples`` pages with ``limit`` (default 1000, hard max 10000) +
``offset``; ``get_metric_series``/``get_trends`` operate on daily_rollups
(one row per metric-day, naturally bounded but still limited for safety).

Actionable errors (spec: "errors are actionable messages"): unknown metrics
raise :class:`UnknownMetric` whose message names the metric and points the
caller at ``list_metrics`` / ``health://metrics``. The MCP tool layer maps
it to an ``is_error`` result the agent can self-correct from.

Freshness (spec scenario 4 "per-metric latest days with median-based
staleness"): the reference day is ``latest_complete_day`` — the newest KST
day D such that all metrics with any data on older days also have data ≤ D
— approximated as the median (middle value of the sorted, deduplicated set
of per-metric ``max(day_kst)``) rounded down to a shared consensus day; a
metric is *stale* when its last day is older than the consensus by more
than :data:`STALENESS_TOLERANCE_DAYS` (1). The median approach tolerates
metrics that legitimately sync at lower cadence (weekly averages, monthly
summaries) without flagging the whole dataset stale.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg.abc import ConnectionType
    from psycopg_pool import ConnectionPool

# Raw-sample pagination bounds (spec §MCP tools: limit default 1000, max 10000).
RAW_DEFAULT_LIMIT = 1000
RAW_MAX_LIMIT = 10000

# ECG/notification listing bounds (run 2026-09-17-normalizer-audit §7: list
# defaults 50/max 500; voltage pages default 1000/max 10000 like raw_samples).
ECG_HRN_DEFAULT_LIMIT = 50
ECG_HRN_MAX_LIMIT = 500
VOLTAGE_DEFAULT_LIMIT = 1000
VOLTAGE_MAX_LIMIT = 10000

# Route paging bounds (run 2026-09-28-mcp-analysis-parity §2: mirror
# get_ecg_detail voltage pages — default 1000, max 10000 like raw_samples).
ROUTE_DEFAULT_LIMIT = 1000
ROUTE_MAX_LIMIT = 10000

# Sleep-session listing bounds (run 2026-09-28-mcp-analysis-parity spec §1:
# default 100, max 500 — same _bounded_limit family as the lane lists).
SLEEP_SESSION_DEFAULT_LIMIT = 100
SLEEP_SESSION_MAX_LIMIT = 500

# Page-offset ceiling: the PostgreSQL OFFSET clause is a bigint, so an
# offset above 2**63 - 1 would surface as a masked NumericValueOutOfRange
# from the SQL layer instead of an actionable message (job-8 eval finding:
# 2**63 through get_ecg_detail) — _validated_offset rejects it before any
# pool borrow. 2**63 - 1 itself is a valid bigint and is accepted (PG
# serves an empty page for it without crashing).
OFFSET_MAX_BIGINT = 2**63 - 1

# A metric is stale when its last day is older than the consensus day by
# more than this many days (spec scenario 4: "median-based staleness").
STALENESS_TOLERANCE_DAYS = 1

# Window bound for get_trends (hard cap so an agent cannot request a
# decade-long scan; 365 days is far beyond any real analysis window).
TRENDS_MAX_WINDOW_DAYS = 365

# Series/rollup page guard: get_metric_series is bounded by the date range,
# but a pathological range (10 years × 100 metrics) must still be bounded.
SERIES_MAX_DAYS = 366 * 5

RESOURCE_METRICS_URI = "health://metrics"
RESOURCE_FRESHNESS_URI = "health://freshness"
RESOURCE_SCHEMA_URI = "health://schema"


class UnknownMetric(Exception):
    """A metric name is not in ``metric_dim``. Actionable by design: the
    message tells the caller exactly how to discover valid names."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"metric '{name}' not found — call list_metrics "
            f"(or read {RESOURCE_METRICS_URI}) for available metrics"
        )


def _resolve_pool(pool: ConnectionPool | None) -> ConnectionPool:
    """Injectable-pool default: the materialized singleton (never a fresh
    build against ambient defaults — parity with the health probe gate)."""
    from health_db.db.pool import existing_pool, get_pool, has_explicit_env

    if pool is not None:
        return pool
    found = existing_pool()
    if found is not None:
        return found
    if has_explicit_env():
        return get_pool()
    raise RuntimeError(
        "no database pool available; set PSQL_* env (see .env.example) "
        "or pass a pool explicitly"
    )


def _dict_rows(conn: ConnectionType, sql: str, params: tuple | list = ()) -> list[dict]:
    """Run a query with ``dict_row``; every helper below funnels through this."""
    import psycopg.rows

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _scalar(conn: ConnectionType, sql: str, params: tuple | list = ()) -> object:
    rows = _dict_rows(conn, sql, params)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def _day(value: object) -> str:
    """date | str → ISO string (JSON-ready)."""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _require_metric(conn: ConnectionType, name: str) -> dict[str, object]:
    """Fetch one metric_dim row or raise :class:`UnknownMetric`."""
    rows = _dict_rows(
        conn,
        """
        SELECT metric_id, name, display_name, canonical_unit, is_cumulative
        FROM metric_dim WHERE name = %s
        """,
        (name,),
    )
    if not rows:
        raise UnknownMetric(name)
    return rows[0]


# ---------------------------------------------------------------------------
# Catalog + freshness
# ---------------------------------------------------------------------------


def list_metrics(pool: ConnectionPool | None = None) -> dict[str, object]:
    """Metric catalog: name, unit, cumulative flag, coverage, first/last day.

    Ordered by name so the output is stable across calls (LLM grounding:
    a repeated ``list_metrics`` must not reshuffle).
    """
    with _resolve_pool(pool).connection() as conn:
        rows = _dict_rows(
            conn,
            """
            SELECT m.name, m.display_name, m.canonical_unit AS unit,
                   m.is_cumulative,
                   MIN(r.day_kst) AS first_day,
                   MAX(r.day_kst) AS last_day
            FROM metric_dim m
            LEFT JOIN raw_samples r ON r.metric_id = m.metric_id
            GROUP BY m.metric_id, m.name, m.display_name, m.canonical_unit,
                     m.is_cumulative
            ORDER BY m.name
            """,
        )
    metrics = [
        {
            "name": row["name"],
            "display_name": row["display_name"],
            "unit": row["unit"],
            "is_cumulative": row["is_cumulative"],
            "first_day": _day(row["first_day"]) if row["first_day"] is not None else None,
            "last_day": _day(row["last_day"]) if row["last_day"] is not None else None,
        }
        for row in rows
    ]
    return {"metrics": metrics, "count": len(metrics)}


def freshness_check(pool: ConnectionPool | None = None) -> dict[str, object]:
    """Per-metric latest days + consensus ``latest_complete_day`` + staleness.

    ``latest_complete_day`` is the consensus day from the per-metric max days
    (median of the sorted set — see module docstring); ``stale`` lists the
    metrics lagging that consensus by more than the tolerance.
    """
    with _resolve_pool(pool).connection() as conn:
        rows = _dict_rows(
            conn,
            """
            SELECT m.name, m.display_name, m.canonical_unit AS unit,
                   MAX(r.day_kst) AS last_day
            FROM metric_dim m
            JOIN raw_samples r ON r.metric_id = m.metric_id
            GROUP BY m.metric_id, m.name, m.display_name, m.canonical_unit
            ORDER BY m.name
            """,
        )
    if not rows:
        return {
            "latest_complete_day": None,
            "stale": [],
            "metrics": [],
            "count": 0,
        }

    last_days: list[date] = []
    per_metric: list[dict[str, object]] = []
    for row in rows:
        day = row["last_day"]
        last_days.append(day if isinstance(day, date) else day)
        per_metric.append(
            {
                "name": row["name"],
                "display_name": row["display_name"],
                "unit": row["unit"],
                "last_day": _day(day),
            }
        )

    ordered = sorted(set(last_days))
    middle = ordered[len(ordered) // 2]
    if len(ordered) % 2 == 0:
        # Even count: pick the EARLIER middle value — a day that has already
        # fully landed for at least half the metrics (the later middle day
        # may still be mid-sync).
        middle = ordered[len(ordered) // 2 - 1]
    consensus = middle.isoformat()
    # Stale rule (spec scenario 4 example: "vo2_max (2 days behind)"): a
    # metric is stale only when it lags the consensus by MORE than the
    # tolerance — one day behind is a normal sync gap, not staleness.
    stale = [
        f"{row['name']} ({(middle - day).days} day(s) behind)"
        for row, day in zip(per_metric, last_days, strict=True)
        if (middle - day).days > STALENESS_TOLERANCE_DAYS
    ]
    return {
        "latest_complete_day": consensus,
        "stale": stale,
        "metrics": per_metric,
        "count": len(per_metric),
    }


# ---------------------------------------------------------------------------
# Day queries
# ---------------------------------------------------------------------------


def query_day(
    day: str,
    metric: str = "",
    pool: ConnectionPool | None = None,
) -> dict[str, object]:
    """Rollup-level summary of one KST day (all metrics, or one when given).

    Mirrors the predecessor CLI ``query-day`` shape: per metric
    ``sum/avg/min/max/count`` from ``daily_rollups``.
    """
    day_iso = _validated_day(day)
    with _resolve_pool(pool).connection() as conn:
        if metric:
            _require_metric(conn, metric)
            rows = _dict_rows(
                conn,
                """
                SELECT m.name, m.display_name, m.canonical_unit AS unit,
                       m.is_cumulative, d.sum_qty, d.avg_qty, d.min_qty,
                       d.max_qty, d.sample_count
                FROM daily_rollups d
                JOIN metric_dim m USING (metric_id)
                WHERE m.name = %s AND d.day_kst = %s
                """,
                (metric, day_iso),
            )
        else:
            rows = _dict_rows(
                conn,
                """
                SELECT m.name, m.display_name, m.canonical_unit AS unit,
                       m.is_cumulative, d.sum_qty, d.avg_qty, d.min_qty,
                       d.max_qty, d.sample_count
                FROM daily_rollups d
                JOIN metric_dim m USING (metric_id)
                WHERE d.day_kst = %s
                ORDER BY m.name
                """,
                (day_iso,),
            )
    entries = [_rollup_entry(row) for row in rows]
    return {"day": day_iso, "metrics": entries, "count": len(entries)}


def _rollup_entry(row: dict[str, object]) -> dict[str, object]:
    """daily_rollups joined row → JSON-ready dict (the shared day shape)."""
    return {
        "name": row["name"],
        "display_name": row["display_name"],
        "unit": row["unit"],
        "is_cumulative": row["is_cumulative"],
        "sum": row["sum_qty"],
        "avg": row["avg_qty"],
        "min": row["min_qty"],
        "max": row["max_qty"],
        "count": row["sample_count"],
    }


def get_metric_series(
    metric: str,
    frm: str,
    to: str,
    pool: ConnectionPool | None = None,
) -> dict[str, object]:
    """Daily series for one metric over an inclusive KST-day range.

    Values come from ``daily_rollups`` (partition-prunable, <1s at 90d);
    cumulative metrics expose their ``sum`` (the trend-friendly number),
    spot metrics their ``avg`` as the point value plus full stats.
    """
    start_iso = _validated_day(frm)
    end_iso = _validated_day(to)
    if end_iso < start_iso:
        raise InvalidParameter(f"invalid range: 'from' {start_iso} is after 'to' {end_iso}")
    with _resolve_pool(pool).connection() as conn:
        meta = _require_metric(conn, metric)
        rows = _dict_rows(
            conn,
            """
            SELECT d.day_kst, d.sum_qty, d.avg_qty, d.min_qty, d.max_qty,
                   d.sample_count
            FROM daily_rollups d
            WHERE d.metric_id = %s AND d.day_kst BETWEEN %s AND %s
            ORDER BY d.day_kst
            """,
            (meta["metric_id"], start_iso, end_iso),
        )
    cumulative = bool(meta["is_cumulative"])
    days = [
        {
            "day": _day(row["day_kst"]),
            "value": row["sum_qty"] if cumulative else row["avg_qty"],
            "sum": row["sum_qty"],
            "avg": row["avg_qty"],
            "min": row["min_qty"],
            "max": row["max_qty"],
            "count": row["sample_count"],
        }
        for row in rows
    ]
    return {
        "metric": metric,
        "unit": meta["canonical_unit"],
        "is_cumulative": cumulative,
        "from": start_iso,
        "to": end_iso,
        "days": days,
        "points": len(days),
    }


def get_trends(
    metric: str,
    window: int,
    pool: ConnectionPool | None = None,
) -> dict[str, object]:
    """Trailing ``window``-day trend for one metric, ending at its last day.

    Aggregation policy (spec §metric_dim, predecessor parity):
    cumulative metrics aggregate with SUM, everything else with AVG.
    """
    if not isinstance(window, int) or window < 1:
        raise ValueError("window must be a positive integer (days)")
    if window > TRENDS_MAX_WINDOW_DAYS:
        raise ValueError(
            f"window must be <= {TRENDS_MAX_WINDOW_DAYS} days (got {window})"
        )
    with _resolve_pool(pool).connection() as conn:
        meta = _require_metric(conn, metric)
        row = _dict_rows(
            conn,
            "SELECT MAX(day_kst) AS last_day FROM daily_rollups WHERE metric_id = %s",
            (meta["metric_id"],),
        )[0]
        last_day = row["last_day"]
        if last_day is None:
            return {
                "metric": metric,
                "unit": meta["canonical_unit"],
                "window": window,
                "days": [],
                "summary": None,
            }
        window_start = date.fromordinal(
            (last_day if isinstance(last_day, date) else date.today()).toordinal()
            - (window - 1)
        )
        rows = _dict_rows(
            conn,
            """
            SELECT d.day_kst, d.sum_qty, d.avg_qty, d.min_qty, d.max_qty,
                   d.sample_count
            FROM daily_rollups d
            WHERE d.metric_id = %s AND d.day_kst >= %s AND d.day_kst <= %s
            ORDER BY d.day_kst
            """,
            (meta["metric_id"], window_start.isoformat(), _day(last_day)),
        )
    cumulative = bool(meta["is_cumulative"])
    days = [
        {
            "day": _day(r["day_kst"]),
            "value": r["sum_qty"] if cumulative else r["avg_qty"],
            "sum": r["sum_qty"],
            "avg": r["avg_qty"],
            "count": r["sample_count"],
        }
        for r in rows
    ]
    values = [d["value"] for d in days if d["value"] is not None]
    total = sum(values) if cumulative else (sum(values) / len(values) if values else None)
    summary: dict[str, object] = {
        "aggregate": "sum" if cumulative else "avg",
        "value": total,
        "unit": meta["canonical_unit"],
        "days_with_data": len(days),
    }
    return {
        "metric": metric,
        "unit": meta["canonical_unit"],
        "is_cumulative": cumulative,
        "window": window,
        "from": _day(window_start),
        "to": _day(last_day),
        "days": days,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Workouts
# ---------------------------------------------------------------------------


def list_workouts(
    start: str = "",
    end: str = "",
    pool: ConnectionPool | None = None,
) -> dict[str, object]:
    """Workouts in [from, to] KST days (inclusive), newest first."""
    where = "WHERE TRUE"
    params: list[object] = []
    if start:
        start_iso = _validated_day(start)
        where += " AND w.day_kst >= %s"
        params.append(start_iso)
    if end:
        end_iso = _validated_day(end)
        where += " AND w.day_kst <= %s"
        params.append(end_iso)
    with _resolve_pool(pool).connection() as conn:
        rows = _dict_rows(
            conn,
            f"""
            SELECT w.id::text AS id, w.name, w.start_ts, w.end_ts, w.day_kst,
                   w.duration_sec, w.active_energy_kcal, w.distance_km,
                   w.avg_heart_rate, w.max_heart_rate,
                   (SELECT count(*) FROM workout_heart_rate h
                     WHERE h.workout_id = w.id) AS hr_points,
                   (SELECT count(*) FROM routes rt
                     WHERE rt.workout_id = w.id) AS route_points
            FROM workouts w
            {where}
            ORDER BY w.start_ts DESC
            LIMIT 1000
            """,
            params,
        )
    workouts = []
    for row in rows:
        workouts.append(
            {
                "id": row["id"],
                "name": row["name"],
                "start": _ts_iso(row["start_ts"]),
                "end": _ts_iso(row["end_ts"]),
                "day": _day(row["day_kst"]),
                "duration_sec": row["duration_sec"],
                "active_energy_kcal": row["active_energy_kcal"],
                "distance_km": row["distance_km"],
                "avg_heart_rate": row["avg_heart_rate"],
                "max_heart_rate": row["max_heart_rate"],
                "hr_points": row["hr_points"],
                "route_points": row["route_points"],
            }
        )
    return {"workouts": workouts, "count": len(workouts)}


def get_workout_detail(
    workout_id: str,
    route_offset: int = 0,
    route_limit: int | None = None,
    pool: ConnectionPool | None = None,
) -> dict[str, object]:
    """One workout + its heart-rate series and a page of route points.

    Route points page in timestamp order (``ORDER BY ts``, the pre-paging
    emission order unchanged): ``route_limit`` clamps to ``[1, 10000]``
    (default 1000 — spec §2, mirroring get_ecg_detail voltage pages;
    ``None`` selects the default); ``route_offset`` follows
    :func:`_validated_offset`; ``next_route_offset`` is ``None`` on the
    last page. All other fields (hr, heart_rate_recovery, weather,
    cadence, speed, energies) stay inline unpaged. Unknown UUIDs raise
    :class:`UnknownWorkout` (actionable: points at ``list_workouts``).
    """
    import uuid as _uuid

    route_offset = _validated_offset(route_offset, "route_offset")
    route_limit = _bounded_limit(
        route_limit, ROUTE_DEFAULT_LIMIT, ROUTE_MAX_LIMIT, "route_limit"
    )
    try:
        parsed = _uuid.UUID(str(workout_id))
    except (ValueError, AttributeError) as exc:
        raise UnknownWorkout(str(workout_id)) from exc
    canonical_id = str(parsed)
    with _resolve_pool(pool).connection() as conn:
        rows = _dict_rows(
            conn,
            """
            SELECT id::text AS id, name, start_ts, end_ts, day_kst,
                   duration_sec, active_energy_kcal, total_energy,
                   basal_energy, mets, temperature, humidity, is_indoor,
                   location, intensity, distance_km,
                   walking_running_distance_km, step_count, step_cadence,
                   speed, avg_heart_rate, max_heart_rate, heart_rate_recovery,
                   (SELECT count(*) FROM routes WHERE workout_id = w.id)
                     AS route_count
            FROM workouts w WHERE w.id = %s
            """,
            (canonical_id,),
        )
        if not rows:
            raise UnknownWorkout(str(workout_id))
        head = rows[0]
        hr_rows = _dict_rows(
            conn,
            """
            SELECT ts, heart_rate FROM workout_heart_rate
            WHERE workout_id = %s ORDER BY ts
            """,
            (canonical_id,),
        )
        route_rows = _dict_rows(
            conn,
            """
            SELECT ts, latitude, longitude, course, speed, elevation,
                   h_acc, v_acc
            FROM routes WHERE workout_id = %s
            ORDER BY ts
            LIMIT %s OFFSET %s
            """,
            (canonical_id, route_limit + 1, route_offset),
        )
    has_more = len(route_rows) > route_limit
    route_rows = route_rows[:route_limit]
    detail = {
        "id": head["id"],
        "name": head["name"],
        "start": _ts_iso(head["start_ts"]),
        "end": _ts_iso(head["end_ts"]),
        "day": _day(head["day_kst"]),
        "duration_sec": head["duration_sec"],
        "active_energy_kcal": head["active_energy_kcal"],
        "total_energy": head["total_energy"],
        "basal_energy": head["basal_energy"],
        "mets": head["mets"],
        "temperature": head["temperature"],
        "humidity": head["humidity"],
        "is_indoor": head["is_indoor"],
        "location": head["location"],
        "intensity": head["intensity"],
        "distance_km": head["distance_km"],
        "walking_running_distance_km": head["walking_running_distance_km"],
        "step_count": head["step_count"],
        "step_cadence": head["step_cadence"],
        "speed": head["speed"],
        "avg_heart_rate": head["avg_heart_rate"],
        "max_heart_rate": head["max_heart_rate"],
        "heart_rate_recovery": head["heart_rate_recovery"],
        "hr": [
            {"ts": _ts_iso(row["ts"]), "bpm": row["heart_rate"]} for row in hr_rows
        ],
        "route": [
            {
                "ts": _ts_iso(row["ts"]),
                "lat": row["latitude"],
                "lon": row["longitude"],
                "course": row["course"],
                "speed": row["speed"],
                "elevation": row["elevation"],
                "h_acc": row["h_acc"],
                "v_acc": row["v_acc"],
            }
            for row in route_rows
        ],
        "route_count": head["route_count"],
        "route_offset": route_offset,
        "route_limit": route_limit,
        "next_route_offset": route_offset + route_limit if has_more else None,
    }
    return detail


class UnknownWorkout(Exception):
    """A workout UUID is not in ``workouts``. Actionable: points at
    ``list_workouts`` for the available range."""

    def __init__(self, workout_id: str) -> None:
        self.workout_id = workout_id
        super().__init__(
            f"workout '{workout_id}' not found — call list_workouts "
            "(or narrow the from/to range) for available workout ids"
        )


def _ts_iso(value: object) -> str | None:
    """Timestamp → ISO 8601 string (JSON-ready); None passes through."""
    return value.isoformat() if value is not None else None  # type: ignore[attr-defined]


def _ts_kst(value: object) -> str | None:
    """timestamptz → ISO 8601 in the FIXED UTC+9 KST offset (iron law 4
    family: never a named zone, never server/session-local — the acceptance
    rows and the consumer's live dates are KST-rendered). None passes
    through."""
    from datetime import timedelta, timezone

    if value is None:
        return None
    return value.astimezone(timezone(timedelta(hours=9))).isoformat()


# ---------------------------------------------------------------------------
# ECG sessions + heart-rate notifications (run 2026-09-17-normalizer-audit §7)
# ---------------------------------------------------------------------------


def _bounded_limit(
    limit: int | None, default: int, maximum: int, label: str
) -> int:
    """Resolve + clamp a list/page ``limit`` into ``[1, maximum]``.

    ``None`` selects ``default`` (spec §7: callers omit the arg to get the
    documented page size); non-int types raise :class:`InvalidParameter`
    (actionable, never a stack trace); out-of-range ints clamp the way
    ``raw_samples`` does. Bools are rejected even though ``bool`` subclasses
    ``int`` (a ``True`` limit is never a page size).
    """
    if limit is None:
        return default
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise InvalidParameter(f"{label} must be an integer")
    if limit < 1:
        return 1
    if limit > maximum:
        return maximum
    return limit


def _day_range_where(alias: str, frm: str, to: str, params: list[object]) -> str:
    """Optional [from, to] KST-day filter fragment (list_workouts contract)."""
    where = "WHERE TRUE"
    if frm:
        where += f" AND {alias}.day_kst >= %s"
        params.append(_validated_day(frm))
    if to:
        where += f" AND {alias}.day_kst <= %s"
        params.append(_validated_day(to))
    return where


def list_ecgs(
    frm: str = "",
    to: str = "",
    limit: int | None = None,
    pool: ConnectionPool | None = None,
) -> dict[str, object]:
    """ECG sessions in [from, to] KST days (inclusive), newest first.

    ``limit`` clamps to ``[1, 500]`` (spec §7: default 50, max 500; ``None``
    selects the 50 default); each row carries the child voltage count
    instead of the voltage payload.
    """
    limit = _bounded_limit(limit, ECG_HRN_DEFAULT_LIMIT, ECG_HRN_MAX_LIMIT, "limit")
    params: list[object] = []
    where = _day_range_where("e", frm, to, params)
    with _resolve_pool(pool).connection() as conn:
        rows = _dict_rows(
            conn,
            f"""
            SELECT e.id::text AS id, e.source_name, e.start_ts, e.end_ts,
                   e.day_kst, e.classification, e.sampling_frequency,
                   e.average_heart_rate, e.voltage_count
            FROM ecgs e
            {where}
            ORDER BY e.start_ts DESC
            LIMIT %s
            """,
            [*params, limit],
        )
    ecgs = [
        {
            "id": row["id"],
            "source": row["source_name"],
            "start": _ts_iso(row["start_ts"]),
            "end": _ts_iso(row["end_ts"]),
            "day": _day(row["day_kst"]),
            "classification": row["classification"],
            "sampling_frequency": row["sampling_frequency"],
            "average_heart_rate": row["average_heart_rate"],
            "voltage_count": row["voltage_count"],
        }
        for row in rows
    ]
    return {"ecgs": ecgs, "count": len(ecgs)}


def get_ecg_detail(
    ecg_id: str,
    voltage_offset: int = 0,
    voltage_limit: int | None = None,
    pool: ConnectionPool | None = None,
) -> dict[str, object]:
    """One ECG session + a page of its voltage samples (array/seq order).

    ``voltage_limit`` clamps to ``[1, 10000]`` (default 1000 — spec §7;
    ``None`` selects the default); ``next_voltage_offset`` is ``None`` on
    the last page. Unknown ids raise :class:`UnknownEcg` (actionable:
    points at ``list_ecgs``).
    """
    ecg_id_num = _validated_lane_id(ecg_id, UnknownEcg)
    voltage_offset = _validated_offset(voltage_offset, "voltage_offset")
    voltage_limit = _bounded_limit(
        voltage_limit, VOLTAGE_DEFAULT_LIMIT, VOLTAGE_MAX_LIMIT, "voltage_limit"
    )
    with _resolve_pool(pool).connection() as conn:
        head_rows = _dict_rows(
            conn,
            """
            SELECT id::text AS id, source_name, start_ts, end_ts, day_kst,
                   classification, sampling_frequency, average_heart_rate,
                   voltage_count
            FROM ecgs WHERE id = %s
            """,
            (ecg_id_num,),
        )
        if not head_rows:
            raise UnknownEcg(str(ecg_id))
        head = head_rows[0]
        voltage_rows = _dict_rows(
            conn,
            """
            SELECT ts, voltage, units
            FROM ecg_voltages
            WHERE ecg_id = %s
            ORDER BY seq
            LIMIT %s OFFSET %s
            """,
            (ecg_id_num, voltage_limit + 1, voltage_offset),
        )
    has_more = len(voltage_rows) > voltage_limit
    voltage_rows = voltage_rows[:voltage_limit]
    return {
        "id": head["id"],
        "source": head["source_name"],
        "start": _ts_iso(head["start_ts"]),
        "end": _ts_iso(head["end_ts"]),
        "day": _day(head["day_kst"]),
        "classification": head["classification"],
        "sampling_frequency": head["sampling_frequency"],
        "average_heart_rate": head["average_heart_rate"],
        "voltage_count": head["voltage_count"],
        "voltage_offset": voltage_offset,
        "voltage_limit": voltage_limit,
        "next_voltage_offset": (
            voltage_offset + voltage_limit if has_more else None
        ),
        "voltages": [
            {
                "ts": _ts_iso(row["ts"]),
                "voltage": row["voltage"],
                "units": row["units"],
            }
            for row in voltage_rows
        ],
    }


class UnknownEcg(Exception):
    """An ECG session id is not in ``ecgs``. Actionable: points at
    ``list_ecgs`` for the available range."""

    def __init__(self, ecg_id: str) -> None:
        self.ecg_id = ecg_id
        super().__init__(
            f"ECG session '{ecg_id}' not found — call list_ecgs "
            "(or narrow the from/to range) for available session ids"
        )


def list_heart_notifications(
    frm: str = "",
    to: str = "",
    limit: int | None = None,
    pool: ConnectionPool | None = None,
) -> dict[str, object]:
    """Heart-rate-notification events in [from, to] KST days, newest first.

    ``limit`` clamps to ``[1, 500]`` (spec §7: default 50, max 500; ``None``
    selects the 50 default); each event embeds its HR/HRV sample rows
    ordered by (ts, kind).
    """
    limit = _bounded_limit(limit, ECG_HRN_DEFAULT_LIMIT, ECG_HRN_MAX_LIMIT, "limit")
    params: list[object] = []
    where = _day_range_where("n", frm, to, params)
    with _resolve_pool(pool).connection() as conn:
        rows = _dict_rows(
            conn,
            f"""
            SELECT n.id::text AS id, n.source_name, n.start_ts, n.end_ts,
                   n.day_kst, n.kind, n.threshold,
                   (SELECT count(*) FROM heart_rate_notification_samples s
                     WHERE s.notification_id = n.id) AS sample_count
            FROM heart_rate_notifications n
            {where}
            ORDER BY n.start_ts DESC
            LIMIT %s
            """,
            [*params, limit],
        )
        ids = [int(row["id"]) for row in rows]
        sample_rows = (
            _dict_rows(
                conn,
                """
                SELECT notification_id::text AS id, ts, kind, value, units
                FROM heart_rate_notification_samples
                WHERE notification_id = ANY(%s::bigint[])
                ORDER BY ts, kind
                """,
                ([ids],),
            )
            if ids
            else []
        )
    samples_by_event: dict[str, list[dict[str, object]]] = {}
    for sample in sample_rows:
        samples_by_event.setdefault(str(sample["id"]), []).append(
            {
                "ts": _ts_iso(sample["ts"]),
                "kind": sample["kind"],
                "value": sample["value"],
                "units": sample["units"],
            }
        )
    notifications = [
        {
            "id": row["id"],
            "source": row["source_name"],
            "start": _ts_iso(row["start_ts"]),
            "end": _ts_iso(row["end_ts"]),
            "day": _day(row["day_kst"]),
            "kind": row["kind"],
            "threshold": row["threshold"],
            "sample_count": row["sample_count"],
            "samples": samples_by_event.get(str(row["id"]), []),
        }
        for row in rows
    ]
    return {"notifications": notifications, "count": len(notifications)}


def _validated_offset(offset: int, label: str) -> int:
    """Non-negative integer page offset (InvalidParameter otherwise).

    Negative values clamp to 0 (a caller asking before the first page gets
    the first page); offsets above the PostgreSQL bigint OFFSET ceiling
    (2**63 - 1) raise :class:`InvalidParameter` — they cannot page anything
    real and would otherwise crash in the SQL layer. 2**63 - 1 itself is
    accepted: PG serves an empty page for it without crashing.
    """
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise InvalidParameter(f"{label} must be an integer")
    if offset > OFFSET_MAX_BIGINT:
        raise InvalidParameter(
            f"{label} must be at most {OFFSET_MAX_BIGINT} "
            "(PostgreSQL OFFSET is a bigint)"
        )
    return offset if offset >= 0 else 0


def _validated_lane_id(value: str, exc_type: type[Exception]) -> int:
    """Parse a lane-row id (integer identity) or raise the caller's error.

    ``exc_type`` is :class:`UnknownEcg` — the same actionable shape the
    workout path uses with :class:`UnknownWorkout`; the exception itself
    owns the lane-named message (id + the listing tool to call).
    """
    try:
        return int(str(value))
    except (ValueError, TypeError) as exc:
        raise exc_type(str(value)) from exc


# ---------------------------------------------------------------------------
# Sleep sessions (run 2026-09-28-mcp-analysis-parity §1)
# ---------------------------------------------------------------------------


def list_sleep_sessions(
    frm: str = "",
    to: str = "",
    limit: int | None = None,
    offset: int = 0,
    pool: ConnectionPool | None = None,
) -> dict[str, object]:
    """Sleep-session rows in [from, to] KST days (inclusive), newest first.

    One row per recognized aggregated-sleep record (Watch nights, AutoSleep
    naps/dozes, in-bed-era rows) — per-source rows are never merged and no
    nap filtering happens server-side; durations stay hours verbatim (the
    ``asleep: 0`` Watch sentinel stores 0.0). Timestamps render in the
    fixed KST offset (iron law 4). ``limit`` clamps to
    ``[1, 500]`` (default 100; ``None`` selects the default); ``offset``
    follows :func:`_validated_offset`; ``next_offset`` is ``None`` on the
    last page so an LLM knows to stop paging (raw_samples/list_workouts
    house convention).
    """
    limit = _bounded_limit(
        limit, SLEEP_SESSION_DEFAULT_LIMIT, SLEEP_SESSION_MAX_LIMIT, "limit"
    )
    offset = _validated_offset(offset, "offset")
    params: list[object] = []
    where = _day_range_where("s", frm, to, params)
    with _resolve_pool(pool).connection() as conn:
        rows = _dict_rows(
            conn,
            f"""
            SELECT s.id::text AS id, s.source_name, s.day_kst,
                   s.sleep_start, s.sleep_end, s.in_bed_start, s.in_bed_end,
                   s.total_sleep, s.asleep, s.deep, s.core, s.rem,
                   s.awake, s.in_bed
            FROM sleep_sessions s
            {where}
            ORDER BY s.sleep_start DESC
            LIMIT %s OFFSET %s
            """,
            [*params, limit + 1, offset],
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    sessions = [
        {
            "id": row["id"],
            "source": row["source_name"],
            "day": _day(row["day_kst"]),
            "sleep_start": _ts_kst(row["sleep_start"]),
            "sleep_end": _ts_kst(row["sleep_end"]),
            "in_bed_start": _ts_kst(row["in_bed_start"]),
            "in_bed_end": _ts_kst(row["in_bed_end"]),
            "total_sleep": row["total_sleep"],
            "asleep": row["asleep"],
            "deep": row["deep"],
            "core": row["core"],
            "rem": row["rem"],
            "awake": row["awake"],
            "in_bed": row["in_bed"],
        }
        for row in rows
    ]
    return {
        "sessions": sessions,
        "count": len(sessions),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if has_more else None,
    }


# ---------------------------------------------------------------------------
# Raw samples (paginated)
# ---------------------------------------------------------------------------


def raw_samples(
    metric: str = "",
    start: str = "",
    end: str = "",
    limit: int = RAW_DEFAULT_LIMIT,
    offset: int = 0,
    pool: ConnectionPool | None = None,
) -> dict[str, object]:
    """One page of raw sample rows, newest-last, with pagination metadata.

    ``limit`` clamps to ``[1, 10000]`` (spec: default 1000, max 10000);
    ``next_offset`` is ``None`` when the page is the last one so an LLM
    knows to stop paging.
    """
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise InvalidParameter("limit must be an integer")
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise InvalidParameter("offset must be an integer")
    if limit < 1:
        limit = 1
    if limit > RAW_MAX_LIMIT:
        limit = RAW_MAX_LIMIT
    if offset < 0:
        offset = 0

    where = "WHERE TRUE"
    params: list[object] = []
    if metric:
        where += " AND m.name = %s"
        params.append(metric)
    if start:
        where += " AND r.day_kst >= %s"
        params.append(_validated_day(start))
    if end:
        where += " AND r.day_kst <= %s"
        params.append(_validated_day(end))

    with _resolve_pool(pool).connection() as conn:
        if metric:
            _require_metric(conn, metric)
        rows = _dict_rows(
            conn,
            f"""
            SELECT m.name, s.name AS source, r.ts, r.qty, r.day_kst
            FROM raw_samples r
            JOIN metric_dim m USING (metric_id)
            JOIN source_dim s USING (source_id)
            {where}
            ORDER BY r.ts
            LIMIT %s OFFSET %s
            """,
            [*params, limit + 1, offset],
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    page = [
        {
            "metric": row["name"],
            "source": row["source"],
            "ts": _ts_iso(row["ts"]),
            "qty": row["qty"],
            "day": _day(row["day_kst"]),
        }
        for row in rows
    ]
    return {
        "rows": page,
        "limit": limit,
        "offset": offset,
        "returned": len(page),
        "next_offset": offset + limit if has_more else None,
    }


# ---------------------------------------------------------------------------
# Validation + schema resource
# ---------------------------------------------------------------------------


class InvalidParameter(Exception):
    """A tool argument is malformed (bad date, non-positive window, ...).
    Actionable by design: the message states the expected format/range."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _validated_day(value: str) -> str:
    """YYYY-MM-DD check (actionable error text); returns the ISO string."""
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (ValueError, TypeError) as exc:
        raise InvalidParameter(
            f"invalid date {value!r} — use YYYY-MM-DD (e.g. 2026-09-08)"
        ) from exc


_SCHEMA_TABLES: tuple[dict[str, object], ...] = (
    {
        "table": "metric_dim",
        "description": "metric catalog: name, canonical unit, cumulative flag",
        "key": "metric_id (identity PK); name UNIQUE",
    },
    {
        "table": "source_dim",
        "description": "sample sources (Apple Watch, iPhone, ...; 'unknown' when omitted)",
        "key": "source_id (identity PK); name UNIQUE",
    },
    {
        "table": "raw_samples",
        "description": (
            "one row per sample; PARTITION BY RANGE (ts) monthly; "
            "day_kst = KST day generated with fixed +9h"
        ),
        "key": "PK (metric_id, source_id, ts) — latest-wins upserts",
    },
    {
        "table": "daily_rollups",
        "description": "KST-day aggregates per metric (sum/avg/min/max/count)",
        "key": "PK (metric_id, day_kst); recomputed per affected day on ingest",
    },
    {
        "table": "workouts",
        "description": "workouts upserted by HAE UUID (latest-wins)",
        "key": "PK id (uuid); day_kst is the KST day of start_ts",
    },
    {
        "table": "workout_heart_rate",
        "description": "workout HR series (bpm per timestamp)",
        "key": "PK (workout_id, ts); FK → workouts ON DELETE CASCADE",
    },
    {
        "table": "routes",
        "description": "workout GPS points (lat/lon + course/speed/elevation/accuracy)",
        "key": "PK (workout_id, ts); FK → workouts ON DELETE CASCADE",
    },
    {
        "table": "raw_ingest",
        "description": "verbatim payload archive in DB (sha256 UNIQUE dedup, outcome)",
        "key": "PK id (bigserial); sha256 UNIQUE",
    },
    {
        "table": "ecgs",
        "description": "ECG sessions (classification, sampling freq, avg HR, voltage count)",
        "key": "PK id (identity); UNIQUE (source_name, start_ts) — latest-wins",
    },
    {
        "table": "ecg_voltages",
        "description": "ECG voltage samples (mcV) at UNIX-epoch timestamps, array order",
        "key": "PK (ecg_id, seq); FK → ecgs ON DELETE CASCADE",
    },
    {
        "table": "heart_rate_notifications",
        "description": "high-heart-rate notification events (kind, threshold, window)",
        "key": "PK id (identity); UNIQUE (source_name, start_ts) — latest-wins",
    },
    {
        "table": "heart_rate_notification_samples",
        "description": "notification HR/HRV samples (kind 'hr'|'hrv'; one ts may carry both)",
        "key": "PK (notification_id, ts, kind); FK → heart_rate_notifications ON DELETE CASCADE",
    },
    {
        "table": "sleep_sessions",
        "description": (
            "one row per recognized aggregated-sleep record (Watch nights, "
            "AutoSleep naps/dozes, in-bed-era; per-source rows never "
            "merged); day_kst = KST day generated with fixed +9h; durations "
            "are hours verbatim (asleep 0 sentinel stored, not re-derived)"
        ),
        "key": "PK id (identity); UNIQUE (source_name, sleep_start) — latest-wins; "
        "out of daily_rollups",
    },
    {
        "table": "auth_log",
        "description": "401 audit (ts, ip, user_agent, reason — never secrets)",
        "key": "PK id (bigserial)",
    },
)


def schema_resource(pool: ConnectionPool | None = None) -> dict[str, object]:
    """Table/column summary for ``health://schema`` (static shape, no DB).

    ``pool`` is accepted (and unused) for call-site uniformity with the
    other query helpers — the table catalog is static, so no DB round-trip
    is needed regardless.
    """
    return {
        "schema": "health-db",
        "tables": [dict(table) for table in _SCHEMA_TABLES],
        "notes": [
            "timestamps are timestamptz UTC",
            "day_kst is always the KST calendar day (fixed UTC+9)",
            "metrics are HAE snake_case names; units follow metric_dim.canonical_unit",
            "query tools read daily_rollups (fast); raw_samples is the paginated detail",
        ],
    }
