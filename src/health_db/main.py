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
  the in-process sha256 dedup index.
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
4. **Duplicate check** — a sha256 already in the archive index returns
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
from health_db.routes import router

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
        # archive-first — the payload is on disk before the 400 returns.
        try:
            report = parse(body)
        except MalformedPayload as exc:
            response, _ = handle_malformed(body, exc, store=store, error_note=False)
            app_state_stats.record_quarantined()
            return response

        content_sha = store.sha256_of(body)
        if store.seen(content_sha):
            app_state_stats.record_duplicate()
            return JSONResponse(
                status_code=200,
                content={"outcome": "duplicate", "sha256": content_sha},
            )

        # Archive verbatim BEFORE reporting success (iron law 2).
        path = store.store(body)
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
    uvicorn.run(app_obj, host="0.0.0.0", port=8000)


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
