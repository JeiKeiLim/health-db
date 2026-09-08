"""Markers for slice-tagged acceptance tests + disposable-postgres fixture contract.

Harness contract (from .tenet/runs/2026-09-08-hae-ingest/harness.md):
- slice1 tests: plain pytest, ASGI TestClient, no DB.
- slice2 tests: marked `requires_pg`; run only when INTEGRATION_TEST_DB points
  at a DISPOSABLE postgres container (never the hostprod DB — iron law 7).
  The concrete fixture lives in tests/conftest.py (created by slice-2 jobs).
"""

pytest_plugins: list[str] = []
