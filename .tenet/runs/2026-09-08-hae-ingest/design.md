# Run Design Direction

## Chosen Direction
- Selected mockup: `.tenet/runs/2026-09-08-hae-ingest/visuals/2026-09-08-01-final-product.html` (final-product view) — the destination approved at the plan-checkpoint
- Design rationale: health-db has no human web UI; its surfaces are JSON-over-HTTP (ingest), MCP tools/resources (agents), and terminal CLI. The design language is therefore "terminal/JSON realism": dark slate panels, monospace payloads, semantic colors for outcomes (green stored / amber quarantine / red 4xx). Wireframes visualize request/response flows with plausible real-shaped HAE payloads rather than screens.
- Project design doctrine used: `.tenet/project/design.md`
- Accepted component examples consulted: none — `.tenet/project/design-components/` is empty (greenfield project, nothing established yet)

## Run-Local Visual Principles
- Color palette: background `#0f172a` (slate-900), panels `#1e293b` (slate-800), borders `#334155`, primary text `#e2e8f0`, muted `#94a3b8`/`#64748b`; accent blue `#38bdf8` (queries/MCP), green `#22c55e` (stored/ok), amber `#f59e0b` (quarantine/archive), red `#ef4444` (401/errors), purple `#a855f7` (future TLS proxy)
- Typography: system sans (`-apple-system`) for chrome/headings, `"SF Mono", Menlo, monospace` for all payload/JSON content (matches the product's actual terminal output)
- Spacing: 1rem panel padding, 1.5rem gutters, 0.7rem pre padding
- Border radius: 6px (pre blocks) / 10-12px (panels)

## Component Patterns
- "Panels": rounded slate-800 cards with a small colored uppercase section label (`h3`, 0.8rem, colored by domain: green=ingest, blue=query)
- "Payload blocks": `pre` with dark background `#0b1120`, inline semantic-color spans (`ok`/`err`/`amber`/`dim`); response JSON always shows realistic values (counts, ms timings, dates around 2026-09)
- "Tables": compact 12px, slate borders, muted headers
- "Badges": pill-shaped status chips (b-green/b-blue/b-amber) for slice states
- Interaction: minimal — hover brightness on SVG architecture boxes; the walkthrough prototype uses JS screen toggling

## Layout
- Grid: 2-column flex (`display:flex; gap:1.5rem`) collapsing naturally on narrow windows (no breakpoints needed for internal artifacts)
- Responsive strategy: not applicable beyond natural flex wrap — these are design artifacts, not shipped UI

## Proposed Project Doctrine Updates
- If health-db ever grows a human-facing UI (e.g. Grafana or a web dashboard), promote this palette/typography baseline into `.tenet/project/design.md` at that time. No proposal now.