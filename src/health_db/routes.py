"""FastAPI router for the ingest service endpoints (decomposition job slice-1-stats).

Routes are defined here and mounted by the app factory (slice-1-api builds
``main.py`` around this router) so the endpoints can be unit-tested against
a throwaway FastAPI app before the real pipeline exists.

- ``GET /api/health`` — public liveness: ``{"status": "ok", "version":
  config.APP_VERSION, "db": <bool>}`` (slice-2-db-config). The ``db`` key
  reports live PostgreSQL connectivity (``db.pool.check_cached``) but must
  never break the endpoint: a down database still returns 200 with
  ``"db": false`` (the 503-ingest semantics belong to slice-2-ingest-
  pipeline and only apply to ``/api/ingest``, which is untouched here).
  The probe never constructs a pool against ambient defaults — it only
  reuses the materialized singleton, or builds one when ``PSQL_*`` env is
  explicitly configured — so unconfigured environments (tests, bare envs)
  always report ``"db": false`` without touching ``localhost:5432``.
- ``GET /api/ingest/stats`` — token-protected counters via the
  :func:`health_db.auth.require_token` dependency (the same dependency the
  ingest route uses, so 401 behavior and the configurable header name are
  identical). Reads only the :data:`health_db.stats.STATS` singleton.

The POST ``/api/ingest`` route itself belongs to slice-1-api (it wires the
full pipeline: auth → cap → sha256 → duplicate → archive → parse) and is
intentionally absent here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

import health_db.config as config
import health_db.stats as stats
from health_db.auth import require_token

router = APIRouter(prefix="/api")

# Tolerable wait for the /api/health DB probe: the endpoint must stay snappy
# even when the database is unreachable (borrow timeout, not connect hang).
_HEALTH_DB_PROBE_TIMEOUT_S = 1.0


@router.get("/health")
def health() -> dict[str, object]:
    """Liveness probe — public, no auth; reports DB connectivity (slice 2)."""
    # Imported lazily: slice-1-only environments (and unit tests that never
    # touch the DB) must not require psycopg at import time, and the probe
    # cache must stay out of app-import cost.
    from health_db.db.pool import check_cached

    return {
        "status": "ok",
        "version": config.APP_VERSION,
        # pool=None → check_cached resolves the materialized singleton itself
        # (never constructs one unless PSQL_* env is explicitly configured).
        "db": check_cached(timeout=_HEALTH_DB_PROBE_TIMEOUT_S),
    }


@router.get(
    "/ingest/stats",
    dependencies=[Depends(require_token)],
)
def ingest_stats() -> dict[str, object]:
    """Ingest counters for the post-deploy validation (token-protected)."""
    return stats.STATS.snapshot()
