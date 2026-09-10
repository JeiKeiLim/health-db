#!/bin/sh
# health-db image entrypoint: apply migrations (idempotent), then exec the
# requested command. Default CMD serves ingest + MCP side by side.
set -eu

cd /app

health-migrate || {
    echo "entrypoint: migrations failed; check PSQL_* connectivity (stderr has no secrets)" >&2
    exit 1
}

# Default CMD is set by compose to "serve"; any other argv (health-replay,
# health CLI, ...) execs directly after migrations.
if [ "${1:-serve}" = "serve" ]; then
    exec uvicorn health_db.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" &
    exec uvicorn health_db.mcp_server:app --host "${MCP_HOST:-0.0.0.0}" --port "${MCP_PORT:-8100}"
fi
exec "$@"