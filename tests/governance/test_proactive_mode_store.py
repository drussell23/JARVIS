"""Regression spine for dial persistence (PRD §30.11 Q1 + Q4)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import proactive_mode_store as st


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("JARVIS_PROACTIVE_MODE_PERSIST",
              "JARVIS_PROACTIVE_MODE_FIRST_CONTACT", "JARVIS_PROJECT_ROOT"):
        monkeypatch.delenv(k, raising=False)
    st.reset_store()
    yield
    st.reset_store()


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git("init", "-q", cwd=r)
    _git("config", "user.email", "t@t", cwd=r)
    _git("config", "user.name", "t", cwd=r)
    (r / "f.txt").write_text("x", encoding="utf-8")
    _git("add", "f.txt", cwd=r)
    _git("commit", "-qm", "init", cwd=r)
    return r


# -- Q1: first contact -----------------------------------------------------


def test_first_contact_is_watch():
    """§30.11 Q1 — a checkout the operator has not vouched for must not
    spend tokens before they look."""
    assert st.first_contact_default() == "watch"


def test_a_fresh_checkout_hydrates_to_watch(repo):
    assert asyncio.run(st.get_store().hydrate(repo)) == "watch"


def test_first_contact_is_not_the_parse_fallback():
    """'I cannot read this name' and 'nobody has chosen yet' are different
    states; collapsing them would make an unparseable string mean the
    operator asked for zero-touch observation."""
    from backend.core.ouroboros.governance import proactive_mode as pm
    assert pm.position("nonsense").name == "safe_auto"
    assert st.first_contact_default() == "watch"


# -- Q4: per-repository persistence ---------------------------------------


def test_the_dial_round_trips_for_a_checkout(repo):
    async def _go():
        s = st.get_store()
        await s.hydrate(repo)
        assert await s.remember("explore") is True
        st.reset_store()
        return await st.get_store().hydrate(repo)
    assert asyncio.run(_go()) == "explore"


def test_state_lives_beside_the_working_tree(repo):
    loc = asyncio.run(st.locate(repo))
    assert loc.worktree == repo.resolve()
    assert loc.path == repo.resolve() / ".jarvis" / st.STATE_FILENAME


def test_two_repositories_do_not_share_a_dial(tmp_path, repo):
    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-q", cwd=other)

    async def _go():
        s = st.get_store()
        await s.hydrate(repo)
        await s.remember("watch")
        st.reset_store()
        s2 = st.get_store()
        await s2.hydrate(other)
        await s2.remember("safe_auto")
        st.reset_store()
        return await st.get_store().hydrate(repo)
    assert asyncio.run(_go()) == "watch", "a second repo clobbered the first"


# -- the worktree illusion -------------------------------------------------


def test_git_worktrees_keep_separate_dials(repo, tmp_path):
    """THE worktree regression. Several directories, one .git backend —
    keying on the git dir would make an operator running `watch` in a review
    checkout silently loosen when a colleague set safe_auto in a feature
    worktree."""
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feature", str(wt), cwd=repo)

    main_loc = asyncio.run(st.locate(repo))
    wt_loc = asyncio.run(st.locate(wt))
    assert main_loc.path != wt_loc.path, "worktrees shared one dial file"
    assert wt_loc.worktree == wt.resolve()

    async def _go():
        s = st.get_store()
        await s.hydrate(repo)
        await s.remember("watch")
        st.reset_store()
        s2 = st.get_store()
        await s2.hydrate(wt)
        await s2.remember("safe_auto")
        st.reset_store()
        return await st.get_store().hydrate(repo)
    assert asyncio.run(_go()) == "watch", "the worktree clobbered main"


def _code_of(fn) -> str:
    """Executable body with the docstring stripped.

    Every prose check in this suite goes through here. Four times this
    session a raw text search matched a docstring that EXPLAINED why
    something was not used — a substring cannot tell an explanation from a
    use, and these modules explain themselves at length."""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    fn_node = tree.body[0]
    body = (fn_node.body[1:]
            if isinstance(fn_node.body[0], ast.Expr) else fn_node.body)
    return "\n".join(ast.unparse(stmt) for stmt in body)


def test_the_worktree_is_resolved_by_toplevel_not_git_dir():
    """--show-toplevel is per-worktree; --git-dir is shared. The fix is the
    coordinate system, not a special case."""
    code = _code_of(st._git_toplevel)
    assert "--show-toplevel" in code
    assert "--git-dir" not in code


# -- degradation always toward watch --------------------------------------


def test_outside_a_repository_degrades_to_watch(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    loc = asyncio.run(st.locate(scratch))
    assert loc.persistent is False
    assert asyncio.run(st.get_store().hydrate(scratch)) == "watch"


def test_a_readonly_workspace_degrades_without_crashing(repo):
    """A read-only mount must cost the persistence and never the loop."""
    jarvis = repo / ".jarvis"
    jarvis.mkdir(parents=True, exist_ok=True)
    os.chmod(jarvis, 0o500)
    try:
        async def _go():
            s = st.get_store()
            rung = await s.hydrate(repo)
            wrote = await s.remember("explore")
            return rung, wrote, s.snapshot()
        rung, wrote, snap = asyncio.run(_go())
        assert rung == "watch"
        assert wrote is False, "claimed a write onto a read-only tree"
        assert snap["degraded"]
    finally:
        os.chmod(jarvis, 0o700)


def test_a_corrupt_state_file_falls_back_to_watch_not_to_the_last_dial(repo):
    """Unreadable is NOT 'no preference'. An unknown judgement must not be
    resolved in the organism's favour."""
    p = repo / ".jarvis" / st.STATE_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert asyncio.run(st.get_store().hydrate(repo)) == "watch"


