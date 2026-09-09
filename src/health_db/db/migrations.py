"""Versioned-SQL migration applier (job slice-2-db-config).

Reads ``migrations/NNNN_name.sql`` files in version order and applies the
pending ones inside a transaction each, recording the version in
``schema_migrations (version int PRIMARY KEY, applied_at timestamptz)``.

Idempotent by construction: already-applied versions are skipped, and the
table is created ``IF NOT EXISTS``, so :func:`apply_all` can run at every
service start (spec: "migrations versioned SQL in migrations/ applied by a
small Python applier"). ``health-migrate`` (see :func:`main`) applies
pending migrations against the ``PSQL_*`` database and exits 0; failures
print a generic, secret-free message to stderr and exit 1 (iron law 1).

The applier is generic: it never inspects migration contents, so
``migrations/0001_init.sql`` (job slice-2-schema) lands without changes
here. Migration SQL must be single-statement-per-file-friendly plain SQL
executed verbatim via :meth:`psycopg.Connection.execute`.

The migrations directory is resolved by :func:`migrations_dir`:
``MIGRATIONS_DIR`` env override, else ``./migrations`` relative to the
current working directory (deploy layout: image WORKDIR contains it), else
the repo-root ``migrations/`` relative to this module file (uv dev layout).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from psycopg.abc import ConnectionType

ENV_MIGRATIONS_DIR = "MIGRATIONS_DIR"

CREATE_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version int PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
""".strip()

SELECT_APPLIED = "SELECT version FROM schema_migrations"
INSERT_APPLIED = "INSERT INTO schema_migrations (version) VALUES (%s)"

_VERSION_PREFIX = re.compile(r"^(?P<version>\d+)")

APPLIED_TABLE_NAME = "schema_migrations"


class MigrationError(Exception):
    """Raised for malformed migration file names or duplicate versions."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One versioned SQL file ready to apply."""

    version: int
    path: Path


def migrations_dir() -> Path:
    """Resolve the migrations directory (env override → cwd → repo root)."""
    env = os.environ.get(ENV_MIGRATIONS_DIR)
    if env:
        return Path(env)
    cwd_candidate = Path.cwd() / "migrations"
    if cwd_candidate.is_dir():
        return cwd_candidate
    return Path(__file__).resolve().parents[3] / "migrations"


def discover(directory: Path) -> list[Migration]:
    """Parse ``NNNN_*.sql`` files into ordered migrations.

    Non-matching files (no leading digits, wrong suffix) are ignored; two
    files claiming the same version raise :class:`MigrationError`.
    """
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")
    seen: dict[int, Path] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".sql":
            continue
        match = _VERSION_PREFIX.match(path.name)
        if match is None:
            continue
        version = int(match.group("version"))
        if version in seen:
            raise MigrationError(
                f"duplicate migration version {version}: "
                f"{seen[version].name} and {path.name}"
            )
        seen[version] = path
    return [Migration(version=version, path=path) for version, path in sorted(seen.items())]


def _row_version(row: object) -> int:
    """Extract the version from a tuple row or a mapping row (dict_row)."""
    if isinstance(row, Mapping):
        return int(row["version"])
    return int(row[0])  # type: ignore[index]  (tuple row per tuple_row factory)


def applied_versions(conn: ConnectionType) -> set[int]:
    """Versions already recorded in ``schema_migrations`` (table must exist)."""
    rows = conn.execute(SELECT_APPLIED).fetchall()
    return {_row_version(row) for row in rows}


def apply_all(conn: ConnectionType, migrations_dir: Path | None = None) -> list[int]:
    """Apply pending migrations in version order; return versions applied now.

    Idempotent: re-running against a fully-migrated database applies nothing.
    The ``schema_migrations`` bookkeeping table is created first (in its own
    transaction); each migration runs in a transaction that also records its
    version, so a half-written migration file never marks itself applied.
    """
    directory = migrations_dir if migrations_dir is not None else _default_dir_or_none()
    if directory is not None and not directory.is_dir():
        directory = None  # missing directory → no migrations to apply (no-op)
    migrations = discover(directory) if directory is not None else []

    with conn.transaction():
        conn.execute(CREATE_SCHEMA_MIGRATIONS)
    done = applied_versions(conn)

    applied_now: list[int] = []
    for migration in migrations:
        if migration.version in done:
            continue
        sql = migration.path.read_text(encoding="utf-8")
        with conn.transaction():
            conn.execute(sql)
            conn.execute(INSERT_APPLIED, (migration.version,))
        applied_now.append(migration.version)
    return applied_now


def _default_dir_or_none() -> Path | None:
    """Directory for the default path, or None when it does not exist (no-op)."""
    directory = migrations_dir()
    return directory if directory.is_dir() else None


def main(argv: list[str] | None = None) -> int:
    """``health-migrate`` entry point: apply pending migrations, exit 0/1."""
    parser = argparse.ArgumentParser(
        prog="health-migrate",
        description="Apply versioned SQL migrations from migrations/ to the PSQL_* database",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=None,
        help="migrations directory (default: MIGRATIONS_DIR env, ./migrations, or repo root)",
    )
    args = parser.parse_args(argv)

    try:
        from health_db.db import pool as db_pool

        directory = args.migrations_dir
        if directory is None:
            directory = migrations_dir()
        if not directory.is_dir():
            raise MigrationError(f"migrations directory not found: {directory}")

        pool = db_pool.get_pool()
        with pool.connection() as conn:
            applied = apply_all(conn, migrations_dir=directory)
        db_pool.reset_pool()
    except Exception:
        print(
            "health-migrate: failed to apply migrations; check database "
            "connectivity and migration SQL (details are in the server log)",
            file=sys.stderr,
        )
        return 1

    label = ", ".join(str(v) for v in applied) if applied else "none"
    print(f"schema_migrations up to date (applied now: {label})")
    return 0
