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

import json

import pytest

pytestmark = [pytest.mark.slice2, pytest.mark.requires_pg]


# ---------------------------------------------------------------------------
# Per-test isolation on the shared container (same contract as slice_2_schema)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_data(pg):
    """Empty all data tables + drop partitions before every test.

    Runs BEFORE the api fixture (module-scope migrations already applied
    by the container setup); each test starts from zero rows regardless
    of file execution order.
    """
    from health_db.db.migrations import apply_all

    with pg.pool.connection() as conn:
        apply_all(conn)
        conn.execute(
            """
            TRUNCATE metric_dim, source_dim, raw_samples, daily_rollups, workouts,
                     workout_heart_rate, routes, raw_ingest, auth_log
            """
        )
    for row in pg.rows(
        """
        SELECT c.relname AS name
        FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = 'raw_samples'::regclass
        """
    ):
        pg.execute(f'DROP TABLE IF EXISTS "{row["name"]}"')


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
            {
                "date": "2026-09-08",
                "totalSleep": 7.5,
                "asleep": 7.0,
                "core": 3.5,
                "deep": 1.5,
                "rem": 2.0,
            },
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
        n = pg.scalar(
            """
            SELECT count(*) FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = 'raw_samples' AND c.relname LIKE 'raw_samples_p2026_%%'
            """
        )
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
        drained = api.replay_pending()
        assert drained == 1, "the outage-queued payload must be replayed exactly once"
        assert pg.scalar("SELECT count(*) FROM raw_samples") == 4
        assert archive.pending_count() == 0, "queue drains to zero after DB returns"
        row = pg.row("SELECT outcome FROM raw_ingest")
        assert row is not None and row["outcome"] == "replayed"

    def test_replay_pending_twice_second_pass_drains_nothing(self, api, pg, archive):
        api.post_valid_metrics(db_down=True)
        assert api.replay_pending() == 1
        # The queue is now empty; a second recovery pass is a no-op.
        assert api.replay_pending() == 0
        assert pg.scalar("SELECT count(*) FROM raw_samples") == 4
        assert archive.pending_count() == 0

    def test_replay_after_outage_recomputes_rollups(self, api, pg, archive):
        """The recovered payload must be fully processed, not just upserted:
        daily_rollups for its KST day are recomputed by the pipeline."""
        api.post_valid_metrics(db_down=True)
        api.replay_pending()
        rolls = pg.rows(
            "SELECT sum_qty, sample_count FROM daily_rollups "
            "JOIN metric_dim USING (metric_id) "
            "WHERE metric_dim.name = 'step_count'"
        )
        assert rolls and rolls[0]["sample_count"] == 2
        assert rolls[0]["sum_qty"] == pytest.approx(8500 + 120)


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
        before_rows = pg.rows("SELECT metric_id, source_id, ts, qty FROM raw_samples ORDER BY ts")
        assert len(before_rows) == 1, "first replay must land the payload"
        api.replay_dir(archive.root)
        after_rows = pg.rows("SELECT metric_id, source_id, ts, qty FROM raw_samples ORDER BY ts")
        assert after_rows == before_rows, "re-running a chunk must change nothing"

    def test_replay_dir_records_replayed_outcome_and_dedups_by_sha(self, api, pg, archive):
        archive.write_payload("2026", make_replay_file())
        api.replay_dir(archive.root)
        api.replay_dir(archive.root)
        rows = pg.rows("SELECT outcome FROM raw_ingest")
        assert len(rows) == 1, "sha256 dedup via raw_ingest: one row, never two applies"
        assert rows[0]["outcome"] == "replayed"
        assert pg.scalar("SELECT count(*) FROM daily_rollups") == 1

    def test_replay_dir_applies_payload_content(self, api, pg, archive):
        """Replay is not a black box: the archived payload's sample values
        (step_count 100 on 2026-01-01) must actually land."""
        archive.write_payload("2026", make_replay_file())
        api.replay_dir(archive.root)
        qty = pg.scalar(
            "SELECT qty FROM raw_samples JOIN metric_dim USING (metric_id) "
            "WHERE metric_dim.name = 'step_count'"
        )
        roll = pg.scalar("SELECT sum_qty FROM daily_rollups")
        assert qty == pytest.approx(100.0)
        assert roll == pytest.approx(100.0)

    def test_replay_dir_skips_quarantine_subtree(self, api, pg, archive):
        """Quarantined payloads were never accepted and must never be applied."""
        quarantine_root = archive.root / "quarantine"
        quarantine_root.mkdir(parents=True)
        (quarantine_root / "bad.json").write_bytes(json.dumps(make_replay_file()).encode("utf-8"))
        api.replay_dir(archive.root)
        assert pg.scalar("SELECT count(*) FROM raw_samples") == 0
        assert pg.scalar("SELECT count(*) FROM raw_ingest") == 0

    def test_replay_dir_survives_malformed_payload_in_tree(self, api, pg, archive):
        """One bad file must not abort a backfill: good payloads still land,
        the bad file is left untouched on disk."""
        archive.write_payload("2026", make_replay_file())
        (archive.root / "2026" / "broken.json").write_bytes(b"{broken")
        processed = api.replay_dir(archive.root)
        assert processed == 2, "both files are seen; only the valid one applies"
        assert pg.scalar("SELECT count(*) FROM raw_samples") == 1
        assert (archive.root / "2026" / "broken.json").read_bytes() == b"{broken"


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


