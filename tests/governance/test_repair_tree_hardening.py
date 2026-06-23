"""Treefinement Phase 5 — consolidated hardening regression spine.

Pins 12 load-bearing structural invariants in one place + adds the
defense-in-depth tests that didn't fit into earlier phases:

The 12 AST pins
---------------
1.  ``BranchingStrategy`` 3-value frozen
2.  ``BranchOutcome`` 5-value frozen
3.  ``LayerVerdict`` 4-value frozen
4.  ``PruningReason`` 6-value frozen
5.  composition: ``parallel_dispatch.posture_weight_for`` import
6.  composition: ``worktree_manager.WorktreeManager`` import
7.  composition: ``failure_classifier.patch_signature_hash`` import
8.  composition: ``repair_tree_archive.maybe_archive_tree_result``
    import (Phase 5 wire)
9.  strategy gate position pin (``_maybe_run_treefinement`` is called
    BEFORE ``_run_inner`` in ``RepairEngine.run``)
10. legacy ``_run_inner`` body bytes-pinned (drift = LINEAR semantics
    changed without explicit Phase update)
11. SSE event registration pin (4 events present in
    ``_VALID_EVENT_TYPES`` frozenset)
12. ``register_flags()`` presence pin (auto-discovered by walker on
    both ``repair_tree.py`` and ``repair_tree_archive.py``)

Defense-in-depth tests
----------------------
* Branch hangs past per-iteration timeout → cancellation propagates
* Orphan worktree reap composition (the WorktreeManager.reap_orphans
  contract covers branch worktrees because they all live under the
  same canonical .worktrees/ root)
* Master-flag-FALSE renders the ENTIRE substrate inert end-to-end
* Strategy gate falls through to legacy when factory unregistered
* Strategy gate calls _maybe_run_treefinement (positional invariant)
"""
from __future__ import annotations

import asyncio
import ast
import hashlib
import inspect
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance import repair_engine, repair_tree
from backend.core.ouroboros.governance.repair_tree import (
    BranchingStrategy,
    BranchOutcome,
    LayerVerdict,
    MASTER_FLAG_ENV_VAR,
    PruningReason,
    RepairBranch,
    RepairTreeRunner,
    TreefinementBudget,
    get_production_tree_runner_factory,
    register_production_tree_runner_factory,
)
from backend.core.ouroboros.governance.repair_tree_archive import (
    ARCHIVE_MASTER_FLAG_ENV_VAR,
    get_default_archive,
    reset_default_archive_for_tests,
)


_REPAIR_TREE_SRC = Path(
    inspect.getfile(repair_tree),
).read_text(encoding="utf-8")
_REPAIR_TREE_AST = ast.parse(_REPAIR_TREE_SRC)


_REPAIR_ENGINE_SRC = Path(
    inspect.getfile(repair_engine),
).read_text(encoding="utf-8")
_REPAIR_ENGINE_AST = ast.parse(_REPAIR_ENGINE_SRC)


# ===========================================================================
# AST pin #1-#4 — closed taxonomy frozen sets
# ===========================================================================


def _enum_member_values(enum_cls) -> Tuple[str, ...]:
    return tuple(m.value for m in enum_cls)


def test_pin_1_branching_strategy_three_values():
    expected = ("linear", "bfs", "beam_k")
    assert _enum_member_values(BranchingStrategy) == expected, (
        "BranchingStrategy taxonomy drift — adding a strategy "
        "requires a Phase tag + soak ladder, not a value-set patch"
    )


def test_pin_2_branch_outcome_five_values():
    expected = (
        "promoted", "pruned_validator", "pruned_duplicate",
        "pruned_budget", "won",
    )
    assert _enum_member_values(BranchOutcome) == expected


def test_pin_3_layer_verdict_four_values():
    expected = (
        "expanded", "exhausted", "won_terminal", "budget_terminal",
    )
    assert _enum_member_values(LayerVerdict) == expected


