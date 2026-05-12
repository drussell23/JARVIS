"""Regression spine for Phase 1.5.C — harness boot-hook wire.

Phase 1.5.C is a single load-bearing edit to
``backend/core/ouroboros/battle_test/harness.py`` that lazy-imports
``maybe_inject_exercise_at_boot`` from the Phase 1.5.A substrate +
calls it once during boot, after both ``intake_router`` and
``worktree_manager`` are resolvable.

This spine pins the structural invariants of that single edit via
AST inspection of ``harness.py``.  Runtime behavior of the boot
hook itself is already pinned by the Phase 1.5.A spine
(``test_l2_exercise_seed.py`` covers ``maybe_inject_exercise_at_boot``
through every verdict outcome).  The job of THIS spine is to prove
the harness actually calls it correctly.

Invariants pinned
-----------------

* harness.py imports ``maybe_inject_exercise_at_boot`` from the
  canonical Phase 1.5.A substrate (composition pin — no parallel
  injection mechanism)
* harness.py imports ``WorktreeManager`` lazily inside the boot
  hook path (composition pin — uses canonical isolation primitive,
  same as Treefinement production wiring + L3 subagent scheduler)
* The hook is wrapped in an outer ``try / except`` that catches
  every non-CancelledError exception (fail-open contract — boot
  MUST never fail due to L2 exercise wiring)
* The hook resolves the intake router via the SAME attribute
  walker pattern the plugin section uses (composition pin —
  single canonical resolution path)
* The hook is positioned AFTER plugin discovery + BEFORE the
  "Boot each subsystem independently" section (so by the time it
  runs, ``self._governed_loop_service`` is fully constructed +
  ``self._config.repo_path`` is valid)
* The hook calls ``maybe_inject_exercise_at_boot`` (not a parallel
  injection function)
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import harness as harness_module


_HARNESS_SRC = Path(
    inspect.getfile(harness_module),
).read_text(encoding="utf-8")
_HARNESS_AST = ast.parse(_HARNESS_SRC)


# ===========================================================================
# Composition pins — harness imports the Phase 1.5.A substrate
# ===========================================================================


def _all_imports():
    """Every ImportFrom node anywhere in harness.py (top-level + lazy
    imports inside method bodies).  We expect the L2 exercise imports
    to be LAZY (not top-level) so non-exercise-mode boots pay zero
    import cost when the feature is disabled."""
    out = []
    for node in ast.walk(_HARNESS_AST):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = tuple(a.name for a in node.names)
            out.append((mod, names))
    return out


def _top_level_imports():
    """Only ImportFrom nodes at module top level."""
    out = []
    for node in _HARNESS_AST.body:
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = tuple(a.name for a in node.names)
            out.append((mod, names))
    return out


def test_harness_imports_maybe_inject_exercise_at_boot():
    """Phase 1.5.C composition pin: the harness imports the canonical
    Phase 1.5.A boot hook function — NOT a parallel injection
    function defined elsewhere."""
    matches = [
        (m, n) for (m, n) in _all_imports()
        if m.endswith(".l2_exercise_seed")
        and "maybe_inject_exercise_at_boot" in n
    ]
    assert matches, (
        "harness.py MUST import maybe_inject_exercise_at_boot from "
        "l2_exercise_seed — Phase 1.5.C composition pin"
    )


def test_harness_imports_canonical_worktree_manager_for_exercise():
    """The boot hook uses the canonical WorktreeManager — same
    primitive Treefinement production wiring + L3 subagent
    scheduler use.  No parallel isolation primitive."""
    matches = [
        (m, n) for (m, n) in _all_imports()
        if m.endswith(".worktree_manager")
        and "WorktreeManager" in n
    ]
    assert matches, (
        "harness.py MUST import WorktreeManager somewhere — composition pin"
    )


def test_l2_exercise_seed_import_is_lazy_not_top_level():
    """The L2 exercise import MUST be lazy (inside a method body),
    NOT top-level.  This keeps non-exercise-mode boots zero-cost +
    avoids circular-import hazards."""
    top_level_l2 = [
        (m, n) for (m, n) in _top_level_imports()
        if m.endswith(".l2_exercise_seed")
    ]
    assert top_level_l2 == [], (
        f"harness.py MUST NOT import l2_exercise_seed at top level; "
        f"found {top_level_l2}. Lazy import only."
    )


# ===========================================================================
# Fail-open invariant — boot hook is wrapped in try/except
# ===========================================================================


def test_boot_hook_call_inside_try_except():
    """The boot-hook call MUST be enclosed in a try/except wrapper.
    Without this, a transient L2-exercise failure would crash harness
    boot — defeating the entire "non-production-impact" §33.1
    contract."""
    # Find the maybe_inject_exercise_at_boot call site
    call_node = None
    for node in ast.walk(_HARNESS_AST):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "maybe_inject_exercise_at_boot"
        ):
            call_node = node
            break
        if (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "maybe_inject_exercise_at_boot"
        ):
            call_node = node.value
            break
    assert call_node is not None, (
        "maybe_inject_exercise_at_boot must appear as a call in harness.py"
    )

    # Walk up the AST to find an enclosing try-block
    # (ast.walk doesn't preserve parent links; we re-scan with a
    # context tracker)
    enclosing_try = _find_enclosing_try(call_node)
    assert enclosing_try is not None, (
        "maybe_inject_exercise_at_boot call MUST be inside a "
        "try/except wrapper — fail-open contract"
    )
    # Verify the try has at least one ExceptHandler that catches
    # broad Exception (or bare except, equivalent for our purpose)
    has_broad_handler = False
    for handler in enclosing_try.handlers:
        if handler.type is None:  # bare except
            has_broad_handler = True
            break
        if isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
            has_broad_handler = True
            break
    assert has_broad_handler, (
        "boot-hook try/except MUST catch broad Exception (or be a "
        "bare except) — boot path can't selectively-leak unknown "
        "exception types"
    )


def _find_enclosing_try(target_node: ast.AST):
    """Walk the harness AST looking for a Try block that contains
    ``target_node`` in its body (recursively).  Returns the Try
    node or None."""
    for node in ast.walk(_HARNESS_AST):
        if isinstance(node, ast.Try):
            for body_node in ast.walk(node):
                if body_node is target_node:
                    # Verify it's actually IN the try body, not in
                    # the except / else / finally clauses
                    for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                        if child is target_node:
                            return node
                    # Fall through if target was in handlers
    return None


# ===========================================================================
# Composition pin — same attribute walker as plugin section
# ===========================================================================


def test_boot_hook_uses_same_attribute_walker_as_plugins():
    """Both the plugin section + the L2 exercise hook resolve the
    intake_router via the SAME 4-attribute walker pattern:
    ``_intake_router`` → ``intake_router`` → ``_router`` → ``router``.

    Drift here = two parallel resolution paths.  Composition
    discipline pin."""
    # Count the occurrences of the 4-attribute tuple in source
    # (both plugins + L2 exercise hook should have it)
    attr_tuple_pattern = (
        '"_intake_router", "intake_router",'
    )
    count = _HARNESS_SRC.count(attr_tuple_pattern)
    assert count >= 2, (
        f"Expected ≥2 occurrences of intake-router attribute walker "
        f"pattern (plugin section + L2 exercise hook), got {count}"
    )


# ===========================================================================
# Positioning pin — hook is AFTER plugin section + BEFORE subsystem boot
# ===========================================================================


def test_boot_hook_positioned_after_plugin_section():
    """The L2 exercise hook MUST be positioned in source AFTER the
    plugin discovery + load section.  Pre-plugin positioning would
    mean the intake_router isn't fully constructed yet."""
    plugin_marker = "plugin discovery/load failed"
    exercise_marker = "L2 exercise corpus boot hook"
    plugin_idx = _HARNESS_SRC.find(plugin_marker)
    exercise_idx = _HARNESS_SRC.find(exercise_marker)
    assert plugin_idx != -1, "plugin section marker missing"
    assert exercise_idx != -1, "L2 exercise hook marker missing"
    assert plugin_idx < exercise_idx, (
        f"L2 exercise hook positioned BEFORE plugin section "
        f"(plugin={plugin_idx} exercise={exercise_idx}); harness boot "
        f"order requires plugin section to land first"
    )


