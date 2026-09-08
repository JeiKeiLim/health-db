"""Unit tests for health_db.archive — verbatim, atomic, idempotent archive.

Covers decomposition job slice-1-archive acceptance: byte-identical archive
round-trip, atomicity (no partial files on simulated mid-write failure),
quarantine path, duplicate detection on a second call — plus the interface
contract (store/sha256_of/seen/mark_seen/find_by_sha256), KST dated layout,
live-config binding, and restart resilience.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest

import health_db.archive as archive_module
import health_db.config as config
from health_db.archive import KST, ArchiveStore

# Frozen "now": 10:00 KST on 2026-09-08 (a true KST datetime, per contract).
FROZEN_NOW = datetime(2026, 9, 8, 10, 0, 0, tzinfo=KST)


@pytest.fixture(autouse=True)
def _freeze_kst_now(monkeypatch):
    monkeypatch.setattr(archive_module, "_now_kst", lambda: FROZEN_NOW)


@pytest.fixture()
def store(tmp_archive_root):
    return ArchiveStore(root=tmp_archive_root)


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def make_body(n: int = 1) -> bytes:
    point = {"qty": n, "date": "2026-09-08 09:00:00 +0900"}
    payload = {
        "data": {
            "metrics": [{"name": "step_count", "units": "count", "data": [point]}],
            "workouts": [],
        }
    }
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# Byte-identical round-trip + layout
# ---------------------------------------------------------------------------


class TestStoreRoundTrip:
    def test_store_returns_path_and_bytes_are_identical(self, store, tmp_archive_root):
        body = make_body()
        path = store.store(body)
        assert path.is_file()
        assert path.read_bytes() == body, "archive must be byte-identical"

    def test_layout_is_root_YYYY_MM_DD(self, store, tmp_archive_root):
        path = store.store(make_body())
        rel = path.relative_to(tmp_archive_root)
        assert len(rel.parts) == 4, "YYYY/MM/DD/filename under archive root"
        assert rel.parts[:3] == ("2026", "09", "08")
        assert rel.parts[0] != "quarantine"

    def test_filename_is_HHMMSS_sha8_json(self, store, tmp_archive_root):
        body = make_body(7)
        path = store.store(body)
        expected_stem = f"100000-{sha(body)[:8]}"
        assert path.name == f"{expected_stem}.json"
        assert path.parent == tmp_archive_root / "2026" / "09" / "08"

    def test_uses_live_config_archive_root(self, monkeypatch, tmp_archive_root):
        monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_archive_root)
        store = ArchiveStore()
        body = make_body()
        path = store.store(body)
        assert path.relative_to(tmp_archive_root).parts[:3] == ("2026", "09", "08")

    def test_sha256_of_matches_hashlib(self, store):
        body = make_body(3)
        assert store.sha256_of(body) == hashlib.sha256(body).hexdigest()
        assert store.sha256_of(b"") == hashlib.sha256(b"").hexdigest()

    def test_kst_offset_used_not_utc_or_local(self, store, tmp_archive_root):
        """Archive paths use the KST wall clock (+9h fixed). The frozen
        ``_now_kst`` contract returns a true KST datetime; here the default
        fixture (10:00 KST) must produce the 10:00 filename and 09-08 date —
        never a UTC-shifted or system-local date/time."""
        path = store.store(make_body())
        assert path.relative_to(tmp_archive_root).parts[:3] == ("2026", "09", "08")
        assert path.name.startswith("100000-")


# ---------------------------------------------------------------------------
# Atomicity: no partial files on simulated mid-write failure
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_no_partial_file_when_write_fails_midway(self, store, tmp_archive_root, monkeypatch):
        import os

        body = make_body()

        def boom(*args, **kwargs):  # matches os.fdopen(fd, mode) arity
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fdopen", boom)
        with pytest.raises(OSError):
            store.store(body)
        files = [p for p in tmp_archive_root.rglob("*") if p.is_file()]
        assert not files, "no file may appear at an archive path after failure"
        assert store.seen(sha(body)) is False

    def test_no_leftover_temp_when_replace_fails(self, store, tmp_archive_root, monkeypatch):
        import os

        body = make_body()
        real_replace = os.replace

        def flaky(src, dst):
            raise OSError(5, "replace failed")

        monkeypatch.setattr(os, "replace", flaky)
        with pytest.raises(OSError):
            store.store(body)
        monkeypatch.setattr(os, "replace", real_replace)
        leftovers = [p for p in tmp_archive_root.rglob("*") if p.is_file()]
        assert not leftovers, "failed atomic write must clean up its temp file"

    def test_success_after_failure_recovers(self, store, monkeypatch):
        import os

        body = make_body(9)
        monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(OSError(5, "x")))
        with pytest.raises(OSError):
            store.store(body)
        monkeypatch.undo()  # failure was transient; retry must succeed
        path = store.store(body)
        assert path.read_bytes() == body


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


class TestQuarantine:
    def test_quarantine_lands_under_quarantine_dir(self, store, tmp_archive_root):
        body = b"{broken"
        path = store.store(body, quarantine=True)
        assert path.parent == tmp_archive_root / "quarantine"
        assert path.read_bytes() == body

    def test_quarantine_does_not_mark_seen(self, store):
        body = b"{broken"
        store.store(body, quarantine=True)
        assert store.seen(sha(body)) is False

    def test_quarantined_content_never_satisfies_duplicate_check(self, store):
        body = make_body()
        store.store(body, quarantine=True)
        assert store.find_by_sha256(sha(body)) is None, "quarantined content is not stored"
        path = store.store(body)
        assert path.parent.name != "quarantine", "re-post of quarantined body is stored normally"
        assert store.find_by_sha256(sha(body)) == path


# ---------------------------------------------------------------------------
# Duplicate detection (second call)
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    def test_second_store_same_body_returns_same_path_no_new_file(self, store, tmp_archive_root):
        body = make_body()
        first = store.store(body)
        second = store.store(body)
        assert first == second
        archived = [p for p in tmp_archive_root.rglob("*.json") if "quarantine" not in p.parts]
        assert len(archived) == 1, "duplicate store must not write a second copy"

    def test_seen_and_mark_seen(self, store):
        body = make_body()
        s = sha(body)
        assert store.seen(s) is False
        store.mark_seen(s)
        assert store.seen(s) is True
        store.mark_seen(s)  # idempotent
        assert store.seen(s) is True

    def test_find_by_sha256_in_memory(self, store):
        body = make_body(11)
        path = store.store(body)
        assert store.find_by_sha256(sha(body)) == path
        assert store.find_by_sha256("0" * 64) is None

    def test_find_by_sha256_after_restart_scans_disk(self, store, tmp_archive_root):
        body = make_body(12)
        path = store.store(body)
        fresh = ArchiveStore(root=tmp_archive_root)  # empty in-memory index
        assert fresh.find_by_sha256(sha(body)) == path, "restart must rediscover archived payloads"

    def test_find_by_sha256_ignores_non_matching_files(self, store, tmp_archive_root):
        store.store(make_body(13))
        fresh = ArchiveStore(root=tmp_archive_root)
        assert fresh.find_by_sha256(sha(make_body(999))) is None

    def test_concurrent_stores_same_body_one_file(self, store, tmp_archive_root):
        import threading

        body = make_body(14)
        results: list = []

        def worker():
            results.append(store.store(body))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(results)) == 1
        archived = [p for p in tmp_archive_root.rglob("*.json") if "quarantine" not in p.parts]
        assert len(archived) == 1

    def test_distinct_bodies_get_distinct_paths(self, store):
        p1 = store.store(make_body(1))
        p2 = store.store(make_body(2))
        assert p1 != p2
        assert p1.read_bytes() == make_body(1)
        assert p2.read_bytes() == make_body(2)

    def test_same_second_name_collision_disambiguated(self, store, tmp_archive_root):
        """Two different bodies in the same frozen second must not clobber
        each other's archive path."""
        b1 = make_body(21)
        b2 = make_body(22)
        p1 = store.store(b1)
        p2 = store.store(b2)
        assert p1 != p2
        assert p1.read_bytes() == b1 and p2.read_bytes() == b2
        assert p1.parent == p2.parent == tmp_archive_root / "2026" / "09" / "08"
