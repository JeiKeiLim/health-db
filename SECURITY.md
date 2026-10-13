# Security policy

## Scope

This repo ships a FastAPI ingest service, an MCP query server, supporting CLI
tools, and a Docker image. Everything assumes it runs on a network you control
(LAN/VPN); it is not a multi-tenant internet service.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** on this repository
(Report a vulnerability → Security tab), or open a security advisory. Please
do not open a public issue for anything exploitable. Expect a response within
a few days; the project is maintained by one person, so a fix may take longer
than a commercial SLA.

## Security posture (what is and is not protected)

- **Fail-closed secrets.** `PSQL_*` and `INGEST_SECRET` default to blank in the
  image; blank means every ingest/MCP request is rejected and the health probe
  never dials an ambient `localhost:5432`. Real values are injected at
  container start (`.env` / orchestrator env) — never baked into image layers.
  Never commit `.env` (it is gitignored).
- **Auth.** Ingest requires the `X-Ingest-Token` header; MCP requires
  `Authorization: Bearer` — both checked against `INGEST_SECRET`. If you
  expose the service beyond a trusted network, rotate the secret and put TLS
  in front (reverse proxy); the app itself serves plain HTTP.
- **DNS-rebinding protection.** The MCP transport validates the `Host` header
  against a localhost-only allowlist; add any external hostname with
  `MCP_ALLOWED_HOSTS`.
- **No code execution from data.** Payloads are parsed and stored, never
  evaluated. Unknown metric names auto-register as rows, not code.
- **Payload cap.** `MAX_PAYLOAD_BYTES` (64 MiB default) rejects oversized
  bodies with 413.
- **Container hardening.** The image runs as non-root UID 10001 and takes all
  configuration from the environment.
- **Your data is the sensitive part.** `data/archive/` holds verbatim health
  JSON — treat the mounted volume, DB, and backups as sensitive at rest;
  restrict access to the host and the PostgreSQL port (never expose :5432
  beyond your trusted network).