"""Unit tests for the replay tool (job slice-2-replay) — no docker, no DB.

A fake ingest seam + fake pool record every call so the tests assert the
OBSERVABLE replay outcomes: which payloads were applied, which were skipped,
which failed, that pending pairs are deleted only after a successful apply,
and that reports are JSON-ready. DB-side dedup behavior (raw_ingest gate)
is covered by the postgres acceptance suite (TestBackfillReplayIdempotent).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from health_db import replay
from health_db.pending import PendingStore

KST = timezone(timedelta(hours=9))


def metrics_payload(qty: float = 100.0, date: str = "2026-01-01 10:00:00 +0900") -> dict:
    return {
        "data": {
            "metrics": [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [{"qty": qty, "date": date, "source": "Apple Watch"}],
                }
            ],
            "workouts": [],
        }
    }


@dataclass
class FakeIngest:
    """Records calls; configurable per-sha outcomes and failures."""

    results: dict[str, object] = field(default_factory=dict)
    fail_shas: set[str] = field(default_factory=set)
    calls: list[tuple[str, str, str]] = field(default_factory=list)  # (sha, body, outcome)
    forces: list[bool] = field(default_factory=list)

    def __call__(self, pool, document, *, sha256, body, headers, outcome, force=False):
        assert pool == "fake-pool", "replay must pass its pool through"
        self.calls.append((sha256, body.decode("utf-8"), outcome))
        self.forces.append(force)
        if sha256 in self.fail_shas:
            raise RuntimeError("simulated store-stage failure")
        return self.results.get(sha256, "stored")


@pytest.fixture()
def fake_pool(monkeypatch):
    """No PSQL_* env, no materialized singleton — replay must raise, never
    silently construct a pool against ambient defaults."""
    from health_db.db import pool as db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda settings=None: "fake-pool")
    monkeypatch.setattr(db_pool, "has_explicit_env", lambda environ=None: True)
    return "fake-pool"


class TestReplayDir:
    def test_applies_every_archive_payload_with_replayed_outcome(self, tmp_path, fake_pool):
        for sub in ("2026/01/01", "2026/01/02"):
            day = tmp_path / sub
            day.mkdir(parents=True)
            (day / "120000-abc.json").write_bytes(json.dumps(metrics_payload()).encode("utf-8"))
        ingest = FakeIngest()

        report = replay.replay_dir(tmp_path, "fake-pool", ingest=ingest)

        assert report.mode == "dir"
        assert report.seen == 2
        assert report.processed == 2
        assert report.failed == 0
        assert report.skipped == 0
        assert {sha for sha, _, _ in ingest.calls} == {
            PendingStore.sha256_of(json.dumps(metrics_payload()).encode("utf-8"))
        }
        assert {outcome for _, _, outcome in ingest.calls} == {"replayed"}

    def test_skips_quarantine_subtree(self, tmp_path, fake_pool):
        live = tmp_path / "2026" / "01" / "01"
        live.mkdir(parents=True)
        (live / "a.json").write_bytes(json.dumps(metrics_payload()).encode("utf-8"))
        quar = tmp_path / "quarantine"
        quar.mkdir()
        (quar / "bad.json").write_bytes(json.dumps(metrics_payload(qty=1)).encode("utf-8"))
        ingest = FakeIngest()

        report = replay.replay_dir(tmp_path, "fake-pool", ingest=ingest)

        assert report.seen == 1, "quarantined bytes were never accepted — never replayed"
        assert report.processed == 1

    def test_skips_pending_sidecar_files(self, tmp_path, fake_pool):
        day = tmp_path / "2026" / "01" / "01"
        day.mkdir(parents=True)
        (day / "120000-abc.json").write_bytes(json.dumps(metrics_payload()).encode("utf-8"))
        (day / "120000-abc.meta.json").write_bytes(b'{"sha256": "x"}')
        ingest = FakeIngest()

        report = replay.replay_dir(tmp_path, "fake-pool", ingest=ingest)

        assert report.seen == 1, ".meta.json sidecars are not payloads"

    def test_unparseable_file_skipped_not_failed_and_left_on_disk(self, tmp_path, fake_pool):
        day = tmp_path / "2026" / "01" / "01"
        day.mkdir(parents=True)
        bad = day / "120000-broken.json"
        bad.write_bytes(b"{broken")
        (day / "120001-good.json").write_bytes(json.dumps(metrics_payload()).encode("utf-8"))
        ingest = FakeIngest()

        report = replay.replay_dir(tmp_path, "fake-pool", ingest=ingest)

        assert report.seen == 2
        assert report.skipped == 1
        assert report.processed == 1
        assert report.errors and "broken" in report.errors[0]
        assert bad.read_bytes() == b"{broken", "replay never edits the archive"

    def test_store_failure_is_counted_not_raised(self, tmp_path, fake_pool):
        day = tmp_path / "2026" / "01" / "01"
        day.mkdir(parents=True)
        body = json.dumps(metrics_payload()).encode("utf-8")
        (day / "120000-abc.json").write_bytes(body)
        ingest = FakeIngest(fail_shas={PendingStore.sha256_of(body)})

        report = replay.replay_dir(tmp_path, "fake-pool", ingest=ingest)

        assert report.failed == 1
        assert report.processed == 0
        assert report.errors and "abc" in report.errors[0]

    def test_no_pool_raises_runtime_error(self, tmp_path, monkeypatch):
        from health_db.db import pool as db_pool

        monkeypatch.setattr(db_pool, "has_explicit_env", lambda environ=None: False)
        monkeypatch.setattr(db_pool, "existing_pool", lambda: None)
        with pytest.raises(RuntimeError):
            replay.replay_dir(tmp_path, None)

    def test_report_summary_is_json_ready(self, tmp_path, fake_pool):
        day = tmp_path / "2026" / "01" / "01"
        day.mkdir(parents=True)
        (day / "a.json").write_bytes(json.dumps(metrics_payload()).encode("utf-8"))
        report = replay.replay_dir(tmp_path, "fake-pool", ingest=FakeIngest())
        encoded = json.dumps(report.summary())
        assert json.loads(encoded)["processed"] == 1

    def test_force_flag_propagates_to_ingest_and_summary(self, tmp_path, fake_pool):
        day = tmp_path / "2026" / "01" / "01"
        day.mkdir(parents=True)
        (day / "a.json").write_bytes(json.dumps(metrics_payload()).encode("utf-8"))
        ingest = FakeIngest()

        report = replay.replay_dir(tmp_path, "fake-pool", ingest=ingest, force=True)

        assert ingest.forces == [True], "repair must send force=True to the pipeline"
        assert report.force is True
        assert report.summary()["force"] is True

    def test_default_force_is_false(self, tmp_path, fake_pool):
        day = tmp_path / "2026" / "01" / "01"
        day.mkdir(parents=True)
        (day / "a.json").write_bytes(json.dumps(metrics_payload()).encode("utf-8"))
        ingest = FakeIngest()
        report = replay.replay_dir(tmp_path, "fake-pool", ingest=ingest)
        assert ingest.forces == [False]
        assert report.summary()["force"] is False


class TestReplayPending:
    def _queue(self, tmp_path, qty=100.0, sha=None, automation=None):
        store = PendingStore(root=tmp_path / "pending")
        body = json.dumps(metrics_payload(qty=qty)).encode("utf-8")
        store.store(
            body,
            sha256=sha or store.sha256_of(body),
            automation_name=automation,
            received_at=datetime(2026, 9, 8, 10, 0, tzinfo=KST),
        )
        return store, body

    def test_drains_queue_and_passes_sidecar_metadata(self, tmp_path, fake_pool):
        store, body = self._queue(tmp_path, automation="metrics-all")
        ingest = FakeIngest()

        report = replay.replay_pending("fake-pool", ingest=ingest, pending=store)

        assert report.pending_drained == 1
        assert report.processed == 1
        assert store.pending_count() == 0, "queue drains to zero after success"
        sha, sent_body, outcome = ingest.calls[0]
        assert sent_body == body.decode("utf-8")
        assert outcome == "replayed"

    def test_failed_apply_keeps_payload_queued(self, tmp_path, fake_pool):
        store, body = self._queue(tmp_path)
        sha = store.sha256_of(body)
        ingest = FakeIngest(fail_shas={sha})

        report = replay.replay_pending("fake-pool", ingest=ingest, pending=store)

        assert report.failed == 1
        assert report.pending_drained == 0
        assert store.pending_count() == 1, "only successful commits may drain a payload"

    def test_mixed_queue_drains_only_successful(self, tmp_path, fake_pool):
        store, ok_body = self._queue(tmp_path, qty=100.0)
        ok_sha = store.sha256_of(ok_body)
        bad_body = json.dumps(metrics_payload(qty=222.0)).encode("utf-8")
        bad_sha = store.sha256_of(bad_body)
        store.store(bad_body, sha256=bad_sha, received_at=datetime(2026, 9, 8, 11, 0, tzinfo=KST))
        ingest = FakeIngest(fail_shas={bad_sha})

        report = replay.replay_pending("fake-pool", ingest=ingest, pending=store)

        assert report.pending_drained == 1
        assert report.failed == 1
        assert store.pending_count() == 1
        assert [sha for sha, _, _ in ingest.calls] == [ok_sha, bad_sha]

    def test_unparseable_queued_payload_skipped_and_kept(self, tmp_path, fake_pool):
        store = PendingStore(root=tmp_path / "pending")
        body = b"{broken"
        store.store(body, sha256=store.sha256_of(body))
        ingest = FakeIngest()

        report = replay.replay_pending("fake-pool", ingest=ingest, pending=store)

        assert report.skipped == 1
        assert report.pending_drained == 0
        assert store.pending_count() == 1

    def test_sidecar_less_payload_still_replays(self, tmp_path, fake_pool):
        """A payload file without its sidecar (crash between atomic writes)
        must replay with best-effort metadata, never be dropped."""
        store = PendingStore(root=tmp_path / "pending")
        body = json.dumps(metrics_payload()).encode("utf-8")
        (tmp_path / "pending").mkdir(parents=True)
        (tmp_path / "pending" / "100000-abcdef12.json").write_bytes(body)
        ingest = FakeIngest()

        report = replay.replay_pending("fake-pool", ingest=ingest, pending=store)

        assert report.seen == 1
        assert report.pending_drained == 1
        assert store.pending_count() == 0

    def test_no_pool_raises_runtime_error(self, tmp_path, monkeypatch):
        from health_db.db import pool as db_pool

        monkeypatch.setattr(db_pool, "existing_pool", lambda: None)
        with pytest.raises(RuntimeError):
            replay.replay_pending(None, pending=PendingStore(root=tmp_path / "p"))


class TestAlreadyStored:
    def test_true_when_row_exists(self):
        class FakeConn:
            def execute(self, sql, params):
                assert "raw_ingest" in sql and params == ("sha-1",)
                return self

            def fetchone(self):
                return ("stored",)

        class FakePool:
            def connection(self):
                return _nullcontext(FakeConn())

        assert replay.already_stored(FakePool(), "sha-1") is True

    def test_false_when_no_row(self):
        class FakeConn:
            def execute(self, sql, params):
                return self

            def fetchone(self):
                return None

        class FakePool:
            def connection(self):
                return _nullcontext(FakeConn())

        assert replay.already_stored(FakePool(), "sha-2") is False


class _nullcontext:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *exc):
        return False


class TestCli:
    def test_help_exits_zero_and_names_prog(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            replay.main(["--help"])
        assert excinfo.value.code == 0
        assert "health-replay" in capsys.readouterr().out

    def test_summary_json_goes_to_stdout_progress_to_stderr(
        self, tmp_path, monkeypatch, capsys, fake_pool
    ):
        import health_db.config as config
        from health_db.db import pool as db_pool

        day = tmp_path / "2026" / "01" / "01"
        day.mkdir(parents=True)
        (day / "a.json").write_bytes(json.dumps(metrics_payload()).encode("utf-8"))
        monkeypatch.setattr(replay, "_payload_files", lambda root: list(root.rglob("a.json")))
        # Bypass the real pool resolution: pin replay_dir to the fake seam and
        # make the connectivity probe succeed against the fake pool.
        monkeypatch.setattr(
            replay,
            "replay_dir",
            lambda root, pool=None, *, ingest=None, force=False: replay.ReplayReport(
                root=str(root), mode="dir", force=force, seen=1, processed=1
            ),
        )
        monkeypatch.setattr(db_pool, "check", lambda pool=None, timeout=3.0: True)
        monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
        monkeypatch.delenv("PSQL_URL", raising=False)

        code = replay.main([])

        assert code == 0
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["processed"] == 1
        assert summary["mode"] == "dir"
        assert "health-replay" in captured.err

    def test_db_unreachable_exits_one_without_summary(self, monkeypatch, capsys):
        import health_db.config as config
        from health_db.db import pool as db_pool

        class DownPool:
            def connection(self, timeout=None):
                raise RuntimeError("down")

        monkeypatch.setattr(db_pool, "get_pool", lambda settings=None: DownPool())
        monkeypatch.setattr(db_pool, "check", lambda pool=None, timeout=3.0: False)
        monkeypatch.setattr(config, "ARCHIVE_ROOT", "/tmp/health-replay-nonexistent")
        monkeypatch.delenv("PSQL_URL", raising=False)

        code = replay.main([])

        assert code == 1
        captured = capsys.readouterr()
        assert captured.out == "", "failure must not emit a JSON summary to stdout"
        assert "unreachable" in captured.err

    @pytest.mark.parametrize("flag", ["--force", "--repair"])
    def test_force_flag_reaches_replay_dir(self, flag, tmp_path, monkeypatch, capsys, fake_pool):
        import health_db.config as config
        from health_db.db import pool as db_pool

        seen: dict[str, object] = {}

        def fake_replay_dir(root, pool=None, *, ingest=None, force=False):
            seen["force"] = force
            return replay.ReplayReport(root=str(root), mode="dir", force=force, seen=1)

        monkeypatch.setattr(replay, "replay_dir", fake_replay_dir)
        monkeypatch.setattr(db_pool, "check", lambda pool=None, timeout=3.0: True)
        monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)

        code = replay.main([flag])

        assert code == 0
        assert seen["force"] is True
        assert json.loads(capsys.readouterr().out)["force"] is True