def test_boot_hook_positioned_before_subsystem_boot_block():
    """The hook MUST be positioned BEFORE the "Boot each subsystem
    independently" block — otherwise it would compete for resources
    with subsystem boot tasks."""
    exercise_marker = "L2 exercise corpus boot hook"
    subsystem_marker = "Boot each subsystem independently"
    exercise_idx = _HARNESS_SRC.find(exercise_marker)
    subsystem_idx = _HARNESS_SRC.find(subsystem_marker)
    assert exercise_idx != -1
    assert subsystem_idx != -1
    assert exercise_idx < subsystem_idx, (
        f"L2 exercise hook positioned AFTER subsystem boot block "
        f"(exercise={exercise_idx} subsystem={subsystem_idx}); "
        f"boot-order invariant violated"
    )


# ===========================================================================
# Behavior pin — harness parses cleanly under all flag states
# ===========================================================================


def test_harness_imports_cleanly_with_master_flag_false(monkeypatch):
    """Defense in depth: importing harness must succeed regardless
    of the master flag's state.  The lazy-import discipline means
    importing harness MUST NEVER import l2_exercise_seed eagerly."""
    monkeypatch.delenv("JARVIS_L2_EXERCISE_CORPUS_ENABLED", raising=False)
    # Force re-import — verify it succeeds even with flag absent.
    # The boot hook only fires when async _boot is called; module
    # import itself should be flag-independent.
    import importlib
    import backend.core.ouroboros.battle_test.harness as h
    importlib.reload(h)
    # If we got here without exception, the import is decoupled from
    # the master flag.  Defensive check via runtime probe:
    assert hasattr(h, "logger")  # harness exposes a logger; sanity check


def test_harness_imports_cleanly_with_master_flag_true(monkeypatch):
    """Symmetric: importing harness with the flag ON should also
    succeed.  Importing l2_exercise_seed itself does NOT activate
    the boot hook (only the awaited maybe_inject_exercise_at_boot
    call does).  AST verified separately above."""
    monkeypatch.setenv("JARVIS_L2_EXERCISE_CORPUS_ENABLED", "true")
    import importlib
    import backend.core.ouroboros.battle_test.harness as h
    importlib.reload(h)
    assert hasattr(h, "logger")


# ===========================================================================
# Defensive — the boot hook's outer try/except catches Exception
# ===========================================================================


def test_boot_hook_comment_documents_fail_open_contract():
    """Operator-visible documentation invariant: the hook's comment
    block must mention the fail-open / default-FALSE contract so
    readers don't accidentally remove the try/except guard."""
    expected_phrases = [
        "Default-FALSE per §33.1",
        "never blocks",  # spans "never blocks\n            # boot."
    ]
    for phrase in expected_phrases:
        assert phrase in _HARNESS_SRC, (
            f"Boot-hook comment block MUST contain {phrase!r} so "
            f"readers don't accidentally remove the fail-open guard"
        )
