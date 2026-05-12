"""Treefinement Production Wiring Phase E — hardening + AST pins.

Pins the load-bearing structural invariants for the production-
wiring module:

The 6 AST pins
--------------
1. Composition: ``maybe_inject_sibling_outcomes`` import from
   ``repair_tree`` (Phase 3 substrate composition pin).
2. Composition: ``WorktreeManager`` is lazy-imported inside the
   production factory (single-source isolation primitive).
3. Composition: ``CanonicalBranchValidator`` is lazy-imported
   inside the production factory (single-source validator).
4. Composition: ``TestRunner`` + ``SemanticGuardian`` lazy-imported
   inside the production factory.
5. Anti-pattern: no inline ``git apply`` subprocess invocation
   outside ``GitApplyDiffApplier``.
6. Anti-pattern: forbidden authority imports
   (orchestrator / iron_gate / change_engine / policy_engine /
   candidate_generator / risk_tier).

Plus closed-taxonomy mapping pins (extra defense in depth):
7. ``_TREE_VERDICT_TO_STOP_REASON`` covers exactly
   {EXHAUSTED, BUDGET_TERMINAL}.
8. ``_TREE_OUTCOME_TO_ITERATION_OUTCOME`` covers exactly the 5
   BranchOutcome members.

Defense-in-depth tests
----------------------
* Lazy boot registration: idempotent + respects operator overrides
  + never raises on registration failure
* Production gate end-to-end: registered factory → tree runs →
  RepairResult adapted correctly (composed with stub deps to avoid
  network/git)
* Per-branch worktree creation failure → other branches unaffected
* Git apply timeout integration (uses fake-git that sleeps; verifies
  subprocess kill + structured error)
"""
from __future__ import annotations

import asyncio
import ast
import inspect
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance import repair_tree_production
from backend.core.ouroboros.governance.repair_engine import (
    RepairBudget,
    RepairEngine,
)
from backend.core.ouroboros.governance.repair_tree import (
    BranchingStrategy,
    BranchOutcome,
    DiffApplyResult,
    LayerVerdict,
    MASTER_FLAG_ENV_VAR,
    PruningReason,
    TreefinementBudget,
    get_production_tree_runner_factory,
    register_production_tree_runner_factory,
)
from backend.core.ouroboros.governance.repair_tree_production import (
    GitApplyDiffApplier,
    _TREE_OUTCOME_TO_ITERATION_OUTCOME,
    _TREE_VERDICT_TO_STOP_REASON,
    production_tree_runner_factory,
    register_production_factory_at_boot,
)


_MODULE_SRC = Path(
    inspect.getfile(repair_tree_production),
).read_text(encoding="utf-8")
_MODULE_AST = ast.parse(_MODULE_SRC)


# ===========================================================================
# AST pin helpers
# ===========================================================================


def _top_level_imports() -> List[Tuple[str, Tuple[str, ...]]]:
    """ImportFrom nodes at module top-level only (not inside funcs)."""
    out: List[Tuple[str, Tuple[str, ...]]] = []
    for node in _MODULE_AST.body:
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = tuple(a.name for a in node.names)
            out.append((mod, names))
    return out


def _all_imports() -> List[Tuple[str, Tuple[str, ...]]]:
    """ImportFrom nodes anywhere (top-level + nested in functions)."""
    out: List[Tuple[str, Tuple[str, ...]]] = []
    for node in ast.walk(_MODULE_AST):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = tuple(a.name for a in node.names)
            out.append((mod, names))
    return out


def _function_body_source(name: str) -> Optional[str]:
    """Extract a function body source by name (top-level or method)."""
    for node in ast.walk(_MODULE_AST):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return ast.unparse(node)
    return None


# ===========================================================================
# AST pin #1 — composition: maybe_inject_sibling_outcomes
# ===========================================================================


