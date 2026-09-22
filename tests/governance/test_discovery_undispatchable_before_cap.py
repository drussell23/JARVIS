"""Work the Sentinel will refuse must not occupy the pass cap.

Third instance of the budget defect (`0f9ef8808d` roadmap, then the red-count
sizing). The Sentinel refuses undispatchable work — a dead target, or a subject
quarantined as impossible on this host — via ``is_dispatchable``, but only
AFTER ``discover`` has capped the list. Found live in bt-2026-09-21-235603:
eight quarantined signed goals (Quartz, missing ``vision.*`` modules, a script
that runs at import) filled all eight slots, the coverage walk never ran
because the cap was already met, and the loop logged ``ExecutionQueueStarved``
every pass for five of the soak's six and a half hours.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.autonomy import goal_discovery as GD
from backend.core.ouroboros.governance.autonomy.goal_discovery import DiscoveredWork

_IMPOSSIBLE = SimpleNamespace(
    importable=False, impossible=True, reason="platform_unavailable: Quartz",
    unresolvable=(), structural=("Quartz",),
)


class _NoLedger:
    def satisfied_goal_ids(self, ids):
        return frozenset()


def _signed(i: int) -> DiscoveredWork:
    return DiscoveredWork(
        target_file=f"tests/test_q{i}.py", subject_file=f"backend/q{i}.py",
        kind="roadmap_goal", evidence="signed", weight=0.75,
        declared_goal_id=f"ov-q{i}",
    )


def _uncovered(stem: str) -> DiscoveredWork:
    return DiscoveredWork(
        target_file=f"tests/test_{stem}.py", subject_file=f"backend/{stem}.py",
        kind="uncovered_module", evidence="none",
        weight=GD._KIND_WEIGHT["uncovered_module"],
    )


@pytest.fixture
def world(monkeypatch):
    impossible = {f"ov-q{i}" for i in range(8)} | {_uncovered("m1").goal_id}
    monkeypatch.setattr(GD, "_from_ambient_reds", lambda *a, **k: [])
    monkeypatch.setattr(
        GD, "_from_roadmap_goals", lambda *a, **k: [_signed(i) for i in range(8)],
    )
    monkeypatch.setattr(
        GD, "_iter_uncovered_modules",
        lambda *a, **k: iter([_uncovered(f"m{i}") for i in range(12)]),
    )
    monkeypatch.setattr(
        GD, "_import_verdict",
        lambda work, root: _IMPOSSIBLE if work.goal_id in impossible else None,
    )
    GD._TREE_PURE.reset()
    yield impossible
    GD._TREE_PURE.reset()


def test_quarantined_signed_goals_do_not_starve_the_walk(world, tmp_path):
    got = asyncio.run(
        GD.discover(repo_root=tmp_path, settled=_NoLedger(), limit=8),
    )

    assert len(got) == 8, f"the pass was starved to {len(got)} candidate(s)"
    assert not any(w.goal_id in world for w in got), (
        "a quarantined goal took a slot the Sentinel will refuse to spend"
    )
    assert all(w.kind == "uncovered_module" for w in got)


def test_everything_discover_returns_the_sentinel_will_dispatch(world, tmp_path):
    """One rule, two call sites: they must never disagree."""
    got = asyncio.run(
        GD.discover(repo_root=tmp_path, settled=_NoLedger(), limit=8),
    )

    assert got
    assert all(GD.is_dispatchable(w, tmp_path) for w in got)


def test_refusal_is_a_pure_reading_of_liveness_and_verdict():
    assert GD._dispatch_refusal(GD.LIVENESS_DEAD, None) == "dead target"
    assert GD._dispatch_refusal(GD.LIVENESS_CREATES_TEST, None) == ""
    assert "impossible" in GD._dispatch_refusal(GD.LIVENESS_LANDABLE, _IMPOSSIBLE)
    unprovisioned = SimpleNamespace(importable=False, impossible=False)
    # Demoted, not refused, unless the operator armed quarantine.
    assert GD._dispatch_refusal(GD.LIVENESS_LANDABLE, unprovisioned) == ""
