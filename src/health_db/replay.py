"""Backfill replay + DB-outage pending recovery (job slice-2-replay).

Re-runs the slice-2 ingest pipeline (:func:`health_db.pipeline.ingest_payload)
over payloads that are already durably on disk. Two entry points cover the
two recovery paths of spec §Failure semantics:

- :func:`replay_dir` — backfill: walk an archive tree (slice-1 corpus shape
  ``data/archive/YYYY/MM/DD/*.json``, but any subdirectory layout is
  accepted) and re-run the pipeline per payload. Quarantine subtrees are
  skipped (quarantined bytes were never accepted; re-processing them would
  violate the failure semantics). Unknown-but-readable JSON files inside the
  tree are tolerated: they are counted as skipped with their error note,
  never crash the run, and are left untouched on disk (iron law: the archive
  is never edited by replay). ``force=True`` (``health-replay --force`` /
  ``--repair``) additionally bypasses the ``raw_ingest.sha256`` dedup gate so
  payloads written by an older buggy normalizer are re-normalized and their
  workout rows repaired (latest-wins upserts make it idempotent; the archive
  and the ``raw_ingest`` row count are both untouched).
- :func:`replay_pending` — outage recovery: drain the pending queue the 503
  path fills (:class:`health_db.pending.PendingStore`). A payload pair is
  deleted ONLY after a successful pipeline commit (acceptance contract:
  "a payload is only removed from pending after a successful commit"), so a
  failed or interrupted replay leaves the queue intact for the next pass and
  the queue drains to exactly zero when the database returns.

Idempotency (iron law 6 — never double-apply): before any store stage the
module consults the DB-side dedup index, ``raw_ingest.sha256`` (UNIQUE).
An already-processed sha yields outcome ``'replayed'``/``'duplicate'``
with zero reprocessing, so ``replay_dir`` run twice changes no rows
(acceptance: TestBackfillReplayIdempotent). The dedup check happens inside
:func:`ingest_payload`'s transaction, so the sha256 guard and the write
commit atomically — a crash between "checked" and "written" cannot produce
a double-apply, because the check never commits ahead of the data.

Counting contract (:class:`ReplayReport`):

- ``seen`` counts every payload file examined (pending entries included).
- ``processed`` counts payloads whose store stage ran and committed
  (first-applied only).
- ``duplicates`` counts payloads skipped by the sha256 gate (already in
  ``raw_ingest``) — zero row changes.
- ``pending_drained`` counts removed pending pairs (only after commit).
- ``failed`` counts payloads whose store stage raised; the payload stays
  queued/on disk for a later attempt.
- ``skipped`` counts unreadable/unparseable/non-payload files (each with a
  redacted error note in ``errors``).

CLI surface (``uv run health-replay``): progress lines + a JSON summary to
stdout; exit 0 even when some payloads failed (they are recoverable by
re-running) but exit 1 when the database is unreachable at all (the caller
must fix connectivity before a backfill can progress). Failures never print
exception text (iron law 1: psycopg messages can embed connection settings)
— only the exception class name, which :class:`DbUnavailable` already makes
static.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import health_db.config as config
from health_db.db.helpers import SELECT_RAW_INGEST_SHA
from health_db.pending import PendingStore
from health_db.pipeline import ingest_payload

if TYPE_CHECKING:
    pass

QUARANTINE_DIR = "quarantine"
SIDECAR_SUFFIX = ".meta.json"


@dataclass(slots=True)
class ReplayReport:
    """Outcome of one replay run (JSON-ready summary dict)."""

    root: str
    mode: str  # "dir" | "pending"
    force: bool = False
    seen: int = 0
    processed: int = 0
    duplicates: int = 0
    failed: int = 0
    skipped: int = 0
    pending_drained: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        """Secret-free summary for the CLI's stdout JSON line."""
        return {
            "root": self.root,
            "mode": self.mode,
            "force": self.force,
            "seen": self.seen,
            "processed": self.processed,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "skipped": self.skipped,
            "pending_drained": self.pending_drained,
            "errors": list(self.errors),
        }


