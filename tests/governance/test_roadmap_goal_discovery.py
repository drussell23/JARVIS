"""Signed goals are work. Discovery has to be able to see them.

Discovery had exactly two sources — ``ambient_red`` and ``uncovered_module`` —
and both scan the FILESYSTEM. Neither can surface a goal by id. So the
substitution DAG filed 14 correctly-signed goals in one live session (7 A→B
pairs, edges intact, scopes disjoint) and the reconciliation ledger recorded
**zero** dispatches of any of them: a correct graph that nothing read.

It also made the DAG gate look healthy when it was merely unreachable —
``blocked=0, dependency_failed=0`` because no candidate carrying edges ever
arrived to be gated.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy import goal_discovery as GD
from backend.core.ouroboros.governance.autonomy.goal_discovery import DiscoveredWork


class _Goal:
    """The shape ``roadmap_reader.RoadmapGoal`` presents."""

    def __init__(self, gid, files, *, deps=(), title="t", desc="d", priority=None):
        self.goal_id, self.target_files = gid, tuple(files)
        self.depends_on, self.title, self.description = tuple(deps), title, desc
        self.priority = priority


class _Doc:
    def __init__(self, goals):
        self.goals = tuple(goals)


def _patch_roadmap(monkeypatch, goals, verdict="valid", doc=True):
    import backend.core.ouroboros.governance.roadmap_reader as rr

    monkeypatch.setattr(
        rr, "read_roadmap",
        lambda **kw: (verdict, _Doc(goals) if doc else None, ""),
    )


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------

def test_a_signed_goal_is_surfaced_at_all(monkeypatch, tmp_path):
    """THE regression: 14 signed goals, 0 dispatches."""
    _patch_roadmap(monkeypatch, [_Goal("ov-dag-repair-x", ["backend/api/x.py"])])
    got = GD._from_roadmap_goals(tmp_path, 10)
    assert len(got) == 1
    assert got[0].goal_id == "ov-dag-repair-x"


def test_discovery_has_a_third_source():
    src = inspect.getsource(GD.discover)
    assert "_from_roadmap_goals" in src
    assert "signed + reds + uncovered" in src


def test_the_signed_source_is_ranked_with_the_others():
    """A source that is collected but never ranked is the same defect one step
    later."""
    src = inspect.getsource(GD.discover)
    pool = src.index("pool = sorted(signed + reds + uncovered")
    loop = src.index("for item in pool:")
    assert pool < loop


# --------------------------------------------------------------------------
# Identity — the goal keeps its own
# --------------------------------------------------------------------------

def test_a_signed_goal_keeps_its_own_id(monkeypatch, tmp_path):
    """Deriving one would mint a second identity for work that already has an
    id — and the ledger, the DAG edges and the signer's duplicate guard all key
    on it."""
    _patch_roadmap(monkeypatch, [_Goal("ov-dag-testsynth-x", ["tests/test_x.py"])])
    got = GD._from_roadmap_goals(tmp_path, 10)
    assert got[0].goal_id == "ov-dag-testsynth-x"
    assert not got[0].goal_id.startswith("ov-auto-")


def test_evidence_derived_work_still_derives_its_id():
    """The declared id must not leak into the sources that have none."""
    w = DiscoveredWork(
        target_file="tests/test_x.py", subject_file="backend/x.py",
        kind="uncovered_module", evidence="e",
    )
    assert w.goal_id.startswith("ov-auto-uncovered-module-")


def test_an_empty_declared_id_falls_back_to_derivation():
    w = DiscoveredWork(
        target_file="tests/test_x.py", subject_file="backend/x.py",
        kind="uncovered_module", evidence="e", declared_goal_id="",
    )
    assert w.goal_id.startswith("ov-auto-")


# --------------------------------------------------------------------------
# The signed task text is carried, never re-derived
# --------------------------------------------------------------------------

def test_the_signed_description_is_handed_to_the_model(monkeypatch, tmp_path):
    """Re-deriving it would hand the model a different instruction than the one
    the signature covers — and for a DAG's repair half the derived text is the
    'write tests for X' template, i.e. exactly the wrong job."""
    _patch_roadmap(monkeypatch, [
        _Goal("ov-dag-repair-x", ["backend/api/x.py"], desc="Repair the retry contract"),
    ])
    got = GD._from_roadmap_goals(tmp_path, 10)
    assert got[0].describe() == "Repair the retry contract"
    assert "has no corresponding test module" not in got[0].describe()


def test_a_titleless_goal_still_describes_something(monkeypatch, tmp_path):
    _patch_roadmap(monkeypatch, [_Goal("g", ["a.py"], desc="", title="Only a title")])
    got = GD._from_roadmap_goals(tmp_path, 10)
    assert got[0].describe()


# --------------------------------------------------------------------------
# The edge survives into discovery
# --------------------------------------------------------------------------

def test_depends_on_reaches_the_candidate(monkeypatch, tmp_path):
    _patch_roadmap(monkeypatch, [
        _Goal("b", ["backend/x.py"], deps=("a",)),
    ])
    got = GD._from_roadmap_goals(tmp_path, 10)
    assert got[0].detail.get("depends_on") == ["a"]


def test_a_dependent_is_withheld_until_its_prerequisite_lands(monkeypatch, tmp_path):
    """The whole point of surfacing signed goals is that the gate can now see
    them. A is selectable; B is not."""
    _patch_roadmap(monkeypatch, [
        _Goal("a", ["tests/test_x.py"]),
        _Goal("b", ["backend/x.py"], deps=("a",)),
    ])
    monkeypatch.setattr(GD, "_from_ambient_reds", lambda *a, **k: [])
    monkeypatch.setattr(GD, "_from_uncovered_modules", lambda *a, **k: [])

    class _Ledger:
        def satisfied_goal_ids(self, ids):
            return frozenset()

    got = asyncio.run(GD.discover(repo_root=tmp_path, settled=_Ledger(), limit=20))
    ids = [w.goal_id for w in got]
    assert "a" in ids
    assert "b" not in ids, "a dependent was selectable before its prerequisite"


def test_the_dependent_is_released_once_the_prerequisite_lands(monkeypatch, tmp_path):
    _patch_roadmap(monkeypatch, [
        _Goal("a", ["tests/test_x.py"]),
        _Goal("b", ["backend/x.py"], deps=("a",)),
    ])
    monkeypatch.setattr(GD, "_from_ambient_reds", lambda *a, **k: [])
    monkeypatch.setattr(GD, "_from_uncovered_modules", lambda *a, **k: [])

    class _Ledger:
        def satisfied_goal_ids(self, ids):
            return frozenset({"a"})

    got = asyncio.run(GD.discover(repo_root=tmp_path, settled=_Ledger(), limit=20))
    ids = [w.goal_id for w in got]
    assert "b" in ids, "the dependent stayed blocked after its prerequisite landed"
    assert "a" not in ids, "the satisfied prerequisite was re-selected"


# --------------------------------------------------------------------------
# The floor still holds
# --------------------------------------------------------------------------

def test_a_signature_cannot_authorise_governance(monkeypatch, tmp_path):
    """The un-signable floor: no signature lets the organism rewrite its own
    cage."""
    _patch_roadmap(monkeypatch, [
        _Goal("g", ["backend/core/ouroboros/governance/risk_engine.py"]),
    ])
    assert GD._from_roadmap_goals(tmp_path, 10) == []


def test_battle_test_internals_are_refused_too(monkeypatch, tmp_path):
    _patch_roadmap(monkeypatch, [
        _Goal("g", ["backend/core/ouroboros/battle_test/harness.py"]),
    ])
    assert GD._from_roadmap_goals(tmp_path, 10) == []


# --------------------------------------------------------------------------
# Weight is derived from the goal's own declaration
# --------------------------------------------------------------------------

def test_priority_orders_signed_goals():
    from backend.core.ouroboros.governance.roadmap_reader import GoalPriority

    weights = [GD._priority_weight(p) for p in GoalPriority]
    assert weights == sorted(weights, reverse=True), weights


def test_the_weight_is_derived_from_the_existing_table():
    """Not a new literal: if the weight table is retuned this moves with it."""
    from backend.core.ouroboros.governance.roadmap_reader import GoalPriority

    top = max(GD._KIND_WEIGHT.values())
    assert GD._priority_weight(GoalPriority.CRITICAL) <= top
    src = inspect.getsource(GD._priority_weight)
    assert "_KIND_WEIGHT" in src


@pytest.mark.parametrize("junk", [None, "", "nonsense", 42, object()])
def test_an_unreadable_priority_still_yields_a_usable_weight(junk):
    w = GD._priority_weight(junk)
    assert 0.0 < w <= max(GD._KIND_WEIGHT.values())


# --------------------------------------------------------------------------
# Total
# --------------------------------------------------------------------------

def test_an_unsigned_or_missing_roadmap_yields_nothing(monkeypatch, tmp_path):
    """Read through the same reader the CAGE consults, so a tampered document
    yields no work rather than unverified work."""
    _patch_roadmap(monkeypatch, [], verdict="invalid_signature", doc=False)
    assert GD._from_roadmap_goals(tmp_path, 10) == []


def test_a_goal_with_no_target_files_is_skipped(monkeypatch, tmp_path):
    """An unscoped goal authorises every file — the one shape that makes the
    cage meaningless."""
    _patch_roadmap(monkeypatch, [_Goal("g", [])])
    assert GD._from_roadmap_goals(tmp_path, 10) == []


def test_the_limit_is_honoured(monkeypatch, tmp_path):
    _patch_roadmap(monkeypatch, [
        _Goal(f"g{i}", [f"backend/x{i}.py"]) for i in range(50)
    ])
    assert len(GD._from_roadmap_goals(tmp_path, 5)) == 5


def test_a_raising_reader_never_breaks_a_pass(monkeypatch, tmp_path):
    import backend.core.ouroboros.governance.roadmap_reader as rr

    def _boom(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(rr, "read_roadmap", _boom)
    assert GD._from_roadmap_goals(tmp_path, 10) == []


def test_the_source_does_not_duplicate_the_ranking_filters():
    """Satisfaction, dependencies and cooldown are the ranking loop's job. A
    second copy here is exactly the duplication that lets two filters
    disagree."""
    src = inspect.getsource(GD._from_roadmap_goals)
    assert "is_cooling" not in src
    assert "satisfied_goal_ids" not in src
    assert "dependency_verdict" not in src