# ---------------------------------------------------------------------------
# Real HAE v2 workout shapes — the regression guard for the original bug.
#
# The pre-fix normalizer stored every scalar workout field as NULL and zero
# HR rows because it only understood the simplified fixture shapes; this test
# POSTs the sanitized real-shape fixture through the REAL ingest route and
# asserts the full observable outcome via DB queries.
# ---------------------------------------------------------------------------


class TestRealWorkoutShapesEndToEnd:
    FIXTURE_ID = "a1b2c3d4-0001-1111-2222-333344445555"
    FIXTURE_STEPS = 170.0 + 171.0 + 172.0
    FIXTURE_WRD = 0.15 + 0.16 + 0.14
    FIXTURE_BASAL_KJ = 5.0 + 6.0 + 7.0

    def test_scalars_fully_populated(self, pg, api):
        api.post_real_workout()
        w = pg.row("SELECT * FROM workouts WHERE id = %s", (self.FIXTURE_ID,))
        from health_db.normalize import KJ_PER_KCAL

        assert w is not None, "real-shape workout row must exist"
        assert w["active_energy_kcal"] == pytest.approx(2100.0 / KJ_PER_KCAL)
        assert w["total_energy"] == pytest.approx(2400.0 / KJ_PER_KCAL)
        assert w["basal_energy"] == pytest.approx(self.FIXTURE_BASAL_KJ / KJ_PER_KCAL)
        assert w["distance_km"] == pytest.approx(6.5)
        assert w["avg_heart_rate"] == pytest.approx(150.0)
        assert w["max_heart_rate"] == 180
        assert w["step_count"] == int(self.FIXTURE_STEPS)
        assert w["walking_running_distance_km"] == pytest.approx(self.FIXTURE_WRD)
        for column in (
            "temperature",
            "humidity",
            "intensity",
            "speed",
            "step_cadence",
        ):
            assert w[column] is not None, f"{column} must be non-null for the real payload"

    def test_all_required_columns_non_null(self, pg, api):
        api.post_real_workout()
        w = pg.row(
            "SELECT active_energy_kcal, total_energy, basal_energy, distance_km, "
            "avg_heart_rate, max_heart_rate, step_count, walking_running_distance_km, "
            "temperature, humidity, intensity, speed, step_cadence "
            "FROM workouts WHERE id = %s",
            (self.FIXTURE_ID,),
        )
        missing = [k for k, v in w.items() if v is None]
        assert missing == [], f"real payload left these NULL: {missing}"

    def test_heart_rate_rows_match_fixture(self, pg, api):
        api.post_real_workout()
        rows = pg.rows(
            "SELECT heart_rate, ts FROM workout_heart_rate "
            "WHERE workout_id = %s ORDER BY ts",
            (self.FIXTURE_ID,),
        )
        assert len(rows) == 5, "real heartRateData has 5 points"
        assert rows[0]["heart_rate"] == 105  # round(105)
        assert rows[1]["heart_rate"] == 114  # round(114)
        assert rows[2]["heart_rate"] == 133  # round(132.75)

    def test_recovery_jsonb_round_trips_as_array(self, pg, api):
        api.post_real_workout()
        recovery = pg.scalar(
            "SELECT heart_rate_recovery FROM workouts WHERE id = %s", (self.FIXTURE_ID,)
        )
        assert isinstance(recovery, list), f"expected JSON array, got {type(recovery)}"
        assert recovery[0]["Avg"] == 165
        assert len(recovery) == 5

    def test_route_rows_match_fixture(self, pg, api):
        api.post_real_workout()
        n = pg.scalar(
            "SELECT count(*) FROM routes WHERE workout_id = %s", (self.FIXTURE_ID,)
        )
        assert n == 5
        first = pg.row(
            "SELECT latitude, longitude FROM routes WHERE workout_id = %s ORDER BY ts LIMIT 1",
            (self.FIXTURE_ID,),
        )
        assert first["latitude"] == pytest.approx(REDACTED4466209698)