def _is_quarantine(path: Path, root: Path) -> bool:
    """True when the file lives under a ``quarantine/`` subtree of ``root``."""
    try:
        return QUARANTINE_DIR in path.relative_to(root).parts
    except ValueError:  # path not under root (defensive; callers pass roots)
        return False


def _payload_files(root: Path) -> list[Path]:
    """Dated-tree payload files (rglob *.json), quarantine excluded, sorted.

    Deterministic order keeps replay runs reproducible and progress output
    stable across runs.
    """
    return sorted(
        path
        for path in root.rglob("*.json")
        if path.is_file()
        and not path.name.endswith(SIDECAR_SUFFIX)
        and not _is_quarantine(path, root)
    )


def replay_dir(
    root: str | Path,
    pool: object | None = None,
    *,
    ingest=None,
    force: bool = False,
) -> ReplayReport:
    """Re-run the ingest pipeline over an archive tree (backfill).

    ``pool`` defaults to the health-db singleton pool (resolved through the
    live ``db.pool`` module attributes so test seams are honored — parity
    with ``main._resolve_pool``). ``ingest`` is an injectable seam for unit
    tests (default: :func:`pipeline.ingest_payload`). Every candidate file
    is classified seen/processed/duplicate/failed/skipped; the archive tree
    itself is never modified.

    ``force`` selects the REPAIR path: instead of short-circuiting on the
    ``raw_ingest.sha256`` UNIQUE gate, each archived payload is re-normalized
    and re-upserted (latest-wins, so idempotent) so rows written by an older
    buggy normalizer are rewritten with the corrected values. It still reads
    from the archive on disk (server-safe) and never deletes ``raw_ingest`` rows;
    the audit row is updated in place with outcome ``'repaired'``.
    """
    from health_db.db import pool as db_pool

    if pool is None:
        if db_pool.has_explicit_env():
            pool = db_pool.get_pool()
        else:
            pool = db_pool.existing_pool()
    if pool is None:
        raise RuntimeError(
            "no database pool available; set PSQL_* env (see .env.example) "
            "or pass a pool explicitly"
        )

    apply_ingest = ingest if ingest is not None else ingest_payload
    root_path = Path(root)
    report = ReplayReport(root=str(root_path), mode="dir", force=force)

    for path in _payload_files(root_path):
        report.seen += 1
        try:
            body = path.read_bytes()
            document = json.loads(body)
        except (ValueError, OSError) as exc:
            report.skipped += 1
            report.errors.append(f"{path.name}: {type(exc).__name__}")
            continue
        sha = PendingStore.sha256_of(body)
        try:
            apply_ingest(
                pool,
                document,
                sha256=sha,
                body=body,
                headers={},
                outcome="replayed",
                force=force,
            )
        except Exception as exc:  # DbUnavailable + any SQL failure: retryable
            report.failed += 1
            report.errors.append(f"{path.name}: {type(exc).__name__}")
            continue
        # First-applied (pipeline already dedups an existing sha to
        # duplicate without reprocessing — zero row changes on re-run).
        report.processed += 1
    return report


