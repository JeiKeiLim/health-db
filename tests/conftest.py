"""Shared pytest fixtures for health-db tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_archive_root(tmp_path: Path) -> Path:
    """Isolated archive root for tests that touch the filesystem."""
    root = tmp_path / "archive"
    root.mkdir()
    return root


def make_body(payload: dict) -> bytes:
    """Serialize a payload dict the way HAE does (compact JSON, UTF-8)."""
    return json.dumps(payload).encode("utf-8")


def sha256_of(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