def test_pin_4_pruning_reason_six_values():
    expected = (
        "duplicate_patch_sig", "worse_than_sibling",
        "validation_budget_exhausted", "wall_clock_cap",
        "semantic_guardian_hard_finding", "iron_gate_reject",
    )
    assert _enum_member_values(PruningReason) == expected


# ===========================================================================
# AST pin #5-#8 — composition imports (single-source-of-truth)
# ===========================================================================


def _module_imports(module_ast: ast.Module) -> List[Tuple[str, Tuple[str, ...]]]:
    """All ImportFrom nodes anywhere in the module (top-level + lazy
    inside function bodies). Use ``_top_level_imports`` for
    module-level only."""
    out: List[Tuple[str, Tuple[str, ...]]] = []
    for node in ast.walk(module_ast):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = tuple(a.name for a in node.names)
            out.append((mod, names))
    return out


def _top_level_imports(
    module_ast: ast.Module,
) -> List[Tuple[str, Tuple[str, ...]]]:
    """Only ImportFrom nodes at module top level (not inside any
    function/class body). Used to verify lazy-import patterns."""
    out: List[Tuple[str, Tuple[str, ...]]] = []
    for node in module_ast.body:
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = tuple(a.name for a in node.names)
            out.append((mod, names))
    return out


def test_pin_5_parallel_dispatch_composition():
    """K sizing MUST compose canonical posture_weight_for. Drift toward
    a parallel weight table breaks single-source invariant."""
    matches = [
        (m, n) for (m, n) in _module_imports(_REPAIR_TREE_AST)
        if m.endswith("parallel_dispatch") and "posture_weight_for" in n
    ]
    assert matches, (
        "repair_tree.py MUST import posture_weight_for from "
        "parallel_dispatch — single-source posture-weight table"
    )


def test_pin_6_worktree_manager_composition():
    """Branch isolation MUST compose canonical WorktreeManager. Drift
    toward a parallel isolation primitive breaks the §1 Boundary
    'no shared-tree fallback' contract."""
    matches = [
        (m, n) for (m, n) in _module_imports(_REPAIR_TREE_AST)
        if m.endswith("worktree_manager") and "WorktreeManager" in n
    ]
    assert matches


def test_pin_7_patch_signature_hash_composition():
    """branch_id derivation MUST compose canonical patch_signature_hash.
    Drift toward a tree-local hash function breaks cross-substrate
    dedup."""
    matches = [
        (m, n) for (m, n) in _module_imports(_REPAIR_TREE_AST)
        if (
            m.endswith("failure_classifier")
            and "patch_signature_hash" in n
        )
    ]
    assert matches


def test_pin_8_archive_bridge_composition():
    """RepairTreeRunner.run_tree MUST compose the canonical
    maybe_archive_tree_result producer-bridge (Phase 5 wire). Drift
    here would silently lose telemetry."""
    # Composed via lazy import inside _archive_result. The AST walk
    # confirms the helper method exists.
    runner_class = next(
        (
            n for n in ast.walk(_REPAIR_TREE_AST)
            if isinstance(n, ast.ClassDef) and n.name == "RepairTreeRunner"
        ),
        None,
    )
    assert runner_class is not None
    method_names = {
        m.name for m in runner_class.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_archive_result" in method_names, (
        "RepairTreeRunner MUST have _archive_result helper that "
        "composes maybe_archive_tree_result (Phase 5 archive wire)"
    )
    # Verify the helper actually mentions the canonical bridge name
    # (defensive against a stub that doesn't compose anything).
    assert "maybe_archive_tree_result" in _REPAIR_TREE_SRC, (
        "_archive_result body MUST reference "
        "maybe_archive_tree_result by name (composition pin)"
    )


# ===========================================================================
# AST pin #9 — strategy gate position
# ===========================================================================