def test_a_rung_not_on_the_ladder_falls_back_to_watch(repo):
    p = repo / ".jarvis" / st.STATE_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"position": "turbo"}), encoding="utf-8")
    assert asyncio.run(st.get_store().hydrate(repo)) == "watch"


def test_every_failure_path_ends_at_watch():
    """The one place in this codebase that fails CLOSED: a broken disk must
    not grant authority."""
    import inspect
    src = inspect.getsource(st.ProactiveModeStore.hydrate)
    returns = [ln for ln in src.splitlines() if "return " in ln]
    assert returns, "hydrate has no returns"
    for ln in returns:
        assert "first_contact_default" in ln or "return name" in ln


def test_repeated_failures_warn_once_not_per_keystroke(repo, caplog):
    """An operator cycling the dial should not get one warning per press."""
    jarvis = repo / ".jarvis"
    jarvis.mkdir(parents=True, exist_ok=True)
    os.chmod(jarvis, 0o500)
    try:
        async def _go():
            s = st.get_store()
            await s.hydrate(repo)
            for _ in range(5):
                await s.remember("explore")
        caplog.clear()
        asyncio.run(_go())
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warns) <= 1
    finally:
        os.chmod(jarvis, 0o700)


# -- posture ---------------------------------------------------------------


def test_persistence_can_be_switched_off(monkeypatch, repo):
    monkeypatch.setenv("JARVIS_PROACTIVE_MODE_PERSIST", "0")
    loc = asyncio.run(st.locate(repo))
    assert loc.persistent is False


def test_writability_is_probed_by_acting_not_by_inspecting():
    """os.access answers about permission bits; the failures that matter
    are a read-only MOUNT, a full disk, an immutable flag."""
    code = _code_of(st._probe_writable)
    assert "os.access" not in code
    assert "write_text" in code and "unlink" in code


def test_the_state_is_published_atomically():
    import inspect
    assert "atomic_replace" in inspect.getsource(st._write_atomic)


def test_the_resolver_is_bounded_and_reaps_its_child():
    """A git call against a dead network mount hangs for the mount's own
    timeout."""
    code = _code_of(st._git_toplevel)
    assert "wait_for" in code and "kill()" in code


def test_the_snapshot_is_serialisable(repo):
    async def _go():
        s = st.get_store()
        await s.hydrate(repo)
        return s.snapshot()
    json.dumps(asyncio.run(_go()))
