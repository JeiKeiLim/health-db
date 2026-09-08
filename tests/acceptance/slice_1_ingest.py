"""Acceptance tests — slice 1 (ingest validation, no DB).

Generated from .tenet/runs/2026-09-08-hae-ingest/scenarios.md.
Run with: uv run pytest tests/acceptance -m slice1

These are executable definitions of "done" for slice 1. They fail until
implementation lands (expected). They run against the FastAPI app via
TestClient / ASGI transport — no browser, no database.

Slice-1 scope: POST /api/ingest accepts real-shaped HAE JSON v2 payloads,
archives them verbatim, dedupes by sha256, quarantines malformed bodies,
rejects bad tokens with 401+audit, enforces the payload cap, and reports
stats. NO PostgreSQL writes in this slice.
"""

import hashlib
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.slice1

# The stored-outcome response contract (spec §API Endpoints): exactly these
# 8 keys, every one asserted in the acceptance tests below.
STORED_RESPONSE_KEYS = {
    "outcome",
    "data_type",
    "metrics",
    "samples",
    "workouts",
    "unknown_formats",
    "archive_path",
    "sha256",
}

# ---------------------------------------------------------------------------
# Fixtures: realistic HAE JSON v2 payload shapes (from research/hae-json-v2-format.md)
# ---------------------------------------------------------------------------


def make_metrics_payload(automation: str = "metrics-all") -> dict:
    return {
        "data": {
            "metrics": [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [
                        {"qty": 8500, "date": "2026-09-08 09:00:00 +0900", "source": "Apple Watch"},
                        {"qty": 120, "date": "2026-09-08 10:00:00 +0900", "source": "Apple Watch"},
                    ],
                },
                {  # heart_rate: Min/Avg/Max shape, no qty
                    "name": "heart_rate",
                    "units": "count/min",
                    "data": [
                        {"date": "2026-09-08 09:00:00 +0900", "Min": 65, "Avg": 72, "Max": 85},
                    ],
                },
                {  # sleep_analysis aggregated shape, no qty
                    "name": "sleep_analysis",
                    "units": "hr",
                    "data": [
                        {
                            "date": "2026-09-08",
                            "totalSleep": 7.5,
                            "asleep": 7.0,
                            "core": 3.5,
                            "deep": 1.5,
                            "rem": 2.0,
                            "sleepStart": "2026-09-07 23:00:00 +0900",
                            "sleepEnd": "2026-09-08 06:30:00 +0900",
                            "inBed": 8.0,
                            "inBedStart": "2026-09-07 22:45:00 +0900",
                            "inBedEnd": "2026-09-08 06:45:00 +0900",
                        },
                    ],
                },
            ],
            "workouts": [],
        }
    }


def make_workout_payload() -> dict:
    return {
        "data": {
            "metrics": [],
            "workouts": [
                {
                    "id": "CC8B17E2-9B4A-4C1D-8E2F-0A1B2C3D4E5F",
                    "name": "Running",
                    "start": "2026-09-08 07:02:00 +0900",
                    "end": "2026-09-08 07:47:00 +0900",
                    "duration": 2700,
                    "activeEnergy": 1420.8,  # kJ
                    "heartRateData": [{"date": "2026-09-08 07:03:00 +0900", "bpm": 128}],
                    "route": [
                        {
                            "timestamp": "2026-09-08 07:03:00 +0900",
                            "latitude": 37.33,
                            "longitude": 127.01,
                            "altitude": 10.0,
                            "course": 180.0,
                            "speed": 2.5,
                            "horizontalAccuracy": 5.0,
                            "verticalAccuracy": 8.0,
                        }
                    ],
                }
            ],
        }
    }