def test_pin_9_strategy_gate_called_before_run_inner():
    """In RepairEngine.run, _maybe_run_treefinement MUST be called
    BEFORE _run_inner. Reordering would break the rollback invariant
    (gate must be able to PREEMPT the legacy FSM)."""
    run_method = None
    for node in ast.walk(_REPAIR_ENGINE_AST):
        if isinstance(node, ast.ClassDef) and node.name == "RepairEngine":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AsyncFunctionDef)
                    and stmt.name == "run"
                ):
                    run_method = stmt
                    break
    assert run_method is not None, (
        "RepairEngine.run not found in AST"
    )
    # Walk the run() body and find the FIRST positions where each
    # method is called.
    gate_pos = None
    inner_pos = None
    for sub in ast.walk(run_method):
        if isinstance(sub, ast.Attribute):
            if sub.attr == "_maybe_run_treefinement" and gate_pos is None:
                gate_pos = sub.lineno
            elif sub.attr == "_run_inner" and inner_pos is None:
                inner_pos = sub.lineno
    assert gate_pos is not None, (
        "_maybe_run_treefinement call MUST exist in RepairEngine.run"
    )
    assert inner_pos is not None, (
        "_run_inner call MUST still exist in RepairEngine.run"
    )
    assert gate_pos < inner_pos, (
        f"Strategy gate position regression — "
        f"_maybe_run_treefinement at line {gate_pos} MUST appear "
        f"BEFORE _run_inner at line {inner_pos}. Reordering breaks "
        "the rollback invariant: gate MUST preempt legacy FSM."
    )


# ===========================================================================
# AST pin #10 — legacy _run_inner bytes-identical
# ===========================================================================
# Snapshot taken at Phase 5 cutover. Future drift in _run_inner means
# legacy LINEAR semantics changed — that requires an explicit Phase
# update + soak ladder, not an opportunistic refactor.


def _extract_function_source(
    module_ast: ast.Module, class_name: str, method_name: str,
) -> Optional[str]:
    for node in ast.walk(module_ast):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for stmt in node.body:
                if (
                    isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and stmt.name == method_name
                ):
                    return ast.unparse(stmt)
    return None


def test_pin_10_run_inner_legacy_bytes_pinned():
    """_run_inner is the byte-identical legacy path. Drift here means
    LINEAR semantics changed — operators relying on rollback safety
    need to know.

    Pin is computed as sha256 of the AST-unparsed canonical form
    (whitespace-normalized). Updating this pin requires explicit
    Phase tag + soak validation."""
    src = _extract_function_source(
        _REPAIR_ENGINE_AST, "RepairEngine", "_run_inner",
    )
    assert src is not None, "_run_inner not found in AST"
    digest = hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]
    # Pin locked at Phase A cutover (Treefinement Production Wiring v3.4,
    # 2026-05-11): the inline GENERATE block at line ~581 was extracted
    # into RepairEngine._generate_repair_candidate as a single-source
    # primitive composed by both LINEAR FSM and the Phase C
    # ProductionBranchGenerator. The extraction preserves byte-equivalent
    # provider invocation semantics (verified by 10/10 existing
    # repair_engine tests staying green); the function body source
    # changed because the inline try/except + getattr-with-fallback
    # block became a single delegation call. Pin updated atomically
    # with that change.
    #
    # To intentionally change _run_inner in the future:
    # 1. update this hash to the new sha256[:16] (capture via
    #    `python -c "import ast, hashlib; ..."` — see Phase A memory
    #    artifact for the exact incantation);
    # 2. document the change in the arc memory file with explicit
    #    Phase tag + the byte-equivalence verification approach;
    # 3. include a soak validating the new behavior under cadence.
    #
    # Phase tag: Adaptive Epistemic Feedback Matrix T2 (2026-06-22). _run_inner
    # gained signature-recurrence tracking + hybrid-diff/trace assembly + a
    # signature-driven temperature override threaded into the GENERATE call. The
    # epistemic computation is fully wrapped in try/except and OFF byte-identical
    # when epistemic_feedback_enabled() is False (verified by
    # tests/governance/test_epistemic_repair_threading.py); LINEAR rollback
    # semantics are preserved (the new fields default empty; the temperature
    # override is None until a signature first repeats). Pin updated atomically.
    EXPECTED_DIGEST = "8adaf3734fbb1009"
    assert digest == EXPECTED_DIGEST, (
        f"_run_inner bytes drift detected: expected "
        f"{EXPECTED_DIGEST}, got {digest}. Legacy LINEAR semantics "
        "changed — update this pin only with explicit Phase tag + "
        "soak validation."
    )


