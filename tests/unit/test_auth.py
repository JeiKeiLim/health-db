"""Unit tests for health_db.auth — token auth, audit, no-secret-leak.

Covers decomposition job slice-1-auth acceptance: missing token → 401,
wrong token → 401 + audit increment, correct token passes, and the secret
is never present in any response or audit material. Also covers the
fail-closed policy, constant-time comparison mechanics, request-client
attribution, and the bounded audit log.
"""

from __future__ import annotations

import threading

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import health_db.auth as auth
import health_db.config as config
from health_db.auth import (
    AUDIT_LOG,
    REASON_AUTH_NOT_CONFIGURED,
    REASON_INVALID_TOKEN,
    REASON_MISSING_TOKEN,
    AuditEntry,
    AuditLog,
    check_token,
    failure_reason,
    require_token,
)

SECRET = "unit-test-secret-value"
HEADER = "X-Ingest-Token"

SECRET_VARIANTS = (
    "unit-test-secret-value",
    "unit-test-secret-value\n",
    " unit-test-secret-value",
    "UNIT-TEST-SECRET-VALUE",
    "unit-test-secret-valu",
    "unit-test-secret-valueX",
)


@pytest.fixture(autouse=True)
def _clear_audit():
    AUDIT_LOG.clear()
    yield
    AUDIT_LOG.clear()


def make_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    secret: str | None = SECRET,
    header: str = HEADER,
) -> TestClient:
    """Fresh FastAPI app with the auth dependency applied to a probe route."""
    monkeypatch.setattr(config, "INGEST_SECRET", secret)
    monkeypatch.setattr(config, "INGEST_HEADER", header)
    app = FastAPI(dependencies=[Depends(require_token)])

    @app.get("/probe")
    def probe() -> dict[str, str]:
        return {"ok": "true"}

    return TestClient(app)


# ---------------------------------------------------------------------------
# Constant-time comparison mechanics
# ---------------------------------------------------------------------------


class TestCheckToken:
    def test_correct_token_passes(self):
        assert check_token(SECRET, SECRET) is True

    def test_missing_provided_or_expected_fails_closed(self):
        assert check_token(None, SECRET) is False
        assert check_token("", SECRET) is False
        assert check_token(SECRET, None) is False
        assert check_token(SECRET, "") is False
        assert check_token(None, None) is False

    def test_wrong_token_fails(self):
        assert check_token("wrong", SECRET) is False

    def test_near_misses_fail(self):
        for variant in SECRET_VARIANTS[1:]:
            assert check_token(variant, SECRET) is False, f"variant {variant!r} must fail"

    def test_whitespace_and_case_differences_fail(self):
        assert check_token(" unit-test-secret-value ", SECRET) is False
        assert check_token(SECRET.upper(), SECRET) is False

    def test_non_ascii_token_does_not_raise(self):
        assert check_token("日本語トークン", SECRET) is False

    @pytest.mark.parametrize(
        ("provided", "expected"),
        [
            ("a", "b"),
            ("a" * 1, "a" * 2),
            ("x" * 500, "y" * 500),
            ("日本語トークン", SECRET),
            (SECRET, SECRET),
        ],
    )
    def test_unequal_lengths_no_type_error(self, provided, expected):
        check_token(provided, expected)


# ---------------------------------------------------------------------------
# Audit log (bounded, thread-safe, fixed reasons)
# ---------------------------------------------------------------------------