class TestHeartRateRecoveryJsonbWrite:
    """Regression: a parsed dict/list must not crash the jsonb workout write.

    psycopg3 3.3.5 cannot adapt a plain dict/list to a ``%s`` jsonb parameter
    (ProgrammingError 'cannot adapt type dict'); the pre-fix pipeline passed
    ``row.heart_rate_recovery`` straight through and the workout transaction
    died as soon as recovery was non-None.
    """

    def test_non_empty_recovery_list_stores(self, pg, api):
        recovery = [
            {"date": "2026-09-08 07:03:00 +0900", "Avg": 170, "Min": 170, "Max": 170},
            {"date": "2026-09-08 07:04:00 +0900", "Avg": 165, "Min": 165, "Max": 165},
        ]
        res = api.post_workout_recovery(recovery)
        assert res["outcome"] == "stored", res
        stored = pg.scalar(
            "SELECT heart_rate_recovery FROM workouts WHERE id = %s",
            ("CC8B17E2-9B4A-4C1D-8E2F-0A1B2C3D4E5F",),
        )
        assert stored == recovery

    def test_legacy_dict_recovery_stores(self, pg, api):
        recovery = {"heartRateRecoveryValue": 25}
        res = api.post_workout_recovery(recovery)
        assert res["outcome"] == "stored", res
        stored = pg.scalar(
            "SELECT heart_rate_recovery FROM workouts WHERE id = %s",
            ("CC8B17E2-9B4A-4C1D-8E2F-0A1B2C3D4E5F",),
        )
        assert stored == recovery


