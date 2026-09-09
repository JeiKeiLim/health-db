"""FastAPI application factory + POST /api/ingest pipeline (job slice-1-api).

Wires the slice-1 ingest service around the finished slice-1 modules:

- ``health_db.routes`` — ``GET /api/health`` + ``GET /api/ingest/stats``
  (mounted verbatim; stats reads the ``health_db.stats.STATS`` singleton).
- ``health_db.auth`` — :func:`require_token` dependency on both ingest
  endpoints (constant-time compare, fail-closed, 401 + audit; body never
  read before auth).
- ``health_db.failures`` — oversize (413, nothing archived) and
  malformed/wrong-shape (400, verbatim quarantine) semantics.
- ``health_db.archive`` — verbatim archive-first store (iron law 2) with
  the in-process sha256 dedup index backed by the persistent disk archive.
- ``health_db.payloads`` — pure parse/report (counts, ``data_type``,
  ``unknown_formats``).

Pipeline for ``POST /api/ingest`` (order is the contract):

1. **Auth** (``require_token``) — 401 + audit; the body is never read or
   archived for untrusted requests.
2. **Size cap** — ``Content-Length`` (when present) then the buffered body
   are checked against ``config.MAX_PAYLOAD_BYTES``; over → 413, body never
   archived.
3. **Parse-first dedup** — :func:`health_db.payloads.parse` runs before the
   dated write, so malformed/wrong-shape bytes are quarantined via
   :func:`health_db.failures.handle_malformed` (400) and never enter the
   dated dedup tree or the slice-2 replay corpus. Every authorized body
   still reaches disk before its outcome is returned (iron law 2), and a
   re-post of bad bytes is re-quarantined, never deduped.
4. **Duplicate check** — a sha256 already archived (in-memory index OR the
   disk-backed index restored by the cold-start scan, so a process restart
   against an existing archive dir is honored) returns
   ``{"outcome": "duplicate"}`` (200, idempotent, no reprocessing); the
   dated tree is not rewritten.
5. **Verbatim archive** — atomic dated write ``YYYY/MM/DD/HHMMSS-<sha8>.json``.
6. **Response** — ``{"outcome": "stored", "data_type", "metrics",
   "samples", "workouts", "unknown_formats", "archive_path", "sha256"}``.

Every authorized request increments ``stats.STATS.received`` first and its
outcome (stored/duplicate/quarantined) second; the HAE automation headers
(``automation-name`` / ``automation-id`` / ``automation-aggregation`` /
``automation-period`` / ``session-id``) are folded into
``last_automation``. Unauthorized attempts mirror into
``unauthorized_attempts`` via the ``AuthError`` handler.

**Fresh app per access**: ``health_db.main.app`` is a factory-backed module
attribute (PEP 562). Each attribute access builds a new FastAPI app with
fresh :class:`Stats` / :class:`AuditLog` / :class:`ArchiveStore` instances
bound to the *live* config attributes at build time — so per-test
``TestClient`` fixtures (which monkeypatch config then import ``app``) get
isolated state without any reset calls, and a single import in a long-lived
process sees one consistent app. The pipeline reads ``config.*`` live at
request time (never a stale snapshot), and the singleton ``STATS`` /
``AUDIT_LOG`` objects remain available for direct use; a built app simply
carries its own instances.

**No DB imports** — slice 1 never touches the database driver or PostgreSQL
(grep-able decomposition constraint); slice 2 adds the store stage behind
the same pipeline.
"""

from __future__ import annotations

import json
import threading
from types import ModuleType
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

import health_db.config as config
import health_db.stats as stats
from health_db.archive import ArchiveStore
from health_db.auth import AUDIT_LOG, AuthError, require_token
from health_db.failures import (
    OversizedPayload,
    check_size,
    handle_malformed,
    oversized_response,
)
from health_db.payloads import MalformedPayload, parse
from health_db.pending import PendingStore
from health_db.pipeline import ingest_payload
from health_db.routes import router