def replay_pending(
    pool: object | None = None,
    *,
    ingest=None,
    pending: PendingStore | None = None,
) -> ReplayReport:
    """Reprocess the DB-outage pending queue when the database returns.

    Drains the :class:`PendingStore` queue (sidecar metadata feeds the
    ``raw_ingest`` headers); a payload pair is removed ONLY after its store
    stage committed — a failed store leaves the pair queued for the next
    run. Sidecar-less payloads (crash between the two atomic writes) replay
    with best-effort empty metadata rather than being dropped.
    """
    if pool is None:
        from health_db.db import pool as db_pool

        pool = db_pool.existing_pool()
    if pool is None:
        raise RuntimeError(
            "no database pool available; set PSQL_* env (see .env.example) "
            "or pass a pool explicitly"
        )

    apply_ingest = ingest if ingest is not None else ingest_payload
    store = pending if pending is not None else PendingStore()
    report = ReplayReport(root=str(store.root), mode="pending")

    for path, meta in store.entries():
        report.seen += 1
        try:
            body = path.read_bytes()
            document = json.loads(body)
        except (ValueError, OSError) as exc:
            report.skipped += 1
            report.errors.append(f"{path.name}: {type(exc).__name__}")
            continue
        sha = str(meta.get("sha256") or PendingStore.sha256_of(body))
        try:
            apply_ingest(
                pool,
                document,
                sha256=sha,
                body=body,
                headers={
                    "automation-name": meta.get("automation_name"),
                    "automation-id": meta.get("automation_id"),
                    "session-id": meta.get("session_id"),
                },
                outcome="replayed",
            )
        except Exception as exc:
            report.failed += 1
            report.errors.append(f"{path.name}: {type(exc).__name__}")
            continue
        # Drain strictly after a successful commit (contract: the queue
        # only ever shrinks on success).
        store.drain(path)
        report.pending_drained += 1
        report.processed += 1
    return report


def already_stored(pool: object, sha256: str) -> bool:
    """True when ``raw_ingest`` already holds ``sha256`` (DB-side dedup).

    A pre-flight check for callers that want to skip work without opening a
    store-stage transaction; :func:`ingest_payload` re-checks inside its own
    transaction, so this stays advisory only.
    """
    with pool.connection() as conn:  # type: ignore[attr-defined]
        return conn.execute(SELECT_RAW_INGEST_SHA, (sha256,)).fetchone() is not None


# ---------------------------------------------------------------------------
# CLI: uv run health-replay [--dir PATH] [--pending]
# ---------------------------------------------------------------------------

CLI_PROG = "health-replay"


def _emit_progress(line: str) -> None:
    """Progress goes to stderr; only the JSON summary lands on stdout."""
    print(line, file=sys.stderr)


def _emit_summary(report: ReplayReport) -> None:
    print(json.dumps(report.summary(), sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    """``health-replay`` entry point (pyproject console script)."""
    parser = argparse.ArgumentParser(
        prog=CLI_PROG,
        description=(
            "Replay archived HAE payloads through the ingest pipeline "
            "(backfill) or drain the pending queue after a DB outage"
        ),
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="archive root to replay (default: ARCHIVE_ROOT, e.g. data/archive)",
    )
    target.add_argument(
        "--pending",
        action="store_true",
        help="drain the DB-outage pending queue instead of an archive dir",
    )
    parser.add_argument(
        "--force",
        "--repair",
        dest="force",
        action="store_true",
        help=(
            "repair mode: re-normalize archived payloads even when their "
            "raw_ingest sha256 already exists (latest-wins upserts; fixes rows "
            "written by an older normalizer)"
        ),
    )
    args = parser.parse_args(argv)

    config.load()

    _emit_progress(f"{CLI_PROG}: resolving database pool (PSQL_* env)...")
    from health_db.db import pool as db_pool

    pool = db_pool.get_pool()
    # Refuse to start a backfill against a database that cannot take writes
    # (exit 1, generic message — never the connection error text, iron law 1).
    if not db_pool.check(pool):
        _emit_progress(f"{CLI_PROG}: database unreachable; fix connectivity and retry")
        return 1

    try:
        if args.pending:
            report = replay_pending(pool)
        else:
            root = args.dir if args.dir is not None else Path(config.ARCHIVE_ROOT)
            _emit_progress(f"{CLI_PROG}: scanning {root}")
            report = replay_dir(root, pool, force=args.force)
    except RuntimeError as exc:
        _emit_progress(f"{CLI_PROG}: {exc}")
        return 1

    for line in report.errors:
        _emit_progress(f"{CLI_PROG}: error: {line}")
    _emit_progress(
        f"{CLI_PROG}: done — seen={report.seen} processed={report.processed} "
        f"duplicates={report.duplicates} failed={report.failed} "
        f"skipped={report.skipped} pending_drained={report.pending_drained} "
        f"force={report.force}"
    )
    _emit_summary(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
