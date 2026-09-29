"""Unit tests — scripts/probe_cleanup.sql: spec §5 ops-script contract.

No docker: the deliverable is a psql script (data cleanup, NOT a migration),
so unit coverage asserts the script's content shape — transactionality,
target tables/order, probe names, ordering doc — rather than executing SQL.
The executable behavior (probe present → cleanup → gone, re-run no-op) is
covered under requires_pg by TestScenario8ProbeCleanup in
tests/acceptance/mcp_analysis_parity.py running the same file verbatim.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "probe_cleanup.sql"


def _normalized() -> str:
    return " ".join(SCRIPT.read_text(encoding="utf-8").split())


def test_script_exists():
    assert SCRIPT.is_file(), f"missing ops script: {SCRIPT}"


def test_transactional_block():
    normalized = _normalized()
    assert "BEGIN;" in normalized
    assert "COMMIT;" in normalized
    assert normalized.index("BEGIN;") < normalized.index("DELETE")
    assert normalized.rindex("COMMIT;") > normalized.rindex("DELETE")


def test_all_three_tables_deleted_in_dependency_order():
    normalized = _normalized()
    rollups = normalized.index("DELETE FROM daily_rollups")
    raw = normalized.index("DELETE FROM raw_samples")
    dim = normalized.index("DELETE FROM metric_dim")
    assert rollups < raw < dim  # catalog (referenced by children) deleted LAST


def test_probe_names_present():
    normalized = _normalized()
    assert "__probe_metric" in normalized
    assert "__probe_metric2" in normalized


def test_deletes_are_subquery_scoped():
    """Children delete via metric_id subquery; catalog deletes by name."""
    normalized = _normalized()
    assert normalized.count("SELECT metric_id FROM metric_dim WHERE name IN") == 2


def test_no_alter_drop_additive_only_discipline():
    """Not a migration: must contain no schema-touching statements."""
    lowered = _normalized().lower()
    for forbidden in ("alter ", "drop table", "drop index", "truncate", "update "):
        assert forbidden not in lowered, f"schema-touching statement found: {forbidden}"


def test_header_documents_replay_ordering():
    """MUST run AFTER backfill replay — deploy sequence step 3 (anti-scenario 8)."""
    normalized = _normalized()
    assert "after" in normalized
    assert "health-replay --dir data/archive --force" in normalized