def _resolve_pool() -> object | None:
    """Resolve the ingest pool: env-configured pool → materialized singleton
    → None (archive-only mode).

    The pool is resolved through the *live* ``db_pool.get_pool`` module
    attribute (looked up at call time, never an import-time alias) so test
    seams that replace ``health_db.db.pool.get_pool`` per test are honored
    no matter when this module was first imported. When ``PSQL_*`` env is
    configured the singleton builder is used (it memoizes, so production
    still gets exactly one pool); with a bare env the materialized
    singleton — if any — is returned; with neither, ``None`` is returned
    and the route stays in archive-only mode. The pipeline never builds a
    pool against ambient defaults (parity with the health probe's gate).
    """
    from health_db.db import pool as db_pool

    if db_pool.has_explicit_env():
        return db_pool.get_pool()
    return db_pool.existing_pool()


# HAE automation headers surfaced to stats.last_automation (spec §API
# Endpoints: automation-name/id/aggregation/period + session-id).
_AUTOMATION_HEADERS = (
    "automation-name",
    "automation-id",
    "automation-aggregation",
    "automation-period",
    "session-id",
)


def _automation_label(request: Request) -> str | None:
    """Compact label from the HAE automation headers, or None when absent.

    Header values are joined in fixed order (``k=v``); only present headers
    contribute, so a fully unlabeled payload yields None and a later
    unlabeled payload keeps the previous stats label.
    """
    parts = [
        f"{name}={value}"
        for name in _AUTOMATION_HEADERS
        if (value := request.headers.get(name)) is not None
    ]
    if not parts:
        return None
    return "; ".join(parts)


def _ingest_headers(request: Request) -> dict[str, str | None]:
    """HAE header values the pipeline stores into ``raw_ingest`` columns."""
    return {
        "automation-name": request.headers.get("automation-name"),
        "automation-id": request.headers.get("automation-id"),
        "session-id": request.headers.get("session-id"),
    }


