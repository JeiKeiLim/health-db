# job-8 hrn from-to filter crashed masked

type: journal
source_job: 00000000-0000-0000-0000-000000000000
job_name: unknown
created: 2026-09-19T15:51:31.832Z

## Findings

- **bug**: list_heart_notifications(frm, to) crashed with psycopg.errors.UndefinedTable ('missing FROM-clause entry for table n') whenever from/to day filters were passed. _day_range_where('n', ...) emits WHERE n.day_kst... but the FROM clause aliased the table 'h'. With no filters the fragment is 'WHERE TRUE' so the no-filter path worked; acceptance TestMcpTools never passed from/to to hrn, so scripted checks missed it.
- **fix**: src/health_db/queries.py list_heart_notifications SQL: alias heart_rate_notifications as 'n' consistently (SELECT n.*, FROM heart_rate_notifications n, ORDER BY n.start_ts). 4 lines, left in working tree (uncommitted; not job scope to commit).
- **fix_verified**: full suite 739 passed + ruff clean after fix; live uvicorn MCP re-probe: hrn from/to hit=1 miss=0, ecgs filters, paging, auth all pass; live CLI re-probe: health hrn --from/--to hit/miss correct, exit codes 0/1/2 correct
- **impact**: Every agent/user using list_heart_notifications with a day range (the documented primary use) got a masked 'Error executing tool' -- violating the actionable-errors contract and spec section 7.
- **lesson**: When a shared WHERE-fragment helper injects an alias, add a filtered-path test per tool. Filter-path coverage for list_ecgs existed; hrn's was absent.
- **verification_env**: disposable postgres:18 @127.0.0.1:15499 + real uvicorn health-mcp on :8199 (streamable-http, Bearer) + health CLI, seeded via real pipeline.ingest_payload; probes under /tmp/e2e_seed.py, /tmp/e2e_probe*.py, /tmp/e2e_regress.py
