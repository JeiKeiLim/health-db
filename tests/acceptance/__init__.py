"""Markers for slice-tagged acceptance tests + disposable-postgres fixture contract.

Harness contract (from the run harness):
- slice1 tests: plain pytest, ASGI TestClient, no DB.
- slice2 tests: marked `requires_pg`; run only when INTEGRATION_TEST_DB points
  at a DISPOSABLE postgres container (never a production DB — iron law 7).
  The concrete fixture lives in tests/conftest.py (created by slice-2 jobs).
"""

pytest_plugins: list[str] = []
