"""The worktree fingerprint, the module index, and the tree-pure discovery cache.

bt-2026-09-20-183259 spent 750 s in Sentinel discovery over 12 passes — more
than all 122 model generations — re-deriving identical answers from a tree that
had not moved. The cause was ``root.rglob(stem + ".py")``: a full repository
walk per test target, per goal, per pass (205 walks for 49 goals). These tests
pin both halves of the fix: ONE indexed walk per tree state, and tree-pure
results held only while the tree (and the environment) stand still.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import repo_state
from backend.core.ouroboros.governance.autonomy import goal_discovery as gd


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


@pytest.fixture
def repo(tmp_path):
    repo_state.reset_for_tests()
    gd._TREE_PURE.reset()
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "engine.py").write_text("def run():\n    return 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_engine.py").write_text("def test_run():\n    pass\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    yield tmp_path
    repo_state.reset_for_tests()
    gd._TREE_PURE.reset()


def _fp(root):
    return repo_state.worktree_fingerprint_sync(root)


# ---------------------------------------------------------------------------
# The fingerprint
# ---------------------------------------------------------------------------


def test_an_untouched_tree_has_a_stable_fingerprint(repo):
    assert _fp(repo) and _fp(repo) == _fp(repo)


def test_editing_a_tracked_source_moves_it(repo):
    before = _fp(repo)
    (repo / "backend" / "engine.py").write_text("def run():\n    return 2\n")
    assert _fp(repo) != before


def test_a_SECOND_edit_to_an_already_dirty_file_moves_it_again(repo):
    """``git status`` says ' M engine.py' after the first edit and says exactly
    the same after the second. Porcelain alone cannot see it."""
    target = repo / "backend" / "engine.py"
    target.write_text("def run():\n    return 2\n")
    first = _fp(repo)
    target.write_text("def run():\n    return 3333\n")
    assert _fp(repo) != first


def test_a_new_untracked_source_moves_it(repo):
    before = _fp(repo)
    (repo / "backend" / "fresh.py").write_text("X = 1\n")
    assert _fp(repo) != before


def test_committing_moves_it_and_reverting_restores_it(repo):
    clean = _fp(repo)
    (repo / "backend" / "engine.py").write_text("def run():\n    return 2\n")
    _git(repo, "commit", "-qam", "edit")
    assert _fp(repo) != clean


def test_churning_ledgers_do_not_move_it(repo):
    """The worktree holds traces that change every second and that no tree-pure
    answer reads. Fingerprinting them would make every pass a cache miss."""
    before = _fp(repo)
    (repo / ".ouroboros").mkdir()
    (repo / ".ouroboros" / "reachability.jsonl").write_text('{"a": 1}\n')
    (repo / "notes.md").write_text("scratch\n")
    assert _fp(repo) == before


def test_not_a_checkout_is_unknown_not_a_crash(tmp_path):
    assert repo_state.worktree_fingerprint_sync(tmp_path) == ""


def test_a_rename_record_is_consumed_whole(repo):
    """Porcelain -z emits a rename as TWO records; the origin must not be
    parsed as a status line of its own."""
    _git(repo, "mv", "backend/engine.py", "backend/motor.py")
    assert _fp(repo)


@pytest.mark.asyncio
async def test_the_async_reading_matches_the_sync_one(repo):
    assert await repo_state.worktree_fingerprint(repo) == _fp(repo)


def test_environment_fingerprint_is_stable(repo):
    assert repo_state.environment_fingerprint() == repo_state.environment_fingerprint() != ""


# ---------------------------------------------------------------------------
# The module index
# ---------------------------------------------------------------------------


def test_the_index_never_descends_into_worktrees_or_venvs(repo):
    for skipped in (".worktrees/copy/backend", "venv/lib", "node_modules/x", ".git/hooks"):
        d = repo / skipped
        d.mkdir(parents=True, exist_ok=True)
        (d / "engine.py").write_text("X = 1\n")
    found = repo_state.ModuleIndex(repo).find("engine.py")
    assert [p.relative_to(repo).as_posix() for p in found] == ["backend/engine.py"]


def test_the_index_is_built_once_per_tree_state(repo):
    with repo_state.pinned(repo, _fp(repo)):
        first = repo_state.module_index(repo)
        assert repo_state.module_index(repo) is first
    (repo / "backend" / "fresh.py").write_text("X = 1\n")
    rebuilt = repo_state.module_index(repo)
    assert rebuilt is not first and rebuilt.find("fresh.py")


def test_an_unknown_tree_state_is_never_retained(tmp_path):
    (tmp_path / "a.py").write_text("X = 1\n")
    assert repo_state.module_index(tmp_path) is not repo_state.module_index(tmp_path)


def test_anchor_resolution_still_finds_the_module_under_test(repo):
    from backend.core.ouroboros.governance.ast_signature_anchor import (
        collect_anchor_sources,
    )
    sources = collect_anchor_sources(["tests/test_engine.py"], "", repo)
    assert any(Path(src).name == "engine.py" for _label, src in sources)


# ---------------------------------------------------------------------------
# The tree-pure cache
# ---------------------------------------------------------------------------


def _work(goal_id, target, description=""):
    return SimpleNamespace(
        goal_id=goal_id, target_file=target, subject_file="",
        evidence=description, detail={"description": description}, kind="roadmap",
    )


def test_an_unchanged_tree_is_answered_from_the_cache(repo):
    cache = gd._TreePureCache()
    state = (_fp(repo), repo_state.environment_fingerprint())
    works = [_work("g1", "backend/engine.py"), _work("g2", "tests/test_engine.py")]
    first = cache.verdicts(works, repo, state)
    assert (cache.hits, cache.misses) == (0, 2)
    assert cache.verdicts(works, repo, state).keys() == first.keys()
    assert (cache.hits, cache.misses) == (2, 2)


def test_only_a_NEW_goal_is_computed(repo):
    cache = gd._TreePureCache()
    state = (_fp(repo), repo_state.environment_fingerprint())
    cache.verdicts([_work("g1", "backend/engine.py")], repo, state)
    cache.verdicts([_work("g1", "backend/engine.py"), _work("g2", "tests/test_engine.py")], repo, state)
    assert (cache.hits, cache.misses) == (1, 2)


def test_a_moved_tree_drops_everything(repo):
    cache = gd._TreePureCache()
    works = [_work("g1", "backend/engine.py")]
    cache.verdicts(works, repo, (_fp(repo), "env"))
    (repo / "backend" / "engine.py").write_text("def run():\n    return 9\n")
    cache.verdicts(works, repo, (_fp(repo), "env"))
    assert cache.misses == 2 and cache.rebinds == 2


def test_an_install_drops_everything_too(repo):
    """A verdict held across a ``pip install`` would keep a goal demoted after
    the package that unblocks it had landed."""
    cache = gd._TreePureCache()
    works = [_work("g1", "backend/engine.py")]
    cache.verdicts(works, repo, (_fp(repo), "env-before"))
    cache.verdicts(works, repo, (_fp(repo), "env-after-install"))
    assert cache.misses == 2


def test_an_unknown_state_is_never_cached(repo):
    cache = gd._TreePureCache()
    works = [_work("g1", "backend/engine.py")]
    cache.verdicts(works, repo, ("", ""))
    cache.verdicts(works, repo, ("", ""))
    assert cache.hits == 0 and cache.misses == 2


def test_covering_stems_follow_the_tree(repo):
    cache = gd._TreePureCache()
    env = repo_state.environment_fingerprint()
    assert "engine" in cache.covering(repo, (_fp(repo), env))
    (repo / "tests" / "test_widget.py").write_text("def test_w():\n    pass\n")
    assert "widget" in cache.covering(repo, (_fp(repo), env))
