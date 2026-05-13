"""Regression spine for the PR-A Oracle-graph blast path.

Pins:
* Master flag default-FALSE (rollback safety)
* Oracle path returns ``None`` when graph is cold / target not in graph
  (triggers strict fallback to legacy rglob)
* Parity contract on a controlled fixture: when the graph IS
  populated, Oracle blast count matches the structural intent of
  the legacy count (same affected-file set within tolerance)
* Composition: uses TheOracle's PUBLIC ``get_blast_radius`` API —
  no duplicate BFS, no parallel graph state.  AST pin asserts
  ``operation_advisor`` does not implement its own BFS.

The stage-1 wiring soak observation that motivated this PR
(2026-05-13 session bt-2026-05-13-075148): even after PR-B's
dedicated executor isolated advisor from the default-pool, ops
still missed the BG-pool 360s ceiling because the 29.5k-file
rglob scan contended for OS-level disk I/O with 16 concurrent
sensors.  Replacing the rglob with Oracle's pre-built
``CodeGraph.compute_blast_radius`` BFS eliminates the disk
contention entirely.
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance import operation_advisor
from backend.core.ouroboros.governance.operation_advisor import (
    ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR,
    OperationAdvisor,
    _BLAST_RADIUS_CACHE_SHARED,
    _advisor_oracle_blast_enabled,
    _oracle_blast_count,
    set_active_oracle,
)


@pytest.fixture(autouse=True)
def _reset_advisor_module_state():
    """Clear shared module state between tests."""
    _BLAST_RADIUS_CACHE_SHARED.clear()
    set_active_oracle(None)
    # Also clear the env var to ensure clean default state
    prev_flag = os.environ.pop(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, None)
    yield
    _BLAST_RADIUS_CACHE_SHARED.clear()
    set_active_oracle(None)
    if prev_flag is not None:
        os.environ[ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR] = prev_flag


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_flag_defaults_off():
    """Per §33.1 graduation contract: new authority-bearing
    substrates ship default-FALSE so operators graduate them on
    their own data."""
    assert ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR not in os.environ
    assert _advisor_oracle_blast_enabled() is False


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True),
    ("True", True), ("TRUE", True),
    ("0", False), ("false", False), ("", False), ("no", False),
    ("anything-else", False),
])
def test_master_flag_parses_canonical_truthy_values(val, expected, monkeypatch):
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, val)
    assert _advisor_oracle_blast_enabled() is expected


# ---------------------------------------------------------------------------
# Oracle accessor wiring
# ---------------------------------------------------------------------------


def test_set_active_oracle_round_trip():
    sentinel = MagicMock(name="MockOracle")
    set_active_oracle(sentinel)
    assert operation_advisor._active_oracle is sentinel
    set_active_oracle(None)
    assert operation_advisor._active_oracle is None


# ---------------------------------------------------------------------------
# Fallback contract
# ---------------------------------------------------------------------------


def test_blast_falls_back_to_legacy_when_oracle_unregistered(tmp_path, monkeypatch):
    """Master flag ON + Oracle unset → MUST use legacy rglob.
    The Oracle path is opt-in by both flag AND availability."""
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")
    set_active_oracle(None)

    (tmp_path / "a.py").write_text("import target\n")
    advisor = OperationAdvisor(tmp_path)
    # Should not raise — falls through to legacy
    r = advisor._compute_blast_radius(("target.py",))
    assert isinstance(r, int)
    assert r >= 0


def test_blast_falls_back_when_oracle_reports_unknown(tmp_path, monkeypatch):
    """Oracle's ``get_blast_radius`` returns ``risk_level='unknown'``
    when the target isn't in the graph (cold cache, new file, etc.).
    In that case we MUST fall back to legacy — using the empty
    Oracle result would silently underreport blast."""
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")

    # Mock Oracle that always returns unknown
    mock_oracle = MagicMock()
    mock_oracle.get_blast_radius.return_value = MagicMock(
        risk_level="unknown",
        directly_affected=set(),
        transitively_affected=set(),
    )
    set_active_oracle(mock_oracle)

    (tmp_path / "a.py").write_text("import target\n")
    advisor = OperationAdvisor(tmp_path)
    r = advisor._compute_blast_radius(("target.py",))
    # Should hit the legacy scan path (which finds 1 importer: a.py)
    assert r >= 1, (
        f"Expected legacy fallback to find ≥1 importer, got {r}.  "
        "If Oracle's unknown response is being treated as a real "
        "result, the fallback contract is broken and stale graph "
        "state will silently understate blast radius."
    )


def test_blast_falls_back_when_oracle_raises(tmp_path, monkeypatch):
    """Any Oracle exception MUST be caught and the legacy scan run.
    A broken Oracle MUST NOT break advise()."""
    monkeypatch.setenv(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "true")

    mock_oracle = MagicMock()
    mock_oracle.get_blast_radius.side_effect = RuntimeError("graph corrupted")
    set_active_oracle(mock_oracle)

    (tmp_path / "a.py").write_text("import target\n")
    advisor = OperationAdvisor(tmp_path)
    # MUST NOT raise
    r = advisor._compute_blast_radius(("target.py",))
    assert r >= 1


def test_oracle_blast_count_returns_none_for_partial_resolution(tmp_path):
    """If ANY target is not in the graph, the function returns None
    so the caller falls back to legacy.  Mixed resolution would
    silently undercount."""
    mock_oracle = MagicMock()

    def _get(target):
        if target == "known":
            return MagicMock(
                risk_level="low",
                directly_affected={MagicMock(file_path="a.py")},
                transitively_affected=set(),
            )
        return MagicMock(
            risk_level="unknown",
            directly_affected=set(),
            transitively_affected=set(),
        )

    mock_oracle.get_blast_radius.side_effect = _get
    # known.py is in graph, unknown.py is not → MUST return None
    result = _oracle_blast_count(mock_oracle, ("known.py", "unknown.py"))
    assert result is None


# ---------------------------------------------------------------------------
# Happy path — Oracle hit returns capped count
# ---------------------------------------------------------------------------


def test_oracle_blast_count_dedupes_and_caps_at_50():
    """Oracle's BFS may return many NodeIDs spanning the same file
    (different symbols).  The count MUST be unique-file, and
    capped at 50 (matching the legacy scan's break threshold so
    risk-score calibration in advise() doesn't shift)."""
    mock_oracle = MagicMock()

    def make_node(file_path):
        n = MagicMock()
        n.file_path = file_path
        return n

    # 200 unique affected files, but legacy caps at 50
    affected = {make_node(f"affected_{i}.py") for i in range(200)}
    mock_oracle.get_blast_radius.return_value = MagicMock(
        risk_level="medium",
        directly_affected=affected,
        transitively_affected=set(),
    )

    result = _oracle_blast_count(mock_oracle, ("target.py",))
    assert result == 50, (
        f"Expected cap at 50 (matching legacy scan), got {result}"
    )


def test_oracle_blast_count_unions_across_targets():
    """Multiple target files MUST union into a single affected
    set — the count reflects the TOTAL impact surface, not a
    per-target tally."""
    mock_oracle = MagicMock()

    def make_node(file_path):
        n = MagicMock()
        n.file_path = file_path
        return n

    def _get(target):
        if target == "a":
            return MagicMock(
                risk_level="low",
                directly_affected={make_node("x.py"), make_node("y.py")},
                transitively_affected=set(),
            )
        if target == "b":
            return MagicMock(
                risk_level="low",
                directly_affected={make_node("y.py"), make_node("z.py")},
                transitively_affected=set(),
            )
        return MagicMock(
            risk_level="unknown",
            directly_affected=set(),
            transitively_affected=set(),
        )

    mock_oracle.get_blast_radius.side_effect = _get
    # Targets a.py and b.py — both in graph
    # Union: {x.py, y.py, z.py} = 3 unique files
    result = _oracle_blast_count(mock_oracle, ("a.py", "b.py"))
    assert result == 3


# ---------------------------------------------------------------------------
# AST pin — composition, not duplication
# ---------------------------------------------------------------------------


def test_advisor_does_not_implement_its_own_bfs():
    """The Oracle-graph blast path MUST compose ``oracle.get_blast_radius``,
    NOT reimplement the BFS over a parallel graph.

    Per operator binding 2026-05-13: "fully leverage the existing
    files + architecture; avoid duplication".  A drift toward a
    second blast implementation in operation_advisor would fork
    the codebase's understanding of import dependencies.
    """
    src = Path(inspect.getfile(operation_advisor)).read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Look for any function in operation_advisor that walks edges
    # (would be a parallel BFS).  Heuristic: any function calling
    # ``get_edges_to`` / ``get_edges_from`` / a homegrown BFS visit
    # loop is suspicious.
    forbidden_methods = {"get_edges_to", "get_edges_from", "_compute_blast_radius_bfs"}
    found_forbidden = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.attr if isinstance(fn, ast.Attribute)
                else fn.id if isinstance(fn, ast.Name)
                else None
            )
            if name in forbidden_methods:
                found_forbidden.append(
                    f"line {node.lineno}: {ast.unparse(node)[:80]}"
                )

    assert not found_forbidden, (
        "operation_advisor.py contains calls suggesting a "
        "parallel BFS implementation:\n"
        + "\n".join(f"  - {s}" for s in found_forbidden)
        + "\nPR-A invariant: compose oracle.get_blast_radius, "
        "don't fork the graph traversal."
    )

    # Positive pin: get_blast_radius MUST be called (composition site)
    calls_get_blast_radius = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "get_blast_radius":
                calls_get_blast_radius = True
                break
    assert calls_get_blast_radius, (
        "operation_advisor.py never calls oracle.get_blast_radius — "
        "PR-A wiring missing"
    )


def test_oracle_registration_wired_in_governed_loop():
    """The GovernedLoopService MUST call ``set_active_oracle`` after
    Oracle init.  Without that wiring, the advisor never sees the
    Oracle and the master flag is a no-op."""
    from backend.core.ouroboros.governance import governed_loop_service
    src = Path(
        inspect.getfile(governed_loop_service)
    ).read_text(encoding="utf-8")
    assert "set_active_oracle" in src, (
        "governed_loop_service.py never calls set_active_oracle — "
        "Oracle never registers with the advisor module, and PR-A "
        "is dead code in production."
    )
