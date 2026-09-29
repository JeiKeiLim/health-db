"""Unit tests — sleep-session lane persistence (job-4,
2026-09-28-mcp-analysis-parity).

Exercises the lane through the REAL :func:`health_db.pipeline.ingest_payload`
against the fake-executor pool from :mod:`tests.unit.test_lane_persistence`
(no docker, no real postgres — the real round-trips live in the requires_pg
acceptance suite ``tests/acceptance/mcp_analysis_parity.py``). Observables:

- a recognized aggregated-sleep payload point lands ONE sleep_sessions row
  with all 13 fields verbatim,
- re-posting the same (source_name, sleep_start) with a changed stage value
  rewrites that single row (latest-wins, iron law 2),
- a sleep-shaped point lacking a parseable sleep_start yields NO row and
  counts under ``skipped.sleep_session`` in the ingest response accounting,
- lane rows are OUT of daily_rollups.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tests.unit.test_lane_persistence import _FakePool, _sha_of, lane_doc
from tests.unit.test_sleep_sessions import NIGHT_2026_09_28 as NIGHT

KST = timezone(timedelta(hours=9))
KEY = ("Jongkuk의 Apple Watch", datetime(2026, 9, 27, 23, 5, 35, tzinfo=KST))


class TestSleepSessionLanePersistence:
    def _payload(self, *points: dict) -> dict:
        metric = {"name": "sleep_analysis", "units": "hr", "data": list(points)}
        return lane_doc(metrics=[metric])

    def _ingest(self, pool, payload, force=False):
        from health_db.pipeline import ingest_payload

        return ingest_payload(
            pool,
            payload,
            sha256=_sha_of(payload),
            body=json.dumps(payload).encode("utf-8"),
            force=force,
        )

    def test_session_row_lands_verbatim(self):
        payload = self._payload(NIGHT)
        pool = _FakePool()
        result = self._ingest(pool, payload)
        assert result.outcome == "stored"
        assert len(pool.txn.sleep_sessions) == 1
        row = pool.txn.sleep_sessions[KEY]
        # All 13 fields verbatim: the asleep: 0 Watch sentinel stores 0.0 —
        # never re-derived from totalSleep − stages.
        assert row["record_date"] == datetime(2026, 9, 28, 0, 0, tzinfo=KST)
        assert row["sleep_start"] == KEY[1]
        assert row["sleep_end"] == datetime(2026, 9, 28, 5, 49, 54, tzinfo=KST)
        assert row["in_bed_start"] == KEY[1]
        assert row["in_bed_end"] == row["sleep_end"]
        assert row["total_sleep"] == 6.2614
        assert row["asleep"] == 0.0
        assert row["in_bed"] == 0.0
        assert row["awake"] == 0.262
        assert row["deep"] == 1.043
        assert row["core"] == 3.7234
        assert row["rem"] == 1.495

    def test_reingest_changed_stage_latest_wins_single_row(self):
        """Same (source_name, sleep_start) arriving in a DIFFERENT payload
        (different sha, so no dedup gate) with changed stage values: one
        row still — every payload column refreshed by DO UPDATE (job-3 SQL
        contract)."""
        first = dict(NIGHT, core=3.7234, deep=1.043)
        second = dict(NIGHT, core=4.0, deep=1.0, totalSleep=6.5)
        pool = _FakePool()
        assert first != second, "distinct shas: the second post must reprocess"
        self._ingest(pool, self._payload(first))
        second_result = self._ingest(pool, self._payload(second), force=True)
        assert second_result.outcome == "stored", "new sha → fresh store, forced"
        assert len(pool.txn.sleep_sessions) == 1, "latest-wins: re-post never re-appends"
        row = pool.txn.sleep_sessions[KEY]
        assert row["core"] == 4.0, "DO UPDATE must refresh every payload column"
        assert row["deep"] == 1.0
        assert row["total_sleep"] == 6.5

    def test_force_reread_same_payload_rewrites_same_row(self):
        """Repair path over the SAME payload (identical sha, force=True):
        the sha gate is bypassed, the upsert conflicts on the natural key,
        and the SAME row is rewritten — count never grows."""
        pool = _FakePool()
        self._ingest(pool, self._payload(NIGHT))
        self._ingest(pool, self._payload(NIGHT), force=True)
        assert len(pool.txn.sleep_sessions) == 1
        row = pool.txn.sleep_sessions[KEY]
        assert row["core"] == 3.7234  # unchanged values, same row

    def test_session_and_existing_sleep_analysis_coexist_zero_new_metric_dim(self):
        payload = self._payload(NIGHT)
        pool = _FakePool()
        result = self._ingest(pool, payload)
        assert result.outcome == "stored"
        assert len(pool.txn.sleep_sessions) == 1
        assert result.samples == 1, "sleep_analysis point still takes its sample lane"
        assert pool.txn.rollups == [], "sleep is a session-like lane: no rollup rows"

    def test_no_sleep_start_skipped_and_counted(self):
        bad = dict(
            NIGHT,
            date="2026-09-17 00:00:00 +0900",
            totalSleep=6.3746,
            awake=0.05,
            core=3.5891,
            deep=1.0473,
            rem=1.7382,
        )
        bad.pop("sleepStart")
        pool = _FakePool()
        result = self._ingest(pool, self._payload(bad))
        assert result.outcome == "stored"
        assert pool.txn.sleep_sessions == {}, "no parseable sleep_start → no row"
        assert result.skipped["sleep_session"] == 1, "the drop must be VISIBLE"
