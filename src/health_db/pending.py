"""Out-of-archive pending queue for payloads that hit a DB outage.

Iron law 2 (archive-before-anything): the dated verbatim archive is written
first, so this queue never replaces the archive — it is the *replay
work-list* for payloads whose store stage could not complete. It lives
OUTSIDE ``ARCHIVE_ROOT`` (default ``data/pending/``) so the archive's
content-sha dedup scan never sees pending files and dedup semantics stay
exactly those of the verbatim corpus.

Layout (spec §Failure semantics — DB unavailable → replay re-processes):

- ``pending/<HHMMSS>-<sha8>.json``  — the verbatim payload bytes
- ``pending/<HHMMSS>-<sha8>.meta.json``  — sidecar metadata

The sidecar carries ``{"sha256", "received_at", "automation_name",
"automation_id", "session_id"}`` — everything :func:`health_db.pipeline.
ingest_payload` needs on replay. The replay tool (slice-2-replay) scans
``list_paths()`` and re-runs the pipeline per entry; a successful replay
deletes the pair (pending queue drains to zero — asserted by the acceptance
``archive.pending_count() == 0`` path).

Writes are atomic (temp + rename, fsync best-effort) mirroring
:class:`health_db.archive.ArchiveStore`. The queue is content-idempotent:
queueing the same sha twice keeps the first file untouched.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import health_db.config as config

if TYPE_CHECKING:
    from collections.abc import Iterator

KST = timezone(timedelta(hours=9), name="KST")
SIDECAR_SUFFIX = ".meta.json"


def _now_kst() -> datetime:
    """Current KST wall time (archive.py parity; tests may freeze it)."""
    return datetime.now(KST)


class PendingStore:
    """Atomic, content-idempotent pending-payload queue for DB outages."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else None
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        """Queue root; follows ``config.PENDING_DIR`` live unless bound."""
        return self._root if self._root is not None else Path(config.PENDING_DIR)

    def store(
        self,
        body: bytes,
        *,
        sha256: str,
        received_at: datetime | None = None,
        automation_name: str | None = None,
        automation_id: str | None = None,
        session_id: str | None = None,
    ) -> Path:
        """Write the verbatim payload + metadata sidecar; return payload path.

        Content-idempotent: an already-queued sha256 returns its existing
        payload path (the outage replay work-list never duplicates).
        """
        with self._lock:
            existing = self._find(sha256)
            if existing is not None:
                return existing

            now = received_at if received_at is not None else _now_kst()
            self.root.mkdir(parents=True, exist_ok=True)
            stem = f"{now:%H%M%S}-{sha256[:8]}"
            payload_path = self.root / f"{stem}.json"
            counter = 1
            while payload_path.exists():
                if self.sha256_of(payload_path.read_bytes()) == sha256:
                    return payload_path  # same content already queued at this name
                counter += 1
                payload_path = self.root / f"{stem}-{counter}.json"

            self._write_atomic(
                payload_path,
                body,
            )
            sidecar = payload_path.with_suffix(SIDECAR_SUFFIX)
            self._write_atomic(
                sidecar,
                json.dumps(
                    {
                        "sha256": sha256,
                        "received_at": now.isoformat(),
                        "automation_name": automation_name,
                        "automation_id": automation_id,
                        "session_id": session_id,
                    },
                    sort_keys=True,
                ).encode("utf-8"),
            )
        return payload_path

    def entries(self) -> Iterator[tuple[Path, dict[str, object]]]:
        """Yield ``(payload_path, metadata)`` for every queued payload.

        Sidecar-less payload files (crash between the two atomic writes)
        still yield with a best-effort metadata dict — the bytes are never
        dropped just because their metadata write did not land.
        """
        for path in sorted(self.root.glob("*.json")):
            if path.name.endswith(SIDECAR_SUFFIX):
                continue
            meta_path = path.with_suffix(SIDECAR_SUFFIX)
            meta: dict[str, object] = {}
            if meta_path.is_file():
                try:
                    loaded = json.loads(meta_path.read_bytes())
                    if isinstance(loaded, dict):
                        meta = loaded
                except (ValueError, OSError):
                    meta = {}
            yield path, meta

    def pending_count(self) -> int:
        """Number of queued payloads (sidecars excluded)."""
        return sum(1 for path in self.root.glob("*.json") if not path.name.endswith(SIDECAR_SUFFIX))

    def drain(self, path: Path) -> None:
        """Remove one drained payload + its sidecar (replay success)."""
        path.unlink(missing_ok=True)
        path.with_suffix(SIDECAR_SUFFIX).unlink(missing_ok=True)

    def clear(self) -> None:
        """Empty the queue (tests)."""
        if self.root.is_dir():
            for path in sorted(self.root.glob("*.json")):
                path.unlink(missing_ok=True)

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def sha256_of(body: bytes) -> str:
        """Hex sha256 of the exact bytes (archive parity)."""
        import hashlib

        return hashlib.sha256(body).hexdigest()

    def _find(self, sha256: str) -> Path | None:
        """Locate a queued payload by sha256 (content-verified), or None."""
        if not self.root.is_dir():
            return None
        for path in sorted(self.root.glob("*.json")):
            if path.name.endswith(SIDECAR_SUFFIX):
                continue
            try:
                if self.sha256_of(path.read_bytes()) == sha256:
                    return path
            except OSError:
                continue
        return None

    def _write_atomic(self, path: Path, body: bytes) -> None:
        """Temp + rename write (archive.py parity; rename durability best-effort)."""
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
