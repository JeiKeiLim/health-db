"""Unit tests for health_db.config — defaults, env overrides, secret redaction.

Covers decomposition job slice-1-config acceptance: monkeypatch-driven env
overrides, default values, validation, the uniform strip/blank-normalization
policy, and the no-secret-leak rule.
"""

from __future__ import annotations

import dataclasses
import warnings
from pathlib import Path

import pytest

from health_db import __version__
from health_db import config as cfg
from health_db.config import Settings, from_env

# ---------------------------------------------------------------------------
# Defaults (no relevant env vars set)
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_all_defaults_when_env_empty(self, monkeypatch):
        for key in cfg.ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        s = from_env({})
        assert s.archive_root == Path("data") / "archive"
        assert s.ingest_header == "X-Ingest-Token"
        assert s.max_payload_bytes == 67_108_864
        assert s.app_version == __version__
        assert s.ingest_secret is None
        assert s.host == "0.0.0.0"
        assert s.port == 8000

    def test_process_env_without_vars_yields_defaults(self, monkeypatch):
        for key in cfg.ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        s = from_env()
        assert s.max_payload_bytes == 67_108_864
        assert s.ingest_secret is None

    def test_explicit_empty_environ_dict(self):
        s = from_env({})
        assert s.archive_root == Path("data/archive")
        assert s.ingest_header == "X-Ingest-Token"


