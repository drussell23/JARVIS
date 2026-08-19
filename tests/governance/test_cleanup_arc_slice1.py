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
    "backend/core/ouroboros/governance/graduation_tracker.py",
    "tests/governance/test_graduation_orchestrator.py",
)

#: archive -> the production path its implementation must never return to.
#:
#: WHY THIS REPLACED A PATH BAN FOR `graduation_orchestrator`
#: ---------------------------------------------------------
#: The original pin forbade the PATH. Slice 132 (#69347) and Slice 136
#: (#69351) then created a genuinely DIFFERENT module at that path — the
#: Cognitive Graduation Matrix, 312 lines exposing `graduate` /
#: `graduate_all` / `GraduationOutcome`, against the archived module's 1,137
#: lines exposing `GraduationOrchestrator` / `GraduationPhase` /
#: `EphemeralUsageTracker`. ZERO symbol overlap; only the name is shared, and
#: `phase9_orchestrator.py` already documents the two as distinct ("different
#: scope", "graduation_orchestrator_archived_only").
#:
#: So the pin failed for a module it was never written about. A path is a
#: PROXY for the property that matters — "the archived implementation has not
#: come back" — and a proxy that cannot tell one module from another reports a
#: violation nobody committed. The check below tests IDENTITY instead, and
#: derives the archived symbol set FROM THE ARCHIVE at test time, so it stays
#: correct if the archive is ever amended.
_ARCHIVE_IDENTITY_PAIRS = (
    (
        "archive/legacy/graduation_orchestrator_2026_04_06.py",
        "backend/core/ouroboros/governance/graduation_orchestrator.py",
    ),
)


def _top_level_symbols(path: Path) -> set:
    """Top-level class/function names defined by a module. NEVER raises."""
    import ast as _ast
    try:
        tree = _ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — unparseable proves nothing either way
        return set()
    return {
        node.name for node in tree.body
        if isinstance(
            node,
            (_ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef),
        )
    }


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


@pytest.mark.parametrize(
    "archived_rel,production_rel", _ARCHIVE_IDENTITY_PAIRS,
)
def test_archived_implementation_is_not_resurrected(
    archived_rel, production_rel,
):
    """The ARCHIVED CODE must not return — a reused name is not a relapse.

    An absent production path is the strongest possible pass. When one does
    exist, it must share no top-level symbol with the archived module: that is
    what distinguishes "someone restored the retired implementation" from
    "a later slice reused a good name for different code"."""
    root = _repo_root()
    production = root / production_rel
    if not production.exists():
        return                      # nothing there at all — strongest pass

    archived = root / archived_rel
    assert archived.exists(), (
        f"archive missing: {archived_rel} — the pin cannot judge a "
        "resurrection without the thing it is comparing against"
    )
    archived_syms = _top_level_symbols(archived)
    assert archived_syms, (
        f"no symbols parsed from {archived_rel}; the comparison would pass "
        "vacuously and prove nothing"
    )
    overlap = archived_syms & _top_level_symbols(production)
    assert not overlap, (
        f"archived implementation resurrected at {production_rel}: "
        f"shares {sorted(overlap)} with {archived_rel}"
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
    # `graduation_orchestrator` is DELIBERATELY importable again: a different
    # module (Slice 132/136) took the name. Its identity — not its
    # importability — is what `test_archived_implementation_is_not_resurrected`
    # guards. Asserting un-importability here would forbid a name rather than
    # an implementation.
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
            # `.detail` — `InvariantViolation` has no `.violation`. This
            # f-string is only evaluated WHEN violations exist, so the
            # AttributeError replaced the report at the one moment it
            # mattered: a diagnostic that fails precisely when it is needed.
            f"{v.invariant_name}: {v.detail}"
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
    # 4 archive-only + 3 consumer-uses-primitive (Slice 2)
    # + 5 observability-module-exposes-register_routes (Slice 3)
    # + 1 observability_route_registry_uses_primitive (Slice 3)
    # + 1 repl_dispatch_registry_uses_primitive (Slice 4)
    # = 14 pins total
    assert len(invs) == 14


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