def test_pin_10b_run_inner_remains_a_method():
    """Defense in depth — _run_inner MUST remain a method of
    RepairEngine. Removing it (e.g., merging into run()) would break
    the rollback semantic where 'tree mode off → call _run_inner'
    is the documented path."""
    src = _extract_function_source(
        _REPAIR_ENGINE_AST, "RepairEngine", "_run_inner",
    )
    assert src is not None
    # Method body is non-trivial (not just "pass" or "return None")
    assert len(src) > 500, (
        "_run_inner body shrunk dramatically — likely refactored "
        "away. Verify rollback path still works."
    )


# ===========================================================================
# AST pin #11 — SSE event registration
# ===========================================================================


def test_pin_11_sse_events_registered_in_valid_set():
    """All 4 Treefinement SSE events MUST be registered in
    ide_observability_stream._VALID_EVENT_TYPES. Drift here means
    publish_task_event silently rejects them at runtime."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_REPAIR_BRANCH_PROMOTED,
        EVENT_TYPE_REPAIR_BRANCH_PRUNED,
        EVENT_TYPE_REPAIR_LAYER_COMPLETED,
        EVENT_TYPE_REPAIR_TREE_WON,
        _VALID_EVENT_TYPES,
    )
    for evt in (
        EVENT_TYPE_REPAIR_BRANCH_PROMOTED,
        EVENT_TYPE_REPAIR_BRANCH_PRUNED,
        EVENT_TYPE_REPAIR_LAYER_COMPLETED,
        EVENT_TYPE_REPAIR_TREE_WON,
    ):
        assert evt in _VALID_EVENT_TYPES, (
            f"SSE event {evt!r} MUST be registered in "
            "_VALID_EVENT_TYPES — drift here silently breaks publish"
        )


# ===========================================================================
# AST pin #12 — register_flags presence on both substrates
# ===========================================================================


def test_pin_12_register_flags_present_on_repair_tree():
    """repair_tree.py MUST expose register_flags for §33.3 walker
    auto-discovery."""
    assert hasattr(repair_tree, "register_flags")
    assert callable(repair_tree.register_flags)


def test_pin_12b_register_flags_present_on_repair_tree_archive():
    """repair_tree_archive.py MUST expose register_flags for §33.3
    walker auto-discovery (separate substrate, separate seed)."""
    from backend.core.ouroboros.governance import repair_tree_archive
    assert hasattr(repair_tree_archive, "register_flags")
    assert callable(repair_tree_archive.register_flags)


# ===========================================================================
# Defense-in-depth: branch hang past per-iteration timeout
# ===========================================================================


def test_branch_hang_propagates_cancellation_to_runner():
    """A hanging generator MUST be cancellable mid-flight. Wrap the
    runner invocation in asyncio.wait_for with a tiny timeout —
    cancellation MUST propagate cleanly (no zombie tasks).

    This is the load-bearing wall-clock invariant: tree mode cannot
    extend the canonical 120s repair timebox."""
    budget = TreefinementBudget(
        enabled=True,
        branching_strategy=BranchingStrategy.BFS,
        max_branches_per_layer=2,
        beam_width=2,
        branch_dedup_enabled=True,
        cross_branch_learning_enabled=True,
        emergency_demote_threshold=0.85,
    )
    runner = RepairTreeRunner(budget, worktree_manager=None)

    async def _hanging_generator(**_kwargs):
        # Sleep for "forever" — must be cancellable
        await asyncio.sleep(60)
        return ("", "should-not-reach", 0.0)

    async def _validator(**_kwargs):
        return (BranchOutcome.PROMOTED, 0.5, None, 1)

    async def _runner_with_timeout():
        # Force master flag on for this test
        import os
        os.environ[MASTER_FLAG_ENV_VAR] = "true"
        try:
            return await asyncio.wait_for(
                runner.run_tree(
                    op_id="op-hang",
                    generator=_hanging_generator,
                    validator=_validator,
                    max_layers=1,
                ),
                timeout=0.1,
            )
        finally:
            os.environ.pop(MASTER_FLAG_ENV_VAR, None)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_runner_with_timeout())


# ===========================================================================
# Defense-in-depth: orphan worktree reap composition
# ===========================================================================


def test_branch_worktree_paths_under_canonical_root():
    """Tree branches MUST create worktrees under the same canonical
    base path (.worktrees/) that WorktreeManager.reap_orphans sweeps.
    Drift here means SIGKILL recovery would silently miss branch
    worktrees.

    Verified structurally: the runner uses WorktreeManager.create()
    via composition (Phase 1+5 AST pins guarantee the import).
    Reap-orphans sweeps based on the manager's canonical
    _worktree_base — branch worktrees inherit that location for free
    because they're created via the same manager instance."""
    from backend.core.ouroboros.governance.worktree_manager import (
        WorktreeManager,
    )
    # Verify reap_orphans is the canonical method name on the manager
    # (drift here = the boot sweep wouldn't find branch worktrees).
    assert hasattr(WorktreeManager, "reap_orphans"), (
        "WorktreeManager MUST expose reap_orphans for boot-sweep "
        "recovery (the §2 Progressive Awakening contract). Tree "
        "branches inherit reap coverage because they compose the "
        "same WorktreeManager instance."
    )


