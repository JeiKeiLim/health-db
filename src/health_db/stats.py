"""Process-wide ingest statistics (decomposition job slice-1-stats).

A single thread-safe :class:`Stats` counters object backs the observability
surface of the ingest service: ``/api/ingest/stats`` (token-protected) reads
:meth:`Stats.snapshot`, while the pipeline (slice-1-api) records outcomes.
Slice 1 keeps counters in memory; slice 2 derives the same shape from the
``raw_ingest`` table, so the snapshot key set is a run contract:

- ``received`` — authorized POSTs accepted by the ingest pipeline (every
  outcome path increments this first: stored, duplicate, quarantined).
- ``stored`` — payloads parsed and accepted.
- ``duplicate`` — payloads skipped by the sha256 content check.
- ``quarantined`` — payloads archived to ``quarantine/`` (malformed/wrong
  shape; always accompanied by a 400 response).
- ``unauthorized_attempts`` — rejected token attempts. Auth itself
  (``health_db.auth``) records the audit entry and raises
  :class:`health_db.auth.AuthError`; the API layer mirrors the attempt into
  this counter from its ``AuthError`` handler, so :class:`Stats` has no
  dependency on the auth module (no import cycle, single write path).
- ``last_received_at`` — ISO-8601 UTC timestamp of the most recent
  authorized request (``None`` until the first one).
- ``last_automation`` — automation label of the most recent payload, as
  passed by the pipeline from the HAE automation headers (``None`` until
  the first labeled payload). A later payload without a label keeps the
  previous label rather than clearing it.

Secret hygiene (harness iron law 1): counters and labels are the only
state. Token values never reach this module — the auth layer audits
attempts without storing request material — so :meth:`snapshot` is safe to
serialize verbatim.

Thread-safety: every mutation happens under one lock, so concurrent
ingest requests (uvicorn threadpool) and the stats reader see consistent
values and exact totals.

The module-level :data:`STATS` singleton is the process-wide instance
consumed by the routes and the API pipeline; tests call :meth:`Stats.reset`
to isolate cases.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

# Run contract for /api/ingest/stats (spec.md §API Endpoints + decomposition
# job slice-1-stats). Order is the JSON key order of snapshot().
SNAPSHOT_KEYS = (
    "received",
    "stored",
    "duplicate",
    "quarantined",
    "unauthorized_attempts",
    "last_received_at",
    "last_automation",
)


class Stats:
    """Thread-safe in-memory ingest counters with a JSON-ready snapshot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._received = 0
        self._stored = 0
        self._duplicate = 0
        self._quarantined = 0
        self._unauthorized_attempts = 0
        self._last_received_at: datetime | None = None
        self._last_automation: str | None = None

    # --- recorders ---------------------------------------------------------

    def record_received(
        self, *, automation: str | None = None, at: datetime | None = None
    ) -> datetime:
        """Count one authorized ingest request and stamp recency.

        ``at`` allows tests (and future replay paths) to pin the timestamp;
        aware datetimes are normalized to UTC and naive ones are treated as
        UTC. Returns the stamped timestamp. The automation label is stored
        verbatim when provided and kept when a later payload omits it.

        The stamp is computed and compared *inside* the lock with a
        monotonic guard: ``last_received_at`` only moves forward, so a
        concurrent request that read the clock earlier but acquires the
        lock later can never regress the recency stamp to an older value.
        """
        with self._lock:
            stamp = at if at is not None else datetime.now(UTC)
            stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
            self._received += 1
            if self._last_received_at is None or stamp >= self._last_received_at:
                self._last_received_at = stamp
            if automation:
                self._last_automation = automation
        return stamp

    def record_stored(self) -> None:
        """Count one payload parsed and accepted (outcome ``stored``)."""
        with self._lock:
            self._stored += 1

    def record_duplicate(self) -> None:
        """Count one payload skipped by the sha256 duplicate check."""
        with self._lock:
            self._duplicate += 1

    def record_quarantined(self) -> None:
        """Count one payload archived to quarantine (outcome: 400)."""
        with self._lock:
            self._quarantined += 1

    def record_unauthorized(self) -> None:
        """Count one rejected token attempt (outcome: 401)."""
        with self._lock:
            self._unauthorized_attempts += 1

    # --- reads -------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """JSON-ready stats dict — the body of GET /api/ingest/stats."""
        with self._lock:
            return {
                "received": self._received,
                "stored": self._stored,
                "duplicate": self._duplicate,
                "quarantined": self._quarantined,
                "unauthorized_attempts": self._unauthorized_attempts,
                "last_received_at": (
                    self._last_received_at.isoformat() if self._last_received_at else None
                ),
                "last_automation": self._last_automation,
            }

    def reset(self) -> None:
        """Clear every counter and recency field (test isolation)."""
        with self._lock:
            self._received = 0
            self._stored = 0
            self._duplicate = 0
            self._quarantined = 0
            self._unauthorized_attempts = 0
            self._last_received_at = None
            self._last_automation = None


# Process-wide singleton consumed by routes and the ingest pipeline.
STATS = Stats()
