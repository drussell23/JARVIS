"""ov cockpit silence Slice 2 Task 5 — F3 + F4 boot-time hygiene reapers.

Live run bt-2026-07-08-013911 surfaced two gaps:

F3: ``.jarvis/coherence_window.jsonl.lock`` (and friends) survived boot
8h-stale because the existing ``_reap_stale_jarvis_locks`` debris sweep
uses a 24h default -- far looser than CrossProcessJSONL's own 300s
staleness threshold. These lock files carry no PID payload (unlike
``intake_router.lock``), so liveness is proven the same way the module
proves it internally: a non-blocking flock attempt. A lock currently
held by a live process is NEVER touched, regardless of mtime age.

F4: a dangling ``ouroboros/auto/<session>-<nonce>`` branch from a dead
session collided with the current session's own branch-name at
``git worktree add -b``, after which AutoCommitter refused commits for
the rest of the session. Liveness is proven via the ledger_sovereignty
ownership marker's ``creator_pid`` (an ``os.kill(pid, 0)`` probe) --
never bare name-matching. ``unit-*`` worktrees and the current
session's own branch are always out of scope / preserved.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import time
from pathlib import Path

import scripts.ouroboros_battle_test as bt
from backend.core.ouroboros.governance.ledger_sovereignty import mark_owned
from backend.core.ouroboros.governance.worktree_manager import WorktreeManager

# An implausibly large PID -- on both macOS and Linux this is guaranteed
# to be unassigned, so os.kill(_DEAD_PID, 0) always raises
# ProcessLookupError without the fragility of forking inside a pytest +
# asyncio process.
_DEAD_PID = 2**30 + 424242


def _age(path: Path, seconds_old: float) -> None:
    """Backdate a file's mtime by ``seconds_old``."""
    past = time.time() - seconds_old
    os.utime(path, (past, past))


# ---------------------------------------------------------------------------
# F3 — stale CrossProcessJSONL *.jsonl.lock reaper
# ---------------------------------------------------------------------------


