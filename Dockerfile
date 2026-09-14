# health-db single-service image: ingest (:8000) + MCP (:8100) + CLI tools.
# Secrets are NEVER baked in — .env is provided at runtime via compose
# env_file (iron law 1). Build: docker build -t health-db .

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