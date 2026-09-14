"""Malformed / oversized failure semantics (decomposition job slice-1-quarantine).

Implements the spec.md §API Endpoints failure table for the two failure
modes that follow token auth, and provides the helpers the API job
(slice-1-api) wires into the POST /api/ingest pipeline:

- **Malformed JSON / wrong shape** → HTTP 400 ``{"error": "malformed",
  "quarantine": <id>}`` — and, FIRST (harness iron law 2: nothing is ever
  lost), the verbatim payload is archived to ``<root>/quarantine/`` via the
  archive store's quarantine lane
  (``health_db.archive.ArchiveStore.store(body, quarantine=True)``), with an
  error note written alongside. A 400 response can therefore never
  correspond to a payload that vanished; conversely no quarantined payload
  enters the dated dedup tree (quarantined content was never accepted).
- **Oversized body** (> ``config.MAX_PAYLOAD_BYTES``) → HTTP 413, *not*
  archived: the spec failure table rejects the body before it is read
  fully, and an untrusted oversized body has no place in the archive.
  :class:`OversizedPayload` carries the limit and actual length for the
  413 response; the API layer raises/forwards it before any I/O. (The
  spec's ``Retry-After: 300`` belongs to the 503 DB-unavailable path, not
  here.)

Response contracts (consumed by slice-1-api):

- ``handle_malformed(body, exc)`` → ``(JSONResponse, Path)`` — the 400 body
  and the on-disk quarantine path. The quarantine ``id`` in the response is
  the archive-relative path string (``quarantine/HHMMSS-sha8.json``) so an
  operator can locate the exact file; it contains no secret and no body
  content. The response also carries an actionable ``reason`` (the parser's
  message, e.g. "payload is not valid JSON: ..."), which describes the JSON
  defect and never echoes body bytes or secrets (iron law 1).
- ``check_size(length)`` → raises :class:`OversizedPayload` when ``length``
  exceeds ``config.MAX_PAYLOAD_BYTES``; returns ``None`` otherwise. Reads
  the limit from the config module's live attribute at call time so tests
  can monkeypatch it. Called before the body is consumed; a raised
  exception means the body must never be archived.
- ``oversized_response(exc)`` → the 413 JSON body.

Ordering guarantees this module enforces:

- ``handle_malformed`` archives BEFORE building the response — if the
  quarantine write itself fails, the exception propagates, so the service
  never returns a 400 that claims a quarantine id without the payload on
  disk (and never silently loses the payload either).
- The error note is a sibling ``<stem>.error.txt`` next to the quarantined
  payload: reason text only, never body content, never secrets. Best-effort
  by design — a note write failure does not fail the quarantine of the
  payload itself.
- Quarantines are never deduped against the stored/dated tree: a re-post of
  bad bytes is re-quarantined (never treated as a stored duplicate, never
  entered into the dedup index). Identical bad bytes within the same clock
  second share one quarantine file (the store's same-second content
  disambiguation) — either way nothing is lost and nothing enters the dated
  tree (parity with ``test_cold_scan_skips_quarantine_files`` in
  tests/unit/test_archive.py).
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi.responses import JSONResponse

import health_db.config as config
from health_db.archive import ArchiveStore
from health_db.payloads import MalformedPayload

# HTTP 413 "Payload Too Large" (RFC 9110: Content Too Large). Plain int —
# starlette's alias constants are deprecated/renamed across versions.
HTTP_413 = 413

_FALLBACK_STORE_LOCK = threading.Lock()
_FALLBACK_STORE: ArchiveStore | None = None


class OversizedPayload(Exception):
    """Body length exceeds ``config.MAX_PAYLOAD_BYTES``.

    Carries ``length`` and ``limit`` for the 413 response; never holds body
    content — the oversize decision is made from the byte count alone (the
    body is neither parsed nor archived).
    """

    def __init__(self, length: int, limit: int) -> None:
        super().__init__(f"payload is {length} bytes; exceeds the {limit}-byte ingest limit")
        self.length = length
        self.limit = limit


def check_size(length: int) -> None:
    """Raise :class:`OversizedPayload` when ``length`` > MAX_PAYLOAD_BYTES.

    Called by the API layer before the body is consumed; a raised exception
    means the body must never be archived.
    """
    if length > config.MAX_PAYLOAD_BYTES:
        raise OversizedPayload(length=length, limit=config.MAX_PAYLOAD_BYTES)


def oversized_response(exc: OversizedPayload) -> JSONResponse:
    """413 JSON body for a rejected oversize body (nothing archived)."""
    return JSONResponse(
        status_code=HTTP_413,
        content={
            "error": "payload_too_large",
            "limit_bytes": exc.limit,
            "received_bytes": exc.length,
        },
    )


def quarantine_id(path: Path, root: Path) -> str:
    """Archive-relative quarantine identifier reported to the client.

    Posix-style (``quarantine/HHMMSS-sha8.json``); falls back to the file
    name alone if ``path`` is not under ``root`` (defensive — cannot happen
    with the real store).
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _fallback_store() -> ArchiveStore:
    """Process-wide store used when the caller does not pass one.

    Cached singleton (mirrors the STATS/AUDIT_LOG conventions). Bound to the
    live ``config.ARCHIVE_ROOT`` at first use; the API job passes its own
    :class:`ArchiveStore` explicitly, so this is a convenience for direct
    unit/scripted use only.
    """
    global _FALLBACK_STORE
    with _FALLBACK_STORE_LOCK:
        if _FALLBACK_STORE is None:
            _FALLBACK_STORE = ArchiveStore()
        return _FALLBACK_STORE


def handle_malformed(
    body: bytes, exc: MalformedPayload, *, store: ArchiveStore | None = None
) -> tuple[JSONResponse, Path]:
    """Quarantine ``body`` verbatim, then build the 400 response.

    Iron-law ordering: the payload is written to ``<root>/quarantine/`` via
    the archive store FIRST; only a successful write produces the 400
    ``JSONResponse`` (if the write raises, the exception propagates — the
    API layer surfaces it and no payload is lost silently). An error note
    (``exc``'s message) is written beside the payload, best-effort.

    ``store`` defaults to the module's cached fallback
    :class:`ArchiveStore`; the API layer passes its pipeline store
    explicitly so there is exactly one archive instance per process.
    """
    archive_store = store if store is not None else _fallback_store()
    path = archive_store.store(body, quarantine=True)
    _write_error_note(path, str(exc))
    response = JSONResponse(
        status_code=400,
        content={
            "error": "malformed",
            "quarantine": quarantine_id(path, archive_store.root),
            "reason": str(exc),
        },
    )
    return response, path


def _write_error_note(path: Path, reason: str) -> None:
    """Best-effort sibling ``<stem>.error.txt`` note (reason text only).

    Never contains body content or secrets — ``MalformedPayload`` messages
    describe the JSON defect and do not echo body bytes.
    """
    note = path.with_name(path.stem + ".error.txt")
    try:
        note.write_text(f"{reason}\n", encoding="utf-8")
    except OSError:
        pass  # payload itself is already quarantined; the note is auxiliary