def test_stale_uncontended_lock_is_reaped(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_STALE_LOCK_AGE_S", raising=False)
    jarvis = tmp_path / ".jarvis"
    jarvis.mkdir()
    lock = jarvis / "coherence_window.jsonl.lock"
    lock.write_text("")
    _age(lock, 600.0)  # 10min > 300s default threshold; nobody holds it

    reaped = bt._reap_stale_cross_process_jsonl_locks(jarvis, quiet=True)

    assert reaped == 1
    assert not lock.exists()


def test_fresh_lock_is_kept(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_STALE_LOCK_AGE_S", raising=False)
    jarvis = tmp_path / ".jarvis"
    jarvis.mkdir()
    lock = jarvis / "coherence_window.jsonl.lock"
    lock.write_text("")  # fresh mtime, well under the 300s threshold

    reaped = bt._reap_stale_cross_process_jsonl_locks(jarvis, quiet=True)

    assert reaped == 0
    assert lock.exists()


def test_live_held_lock_is_never_reaped_even_when_stale(tmp_path, monkeypatch):
    """Binding constraint: NEVER touch a lock owned by a live PID --
    proven here via genuine OS-level flock contention, not just mtime,
    since these lock files carry no PID payload to read instead."""
    monkeypatch.delenv("JARVIS_STALE_LOCK_AGE_S", raising=False)
    jarvis = tmp_path / ".jarvis"
    jarvis.mkdir()
    lock = jarvis / "coherence_window.jsonl.lock"
    lock.write_text("")
    _age(lock, 600.0)  # stale by mtime alone

    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)  # simulate a live holder
    try:
        reaped = bt._reap_stale_cross_process_jsonl_locks(jarvis, quiet=True)
        assert reaped == 0
        assert lock.exists()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_missing_jarvis_dir_is_safe():
    reaped = bt._reap_stale_cross_process_jsonl_locks(
        Path("/nonexistent/does/not/exist"), quiet=True,
    )
    assert reaped == 0


def test_non_jsonl_lock_untouched(tmp_path, monkeypatch):
    """intake_router.lock (and any other non-.jsonl .lock file) is
    outside this reaper's scope -- it only matches *.jsonl.lock."""
    monkeypatch.delenv("JARVIS_STALE_LOCK_AGE_S", raising=False)
    jarvis = tmp_path / ".jarvis"
    jarvis.mkdir()
    router = jarvis / "intake_router.lock"
    router.write_text('{"pid": 123}')
    _age(router, 99999.0)

    reaped = bt._reap_stale_cross_process_jsonl_locks(jarvis, quiet=True)

    assert reaped == 0
    assert router.exists()


# ---------------------------------------------------------------------------
# F4 — dangling ouroboros/auto/* branch + worktree reaper
# ---------------------------------------------------------------------------


async def _run_git(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode()


async def _init_git_repo(path: Path) -> None:
    for cmd in (
        ["-C", str(path), "init"],
        ["-C", str(path), "config", "user.email", "t@t.com"],
        ["-C", str(path), "config", "user.name", "T"],
        ["-C", str(path), "commit", "--allow-empty", "-m", "init"],
    ):
        await _run_git(*cmd)


async def _git_worktree_add(repo: Path, branch: str, wt_dir: Path) -> None:
    await _run_git("-C", str(repo), "worktree", "add", "-b", branch, str(wt_dir))


async def _list_auto_branches(repo: Path) -> "list[str]":
    out = await _run_git(
        "-C", str(repo), "for-each-ref",
        "--format=%(refname:short)", "refs/heads/ouroboros/auto/*",
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def _stamp_marker(wt_dir: Path, *, session_id: str, branch_name: str, pid: int) -> None:
    mark_owned(wt_dir, session_id=session_id, branch_name=branch_name, creator_pid=pid)


async def test_dangling_branch_from_dead_session_is_reaped(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    await _init_git_repo(repo)
    base = repo / ".worktrees"
    base.mkdir()

    dead_branch = "ouroboros/auto/bt-dead-c21890"
    dead_dir = base / "ouroboros__auto__bt-dead-c21890"
    await _git_worktree_add(repo, dead_branch, dead_dir)
    assert dead_dir.exists(), "setup: worktree should exist"
    _stamp_marker(dead_dir, session_id="bt-dead", branch_name=dead_branch, pid=_DEAD_PID)

    mgr = WorktreeManager(repo_root=repo, worktree_base=base)
    reaped = await mgr.reap_dangling_auto_branches(
        current_branch="ouroboros/auto/bt-live-aaaaaa",
    )

    assert reaped == 1
    assert not dead_dir.exists()
    # No unique commits on the dead branch -> tip reachable from the repo's
    # own default branch -> ref safely deleted too (not just the checkout).
    assert dead_branch not in await _list_auto_branches(repo)


async def test_live_session_worktree_is_preserved(tmp_path):
    """A worktree owned by a genuinely alive PID (our own test process)
    must survive -- proves liveness is keyed on PID, not name-matching,
    even for a session that is NOT the current one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    await _init_git_repo(repo)
    base = repo / ".worktrees"
    base.mkdir()

    live_branch = "ouroboros/auto/bt-otherlive-bbbbbb"
    live_dir = base / "ouroboros__auto__bt-otherlive-bbbbbb"
    await _git_worktree_add(repo, live_branch, live_dir)
    _stamp_marker(live_dir, session_id="bt-otherlive", branch_name=live_branch, pid=os.getpid())

    mgr = WorktreeManager(repo_root=repo, worktree_base=base)
    reaped = await mgr.reap_dangling_auto_branches(
        current_branch="ouroboros/auto/bt-current-cccccc",
    )

    assert reaped == 0
    assert live_dir.exists()
    assert live_branch in await _list_auto_branches(repo)


async def test_current_session_own_branch_is_never_touched(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    await _init_git_repo(repo)
    base = repo / ".worktrees"
    base.mkdir()

    own_branch = "ouroboros/auto/bt-current-cccccc"
    own_dir = base / "ouroboros__auto__bt-current-cccccc"
    await _git_worktree_add(repo, own_branch, own_dir)
    # Even if (implausibly) marked with a dead PID, the exact-name match
    # against current_branch must win -- never self-reap.
    _stamp_marker(own_dir, session_id="bt-current", branch_name=own_branch, pid=_DEAD_PID)

    mgr = WorktreeManager(repo_root=repo, worktree_base=base)
    reaped = await mgr.reap_dangling_auto_branches(current_branch=own_branch)

    assert reaped == 0
    assert own_dir.exists()
    assert own_branch in await _list_auto_branches(repo)


async def test_unit_prefix_worktree_untouched(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    await _init_git_repo(repo)
    base = repo / ".worktrees"
    base.mkdir()

    unit_dir = base / "unit-abc123"
    await _git_worktree_add(repo, "unit-abc123", unit_dir)

    mgr = WorktreeManager(repo_root=repo, worktree_base=base)
    reaped = await mgr.reap_dangling_auto_branches(
        current_branch="ouroboros/auto/bt-current-cccccc",
    )

    assert reaped == 0
    assert unit_dir.exists()


async def test_dead_session_branch_with_unique_commit_ref_preserved(tmp_path):
    """A dead session that DID land an autonomous commit: the worktree
    dir is reaped (disk reclaim), but the branch ref -- carrying a
    commit unreachable from any other ref -- is preserved as forensic
    evidence rather than silently discarding unpushed work."""
    repo = tmp_path / "repo"
    repo.mkdir()
    await _init_git_repo(repo)
    base = repo / ".worktrees"
    base.mkdir()

    dead_branch = "ouroboros/auto/bt-dead-uniquecommit"
    dead_dir = base / "ouroboros__auto__bt-dead-uniquecommit"
    await _git_worktree_add(repo, dead_branch, dead_dir)
    (dead_dir / "scratch.txt").write_text("autonomous work")
    await _run_git("-C", str(dead_dir), "add", "scratch.txt")
    await _run_git("-C", str(dead_dir), "commit", "-m", "autonomous change")
    _stamp_marker(dead_dir, session_id="bt-dead2", branch_name=dead_branch, pid=_DEAD_PID)

    mgr = WorktreeManager(repo_root=repo, worktree_base=base)
    reaped = await mgr.reap_dangling_auto_branches(
        current_branch="ouroboros/auto/bt-live-zzzzzz",
    )

    assert reaped == 1  # worktree checkout reclaimed
    assert not dead_dir.exists()
    assert dead_branch in await _list_auto_branches(repo)  # ref PRESERVED


async def test_missing_marker_is_never_reaped(tmp_path):
    """No ownership marker at all -> unknown status -> conservative
    skip, even though the branch matches the prefix and isn't current."""
    repo = tmp_path / "repo"
    repo.mkdir()
    await _init_git_repo(repo)
    base = repo / ".worktrees"
    base.mkdir()

    unmarked_branch = "ouroboros/auto/bt-unmarked-dddddd"
    unmarked_dir = base / "ouroboros__auto__bt-unmarked-dddddd"
    await _git_worktree_add(repo, unmarked_branch, unmarked_dir)
    # No mark_owned() call -- no marker on disk.

    mgr = WorktreeManager(repo_root=repo, worktree_base=base)
    reaped = await mgr.reap_dangling_auto_branches(
        current_branch="ouroboros/auto/bt-current-cccccc",
    )

    assert reaped == 0
    assert unmarked_dir.exists()
