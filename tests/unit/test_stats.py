"""Unit tests for health_db.stats and health_db.routes (slice-1-stats).

Covers decomposition job slice-1-stats acceptance: counters increment per
outcome, snapshot() matches the spec shape (spec.md §API Endpoints), and
GET /api/ingest/stats requires the ingest token while GET /api/health is
public. Snapshot content is verified as JSON-ready — no secret ever
appears in the body (iron law 1).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import health_db.config as config
import health_db.stats as stats_module
from health_db.auth import AUDIT_LOG
from health_db.routes import router
from health_db.stats import STATS, Stats

SECRET = "unit-test-stats-secret"
HDR = {"X-Ingest-Token": SECRET}


@pytest.fixture(autouse=True)
def _isolate():
    """Fresh counters + audit log for every test; known secret."""
    STATS.reset()
    AUDIT_LOG.clear()
    yield
    STATS.reset()
    AUDIT_LOG.clear()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Minimal FastAPI app mounting the routes router with a known token."""
    monkeypatch.setattr(config, "INGEST_SECRET", SECRET)
    monkeypatch.setattr(config, "INGEST_HEADER", "X-Ingest-Token")
    monkeypatch.setattr(config, "APP_VERSION", "9.9.9-test")
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Stats counters — increment semantics per outcome
# ---------------------------------------------------------------------------


class TestCounters:
    def test_fresh_snapshot_is_zeroed(self):
        snap = Stats().snapshot()
        assert snap == {
            "received": 0,
            "stored": 0,
            "duplicate": 0,
            "quarantined": 0,
            "unauthorized_attempts": 0,
            "last_received_at": None,
            "last_automation": None,
        }

    def test_record_received_increments_and_stamps(self):
        s = Stats()
        at = datetime(2026, 9, 8, 1, 0, 0, tzinfo=UTC)
        s.record_received(automation="metrics-all", at=at)
        snap = s.snapshot()
        assert snap["received"] == 1
        assert snap["last_received_at"] == "2026-09-08T01:00:00+00:00"
        assert snap["last_automation"] == "metrics-all"

    def test_each_outcome_counter_is_independent(self):
        s = Stats()
        s.record_received()
        s.record_stored()
        s.record_stored()
        s.record_received()
        s.record_duplicate()
        s.record_quarantined()
        snap = s.snapshot()
        assert snap["received"] == 2
        assert snap["stored"] == 2
        assert snap["duplicate"] == 1
        assert snap["quarantined"] == 1
        assert snap["unauthorized_attempts"] == 0

    def test_unauthorized_counter_increments(self):
        s = Stats()
        for _ in range(3):
            s.record_unauthorized()
        assert s.snapshot()["unauthorized_attempts"] == 3

    def test_received_increments_for_every_outcome_path(self):
        """received counts authorized requests regardless of final outcome."""
        s = Stats()
        for _ in range(2):
            s.record_received()
            s.record_stored()
        s.record_received()
        s.record_duplicate()
        s.record_received()
        s.record_quarantined()
        assert s.snapshot()["received"] == 4

    def test_last_automation_kept_when_next_payload_unlabeled(self):
        s = Stats()
        s.record_received(automation="metrics-hr-steps")
        s.record_received()
        assert s.snapshot()["last_automation"] == "metrics-hr-steps"
        s.record_received(automation="workouts")
        assert s.snapshot()["last_automation"] == "workouts"

    def test_last_received_at_tracks_most_recent(self):
        s = Stats()
        first = datetime(2026, 9, 8, 1, 0, 0, tzinfo=UTC)
        second = first + timedelta(minutes=30)
        s.record_received(at=first)
        s.record_received(at=second)
        assert s.snapshot()["last_received_at"] == second.isoformat()

    def test_naive_timestamp_treated_as_utc(self):
        s = Stats()
        s.record_received(at=datetime(2026, 9, 8, 12, 0, 0))
        assert s.snapshot()["last_received_at"] == "2026-09-08T12:00:00+00:00"

    def test_non_utc_offset_normalized_to_utc(self):
        s = Stats()
        s.record_received(at=datetime(2026, 9, 8, 21, 0, 0, tzinfo=timezone(timedelta(hours=9))))
        assert s.snapshot()["last_received_at"] == "2026-09-08T12:00:00+00:00"

    def test_reset_clears_everything(self):
        s = Stats()
        s.record_received(automation="workouts")
        s.record_stored()
        s.record_unauthorized()
        s.reset()
        assert s.snapshot() == Stats().snapshot()

    def test_concurrent_recording_never_loses_counts(self):
        s = Stats()
        n_threads, n_increments = 8, 250

        def worker():
            for _ in range(n_increments):
                s.record_received()
                s.record_stored()

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = s.snapshot()
        assert snap["received"] == n_threads * n_increments
        assert snap["stored"] == n_threads * n_increments

    # Regression: code_critic found record_received computing the timestamp
    # outside the lock and assigning last_received_at unconditionally, so a
    # concurrent older request could regress the recency stamp.

    def test_out_of_order_call_never_regresses_last_received_at(self):
        s = Stats()
        newer = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
        older = newer - timedelta(minutes=30)
        assert s.record_received(at=newer) == newer
        s.record_received(at=older)
        assert s.snapshot()["last_received_at"] == newer.isoformat()

    def test_equal_timestamp_does_not_regress(self):
        s = Stats()
        at = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
        s.record_received(at=at)
        s.record_received(at=at.replace())  # equal instant, distinct object
        assert s.snapshot()["last_received_at"] == at.isoformat()

    def test_concurrent_out_of_order_stamps_never_regress(self):
        """Threads record pinned stamps in reverse order; the max must win."""
        s = Stats()
        n_threads = 16
        base = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
        stamps = [base - timedelta(minutes=i) for i in range(n_threads)]
        barrier = threading.Barrier(n_threads)

        def worker(idx: int) -> None:
            barrier.wait()  # maximize interleaving of clock-vs-lock ordering
            s.record_received(at=stamps[idx])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        expected = max(stamps).isoformat()
        assert s.snapshot()["last_received_at"] == expected

    def test_interleaved_live_clock_calls_never_regress(self):
        """Repeated live-clock recordings under contention only move forward."""
        s = Stats()
        first = s.record_received()
        n_threads, n_calls = 4, 50

        def worker() -> None:
            for _ in range(n_calls):
                s.record_received()

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        current = datetime.fromisoformat(s.snapshot()["last_received_at"])
        assert current >= first  # never regressed below the earlier stamp


