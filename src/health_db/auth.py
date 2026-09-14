"""Ingest token authentication (decomposition job slice-1-auth).

FastAPI dependency :func:`require_token` guards every ingest endpoint. It
compares the request header named by ``config.INGEST_HEADER`` (default
``X-Ingest-Token``) against ``config.INGEST_SECRET`` — both read from the
config module's live attributes at request time so tests can monkeypatch
them (see tests/acceptance/slice_1_ingest.py) — using a constant-time
comparison (harness iron law 3), records unauthorized attempts to an
in-memory :class:`AuditLog`, and raises :class:`AuthError` (HTTP 401, small
JSON body). It never leaks a stack trace, the secret, or the supplied token
(iron law 1).

Policies:

- **Constant-time comparison**: ``hmac.compare_digest`` over the SHA-256
  digests of the provided and expected tokens. Fixed-length digests keep the
  comparison constant-time regardless of input lengths, avoid leaking the
  secret's length through early-exit behaviour, and tolerate any header
  bytes (no ASCII-only restriction, no ``TypeError`` on odd input).
- **Fail closed** (spec §Auth Flow, config module docstring): when
  ``INGEST_SECRET`` is ``None`` or empty, *every* request is rejected with
  reason ``auth_not_configured`` — a missing/blank secret means "deny all",
  never "allow all".
- **Audit contents** (iron law 1): :class:`AuditEntry` carries exactly
  ``ts`` / ``ip`` / ``user_agent`` / ``reason``. Reasons are fixed strings
  (``missing_token`` / ``invalid_token`` / ``auth_not_configured``); token
  values and request bodies have no path into the audit, logs, or 401
  responses.
- **Bounded memory**: the in-memory log keeps only the most recent
  ``max_entries`` entries (default 1000) while :attr:`AuditLog.count`
  reflects every recorded attempt, so a flood of bad tokens cannot grow
  memory unboundedly. Slice 2 persists the same shape to the ``auth_log``
  table; this in-memory log is the slice-1 stand-in.
- **Auth runs before the body is read**: the dependency only inspects
  headers, so an untrusted body is never consumed, parsed, or archived on
  401 (spec failure semantics: "body NOT archived (never trusted)").

The API layer (slice-1-api) may register an exception handler for
:class:`AuthError` (e.g. to mirror the attempt into the stats counters);
the audit entry is recorded *before* the exception is raised, and FastAPI's
default handler already renders :class:`AuthError` as the 401 JSON body.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException, Request, status

import health_db.config as config

# Fixed audit reason strings — never derived from request material.
REASON_MISSING_TOKEN = "missing_token"
REASON_INVALID_TOKEN = "invalid_token"
REASON_AUTH_NOT_CONFIGURED = "auth_not_configured"

# Single generic 401 message: same text for every failure mode so the
# response distinguishes nothing an attacker could learn from.
UNAUTHORIZED_DETAIL = "Unauthorized: missing or invalid ingest token"

_AUDIT_MAX_ENTRIES = 1000


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One unauthorized attempt. Only these four fields exist — by
    construction, token values and request bodies cannot be stored."""

    ts: datetime
    ip: str | None
    user_agent: str | None
    reason: str


class AuditLog:
    """Thread-safe in-memory audit of unauthorized ingest attempts.

    ``count`` counts every attempt ever recorded; ``entries()`` returns only
    the most recent ``max_entries`` (oldest dropped) so repeated bad tokens
    cannot grow memory unboundedly.
    """

    def __init__(self, max_entries: int = _AUDIT_MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = max_entries
        self._entries: deque[AuditEntry] = deque(maxlen=max_entries)
        self._total = 0
        self._lock = threading.Lock()

    def record(
        self, *, ip: str | None = None, user_agent: str | None = None, reason: str
    ) -> AuditEntry:
        entry = AuditEntry(ts=datetime.now(UTC), ip=ip, user_agent=user_agent, reason=reason)
        with self._lock:
            self._entries.append(entry)
            self._total += 1
        return entry

    @property
    def count(self) -> int:
        with self._lock:
            return self._total

    def entries(self) -> tuple[AuditEntry, ...]:
        with self._lock:
            return tuple(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total = 0


# Process-wide singleton consumed by require_token; slice-1-stats exposes
# its count via the /api/ingest/stats endpoint.
AUDIT_LOG = AuditLog()


def check_token(provided: str | None, expected: str | None) -> bool:
    """Constant-time token comparison, fail-closed on any missing side.

    Both strings are hashed to fixed-length SHA-256 digests before
    ``hmac.compare_digest`` so the comparison time never depends on input
    lengths and non-ASCII header values cannot raise.
    """
    if not expected or provided is None:
        return False
    return hmac.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def failure_reason(provided: str | None, expected: str | None) -> str:
    """Fixed reason string for the audit log — never contains request material."""
    if not expected:
        return REASON_AUTH_NOT_CONFIGURED
    if not provided:
        return REASON_MISSING_TOKEN
    return REASON_INVALID_TOKEN


class AuthError(HTTPException):
    """401 raised by :func:`require_token`; carries the audit entry so the
    API layer can register a handler (e.g. to count the attempt in stats).
    Renders as ``{"detail": ...}`` via FastAPI's default HTTPException
    handling when no custom handler is registered."""

    def __init__(self, entry: AuditEntry) -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=UNAUTHORIZED_DETAIL)
        self.entry = entry


def require_token(request: Request) -> None:
    """FastAPI dependency: verify the ingest token or raise 401 + audit.

    Reads only headers — the request body is never touched, so untrusted
    bodies are neither read nor archived on rejection.
    """
    provided = request.headers.get(config.INGEST_HEADER)
    expected = config.INGEST_SECRET
    if check_token(provided, expected):
        return
    client = request.client
    entry = AUDIT_LOG.record(
        ip=client.host if client is not None else None,
        user_agent=request.headers.get("user-agent"),
        reason=failure_reason(provided, expected),
    )
    raise AuthError(entry)
