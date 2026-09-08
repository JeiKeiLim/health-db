"""Acceptance tests — slice 2 (database + MCP + replay).

Generated from .tenet/runs/2026-09-08-hae-ingest/scenarios.md.
Tagged slice2 — NOT run by the slice-1 integration job; the slice-2 e2e job
runs slice-1 + slice-2 tests additively.

Slice-2 scope: PostgreSQL persistence (partitioned raw_samples, latest-wins
upserts, KST-day rollups), replay of archived payloads, workout/HR/route
storage, MCP streamable-HTTP server (8 tools / 3 resources, Bearer auth),
DB-outage safety. Requires a disposable postgres container via
INTEGRATION_TEST_DB (see harness.md iron law 7).
"""

import pytest

pytestmark = [pytest.mark.slice2, pytest.mark.requires_pg]


# ---------------------------------------------------------------------------
# Scenario 1 (slice-2 part): hourly delta → upsert → rollup recompute
# ---------------------------------------------------------------------------


class TestIngestPersistsAndRollsUp:
    def test_metrics_payload_lands_in_raw_samples(self, pg, api):
        res = api.post_valid_metrics()
        assert res["outcome"] == "stored"
        rows = pg.rows("SELECT metric_id, qty FROM raw_samples ORDER BY ts")
        assert len(rows) == 4

    def test_latest_wins_no_double_counts(self, pg, api):
        api.post_valid_metrics()
        # same (metric, source, ts) with corrected qty
        api.post_valid_metrics(qty_override=999)
        rows = pg.rows("SELECT qty FROM raw_samples ORDER BY ts")
        assert len(rows) == 4, "overlapping re-send must update, not append"
        assert 999 in [r["qty"] for r in rows]

    def test_rollups_recomputed_per_kst_day(self, pg, api):
        api.post_valid_metrics()
        rolls = pg.rows(
            "SELECT sum_qty, sample_count FROM daily_rollups "
            "JOIN metric_dim USING (metric_id) "
            "WHERE metric_dim.name = 'step_count'"
        )
        assert rolls and rolls[0]["sample_count"] == 2
        assert rolls[0]["sum_qty"] == pytest.approx(8500 + 120)

    def test_kst_midnight_boundary(self, pg, api):
        """23:59 KST and 00:01 KST next day must land in different days."""
        api.post_metrics([("2026-09-08 23:59:00 +0900", 5.0), ("2026-09-09 00:01:00 +0900", 7.0)])
        days = pg.rows(
            "SELECT day_kst, count(*) AS n FROM raw_samples "
            "JOIN metric_dim USING (metric_id) "
            "WHERE metric_dim.name = 'step_count' GROUP BY day_kst ORDER BY day_kst"
        )
        assert [d["day_kst"].isoformat() for d in days] == ["2026-09-08", "2026-09-09"]

    def test_non_canonical_unit_skipped(self, pg, api):
        api.post_metric(name="weight_body_mass", units="lb", qty=160.0)
        n = pg.scalar("SELECT count(*) FROM raw_samples")
        assert n == 0, "lb must be skipped (canonical unit is kg)"

    def test_kj_converted_to_kcal(self, pg, api):
        api.post_metric(name="active_energy", units="kJ", qty=418.4)
        qty = pg.scalar("SELECT qty FROM raw_samples")
        assert qty == pytest.approx(100.0, rel=1e-6)

    def test_heart_rate_avg_is_qty(self, pg, api):
        api.post_metric_raw(
            "heart_rate",
            "count/min",
            {"date": "2026-09-08 09:00:00 +0900", "Min": 60, "Avg": 72, "Max": 90},
        )
        qty = pg.scalar("SELECT qty FROM raw_samples")
        assert qty == 72

    def test_blood_pressure_split(self, pg, api):
        api.post_metric_raw(
            "blood_pressure",
            "mmHg",
            {"date": "2026-09-08 09:00:00 +0900", "systolic": 120, "diastolic": 80},
        )
        names = [
            r["name"]
            for r in pg.rows(
                "SELECT metric_dim.name FROM raw_samples "
                "JOIN metric_dim USING (metric_id) ORDER BY name"
            )
        ]
        assert names == ["blood_pressure_diastolic", "blood_pressure_systolic"]

    def test_sleep_asleep_preference(self, pg, api):
        api.post_metric_raw(
            "sleep_analysis",
            "hr",
            {"date": "2026-09-08", "asleep": 7.0, "core": 3.5, "deep": 1.5, "rem": 2.0},
        )
        qty = pg.scalar("SELECT qty FROM raw_samples")
        assert qty == 7.0, "asleep wins when present (AutoSleep rule)"

    def test_unknown_metric_autoregisters(self, pg, api):
        api.post_metric(name="brand_new_metric", units="count", qty=1)
        row = pg.row("SELECT name, canonical_unit FROM metric_dim WHERE name = 'brand_new_metric'")
        assert row is not None

    def test_workout_with_hr_and_route(self, pg, api):
        api.post_valid_workout()
        w = pg.row("SELECT name, active_energy_kcal FROM workouts")
        assert w["name"] == "Running"
        assert w["active_energy_kcal"] == pytest.approx(1420.8 / 4.184, rel=1e-6)
        assert pg.scalar("SELECT count(*) FROM workout_heart_rate") == 1
        assert pg.scalar("SELECT count(*) FROM routes") == 1

    def test_raw_ingest_records_headers(self, pg, api):
        api.post_valid_metrics(automation_name="metrics-all")
        row = pg.row("SELECT automation_name, data_type, outcome FROM raw_ingest")
        assert row["automation_name"] == "metrics-all"
        assert row["data_type"] == "metrics"
        assert row["outcome"] == "stored"

    def test_monthly_partition_created(self, pg, api):
        api.post_valid_metrics()
        n = pg.scalar("SELECT count(*) FROM pg_tables WHERE tablename LIKE 'raw_samples_2026_%'")
        assert n >= 1, "ingest must ensure current-month partition exists"


