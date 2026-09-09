"""Database access layer (slice 2).

- :mod:`health_db.db.pool` — psycopg3 connection pool from ``PSQL_*`` env.
- :mod:`health_db.db.migrations` — versioned-SQL migration applier.

Plain SQL only (harness: no ORM); the pool is the single connection source
for the ingest pipeline, migrations, queries, and replay.
"""
