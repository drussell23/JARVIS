"""Regression spine for AlphaVerus Treefinement L2 Phase 0 substrate.

Pins the Phase 0 contract:

* Four closed taxonomies (BranchingStrategy / BranchOutcome /
  LayerVerdict / PruningReason) — value sets bytes-pinned via AST
  walk so additions/removals fail the spine before runtime.
* Three frozen dataclasses (RepairBranch / RepairTreeLayer /
  RepairTreeResult) — symmetric to_dict / from_dict round-trip per
  §33.5.
* TreefinementBudget.from_env — defaults preserve LINEAR-equivalent
  behavior; malformed values fall back gracefully (NEVER raises).
* treefinement_enabled() — master flag default FALSE per §33.1
  graduation contract.
* RepairTreeRunner — Phase 0 ships skeleton only; run_tree raises
  NotImplementedError to make accidental wiring loud.
* Module never imports orchestrator/policy/iron_gate/change_engine
  (authority asymmetry per §1 Boundary).

Phase 1 wires execution; Phase 5 hardens with composition pins.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.governance import repair_tree
from backend.core.ouroboros.governance.repair_tree import (
    BEAM_WIDTH_ENV_VAR,
    BRANCH_DEDUP_ENV_VAR,
    CROSS_BRANCH_LEARNING_ENV_VAR,
    EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR,
    MASTER_FLAG_ENV_VAR,
    MAX_BRANCHES_PER_LAYER_ENV_VAR,
    REPAIR_TREE_SCHEMA_VERSION,
    STRATEGY_ENV_VAR,
    BranchingStrategy,
    BranchOutcome,
    LayerVerdict,
    PruningReason,
    RepairBranch,
    RepairTreeLayer,
    RepairTreeResult,
    RepairTreeRunner,
    TreefinementBudget,
    treefinement_enabled,
)


_MODULE_SRC = Path(inspect.getfile(repair_tree)).read_text(encoding="utf-8")
_MODULE_AST = ast.parse(_MODULE_SRC)


# ===========================================================================
# Closed taxonomy bytes-pinning (drift here = blind-alley risk regression)
# ===========================================================================


def _enum_values_from_ast(class_name: str) -> tuple[str, ...]:
    """Extract enum member values from the module AST.

    Walking the AST instead of the live class catches the case where
    someone refactors an enum into a non-Enum class — the AST pin
    fails before the spine even imports the class.
    """
    for node in ast.walk(_MODULE_AST):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            values: list[str] = []
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and isinstance(
                    stmt.value, ast.Constant
                ):
                    if isinstance(stmt.value.value, str):
                        values.append(stmt.value.value)
            return tuple(values)
    raise AssertionError(f"class {class_name!r} not found in module AST")


def test_branching_strategy_three_values_pinned():
    """LINEAR + BFS + BEAM_K. Adding a 4th strategy without spine
    update silently changes the operator decision surface."""
    expected = ("linear", "bfs", "beam_k")
    actual = _enum_values_from_ast("BranchingStrategy")
    assert actual == expected, (
        f"BranchingStrategy AST drift: expected {expected}, got "
        f"{actual} — adding a strategy requires a Phase tag + soak "
        "ladder, not a value-set patch"
    )
    # Live class agreement
    assert {m.value for m in BranchingStrategy} == set(expected)


def test_branch_outcome_five_values_pinned():
    """PROMOTED + 3 PRUNED_* + WON. Outcome taxonomy is the
    pruning-oracle contract; drift breaks the validator scoring."""
    expected = (
        "promoted",
        "pruned_validator",
        "pruned_duplicate",
        "pruned_budget",
        "won",
    )
    actual = _enum_values_from_ast("BranchOutcome")
    assert actual == expected, (
        f"BranchOutcome AST drift: expected {expected}, got {actual}"
    )
    assert {m.value for m in BranchOutcome} == set(expected)


def test_layer_verdict_four_values_pinned():
    """EXPANDED / EXHAUSTED / WON_TERMINAL / BUDGET_TERMINAL —
    closed verdict surface for layer aggregation."""
    expected = (
        "expanded",
        "exhausted",
        "won_terminal",
        "budget_terminal",
    )
    actual = _enum_values_from_ast("LayerVerdict")
    assert actual == expected
    assert {m.value for m in LayerVerdict} == set(expected)


def test_pruning_reason_six_values_pinned():
    """6 prune reasons — operator-visible diagnostic; drift makes
    historical IDE-GET lookups silently re-classify."""
    expected = (
        "duplicate_patch_sig",
        "worse_than_sibling",
        "validation_budget_exhausted",
        "wall_clock_cap",
        "semantic_guardian_hard_finding",
        "iron_gate_reject",
    )
    actual = _enum_values_from_ast("PruningReason")
    assert actual == expected
    assert {m.value for m in PruningReason} == set(expected)


# ===========================================================================
# Schema version + env vocabulary (string constants pinned by name only —
# the values are operator-facing env vars, drift = breaking change)
# ===========================================================================


def test_schema_version_constant():
    assert REPAIR_TREE_SCHEMA_VERSION == "repair_tree.v1"


def test_env_var_naming_cage():
    """All env knobs live under the JARVIS_L2_* prefix (§33.3
    naming-cage discipline). Master flag MUST follow the
    JARVIS_<SUBSTRATE>_ENABLED convention."""
    knobs = [
        MASTER_FLAG_ENV_VAR,
        STRATEGY_ENV_VAR,
        MAX_BRANCHES_PER_LAYER_ENV_VAR,
        BEAM_WIDTH_ENV_VAR,
        BRANCH_DEDUP_ENV_VAR,
        CROSS_BRANCH_LEARNING_ENV_VAR,
        EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR,
    ]
    for k in knobs:
        assert k.startswith("JARVIS_L2_"), (
            f"{k!r} violates JARVIS_L2_* naming cage"
        )
    assert MASTER_FLAG_ENV_VAR == "JARVIS_L2_TREEFINEMENT_ENABLED"


# ===========================================================================
# Frozen dataclass round-trip (symmetric to_dict / from_dict per §33.5)
# ===========================================================================


def _sample_branch(**overrides: Any) -> RepairBranch:
    base: dict[str, Any] = {
        "branch_id": "abcdef0123456789",
        "parent_branch_id": None,
        "layer_index": 0,
        "failure_class": "test",
        "fix_hypothesis": "rename foo to bar",
        "diff": "--- a\n+++ b\n",
        "validator_score": 0.75,
        "outcome": BranchOutcome.PROMOTED,
        "prune_reason": None,
        "worktree_id": "unit-abc",
        "cost_usd": 0.012,
        "validation_runs_consumed": 2,
    }
    base.update(overrides)
    return RepairBranch(**base)


def test_repair_branch_round_trip_promoted():
    branch = _sample_branch()
    payload = branch.to_dict()
    restored = RepairBranch.from_dict(payload)
    assert restored == branch
    assert payload["schema_version"] == REPAIR_TREE_SCHEMA_VERSION
    # Outcome serialized as string, not enum
    assert payload["outcome"] == "promoted"
    assert payload["prune_reason"] is None


def test_repair_branch_round_trip_pruned_with_reason():
    branch = _sample_branch(
        outcome=BranchOutcome.PRUNED_DUPLICATE,
        prune_reason=PruningReason.DUPLICATE_PATCH_SIG,
        parent_branch_id="parent_sig",
        layer_index=2,
    )
    payload = branch.to_dict()
    restored = RepairBranch.from_dict(payload)
    assert restored == branch
    assert payload["prune_reason"] == "duplicate_patch_sig"


def test_repair_branch_frozen():
    branch = _sample_branch()
    with pytest.raises((AttributeError, Exception)):
        branch.layer_index = 99  # type: ignore[misc]


def test_repair_tree_layer_round_trip():
    branches = (
        _sample_branch(branch_id="b1"),
        _sample_branch(
            branch_id="b2",
            outcome=BranchOutcome.PRUNED_VALIDATOR,
            prune_reason=PruningReason.WORSE_THAN_SIBLING,
        ),
    )
    layer = RepairTreeLayer(
        layer_index=0,
        branches=branches,
        verdict=LayerVerdict.EXPANDED,
        wall_ms=234.5,
        parallel_units_actual=2,
    )
    payload = layer.to_dict()
    restored = RepairTreeLayer.from_dict(payload)
    assert restored == layer
    assert len(payload["branches"]) == 2
    assert payload["verdict"] == "expanded"


def test_repair_tree_result_round_trip_won():
    branch = _sample_branch(outcome=BranchOutcome.WON, branch_id="winner")
    layer = RepairTreeLayer(
        layer_index=0,
        branches=(branch,),
        verdict=LayerVerdict.WON_TERMINAL,
        wall_ms=100.0,
        parallel_units_actual=1,
    )
    result = RepairTreeResult(
        root_op_id="op-test-1",
        layers=(layer,),
        winning_branch_path=("winner",),
        final_status={"terminal": "L2_CONVERGED"},
    )
    payload = result.to_dict()
    restored = RepairTreeResult.from_dict(payload)
    assert restored == result
    assert payload["winning_branch_path"] == ["winner"]
    assert payload["final_status"]["terminal"] == "L2_CONVERGED"


def test_repair_tree_result_round_trip_exhausted():
    """Exhausted tree has empty winning_branch_path; final_status None."""
    branch = _sample_branch(
        outcome=BranchOutcome.PRUNED_VALIDATOR,
        prune_reason=PruningReason.VALIDATION_BUDGET_EXHAUSTED,
    )
    layer = RepairTreeLayer(
        layer_index=0,
        branches=(branch,),
        verdict=LayerVerdict.EXHAUSTED,
        wall_ms=50.0,
        parallel_units_actual=1,
    )
    result = RepairTreeResult(
        root_op_id="op-test-2",
        layers=(layer,),
        winning_branch_path=(),
        final_status=None,
    )
    payload = result.to_dict()
    restored = RepairTreeResult.from_dict(payload)
    assert restored == result
    assert payload["winning_branch_path"] == []
    assert payload["final_status"] is None


# ===========================================================================
# TreefinementBudget — env loader (NEVER raises; defaults preserve LINEAR)
# ===========================================================================


def test_budget_defaults_preserve_linear(monkeypatch):
    """No env vars set → LINEAR strategy + master flag FALSE.
    This is the load-bearing rollback invariant — if defaults
    drifted, every soak booted into tree mode silently."""
    for k in [
        MASTER_FLAG_ENV_VAR,
        STRATEGY_ENV_VAR,
        MAX_BRANCHES_PER_LAYER_ENV_VAR,
        BEAM_WIDTH_ENV_VAR,
        BRANCH_DEDUP_ENV_VAR,
        CROSS_BRANCH_LEARNING_ENV_VAR,
        EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR,
    ]:
        monkeypatch.delenv(k, raising=False)
    budget = TreefinementBudget.from_env()
    assert budget.enabled is False
    assert budget.branching_strategy == BranchingStrategy.LINEAR
    assert budget.max_branches_per_layer == 3
    assert budget.beam_width == 2
    assert budget.branch_dedup_enabled is True
    assert budget.cross_branch_learning_enabled is True
    assert budget.emergency_demote_threshold == 0.85


def test_budget_env_overrides_applied(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(STRATEGY_ENV_VAR, "bfs")
    monkeypatch.setenv(MAX_BRANCHES_PER_LAYER_ENV_VAR, "4")
    monkeypatch.setenv(BEAM_WIDTH_ENV_VAR, "3")
    monkeypatch.setenv(BRANCH_DEDUP_ENV_VAR, "false")
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "false")
    monkeypatch.setenv(EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR, "0.75")

    budget = TreefinementBudget.from_env()
    assert budget.enabled is True
    assert budget.branching_strategy == BranchingStrategy.BFS
    assert budget.max_branches_per_layer == 4
    assert budget.beam_width == 3
    assert budget.branch_dedup_enabled is False
    assert budget.cross_branch_learning_enabled is False
    assert budget.emergency_demote_threshold == 0.75


def test_budget_invalid_strategy_falls_back_to_linear(monkeypatch):
    """Malformed strategy value MUST fall back to LINEAR — the
    safe default that preserves byte-identical legacy behavior."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(STRATEGY_ENV_VAR, "treefinement")  # invalid

    budget = TreefinementBudget.from_env()
    assert budget.branching_strategy == BranchingStrategy.LINEAR


