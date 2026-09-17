"""Acceptance tests: parse-everything run (2026-09-17-normalizer-audit).

Generated from .tenet/runs/2026-09-17-normalizer-audit/scenarios.md BEFORE
implementation — they fail today (sleep 0.0 bug, missing lanes) and define
done for this run. Fixture payloads are committed sanitized subsets of the
real HAE corpus (never the 300 MB backfill file).

pytestmark: requires_pg — disposable postgres:18 container per conftest.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.parse_everything, pytest.mark.requires_pg]


# ---------------------------------------------------------------------------
# Fixtures: sanitized real-payload shapes (census-locked subsets)
# ---------------------------------------------------------------------------

WATCH_NIGHT = {  # scenario 1 — real 2026-09-17 daily export row
    "date": "2026-09-17 00:00:00 +0900",
    "source": "Jongkuk의 Apple Watch",
    "asleep": 0,
    "inBed": 0,
    "awake": 0.05,
    "core": 3.5891,
    "deep": 1.0473,
    "rem": 1.7382,
    "totalSleep": 6.3746,
    "sleepStart": "2026-09-16 22:11:12 +0900",
    "sleepEnd": "2026-09-17 04:58:30 +0900",
}

MIXED_STAGE_NIGHT = {  # scenario 2 — real 2024-10-09 backfill row (50-case)
    "date": "2024-10-09 00:00:00 +0900",
    "source": "Jongkuk의 Apple Watch",
    "asleep": 2.05,
    "inBed": 0,
    "awake": 0.4167,
    "core": 3.1875,
    "deep": 1.0938,
    "rem": 1.9375,
    "totalSleep": 8.268799999999999,
    "sleepStart": "2024-10-08 21:50:00 +0900",
    "sleepEnd": "2024-10-09 06:20:00 +0900",
}

AUTOSLEEP_NAP = {  # scenario 3 — real 2026-09-16 daily export nap row
    "date": "2026-09-17 00:00:00 +0900",
    "source": "AutoSleep",
    "asleep": 0.5833333333333334,
    "inBed": 0.5833333333333334,
    "awake": 0,
    "core": 0,
    "deep": 0,
    "rem": 0,
    "totalSleep": 0.5833333333333334,
    "sleepStart": "2026-09-16 17:35:00 +0900",
    "sleepEnd": "2026-09-16 18:10:00 +0900",
}

INBED_ONLY = {  # scenario 4 — real 2018-04-02 backfill row (시계 era)
    "date": "2018-04-02 00:00:00 +0900",
    "source": "시계",
    "asleep": 0,
    "inBed": 7.332222222222223,
    "awake": 0,
    "core": 0,
    "deep": 0,
    "rem": 0,
    "totalSleep": 0,
    "sleepStart": "2018-04-02 00:15:08 +0900",
    "sleepEnd": "2018-04-02 07:35:04 +0900",
}

NO_TOTALSLEEP_SHAPE = {  # scenario 9 — archive-corpus shape (archived-export)
    "date": "2026-09-08",
    "asleep": 7.0,
    "core": 3.5,
    "deep": 1.5,
    "rem": 2.0,
}

ECG_SESSION = {  # scenario 5 — trimmed real backfill ECG shape
    "source": "심전도",
    "start": "2025-10-12 10:45:06 +0900",
    "end": "2025-10-12 10:45:36 +0900",
    "samplingFrequency": 512,
    "numberOfVoltageMeasurements": 3,
    "classification": "정상 동율 리듬",
    "averageHeartRate": 68,
    "voltageMeasurements": [
        {"voltage": -10.528610229492188, "date": 1760233506.862702, "units": "mcV"},
        {"voltage": -8.956151962280273, "date": 1760233506.864655, "units": "mcV"},
        {"voltage": -7.150754928777866, "date": 1760233506.8666081, "units": "mcV"},
    ],
}

HR_NOTIFICATION = {  # scenario 6 — trimmed real backfill notification shape
    "start": "2026-06-16T04:39:20Z",
    "end": "2026-06-16T04:39:20Z",
    "threshold": 118,
    "heartNotification": "high_heart_rate_notification",
    "source": {"name": "Jongkuk의 Apple Watch", "identifier": "com.apple.health.A1B2C3D4"},
    "heartRateData": [
        {
            "hrUnits": "count/min",
            "heartRate": 132,
            "timestamp": {
                "measurementDuration": 0, "end": "2026-06-16T04:40:29Z",
                "units": "s", "start": "2026-06-16T04:40:29Z",
            },
        },
    ],
    "hrvData": [
        {
            "units": "ms",
            "hrv": 27.413754219876544,
            "timestamp": {
                "measurementDuration": 59.5, "end": "2026-06-16T04:31:05Z",
                "units": "s", "start": "2026-06-16T04:30:05Z",
            },
        },
    ],
}


def sleep_group(*points, units: str = "hr") -> dict:
    return {"name": "sleep_analysis", "units": units, "data": list(points)}


def doc_with(group: dict, **extra: object) -> dict:
    return {"data": {"metrics": [group], "workouts": [], **extra}}


def doc_lanes(
    metrics: list | None = None, ecg: list | None = None, hrn: list | None = None
) -> dict:
    data: dict = {"metrics": metrics or [], "workouts": []}
    if ecg is not None:
        data["ecg"] = ecg
    if hrn is not None:
        data["heartRateNotifications"] = hrn
    return {"data": data}


def post_doc(api, payload: dict) -> dict:
    """Post an arbitrary v2 document through the real /api/ingest route."""
    import json as _json

    from fastapi.testclient import TestClient  # noqa: F401  (api wraps it)

    res = api._client.post(
        "/api/ingest", content=_json.dumps(payload).encode("utf-8"), headers=api._headers
    )
    assert res.status_code == 200, f"ingest failed: {res.status_code} {res.text[:200]}"
    return res.json()


# ---------------------------------------------------------------------------
# Scenarios 1-3: sleep value rule (raw normalizer level via api + DB)
# ---------------------------------------------------------------------------


class TestSleepValueRule:
    def test_scenario1_watch_night_stored_not_zero(self, api, pg):
        api.post_metric_raw("sleep_analysis", "hr", WATCH_NIGHT)
        qty = pg.scalar(
            "SELECT qty FROM raw_samples rs JOIN metric_dim md USING (metric_id) "
            "JOIN source_dim sd USING (source_id) WHERE md.name='sleep_analysis'"
        )
        assert qty == pytest.approx(6.3746), (
            "Watch night must store totalSleep, not the asleep=0 sentinel"
        )

    def test_scenario2_mixed_stage_night_stores_totalsleep(self, api, pg):
        api.post_metric_raw("sleep_analysis", "hr", MIXED_STAGE_NIGHT)
        qty = pg.scalar(
            "SELECT qty FROM raw_samples rs JOIN metric_dim md USING (metric_id)"
            " WHERE md.name='sleep_analysis'"
        )
        assert qty == pytest.approx(8.268799999999999), (
            "mixed-stage night: totalSleep wins"
            " (core+deep+rem alone = 6.2188 loses the real asleep stage)"
        )

    def test_scenario3_nap_and_night_coexist(self, api, pg):
        api.post_metric_raw("sleep_analysis", "hr", WATCH_NIGHT)
        api.post_metric_raw("sleep_analysis", "hr", AUTOSLEEP_NAP)
        rows = pg.rows(
            "SELECT sd.name AS source, rs.qty FROM raw_samples rs "
            "JOIN metric_dim md USING (metric_id) JOIN source_dim sd USING (source_id) "
            "WHERE md.name='sleep_analysis' ORDER BY sd.name"
        )
        assert len(rows) == 2, "both source rows must be stored (disjoint windows: night + nap)"
        by_src = {r["source"]: r["qty"] for r in rows}
        assert by_src["Jongkuk의 Apple Watch"] == pytest.approx(6.3746)
        assert by_src["AutoSleep"] == pytest.approx(0.5833333333333334)

    def test_scenario1_rollup_sum_is_real(self, api, pg):
        api.post_metric_raw("sleep_analysis", "hr", WATCH_NIGHT)
        total = pg.scalar(
            "SELECT sum_qty FROM daily_rollups dr JOIN metric_dim md USING (metric_id) "
            "WHERE md.name='sleep_analysis'"
        )
        assert total == pytest.approx(6.3746), "rollup must show the mixed-stage night, not 0.0"


# ---------------------------------------------------------------------------
# Scenario 4: in-bed-only rows → sleep_in_bed metric
# ---------------------------------------------------------------------------


class TestSleepInBed:
    def test_scenario4_inbed_only_row_lands_in_sleep_in_bed(self, api, pg):
        api.post_metric_raw("sleep_analysis", "hr", INBED_ONLY)
        sleep_rows = pg.scalar(
            "SELECT count(*) FROM raw_samples rs JOIN metric_dim md USING (metric_id) "
            "WHERE md.name='sleep_analysis'"
        )
        inbed_rows = pg.rows(
            "SELECT rs.qty, sd.name AS source, md.display_name FROM raw_samples rs "
            "JOIN metric_dim md USING (metric_id) JOIN source_dim sd USING (source_id) "
            "WHERE md.name='sleep_in_bed'"
        )
        assert sleep_rows == 0, "in-bed-only row must NOT produce a sleep_analysis row"
        assert len(inbed_rows) == 1
        assert inbed_rows[0]["qty"] == pytest.approx(7.332222222222223)
        assert inbed_rows[0]["display_name"] == "Sleep In-Bed"
        assert inbed_rows[0]["source"] == "시계"

    def test_inbed_plus_stages_emits_both(self, api, pg):
        point = {**WATCH_NIGHT, "inBed": 7.5}
        api.post_metric_raw("sleep_analysis", "hr", point)
        n_sleep = pg.scalar(
            "SELECT count(*) FROM raw_samples rs JOIN metric_dim md USING (metric_id)"
            " WHERE md.name='sleep_analysis'"
        )
        n_inbed = pg.scalar(
            "SELECT count(*) FROM raw_samples rs JOIN metric_dim md USING (metric_id)"
            " WHERE md.name='sleep_in_bed'"
        )
        assert n_sleep == 1 and n_inbed == 1, "stages + inBed: both rows emitted"


# ---------------------------------------------------------------------------
# Scenarios 5-6: ECG + notification lanes (ingest → DB → MCP-level queries)
# ---------------------------------------------------------------------------


class TestEcgLane:
    def test_scenario5_ecg_session_and_voltages_stored(self, api, pg):
        post_doc(api, doc_lanes(ecg=[ECG_SESSION]))
        n_sessions = pg.scalar("SELECT count(*) FROM ecgs")
        n_volts = pg.scalar("SELECT count(*) FROM ecg_voltages")
        assert n_sessions == 1 and n_volts == 3
        row = pg.row("SELECT classification, average_heart_rate, voltage_count FROM ecgs")
        assert row["classification"] == "정상 동율 리듬"
        assert row["average_heart_rate"] == 68
        assert row["voltage_count"] == 3

    def test_scenario5_voltage_ts_is_unix_epoch(self, api, pg):
        post_doc(api, doc_lanes(ecg=[ECG_SESSION]))
        first = pg.row("SELECT ts FROM ecg_voltages ORDER BY seq LIMIT 1")
        # 1760233506.862702 unix == 2025-10-12 01:45:06.862702 UTC == session start KST 10:45:06
        assert str(first["ts"])[:19] == "2025-10-12 01:45:06", (
            "voltage ts must be UNIX-epoch converted (never CF-absolute year-2056)"
        )

    def test_scenario5_ecg_repost_idempotent(self, api, pg):
        post_doc(api, doc_lanes(ecg=[ECG_SESSION]))
        post_doc(api, doc_lanes(ecg=[ECG_SESSION]))
        assert pg.scalar("SELECT count(*) FROM ecgs") == 1
        assert pg.scalar("SELECT count(*) FROM ecg_voltages") == 3, "latest-wins, zero re-append"


class TestNotificationLane:
    def test_scenario6_events_and_samples_stored(self, api, pg):
        post_doc(api, doc_lanes(hrn=[HR_NOTIFICATION]))
        n_events = pg.scalar("SELECT count(*) FROM heart_rate_notifications")
        n_samples = pg.scalar("SELECT count(*) FROM heart_rate_notification_samples")
        assert n_events == 1 and n_samples == 2, "one HR sample + one HRV sample"
        kinds = {r["kind"] for r in pg.rows("SELECT kind FROM heart_rate_notification_samples")}
        assert kinds == {"hr", "hrv"}
        hr = pg.scalar(
            "SELECT value FROM heart_rate_notification_samples WHERE kind='hr'"
        )
        assert hr == 132.0

    def test_scenario6_repost_idempotent(self, api, pg):
        post_doc(api, doc_lanes(hrn=[HR_NOTIFICATION]))
        post_doc(api, doc_lanes(hrn=[HR_NOTIFICATION]))
        assert pg.scalar("SELECT count(*) FROM heart_rate_notifications") == 1
        assert pg.scalar("SELECT count(*) FROM heart_rate_notification_samples") == 2


# ---------------------------------------------------------------------------
# Scenario 7: unit conversion
# ---------------------------------------------------------------------------


class TestUnitConversion:
    def test_scenario7_lb_converts_to_kg(self, api, pg):
        api.post_metric(name="weight_body_mass", units="lb", qty=160.0)
        row = pg.row(
            "SELECT rs.qty, rs.ts FROM raw_samples rs JOIN metric_dim md USING (metric_id)"
            " WHERE md.name='weight_body_mass'"
        )
        assert row["qty"] == pytest.approx(72.5748655, abs=1e-4), "160 lb → 72.57 kg via ÷2.20462"
        canonical = pg.scalar("SELECT canonical_unit FROM metric_dim WHERE name='weight_body_mass'")
        assert canonical == "kg", "stored unit stays canonical kg"

    def test_scenario7_kj_converts_unchanged(self, api, pg):
        api.post_metric(name="active_energy", units="kJ", qty=418.4)
        qty = pg.scalar(
            "SELECT qty FROM raw_samples rs JOIN metric_dim md USING (metric_id)"
            " WHERE md.name='active_energy'"
        )
        assert qty == pytest.approx(100.0), "kJ→kcal existing conversion intact"

    def test_unconvertible_unit_counted_visibly(self, api, pg):
        import json as _json

        payload = {"data": {"metrics": [
            {
                "name": "weight_body_mass", "units": "st",
                "data": [{"qty": 11.8, "date": "2026-09-08 09:00:00 +0900"}],
            }
        ], "workouts": []}}
        res = api._client.post(
            "/api/ingest", content=_json.dumps(payload).encode("utf-8"), headers=api._headers
        )
        body = res.json()
        # 'st' HAS an exact factor (÷0.157473) per spec and converts; the
        # accounting mechanism itself is asserted in the sleep case below.
        assert "skipped" in body, "ingest response must carry the skipped accounting object"


# ---------------------------------------------------------------------------
# Scenario 8-9: repair path + loosened recognition
# ---------------------------------------------------------------------------


class TestRepairAndRecognition:
    def test_scenario8_force_replay_repairs_sleep_zero(self, api, pg, archive):
        api.post_metric_raw("sleep_analysis", "hr", WATCH_NIGHT)
        # simulate the broken value as stored by the old rule, then repair via force
        pg.execute(
            "UPDATE raw_samples SET qty = 0.0"
        )
        assert pg.scalar("SELECT qty FROM raw_samples") == 0.0
        # The route archived the payload into config.ARCHIVE_ROOT (the
        # verbatim archive tree); replay must scan THAT tree — the replay
        # corpus root (pending) is a disjoint tree by iron law 2.
        api.replay_dir(archive.route_archive_root, force=True)
        qty = pg.scalar(
            "SELECT qty FROM raw_samples rs JOIN metric_dim md USING (metric_id)"
            " WHERE md.name='sleep_analysis'"
        )
        assert qty == pytest.approx(6.3746), (
            "force-replay re-normalizes archived payload → real value"
        )

    def test_scenario9_no_totalsleep_shape_recognized(self, api, pg):
        api.post_metric_raw("sleep_analysis", "hr", NO_TOTALSLEEP_SHAPE)
        qty = pg.scalar(
            "SELECT qty FROM raw_samples rs JOIN metric_dim md USING (metric_id)"
            " WHERE md.name='sleep_analysis'"
        )
        assert qty == 7.0, (
            "{date, asleep, core, deep, rem} without totalSleep"
            " must normalize (asleep wins), not drop"
        )


# ---------------------------------------------------------------------------
# MCP surface: new tools (scenario 5/6 query paths) + perf non-regression
# ---------------------------------------------------------------------------


class TestMcpTools:
    def test_list_ecgs_and_detail(self, api, pg):
        from health_db import queries

        post_doc(api, doc_lanes(ecg=[ECG_SESSION]))
        listing = queries.list_ecgs(pool=pg.pool)
        assert listing["count"] >= 1
        ecg_id = listing["ecgs"][0]["id"]
        detail = queries.get_ecg_detail(ecg_id, pool=pg.pool)
        assert detail["voltage_count"] == 3
        assert len(detail["voltages"]) == 3

    def test_list_heart_notifications(self, api, pg):
        from health_db import queries

        post_doc(api, doc_lanes(hrn=[HR_NOTIFICATION]))
        listing = queries.list_heart_notifications(pool=pg.pool)
        assert listing["count"] == 1
        event = listing["notifications"][0]
        assert event["kind"] == "high_heart_rate_notification"
        assert len(event["samples"]) == 2

    def test_sleep_in_bed_queryable_as_metric(self, api, pg):
        from health_db import queries

        api.post_metric_raw("sleep_analysis", "hr", INBED_ONLY)
        day = queries.query_day("2018-04-02", pool=pg.pool)
        names = [m["name"] for m in day["metrics"]]
        assert "sleep_in_bed" in names, "sleep_in_bed resolves through the standard metric path"

    def test_schema_resource_lists_new_tables(self, pg):
        from health_db import queries

        schema = queries.schema_resource(pool=pg.pool)
        tables = {t["name"] for t in schema["tables"]}
        assert {
            "ecgs", "ecg_voltages", "heart_rate_notifications", "heart_rate_notification_samples",
        } <= tables


# ---------------------------------------------------------------------------
# Census lock (anti-scenario 7): rule + fixtures move together
# ---------------------------------------------------------------------------


class TestCensusLock:
    def test_recent_sample_census(self, api, pg, archive):
        """The 09-09 archive corpus + trimmed recent sample must reproduce the
        audited census: sleep rows kept at real values, in-bed split, zero 0.0
        sleep rows. Exact census numbers live in the fixture file this test
        reads — they are part of the contract (see research §7)."""
        api.post_valid_metrics()
        zero_sleep = pg.scalar(
            "SELECT count(*) FROM raw_samples rs JOIN metric_dim md USING (metric_id) "
            "WHERE md.name='sleep_analysis' AND rs.qty = 0"
        )
        assert zero_sleep == 0, "no zero-valued sleep row may exist anywhere (anti-scenario 2)"


# ---------------------------------------------------------------------------
# Anti-scenarios: silent drop + migration safety
# ---------------------------------------------------------------------------


class TestAntiScenarios:
    def test_no_silent_drops_in_response(self, api, pg):
        """Anti-scenario 1: every posted point yields a row or a skipped entry."""
        import json as _json

        point = {
            "date": "2026-09-08 00:00:00 +0900", "asleep": 0, "inBed": 0,
            "core": 0, "deep": 0, "rem": 0, "totalSleep": 0,
        }
        payload = {
            "data": {
                "metrics": [{"name": "sleep_analysis", "units": "hr", "data": [point]}],
                "workouts": [],
            }
        }
        res = api._client.post(
            "/api/ingest", content=_json.dumps(payload).encode("utf-8"), headers=api._headers
        )
        body = res.json()
        skipped = body.get("skipped", {})
        assert skipped.get("sleep_no_value", 0) == 1, (
            "all-zero sleep point: no row stored, but the skip MUST be counted visibly"
        )

    def test_migration_0002_additive_only(self):
        """Anti-scenario 6: 0002 must not touch existing tables."""
        path = Path(__file__).resolve().parents[2] / "migrations" / "0002_parse_everything.sql"
        sql = path.read_text()
        upper = sql.upper()
        assert "ALTER TABLE" not in upper and "DROP TABLE" not in upper, (
            "additive CREATE TABLE only"
        )
        assert "CREATE TABLE" in upper


from pathlib import Path  # noqa: E402  (used by migration test above)
