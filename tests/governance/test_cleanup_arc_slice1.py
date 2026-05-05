"""§32.5 Cleanup Arc Slice 1 — regression spine.

Verifies:

  * Archived files exist at expected ``archive/legacy/`` paths.
  * Forbidden production paths are absent.
  * Provenance README exists with required sections.
  * 4 ``cleanup_invariants`` AST pins all PASS.
  * Production modules (harness.py, runtime_task_orchestrator.py,
    governed_loop_service.py) import cleanly without
    ``graduation_orchestrator`` / ``graduation_tracker``.
  * The archived modules are NOT importable via the production
    dotted path.
  * ``jarvis_intelligence.py:447`` TODO is closed (capabilities_-
    graduated reads from FlagRegistry, not the orchestrator).
"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Archive integrity
# ---------------------------------------------------------------------------


_EXPECTED_ARCHIVE_PATHS = (
    "archive/legacy/graduation_orchestrator_2026_04_06.py",
    "archive/legacy/graduation_tracker_2026_04_06.py",
    "archive/legacy/test_graduation_orchestrator_2026_04_06.py",
)

_FORBIDDEN_PRODUCTION_PATHS = (
    "backend/core/ouroboros/governance/graduation_orchestrator.py",
    "backend/core/ouroboros/governance/graduation_tracker.py",
    "tests/governance/test_graduation_orchestrator.py",
)


@pytest.mark.parametrize("rel_path", _EXPECTED_ARCHIVE_PATHS)
def test_archived_file_exists(rel_path):
    path = _repo_root() / rel_path
    assert path.exists(), (
        f"archived file missing: {rel_path}"
    )
    # Files MUST not be empty stubs — they preserve real
    # historical code for design-lineage audit.
    size = path.stat().st_size
    assert size > 1_000, (
        f"archived file too small ({size} bytes): "
        f"{rel_path} — expected real preserved code"
    )


@pytest.mark.parametrize(
    "rel_path", _FORBIDDEN_PRODUCTION_PATHS,
)
def test_forbidden_production_path_absent(rel_path):
    path = _repo_root() / rel_path
    assert not path.exists(), (
        f"forbidden production path re-introduced: "
        f"{rel_path}"
    )


def test_archive_readme_exists():
    readme = _repo_root() / "archive" / "legacy" / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8")
    # Provenance README must document the salvage decision.
    assert "graduation_orchestrator" in text
    assert "M10" in text
    assert "§32.5" in text


# ---------------------------------------------------------------------------
# Production import cleanliness
# ---------------------------------------------------------------------------


def test_archived_module_not_importable_via_production_path():
    """The archived modules MUST NOT be importable via their
    original dotted production path. Any importer that tried
    would get ImportError."""
    spec = importlib.util.find_spec(
        "backend.core.ouroboros.governance.graduation_orchestrator",
    )
    assert spec is None, (
        "graduation_orchestrator still importable from "
        "production path — archive may have failed"
    )
    spec = importlib.util.find_spec(
        "backend.core.ouroboros.governance.graduation_tracker",
    )
    assert spec is None, (
        "graduation_tracker still importable from production "
        "path"
    )


def test_harness_imports_clean():
    """harness.py must import without referencing archived
    modules."""
    mod = importlib.import_module(
        "backend.core.ouroboros.battle_test.harness",
    )
    # boot_graduation method MUST be removed
    cls_names = [
        name for name in dir(mod)
        if not name.startswith("_")
    ]
    # The removal target — there should be no `boot_graduation`
    # function or method on any export.
    if "BattleTestHarness" in cls_names:
        harness_cls = getattr(mod, "BattleTestHarness")
        assert not hasattr(harness_cls, "boot_graduation"), (
            "BattleTestHarness MUST NOT re-introduce "
            "boot_graduation method"
        )


def test_runtime_task_orchestrator_imports_clean():
    importlib.import_module(
        "backend.core.runtime_task_orchestrator",
    )


def test_governed_loop_service_imports_clean():
    importlib.import_module(
        "backend.core.ouroboros.governance.governed_loop_service",
    )


# ---------------------------------------------------------------------------
# AST pin discovery + validation
# ---------------------------------------------------------------------------


_EXPECTED_CLEANUP_PIN_NAMES = {
    "graduation_orchestrator_archived_only_harness",
    "graduation_orchestrator_archived_only_runtime_task",
    "graduation_orchestrator_archived_only_governed_loop",
    "graduation_orchestrator_module_archived",
}


def test_cleanup_pins_registered():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        list_shipped_code_invariants,
    )
    registered = {
        inv.invariant_name
        for inv in list_shipped_code_invariants()
        if inv.invariant_name.startswith(
            "graduation_orchestrator_",
        )
    }
    missing = _EXPECTED_CLEANUP_PIN_NAMES - registered
    assert not missing, (
        f"missing cleanup pins: {missing}"
    )


def test_cleanup_pins_pass_validation():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        validate_all,
    )
    violations = validate_all()
    cleanup_violations = [
        v for v in violations
        if v.invariant_name.startswith(
            "graduation_orchestrator_",
        )
    ]
    assert not cleanup_violations, (
        "cleanup pin violations: " + "; ".join(
            f"{v.invariant_name}: {v.violation}"
            for v in cleanup_violations
        )
    )


# ---------------------------------------------------------------------------
# jarvis_intelligence.py:447 TODO closure
# ---------------------------------------------------------------------------


def test_jarvis_intelligence_todo_closed():
    """The TODO that pointed at the archived
    graduation_orchestrator MUST have been replaced with a read
    from FlagRegistry SEED_SPECS (default-true bool count).
    Audit confirmed this closure pre-§32.5; pin it
    structurally."""
    target = (
        _repo_root()
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "jarvis_intelligence.py"
    )
    text = target.read_text(encoding="utf-8")
    # The file MUST NOT carry a live TODO referencing the
    # archived orchestrator.
    forbidden_marker = (
        "TODO: import graduation_orchestrator"
    )
    assert forbidden_marker not in text, (
        "jarvis_intelligence.py still has a TODO pointing "
        "at the archived orchestrator"
    )
    # Closure marker MUST be present — the comment explaining
    # the TODO was replaced with the FlagRegistry read.
    assert "graduation_orchestrator" in text, (
        "jarvis_intelligence.py should retain a comment "
        "explaining the closed TODO for audit-trail clarity"
    )
    # And the FlagRegistry-based capability count MUST be
    # present.
    assert "capabilities_graduated" in text


# ---------------------------------------------------------------------------
# Cleanup module structural invariants
# ---------------------------------------------------------------------------


def test_cleanup_invariants_module_has_register_function():
    from backend.core.ouroboros.governance import cleanup_invariants
    assert hasattr(cleanup_invariants, "register_shipped_invariants")
    invs = cleanup_invariants.register_shipped_invariants()
    assert len(invs) == 4


def test_cleanup_invariants_authority_asymmetry():
    """cleanup_invariants.py MUST be pure substrate — stdlib +
    ShippedCodeInvariant import only. No orchestrator / iron_-
    gate / policy / providers imports."""
    target = (
        _repo_root()
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "cleanup_invariants.py"
    )
    import ast as _ast
    tree = _ast.parse(target.read_text(encoding="utf-8"))
    forbidden_substrings = (
        "orchestrator",  # we forbid orchestrator import; archived
        "iron_gate",     # name itself is fine in strings;
        "policy",        # we just check the import sources
        "providers",
        "candidate_generator",
    )
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            for forbidden in forbidden_substrings:
                if (
                    forbidden in module
                    and "shipped_code_invariants" not in module
                ):
                    pytest.fail(
                        f"cleanup_invariants.py MUST NOT "
                        f"import {module!r} (authority "
                        f"asymmetry — pure substrate)"
                    )