class TestAuditLog:
    def test_record_captures_request_metadata_only(self):
        log = AuditLog()
        log.record(ip="203.0.113.7", user_agent="HAE/1.0", reason=REASON_INVALID_TOKEN)
        (entry,) = log.entries()
        assert isinstance(entry, AuditEntry)
        assert entry.ip == "203.0.113.7"
        assert entry.user_agent == "HAE/1.0"
        assert entry.reason == REASON_INVALID_TOKEN
        assert entry.ts.tzinfo is not None

    def test_audit_entries_never_contain_secret_or_token(self):
        log = AuditLog()
        # user_agent is request-controlled, so it may echo arbitrary material
        # (that is the attacker's own string, not our secret) — the audit
        # fields *we* generate (ts, ip, reason) must never contain it.
        log.record(ip="10.0.0.1", user_agent=f"evil {SECRET}", reason=REASON_INVALID_TOKEN)
        entry = log.entries()[0]
        assert SECRET not in entry.reason
        assert SECRET not in entry.ip

    def test_reasons_are_fixed_strings(self):
        assert failure_reason(None, SECRET) == REASON_MISSING_TOKEN
        assert failure_reason("bad", SECRET) == REASON_INVALID_TOKEN
        assert failure_reason(SECRET, None) == REASON_AUTH_NOT_CONFIGURED
        assert failure_reason(None, "") == REASON_AUTH_NOT_CONFIGURED
        for reason in (REASON_MISSING_TOKEN, REASON_INVALID_TOKEN, REASON_AUTH_NOT_CONFIGURED):
            assert reason not in (SECRET, "")  # never request material

    def test_count_grows_even_beyond_capacity(self):
        log = AuditLog(max_entries=3)
        for i in range(5):
            log.record(ip=f"10.0.0.{i}", user_agent=None, reason=REASON_INVALID_TOKEN)
        assert log.count == 5
        assert len(log.entries()) == 3, "only most-recent entries retained"

    def test_entries_are_oldest_dropped(self):
        log = AuditLog(max_entries=2)
        log.record(ip="10.0.0.1", user_agent=None, reason=REASON_MISSING_TOKEN)
        log.record(ip="10.0.0.2", user_agent=None, reason=REASON_INVALID_TOKEN)
        log.record(ip="10.0.0.3", user_agent=None, reason=REASON_INVALID_TOKEN)
        ips = [e.ip for e in log.entries()]
        assert ips == ["10.0.0.2", "10.0.0.3"]

    def test_invalid_max_entries_rejected(self):
        with pytest.raises(ValueError):
            AuditLog(max_entries=0)

    def test_clear_resets(self):
        log = AuditLog()
        log.record(ip=None, user_agent=None, reason=REASON_MISSING_TOKEN)
        log.clear()
        assert log.count == 0
        assert log.entries() == ()

    def test_concurrent_record_is_thread_safe(self):
        log = AuditLog(max_entries=100_000)

        def hammer(n: int) -> None:
            for i in range(200):
                log.record(ip=f"10.0.{n}.{i}", user_agent=None, reason=REASON_INVALID_TOKEN)

        threads = [threading.Thread(target=hammer, args=(n,)) for n in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert log.count == 2000

    def test_module_singleton_is_shared_audit_log(self):
        assert isinstance(AUDIT_LOG, AuditLog)


# ---------------------------------------------------------------------------
# HTTP behavior via the FastAPI dependency (TestClient)
# ---------------------------------------------------------------------------


class TestRequireTokenHTTP:
    def test_correct_token_passes(self, monkeypatch):
        client = make_client(monkeypatch)
        res = client.get("/probe", headers={HEADER: SECRET})
        assert res.status_code == 200
        assert res.json() == {"ok": "true"}
        assert AUDIT_LOG.count == 0

    def test_missing_token_401(self, monkeypatch):
        client = make_client(monkeypatch)
        res = client.get("/probe")
        assert res.status_code == 401
        assert AUDIT_LOG.count == 1
        (entry,) = AUDIT_LOG.entries()
        assert entry.reason == REASON_MISSING_TOKEN

    def test_wrong_token_401_and_audited(self, monkeypatch):
        client = make_client(monkeypatch)
        res = client.get("/probe", headers={HEADER: "wrong-token"})
        assert res.status_code == 401
        assert AUDIT_LOG.count == 1
        (entry,) = AUDIT_LOG.entries()
        assert entry.reason == REASON_INVALID_TOKEN
        assert entry.ip == "testclient"  # request client attributed
        assert entry.user_agent is not None

    def test_401_is_small_json_no_stack_trace(self, monkeypatch):
        client = make_client(monkeypatch)
        res = client.get("/probe", headers={HEADER: "wrong-token"})
        assert res.status_code == 401
        assert res.headers["content-type"].startswith("application/json")
        assert res.text.count("\n") == 0, "single-line JSON body"
        body = res.json()
        assert set(body) == {"detail"}

    def test_secret_absent_from_response_and_audit(self, monkeypatch):
        client = make_client(monkeypatch)
        for headers in ({}, {HEADER: "wrong"}, {HEADER: SECRET[: len(SECRET) // 2]}):
            res = client.get("/probe", headers=headers)
            assert res.status_code == 401
            assert SECRET not in res.text
        assert all(
            SECRET not in str(f)
            for e in AUDIT_LOG.entries()
            for f in (e.reason, e.ip, e.user_agent, e.ts)
        )

    def test_configured_header_name_respected(self, monkeypatch):
        client = make_client(monkeypatch, header="X-Custom-Token")
        # token under the default header name must NOT authenticate
        res = client.get("/probe", headers={HEADER: SECRET})
        assert res.status_code == 401
        res = client.get("/probe", headers={"X-Custom-Token": SECRET})
        assert res.status_code == 200

    def test_fail_closed_when_secret_unset(self, monkeypatch):
        client = make_client(monkeypatch, secret=None)
        res = client.get("/probe", headers={HEADER: SECRET})
        assert res.status_code == 401
        res = client.get("/probe", headers={HEADER: ""})
        assert res.status_code == 401
        res = client.get("/probe", headers={HEADER: "anything"})
        assert res.status_code == 401
        assert AUDIT_LOG.count == 3
        assert all(e.reason == REASON_AUTH_NOT_CONFIGURED for e in AUDIT_LOG.entries())

    def test_fail_closed_when_secret_blank(self, monkeypatch):
        client = make_client(monkeypatch, secret="")
        assert client.get("/probe", headers={HEADER: SECRET}).status_code == 401

    def test_each_failure_audited_independently(self, monkeypatch):
        client = make_client(monkeypatch)
        client.get("/probe")  # missing
        client.get("/probe", headers={HEADER: "bad"})  # invalid
        client.get("/probe", headers={"Other": "x"})  # missing again
        assert AUDIT_LOG.count == 3
        assert [e.reason for e in AUDIT_LOG.entries()] == [
            REASON_MISSING_TOKEN,
            REASON_INVALID_TOKEN,
            REASON_MISSING_TOKEN,
        ]

    def test_success_after_failures_stops_auditing(self, monkeypatch):
        client = make_client(monkeypatch)
        client.get("/probe", headers={HEADER: "bad"})
        client.get("/probe", headers={HEADER: SECRET})
        assert AUDIT_LOG.count == 1  # only the failed attempt is audited

    def test_user_agent_and_ip_captured(self, monkeypatch):
        client = make_client(monkeypatch)
        client.get("/probe", headers={HEADER: "bad", "User-Agent": "pytest-agent/1.0"})
        (entry,) = AUDIT_LOG.entries()
        assert entry.user_agent == "pytest-agent/1.0"
        assert entry.ip == "testclient"

    def test_dependency_rejects_post_like_ingest_route(self, monkeypatch):
        """Behavioral: a POST ingest-style route is guarded the same way."""
        monkeypatch.setattr(config, "INGEST_SECRET", SECRET)
        monkeypatch.setattr(config, "INGEST_HEADER", HEADER)
        app = FastAPI(dependencies=[Depends(require_token)])

        @app.post("/api/ingest")
        def ingest() -> dict[str, str]:
            return {"outcome": "stored"}

        client = TestClient(app)
        res = client.post("/api/ingest", content=b'{"data": {}}')
        assert res.status_code == 401
        res = client.post("/api/ingest", content=b'{"data": {}}', headers={HEADER: SECRET})
        assert res.status_code == 200
        assert res.json() == {"outcome": "stored"}


# ---------------------------------------------------------------------------
# Iron law 1: no secret in __repr__-adjacent surfaces (belt & braces)
# ---------------------------------------------------------------------------


class TestNoLeakageSurfaces:
    def test_auth_error_default_detail_is_generic(self, monkeypatch):
        client = make_client(monkeypatch)
        res = client.get("/probe", headers={HEADER: "wrong"})
        detail = res.json()["detail"]
        assert detail == auth.UNAUTHORIZED_DETAIL
        assert SECRET not in detail
        assert "wrong" not in detail  # never echoes the supplied token
        assert "token" in detail.lower()  # but is actionable in kind

    def test_401_body_shape_is_stable_across_failure_modes(self, monkeypatch):
        client = make_client(monkeypatch)
        bodies = {
            client.get("/probe", headers=h).text for h in ({}, {HEADER: "wrong"}, {HEADER: ""})
        }
        # missing / invalid / blank-token all share one generic body
        assert len(bodies) == 1
        assert "Unauthorized" in bodies.pop()
