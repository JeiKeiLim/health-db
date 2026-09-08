"""Environment-driven settings for health-db (harness: config from env only).

Slice-1 consumers (auth, archive, stats, api) read the module-level live
attributes — ``INGEST_SECRET``, ``INGEST_HEADER``, ``MAX_PAYLOAD_BYTES``,
``ARCHIVE_ROOT``, ``APP_VERSION`` — which track ``Settings.from_env()``
snapshots. Tests override the module attributes directly (see
tests/acceptance/slice_1_ingest.py) or exercise :func:`from_env` via
monkeypatched ``os.environ``.

Normalization policy — applied uniformly to every env var:

- Values are stripped of surrounding whitespace.
- A value that is blank after stripping is treated as absent.
- ``ARCHIVE_ROOT`` / ``APP_VERSION`` / ``INGEST_HEADER``: absent falls back to
  the built-in default.
- ``MAX_PAYLOAD_BYTES``: absent falls back to the default; an unparseable or
  non-positive integer also falls back to the default (with a ``UserWarning``)
  so a malformed limit can never crash ingest startup.
- ``INGEST_SECRET``: key absent → ``None``; key present but blank → ``""``
  (empty string, with a ``UserWarning``).

Fail-closed auth policy (spec §Auth Flow): a missing or empty
``INGEST_SECRET`` never disables authentication. The auth layer (slice-1-auth)
MUST reject every ingest request while the configured secret is ``None`` or
``""`` — a blank secret means "deny all", never "allow all". The warning on a
present-but-blank secret exists so operators notice the misconfiguration; it
never includes the secret value itself.

Secrets are never logged: :class:`Settings.__repr__` redacts ``ingest_secret``
(iron law 1), and no code path in this module emits secret values.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from pathlib import Path

from health_db import __version__

# --- Env keys (documented in .env.example) ---
ENV_ARCHIVE_ROOT = "ARCHIVE_ROOT"
ENV_INGEST_SECRET = "INGEST_SECRET"
ENV_INGEST_HEADER = "INGEST_HEADER"
ENV_MAX_PAYLOAD_BYTES = "MAX_PAYLOAD_BYTES"
ENV_APP_VERSION = "APP_VERSION"
ENV_HOST = "HOST"
ENV_PORT = "PORT"

ENV_KEYS = (
    ENV_ARCHIVE_ROOT,
    ENV_INGEST_SECRET,
    ENV_INGEST_HEADER,
    ENV_MAX_PAYLOAD_BYTES,
    ENV_APP_VERSION,
    ENV_HOST,
    ENV_PORT,
)

# --- Defaults (spec §API Endpoints + decomposition job slice-1-config) ---
DEFAULT_ARCHIVE_ROOT: Path = Path("data") / "archive"
DEFAULT_INGEST_HEADER = "X-Ingest-Token"
DEFAULT_MAX_PAYLOAD_BYTES = 67_108_864  # 64 MiB
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable snapshot of ingest-service configuration."""

    archive_root: Path = DEFAULT_ARCHIVE_ROOT
    ingest_header: str = DEFAULT_INGEST_HEADER
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    app_version: str = __version__
    ingest_secret: str | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    def redacted(self) -> dict[str, object]:
        """Secret-free snapshot dict, safe for logs/stats/error notes."""
        return {
            "archive_root": str(self.archive_root),
            "ingest_header": self.ingest_header,
            "max_payload_bytes": self.max_payload_bytes,
            "app_version": self.app_version,
            "ingest_secret": "***" if self.ingest_secret else None,
            "host": self.host,
            "port": self.port,
        }

    def __repr__(self) -> str:  # iron law 1: never surface the secret
        fields = ", ".join(f"{k}={v!r}" for k, v in self.redacted().items())
        return f"{type(self).__name__}({fields})"