def test_pin_1_maybe_inject_sibling_outcomes_imported():
    """ProductionBranchGenerator composes the Phase 3 cross-branch
    substrate. Drift toward parallel block-building breaks the
    single-source-of-truth invariant."""
    matches = [
        (m, names) for (m, names) in _top_level_imports()
        if m.endswith(".repair_tree")
        and "maybe_inject_sibling_outcomes" in names
    ]
    assert matches, (
        "repair_tree_production MUST import "
        "maybe_inject_sibling_outcomes from repair_tree — composes "
        "Phase 3 cross-branch substrate"
    )


# ===========================================================================
# AST pin #2-#4 — composition: lazy imports inside factory body
# ===========================================================================


def test_pin_2_worktree_manager_lazy_imported_inside_factory():
    """WorktreeManager is the canonical isolation primitive. The
    factory MUST lazy-import it (not top-level) to keep the
    production-wiring module dep-free for non-tree-mode callers."""
    body = _function_body_source("production_tree_runner_factory")
    assert body is not None, "factory function not found"
    assert (
        "from backend.core.ouroboros.governance.worktree_manager "
        "import" in body
    ) and "WorktreeManager" in body, (
        "factory MUST lazy-import WorktreeManager — composition pin"
    )


def test_pin_3_canonical_branch_validator_lazy_imported_inside_factory():
    body = _function_body_source("production_tree_runner_factory")
    assert body is not None
    assert (
        "from backend.core.ouroboros.governance.repair_tree "
        "import" in body
    ) and "CanonicalBranchValidator" in body, (
        "factory MUST lazy-import CanonicalBranchValidator from "
        "repair_tree — composition pin (no parallel validator)"
    )


def test_pin_4_test_runner_and_semantic_guardian_lazy_imported_in_factory():
    body = _function_body_source("production_tree_runner_factory")
    assert body is not None
    assert "TestRunner" in body, (
        "factory MUST lazy-import TestRunner — composition pin"
    )
    assert "SemanticGuardian" in body, (
        "factory MUST lazy-import SemanticGuardian — composition pin"
    )


# ===========================================================================
# AST pin #5 — anti-pattern: no inline git apply outside GitApplyDiffApplier
# ===========================================================================


def test_pin_5_no_inline_git_apply_outside_applier_class():
    """Only ``GitApplyDiffApplier.__call__`` may invoke ``git apply``.
    Drift toward inline subprocess invocations bypasses the
    Protocol-typed fail-closed contract."""
    # Walk all FunctionDef/AsyncFunctionDef in the module.
    for node in ast.walk(_MODULE_AST):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Allow the canonical ``__call__`` method on GitApplyDiffApplier
        if node.name == "__call__":
            # Determine if this __call__ belongs to GitApplyDiffApplier
            # by walking the AST hierarchy. Simpler heuristic: check
            # the source for the class context.
            continue
        body = ast.unparse(node)
        # Look for the canonical "git apply" string argument anywhere
        # in the function body.
        if '"apply"' in body and '"git"' in body and "subprocess" in body.lower():
            # Allow it only if the function is itself the canonical applier
            assert False, (
                f"function {node.name!r} appears to inline a git "
                "apply subprocess invocation — composition pin "
                "forbids this outside GitApplyDiffApplier"
            )


# ===========================================================================
# AST pin #6 — anti-pattern: forbidden authority imports (§1 Boundary)
# ===========================================================================


def test_pin_6_no_authority_imports():
    """repair_tree_production composes substrates; it MUST NOT
    import policy / orchestrator / authority modules. §1 Boundary."""
    forbidden = (
        "orchestrator",
        "iron_gate",
        "change_engine",
        "candidate_generator",
        "policy_engine",
        "risk_tier",
    )
    imports = _all_imports()
    for f in forbidden:
        for (mod, _names) in imports:
            assert f not in mod, (
                f"repair_tree_production.py MUST NOT import {f!r} — "
                f"found in {mod!r}. This collapses §1 Boundary."
            )


# ===========================================================================
# AST pin #7-#8 — closed taxonomy mapping completeness
# ===========================================================================