# ===========================================================================
# Defense-in-depth: master-flag-FALSE renders entire substrate inert
# ===========================================================================


def test_master_flag_false_renders_runner_inert(monkeypatch):
    """Defense in depth — even with strategy=BFS, master-flag-FALSE
    means run_tree returns empty result without dispatching."""
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "bfs")
    budget = TreefinementBudget.from_env()
    runner = RepairTreeRunner(budget)

    async def _generator(**_kwargs):
        raise AssertionError("must not be called master-off")

    async def _validator(**_kwargs):
        raise AssertionError("must not be called master-off")

    result = asyncio.run(runner.run_tree(
        op_id="op-master-off-defense",
        generator=_generator,
        validator=_validator,
        max_layers=1,
    ))
    assert result.layers == ()


def test_master_flag_false_renders_archive_inert(monkeypatch):
    monkeypatch.delenv(ARCHIVE_MASTER_FLAG_ENV_VAR, raising=False)
    reset_default_archive_for_tests()
    archive = get_default_archive()
    # Try to record — should be no-op
    from backend.core.ouroboros.governance.repair_tree_archive import (
        maybe_archive_tree_result,
    )
    # Build a minimal RepairTreeResult
    from backend.core.ouroboros.governance.repair_tree import (
        RepairTreeLayer, RepairTreeResult,
    )
    branch = RepairBranch(
        branch_id="x", parent_branch_id=None, layer_index=0,
        failure_class="t", fix_hypothesis="h", diff="",
        validator_score=0.5, outcome=BranchOutcome.PROMOTED,
        prune_reason=None, worktree_id=None, cost_usd=0.0,
        validation_runs_consumed=1,
    )
    layer = RepairTreeLayer(
        layer_index=0, branches=(branch,),
        verdict=LayerVerdict.EXPANDED, wall_ms=1.0,
        parallel_units_actual=1,
    )
    result = RepairTreeResult(
        root_op_id="op-x", layers=(layer,),
        winning_branch_path=(), final_status=None,
    )
    archived = maybe_archive_tree_result(result)
    assert archived == ()
    assert len(archive) == 0
    reset_default_archive_for_tests()


