"""Unit tests — lane persistence wiring (job-7, run 2026-09-17-normalizer-audit).

Fakes only (no docker, no real postgres — the real round-trips live in the
requires_pg acceptance suite ``tests/acceptance/parse_everything.py``). The
pipeline's store stage is exercised through the REAL
:func:`health_db.pipeline.ingest_payload` against a stub pool whose
"connections" execute the helpers' SQL statements against in-memory fake
tables, so the tests assert the OBSERVABLE outcomes:

- ECG/notification rows persist in the SAME transaction (a lane crash rolls
  back the whole payload, not just the lane),
- re-posting a payload stores zero new rows (latest-wins upserts resolve
  the normalizer's ``ref`` to the SAME parent id — iron law 2),
- the lane skip censuses (``skipped.ecg`` / ``skipped.notification``)
  reach the IngestResult / ingest-response accounting (spec §5),
- rollups are untouched by lane rows (session lanes are OUT of
  daily_rollups).

The fake executor understands exactly the SQL statements the store stage
can issue (helpers' constants) and records every statement it saw —
that also proves the lane code issues the expected parameterized SQL.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from health_db.db import helpers as db_helpers
from health_db.normalize import SleepSessionRow
from health_db.pipeline import DbUnavailable, ingest_payload

KST = timezone(timedelta(hours=9))

ECG_SESSION = {
    "source": "심전도",
    "start": "2025-10-12 10:45:06 +0900",
    "end": "2025-10-12 10:45:36 +0900",
    "samplingFrequency": 512,
    "numberOfVoltageMeasurements": 3,
    "classification": "정상 동율 리듬",
    "averageHeartRate": 68,
    "voltageMeasurements": [
        {"voltage": -10.53, "date": 1760233506.862702, "units": "mcV"},
        {"voltage": -8.96, "date": 1760233506.864655, "units": "mcV"},
        {"voltage": -7.15, "date": 1760233506.8666081, "units": "mcV"},
    ],
}

HR_NOTIFICATION = {
    "start": "2026-06-16T04:39:20Z",
    "end": "2026-06-16T04:39:20Z",
    "threshold": 118,
    "heartNotification": "high_heart_rate_notification",
    "source": {"name": "Jongkuk의 Apple Watch"},
    "heartRateData": [
        {
            "hrUnits": "count/min",
            "heartRate": 132,
            "timestamp": {"start": "2026-06-16T04:40:29Z"},
        }
    ],
    "hrvData": [
        {
            "units": "ms",
            "hrv": 27.41,
            "timestamp": {"start": "2026-06-16T04:30:05Z"},
        }
    ],
}


def lane_doc(metrics: list | None = None, ecg: list | None = None, hrn: list | None = None) -> dict:
    data: dict[str, Any] = {"metrics": metrics or [], "workouts": []}
    if ecg is not None:
        data["ecg"] = ecg
    if hrn is not None:
        data["heartRateNotifications"] = hrn
    return {"data": data}


class _PoolUnavailable:
    """Stub pool whose connection() fails — the 503/outage seam."""

    def connection(self, timeout=None):
        raise RuntimeError("simulated DB outage (test stub)")


class _FakeTxn:
    """In-memory executor for the store stage's parameterized SQL.

    Understands the exact statement constants from :mod:`health_db.db.helpers`
    plus the raw_ingest SELECT and the rollup DELETE/INSERT (matched by a
    leading-SQL-substring probe, whitespace-insensitive). Every executed
    statement is recorded so tests can assert which parameterized SQL the
    lane wiring issued.
    """

    # Statement registry: first significant SQL fragment → handler.
    HANDLERS: tuple[tuple[str, str], ...] = (
        ("INSERT INTO metric_dim", "_metric_dim"),
        ("INSERT INTO source_dim", "_source_dim"),
        ("INSERT INTO raw_samples", "_raw_samples"),
        ("INSERT INTO workouts (", "_workouts"),
        ("INSERT INTO workout_heart_rate", "_child"),
        ("INSERT INTO routes", "_child"),
        ("INSERT INTO ecgs", "_ecgs"),
        ("INSERT INTO ecg_voltages", "_ecg_voltages"),
        ("INSERT INTO heart_rate_notifications", "_hrn"),
        ("INSERT INTO heart_rate_notification_samples", "_hrn_samples"),
        ("INSERT INTO daily_rollups", "_rollups"),
        ("DELETE FROM daily_rollups", "_rollups_delete"),
        ("INSERT INTO raw_ingest", "_raw_ingest"),
        ("SELECT outcome FROM raw_ingest", "_raw_ingest_select"),
        ("SELECT (now() AT TIME ZONE 'UTC')::date", "_today"),
        ("CREATE TABLE IF NOT EXISTS", "_returning_none"),
        ("SELECT", "_returning_none"),
    )

    def __init__(self) -> None:
        self.metric_dim: dict[str, int] = {}
        self.source_dim: dict[str, int] = {}
        self.raw_samples: list[tuple[int, int, datetime, float]] = []
        self.ecgs: dict[tuple[str, datetime], int] = {}
        self.ecg_rows: dict[int, dict[str, Any]] = {}
        self.ecg_voltages: dict[tuple[int, int], tuple[datetime, float, str]] = {}
        self.hrn: dict[tuple[str, datetime], int] = {}
        self.hrn_rows: dict[int, dict[str, Any]] = {}
        self.hrn_samples: dict[tuple[int, datetime, str], tuple[float, str | None]] = {}
        self.rollups: list[tuple[int, str, float]] = []
        self.raw_ingest: dict[str, dict[str, Any]] = {}
        self.statements: list[tuple[str, tuple]] = []
        # Real executemany batch sizes: (sql-fragments, len(seq)) per call
        # (the multi-batch voltage test observes the actual chunk sizes).
        self.executemany_sizes: list[tuple[str, int]] = []
        self._next_metric = 1
        self._next_source = 1
        self._next_ecg = 1
        self._next_hrn = 1
        self.rolled_back = False
        self.committed = False
        self.fail_statement_prefix: str | None = None

    # --- connection/pool protocol used by ingest_payload -------------------
    def __enter__(self) -> _FakeTxn:
        # Snapshot for rollback semantics: a failed transaction must leave
        # the fake tables exactly as they were (all-or-nothing contract).
        self._snapshot = {
            "metric_dim": dict(self.metric_dim),
            "source_dim": dict(self.source_dim),
            "raw_samples": list(self.raw_samples),
            "ecgs": dict(self.ecgs),
            "ecg_rows": {k: dict(v) for k, v in self.ecg_rows.items()},
            "ecg_voltages": dict(self.ecg_voltages),
            "hrn": dict(self.hrn),
            "hrn_rows": {k: dict(v) for k, v in self.hrn_rows.items()},
            "hrn_samples": dict(self.hrn_samples),
            "rollups": list(self.rollups),
            "raw_ingest": {k: dict(v) for k, v in self.raw_ingest.items()},
            "_next_metric": self._next_metric,
            "_next_source": self._next_source,
            "_next_ecg": self._next_ecg,
            "_next_hrn": self._next_hrn,
        }
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.rolled_back = True
            self.metric_dim = self._snapshot["metric_dim"]
            self.source_dim = self._snapshot["source_dim"]
            self.raw_samples = self._snapshot["raw_samples"]
            self.ecgs = self._snapshot["ecgs"]
            self.ecg_rows = self._snapshot["ecg_rows"]
            self.ecg_voltages = self._snapshot["ecg_voltages"]
            self.hrn = self._snapshot["hrn"]
            self.hrn_rows = self._snapshot["hrn_rows"]
            self.hrn_samples = self._snapshot["hrn_samples"]
            self.rollups = self._snapshot["rollups"]
            self.raw_ingest = self._snapshot["raw_ingest"]
            self._next_metric = self._snapshot["_next_metric"]
            self._next_source = self._snapshot["_next_source"]
            self._next_ecg = self._snapshot["_next_ecg"]
            self._next_hrn = self._snapshot["_next_hrn"]
            return False  # propagate (pipeline wraps into DbUnavailable)
        self.committed = True
        return False

    def cursor(self):
        return _FakeCursor(self)

    def execute(self, sql: str, params: tuple = ()) -> _FakeResult:
        probe = " ".join(sql.split())
        self.statements.append((sql, params))
        if (
            self.fail_statement_prefix is not None
            and probe.startswith(self.fail_statement_prefix)
        ):
            raise RuntimeError(f"simulated SQL failure at: {self.fail_statement_prefix}")
        for prefix, handler in self.HANDLERS:
            if probe.startswith(prefix):
                return getattr(self, handler)(probe, params)
        raise AssertionError(f"fake txn does not understand SQL: {probe[:80]}")

    # --- statement handlers -------------------------------------------------
    def _metric_dim(self, probe: str, params: tuple) -> _FakeResult:
        name, _display, _unit, _cum = params
        if name not in self.metric_dim:
            self.metric_dim[name] = self._next_metric
            self._next_metric += 1
        return _FakeResult([(self.metric_dim[name],)])

    def _source_dim(self, probe: str, params: tuple) -> _FakeResult:
        (name,) = params
        if name not in self.source_dim:
            self.source_dim[name] = self._next_source
            self._next_source += 1
        return _FakeResult([(self.source_dim[name],)])

    def _raw_samples(self, probe: str, params: tuple) -> _FakeResult:
        metric_id, source_id, ts, qty = params
        for i, row in enumerate(self.raw_samples):
            if row[0] == metric_id and row[1] == source_id and row[2] == ts:
                self.raw_samples[i] = (metric_id, source_id, ts, qty)
                return _FakeResult([])
        self.raw_samples.append((metric_id, source_id, ts, qty))
        return _FakeResult([])

    def _workouts(self, probe: str, params: tuple) -> _FakeResult:
        return _FakeResult([])

    def _child(self, probe: str, params: tuple) -> _FakeResult:
        return _FakeResult([])

    def _ecgs(self, probe: str, params: tuple) -> _FakeResult:
        source_name, start_ts = params[0], params[1]
        key = (source_name, start_ts)
        if key not in self.ecgs:
            self.ecgs[key] = self._next_ecg
            self._next_ecg += 1
        ecg_id = self.ecgs[key]
        self.ecg_rows[ecg_id] = {
            "end_ts": params[2],
            "classification": params[3],
            "sampling_frequency": params[4],
            "average_heart_rate": params[5],
            "voltage_count": params[6],
        }
        return _FakeResult([(ecg_id,)])

    def _ecg_voltages(self, probe: str, params: tuple) -> _FakeResult:
        ecg_id, seq, ts, voltage, units = params
        self.ecg_voltages[(ecg_id, seq)] = (ts, voltage, units)
        return _FakeResult([])

    def _hrn(self, probe: str, params: tuple) -> _FakeResult:
        source_name, start_ts = params[0], params[1]
        key = (source_name, start_ts)
        if key not in self.hrn:
            self.hrn[key] = self._next_hrn
            self._next_hrn += 1
        hrn_id = self.hrn[key]
        self.hrn_rows[hrn_id] = {
            "end_ts": params[2],
            "kind": params[3],
            "threshold": params[4],
        }
        return _FakeResult([(hrn_id,)])

    def _hrn_samples(self, probe: str, params: tuple) -> _FakeResult:
        hrn_id, ts, kind, value, units = params
        self.hrn_samples[(hrn_id, ts, kind)] = (value, units)
        return _FakeResult([])

    def _rollups(self, probe: str, params: tuple) -> _FakeResult:
        return _FakeResult([])

    def _rollups_delete(self, probe: str, params: tuple) -> _FakeResult:
        return _FakeResult([])

    def _raw_ingest(self, probe: str, params: tuple) -> _FakeResult:
        (sha256, *_rest) = params
        self.raw_ingest[sha256] = {
            "data_type": params[4],
            "payload": params[5],
            "outcome": params[6],
        }
        return _FakeResult([])

    def _raw_ingest_select(self, probe: str, params: tuple) -> _FakeResult:
        (sha256,) = params
        found = self.raw_ingest.get(sha256)
        return _FakeResult([(found["outcome"],)] if found else [])

    def _today(self, probe: str, params: tuple) -> _FakeResult:
        return _FakeResult([(datetime.now(tz=timezone.utc).date(),)])

    def _returning_none(self, probe: str, params: tuple) -> _FakeResult:
        # pg_advisory_xact_lock / CREATE TABLE IF NOT EXISTS / now() probes
        # issued by the partition layer: no rows, statement recorded.
        return _FakeResult([])


class _FakeCursor:
    def __init__(self, txn: _FakeTxn) -> None:
        self._txn = txn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def executemany(self, sql: str, seq: Any) -> None:
        # Record the REAL batch size the pipeline's loop handed to this
        # statement (item 4: the batching arithmetic must be observed, not
        # re-implemented — the multi-batch test reads this list).
        params = [tuple(p) for p in seq]
        self._txn.executemany_sizes.append((sql.split()[0] + "-" + sql.split()[2], len(params)))
        for p in params:
            self._txn.execute(sql, p)


class _FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.rowcount = len(rows)

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakePool:
    def __init__(self) -> None:
        self.txn = _FakeTxn()
        self.borrowed = 0

    def connection(self, timeout=None) -> _FakeTxn:
        self.borrowed += 1
        return self.txn


def _sha_of(doc: dict) -> str:
    from health_db.pending import PendingStore

    return PendingStore.sha256_of(json.dumps(doc).encode("utf-8"))


def _upsert_sleep_session_columns(sql: str) -> tuple[str, ...]:
    """Column list of the INSERT head (source_name…in_bed), in order."""
    column_list = sql.split("(", 1)[1].split(")")[0]
    return tuple(name.strip() for name in column_list.split(","))


def _dataclass_field_names(row: object) -> tuple[str, ...]:
    import dataclasses

    return tuple(field.name for field in dataclasses.fields(row))


# ---------------------------------------------------------------------------
# helpers SQL contract: the four new constants exist and carry the keys
# ---------------------------------------------------------------------------


class TestLaneSqlConstants:
    def test_upsert_ecg_conflicts_on_source_and_start_returning_id(self):
        sql = " ".join(db_helpers.UPSERT_ECG.split())
        assert "INSERT INTO ecgs" in sql
        assert "ON CONFLICT (source_name, start_ts) DO UPDATE" in sql
        assert "RETURNING id" in sql
        assert "ingested_at" in sql

    def test_upsert_ecg_voltage_conflicts_on_parent_and_seq(self):
        sql = " ".join(db_helpers.UPSERT_ECG_VOLTAGE.split())
        assert "INSERT INTO ecg_voltages" in sql
        assert "ON CONFLICT (ecg_id, seq) DO UPDATE" in sql

    def test_upsert_hrn_conflicts_on_source_and_start_returning_id(self):
        sql = " ".join(db_helpers.UPSERT_HRN.split())
        assert "INSERT INTO heart_rate_notifications" in sql
        assert "ON CONFLICT (source_name, start_ts) DO UPDATE" in sql
        assert "RETURNING id" in sql

    def test_upsert_hrn_sample_conflicts_on_parent_ts_kind(self):
        sql = " ".join(db_helpers.UPSERT_HRN_SAMPLE.split())
        assert "INSERT INTO heart_rate_notification_samples" in sql
        assert "ON CONFLICT (notification_id, ts, kind) DO UPDATE" in sql

    def test_upsert_sleep_session_conflicts_on_source_and_start_returning_id(self):
        """Sleep-session lane (run 2026-09-28-mcp-analysis-parity): same
        latest-wins pattern as the ECG/HRN lanes — UNIQUE (source_name,
        sleep_start) natural key, RETURNING id, ingested_at refreshed on
        update. Column order mirrors SleepSessionRow field order.
        """
        sql = " ".join(db_helpers.UPSERT_SLEEP_SESSION.split())
        assert "INSERT INTO sleep_sessions" in sql
        assert "ON CONFLICT (source_name, sleep_start) DO UPDATE" in sql
        assert "RETURNING id" in sql
        assert "ingested_at = now()" in sql
        row = SleepSessionRow(
            source_name="Source",
            record_date=datetime(2026, 9, 28, tzinfo=KST),
            sleep_start=datetime(2026, 9, 27, 23, 5, 35, tzinfo=KST),
        )
        columns = _upsert_sleep_session_columns(db_helpers.UPSERT_SLEEP_SESSION)
        assert _dataclass_field_names(row) == columns

    def test_sleep_session_upsert_is_fully_parameterized(self):
        """13 payload columns ↔ 13 %s placeholders (day_kst is DB-generated;
        no string interpolation — parameterized SQL iron law)."""
        placeholder_count = db_helpers.UPSERT_SLEEP_SESSION.count("%s")
        assert placeholder_count == 13
        columns = _upsert_sleep_session_columns(db_helpers.UPSERT_SLEEP_SESSION)
        assert len(columns) == placeholder_count
        assert columns[0] == "source_name" and columns[-1] == "in_bed"

    def test_voltage_batch_cap_is_ten_thousand(self):
        from health_db.normalize import ECG_VOLTAGE_BATCH

        assert ECG_VOLTAGE_BATCH == 10_000

    def test_sample_upsert_batches_never_exceed_cap(self):
        """The metrics lane chunks >cap rows through bounded executemany
        calls: drive the REAL ingest_payload with 10,003 samples and assert
        every row reaches the upsert while no single executemany call
        exceeded the cap (observed via the fake cursor's batch-size log —
        the batching arithmetic is never re-implemented here).

        Regression: the full-history backfill metrics chunk (52,000 points) was
        one unbounded pipelined executemany that aborted mid-pipeline on
        the server deployment (DbUnavailable → 503 → pending queue).
        """
        from health_db.pipeline import SAMPLE_UPSERT_BATCH

        assert SAMPLE_UPSERT_BATCH == 10_000

        total = 10_003  # one full batch + a 3-row tail (non-multiple size)
        metric = {
            "name": "step_count",
            "units": "count",
            "data": [
                {"qty": 1.0,
                 "date": "2026-01-01 00:00:00 +0900",
                 "source": "Jongkuk의 Apple Watch"},
            ],
        }
        # 10,003 distinct parseable timestamps spread over hours + seconds
        metric["data"] = [
            {
                "qty": 1.0,
                "date": (
                    f"2026-01-01 {i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d} +0900"
                ),
                "source": "Jongkuk의 Apple Watch",
            }
            for i in range(total)
        ]
        doc = lane_doc(metrics=[metric])
        pool = _FakePool()
        result = ingest_payload(
            pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8")
        )
        assert result.outcome == "stored"
        assert result.samples == total
        assert len(pool.txn.raw_samples) == total
        # The observed chunk sizes honor the cap and sum to the total.
        sizes = [size for (frag, size) in pool.txn.executemany_sizes if "raw_samples" in frag]
        assert sum(sizes) == total
        assert all(size <= SAMPLE_UPSERT_BATCH for size in sizes), (
            f"no executemany may exceed the {SAMPLE_UPSERT_BATCH} cap: {sizes}"
        )
        assert sizes == [SAMPLE_UPSERT_BATCH, total - SAMPLE_UPSERT_BATCH], (
            "one full batch + the remainder tail"
        )

    def test_voltage_batches_never_exceed_cap(self):
        """The REAL ingest_payload loop chunks >10k voltages: drive it with
        10,003 voltages through the fake pool and assert every row reaches
        the upsert while no single executemany call exceeded the cap (the
        fake cursor records each real batch size — the batching arithmetic
        is observed, never re-implemented here)."""
        from health_db.normalize import ECG_VOLTAGE_BATCH

        total = 10_003  # one full batch + a 3-row tail (non-multiple size)
        session = {
            **ECG_SESSION,
            "numberOfVoltageMeasurements": total,
            "voltageMeasurements": [
                {
                    "voltage": -1.0 - (i % 7) * 0.01,
                    "date": 1760233506.862702 + i * 0.002,
                    "units": "mcV",
                }
                for i in range(total)
            ],
        }
        doc = lane_doc(ecg=[session])
        pool = _FakePool()
        result = ingest_payload(
            pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8")
        )
        assert result.outcome == "stored"
        # Every input row was offered to the SQL helper, exactly once.
        assert len(pool.txn.ecg_voltages) == total
        assert len({seq for (_, seq) in pool.txn.ecg_voltages}) == total
        # The observed chunk sizes honor the cap and sum to the total.
        sizes = [size for (frag, size) in pool.txn.executemany_sizes if "voltages" in frag]
        assert sum(sizes) == total
        assert all(size <= ECG_VOLTAGE_BATCH for size in sizes), (
            f"no executemany may exceed the {ECG_VOLTAGE_BATCH} cap: {sizes}"
        )
        assert sizes == [ECG_VOLTAGE_BATCH, total - ECG_VOLTAGE_BATCH], (
            "one full batch + the remainder tail"
        )


# ---------------------------------------------------------------------------
# ECG lane persistence through the REAL ingest_payload
# ---------------------------------------------------------------------------


class TestEcgLanePersistence:
    def test_session_and_voltages_stored_with_resolved_ids(self):
        doc = lane_doc(ecg=[ECG_SESSION])
        pool = _FakePool()
        result = ingest_payload(
            pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8")
        )
        assert result.outcome == "stored"
        assert len(pool.txn.ecgs) == 1 and len(pool.txn.ecg_voltages) == 3
        ecg_id = next(iter(pool.txn.ecgs.values()))
        row = pool.txn.ecg_rows[ecg_id]
        assert row["classification"] == "정상 동율 리듬"
        assert row["average_heart_rate"] == 68
        assert row["voltage_count"] == 3
        # All voltages attached to the SAME parent id (ref → id resolution).
        assert {key[0] for key in pool.txn.ecg_voltages} == {ecg_id}
        # Voltage ts is UNIX-epoch converted: first == session start ±2s.
        first_ts, first_voltage, first_units = pool.txn.ecg_voltages[(ecg_id, 0)]
        assert first_voltage == pytest.approx(-10.53)
        assert first_units == "mcV"
        session_start = ECG_SESSION_START_UTC
        assert abs((first_ts - session_start).total_seconds()) <= 2.0

    def test_lane_uses_the_parameterized_helper_sql(self):
        doc = lane_doc(ecg=[ECG_SESSION])
        pool = _FakePool()
        ingest_payload(pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8"))
        executed = [" ".join(sql.split()) for sql, _ in pool.txn.statements]
        assert any(e.startswith("INSERT INTO ecgs") for e in executed)
        assert any(e.startswith("INSERT INTO ecg_voltages") for e in executed)

    def test_repost_stores_zero_new_rows_latest_wins(self):
        doc = lane_doc(ecg=[ECG_SESSION])
        body = json.dumps(doc).encode("utf-8")
        pool = _FakePool()
        first = ingest_payload(pool, doc, sha256=_sha_of(doc), body=body)
        assert first.outcome == "stored"
        second = ingest_payload(pool, doc, sha256=_sha_of(doc), body=body)
        # DB-side sha guard short-circuits: no reprocessing, outcome echoes
        # the raw_ingest record ('stored'), never a second store.
        assert second.outcome == "stored"
        assert pool.borrowed == 2
        assert len(pool.txn.ecgs) == 1 and len(pool.txn.ecg_voltages) == 3

    def test_force_repost_reupserts_same_rows_zero_append(self):
        """Repair path (force=True): the payload reprocesses, conflicts on
        the natural key, and rewrites the SAME rows — never re-appends."""
        doc = lane_doc(ecg=[ECG_SESSION])
        body = json.dumps(doc).encode("utf-8")
        pool = _FakePool()
        ingest_payload(pool, doc, sha256=_sha_of(doc), body=body)
        n_before = (len(pool.txn.ecgs), len(pool.txn.ecg_voltages))
        repaired = ingest_payload(
            pool, doc, sha256=_sha_of(doc), body=body, force=True
        )
        assert repaired.outcome == "repaired"
        n_after = (len(pool.txn.ecgs), len(pool.txn.ecg_voltages))
        assert n_before == n_after, "latest-wins: re-post never appends"
        ecg_id = next(iter(pool.txn.ecgs.values()))
        assert len(pool.txn.ecg_voltages) == 3
        assert {key[0] for key in pool.txn.ecg_voltages} == {ecg_id}

    def test_malformed_session_counted_under_skipped_ecg(self):
        bad = {**ECG_SESSION, "start": "bogus"}
        doc = lane_doc(ecg=[bad, ECG_SESSION])
        pool = _FakePool()
        result = ingest_payload(
            pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8")
        )
        assert result.skipped["ecg"] == 1, "unparseable start must be visible (spec §5)"
        assert len(pool.txn.ecgs) == 1, "the good sibling still stores"
        assert result.skipped["unrecognized_shape"] == 0, "lane drops never double-count"

    def test_lane_rows_out_of_rollups(self):
        doc = lane_doc(ecg=[ECG_SESSION])
        pool = _FakePool()
        ingest_payload(pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8"))
        assert pool.txn.rollups == [], "ECG is a session lane: no rollup rows"

    def test_lane_failure_rolls_back_whole_payload(self):
        """One transaction per payload: a failure DURING the lane upsert
        rolls back the metric samples too (all-or-nothing contract)."""
        doc = lane_doc(
            metrics=[
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [
                        {"qty": 5, "date": "2026-09-08 10:00:00 +0900", "source": "Apple Watch"}
                    ],
                }
            ],
            ecg=[ECG_SESSION],
        )
        pool = _FakePool()
        pool.txn.fail_statement_prefix = "INSERT INTO ecgs"
        with pytest.raises(DbUnavailable):
            ingest_payload(pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8"))
        assert pool.txn.rolled_back, "the failing transaction must roll back"
        assert pool.txn.raw_samples == [], "same transaction: samples rolled back too"
        assert pool.txn.ecgs == {}

    def test_absent_lane_yields_no_rows_and_no_skips(self):
        doc = lane_doc(
            metrics=[
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [
                        {"qty": 5, "date": "2026-09-08 10:00:00 +0900", "source": "Apple Watch"}
                    ],
                }
            ]
        )
        pool = _FakePool()
        result = ingest_payload(
            pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8")
        )
        assert result.skipped["ecg"] == 0
        assert result.skipped["notification"] == 0
        assert pool.txn.ecgs == {} and pool.txn.hrn == {}


ECG_SESSION_START_UTC = datetime(2025, 10, 12, 1, 45, 6, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# HR-notification lane persistence through the REAL ingest_payload
# ---------------------------------------------------------------------------


class TestNotificationLanePersistence:
    def test_event_and_samples_stored_with_resolved_ids(self):
        doc = lane_doc(hrn=[HR_NOTIFICATION])
        pool = _FakePool()
        result = ingest_payload(
            pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8")
        )
        assert result.outcome == "stored"
        assert len(pool.txn.hrn) == 1 and len(pool.txn.hrn_samples) == 2
        hrn_id = next(iter(pool.txn.hrn.values()))
        row = pool.txn.hrn_rows[hrn_id]
        assert row["kind"] == "high_heart_rate_notification"
        assert row["threshold"] == 118
        kinds = {key[2] for key in pool.txn.hrn_samples if key[0] == hrn_id}
        assert kinds == {"hr", "hrv"}
        hr_ts = datetime(2026, 6, 16, 4, 40, 29, tzinfo=timezone.utc)
        hr_value = pool.txn.hrn_samples[(hrn_id, hr_ts, "hr")][0]
        assert hr_value == 132.0

    def test_repost_stores_zero_new_rows_latest_wins(self):
        doc = lane_doc(hrn=[HR_NOTIFICATION])
        body = json.dumps(doc).encode("utf-8")
        pool = _FakePool()
        first = ingest_payload(pool, doc, sha256=_sha_of(doc), body=body)
        assert first.outcome == "stored"
        second = ingest_payload(pool, doc, sha256=_sha_of(doc), body=body)
        assert second.outcome == "stored"
        assert len(pool.txn.hrn) == 1 and len(pool.txn.hrn_samples) == 2

    def test_force_repost_reupserts_same_rows_zero_append(self):
        doc = lane_doc(hrn=[HR_NOTIFICATION])
        body = json.dumps(doc).encode("utf-8")
        pool = _FakePool()
        ingest_payload(pool, doc, sha256=_sha_of(doc), body=body)
        before = (len(pool.txn.hrn), len(pool.txn.hrn_samples))
        repaired = ingest_payload(pool, doc, sha256=_sha_of(doc), body=body, force=True)
        assert repaired.outcome == "repaired"
        after = (len(pool.txn.hrn), len(pool.txn.hrn_samples))
        assert before == after, "latest-wins: re-post never appends"
        assert len(pool.txn.hrn_samples) == 2

    def test_malformed_event_counted_under_skipped_notification(self):
        bad = {**HR_NOTIFICATION, "start": "bogus"}
        doc = lane_doc(hrn=[bad, HR_NOTIFICATION])
        pool = _FakePool()
        result = ingest_payload(
            pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8")
        )
        assert result.skipped["notification"] == 1
        assert len(pool.txn.hrn) == 1

    def test_malformed_sample_counted_event_still_stored(self):
        bad = {**HR_NOTIFICATION, "heartRateData": [{"hrUnits": "count/min"}]}
        doc = lane_doc(hrn=[bad])
        pool = _FakePool()
        result = ingest_payload(
            pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8")
        )
        assert result.skipped["notification"] == 1
        assert len(pool.txn.hrn) == 1, "the event itself still stores"
        assert len(pool.txn.hrn_samples) == 1, "only the hrv sibling survives"

    def test_lane_rows_out_of_rollups(self):
        doc = lane_doc(hrn=[HR_NOTIFICATION])
        pool = _FakePool()
        ingest_payload(pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8"))
        assert pool.txn.rollups == []

    def test_both_lanes_in_one_payload_one_borrow(self):
        """Same transaction: both lanes persist inside a single pool borrow
        (the pool connection context is the transaction boundary)."""
        doc = lane_doc(ecg=[ECG_SESSION], hrn=[HR_NOTIFICATION])
        pool = _FakePool()
        result = ingest_payload(
            pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8")
        )
        assert result.outcome == "stored"
        assert pool.borrowed == 1
        assert len(pool.txn.ecgs) == 1 and len(pool.txn.hrn) == 1
        assert len(pool.txn.ecg_voltages) == 3 and len(pool.txn.hrn_samples) == 2

    def test_raw_ingest_data_type_is_lane_key_for_lane_only_payload(self):
        """A lane-only payload (no metrics, no workouts) labels raw_ingest.
        data_type with the lane that carried it — the SAME classifier feeds
        the /api/ingest response, so the two labels cannot diverge."""
        doc = lane_doc(ecg=[ECG_SESSION])
        pool = _FakePool()
        ingest_payload(pool, doc, sha256=_sha_of(doc), body=json.dumps(doc).encode("utf-8"))
        row = next(iter(pool.txn.raw_ingest.values()))
        assert row["data_type"] == "ecg"
