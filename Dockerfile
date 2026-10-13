# check=skip=SecretsUsedInArgOrEnv
# health-db single-service image: ingest (:8000) + MCP (:8100) + CLI tools.
# Secrets are NEVER baked in — values arrive as environment variables at
# container start (.env via compose env_file, or your orchestrator's env
# settings). Build: docker build -t health-db .

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependency + project install from the lockfile (frozen, reproducible).
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MIGRATIONS_DIR=/app/migrations \
    ARCHIVE_ROOT=/app/data/archive \
    PENDING_DIR=/app/data/pending

# Runtime configuration keys, declared so Docker's create-container
# wizard lists them as editable fields and `docker inspect` documents them.
# Values come at container start (.env via compose env_file, or typed into
# the wizard) and override everything here — image layers are readable, so
# never put real secrets below.
#
# PSQL_* and INGEST_SECRET are declared blank on purpose. Blank is what keeps
# the app fail-closed: PSQL_* blank means "not configured" (the health probe
# never dials an ambient localhost:5432), and a blank INGEST_SECRET rejects
# every ingest/MCP request until a real secret is supplied. BuildKit's
# SecretsUsedInArgOrEnv lint fires on the names — that is expected; the
# values here are empty and the real ones are injected at runtime.
ENV PSQL_URL="" \
    PSQL_PORT="" \
    PSQL_USER="" \
    PSQL_TABLE="" \
    PSQL_PASSWORD="" \
    INGEST_SECRET="" \
    INGEST_HEADER="X-Ingest-Token" \
    MAX_PAYLOAD_BYTES="67108864" \
    HOST="0.0.0.0" \
    PORT="8000" \
    MCP_HOST="0.0.0.0" \
    MCP_PORT="8100" \
    MCP_ALLOWED_HOSTS=""

# Non-root runtime user; data/ (verbatim archive + pending queue) must be a
# writable volume mount — the archive is iron law 2 and must survive
# container recreation.
RUN useradd --create-home --uid 10001 health \
    && mkdir -p /app/data/archive /app/data/pending \
    && chown -R health:health /app/data

COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER health

EXPOSE 8000 8100

# Default: run migrations, then serve ingest + MCP together.
# One-off tools run instead: docker compose run health-db health-replay --pending
ENTRYPOINT ["/app/entrypoint.sh"]