"""Regression spine for Treefinement Phase 4 Slice 4d —
``/repair_tree`` REPL dispatcher.

Pins the §33.3 naming-cage discipline (filename → verb auto-
discovery), master-flag-gated subcommand surface, and the
canonical defer-to-archive composition pattern.
"""
from __future__ import annotations

from typing import List

import pytest

from backend.core.ouroboros.governance.repair_tree import (
    BranchOutcome,
    LayerVerdict,
    PruningReason,
    RepairBranch,
    RepairTreeLayer,
    RepairTreeResult,
)
from backend.core.ouroboros.governance.repair_tree_archive import (
    ARCHIVE_MASTER_FLAG_ENV_VAR,
    get_default_archive,
    reset_default_archive_for_tests,
)
from backend.core.ouroboros.governance.repair_tree_repl import (
    RepairTreeReplDispatchResult,
    dispatch_repair_tree_command,
)


def _make_branch(*, bid: str, score: float = 0.5,
                 outcome: BranchOutcome = BranchOutcome.PROMOTED,
                 layer_index: int = 0,
                 hypothesis: str = "rename") -> RepairBranch:
    return RepairBranch(
        branch_id=bid, parent_branch_id=None,
        layer_index=layer_index, failure_class="test",
        fix_hypothesis=hypothesis, diff="--- a\n+++ b\n",
        validator_score=score, outcome=outcome,
        prune_reason=None, worktree_id="unit-x",
        cost_usd=0.001, validation_runs_consumed=1,
    )


