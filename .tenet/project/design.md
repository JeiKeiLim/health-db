# Project Design

## Interaction Surfaces

1. **HAE ingest endpoint** — machine-only (the iOS app). No UI. Behavior contract documented in architecture.md.
2. **MCP HTTP server** — the primary human-agent surface, consumed by MCP clients (LLM agents). Interaction design is LLM-centric:
   - **Catalog-first grounding**: `health://metrics` resource and `list_metrics` tool let an agent discover metric names, units, and coverage before querying — no guessing identifiers
   - **Summaries by default**: tools return bounded, digestible output; `raw_samples` is paginated with explicit limits; detail tools (`get_workout_detail`) are opt-in depth
   - **URI-addressable resources**: `health://metrics`, `health://freshness`, `health://schema` — stable URIs an agent can attach/read for grounding
   - **Errors as guidance**: tool errors return actionable messages ("metric not found — see health://metrics catalog"), never stack traces
3. **CLI wrapper** — developer/intermediate testing surface; minimal flags, JSON to stdout, diagnostics to stderr (inherits predecessor conventions). Not the product.

## Language

- Metric names preserved exactly as HAE/HealthKit emits them (e.g. "Step Count", "Heart Rate Variability (SDNN)")
- Dates: `YYYY-MM-DD` for days (KST boundaries), ISO-8601 with offset for timestamps
- JSON output conventions inherited from predecessor (`sum`/`avg`/`min`/`max`/`count`/`unit` rollup shape)

## Feedback

- Ingest responses: precise status codes (200 accepted / duplicate-idempotent, 400 malformed + quarantine id, 401 + audit, 503 + Retry-After) with brief machine-readable JSON bodies
- Sync health observable via `health://freshness` resource and `freshness_check` tool (median-based staleness semantics inherited from predecessor)

## Accessibility / Visual System

Not applicable — no human-facing UI in this project beyond terminal output. If dashboards are added later (Grafana against the DB), that is out of scope for this run.