def _stripped(environ: dict[str, str], key: str) -> str | None:
    """Env value with surrounding whitespace removed; blank → None (absent)."""
    raw = environ.get(key)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _max_payload_bytes(environ: dict[str, str]) -> int:
    """Parse MAX_PAYLOAD_BYTES; absent/invalid/non-positive → default (+warning)."""
    raw = _stripped(environ, ENV_MAX_PAYLOAD_BYTES)
    if raw is None:
        return DEFAULT_MAX_PAYLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        warnings.warn(
            f"{ENV_MAX_PAYLOAD_BYTES}={raw!r} is not an integer; "
            f"falling back to the default ({DEFAULT_MAX_PAYLOAD_BYTES} bytes)"
        )
        return DEFAULT_MAX_PAYLOAD_BYTES
    if value <= 0:
        warnings.warn(
            f"{ENV_MAX_PAYLOAD_BYTES} must be a positive integer, got {value}; "
            f"falling back to the default ({DEFAULT_MAX_PAYLOAD_BYTES} bytes)"
        )
        return DEFAULT_MAX_PAYLOAD_BYTES
    return value


def _ingest_secret(environ: dict[str, str]) -> str | None:
    """INGEST_SECRET: unset → None; set-but-blank → "" (fail-closed, see module docs)."""
    raw = environ.get(ENV_INGEST_SECRET)
    if raw is None:
        return None
    secret = raw.strip()
    if not secret:
        warnings.warn(
            f"{ENV_INGEST_SECRET} is set but blank; ingest auth will fail closed — "
            "every request is rejected until a non-empty secret is configured"
        )
    return secret


def _port(environ: dict[str, str]) -> int:
    """Parse PORT; absent → default, invalid/out-of-range → default (+warning)."""
    raw = _stripped(environ, ENV_PORT)
    if raw is None:
        return DEFAULT_PORT
    try:
        value = int(raw)
    except ValueError:
        warnings.warn(
            f"{ENV_PORT}={raw!r} is not an integer; "
            f"falling back to the default ({DEFAULT_PORT})"
        )
        return DEFAULT_PORT
    if not 1 <= value <= 65535:
        warnings.warn(
            f"{ENV_PORT} must be in 1..65535, got {value}; "
            f"falling back to the default ({DEFAULT_PORT})"
        )
        return DEFAULT_PORT
    return value


def _read_env(environ: dict[str, str]) -> Settings:
    archive_root = _stripped(environ, ENV_ARCHIVE_ROOT)
    header = _stripped(environ, ENV_INGEST_HEADER)
    app_version = _stripped(environ, ENV_APP_VERSION)
    host = _stripped(environ, ENV_HOST)
    return Settings(
        archive_root=Path(archive_root) if archive_root else DEFAULT_ARCHIVE_ROOT,
        ingest_header=header or DEFAULT_INGEST_HEADER,
        max_payload_bytes=_max_payload_bytes(environ),
        app_version=app_version or __version__,
        ingest_secret=_ingest_secret(environ),
        host=host or DEFAULT_HOST,
        port=_port(environ),
    )


def from_env(environ: dict[str, str] | None = None) -> Settings:
    """Build a Settings snapshot from ``os.environ`` (or an injected mapping).

    Passing ``environ`` explicitly keeps this pure and lets unit tests feed a
    dict instead of mutating process state. See the module docstring for the
    normalization and fail-closed auth policies.
    """
    return _read_env(dict(os.environ if environ is None else environ))


def apply(settings: Settings) -> None:
    """Publish a Settings snapshot onto the module-level live attributes."""
    global ARCHIVE_ROOT, INGEST_HEADER, INGEST_SECRET, MAX_PAYLOAD_BYTES
    global APP_VERSION, HOST, PORT
    ARCHIVE_ROOT = settings.archive_root
    INGEST_HEADER = settings.ingest_header
    MAX_PAYLOAD_BYTES = settings.max_payload_bytes
    APP_VERSION = settings.app_version
    INGEST_SECRET = settings.ingest_secret
    HOST = settings.host
    PORT = settings.port


def current() -> Settings:
    """Read the live module-level attributes back into a Settings snapshot."""
    return Settings(
        archive_root=ARCHIVE_ROOT,
        ingest_header=INGEST_HEADER,
        max_payload_bytes=MAX_PAYLOAD_BYTES,
        app_version=APP_VERSION,
        ingest_secret=INGEST_SECRET,
        host=HOST,
        port=PORT,
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
HOST: str = DEFAULT_HOST
PORT: int = DEFAULT_PORT

apply(from_env())
