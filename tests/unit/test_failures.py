"""Unit tests for health_db.failures — malformed/oversize failure semantics.

Covers decomposition job slice-1-quarantine acceptance:

- ``b"{broken"`` → 400 + payload quarantined verbatim on disk (iron law 2:
  nothing lost — the 400 response carries a quarantine id that resolves to
  a real file containing the exact received bytes),
- ``{"nope": 1}`` (valid JSON, wrong shape) → 400 + quarantined,
- oversized body against a monkeypatched 1 KiB cap → 413 with NOTHING
  archived anywhere under the archive root,

plus the interface contracts consumed by slice-1-api: ``check_size`` /
``OversizedPayload`` / ``oversized_response`` / ``handle_malformed`` /
``quarantine_id``, the error-note sidecar, and the iron-law ordering (the
quarantine write precedes the response; a failed write propagates instead
of producing a 400 without a payload on disk).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

import health_db.config as config
import health_db.failures as failures_module
from health_db.archive import ArchiveStore
from health_db.failures import (
    HTTP_413,
    OversizedPayload,
    check_size,
    handle_malformed,
    oversized_response,
    quarantine_id,
)
from health_db.payloads import MalformedPayload, parse


def make_store(tmp_archive_root: Path) -> ArchiveStore:
    return ArchiveStore(root=tmp_archive_root)


def malformed_exc(body: bytes) -> MalformedPayload:
    """Run the real parser on ``body`` and return its MalformedPayload.

    Keeps tests honest: the exception handed to ``handle_malformed`` is the
    exact one the API pipeline would see from ``health_db.payloads.parse``.
    """
    with pytest.raises(MalformedPayload) as excinfo:
        parse(body)
    return excinfo.value


def quarantined_files(tmp_archive_root: Path) -> list[Path]:
    quarantine = tmp_archive_root / "quarantine"
    if not quarantine.is_dir():
        return []
    return sorted(quarantine.rglob("*"))


@pytest.fixture(autouse=True)
def _isolated_fallback_store(tmp_archive_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs against the isolated tmp archive root — even the
    fallback-store path (no explicit store) must never write into the repo's
    real ``data/archive`` tree."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_archive_root)
    monkeypatch.setattr(failures_module, "_FALLBACK_STORE", None)


def archived_dated_files(tmp_archive_root: Path) -> list[Path]:
    """Files in the dated tree (everything except the quarantine lane)."""
    return [
        p
        for p in tmp_archive_root.rglob("*")
        if p.is_file() and "quarantine" not in p.relative_to(tmp_archive_root).parts
    ]


def all_archived_files(tmp_archive_root: Path) -> list[Path]:
    return [p for p in tmp_archive_root.rglob("*") if p.is_file()]


# ---------------------------------------------------------------------------
# Malformed JSON → 400 + quarantined verbatim
# ---------------------------------------------------------------------------


class TestMalformedJson400Quarantined:
    def test_broken_json_returns_400_with_quarantine_id(self, tmp_archive_root):
        store = make_store(tmp_archive_root)
        body = b"{broken"
        response, path = handle_malformed(body, MalformedPayload("payload is not valid JSON: x"))
        assert isinstance(response, JSONResponse)
        assert response.status_code == 400
        payload = json.loads(bytes(response.body))
        assert payload["error"] == "malformed"
        assert payload["quarantine"]
        assert payload["quarantine"] == quarantine_id(path, store.root)

    def test_broken_json_payload_is_quarantined_verbatim_on_disk(self, tmp_archive_root):
        body = b"{broken"
        _, path = handle_malformed(body, MalformedPayload("bad json"))
        assert path.parent == tmp_archive_root / "quarantine"
        assert path.read_bytes() == body, "quarantined payload must be byte-identical"

    def test_wrong_shape_valid_json_returns_400_and_quarantined(self, tmp_archive_root):
        body = json.dumps({"nope": 1}).encode()
        response, path = handle_malformed(body, malformed_exc(body))
        assert response.status_code == 400
        assert path.parent == tmp_archive_root / "quarantine"
        assert path.read_bytes() == body

    def test_quarantine_id_resolves_to_the_quarantine_file(self, tmp_archive_root):
        store = make_store(tmp_archive_root)
        response, path = handle_malformed(b"{broken", MalformedPayload("bad json"), store=store)
        qid = json.loads(bytes(response.body))["quarantine"]
        assert qid.startswith("quarantine/")
        resolved = tmp_archive_root / qid
        assert resolved == path, "the reported id must resolve to the quarantined file"

    def test_empty_body_is_malformed_and_quarantined(self, tmp_archive_root):
        body = b""
        response, path = handle_malformed(body, malformed_exc(body))
        assert response.status_code == 400
        assert path.read_bytes() == b""
        assert path.parent == tmp_archive_root / "quarantine"

    def test_repost_of_bad_bytes_is_requarantined_not_deduped(self, tmp_archive_root):
        """A re-post of the same bad bytes must never satisfy the duplicate
        check (quarantine lane is outside the dedup index) and must never
        enter the dated tree. Within the same clock second the store's
        content disambiguation yields a single quarantine file, whose id the
        second 400 reports."""
        store = make_store(tmp_archive_root)
        body = b"{broken"
        first, _ = handle_malformed(body, MalformedPayload("x"), store=store)
        second, second_path = handle_malformed(body, MalformedPayload("x"), store=store)
        q1 = json.loads(bytes(first.body))["quarantine"]
        q2 = json.loads(bytes(second.body))["quarantine"]
        assert q1 == q2, "same bad bytes in the same second share one quarantine file"
        assert second_path == tmp_archive_root / q1
        files = [p for p in quarantined_files(tmp_archive_root) if p.suffix == ".json"]
        assert files == [second_path], "exactly one copy of the bad bytes"
        assert archived_dated_files(tmp_archive_root) == []

    def test_quarantined_content_never_enters_dated_dedup_tree(self, tmp_archive_root):
        store = make_store(tmp_archive_root)
        body = b"{broken"
        handle_malformed(body, MalformedPayload("x"), store=store)
        assert store.find_by_sha256(store.sha256_of(body)) is None
        assert archived_dated_files(tmp_archive_root) == []

    def test_response_body_carries_actionable_reason(self, tmp_archive_root):
        body = b"{broken"
        response, _ = handle_malformed(body, malformed_exc(body))
        payload = json.loads(bytes(response.body))
        assert "not valid JSON" in payload["reason"]

    def test_uses_live_config_archive_root(self):
        """Without an explicit store, the fallback store follows config.ARCHIVE_ROOT."""
        _, path = handle_malformed(b"{broken", MalformedPayload("x"))
        assert path.parent == config.ARCHIVE_ROOT / "quarantine"


# ---------------------------------------------------------------------------
# Error note sidecar
# ---------------------------------------------------------------------------


class TestErrorNote:
    def test_error_note_written_beside_payload(self, tmp_archive_root):
        _, path = handle_malformed(b"{broken", MalformedPayload("payload is not valid JSON: oops"))
        note = path.with_name(path.stem + ".error.txt")
        assert note.is_file()
        assert "not valid JSON" in note.read_text(encoding="utf-8")

    def test_note_failure_does_not_lose_payload(self, tmp_archive_root, monkeypatch):
        """The note is auxiliary: if writing it fails, the payload stays
        quarantined and the 400 is still produced."""
        body = b"{broken"

        def boom(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(Path, "write_text", boom)
        response, path = handle_malformed(body, MalformedPayload("x"))
        assert response.status_code == 400
        assert path.read_bytes() == body, "payload must be quarantined even if the note fails"


# ---------------------------------------------------------------------------
# Iron-law ordering: quarantine write precedes the response
# ---------------------------------------------------------------------------


class TestArchiveFirstOrdering:
    def test_failed_quarantine_write_propagates_no_400_without_payload(
        self, tmp_archive_root, monkeypatch
    ):
        """If the quarantine write itself fails, the exception must propagate —
        never a 400 claiming a quarantine id for a payload that is not on disk."""
        store = make_store(tmp_archive_root)
        body = b"{broken"

        def failing_store(inner: bytes, *, quarantine: bool = False) -> Path:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(store, "store", failing_store)
        with pytest.raises(OSError):
            handle_malformed(body, MalformedPayload("x"), store=store)
        assert all_archived_files(tmp_archive_root) == []

    def test_response_built_only_after_payload_is_on_disk(self, tmp_archive_root):
        """Behavioral proof of ordering: after handle_malformed returns, the
        payload file exists and the response references it — archive first,
        response second."""
        response, path = handle_malformed(b"{broken", MalformedPayload("x"))
        assert path.is_file(), "payload on disk"
        assert json.loads(bytes(response.body))["quarantine"] == quarantine_id(
            path, path.parent.parent
        )


# ---------------------------------------------------------------------------
# Oversized body → 413, never archived
# ---------------------------------------------------------------------------


class TestOversized413NotArchived:
    def test_2mib_against_1kib_cap_raises_oversized(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_PAYLOAD_BYTES", 1024)
        big = b"x" * (2 * 1024 * 1024)
        with pytest.raises(OversizedPayload) as excinfo:
            check_size(len(big))
        assert excinfo.value.length == len(big)
        assert excinfo.value.limit == 1024

    def test_under_cap_passes(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_PAYLOAD_BYTES", 1024)
        assert check_size(1024) is None, "exactly at the cap is allowed (strictly greater fails)"
        assert check_size(0) is None

    def test_oversized_nothing_archived(self, tmp_archive_root, monkeypatch):
        store = make_store(tmp_archive_root)
        monkeypatch.setattr(config, "MAX_PAYLOAD_BYTES", 1024)
        big = b"x" * (2 * 1024 * 1024)
        with pytest.raises(OversizedPayload):
            check_size(len(big))
        # The API layer never calls the store on this path — nothing on disk.
        assert all_archived_files(tmp_archive_root) == []
        assert store.seen(store.sha256_of(big)) is False

    def test_oversized_response_is_413_with_limit_and_length(self):
        response = oversized_response(OversizedPayload(length=2048, limit=1024))
        assert response.status_code == HTTP_413 == 413
        payload = json.loads(bytes(response.body))
        assert payload["error"] == "payload_too_large"
        assert payload["limit_bytes"] == 1024
        assert payload["received_bytes"] == 2048

    def test_default_limit_is_64_mib(self):
        assert config.MAX_PAYLOAD_BYTES == 67_108_864


# ---------------------------------------------------------------------------
# No secret leakage (iron law 1) — failure responses carry no token material
# ---------------------------------------------------------------------------


class TestNoSecretLeakage:
    def test_400_body_contains_no_secret(self, tmp_archive_root, monkeypatch):
        monkeypatch.setattr(config, "INGEST_SECRET", "super-secret-token-value")
        response, _ = handle_malformed(b"{broken", MalformedPayload("x"))
        text = bytes(response.body).decode()
        assert "super-secret-token-value" not in text

    def test_413_body_contains_no_secret(self, monkeypatch):
        monkeypatch.setattr(config, "INGEST_SECRET", "super-secret-token-value")
        text = bytes(oversized_response(OversizedPayload(2048, 1024)).body).decode()
        assert "super-secret-token-value" not in text

    def test_quarantine_error_note_contains_no_secret(self, tmp_archive_root, monkeypatch):
        monkeypatch.setattr(config, "INGEST_SECRET", "super-secret-token-value")
        _, path = handle_malformed(b"{broken", MalformedPayload("x"))
        assert (
            "super-secret-token-value" not in path.with_name(path.stem + ".error.txt").read_text()
        )


# ---------------------------------------------------------------------------
# quarantine_id helper
# ---------------------------------------------------------------------------


class TestQuarantineId:
    def test_relative_posix_id(self, tmp_archive_root):
        path = tmp_archive_root / "quarantine" / "100000-abcd1234.json"
        assert quarantine_id(path, tmp_archive_root) == "quarantine/100000-abcd1234.json"

    def test_path_outside_root_falls_back_to_name(self, tmp_archive_root):
        outside = Path("/tmp/elsewhere/file.json")
        assert quarantine_id(outside, tmp_archive_root) == "file.json"
