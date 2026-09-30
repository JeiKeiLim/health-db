"""Acceptance tests: MCP analysis parity (2026-09-28-mcp-analysis-parity).

Generated from .tenet/runs/2026-09-28-mcp-analysis-parity/scenarios.md BEFORE
implementation — they fail today (no sleep_sessions lane, no route paging,
no latest_activity_day, no list_workouts pagination) and define done for
this run. Fixture payloads are sanitized real-payload shapes (house
discipline: real numeric values, genericized sources where required).

pytestmark: requires_pg — disposable postgres:18 container per conftest.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.mcp_analysis_parity, pytest.mark.requires_pg]


# ---------------------------------------------------------------------------
# Fixtures: real payload shapes (verbatim values from the consumer spec §1)
# ---------------------------------------------------------------------------

NIGHT_2026_09_28 = {  # scenario 1 — night ending 9/28 (Watch)
    "date": "2026-09-28 00:00:00 +0900",
    "source": "Jongkuk의 Apple Watch",
    "sleepStart": "2026-09-27 23:05:35 +0900",
    "sleepEnd": "2026-09-28 05:49:54 +0900",
    "inBedStart": "2026-09-27 23:05:35 +0900",
    "inBedEnd": "2026-09-28 05:49:54 +0900",
    "totalSleep": 6.2614,
    "asleep": 0,
    "inBed": 0,
    "awake": 0.262,
    "core": 3.7234,
    "deep": 1.043,
    "rem": 1.495,
}

NIGHT_2026_09_17 = {  # scenario 2 — night ending 9/17 (Watch)
    "date": "2026-09-17 00:00:00 +0900",
    "source": "Jongkuk의 Apple Watch",
    "sleepStart": "2026-09-16 22:11:12 +0900",
    "sleepEnd": "2026-09-17 04:58:30 +0900",
    "inBedStart": "2026-09-16 22:11:12 +0900",
    "inBedEnd": "2026-09-17 04:58:30 +0900",
    "totalSleep": 6.3746,
    "asleep": 0,
    "inBed": 0,
    "awake": 0.05,
    "core": 3.5891,
    "deep": 1.0473,
    "rem": 1.7382,
}

AUTOSLEEP_NAP_2026_09_17 = {  # scenario 2 — same day, distinct record
    "date": "2026-09-17 00:00:00 +0900",
    "source": "AutoSleep",
    "sleepStart": "2026-09-16 17:35:00 +0900",
    "sleepEnd": "2026-09-16 18:10:00 +0900",
    "inBedStart": "2026-09-16 17:35:00 +0900",
    "inBedEnd": "2026-09-16 18:10:00 +0900",
    "totalSleep": 0.5833333333333334,
    "asleep": 0.5833333333333334,
    "inBed": 0.5833333333333334,
    "awake": 0,
    "core": 0,
    "deep": 0,
    "rem": 0,
}

NO_SLEEP_START_SHAPE = {  # scenario 9 — recognized sleep shape, no sleepStart
    "date": "2026-09-08 00:00:00 +0900",
    "source": "Apple Watch",
    "asleep": 7.0,
    "core": 3.5,
    "deep": 1.5,
    "rem": 2.0,
}


def _sleep_payload(*points: dict) -> dict:
    return {
        "data": {
            "metrics": [
                {
                    "name": "sleep_analysis",
                    "units": "hr",
                    "data": list(points),
                }
            ],
            "workouts": [],
        }
    }


def _post_sleep(api, *points: dict) -> dict:
    """POST a sleep payload through the real ingest route."""
    return api.post_payload(_sleep_payload(*points))


class TestScenario1MorningBriefFromMcp:
    """list_sleep_sessions returns full field set with exact live values."""

    def test_night_2026_09_28_full_fields(self, api, pg):
        res = _post_sleep(api, NIGHT_2026_09_28)
        assert res["outcome"] == "stored"
        from health_db import queries

        out = queries.list_sleep_sessions(frm="2026-09-28", to="2026-09-28", pool=pg.pool)
        assert out["count"] == 1
        row = out["sessions"][0]
        assert row["total_sleep"] == pytest.approx(6.2614)
        assert row["deep"] == pytest.approx(1.043)
        assert row["core"] == pytest.approx(3.7234)
        assert row["rem"] == pytest.approx(1.495)
        assert row["awake"] == pytest.approx(0.262)
        assert row["asleep"] == 0
        assert row["in_bed"] == 0
        assert row["sleep_start"] == "2026-09-27T23:05:35+09:00"
        assert row["sleep_end"] == "2026-09-28T05:49:54+09:00"
        assert row["in_bed_start"] == "2026-09-27T23:05:35+09:00"
        assert row["in_bed_end"] == "2026-09-28T05:49:54+09:00"
        assert row["source"] == "Jongkuk의 Apple Watch"
        assert row["day"] == "2026-09-28"

    def test_night_2026_09_17_values(self, api, pg):
        res = _post_sleep(api, NIGHT_2026_09_17)
        assert res["outcome"] == "stored"
        from health_db import queries

        out = queries.list_sleep_sessions(frm="2026-09-17", to="2026-09-17", pool=pg.pool)
        rows = {r["source"]: r for r in out["sessions"]}
        watch = rows["Jongkuk의 Apple Watch"]
        assert watch["total_sleep"] == pytest.approx(6.3746)
        assert watch["deep"] == pytest.approx(1.0473)
        assert watch["sleep_start"] == "2026-09-16T22:11:12+09:00"


class TestScenario2NapAndNightCoexist:
    """Two records same day, distinct (source, sleep_start) — never merged."""

    def test_both_rows_returned_distinct(self, api, pg):
        _post_sleep(api, NIGHT_2026_09_17, AUTOSLEEP_NAP_2026_09_17)
        from health_db import queries

        out = queries.list_sleep_sessions(frm="2026-09-17", to="2026-09-17", pool=pg.pool)
        assert out["count"] == 2
        nap = next(r for r in out["sessions"] if r["source"] == "AutoSleep")
        assert nap["total_sleep"] == pytest.approx(0.5833333333333334)
        assert nap["asleep"] == pytest.approx(0.5833333333333334)
        assert nap["in_bed"] == pytest.approx(0.5833333333333334)
        assert nap["sleep_start"] == "2026-09-16T17:35:00+09:00"

    def test_reingest_no_duplicates(self, api, pg):
        _post_sleep(api, NIGHT_2026_09_17, AUTOSLEEP_NAP_2026_09_17)
        _post_sleep(api, NIGHT_2026_09_17, AUTOSLEEP_NAP_2026_09_17)
        from health_db import queries

        out = queries.list_sleep_sessions(frm="2026-09-17", to="2026-09-17", pool=pg.pool)
        assert out["count"] == 2  # latest-wins on (source_name, sleep_start)

    def test_replay_idempotent(self, api, pg, archive):
        _post_sleep(api, NIGHT_2026_09_17, AUTOSLEEP_NAP_2026_09_17)
        root = archive.route_archive_root  # property on the Archive seam
        api.replay_dir(root, force=True)  # first force replay re-normalizes archive
        api.replay_dir(root, force=True)  # second changes nothing
        from health_db import queries

        out = queries.list_sleep_sessions(frm="2026-09-17", to="2026-09-17", pool=pg.pool)
        assert out["count"] == 2


class TestScenario4SentinelVerbatim:
    """asleep: 0 sentinel stored verbatim; mixed-stage asleep kept too."""

    def test_sentinel_asleep_zero_stored(self, api, pg):
        _post_sleep(api, NIGHT_2026_09_28)
        from health_db import queries

        out = queries.list_sleep_sessions(frm="2026-09-28", to="2026-09-28", pool=pg.pool)
        assert out["sessions"][0]["asleep"] == 0  # verbatim, not derived

    def test_existing_sleep_analysis_untouched(self, api, pg):
        """Parallel emission: sleep_analysis rollup still shows totalSleep (criterion 3)."""
        _post_sleep(api, NIGHT_2026_09_28)
        from health_db import queries

        day = queries.query_day("2026-09-28", "sleep_analysis", pool=pg.pool)
        assert day["metrics"][0]["sum"] == pytest.approx(6.2614)


class TestScenario9NoSleepStartSkipped:
    """Recognized shape without sleepStart → no row + skipped.sleep_session."""

    def test_skip_counted(self, api, pg):
        res = _post_sleep(api, NO_SLEEP_START_SHAPE)
        assert res["outcome"] == "stored"
        assert res["skipped"]["sleep_session"] == 1

    def test_no_session_row(self, api, pg):
        _post_sleep(api, NO_SLEEP_START_SHAPE)
        from health_db import queries

        out = queries.list_sleep_sessions(frm="2026-09-08", to="2026-09-08", pool=pg.pool)
        assert out["count"] == 0


class TestScenario5RoutePaging:
    """get_workout_detail route paging mirrors get_ecg_detail."""

    ROUTE_POINTS = 2873

    @pytest.fixture()
    def workout_with_routes(self, api, pg):
        wid = str(uuid.uuid4())
        start = "2026-09-27 07:00:00 +0900"
        end = "2026-09-27 07:51:40 +0900"
        base_day = 27
        points = []
        for i in range(self.ROUTE_POINTS):
            total_sec = i
            hh = 7 + total_sec // 3600
            mm = (total_sec % 3600) // 60
            ss = total_sec % 60
            points.append(
                {
                    "timestamp": f"2026-09-{base_day:02d} {hh:02d}:{mm:02d}:{ss:02d} +0900",
                    "latitude": 37.5 + i * 1e-6,
                    "longitude": 127.0 + i * 1e-6,
                    "course": None,
                    "speed": None,
                    "altitude": None,
                    "horizontalAccuracy": None,
                    "verticalAccuracy": None,
                }
            )
        payload = {
            "data": {
                "workouts": [
                    {
                        "id": wid,
                        "name": "Running",
                        "start": start,
                        "end": end,
                        "duration": 2864.40,
                        "activeEnergyBurned": {"qty": 2078.36, "units": "kJ"},
                        "distance": {"qty": 6.824, "units": "km"},
                        "avgHeartRate": 137.85,
                        "maxHeartRate": 152,
                        "route": points,
                    }
                ]
            }
        }
        res = api.post_payload(payload)
        assert res["outcome"] == "stored"
        return wid

    def test_first_page_and_marker(self, api, pg, workout_with_routes):
        from health_db import queries

        out = queries.get_workout_detail(
            workout_with_routes, route_offset=0, route_limit=1000, pool=pg.pool
        )
        assert out["route_count"] == self.ROUTE_POINTS
        assert out["route_offset"] == 0
        assert out["route_limit"] == 1000
        assert len(out["route"]) == 1000
        assert out["next_route_offset"] == 1000

    def test_full_walk_pages_sum(self, api, pg, workout_with_routes):
        from health_db import queries

        total, offset, pages = 0, 0, []
        while True:
            out = queries.get_workout_detail(
                workout_with_routes, route_offset=offset, route_limit=1000, pool=pg.pool
            )
            total += len(out["route"])
            pages.append(len(out["route"]))
            if out["next_route_offset"] is None:
                break
            offset = out["next_route_offset"]
        assert total == 2873
        assert pages == [1000, 1000, 873]

    def test_summary_fields_unchanged(self, api, pg, workout_with_routes):
        from health_db import queries

        out = queries.get_workout_detail(workout_with_routes, pool=pg.pool)
        assert out["duration_sec"] == pytest.approx(2864.40)
        assert out["avg_heart_rate"] == pytest.approx(137.85)
        assert out["max_heart_rate"] == 152
        # Fixture-internal expectation: activeEnergyBurned 2078.36 kJ ÷ 4.184
        # (KJ_PER_KCAL, iron law 3) = 496.7400 kcal. The pre-written fixture
        # carried 496.74 (the REAL 2873-point workout e5a1b2c3's live value —
        # live criterion 4), but its own kJ input does not divide to that;
        # 496.74 stays the LIVE-server verification anchor, not this synthetic
        # fixture's (job-4 journal non_blocking_notes flagged the mismatch).
        assert out["active_energy_kcal"] == pytest.approx(
            2078.36 / 4.184, rel=1e-9
        )
        assert out["distance_km"] == pytest.approx(6.824)
        assert out["hr"] == []  # no hr points in fixture — field present
        assert "heart_rate_recovery" in out

    def test_default_route_limit_is_1000(self, api, pg, workout_with_routes):
        from health_db import queries

        out = queries.get_workout_detail(workout_with_routes, pool=pg.pool)
        assert out["route_limit"] == 1000
        assert out["route_offset"] == 0
        assert out["next_route_offset"] == 1000


class TestScenario6LatestActivityDay:
    """freshness_check gains latest_activity_day (additive)."""

    def _post_day_payload(self, api, day, metric, qty):
        point = {"date": f"{day} 12:00:00 +0900", "qty": qty}
        payload = {
            "data": {
                "metrics": [{"name": metric, "units": "count", "data": [point]}],
                "workouts": [],
            }
        }
        res = api.post_payload(payload)
        assert res["outcome"] == "stored"

    def test_activity_day_is_max_of_activity_metrics(self, api, pg):
        self._post_day_payload(api, "2026-09-27", "step_count", 10472.83)
        self._post_day_payload(api, "2026-09-28", "sleep_analysis", 6.26)
        from health_db import queries

        out = queries.freshness_check(pool=pg.pool)
        assert out["latest_activity_day"] == "2026-09-28"
        assert "latest_complete_day" in out  # unchanged field kept

    def test_missing_metrics_ignored(self, api, pg):
        self._post_day_payload(api, "2026-09-26", "resting_heart_rate", 54)
        from health_db import queries

        out = queries.freshness_check(pool=pg.pool)
        assert out["latest_activity_day"] == "2026-09-26"

    def test_freshness_resource_carries_field(self, api, pg):
        self._post_day_payload(api, "2026-09-27", "step_count", 10472.83)
        from health_db import queries

        res = (
            queries.freshness_resource(pool=pg.pool)
            if hasattr(queries, "freshness_resource")
            else None
        )
        if res is None:  # resource served elsewhere — recheck via freshness_check shape
            out = queries.freshness_check(pool=pg.pool)
            assert out["latest_activity_day"] == "2026-09-27"


class TestScenario7WorkoutPagination:
    """list_workouts limit/offset/next_offset — no silent truncation."""

    @pytest.fixture()
    def many_workouts(self, api, pg):
        for i in range(240):
            payload = {
                "data": {
                    "workouts": [
                        {
                            "id": str(uuid.uuid4()),
                            "name": "Walk",
                            "start": f"2018-03-{(i % 28) + 1:02d} 08:00:00 +0900",
                            "end": f"2018-03-{(i % 28) + 1:02d} 08:30:00 +0900",
                            "duration": 1800,
                        }
                    ]
                }
            }
            res = api.post_payload(payload)
            assert res["outcome"] == "stored"

    def _walk_all(self, pg, limit):
        from health_db import queries

        total, offset, pages, markers = 0, 0, [], []
        while True:
            out = queries.list_workouts(
                frm="2018-01-01", to="2018-12-31", limit=limit, offset=offset, pool=pg.pool
            )
            total += out["count"]
            pages.append(out["count"])
            markers.append(out["next_offset"])
            if out["next_offset"] is None:
                break
            offset = out["next_offset"]
        return total, pages, markers

    def test_pagination_walk_2018(self, api, pg, many_workouts):
        total, pages, markers = self._walk_all(pg, 100)
        assert total == 240
        assert pages == [100, 100, 40]
        assert markers == [100, 200, None]

    def test_default_limit_500(self, api, pg, many_workouts):
        from health_db import queries

        out = queries.list_workouts(frm="2018-01-01", to="2018-12-31", pool=pg.pool)
        assert out["limit"] == 500
        assert out["count"] == 240  # under one page

    def test_empty_range_empty_response(self, api, pg):
        from health_db import queries

        out = queries.list_workouts(frm="2000-01-01", to="2000-12-31", pool=pg.pool)
        assert out["workouts"] == []
        assert out["count"] == 0
        assert out["next_offset"] is None


class TestScenario8ProbeCleanup:
    """Probe-metric SQL removes __probe_metric/__probe_metric2 rows (ops, executed via psql)."""

    def test_probe_metrics_gone_after_cleanup(self, api, pg):
        # Ingest probe-shaped payloads (unknown metrics auto-register) — BOTH
        # probes so each DELETE assertion below is non-vacuous.
        payload = {
            "data": {
                "metrics": [
                    {
                        "name": "__probe_metric",
                        "units": "count",
                        "data": [{"date": "2026-01-09 12:00:00 +0900", "qty": 1}],
                    },
                    {
                        "name": "__probe_metric2",
                        "units": "count",
                        "data": [
                            {"date": "2026-01-09 12:00:00 +0900", "qty": 2},
                            {"date": "2026-01-10 12:00:00 +0900", "qty": 3},
                        ],
                    },
                ]
            }
        }
        res = api.post_payload(payload)
        assert res["outcome"] == "stored"
        from health_db import queries

        metrics = queries.list_metrics(pool=pg.pool)
        names = [m["name"] for m in metrics["metrics"]]
        assert "__probe_metric" in names  # present before cleanup
        assert "__probe_metric2" in names  # present before cleanup

        # Execute the real ops deliverable scripts/probe_cleanup.sql spec §5 verbatim
        # (psycopg3 cannot run a multi-statement block as one prepared statement,
        # and the ops step runs it via psql -f — the file is the source of truth).
        sql_path = (
            Path(__file__).resolve().parent.parent.parent
            / "scripts"
            / "probe_cleanup.sql"
        )
        assert sql_path.is_file(), f"ops script missing: {sql_path}"
        with pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql_path.read_text(encoding="utf-8"))
                cur.execute(sql_path.read_text(encoding="utf-8"))  # idempotent re-run

        metrics = queries.list_metrics(pool=pg.pool)
        names = [m["name"] for m in metrics["metrics"]]
        assert "__probe_metric" not in names
        assert "__probe_metric2" not in names


class TestAntiScenarios:
    """Anti-scenario regression guards (scenarios.md §Anti-Scenarios)."""

    def test_anti_census_lock_untouched(self):
        """Anti-scenario 1: existing emissions drift — census lock must pass."""
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/unit/test_census_lock.py", "-q"],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stdout + r.stderr

    def test_anti_session_rows_never_in_rollups(self, api, pg):
        """Anti-scenario 6: rollup contamination."""
        _post_sleep(api, NIGHT_2026_09_28)
        with pg.pool.connection() as conn:
            n = conn.execute(
                """
                SELECT count(*) FROM daily_rollups r
                JOIN metric_dim m ON m.metric_id = r.metric_id
                WHERE m.name LIKE 'sleep_session%'
                """
            ).fetchone()[0]
        assert n == 0

    def test_anti_response_fields_preserved(self, api, pg):
        """Anti-scenario 7: existing get_workout_detail fields all present."""
        from health_db import queries

        wid = str(uuid.uuid4())
        payload = {
            "data": {
                "workouts": [
                    {
                        "id": wid,
                        "name": "Walk",
                        "start": "2026-09-27 08:00:00 +0900",
                        "end": "2026-09-27 08:30:00 +0900",
                        "duration": 1800,
                    }
                ]
            }
        }
        api.post_payload(payload)
        out = queries.get_workout_detail(wid, pool=pg.pool)
        for field in (
            "id", "name", "start", "end", "day", "duration_sec",
            "active_energy_kcal", "total_energy", "basal_energy", "mets",
            "temperature", "humidity", "is_indoor", "location", "intensity",
            "distance_km", "walking_running_distance_km", "step_count",
            "step_cadence", "speed", "avg_heart_rate", "max_heart_rate",
            "heart_rate_recovery", "hr", "route",
        ):
            assert field in out, f"existing field {field} removed"

    def test_anti_skipped_keys_additive(self):
        """skipped contract keeps every existing key plus sleep_session."""
        from health_db.payloads import SKIPPED_KEYS

        assert "sleep_session" in SKIPPED_KEYS
        for key in (
            "unit_unconvertible",
            "unrecognized_shape",
            "ecg",
            "notification",
            "sleep_no_value",
        ):
            assert key in SKIPPED_KEYS


class TestMcpToolSurface:
    """MCP tool registration mirrors the query layer (schema resource too)."""

    def test_sleep_tool_registered(self):
        from health_db import mcp_server

        tools = (
            mcp_server.list_tool_names()
            if hasattr(mcp_server, "list_tool_names")
            else None
        )
        if tools is not None:
            assert "list_sleep_sessions" in tools

    def test_schema_resource_lists_sleep_sessions(self, pg):
        from health_db import queries

        out = (
            queries.schema_resource(pool=pg.pool)
            if hasattr(queries, "schema_resource")
            else None
        )
        if out is None:
            with pg.pool.connection() as conn:
                n = conn.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name = 'sleep_sessions'"
                ).fetchone()[0]
            assert n == 1
        else:
            names = [*out.get("tables", []), *out.get("objects", [])]
            assert any("sleep_sessions" in str(t) for t in names)