# ---------------------------------------------------------------------------
# Scenario 5: DB outage resilience — 503, archived, replayable
# ---------------------------------------------------------------------------


class TestDbOutageSafety:
    def test_db_down_returns_503_payload_archived_pending(self, api, archive):
        res = api.post_valid_metrics(db_down=True)
        assert res.status_code == 503
        assert res.headers["retry-after"]
        assert archive.pending_count() == 1

    def test_replay_pending_after_db_returns(self, api, pg, archive):
        api.post_valid_metrics(db_down=True)
        api.replay_pending()
        assert pg.scalar("SELECT count(*) FROM raw_samples") == 4
        assert archive.pending_count() == 0


# ---------------------------------------------------------------------------
# Scenario 4: MCP agent session (tools + resources + auth)
# ---------------------------------------------------------------------------


class TestMcpSurface:
    def test_mcp_requires_bearer(self, mcp):
        with pytest.raises(Exception):
            mcp.call_tool_no_auth("list_metrics")

    def test_list_metrics_catalog(self, mcp):
        out = mcp.call_tool("list_metrics")
        names = [m["name"] for m in out["metrics"]]
        assert "step_count" in names
        entry = next(m for m in out["metrics"] if m["name"] == "step_count")
        assert entry["unit"] == "count"
        assert entry["is_cumulative"] is True
        assert entry["last_day"]

    def test_resource_metrics_catalog(self, mcp):
        res = mcp.read_resource("health://metrics")
        assert "step_count" in res

    def test_resource_freshness(self, mcp):
        res = mcp.read_resource("health://freshness")
        assert "latest_complete_day" in res

    def test_resource_schema(self, mcp):
        res = mcp.read_resource("health://schema")
        assert "raw_samples" in res

    def test_query_day_and_series(self, mcp):
        day = mcp.call_tool("query_day", date="2026-09-08", metric="step_count")
        assert day["sum"] == 8500 + 120
        series = mcp.call_tool(
            "get_metric_series", metric="step_count", **{"from": "2026-09-01", "to": "2026-09-08"}
        )
        assert series["points"] >= 1

    def test_raw_samples_paginated(self, mcp):
        page = mcp.call_tool("raw_samples", metric="step_count", limit=1)
        assert len(page["rows"]) == 1
        assert page["next_offset"] == 1

    def test_workout_detail_with_hr_and_route(self, mcp):
        detail = mcp.call_tool("get_workout_detail", id="CC8B17E2-9B4A-4C1D-8E2F-0A1B2C3D4E5F")
        assert detail["hr"]
        assert detail["route"]

    def test_trends_and_freshness(self, mcp):
        trends = mcp.call_tool("get_trends", metric="step_count", window=7)
        assert trends["days"]
        fresh = mcp.call_tool("freshness_check")
        assert fresh["latest_complete_day"]

    def test_unknown_metric_error_is_actionable(self, mcp):
        err = mcp.call_tool_expect_error(
            "get_metric_series", metric="nope_metric", **{"from": "2026-09-01", "to": "2026-09-08"}
        )
        assert "list_metrics" in err or "health://metrics" in err


# ---------------------------------------------------------------------------
# Scenario 3: backfill idempotency via replay
# ---------------------------------------------------------------------------


class TestBackfillReplayIdempotent:
    def test_replay_dir_twice_no_double_counts(self, api, pg, archive):
        archive.write_payload("2026", make_replay_file())
        api.replay_dir(archive.root)
        before = pg.scalar("SELECT count(*) FROM raw_samples")
        api.replay_dir(archive.root)
        after = pg.scalar("SELECT count(*) FROM raw_samples")
        assert before == after, "re-running a chunk must change nothing"


def make_replay_file() -> dict:
    return {
        "data": {
            "metrics": [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [
                        {"qty": 100, "date": "2026-01-01 10:00:00 +0900", "source": "Apple Watch"}
                    ],
                },
            ],
            "workouts": [],
        }
    }
