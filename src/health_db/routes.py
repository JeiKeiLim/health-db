"""FastAPI router for the ingest service endpoints (decomposition job slice-1-stats).

Routes are defined here and mounted by the app factory (slice-1-api builds
``main.py`` around this router) so the endpoints can be unit-tested against
a throwaway FastAPI app before the real pipeline exists.

- ``GET /api/health`` — public liveness: ``{"status": "ok", "version":
  config.APP_VERSION}``. Never touches auth or the request body.
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


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — public, no auth, no DB (DB check lands in slice 2)."""
    return {"status": "ok", "version": config.APP_VERSION}


@router.get(
    "/ingest/stats",
    dependencies=[Depends(require_token)],
)
def ingest_stats() -> dict[str, object]:
    """Ingest counters for the post-deploy validation (token-protected)."""
    return stats.STATS.snapshot()
