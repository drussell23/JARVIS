"""Propagation tests for the Sovereign Call-Graph Risk Matrix (C1) +
recursion-depth inheritance (M1).

Proves:
  * SubGoal.scoped_symbols defaults to () (additive, byte-identical unset).
  * decompose_for_block STOPS discarding isolate_symbols' result — the
    mutation sub-goal carries the matched "file::Symbol" refs.
  * The test-gen sub-goal does NOT carry scoped_symbols (it doesn't
    mutate the scoped symbols).
  * _make_envelope_for_sub_goal stamps evidence["scoped_symbols"] +
    evidence["recursion_depth"] so the scope/depth rides intake → ctx.
  * recursion_depth increments from the parent (M1).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Tuple

import pytest

from backend.core.ouroboros.governance.goal_decomposition_planner import (
    SubGoal,
    SubGoalKind,
    _make_envelope_for_sub_goal,
    decompose_for_block,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeScopedTarget:
    file_path: str
    symbol: str
    lineno: int = 0
    end_lineno: int = 0


@dataclass
class FakeGoal:
    goal_id: str
    title: str
    description: str
    target_files: Tuple[str, ...]


def _scoper_returns(symbols_by_file):
    """Build a fake isolate_symbols-shaped callable."""
    def _scope(file_path, description):
        return tuple(
            FakeScopedTarget(file_path=file_path, symbol=s)
            for s in symbols_by_file.get(file_path, [])
        )
    return _scope


# ---------------------------------------------------------------------------
# SubGoal field
# ---------------------------------------------------------------------------


def test_subgoal_scoped_symbols_defaults_empty():
    sg = SubGoal(
        sub_goal_id="g::s0",
        parent_goal_id="g",
        title="t",
        description="d",
        kind=SubGoalKind.ATOMIC,
        target_files=("a.py",),
        depends_on_sub_ids=(),
        estimated_complexity="moderate",
        boundary_crossed=False,
    )
    assert sg.scoped_symbols == ()
    assert sg.to_dict()["scoped_symbols"] == []


# ---------------------------------------------------------------------------
# decompose_for_block populates scoped_symbols (stops discarding)
# ---------------------------------------------------------------------------


def test_mutation_subgoal_carries_scoped_symbols():
    goal = FakeGoal(
        goal_id="goal-1",
        title="refactor embed",
        description="edit SemanticIndex.embed",
        target_files=("semantic_index.py",),
    )
    scoper = _scoper_returns(
        {"semantic_index.py": ["SemanticIndex.embed"]}
    )
    subs = decompose_for_block(goal, zero_coverage=False, scoper=scoper)
    assert len(subs) == 1
    mutation = subs[0]
    assert mutation.scoped_symbols == ("semantic_index.py::SemanticIndex.embed",)


def test_zero_coverage_only_mutation_subgoal_carries_scope():
    goal = FakeGoal(
        goal_id="goal-2",
        title="refactor embed",
        description="edit embed",
        target_files=("semantic_index.py",),
    )
    scoper = _scoper_returns({"semantic_index.py": ["embed"]})
    subs = decompose_for_block(goal, zero_coverage=True, scoper=scoper)
    # step-00 test-gen + step-01 mutation
    assert len(subs) == 2
    test_gen = next(s for s in subs if s.sub_goal_id.endswith("step-00"))
    mutation = next(s for s in subs if s.sub_goal_id.endswith("step-01"))
    assert test_gen.scoped_symbols == ()  # test-gen does NOT mutate symbols
    assert mutation.scoped_symbols == ("semantic_index.py::embed",)


def test_whole_file_fallback_symbol_skipped():
    # ScopedTarget.symbol == "" is B1's whole-file fallback → no ref.
    goal = FakeGoal(
        goal_id="goal-3",
        title="t",
        description="d",
        target_files=("mod.py",),
    )
    scoper = _scoper_returns({"mod.py": [""]})
    subs = decompose_for_block(goal, zero_coverage=False, scoper=scoper)
    assert subs[0].scoped_symbols == ()


def test_no_scoper_no_symbols():
    goal = FakeGoal(
        goal_id="goal-4",
        title="t",
        description="d",
        target_files=("mod.py",),
    )
    subs = decompose_for_block(goal, zero_coverage=False, scoper=None)
    assert subs[0].scoped_symbols == ()


def test_dedups_repeated_symbols_across_files():
    goal = FakeGoal(
        goal_id="goal-5",
        title="t",
        description="d",
        target_files=("a.py", "b.py"),
    )
    scoper = _scoper_returns({"a.py": ["foo"], "b.py": ["bar", "bar"]})
    subs = decompose_for_block(goal, zero_coverage=False, scoper=scoper)
    syms = subs[0].scoped_symbols
    assert syms == ("a.py::foo", "b.py::bar")  # b.py::bar deduped


# ---------------------------------------------------------------------------
# Envelope evidence stamping
# ---------------------------------------------------------------------------


def test_envelope_stamps_scoped_symbols_and_depth():
    sg = SubGoal(
        sub_goal_id="g::s0",
        parent_goal_id="g",
        title="t",
        description="d",
        kind=SubGoalKind.ATOMIC,
        target_files=("a.py",),
        depends_on_sub_ids=(),
        estimated_complexity="moderate",
        boundary_crossed=False,
        scoped_symbols=("a.py::foo",),
    )
    env = _make_envelope_for_sub_goal(sg)
    assert env is not None
    evidence = _envelope_evidence(env)
    assert evidence.get("scoped_symbols") == ["a.py::foo"]
    assert evidence.get("recursion_depth") == 1  # parent absent → depth 1


def test_recursion_depth_increments_from_parent():
    sg = SubGoal(
        sub_goal_id="g::s0",
        parent_goal_id="g",
        title="t",
        description="d",
        kind=SubGoalKind.ATOMIC,
        target_files=("a.py",),
        depends_on_sub_ids=(),
        estimated_complexity="moderate",
        boundary_crossed=False,
    )
    env = _make_envelope_for_sub_goal(
        sg, parent_evidence={"recursion_depth": 3}
    )
    assert env is not None
    evidence = _envelope_evidence(env)
    assert evidence.get("recursion_depth") == 4


def test_recursion_depth_malformed_parent_defaults_to_one():
    sg = SubGoal(
        sub_goal_id="g::s0",
        parent_goal_id="g",
        title="t",
        description="d",
        kind=SubGoalKind.ATOMIC,
        target_files=("a.py",),
        depends_on_sub_ids=(),
        estimated_complexity="moderate",
        boundary_crossed=False,
    )
    env = _make_envelope_for_sub_goal(
        sg, parent_evidence={"recursion_depth": "garbage"}
    )
    assert env is not None
    assert _envelope_evidence(env).get("recursion_depth") == 1


def test_empty_scope_stamps_empty_list():
    sg = SubGoal(
        sub_goal_id="g::s0",
        parent_goal_id="g",
        title="t",
        description="d",
        kind=SubGoalKind.ATOMIC,
        target_files=("a.py",),
        depends_on_sub_ids=(),
        estimated_complexity="moderate",
        boundary_crossed=False,
    )
    env = _make_envelope_for_sub_goal(sg)
    assert env is not None
    assert _envelope_evidence(env).get("scoped_symbols") == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _envelope_evidence(env):
    """Extract the evidence dict from a make_envelope result, tolerating
    either an attribute or a JSON-string field."""
    ev = getattr(env, "evidence", None)
    if ev is None:
        # Some envelope shapes store evidence as a JSON string.
        raw = getattr(env, "intake_evidence_json", "") or ""
        if raw:
            return json.loads(raw)
        return {}
    if isinstance(ev, str):
        return json.loads(ev) if ev else {}
    if isinstance(ev, dict):
        return ev
    # dataclass / object with to_dict
    if hasattr(ev, "to_dict"):
        return ev.to_dict()
    return dict(ev)
