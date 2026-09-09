"""Partition maintenance for ``raw_samples`` (job slice-2-schema).

Declarative partitioning never auto-creates partitions (research
postgres18-schema.md note 3): an insert into a range with no partition fails.
:func:`ensure_partitions` pre-creates the CURRENT and NEXT month partitions
(idempotent, advisory-locked) so hourly syncs and backfills never hit a
missing-partition error. It is safe to call at every ingest and from
concurrent workers: the ``pg_advisory_xact_lock`` serializes creators and the
``CREATE TABLE IF NOT EXISTS`` no-ops when the partition already exists.

Naming: ``raw_samples_pYYYY_MM`` (e.g. ``raw_samples_p2026_09``), matching
the DDL used in the acceptance tests.
"""

from __future__ import annotations

from datetime import date

from psycopg.abc import ConnectionType

PARENT_TABLE = "raw_samples"
PARTITION_NAME = "raw_samples_p{year}_{month:02d}"

ADVISORY_LOCK_KEY = 748_291_003

DAYS_PER_MONTH = 31

CREATE_PARTITION = """
CREATE TABLE IF NOT EXISTS {name} PARTITION OF {parent}
    FOR VALUES FROM ('{start}') TO ('{end}')
""".strip()


def month_range(month_start: date) -> tuple[date, date]:
    """[start, end) bounds of the month containing ``month_start`` (UTC naive)."""
    start = month_start.replace(day=1)
    if start.month == 12:
        nxt = start.replace(year=start.year + 1, month=1)
    else:
        nxt = start.replace(month=start.month + 1)
    return start, nxt


def partition_name(month_start: date) -> str:
    """Partition name for the month containing ``month_start``."""
    start, _ = month_range(month_start)
    return PARTITION_NAME.format(year=start.year, month=start.month)


def ensure_partitions(conn: ConnectionType, *, today: date | None = None) -> list[str]:
    """Create current + next month partitions of ``raw_samples`` if missing.

    Idempotent: re-running on an already-provisioned database executes only
    no-op ``CREATE TABLE IF NOT EXISTS`` statements. The transaction-scoped
    advisory lock makes concurrent callers (multiple ingest requests, the
    replay tool) safe — the second one waits, then no-ops.

    ``today`` is injectable for deterministic tests; the default derives from
    ``now()`` inside postgres so the service's clock is the single source of
    truth. Returns the names of the partitions ensured (current, next).
    """
    if today is None:
        row = conn.execute("SELECT (now() AT TIME ZONE 'UTC')::date").fetchone()
        today = row[0]

    current_start, current_end = month_range(today)
    next_start, next_end = month_range(current_end)

    conn.execute(f"SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY})")
    ensured: list[str] = []
    for start, end in ((current_start, current_end), (next_start, next_end)):
        name = partition_name(start)
        conn.execute(
            CREATE_PARTITION.format(
                name=name,
                parent=PARENT_TABLE,
                start=start.isoformat(),
                end=end.isoformat(),
            )
        )
        ensured.append(name)
    return ensured


def ensure_backfill_range(
    conn: ConnectionType,
    *,
    first_day: date,
    last_day: date,
) -> list[str]:
    """Ensure partitions cover an arbitrary historical range (backfill).

    Monthly, inclusive of both endpoint months. Same advisory lock +
    ``IF NOT EXISTS`` idempotency as :func:`ensure_partitions`. Returns the
    ensured partition names in chronological order.
    """
    if last_day < first_day:
        return []

    conn.execute(f"SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY})")
    ensured: list[str] = []
    month_cursor = first_day.replace(day=1)
    while month_cursor <= last_day:
        start, end = month_range(month_cursor)
        name = partition_name(month_cursor)
        conn.execute(
            CREATE_PARTITION.format(
                name=name,
                parent=PARENT_TABLE,
                start=start.isoformat(),
                end=end.isoformat(),
            )
        )
        ensured.append(name)
        if end > date(end.year, 12, 31):
            month_cursor = date(end.year + 1, 1, 1)
        else:
            month_cursor = end
    return ensured
