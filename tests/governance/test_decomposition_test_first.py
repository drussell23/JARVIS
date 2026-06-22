"""Tests for Task B2: test-first prerequisite injection in decompose_for_block.

TDD — tests written BEFORE implementation. All tests must fail (RED) first,
then pass after implementation (GREEN).
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import goal_decomposition_planner as g


# ---------------------------------------------------------------------------
# Minimal RoadmapGoal stand-in (as specified in the brief)
# ---------------------------------------------------------------------------


class _Goal:
    goal_id = "GOAL-001"
    title = "t"
    description = "route SemanticIndex.build through subprocess"
    target_files = ("backend/core/ouroboros/governance/semantic_index.py",)


class _MultiFileGoal:
    goal_id = "GOAL-002"
    title = "Refactor intake pipeline"
    description = "update UnifiedIntakeRouter and SemanticIndex.build"
    target_files = (
        "backend/core/ouroboros/governance/semantic_index.py",
        "backend/core/ouroboros/governance/orchestrator.py",
    )


class _NoTitleGoal:
    goal_id = "GOAL-003"
    title = ""
    description = "some description"
    target_files = ("backend/core/ouroboros/governance/semantic_index.py",)


class _NoFilesGoal:
    goal_id = "GOAL-004"
    title = "refactor"
    description = "refactor something"
    target_files = ()


# ---------------------------------------------------------------------------
# Core requirement: brief test cases (verbatim from spec)
# ---------------------------------------------------------------------------


def test_test_first_prepended_and_mutation_depends_on_it(tmp_path, monkeypatch):
    """With zero_coverage=True, index 0 is a test-gen SubGoal and every
    mutation SubGoal depends on it."""
    subs = g.decompose_for_block(_Goal(), zero_coverage=True)
    assert subs[0].kind is g.SubGoalKind.SEQUENTIAL
    assert "PyTest" in subs[0].title or "test" in subs[0].title.lower()
    mutation = [s for s in subs if s is not subs[0]]
    assert mutation and all(subs[0].sub_goal_id in s.depends_on_sub_ids for s in mutation)


def test_no_zero_coverage_no_test_subgoal():
    """With zero_coverage=False, no test-gen SubGoal is prepended."""
    subs = g.decompose_for_block(_Goal(), zero_coverage=False)
    assert all("PyTest" not in s.title for s in subs)


# ---------------------------------------------------------------------------
# Additional coverage
# ---------------------------------------------------------------------------


def test_decompose_for_block_returns_tuple():
    """decompose_for_block always returns a tuple."""
    result = g.decompose_for_block(_Goal(), zero_coverage=True)
    assert isinstance(result, tuple)


def test_test_subgoal_id_is_deterministic():
    """Calling decompose_for_block twice with the same goal yields the same ids."""
    subs1 = g.decompose_for_block(_Goal(), zero_coverage=True)
    subs2 = g.decompose_for_block(_Goal(), zero_coverage=True)
    assert subs1[0].sub_goal_id == subs2[0].sub_goal_id


def test_test_subgoal_parent_id_matches_goal():
    """The test SubGoal's parent_goal_id must match the goal's goal_id."""
    subs = g.decompose_for_block(_Goal(), zero_coverage=True)
    assert subs[0].parent_goal_id == _Goal.goal_id


def test_test_subgoal_target_files_are_symbol_bearing_files():
    """The test SubGoal's target_files should include the input file(s)."""
    subs = g.decompose_for_block(_Goal(), zero_coverage=True)
    # The test-gen SubGoal must target at least one file from the goal
    assert len(subs[0].target_files) >= 1
    # All files in the test sub-goal should be from the goal's files
    goal_files = set(_Goal.target_files)
    for f in subs[0].target_files:
        assert f in goal_files


def test_mutation_subgoal_target_files_narrowed():
    """Mutation SubGoal target_files should be a subset of the goal's target_files."""
    subs = g.decompose_for_block(_Goal(), zero_coverage=True)
    mutation_subs = [s for s in subs if s is not subs[0]]
    goal_files = set(_Goal.target_files)
    for ms in mutation_subs:
        for f in ms.target_files:
            assert f in goal_files


def test_zero_coverage_false_mutation_subgoal_has_no_test_deps():
    """When zero_coverage=False, mutation SubGoals must not depend on a test sub-goal."""
    subs = g.decompose_for_block(_Goal(), zero_coverage=False)
    # No sub-goal should have 'test' in its title (via PyTest check)
    pytest_sub_ids = {s.sub_goal_id for s in subs if "PyTest" in s.title or "test suite" in s.title.lower()}
    assert len(pytest_sub_ids) == 0


def test_at_least_one_mutation_subgoal_when_zero_coverage_true():
    """With zero_coverage=True, there must be at least one mutation sub-goal after the test one."""
    subs = g.decompose_for_block(_Goal(), zero_coverage=True)
    assert len(subs) >= 2


def test_at_least_one_subgoal_when_zero_coverage_false():
    """With zero_coverage=False, there must be at least one (mutation) sub-goal."""
    subs = g.decompose_for_block(_Goal(), zero_coverage=False)
    assert len(subs) >= 1


def test_ids_follow_step_convention():
    """Sub-goal IDs should follow the parent_id::step-NN convention."""
    subs = g.decompose_for_block(_Goal(), zero_coverage=True)
    for sub in subs:
        assert sub.sub_goal_id.startswith(_Goal.goal_id + "::")


def test_fail_soft_on_bad_goal_returns_non_empty_tuple():
    """decompose_for_block never crashes, even on a malformed goal."""
    class _BadGoal:
        goal_id = "BAD-001"
        title = "valid title"
        description = "some description"
        target_files = None  # malformed

    result = g.decompose_for_block(_BadGoal(), zero_coverage=True)
    assert isinstance(result, tuple)
    assert len(result) >= 1


def test_fail_soft_on_empty_title_returns_non_empty_tuple():
    """decompose_for_block never crashes on empty title, returns fallback."""
    result = g.decompose_for_block(_NoTitleGoal(), zero_coverage=True)
    assert isinstance(result, tuple)
    assert len(result) >= 1


def test_multi_file_goal_test_subgoal_at_index_zero():
    """Even with multiple target files, test SubGoal is at index 0."""
    subs = g.decompose_for_block(_MultiFileGoal(), zero_coverage=True)
    assert subs[0].kind is g.SubGoalKind.SEQUENTIAL
    assert "PyTest" in subs[0].title or "test" in subs[0].title.lower()


def test_multi_file_goal_all_mutations_depend_on_test():
    """With multiple target files and zero_coverage=True, all mutation subs depend on test."""
    subs = g.decompose_for_block(_MultiFileGoal(), zero_coverage=True)
    test_id = subs[0].sub_goal_id
    mutation_subs = [s for s in subs if s is not subs[0]]
    assert mutation_subs
    assert all(test_id in s.depends_on_sub_ids for s in mutation_subs)


def test_decompose_for_block_is_exported():
    """decompose_for_block must be accessible from the module namespace."""
    assert hasattr(g, "decompose_for_block")
    assert callable(g.decompose_for_block)
