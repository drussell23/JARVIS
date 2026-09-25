"""Top-level scripts in ``backend/`` rank after package modules, never shed.

bt-2026-09-25-004828: 13 of 13 identifiable VALIDATE failures were goals on
dead debug scripts at the root of ``backend/``, and 50 of the roadmap's 71
signed goals targeted such scripts, because the walk visited that directory
first and every uncovered module weighs the same.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.autonomy import goal_discovery as GD
from backend.core.ouroboros.governance.autonomy.goal_discovery import DiscoveredWork

_BODY = "def f():\n    return 1\n" + "# pad\n" * 120   # past the 512-byte stub floor


def _signed(gid: str, subject: str) -> DiscoveredWork:
    stem = subject.rsplit("/", 1)[-1][:-3]
    return DiscoveredWork(
        target_file=f"tests/test_{stem}.py", subject_file=f"tests/test_{stem}.py",
        kind="roadmap_goal", evidence=gid, weight=0.75, declared_goal_id=gid,
        detail={"description": f"`{subject}` has no corresponding test module."},
    )


@pytest.mark.parametrize("work, expected", [
    (DiscoveredWork("tests/test_x.py", "uncovered_module", "e",
                    subject_file="backend/x.py"), True),
    (DiscoveredWork("tests/test_x.py", "uncovered_module", "e",
                    subject_file="backend/api/x.py"), False),
    (_signed("g", "backend/trace_live_error.py"), True),
    (_signed("g", "backend/vision/multi_space.py"), False),
    # A production edit on a script is still a script.
    (DiscoveredWork("backend/runtime_patcher.py", "ambient_red", "e"), True),
    # Names a package module too — the goal is not only about the script.
    (DiscoveredWork("tests/test_x.py", "roadmap_goal", "e", detail={
        "description": "`backend/x.py` via `backend/core/y.py`"}), False),
    # Names no source subject at all: never demoted.
    (DiscoveredWork("tests/test_x.py", "roadmap_goal", "e",
                    detail={"description": "tidy the suite"}), False),
])
def test_top_level_script_classification(work, expected):
    assert GD._targets_top_level_script(work) is expected


def _tree(tmp_path):
    (tmp_path / "backend" / "pkg").mkdir(parents=True)
    (tmp_path / "backend" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "backend" / "pkg" / "mod.py").write_text(_BODY)
    (tmp_path / "backend" / "script.py").write_text(_BODY)
    (tmp_path / "tests").mkdir()


def test_the_walk_yields_packages_before_top_level_scripts(tmp_path):
    _tree(tmp_path)
    subjects = [w.subject_file for w in GD._iter_uncovered_modules(tmp_path)]
    assert subjects == ["backend/pkg/mod.py", "backend/script.py"]
    assert [w.subject_file for w in GD._iter_uncovered_modules(
        tmp_path, tier=GD.WALK_TOP_LEVEL)] == ["backend/script.py"]
    assert [w.subject_file for w in GD._iter_uncovered_modules(
        tmp_path, tier=GD.WALK_PACKAGES)] == ["backend/pkg/mod.py"]


@pytest.mark.timeout(30)
def test_signed_script_goals_rank_after_walked_package_work(monkeypatch, tmp_path):
    """The live shape: signed goals on scripts used to fill the cap before the
    walk ran. Demoted, they follow package work — and are still selected."""
    _tree(tmp_path)

    class _Nothing:
        def satisfied_goal_ids(self, ids):
            return frozenset()

    class _NoCooling:
        def is_cooling(self, target):
            return False

    monkeypatch.setattr(GD, "_from_ambient_reds", lambda *a, **k: [])
    monkeypatch.setattr(GD, "_from_roadmap_goals", lambda *a, **k: [
        _signed("ov-script", "backend/old_debug.py"),
        _signed("ov-pkg", "backend/pkg/other.py"),
    ])
    GD._TREE_PURE.reset()

    got = asyncio.run(GD.discover(
        repo_root=tmp_path, settled=_Nothing(), cooldown=_NoCooling(), limit=8,
    ))

    assert [w.target_file for w in got] == [
        "tests/test_other.py",      # signed, package
        "tests/test_mod.py",        # walked, package
        "tests/test_old_debug.py",  # signed, top-level script
        "tests/test_script.py",     # walked, top-level script
    ]


@pytest.mark.timeout(30)
def test_a_landed_prerequisite_outside_the_batch_still_unblocks(
    monkeypatch, tmp_path,
):
    """Settlement used to be asked only for the ids in the batch being ranked,
    and the DAG gate read its prerequisites from that answer — so a landed
    prerequisite ranked in another batch (or not a candidate at all) left its
    dependent blocked forever. Splitting the pass by path tier made that the
    common case."""
    _tree(tmp_path)
    b = _signed("ov-b", "backend/pkg/mod.py")

    class _ALanded:
        def satisfied_goal_ids(self, ids):
            return frozenset(i for i in ids if i == "ov-a")

    class _NoCooling:
        def is_cooling(self, target):
            return False

    async def _index():
        return {
            "ov-a": {"depends_on": (), "target": "tests/test_a.py"},
            "ov-b": {"depends_on": ("ov-a",), "target": b.target_file},
        }

    monkeypatch.setattr(GD, "_dag_index", _index)
    monkeypatch.setattr(GD, "_from_ambient_reds", lambda *a, **k: [])
    monkeypatch.setattr(GD, "_from_roadmap_goals", lambda *a, **k: [b])
    monkeypatch.setattr(GD, "_iter_uncovered_modules", lambda *a, **k: [])
    GD._TREE_PURE.reset()

    got = asyncio.run(GD.discover(
        repo_root=tmp_path, settled=_ALanded(), cooldown=_NoCooling(), limit=8,
    ))

    assert [w.goal_id for w in got] == ["ov-b"]


@pytest.mark.timeout(30)
def test_a_cap_filled_by_package_work_leaves_scripts_out(monkeypatch, tmp_path):
    _tree(tmp_path)

    class _Nothing:
        def satisfied_goal_ids(self, ids):
            return frozenset()

    class _NoCooling:
        def is_cooling(self, target):
            return False

    monkeypatch.setattr(GD, "_from_ambient_reds", lambda *a, **k: [])
    monkeypatch.setattr(GD, "_from_roadmap_goals", lambda *a, **k: [
        _signed("ov-script", "backend/old_debug.py"),
    ])
    GD._TREE_PURE.reset()

    got = asyncio.run(GD.discover(
        repo_root=tmp_path, settled=_Nothing(), cooldown=_NoCooling(), limit=1,
    ))

    assert [w.target_file for w in got] == ["tests/test_mod.py"]