# ---------------------------------------------------------------------------
# Env overrides (monkeypatch pattern used by downstream slices)
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    def test_every_field_overridable_from_env(self):
        s = from_env(
            {
                "ARCHIVE_ROOT": "/tmp/custom-archive",
                "INGEST_SECRET": "s3cret-value",
                "INGEST_HEADER": "X-Custom-Token",
                "MAX_PAYLOAD_BYTES": "1024",
                "APP_VERSION": "9.9.9-test",
                "HOST": "127.0.0.1",
                "PORT": "45919",
            }
        )
        assert s.archive_root == Path("/tmp/custom-archive")
        assert s.ingest_secret == "s3cret-value"
        assert s.ingest_header == "X-Custom-Token"
        assert s.max_payload_bytes == 1024
        assert s.app_version == "9.9.9-test"
        assert s.host == "127.0.0.1"
        assert s.port == 45919

    def test_monkeypatch_os_environ_is_respected(self, monkeypatch):
        monkeypatch.setenv("INGEST_SECRET", "monkey-value")
        monkeypatch.setenv("INGEST_HEADER", "X-Monkey-Header")
        monkeypatch.setenv("MAX_PAYLOAD_BYTES", "2048")
        monkeypatch.setenv("ARCHIVE_ROOT", "data/other-archive")
        s = from_env()
        assert s.ingest_secret == "monkey-value"
        assert s.ingest_header == "X-Monkey-Header"
        assert s.max_payload_bytes == 2048
        assert s.archive_root == Path("data/other-archive")

    def test_empty_ingest_secret_is_blank_string_fail_closed(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = from_env({"INGEST_SECRET": ""})
        assert s.ingest_secret == ""

    def test_missing_ingest_secret_is_none_not_error(self):
        assert from_env({}).ingest_secret is None


# ---------------------------------------------------------------------------
# Validation / parsing
# ---------------------------------------------------------------------------


class TestValidation:
    def test_empty_ingest_header_falls_back_to_default(self):
        s = from_env({"INGEST_HEADER": "   "})
        assert s.ingest_header == "X-Ingest-Token"

    def test_whitespace_ingest_header_is_stripped(self):
        s = from_env({"INGEST_HEADER": "  X-Custom-Token  "})
        assert s.ingest_header == "X-Custom-Token"

    def test_whitespace_archive_root_is_stripped(self):
        s = from_env({"ARCHIVE_ROOT": "  data/other-archive  "})
        assert s.archive_root == Path("data/other-archive")

    def test_whitespace_app_version_is_stripped(self):
        s = from_env({"APP_VERSION": "  9.9.9-test  "})
        assert s.app_version == "9.9.9-test"

    def test_blank_app_version_falls_back_to_package_version(self):
        s = from_env({"APP_VERSION": "   "})
        assert s.app_version == __version__

    def test_empty_archive_root_falls_back_to_default(self):
        s = from_env({"ARCHIVE_ROOT": ""})
        assert s.archive_root == Path("data/archive")

    def test_invalid_max_payload_bytes_falls_back_to_default_with_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            s = from_env({"MAX_PAYLOAD_BYTES": "64MiB"})
        assert s.max_payload_bytes == cfg.DEFAULT_MAX_PAYLOAD_BYTES
        assert any("MAX_PAYLOAD_BYTES" in str(w.message) for w in caught)

    def test_zero_and_negative_max_payload_bytes_fall_back_with_warning(self):
        for bad in ("0", "-5"):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                s = from_env({"MAX_PAYLOAD_BYTES": bad})
            assert s.max_payload_bytes == cfg.DEFAULT_MAX_PAYLOAD_BYTES
            assert any("positive" in str(w.message) for w in caught)

    def test_blank_max_payload_bytes_falls_back_to_default(self):
        s = from_env({"MAX_PAYLOAD_BYTES": "   "})
        assert s.max_payload_bytes == cfg.DEFAULT_MAX_PAYLOAD_BYTES

    def test_invalid_port_falls_back_to_default_with_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            s = from_env({"PORT": "not-a-port"})
        assert s.port == cfg.DEFAULT_PORT
        assert any("PORT" in str(w.message) for w in caught)

    def test_out_of_range_port_falls_back_with_warning(self):
        for bad in ("0", "65536", "-1"):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                s = from_env({"PORT": bad})
            assert s.port == cfg.DEFAULT_PORT
            assert any("1..65535" in str(w.message) for w in caught)

    def test_blank_port_falls_back_to_default(self):
        s = from_env({"PORT": "   "})
        assert s.port == cfg.DEFAULT_PORT

    def test_blank_host_falls_back_to_default(self):
        s = from_env({"HOST": "   "})
        assert s.host == cfg.DEFAULT_HOST

    def test_whitespace_ingest_secret_is_stripped(self):
        s = from_env({"INGEST_SECRET": "  s3cret-value  "})
        assert s.ingest_secret == "s3cret-value"

    def test_blank_ingest_secret_is_empty_string_with_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            s = from_env({"INGEST_SECRET": "   "})
        assert s.ingest_secret == ""
        assert any("fail closed" in str(w.message) for w in caught)

    def test_blank_ingest_secret_warning_never_contains_value(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from_env({"INGEST_SECRET": "   "})
        assert all("s3cret" not in str(w.message) for w in caught)

    def test_no_warning_on_fully_absent_vars(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            s = from_env({})
        assert s.ingest_secret is None
        assert s.max_payload_bytes == cfg.DEFAULT_MAX_PAYLOAD_BYTES


# ---------------------------------------------------------------------------
# Live module attributes: apply / current / load round-trip
# ---------------------------------------------------------------------------


class TestApplyCurrentRoundTrip:
    def test_apply_publishes_live_attrs(self, monkeypatch):
        settings = Settings(
            archive_root=Path("data/tmp-archive"),
            ingest_header="X-Tmp-Header",
            max_payload_bytes=4096,
            app_version="0.0-test",
            ingest_secret="tmp-secret",
            host="127.0.0.1",
            port=45919,
        )
        monkeypatch.setattr(cfg, "ARCHIVE_ROOT", cfg.DEFAULT_ARCHIVE_ROOT)
        monkeypatch.setattr(cfg, "INGEST_HEADER", cfg.DEFAULT_INGEST_HEADER)
        monkeypatch.setattr(cfg, "MAX_PAYLOAD_BYTES", cfg.DEFAULT_MAX_PAYLOAD_BYTES)
        monkeypatch.setattr(cfg, "APP_VERSION", __version__)
        monkeypatch.setattr(cfg, "INGEST_SECRET", None)
        monkeypatch.setattr(cfg, "HOST", cfg.DEFAULT_HOST)
        monkeypatch.setattr(cfg, "PORT", cfg.DEFAULT_PORT)
        cfg.apply(settings)
        assert cfg.ARCHIVE_ROOT == Path("data/tmp-archive")
        assert cfg.INGEST_HEADER == "X-Tmp-Header"
        assert cfg.MAX_PAYLOAD_BYTES == 4096
        assert cfg.APP_VERSION == "0.0-test"
        assert cfg.INGEST_SECRET == "tmp-secret"
        assert cfg.HOST == "127.0.0.1"
        assert cfg.PORT == 45919
        assert cfg.current() == settings

    def test_current_reflects_live_attrs(self):
        s = cfg.current()
        assert s.archive_root == cfg.ARCHIVE_ROOT
        assert s.ingest_header == cfg.INGEST_HEADER
        assert s.max_payload_bytes == cfg.MAX_PAYLOAD_BYTES
        assert s.ingest_secret == cfg.INGEST_SECRET


# ---------------------------------------------------------------------------
# Frozen dataclass + redaction (iron law 1)
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    def test_settings_frozen(self):
        s = from_env({"INGEST_SECRET": "frozen-secret"})
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.ingest_secret = "mutated"

    def test_repr_never_contains_secret(self):
        s = from_env({"INGEST_SECRET": "super-secret-value-42"})
        assert "super-secret-value-42" not in repr(s)
        assert "ingest_secret='***'" in repr(s)

    def test_redacted_masks_secret_but_keeps_operational_values(self):
        s = from_env(
            {
                "INGEST_SECRET": "leak-me-not",
                "INGEST_HEADER": "X-Some-Header",
                "MAX_PAYLOAD_BYTES": "2048",
            }
        )
        d = s.redacted()
        assert d["ingest_secret"] == "***"
        assert d["ingest_header"] == "X-Some-Header"
        assert d["max_payload_bytes"] == 2048
        assert "leak-me-not" not in repr(d)

    def test_str_uses_redacted_repr(self):
        s = from_env({"INGEST_SECRET": "another-secret"})
        assert "another-secret" not in str(s)