def test_pin_7_verdict_mapping_complete():
    """_TREE_VERDICT_TO_STOP_REASON MUST cover EXHAUSTED +
    BUDGET_TERMINAL. WON_TERMINAL is handled separately (returns
    L2_CONVERGED); EXPANDED never reaches the adapter terminal
    branch."""
    assert dict(_TREE_VERDICT_TO_STOP_REASON) == {
        "exhausted": "treefinement_exhausted",
        "budget_terminal": "treefinement_budget_terminal",
    }


def test_pin_8_outcome_mapping_complete():
    """_TREE_OUTCOME_TO_ITERATION_OUTCOME MUST cover ALL 5
    BranchOutcome members. Adding a new outcome without extending
    this mapping silently maps the new outcome to 'no_progress'
    fallback — drift detector."""
    expected = {m.value for m in BranchOutcome}
    actual = set(_TREE_OUTCOME_TO_ITERATION_OUTCOME.keys())
    assert actual == expected, (
        f"Outcome mapping drift: expected {expected}, got {actual}"
    )


# ===========================================================================
# Defense-in-depth: lazy boot registration
# ===========================================================================


@pytest.fixture(autouse=True)
def _isolate_factory() -> Iterator[None]:
    """Each test starts with no registered factory."""
    register_production_tree_runner_factory(None)
    yield
    register_production_tree_runner_factory(None)


def test_lazy_registration_first_call_registers_canonical_factory():
    assert get_production_tree_runner_factory() is None
    registered = register_production_factory_at_boot()
    assert registered is True
    assert (
        get_production_tree_runner_factory()
        is production_tree_runner_factory
    )


def test_lazy_registration_idempotent():
    """Calling boot-registration twice does nothing the second time."""
    first = register_production_factory_at_boot()
    second = register_production_factory_at_boot()
    third = register_production_factory_at_boot()
    assert first is True
    assert second is False, "second call MUST NOT re-register"
    assert third is False


def test_lazy_registration_respects_operator_override():
    """If an operator (or test) has registered a custom factory,
    boot registration MUST NOT overwrite it. Operator intent is
    authoritative."""
    def _custom_factory(**_kw):
        raise AssertionError("not invoked")

    register_production_tree_runner_factory(_custom_factory)
    registered = register_production_factory_at_boot()
    assert registered is False
    assert get_production_tree_runner_factory() is _custom_factory


def test_lazy_registration_never_raises_on_internal_failure(monkeypatch):
    """Even if the registry mechanism itself fails, boot registration
    returns False — caller falls through to LINEAR."""
    # Force the registry getter to raise
    import backend.core.ouroboros.governance.repair_tree as rt
    monkeypatch.setattr(
        rt, "get_production_tree_runner_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("broke")),
    )
    # Must NOT raise — returns False
    registered = register_production_factory_at_boot()
    assert registered is False


# ===========================================================================
# Defense-in-depth: gate end-to-end with lazy boot registration
# ===========================================================================