def _make_result(*, op_id: str = "op-1",
                 branches_per_layer: int = 2,
                 layer_count: int = 1,
                 outcome: BranchOutcome = BranchOutcome.PROMOTED) -> RepairTreeResult:
    layers: List[RepairTreeLayer] = []
    for li in range(layer_count):
        branches = tuple(
            _make_branch(bid=f"{op_id}-{li}-{i}",
                         outcome=outcome, layer_index=li)
            for i in range(branches_per_layer)
        )
        layers.append(RepairTreeLayer(
            layer_index=li, branches=branches,
            verdict=LayerVerdict.EXPANDED, wall_ms=10.0,
            parallel_units_actual=branches_per_layer,
        ))
    return RepairTreeResult(
        root_op_id=op_id, layers=tuple(layers),
        winning_branch_path=(), final_status=None,
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv(ARCHIVE_MASTER_FLAG_ENV_VAR, "true")
    reset_default_archive_for_tests()
    yield
    reset_default_archive_for_tests()


# ===========================================================================
# Match discipline
# ===========================================================================


def test_dispatcher_matches_canonical_verb_forms():
    for line in (
        "/repair_tree",
        "/repair_tree help",
        "/repair_tree recent",
        "repair_tree stats",  # without leading slash also accepted
    ):
        result = dispatch_repair_tree_command(line)
        assert result.matched is True


def test_dispatcher_unmatched_for_unrelated_lines():
    for line in (
        "/something_else",
        "",
        "  ",
        "/repair_treee help",  # typo
    ):
        result = dispatch_repair_tree_command(line)
        assert result.matched is False


# ===========================================================================
# Help — always works, bypasses master gate
# ===========================================================================


def test_help_always_works(monkeypatch):
    """`/repair_tree help` MUST work even when master flag is off."""
    import os
    old = os.environ.pop(ARCHIVE_MASTER_FLAG_ENV_VAR, None)
    try:
        result = dispatch_repair_tree_command("/repair_tree help")
        assert result.ok is True
        assert "/repair_tree" in result.text
        assert "Master flag:" in result.text
    finally:
        if old is not None:
            os.environ[ARCHIVE_MASTER_FLAG_ENV_VAR] = old


def test_help_aliases():
    for line in ("/repair_tree help", "/repair_tree ?"):
        result = dispatch_repair_tree_command(line)
        assert result.ok is True
        assert "Subcommands:" in result.text


# ===========================================================================
# Master-flag gate — disabled-notice when off
# ===========================================================================


def test_master_off_returns_disabled_notice(monkeypatch):
    import os
    old = os.environ.pop(ARCHIVE_MASTER_FLAG_ENV_VAR, None)
    try:
        result = dispatch_repair_tree_command("/repair_tree recent")
        assert result.ok is False
        assert "archive disabled" in result.text
        assert ARCHIVE_MASTER_FLAG_ENV_VAR in result.text
    finally:
        if old is not None:
            os.environ[ARCHIVE_MASTER_FLAG_ENV_VAR] = old


# ===========================================================================
# Subcommands
# ===========================================================================


def test_recent_empty_archive():
    result = dispatch_repair_tree_command("/repair_tree recent")
    assert result.ok is True
    assert "archive is empty" in result.text


def test_recent_lists_archived_branches():
    archive = get_default_archive()
    archive.record_result(_make_result(op_id="op-A", branches_per_layer=2))
    result = dispatch_repair_tree_command("/repair_tree recent")
    assert result.ok is True
    assert "/repair_tree recent" in result.text
    assert "b-1" in result.text
    assert "b-2" in result.text


def test_recent_with_explicit_limit():
    archive = get_default_archive()
    for i in range(5):
        archive.record_result(_make_result(op_id=f"op-{i}", branches_per_layer=1))
    result = dispatch_repair_tree_command("/repair_tree recent 2")
    assert result.ok is True
    assert "of ≤2" in result.text


def test_branches_alias_for_recent():
    archive = get_default_archive()
    archive.record_result(_make_result(branches_per_layer=1))
    result_recent = dispatch_repair_tree_command("/repair_tree recent")
    result_branches = dispatch_repair_tree_command("/repair_tree branches")
    assert result_recent.ok is True
    assert result_branches.ok is True


def test_op_subcommand():
    archive = get_default_archive()
    archive.record_result(_make_result(op_id="op-target", branches_per_layer=3))
    archive.record_result(_make_result(op_id="op-other", branches_per_layer=2))
    result = dispatch_repair_tree_command("/repair_tree op op-target")
    assert result.ok is True
    assert "op op-target" in result.text
    assert "3 of 3 archived" in result.text


def test_op_missing_id_returns_error():
    result = dispatch_repair_tree_command("/repair_tree op")
    assert result.ok is False
    assert "missing op_id" in result.text


def test_op_unknown_returns_friendly_message():
    result = dispatch_repair_tree_command("/repair_tree op never-existed")
    assert result.ok is True
    assert "no archived branches" in result.text


def test_layers_subcommand():
    archive = get_default_archive()
    archive.record_result(_make_result(
        op_id="op-multi", branches_per_layer=2, layer_count=3,
    ))
    result = dispatch_repair_tree_command("/repair_tree layers op-multi")
    assert result.ok is True
    assert "L0:" in result.text
    assert "L1:" in result.text
    assert "L2:" in result.text


def test_layers_missing_id_returns_error():
    result = dispatch_repair_tree_command("/repair_tree layers")
    assert result.ok is False
    assert "missing op_id" in result.text


def test_stats_subcommand():
    archive = get_default_archive()
    archive.record_result(_make_result(branches_per_layer=2))
    result = dispatch_repair_tree_command("/repair_tree stats")
    assert result.ok is True
    assert "capacity" in result.text
    assert "size" in result.text
    assert "utilization" in result.text


def test_unknown_subcommand_returns_error():
    result = dispatch_repair_tree_command("/repair_tree teleport")
    assert result.ok is False
    assert "unknown subcommand" in result.text
    assert "teleport" in result.text


# ===========================================================================
# Defensive — never raises
# ===========================================================================


def test_dispatcher_never_raises_on_garbage():
    """Adversarial input MUST NOT propagate exceptions."""
    for line in ("/repair_tree '\"`unclosed", "/repair_tree \\x00"):
        result = dispatch_repair_tree_command(line)
        assert isinstance(result, RepairTreeReplDispatchResult)