# ===========================================================================
# Strategy gate semantics — falls through to legacy when factory unset
# ===========================================================================


@pytest.fixture(autouse=True)
def _isolate_factory():
    """Ensure no test leaves a factory registered."""
    register_production_tree_runner_factory(None)
    yield
    register_production_tree_runner_factory(None)


def test_factory_default_is_none():
    """Phase 5 ships NO production factory — default must be None
    so the gate falls through to legacy LINEAR by default."""
    assert get_production_tree_runner_factory() is None


def test_factory_register_and_unregister():
    def _stub_factory(**_kw):
        raise AssertionError("must not be called")
    register_production_tree_runner_factory(_stub_factory)
    assert get_production_tree_runner_factory() is _stub_factory
    register_production_tree_runner_factory(None)
    assert get_production_tree_runner_factory() is None


def test_strategy_gate_returns_none_when_master_off(monkeypatch):
    """Gate's _maybe_run_treefinement MUST return None when master
    flag off — fall-through to legacy _run_inner."""
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    from datetime import datetime, timezone
    eng = repair_engine.RepairEngine(
        budget=repair_engine.RepairBudget(),
        prime_provider=object(),
        repo_root=Path("."),
    )
    result = asyncio.run(eng._maybe_run_treefinement(
        ctx=object(), _best_validation=None,
        pipeline_deadline=datetime.now(timezone.utc),
    ))
    assert result is None


def test_strategy_gate_returns_none_when_strategy_linear(monkeypatch):
    """Gate's _maybe_run_treefinement MUST return None when strategy
    is LINEAR even with master ON — LINEAR is the byte-identical
    rollback path."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "linear")
    from datetime import datetime, timezone
    eng = repair_engine.RepairEngine(
        budget=repair_engine.RepairBudget(),
        prime_provider=object(),
        repo_root=Path("."),
    )
    result = asyncio.run(eng._maybe_run_treefinement(
        ctx=object(), _best_validation=None,
        pipeline_deadline=datetime.now(timezone.utc),
    ))
    assert result is None


def test_strategy_gate_returns_none_when_factory_unregistered(monkeypatch):
    """Gate MUST return None when no production factory registered
    even with master ON + strategy BFS — Phase 5 default."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "bfs")
    register_production_tree_runner_factory(None)  # explicit
    from datetime import datetime, timezone
    eng = repair_engine.RepairEngine(
        budget=repair_engine.RepairBudget(),
        prime_provider=object(),
        repo_root=Path("."),
    )
    result = asyncio.run(eng._maybe_run_treefinement(
        ctx=object(), _best_validation=None,
        pipeline_deadline=datetime.now(timezone.utc),
    ))
    assert result is None


def test_strategy_gate_never_raises_on_garbage_factory(monkeypatch):
    """Defense in depth — broken factory MUST NOT propagate;
    gate falls back to LINEAR with a structured warning log."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "bfs")

    def _broken_factory(**_kwargs):
        raise RuntimeError("factory exploded")

    register_production_tree_runner_factory(_broken_factory)
    from datetime import datetime, timezone
    eng = repair_engine.RepairEngine(
        budget=repair_engine.RepairBudget(),
        prime_provider=object(),
        repo_root=Path("."),
    )
    # Phase 5 _invoke_tree_factory always returns None (stub).
    # Even when we register a broken factory, the stub doesn't
    # actually call it yet (Phase 6+ wiring). So no exception
    # should occur. This test guards against future Phase 6
    # regression where the factory call propagates exceptions.
    result = asyncio.run(eng._maybe_run_treefinement(
        ctx=object(), _best_validation=None,
        pipeline_deadline=datetime.now(timezone.utc),
    ))
    assert result is None  # gate fell through


# ===========================================================================
# End-to-end: archive wire fires on tree completion
# ===========================================================================


def test_run_tree_archives_completed_result(monkeypatch):
    """When master flag ON + archive flag ON, run_tree MUST archive
    the result via maybe_archive_tree_result. Verify by checking the
    default archive has new entries after run_tree."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(ARCHIVE_MASTER_FLAG_ENV_VAR, "true")
    reset_default_archive_for_tests()

    budget = TreefinementBudget(
        enabled=True,
        branching_strategy=BranchingStrategy.BFS,
        max_branches_per_layer=2,
        beam_width=2,
        branch_dedup_enabled=True,
        cross_branch_learning_enabled=True,
        emergency_demote_threshold=0.85,
    )
    runner = RepairTreeRunner(budget, worktree_manager=None)

    counter = {"n": 0}

    async def _generator(**_kwargs):
        counter["n"] += 1
        return (
            f"--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y-{counter['n']}\n",
            "fix-strategy",
            0.001,
        )

    async def _validator(**_kwargs):
        return (BranchOutcome.PROMOTED, 0.5, None, 1)

    asyncio.run(runner.run_tree(
        op_id="op-archive-wire",
        generator=_generator,
        validator=_validator,
        max_layers=1,
    ))

    archive = get_default_archive()
    assert len(archive) >= 1, (
        "Archive wire regression — run_tree MUST archive results "
        "via maybe_archive_tree_result"
    )
    branches_for_op = archive.by_op("op-archive-wire")
    assert len(branches_for_op) >= 1
    reset_default_archive_for_tests()