def create_app() -> FastAPI:
    """Build a fresh ingest app with its own stats/audit/archive state.

    Stats and audit reset to zero on every build (test isolation) and are
    installed both on ``app.state`` and as the process-wide ``STATS`` /
    ``AUDIT_LOG`` singletons — the mounted routes read the singletons, and
    the pipeline records through the same objects, so counters agree.
    """
    app = FastAPI(title="health-db ingest", version=config.APP_VERSION)
    app.include_router(router)
    store = ArchiveStore()
    app_state_stats = stats.STATS
    app_state_stats.reset()
    app_state_audit = AUDIT_LOG
    app_state_audit.clear()
    app.state.stats = app_state_stats
    app.state.audit_log = app_state_audit
    app.state.archive_store = store

    async def auth_error_handler(_: Request, exc: AuthError) -> JSONResponse:
        # Mirror the audited attempt into the stats counters (auth.py records
        # the audit entry before raising; the body was never read/archived).
        app_state_stats.record_unauthorized()
        return JSONResponse(status_code=401, content={"detail": str(exc.detail)})

    app.exception_handler(AuthError)(auth_error_handler)

    @app.post("/api/ingest", dependencies=[Depends(require_token)])
    async def ingest(request: Request) -> JSONResponse:
        # The require_token dependency has already passed (401 path never
        # reaches here). Count the authorized request before any outcome.
        app_state_stats.record_received(automation=_automation_label(request))

        # Size cap: reject before buffering work where possible. The header
        # lies sometimes, so the buffered body is re-checked below; either
        # way an oversized body is never archived.
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit():
            try:
                check_size(int(declared))
            except OversizedPayload as exc:
                return oversized_response(exc)
        body = await request.body()
        try:
            check_size(len(body))
        except OversizedPayload as exc:
            return oversized_response(exc)

        # Parse before the dated write: malformed/wrong-shape bytes are
        # quarantined (verbatim, 400) and never enter the dedup index or
        # the slice-2 replay corpus. The quarantine write is still
        # archive-first — the payload is on disk before the 400 returns,
        # with its error-note sidecar (<stem>.error.txt, spec failure
        # table) written alongside by the module's always-on behavior.
        try:
            document = json.loads(body)
            report = parse(body)
        except (MalformedPayload, ValueError) as exc:
            response, _ = handle_malformed(
                body,
                exc if isinstance(exc, MalformedPayload) else MalformedPayload(str(exc)),
                store=store,
            )
            app_state_stats.record_quarantined()
            return response

        content_sha = store.sha256_of(body)
        # Duplicate gate consults BOTH indexes: the in-memory ``seen()`` set
        # (fast path, same process) and the persistent disk-backed archive
        # index via :meth:`ArchiveStore.find_by_sha256` — a restart-restored
        # archive (empty in-memory index) must still yield
        # ``{"outcome": "duplicate"}`` with no second copy, never a
        # re-store that double-counts into stats.stored.
        if store.seen(content_sha) or store.find_by_sha256(content_sha) is not None:
            app_state_stats.record_duplicate()
            return JSONResponse(
                status_code=200,
                content={"outcome": "duplicate", "sha256": content_sha},
            )

        # Archive verbatim BEFORE reporting success (iron law 2).
        path = store.store(body)

        # Slice 2 store stage: normalize → upsert → rollup recompute in one
        # transaction. The disk archive above is the outage safety net (iron
        # law 2); a DB outage also queues the payload for replay before the
        # 503 returns, so replay never depends on rescanning the corpus.
        content_sha = store.sha256_of(body)
        headers = _ingest_headers(request)
        received_stamp = app_state_stats.last_received_at()
        pool_obj = _resolve_pool()
        if pool_obj is None:
            # No DB configured (no PSQL_* env, no materialized pool):
            # archive-only mode — slice-1 semantics, payload counts as
            # stored (the archive IS the durability). /api/health reports
            # db=false so the missing configuration is observable.
            app_state_stats.record_stored()
            return JSONResponse(
                status_code=200,
                content={
                    "outcome": "stored",
                    "data_type": report.data_type,
                    "metrics": report.metrics,
                    "samples": report.samples,
                    "workouts": report.workouts,
                    "unknown_formats": list(report.unknown_formats),
                    "archive_path": str(path),
                    "sha256": content_sha,
                },
            )
        try:
            result = ingest_payload(
                pool_obj,
                document,
                sha256=content_sha,
                body=body,
                headers=headers,
            )
        except Exception as exc:
            from health_db.pipeline import DbUnavailable

            if not isinstance(exc, DbUnavailable):
                raise
            # Outage: archive stays (already written), queue for replay,
            # then 503 + Retry-After (spec failure table).
            from datetime import datetime as _dt

            PendingStore().store(
                body,
                sha256=content_sha,
                received_at=_dt.fromisoformat(received_stamp) if received_stamp else None,
                automation_name=headers["automation-name"],
                automation_id=headers["automation-id"],
                session_id=headers["session-id"],
            )
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "300"},
                content={"detail": "database unavailable; payload archived for replay"},
            )

        if result.outcome == "duplicate":
            app_state_stats.record_duplicate()
            return JSONResponse(
                status_code=200,
                content={"outcome": "duplicate", "sha256": content_sha},
            )

        app_state_stats.record_stored()
        return JSONResponse(
            status_code=200,
            content={
                "outcome": "stored",
                "data_type": report.data_type,
                "metrics": result.metrics,
                "samples": result.samples,
                "workouts": result.workouts,
                "unknown_formats": list(report.unknown_formats),
                "archive_path": str(path),
                "sha256": content_sha,
            },
        )

    return app


_FACTORY_LOCK = threading.Lock()
_FACTORY: ModuleType | None = None


def _factory_module() -> ModuleType:
    """Lazily import this module under its canonical name.

    ``uv run health-db`` may exec this file as ``__main__``, so a direct
    ``from health_db.main import app`` inside :func:`serve` would import a
    second module copy. Importing by name returns the already-initialized
    canonical module (whose attribute access builds the app against the
    then-current config) when available.
    """
    global _FACTORY
    with _FACTORY_LOCK:
        if _FACTORY is None:
            import importlib

            _FACTORY = importlib.import_module(__name__.removesuffix(".__main__"))
    return _FACTORY


def serve() -> None:
    """Entry point (``uv run health-db``): load env config, serve on :8000."""
    import uvicorn

    config.load()
    module = _factory_module()
    app_obj: Any = getattr(module, "app")
    uvicorn.run(app_obj, host=config.HOST, port=config.PORT)


def main() -> None:
    """Console-script alias — pyproject wires ``health-db`` here."""
    serve()


def __getattr__(name: str) -> Any:
    """Module-level ``app`` is built fresh per access (PEP 562).

    Each access returns a new FastAPI app whose pipeline reads the live
    config attributes and carries fresh Stats/AuditLog/ArchiveStore state,
    so TestClient fixtures that monkeypatch config per test get isolated
    apps without shared mutable singletons. Any other missing attribute is
    a normal ImportError.
    """
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