class TestWorkoutRepairPath:
    """Repair path: force-replay archived payloads to fix already-broken rows.

    Requirements: (a) re-running repair is idempotent; (b) raw_samples and
    daily_rollups are unchanged; (c) it works from the archive on disk.
    """

    def _snapshot_counts(self, pg) -> dict:
        return {
            "raw_samples": pg.scalar("SELECT count(*) FROM raw_samples"),
            "daily_rollups": pg.scalar("SELECT count(*) FROM daily_rollups"),
            "raw_ingest": pg.scalar("SELECT count(*) FROM raw_ingest"),
        }

    def test_repair_fixes_nulled_scalars_and_is_idempotent(self, pg, api, archive):
        # 1. Ingest the real fixture (correct values).
        api.post_real_workout()
        good = pg.row(
            "SELECT * FROM workouts WHERE id = %s",
            (TestRealWorkoutShapesEndToEnd.FIXTURE_ID,),
        )
        assert good["active_energy_kcal"] > 0

        # 2. Simulate the historical damage: NULL the scalar columns + drop HR rows.
        pg.execute(
            "UPDATE workouts SET active_energy_kcal = NULL, total_energy = NULL, "
            "basal_energy = NULL, distance_km = NULL, avg_heart_rate = NULL, "
            "max_heart_rate = NULL, step_count = NULL, "
            "walking_running_distance_km = NULL, temperature = NULL, humidity = NULL, "
            "intensity = NULL, speed = NULL, step_cadence = NULL, "
            "heart_rate_recovery = NULL WHERE id = %s",
            (TestRealWorkoutShapesEndToEnd.FIXTURE_ID,),
        )
        pg.execute(
            "DELETE FROM workout_heart_rate WHERE workout_id = %s",
            (TestRealWorkoutShapesEndToEnd.FIXTURE_ID,),
        )
        broken = pg.row("SELECT active_energy_kcal FROM workouts WHERE id = %s",
                        (TestRealWorkoutShapesEndToEnd.FIXTURE_ID,))
        assert broken["active_energy_kcal"] is None
        assert pg.scalar("SELECT count(*) FROM workout_heart_rate") == 0

        # 3. Repair from the archive on disk (sha already in raw_ingest → needs force).
        archive.write_payload("2026", api.real_workout_payload())
        seen = api.repair_dir(archive.root)
        assert seen == 1

        fixed = pg.row("SELECT * FROM workouts WHERE id = %s",
                       (TestRealWorkoutShapesEndToEnd.FIXTURE_ID,))
        assert fixed["active_energy_kcal"] > 0
        assert fixed["total_energy"] > 0
        assert fixed["basal_energy"] > 0
        assert fixed["distance_km"] > 0
        assert fixed["avg_heart_rate"] > 0
        assert fixed["max_heart_rate"] > 0
        assert fixed["step_count"] > 0
        assert fixed["walking_running_distance_km"] > 0
        assert fixed["heart_rate_recovery"] is not None
        assert pg.scalar("SELECT count(*) FROM workout_heart_rate") == 5

        # 4. Idempotency: a second repair changes no counters/values.
        #   (ingested_at is a now() audit stamp that legitimately advances.)
        before = self._snapshot_counts(pg)
        columns = ", ".join(
            c
            for c in pg.row("SELECT * FROM workouts LIMIT 1").keys()
            if c != "ingested_at"
        )
        fixed_before = pg.rows(f"SELECT {columns} FROM workouts ORDER BY id")
        api.repair_dir(archive.root)
        assert self._snapshot_counts(pg) == before, "repair must not change sample/rollup counts"
        assert pg.rows(f"SELECT {columns} FROM workouts ORDER BY id") == fixed_before

    # --- Content-level guards: raw_samples / daily_rollups must be
    #     byte-identical across force-replays, even with real metric rows
    #     present (a count-only check is trivially true when the fixture is
    #     metrics:[]). ---

    @staticmethod
    def _samples_md5(pg) -> str:
        """md5 of ordered raw_samples content (all columns except the
        ``ingested_at`` audit stamp, which legitimately advances on each
        latest-wins repair — same exclusion the workout comparison uses)."""
        return pg.scalar(
            """
            SELECT md5(string_agg(
                metric_id::text || '|' || source_id::text || '|' || ts::text
                || '|' || qty::text || '|' || day_kst::text,
                E'\\n' ORDER BY metric_id, source_id, ts))
            FROM raw_samples
            """
        )

    @staticmethod
    def _rollups_md5(pg) -> str:
        """md5 of ordered daily_rollups content (all columns except the
        ``updated_at`` audit stamp, which advances on each recompute — the
        order-sensitive float8 values themselves are what must be stable)."""
        return pg.scalar(
            """
            SELECT md5(string_agg(
                metric_id::text || '|' || day_kst::text || '|' || sum_qty::text
                || '|' || avg_qty::text || '|' || min_qty::text
                || '|' || max_qty::text || '|' || sample_count::text,
                E'\\n' ORDER BY metric_id, day_kst))
            FROM daily_rollups
            """
        )

    def test_force_repair_leaves_samples_and_rollups_byte_identical(
        self, pg, api, archive
    ):
        """Force-repair is byte-idempotent for raw_samples AND daily_rollups.

        The repair regression ingests a payload with REAL metric samples
        (step_count + heart_rate, real ``{qty, date}`` shape) so
        ``daily_rollups`` is genuinely populated — without that, a
        "rollups unchanged" assertion over a metrics:[] fixture would be
        trivially true. Both tables are snapshotted by row count AND full
        md5 of ordered content, and ``raw_ingest.received_at`` is asserted
        unchanged (repair refreshes payload/outcome in place but must
        preserve the original receive stamp).

        Crucially this runs force-repair TWICE and compares the two rollup
        md5s: the float8 ``sum``/``avg`` aggregates are order-sensitive, so
        a non-deterministic recompute (parallel/hash aggregation over a heap
        order that each repair re-UPDATE perturbs) is caught as a ULP-level
        md5 drift — the exact regression observed on the dev DB.
        """
        payload = api.repair_fixture_payload()
        api.post_repair_fixture()

        # Populated guards (fail loudly if the fixture ever loses its metrics).
        assert pg.scalar("SELECT count(*) FROM raw_samples") == 8 + 3
        assert pg.scalar("SELECT count(*) FROM daily_rollups") == 2

        received_at = pg.scalar("SELECT received_at FROM raw_ingest")

        archive.write_payload("2026", payload)

        def snapshot() -> tuple[int, int, str, str, object]:
            return (
                pg.scalar("SELECT count(*) FROM raw_samples"),
                pg.scalar("SELECT count(*) FROM daily_rollups"),
                self._samples_md5(pg),
                self._rollups_md5(pg),
                pg.scalar("SELECT received_at FROM raw_ingest"),
            )

        before = snapshot()
        assert before[4] == received_at, "received_at must be preserved by repair"

        assert api.repair_dir(archive.root) == 1
        after_first = snapshot()
        assert after_first == before, "force-repair changed sample/rollup content"

        # Second consecutive force-replay: the regression is ULP-level drift
        # between two repairs, so compare the rollup md5 directly.
        api.repair_dir(archive.root)
        after_second = snapshot()
        assert after_second == after_first, (
            "consecutive force-repairs must be byte-identical "
            f"(rollups md5 {after_first[3]} != {after_second[3]})"
        )

    def test_repair_outcome_recorded_and_dedup_preserved(self, pg, api, archive):
        api.post_real_workout()
        archive.write_payload("2026", api.real_workout_payload())
        api.repair_dir(archive.root)
        row = pg.row("SELECT outcome, payload FROM raw_ingest")
        assert row["outcome"] == "repaired", "repair must be visible in the audit outcome"

    def test_plain_replay_does_not_reprocess_existing_sha(self, pg, api, archive):
        """Without --force the sha gate short-circuits (the reason repair exists)."""
        api.post_real_workout()
        pg.execute(
            "UPDATE workouts SET distance_km = NULL WHERE id = %s",
            (TestRealWorkoutShapesEndToEnd.FIXTURE_ID,),
        )
        archive.write_payload("2026", api.real_workout_payload())
        api.replay_dir(archive.root)  # no force
        still_null = pg.scalar(
            "SELECT distance_km FROM workouts WHERE id = %s",
            (TestRealWorkoutShapesEndToEnd.FIXTURE_ID,),
        )
        assert still_null is None, "plain replay must not reprocess an existing sha"
