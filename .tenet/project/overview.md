# Project Overview

## Purpose

`health-db` is the new home for the personal Apple Health data platform. It replaces the `predecessor` project (HAE AutoSync → `.hae`/JSON files → SQLite) with a direct pipeline: the **Health Auto Export (HAE)** iOS app POSTs JSON v2 exports over REST to a self-hosted ingest service, which normalizes and stores them in PostgreSQL. HealthKit remains the single source of truth — all history is backfilled via HAE itself, no SQLite migration.

## User Personas

- **Owner (single user)**: the only data subject and operator. Syncs an iPhone + Apple Watch.
- **Hermes agent / MCP clients**: LLM agents that query health data through the MCP HTTP server (tools + resources), used for personal insights and automation.
- **HAE iOS app**: the sole writer, pushing metrics/workouts via 3 REST automations.

## Success Metrics

- Ingest: every accepted payload acknowledged < 2s; 100% of payloads archived verbatim; zero unexplained 4xx/5xx during a post-deploy validation.
- Backfill: full ~3yr history loaded within one day of chunked manual exports.
- Delta syncs: hourly automations land within 5 minutes of the hour.
- Queries: MCP rollup-level responses < 1s for 90-day ranges.

## Current Status

Greenfield — implementation pending. PostgreSQL 18.6 target verified live at `<server-host>:5432` (database `health_app`, role `health_app`). Delivered in 2 agile slices: ① real-app ingest validation with raw payload capture (no DB), ② database + MCP query surface + backfill + server deployment.