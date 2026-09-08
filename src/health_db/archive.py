"""Verbatim payload archive — the archive-first iron law (job slice-1-archive).

Every accepted ingest body is written to disk byte-identically BEFORE any
processing outcome (parse verdict, DB write) is decided (harness iron law 2:
"archive-before-anything: a payload is on disk before any processing outcome
is decided; DB failure must never lose a payload").

Ordering responsibilities of this module vs the API layer:

- The API layer calls the archive only AFTER token auth passes — untrusted
  (401) bodies are never archived (spec failure semantics). This module holds
  no auth knowledge and archives exactly the bytes it is given.
- ``store()`` performs the verbatim write; parse/validation outcomes belong
  to later pipeline stages, so nothing here can veto an archive.

Layout (decomposition contract): ``<root>/YYYY/MM/DD/HHMMSS-<sha8>.json``
where ``<root>`` is ``config.ARCHIVE_ROOT`` (default ``data/archive``) and
the date directory uses KST — fixed UTC+9, no DST — mirroring the database's
``day_kst`` semantics; system-local time is never consulted. Bodies destined
for quarantine go to ``<root>/quarantine/`` instead (same naming), so the
dated tree contains only payloads accepted for processing.

Durability: writes are atomic — bytes go to a hidden temp file in the same
directory, are flushed + fsync'd, then ``os.replace``'d into place (and the
directory entry fsync'd best-effort). Any failure unlinks the temp file, so
a partial file can never appear at an archive path.

Duplicate index (slice 1 has no database):

- ``sha256_of(body)`` / ``seen(sha)`` / ``mark_seen(sha)`` — in-memory,
  thread-safe index of stored content hashes.
- ``store()`` is idempotent: a body whose sha256 is already archived returns
  the existing path instead of writing a second copy ("no reprocessing"),
  so re-running ingest or replay is always a no-op on the archive tree.
- ``find_by_sha256(sha)`` checks the in-memory index, then (incrementally)
  scans the dated tree for the payload — a fresh instance (process restart)
  rediscovers payloads archived by a previous run. Quarantined files are
  excluded from that scan: quarantined content was never accepted, so it
  never satisfies a duplicate check and a re-post must be re-quarantined.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import health_db.config as config

# Fixed KST offset (UTC+9, no DST) — parity with the day_kst generated-column
# semantics. Never use a named IANA zone or system-local time here.
KST = timezone(timedelta(hours=9), name="KST")

_QUARANTINE_DIR = "quarantine"


def _now_kst() -> datetime:
    """Current KST wall time (module-level so tests can freeze it)."""
    return datetime.now(KST)


class ArchiveStore:
    """Verbatim, atomic, idempotent payload archive under a dated tree."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else None
        self._lock = threading.Lock()
        # sha256 -> archived Path (None when marked without a known path).
        self._index: dict[str, Path | None] = {}
        # Files already considered by a disk scan (never re-read).
        self._scanned: set[Path] = set()

    @property
    def root(self) -> Path:
        """Archive root; follows ``config.ARCHIVE_ROOT`` live unless bound."""
        return self._root if self._root is not None else Path(config.ARCHIVE_ROOT)

    @staticmethod
    def sha256_of(body: bytes) -> str:
        """Hex sha256 of the exact bytes received."""
        return hashlib.sha256(body).hexdigest()

    def seen(self, sha: str) -> bool:
        """True when ``sha`` is in the in-memory index of stored payloads."""
        with self._lock:
            return sha in self._index

    def mark_seen(self, sha: str) -> None:
        """Register ``sha`` as seen without a known on-disk path."""
        with self._lock:
            self._index.setdefault(sha, None)

    def find_by_sha256(self, sha: str) -> Path | None:
        """Locate an archived payload by full sha256, or ``None``.

        In-memory index first; falls back to an incremental scan of the
        dated tree (restart resilience). Quarantine files never match.
        """
        with self._lock:
            path = self._index.get(sha)
        if path is not None and path.is_file():
            return path
        return self._scan_for(sha)

    def store(self, body: bytes, *, quarantine: bool = False) -> Path:
        """Write ``body`` byte-identically to the archive tree; return path.

        Non-quarantine stores are idempotent: an already-archived body
        returns its existing path without rewriting. Quarantine stores are
        never deduped against the stored index and never mark ``seen``.
        """
        sha = self.sha256_of(body)
        if not quarantine:
            existing = self.find_by_sha256(sha)
            if existing is not None:
                return existing
        now = _now_kst()
        if quarantine:
            directory = self.root / _QUARANTINE_DIR
        else:
            directory = self.root / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{now:%H%M%S}-{sha[:8]}"
        path = self._write_atomic(directory, stem, body, sha)
        if not quarantine:
            with self._lock:
                self._index[sha] = path
        return path

    # --- internals ---------------------------------------------------------

    def _write_atomic(self, directory: Path, stem: str, body: bytes, sha: str) -> Path:
        """Atomic temp+rename write; disambiguates same-second name clashes."""
        path = directory / f"{stem}.json"
        counter = 1
        while path.exists():
            if self.sha256_of(path.read_bytes()) == sha:
                return path  # same content already on disk at this name
            counter += 1
            path = directory / f"{stem}-{counter}.json"
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.stem}.", suffix=".tmp")
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
        try:  # best-effort: make the rename itself durable
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        return path

    def _scan_for(self, sha: str) -> Path | None:
        """Incrementally scan the dated tree for ``sha``; index and return it."""
        sha8 = sha[:8]
        root = self.root
        if not root.is_dir():
            return None
        for candidate in sorted(root.rglob("*.json")):
            with self._lock:
                already_scanned = candidate in self._scanned
                self._scanned.add(candidate)
            if already_scanned:
                continue
            if _QUARANTINE_DIR in candidate.relative_to(root).parts:
                continue
            if sha8 not in candidate.stem.split("-"):
                continue
            if self.sha256_of(candidate.read_bytes()) == sha:
                with self._lock:
                    self._index[sha] = candidate
                return candidate
        return None