# ---------------------------------------------------------------------------
# Snapshot shape — spec.md §API Endpoints contract
# ---------------------------------------------------------------------------


class TestSnapshotShape:
    def test_snapshot_keys_match_spec(self):
        STATS.record_received(automation="metrics-all")
        snap = STATS.snapshot()
        assert set(snap) == {
            "received",
            "stored",
            "duplicate",
            "quarantined",
            "unauthorized_attempts",
            "last_received_at",
            "last_automation",
        }
        assert list(snap) == list(stats_module.SNAPSHOT_KEYS)

    def test_snapshot_is_json_serializable(self):
        STATS.record_received(automation="workouts")
        STATS.record_stored()
        STATS.record_unauthorized()
        encoded = json.dumps(STATS.snapshot())
        assert json.loads(encoded) == STATS.snapshot()

    def test_snapshot_never_carries_the_secret(self, client):
        STATS.record_received(automation="metrics-all")
        body = client.get("/api/ingest/stats", headers=HDR).text
        assert SECRET not in body

    def test_singleton_is_the_module_instance(self):
        assert stats_module.STATS is STATS
        assert isinstance(STATS, Stats)


# ---------------------------------------------------------------------------
# GET /api/health — public liveness
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def test_health_ok_public_no_token(self, client):
        res = client.get("/api/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

    def test_health_reports_app_version(self, client):
        assert client.get("/api/health").json()["version"] == "9.9.9-test"

    def test_health_does_not_count_as_received(self, client):
        client.get("/api/health")
        assert client.get("/api/ingest/stats", headers=HDR).json()["received"] == 0


# ---------------------------------------------------------------------------
# GET /api/ingest/stats — token protection via require_token
# ---------------------------------------------------------------------------


class TestStatsEndpointProtection:
    def test_missing_token_401(self, client):
        res = client.get("/api/ingest/stats")
        assert res.status_code == 401

    def test_wrong_token_401(self, client):
        res = client.get("/api/ingest/stats", headers={"X-Ingest-Token": "wrong"})
        assert res.status_code == 401

    def test_correct_token_200(self, client):
        res = client.get("/api/ingest/stats", headers=HDR)
        assert res.status_code == 200
        assert set(res.json()) == set(stats_module.SNAPSHOT_KEYS)

    def test_401_body_carries_no_secret(self, client):
        res = client.get("/api/ingest/stats", headers={"X-Ingest-Token": "wrong"})
        assert SECRET not in res.text

    def test_401_attempt_is_audited_not_counted_as_received(self, client):
        client.get("/api/ingest/stats", headers={"X-Ingest-Token": "wrong"})
        assert AUDIT_LOG.count == 1
        snap = STATS.snapshot()
        assert snap["received"] == 0
        assert snap["unauthorized_attempts"] == 0  # auth audits; API mirrors into stats

    def test_stats_reflects_recorded_outcomes(self, client):
        STATS.record_received(automation="metrics-all")
        STATS.record_stored()
        STATS.record_duplicate()
        STATS.record_quarantined()
        STATS.record_unauthorized()
        snap = client.get("/api/ingest/stats", headers=HDR).json()
        assert snap == {
            "received": 1,
            "stored": 1,
            "duplicate": 1,
            "quarantined": 1,
            "unauthorized_attempts": 1,
            "last_received_at": snap["last_received_at"],
            "last_automation": "metrics-all",
        }
        assert snap["last_received_at"] is not None

    def test_configurable_header_name_is_respected(self, client, monkeypatch):
        monkeypatch.setattr(config, "INGEST_HEADER", "X-Custom-Token")
        ok = client.get("/api/ingest/stats", headers={"X-Custom-Token": SECRET})
        bad = client.get("/api/ingest/stats", headers={"X-Ingest-Token": SECRET})
        assert ok.status_code == 200
        assert bad.status_code == 401
