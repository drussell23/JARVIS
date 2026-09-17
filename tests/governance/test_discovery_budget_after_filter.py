"""The pass cap must be spent on work that SURVIVED the filters.

Second instance of the defect `0f9ef8808d` fixed on the roadmap source, one
source over. The coverage scan was sized from the RAW ambient-red count::

    if len(reds) < cap:
        uncovered = _from_uncovered_modules(repo_root, cap - len(reds))

Reds are filtered downstream — satisfied, cooling, dependency-blocked — and
several failing tests routinely collapse onto one subject file, so a pass could
hold `cap` reds, admit one of them, and never look at the cheap tier at all.
Eligibility was again being decided before any filter ran.

These pin the property directly: what the cap governs is how much SELECTED work
a pass takes on, never which candidates are allowed to be considered.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.autonomy import goal_discovery as GD
from backend.core.ouroboros.governance.autonomy.goal_discovery import DiscoveredWork


class _NoRoadmap:
    """Discovery with the document sources silenced."""

    def satisfied_goal_ids(self, ids):
        return frozenset()


def _red(target: str) -> DiscoveredWork:
    return DiscoveredWork(
        target_file=target, kind="ambient_red", evidence="t",
        weight=GD._KIND_WEIGHT["ambient_red"],
    )


def _uncovered(stem: str) -> DiscoveredWork:
    return DiscoveredWork(
        target_file=f"tests/test_{stem}.py", subject_file=f"backend/{stem}.py",
        kind="uncovered_module", evidence="none",
        weight=GD._KIND_WEIGHT["uncovered_module"],
    )


@pytest.fixture(autouse=True)
def _no_signed(monkeypatch):
    monkeypatch.setattr(GD, "_from_roadmap_goals", lambda *a, **k: [])


def test_reds_that_all_collapse_to_one_subject_do_not_eat_the_budget(
    monkeypatch, tmp_path,
):
    """Eight failing tests, one subject file. The pass must still fill up."""
    cap = 8
    monkeypatch.setattr(
        GD, "_from_ambient_reds",
        lambda *a, **k: [_red("backend/api/same.py") for _ in range(cap)],
    )
    monkeypatch.setattr(
        GD, "_iter_uncovered_modules",
        lambda *a, **k: iter([_uncovered(f"m{i}") for i in range(cap)]),
    )

    got = asyncio.run(
        GD.discover(repo_root=tmp_path, settled=_NoRoadmap(), limit=cap),
    )

    assert len(got) == cap, (
        f"{cap} reds deduped to 1 and the cheap tier was never consulted: "
        f"got {len(got)} candidate(s)"
    )
    assert sum(1 for w in got if w.kind == "uncovered_module") == cap - 1


def test_cooling_reds_do_not_eat_the_budget(monkeypatch, tmp_path):
    """A red whose target is cooling is not work this pass can take."""
    cap = 4

    class _AllCooling:
        def is_cooling(self, target):
            return target.startswith("backend/api/red")

    monkeypatch.setattr(
        GD, "_from_ambient_reds",
        lambda *a, **k: [_red(f"backend/api/red{i}.py") for i in range(cap)],
    )
    monkeypatch.setattr(
        GD, "_iter_uncovered_modules",
        lambda *a, **k: iter([_uncovered(f"m{i}") for i in range(cap)]),
    )

    got = asyncio.run(GD.discover(
        repo_root=tmp_path, settled=_NoRoadmap(),
        cooldown=_AllCooling(), limit=cap,
    ))

    assert [w.kind for w in got] == ["uncovered_module"] * cap


def test_satisfied_uncovered_work_is_replaced_not_dropped(monkeypatch, tmp_path):
    """The walk resumes past work the ledger has already satisfied.

    The lazy walk is pulled in batches; a batch pruned by the satisfaction
    filter must be followed by the NEXT candidates, not by a short pass.
    """
    cap = 3
    landed = {GD.DiscoveredWork(
        target_file="tests/test_m0.py", subject_file="backend/m0.py",
        kind="uncovered_module", evidence="none", weight=0.4,
    ).goal_id}

    class _Landed:
        def satisfied_goal_ids(self, ids):
            return frozenset(i for i in ids if i in landed)

    monkeypatch.setattr(GD, "_from_ambient_reds", lambda *a, **k: [])
    monkeypatch.setattr(
        GD, "_iter_uncovered_modules",
        lambda *a, **k: iter([_uncovered(f"m{i}") for i in range(cap + 2)]),
    )

    got = asyncio.run(
        GD.discover(repo_root=tmp_path, settled=_Landed(), limit=cap),
    )

    assert len(got) == cap, "a satisfied candidate shortened the pass"
    assert "tests/test_m0.py" not in [w.target_file for w in got]


def test_the_cap_is_still_enforced(monkeypatch, tmp_path):
    """The other half of the contract: filtering first must not uncap a pass."""
    monkeypatch.setattr(
        GD, "_from_ambient_reds",
        lambda *a, **k: [_red(f"backend/api/r{i}.py") for i in range(20)],
    )
    monkeypatch.setattr(
        GD, "_iter_uncovered_modules",
        lambda *a, **k: iter([_uncovered(f"m{i}") for i in range(20)]),
    )

    got = asyncio.run(
        GD.discover(repo_root=tmp_path, settled=_NoRoadmap(), limit=5),
    )

    assert len(got) == 5


def test_the_walk_is_not_drained_to_fill_a_small_cap(monkeypatch, tmp_path):
    """Laziness is load-bearing: the cheap tier exists to avoid enumerating
    every module in the repository on every pass."""
    pulled = []

    def _counting_walk(*_a, **_k):
        for i in range(500):
            pulled.append(i)
            yield _uncovered(f"m{i}")

    monkeypatch.setattr(GD, "_from_ambient_reds", lambda *a, **k: [])
    monkeypatch.setattr(GD, "_iter_uncovered_modules", _counting_walk)

    got = asyncio.run(
        GD.discover(repo_root=tmp_path, settled=_NoRoadmap(), limit=3),
    )

    assert len(got) == 3
    assert len(pulled) <= 10, (
        f"the walk enumerated {len(pulled)} modules to fill a cap of 3"
    )