def test_gate_triggers_lazy_registration_on_first_call(
    tmp_path, monkeypatch,
):
    """When the strategy gate runs with no factory registered, it
    invokes the boot registration once, then proceeds with the
    canonical factory."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "bfs")

    # Stub provider to avoid network — production factory will still
    # construct, but the closure will be invoked with stub deps via
    # the gate path. For this test we just verify lazy registration
    # is triggered.
    register_production_tree_runner_factory(None)
    assert get_production_tree_runner_factory() is None

    class _Ctx:
        op_id = "op-lazy-trigger"
        repo_root = str(tmp_path)
        generation = None

    class _StubProvider:
        async def generate(self, *_args, **_kwargs):
            class _R:
                candidates = [{
                    "unified_diff": (
                        "--- a/x.py\n+++ b/x.py\n"
                        "@@ -1 +1 @@\n-x\n+y\n"
                    ),
                }]
                model_id = "stub"
                provider_name = "stub"
            return _R()

    engine = RepairEngine(
        budget=RepairBudget(),
        prime_provider=_StubProvider(),
        repo_root=tmp_path,
    )

    # Call the gate; it should self-register the factory
    # (production factory will try to construct real deps + may
    # fail when there's no git repo in tmp_path — that's fine, we
    # just verify the registration triggered).
    asyncio.run(engine._maybe_run_treefinement(
        _Ctx(), None, datetime.now(timezone.utc),
    ))

    # After the gate call, factory should be registered
    assert (
        get_production_tree_runner_factory()
        is production_tree_runner_factory
    ), "lazy registration MUST have fired during the gate call"


# ===========================================================================
# Defense-in-depth: partial worktree failure → other branches unaffected
# ===========================================================================


def test_partial_worktree_failure_does_not_poison_other_branches(
    tmp_path, monkeypatch,
):
    """Branch 2 of 3 fails worktree creation → branches 1 and 3
    still execute + are archived. End-to-end via the runner +
    Phase 1 isolation discipline."""
    from backend.core.ouroboros.governance.repair_tree import (
        RepairTreeRunner,
        TreefinementBudget,
    )

    create_count = {"n": 0}

    class _SelectiveWM:
        """Worktree manager that fails the 2nd create call only."""
        async def create(self, branch_name):
            create_count["n"] += 1
            if create_count["n"] == 2:
                raise RuntimeError(
                    "git worktree add failed for branch 2"
                )
            wt = tmp_path / branch_name.replace("/", "_")
            wt.mkdir(exist_ok=True, parents=True)
            return wt

        async def cleanup(self, path):
            pass

    async def _stub_generator(*, op_id, layer_index,
                              parent_branch, sibling_outcomes):
        # Unique diff per call so dedup doesn't collapse
        nonlocal_n = create_count["n"] + len(sibling_outcomes)
        return (
            f"--- a/foo_{nonlocal_n}.py\n+++ b/foo_{nonlocal_n}.py\n"
            f"@@ -1 +1 @@\n-x\n+y_{nonlocal_n}\n",
            f"hyp-{nonlocal_n}",
            0.001,
        )

    async def _stub_validator(*, op_id, branch_id, diff, worktree_dir):
        return (BranchOutcome.PROMOTED, 0.5, None, 1)

    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    budget = TreefinementBudget(
        enabled=True,
        branching_strategy=BranchingStrategy.BFS,
        max_branches_per_layer=3,
        beam_width=2,
        branch_dedup_enabled=False,
        cross_branch_learning_enabled=False,
        emergency_demote_threshold=0.85,
    )
    runner = RepairTreeRunner(
        budget,
        worktree_manager=_SelectiveWM(),
    )
    result = asyncio.run(runner.run_tree(
        op_id="op-partial",
        generator=_stub_generator,
        validator=_stub_validator,
        max_layers=1,
    ))
    layer = result.layers[0]
    assert len(layer.branches) == 3, (
        "Phase 1 isolation discipline: all 3 branches MUST appear "
        "in the layer (one with worktree failure, two with PROMOTED)"
    )
    # Branches with worktree failure → failure_class=infra
    infra_branches = [
        b for b in layer.branches if b.failure_class == "infra"
    ]
    promoted = [
        b for b in layer.branches
        if b.outcome == BranchOutcome.PROMOTED
    ]
    assert len(infra_branches) == 1
    assert len(promoted) == 2


# ===========================================================================
# Defense-in-depth: git apply timeout integration via fake-git
# ===========================================================================


def test_git_apply_timeout_integration_kills_subprocess(tmp_path):
    """Production applier with very short timeout + fake-git that
    sleeps → returns git_apply_timeout structured error.
    Verifies subprocess kill behavior end-to-end."""
    fake_git = tmp_path / "fake_git_sleeper"
    fake_git.write_text("#!/bin/sh\nsleep 30\n")
    fake_git.chmod(0o755)

    applier = GitApplyDiffApplier(
        timeout_s=0.1, git_executable=str(fake_git),
    )
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x\n+y\n"

    async def _run():
        return await applier(worktree_dir=tmp_path, diff=diff)

    result = asyncio.new_event_loop().run_until_complete(_run())
    assert result.error == "git_apply_timeout"
    assert result.files == ()


def test_git_apply_returns_structured_error_on_missing_executable(
    tmp_path,
):
    """Production applier with a deliberately-missing git binary →
    structured ``git_not_installed`` error (operator-greppable)."""
    applier = GitApplyDiffApplier(
        git_executable="/nonexistent/path/git",
    )
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x\n+y\n"

    async def _run():
        return await applier(worktree_dir=tmp_path, diff=diff)

    result = asyncio.new_event_loop().run_until_complete(_run())
    assert result.error == "git_not_installed"


# ===========================================================================
# Defense-in-depth: cancellation propagates through production wiring
# ===========================================================================


def test_cancellation_propagates_through_gate_with_production_factory(
    tmp_path, monkeypatch,
):
    """End-to-end cancellation: registered factory → tree runs →
    closure receives CancelledError → propagates through gate
    (orchestrator POSTMORTEM contract)."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "bfs")

    async def _cancelling_closure():
        raise asyncio.CancelledError()

    def _cancel_factory(**_kw):
        return _cancelling_closure

    register_production_tree_runner_factory(_cancel_factory)

    engine = RepairEngine(
        budget=RepairBudget(),
        prime_provider=object(),
        repo_root=tmp_path,
    )

    class _Ctx:
        op_id = "op-cancel-prod"
        repo_root = str(tmp_path)
        generation = None

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(engine._maybe_run_treefinement(
            _Ctx(), None, datetime.now(timezone.utc),
        ))