def test_budget_int_clamps_apply(monkeypatch):
    """K and beam_width clamped to [1, 16] — pathological env
    values can't produce K=10000 trees."""
    monkeypatch.setenv(MAX_BRANCHES_PER_LAYER_ENV_VAR, "999")
    monkeypatch.setenv(BEAM_WIDTH_ENV_VAR, "0")

    budget = TreefinementBudget.from_env()
    assert budget.max_branches_per_layer == 16  # ceiling
    assert budget.beam_width == 1               # floor


def test_budget_malformed_int_falls_back(monkeypatch):
    monkeypatch.setenv(MAX_BRANCHES_PER_LAYER_ENV_VAR, "not-a-number")
    budget = TreefinementBudget.from_env()
    assert budget.max_branches_per_layer == 3  # default


def test_budget_threshold_clamped_to_unit_interval(monkeypatch):
    monkeypatch.setenv(EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR, "1.5")
    budget = TreefinementBudget.from_env()
    assert budget.emergency_demote_threshold == 1.0

    monkeypatch.setenv(EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR, "-0.5")
    budget = TreefinementBudget.from_env()
    assert budget.emergency_demote_threshold == 0.0


def test_budget_from_env_never_raises_under_garbage(monkeypatch):
    """Adversarial env state — every knob malformed. from_env MUST
    return a valid budget with defaults applied."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "")
    monkeypatch.setenv(STRATEGY_ENV_VAR, "%%%")
    monkeypatch.setenv(MAX_BRANCHES_PER_LAYER_ENV_VAR, "infinity")
    monkeypatch.setenv(BEAM_WIDTH_ENV_VAR, "NaN")
    monkeypatch.setenv(BRANCH_DEDUP_ENV_VAR, "maybe")
    monkeypatch.setenv(CROSS_BRANCH_LEARNING_ENV_VAR, "")
    monkeypatch.setenv(EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR, "elephant")

    # MUST NOT raise
    budget = TreefinementBudget.from_env()
    assert isinstance(budget, TreefinementBudget)
    assert budget.branching_strategy == BranchingStrategy.LINEAR


# ===========================================================================
# Master flag accessor
# ===========================================================================


def test_treefinement_enabled_default_false(monkeypatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    assert treefinement_enabled() is False


def test_treefinement_enabled_respects_env(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    assert treefinement_enabled() is True
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    assert treefinement_enabled() is False
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "0")
    assert treefinement_enabled() is False


# ===========================================================================
# RepairTreeRunner — Phase 0 skeleton contract
# ===========================================================================


def test_runner_construction_accepts_dependencies():
    """Constructor MUST accept the three injection points (repair
    budget, worktree manager, clock) for test composability. Phase 1
    runner needs all three to be substitutable."""
    budget = TreefinementBudget.from_env()
    runner = RepairTreeRunner(
        budget,
        repair_budget=object(),
        worktree_manager=object(),
        clock=lambda: 42.0,
    )
    assert runner.budget is budget
    assert runner._repair_budget is not None
    assert runner._worktree_manager is not None
    assert runner._clock() == 42.0


def test_runner_default_clock_is_monotonic():
    """When no clock injected, default is time.monotonic — the §11
    monotonic-clock discipline (sleep/suspend immune)."""
    import time as _time
    budget = TreefinementBudget.from_env()
    runner = RepairTreeRunner(budget)
    assert runner._clock is _time.monotonic


def test_run_tree_raises_not_implemented_phase0():
    """Phase 0 ships substrate skeleton only. Accidental wiring of
    run_tree() in production MUST be loud. Phase 1 replaces this."""
    budget = TreefinementBudget.from_env()
    runner = RepairTreeRunner(budget)

    async def _invoke():
        await runner.run_tree()

    with pytest.raises(NotImplementedError) as exc_info:
        asyncio.run(_invoke())
    assert MASTER_FLAG_ENV_VAR in str(exc_info.value), (
        "Phase 0 NotImplementedError MUST mention the master flag — "
        "operators reading the traceback need the override path"
    )


# ===========================================================================
# Authority asymmetry — no policy / orchestrator imports (§1 Boundary)
# ===========================================================================


def test_module_does_not_import_authority_substrates():
    """RepairTree is descriptive substrate. Drift toward orchestrator
    or policy imports collapses §1 Boundary — branches would gain
    authority over GENERATE / VALIDATE / APPLY which is the very
    invariant the parallel exploration depends on staying inert."""
    forbidden = (
        "orchestrator",
        "iron_gate",
        "change_engine",
        "candidate_generator",
        "policy_engine",
        "risk_tier",
    )
    imports: list[str] = []
    for node in ast.walk(_MODULE_AST):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imports.append(mod)
    for f in forbidden:
        for imp in imports:
            assert f not in imp, (
                f"repair_tree.py MUST NOT import {f!r} — found in "
                f"{imp!r}. This collapses §1 Boundary."
            )


def test_module_does_not_define_parallel_signature_function():
    """The branch_id derivation MUST compose repair_engine._patch_sig
    in Phase 1 — Phase 0 verifies no parallel signature primitive
    has been defined here as a shortcut."""
    for node in ast.walk(_MODULE_AST):
        if isinstance(node, ast.FunctionDef):
            assert "patch_sig" not in node.name.lower(), (
                f"function {node.name!r} suggests a parallel "
                "signature primitive; Phase 1 MUST compose "
                "repair_engine._patch_sig instead"
            )


def test_module_does_not_define_parallel_budget_envelope():
    """The shared validation envelope MUST stay on RepairBudget.
    Phase 0 verifies TreefinementBudget does NOT define
    max_total_validation_runs (single-source invariant)."""
    fields = {f.name for f in TreefinementBudget.__dataclass_fields__.values()}
    assert "max_total_validation_runs" not in fields, (
        "TreefinementBudget MUST NOT carry max_total_validation_runs "
        "— that knob lives on RepairBudget (shared envelope per §1 "
        "Boundary). Composition, not duplication."
    )
    assert "timebox_s" not in fields
    assert "per_iteration_test_timeout_s" not in fields
