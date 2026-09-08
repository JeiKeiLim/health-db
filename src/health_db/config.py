"""Environment-driven settings for health-db (harness: config from env only).

Slice-1 consumers (auth, archive, stats, api) read the module-level live
attributes — ``INGEST_SECRET``, ``INGEST_HEADER``, ``MAX_PAYLOAD_BYTES``,
``ARCHIVE_ROOT``, ``APP_VERSION`` — which track ``Settings.from_env()``
snapshots. Tests override the module attributes directly (see
tests/acceptance/slice_1_ingest.py) or exercise :func:`from_env` via
monkeypatched ``os.environ``.

Secrets are never logged: :class:`Settings.__repr__` redacts ``ingest_secret``
(iron law 1), and no code path in this module emits secret values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from health_db import __version__

# --- Env keys (documented in .env.example) ---
ENV_ARCHIVE_ROOT = "ARCHIVE_ROOT"
ENV_INGEST_SECRET = "INGEST_SECRET"
ENV_INGEST_HEADER = "INGEST_HEADER"
ENV_MAX_PAYLOAD_BYTES = "MAX_PAYLOAD_BYTES"
ENV_APP_VERSION = "APP_VERSION"

ENV_KEYS = (
    ENV_ARCHIVE_ROOT,
    ENV_INGEST_SECRET,
    ENV_INGEST_HEADER,
    ENV_MAX_PAYLOAD_BYTES,
    ENV_APP_VERSION,
)

# --- Defaults (spec §API Endpoints + decomposition job slice-1-config) ---
DEFAULT_ARCHIVE_ROOT: Path = Path("data") / "archive"
DEFAULT_INGEST_HEADER = "X-Ingest-Token"
DEFAULT_MAX_PAYLOAD_BYTES = 67_108_864  # 64 MiB


class ConfigError(ValueError):
    """Raised when env values cannot be parsed into valid settings."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable snapshot of ingest-service configuration."""

    archive_root: Path = DEFAULT_ARCHIVE_ROOT
    ingest_header: str = DEFAULT_INGEST_HEADER
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    app_version: str = __version__
    ingest_secret: str | None = None

    def redacted(self) -> dict[str, object]:
        """Secret-free snapshot dict, safe for logs/stats/error notes."""
        return {
            "archive_root": str(self.archive_root),
            "ingest_header": self.ingest_header,
            "max_payload_bytes": self.max_payload_bytes,
            "app_version": self.app_version,
            "ingest_secret": "***" if self.ingest_secret else None,
        }

    def __repr__(self) -> str:  # iron law 1: never surface the secret
        fields = ", ".join(f"{k}={v!r}" for k, v in self.redacted().items())
        return f"{type(self).__name__}({fields})"


def _read_env(environ: dict[str, str]) -> Settings:
    secret = environ.get(ENV_INGEST_SECRET) or None
    header = environ.get(ENV_INGEST_HEADER, DEFAULT_INGEST_HEADER).strip()
    if not header:
        raise ConfigError(f"{ENV_INGEST_HEADER} is set but empty; expected a header name")
    raw_max = environ.get(ENV_MAX_PAYLOAD_BYTES)
    max_payload = DEFAULT_MAX_PAYLOAD_BYTES if raw_max is None else _parse_int(raw_max)
    raw_root = environ.get(ENV_ARCHIVE_ROOT)
    archive_root = Path(raw_root) if raw_root else DEFAULT_ARCHIVE_ROOT
    app_version = environ.get(ENV_APP_VERSION, __version__).strip() or __version__
    return Settings(
        archive_root=archive_root,
        ingest_header=header,
        max_payload_bytes=max_payload,
        app_version=app_version,
        ingest_secret=secret,
    )


def _parse_int(raw: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(
            f"{ENV_MAX_PAYLOAD_BYTES} must be an integer number of bytes, got {raw!r}"
        ) from exc
    if value <= 0:
        raise ConfigError(f"{ENV_MAX_PAYLOAD_BYTES} must be a positive integer, got {value}")
    return value


def from_env(environ: dict[str, str] | None = None) -> Settings:
    """Build a Settings snapshot from ``os.environ`` (or an injected mapping).

    Passing ``environ`` explicitly keeps this pure and lets unit tests feed a
    dict instead of mutating process state.
    """
    return _read_env(dict(os.environ if environ is None else environ))


def apply(settings: Settings) -> None:
    """Publish a Settings snapshot onto the module-level live attributes."""
    global ARCHIVE_ROOT, INGEST_HEADER, INGEST_SECRET, MAX_PAYLOAD_BYTES, APP_VERSION
    ARCHIVE_ROOT = settings.archive_root
    INGEST_HEADER = settings.ingest_header
    MAX_PAYLOAD_BYTES = settings.max_payload_bytes
    APP_VERSION = settings.app_version
    INGEST_SECRET = settings.ingest_secret


def current() -> Settings:
    """Read the live module-level attributes back into a Settings snapshot."""
    return Settings(
        archive_root=ARCHIVE_ROOT,
        ingest_header=INGEST_HEADER,
        max_payload_bytes=MAX_PAYLOAD_BYTES,
        app_version=APP_VERSION,
        ingest_secret=INGEST_SECRET,
    )


def load() -> Settings:
    """from_env + apply: the entry point used at service startup."""
    settings = from_env()
    apply(settings)
    return settings


# Live attributes, initialized from the process environment at import.
# Downstream modules read these; tests may monkeypatch them per-case.
ARCHIVE_ROOT: Path = DEFAULT_ARCHIVE_ROOT
INGEST_HEADER: str = DEFAULT_INGEST_HEADER
MAX_PAYLOAD_BYTES: int = DEFAULT_MAX_PAYLOAD_BYTES
APP_VERSION: str = __version__
INGEST_SECRET: str | None = None

apply(from_env())