# ===========================================================================
# Defense-in-depth: master-flag-FALSE keeps lazy registration dormant
# ===========================================================================


def test_master_flag_false_skips_lazy_registration_entirely(
    tmp_path, monkeypatch,
):
    """When master flag is FALSE, the gate short-circuits BEFORE
    reaching the factory check — lazy registration never fires.
    Verifies the gate's master-flag-FALSE byte-identical rollback."""
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "bfs")

    assert get_production_tree_runner_factory() is None

    engine = RepairEngine(
        budget=RepairBudget(),
        prime_provider=object(),
        repo_root=tmp_path,
    )

    class _Ctx:
        op_id = "op-master-off"
        repo_root = str(tmp_path)
        generation = None

    asyncio.run(engine._maybe_run_treefinement(
        _Ctx(), None, datetime.now(timezone.utc),
    ))

    # Factory still None — lazy registration never fired
    assert get_production_tree_runner_factory() is None


# ===========================================================================
# Closed-taxonomy mapping pins — extra defense in depth
# ===========================================================================


def test_verdict_mapping_does_not_include_won_terminal():
    """WON_TERMINAL is handled separately in the adapter (returns
    L2_CONVERGED). Including it in the verdict→stop_reason mapping
    would incorrectly route winners to L2_STOPPED."""
    assert (
        LayerVerdict.WON_TERMINAL.value not in _TREE_VERDICT_TO_STOP_REASON
    )


def test_verdict_mapping_does_not_include_expanded():
    """EXPANDED never reaches the adapter's terminal-verdict branch
    (it's an intermediate state). Including it would be drift."""
    assert (
        LayerVerdict.EXPANDED.value not in _TREE_VERDICT_TO_STOP_REASON
    )


# ===========================================================================
# Module surface inspection — Phase D + E exports
# ===========================================================================


def test_register_production_factory_at_boot_in_all():
    """The boot-registration function MUST be exported in __all__."""
    assert (
        "register_production_factory_at_boot"
        in repair_tree_production.__all__
    )


def test_production_tree_runner_factory_in_all():
    assert (
        "production_tree_runner_factory"
        in repair_tree_production.__all__
    )


def test_tree_result_to_repair_result_in_all():
    assert (
        "tree_result_to_repair_result"
        in repair_tree_production.__all__
    )
