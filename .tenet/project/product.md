# Project Product

## User-Facing Behavior

1. **HAE sync (write path)**: The HAE app runs three automations — Health Metrics (all metrics, hourly aggregation), Health Metrics (Heart Rate + Step Count only, minute aggregation, disjoint set), Workouts (workout metrics + routes, minute grouping). Payloads POST to the ingest endpoint with a token header.
2. **Ingest acceptance**: Valid payloads return 200 in < 2s and are archived verbatim on disk before any further processing (archive-first: nothing is ever lost). Malformed payloads → 400 + quarantine archive. Unknown metrics → accepted and auto-registered. Bad token → 401 + audit log (IP, UA). DB outage → 503 + Retry-After, payload still archived. Duplicate re-posts → idempotent 200.
3. **Query path (MCP HTTP server)**: MCP clients (LLM agents) interact via streamable HTTP with token auth (Bearer). Surface is MCP-native, not a CLI port:
   - Tools: `query_day`, `get_metric_series`, `list_metrics`, `list_workouts`, `get_workout_detail` (HR + routes), `get_trends`, `freshness_check`, `raw_samples` (paginated)
   - Resources (URI-addressable): `health://metrics` (catalog with units & coverage), `health://freshness`, `health://schema`
   - Design goals: catalog-first grounding, pagination everywhere, summaries by default, LLM-friendly output shaping
4. **CLI wrapper**: a thin CLI mirrors core commands for intermediate development testing only; MCP is the product surface.

## Stable Requirements

- Quantified targets: ack < 2s; verbatim archive 100%; zero unexplained errors over post-deploy validation; backfill ≤ 1 day; hourly delta within 5 min of the hour; rollup query < 1s for 90-day ranges.
- Failure semantics: archive-first safety (see above); duplicates idempotent via content-hash skip or latest-wins upsert.

## Non-Goals

- No analysis features (stress, training load, correlations) — deferred, out of scope for this run.
- No SQLite migration — history comes from HAE full sync.
- No multi-user support — single data subject.
- No migration of the TCP/MCP-server direction of HAE (app-as-server); this project only consumes the REST export direction.

## Product Boundaries

- External surface: exactly one HTTP service (ingest + MCP on the same FastAPI app, or co-deployed components), fronted later by TLS reverse proxy / port-forward. PostgreSQL is never exposed outside LAN/VPN.
- predecessor remains running on AutoSync during the overlap/validation period and is retired after cutover.