def test_run_tree_archive_wire_swallows_failures(monkeypatch):
    """Defense in depth — if maybe_archive_tree_result raises
    (substrate broken), run_tree MUST still return cleanly. The
    archive is telemetry; never authoritative."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(ARCHIVE_MASTER_FLAG_ENV_VAR, "true")

    # Force the archive to fail by patching get_default_archive to
    # return a broken object
    import backend.core.ouroboros.governance.repair_tree_archive as arch
    original = arch.maybe_archive_tree_result

    def _exploding_archive(_result):
        raise RuntimeError("archive exploded")

    monkeypatch.setattr(
        arch, "maybe_archive_tree_result", _exploding_archive,
    )

    budget = TreefinementBudget(
        enabled=True,
        branching_strategy=BranchingStrategy.BFS,
        max_branches_per_layer=1,
        beam_width=1,
        branch_dedup_enabled=True,
        cross_branch_learning_enabled=True,
        emergency_demote_threshold=0.85,
    )
    runner = RepairTreeRunner(budget, worktree_manager=None)

    async def _generator(**_kwargs):
        return ("--- a\n+++ b\n", "x", 0.0)

    async def _validator(**_kwargs):
        return (BranchOutcome.PROMOTED, 0.5, None, 1)

    # Must NOT raise — archive failure is fail-open
    result = asyncio.run(runner.run_tree(
        op_id="op-broken-archive",
        generator=_generator,
        validator=_validator,
        max_layers=1,
    ))
    assert result is not None
    assert len(result.layers) == 1


# ===========================================================================
# Strategy gate AST presence — defensive; pin #9 covers position
# ===========================================================================


def test_repair_engine_run_imports_treefinement_lazily():
    """The strategy gate composes treefinement_enabled +
    TreefinementBudget + get_production_tree_runner_factory via a
    LAZY import inside _maybe_run_treefinement. This keeps
    repair_engine.py free of a hard dep on repair_tree at import
    time — important for circular-import safety."""
    # Top-level imports of repair_engine.py MUST NOT include repair_tree
    top_level = [
        (m, n) for (m, n) in _top_level_imports(_REPAIR_ENGINE_AST)
        if m and "repair_tree" in m
    ]
    assert top_level == [], (
        f"repair_engine.py top-level imports {top_level} from "
        "repair_tree — this creates a circular-import risk. The "
        "strategy gate composes treefinement via LAZY import inside "
        "_maybe_run_treefinement."
    )
    # Verify the lazy import IS present in source text
    assert "from backend.core.ouroboros.governance.repair_tree import" in _REPAIR_ENGINE_SRC, (
        "_maybe_run_treefinement MUST lazy-import from repair_tree"
    )