def body(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def assert_stored_shape(out: dict) -> None:
    """Assert the full 8-key stored-response contract (no missing, no extra)."""
    assert set(out) == STORED_RESPONSE_KEYS, (
        f"stored response must carry exactly the 8 contract keys; got {sorted(out)}"
    )
    assert out["outcome"] == "stored"
    assert isinstance(out["data_type"], str) and out["data_type"] in {"metrics", "workouts"}
    assert isinstance(out["metrics"], int) and out["metrics"] >= 0
    assert isinstance(out["samples"], int) and out["samples"] >= 0
    assert isinstance(out["workouts"], int) and out["workouts"] >= 0
    assert isinstance(out["unknown_formats"], list)
    for entry in out["unknown_formats"]:
        assert set(entry) == {"name", "units", "reason"}
    assert out["archive_path"], "must report the on-disk archive path"
    assert Path(out["archive_path"]).is_file(), "reported archive_path must exist on disk"
    assert isinstance(out["sha256"], str) and len(out["sha256"]) == 64


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """ASGI TestClient wired to a temp archive dir + known token."""
    from fastapi.testclient import TestClient

    import health_db.config as config
    import health_db.main as main  # noqa: F401  (app factory)

    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setattr(config, "INGEST_SECRET", "test-secret-token")
    monkeypatch.setattr(config, "INGEST_HEADER", "X-Ingest-Token")
    monkeypatch.setattr(config, "MAX_PAYLOAD_BYTES", 64 * 1024 * 1024)
    from health_db.main import app

    return TestClient(app)


HDR = {"X-Ingest-Token": "test-secret-token"}


# ---------------------------------------------------------------------------
# Scenario 2 (slice-1 part): real-app validation — accept + archive + stats
# ---------------------------------------------------------------------------


class TestAcceptsAndArchives:
    def test_valid_metrics_payload_200_stored(self, client):
        b = body(make_metrics_payload())
        res = client.post("/api/ingest", content=b, headers=HDR)
        assert res.status_code == 200
        out = res.json()
        assert_stored_shape(out)
        assert out["data_type"] == "metrics"
        assert out["metrics"] == 3  # 3 metric groups seen
        assert out["samples"] == 4  # 2+1+1 data points
        assert out["workouts"] == 0
        assert out["unknown_formats"] == []
        assert out["sha256"] == sha(b)

    def test_valid_workout_payload_200_stored(self, client):
        b = body(make_workout_payload())
        res = client.post("/api/ingest", content=b, headers=HDR)
        assert res.status_code == 200
        out = res.json()
        assert_stored_shape(out)
        assert out["data_type"] == "workouts"
        assert out["metrics"] == 0
        assert out["samples"] == 0
        assert out["workouts"] == 1
        assert out["unknown_formats"] == []
        assert out["sha256"] == sha(b)

    def test_payload_archived_verbatim_on_disk(self, client, tmp_path):
        b = body(make_metrics_payload())
        client.post("/api/ingest", content=b, headers=HDR)
        archived = (tmp_path / "archive").rglob("*.json")
        files = list(archived)
        assert len(files) == 1
        assert files[0].read_bytes() == b, "archive must be byte-identical"

    def test_stats_endpoint_counts(self, client):
        client.post("/api/ingest", content=body(make_metrics_payload()), headers=HDR)
        client.post("/api/ingest", content=body(make_workout_payload()), headers=HDR)
        res = client.get("/api/ingest/stats", headers=HDR)
        assert res.status_code == 200
        stats = res.json()
        assert stats["received"] == 2
        assert stats["stored"] == 2
        assert stats["duplicate"] == 0
        assert stats["quarantined"] == 0
        assert stats["last_received_at"]

    def test_health_endpoint_ok(self, client):
        res = client.get("/api/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Failure semantics (spec table): 401 / 400+quarantine / duplicate / 413
# ---------------------------------------------------------------------------


class TestFailureSemantics:
    def test_missing_token_401(self, client):
        res = client.post("/api/ingest", content=body(make_metrics_payload()))
        assert res.status_code == 401

    def test_wrong_token_401_and_audited_body_not_archived(self, client, tmp_path):
        res = client.post(
            "/api/ingest", content=body(make_metrics_payload()), headers={"X-Ingest-Token": "wrong"}
        )
        assert res.status_code == 401
        assert not list((tmp_path / "archive").rglob("*")), "untrusted body must NOT be archived"
        # audit exists and carries no token material
        audit = client.get("/api/ingest/stats", headers=HDR).json()
        assert audit["unauthorized_attempts"] == 1

    def test_malformed_json_400_quarantined(self, client, tmp_path):
        res = client.post("/api/ingest", content=b"{broken", headers=HDR)
        assert res.status_code == 400
        assert res.json()["quarantine"]
        q = list((tmp_path / "archive" / "quarantine").rglob("*"))
        assert len(q) == 1, "malformed payload archived to quarantine/"

    def test_wrong_shape_400_quarantined(self, client):
        res = client.post("/api/ingest", content=json.dumps({"nope": 1}).encode(), headers=HDR)
        assert res.status_code == 400

    def test_duplicate_repost_idempotent_200(self, client):
        b = body(make_metrics_payload())
        first = client.post("/api/ingest", content=b, headers=HDR).json()
        second = client.post("/api/ingest", content=b, headers=HDR)
        assert second.status_code == 200
        out = second.json()
        assert out["outcome"] == "duplicate"
        assert out["sha256"] == first["sha256"]
        stats = client.get("/api/ingest/stats", headers=HDR).json()
        assert stats["stored"] == 1 and stats["duplicate"] == 1

    def test_oversized_413_not_archived(self, client, tmp_path, monkeypatch):
        import health_db.config as config

        monkeypatch.setattr(config, "MAX_PAYLOAD_BYTES", 1024)
        big = b"x" * 2048
        res = client.post("/api/ingest", content=big, headers=HDR)
        assert res.status_code == 413
        assert not list((tmp_path / "archive").rglob("*.json"))


# ---------------------------------------------------------------------------
# Anti-scenario 2: lost payload on processing failure (must never happen)
# ---------------------------------------------------------------------------


class TestArchiveFirstNeverLosesPayload:
    def test_malformed_is_still_on_disk(self, client, tmp_path):
        """400 responses must still leave the payload in quarantine."""
        client.post("/api/ingest", content=b"garbage-not-json", headers=HDR)
        assert list((tmp_path / "archive" / "quarantine").rglob("*"))


# ---------------------------------------------------------------------------
# Anti-scenario 5: secret leakage
# ---------------------------------------------------------------------------


class TestNoSecretLeakage:
    def test_401_response_carries_no_secret(self, client):
        res = client.post(
            "/api/ingest", content=body(make_metrics_payload()), headers={"X-Ingest-Token": "wrong"}
        )
        assert "test-secret-token" not in res.text

    def test_stats_carries_no_secret(self, client):
        res = client.get("/api/ingest/stats", headers=HDR)
        assert "test-secret-token" not in res.text


# ---------------------------------------------------------------------------
# Unknown metric auto-accept (spec: accepted and reported, not rejected)
# ---------------------------------------------------------------------------


class TestUnknownMetricAccepted:
    def test_unknown_metric_reported_as_accepted(self, client):
        payload = {
            "data": {
                "metrics": [
                    {
                        "name": "some_future_metric",
                        "units": "count",
                        "data": [{"qty": 3, "date": "2026-09-08 09:00:00 +0900"}],
                    },
                ],
                "workouts": [],
            }
        }
        res = client.post("/api/ingest", content=body(payload), headers=HDR)
        assert res.status_code == 200
        out = res.json()
        assert_stored_shape(out)
        assert out["metrics"] == 1


# ---------------------------------------------------------------------------
# HAE automation headers → stats.last_automation wiring (spec §API Endpoints)
# ---------------------------------------------------------------------------


class TestAutomationHeadersToStats:
    def test_automation_headers_flow_to_stats_last_automation(self, client):
        """POST with the HAE automation headers → GET /api/ingest/stats
        (authed) reports last_automation reflecting automation-name."""
        headers = {
            **HDR,
            "automation-name": "metrics-all",
            "automation-id": "B14E8EA2-9C43-4FBB-9BC1-A5C57D0D2B4F",
            "session-id": "e5f6a7b8-c9d0-4e1f-a2b3-4c5d6e7f8a90",
        }
        res = client.post("/api/ingest", content=body(make_metrics_payload()), headers=headers)
        assert res.status_code == 200
        assert res.json()["outcome"] == "stored"

        stats = client.get("/api/ingest/stats", headers=HDR).json()
        assert stats["last_automation"] is not None
        assert "automation-name=metrics-all" in stats["last_automation"]
        assert "automation-id=B14E8EA2-9C43-4FBB-9BC1-A5C57D0D2B4F" in stats["last_automation"]
        assert "session-id=e5f6a7b8-c9d0-4e1f-a2b3-4c5d6e7f8a90" in stats["last_automation"]

    def test_second_automation_overwrites_last_automation(self, client):
        """The most recent labeled payload wins (hourly automations differ)."""
        client.post(
            "/api/ingest",
            content=body(make_metrics_payload()),
            headers={**HDR, "automation-name": "metrics-all"},
        )
        client.post(
            "/api/ingest",
            content=body(make_workout_payload()),
            headers={**HDR, "automation-name": "workouts"},
        )
        stats = client.get("/api/ingest/stats", headers=HDR).json()
        assert "automation-name=workouts" in stats["last_automation"]
        assert "automation-name=metrics-all" not in stats["last_automation"]

    def test_missing_automation_headers_leave_last_automation_unchanged(self, client):
        """A later payload without automation headers must NOT clear or
        replace the previous label (stats contract: label kept, not reset)."""
        client.post(
            "/api/ingest",
            content=body(make_metrics_payload()),
            headers={**HDR, "automation-name": "metrics-hr-steps"},
        )
        before = client.get("/api/ingest/stats", headers=HDR).json()["last_automation"]
        assert before is not None

        res = client.post("/api/ingest", content=body(make_workout_payload()), headers=HDR)
        assert res.status_code == 200
        stats = client.get("/api/ingest/stats", headers=HDR).json()
        assert stats["last_automation"] == before, (
            "unlabeled payload keeps the previous last_automation"
        )

    def test_no_automation_headers_ever_last_automation_stays_none(self, client):
        """Without any labeled payload, last_automation stays None."""
        res = client.post("/api/ingest", content=body(make_metrics_payload()), headers=HDR)
        assert res.status_code == 200
        stats = client.get("/api/ingest/stats", headers=HDR).json()
        assert stats["last_automation"] is None
        assert stats["received"] == 1 and stats["stored"] == 1

    def test_partial_automation_headers_label_only_present_ones(self, client):
        """Only present headers contribute to the label (fixed order k=v)."""
        client.post(
            "/api/ingest",
            content=body(make_metrics_payload()),
            headers={**HDR, "automation-name": "workouts", "session-id": "s-123"},
        )
        label = client.get("/api/ingest/stats", headers=HDR).json()["last_automation"]
        assert label == "automation-name=workouts; session-id=s-123"
