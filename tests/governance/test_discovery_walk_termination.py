"""The batched walk must terminate even when every candidate is pruned.

The ranking loop pulls the coverage walk in batches sized to the remaining cap.
If the source hands back a re-iterable (a list, which is what a test double
supplies) and the ledger prunes each batch WITHOUT the gate marking anything
seen, an un-wrapped source would return the same candidates forever.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.autonomy import goal_discovery as GD
from backend.core.ouroboros.governance.autonomy.goal_discovery import DiscoveredWork


def _uncovered(stem: str) -> DiscoveredWork:
    return DiscoveredWork(
        target_file=f"tests/test_{stem}.py", subject_file=f"backend/{stem}.py",
        kind="uncovered_module", evidence="none",
        weight=GD._KIND_WEIGHT["uncovered_module"],
    )


@pytest.mark.timeout(15)
def test_a_list_source_whose_every_item_is_satisfied_terminates(
    monkeypatch, tmp_path,
):
    class _EverythingLanded:
        def satisfied_goal_ids(self, ids):
            return frozenset(ids)

    monkeypatch.setattr(GD, "_from_roadmap_goals", lambda *a, **k: [])
    monkeypatch.setattr(GD, "_from_ambient_reds", lambda *a, **k: [])
    # A LIST, deliberately — re-iterable, the shape that could spin.
    monkeypatch.setattr(
        GD, "_iter_uncovered_modules",
        lambda *a, **k: [_uncovered(f"m{i}") for i in range(4)],
    )

    got = asyncio.run(GD.discover(
        repo_root=tmp_path, settled=_EverythingLanded(), limit=8,
    ))

    assert got == ()
