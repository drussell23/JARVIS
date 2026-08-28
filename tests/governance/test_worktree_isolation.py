# tests/governance/test_worktree_isolation.py
"""WorktreeManager: async git worktree lifecycle for subagent isolation."""
import asyncio
import inspect
import pytest
from pathlib import Path

from backend.core.ouroboros.governance.worktree_manager import WorktreeManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo with one empty commit so worktrees work."""
    cmds = [
        ["git", "-C", str(path), "init"],
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        ["git", "-C", str(path), "config", "user.name", "Test"],
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init"],
    ]
    for cmd in cmds:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _orphan(*worktrees: Path) -> None:
    """Mark worktrees as debris left by a process that no longer exists.

    `reap_orphans` reaps what SIGKILL / OOM / power-loss left behind — by
    definition the creating process is gone. Tests build their fixtures
    in-process, so without this the workspace lock records a LIVE pid (ours)
    and the reaper correctly refuses to touch them.

    That refusal is not a test artifact to work around: it is the production
    behaviour being protected. `resolve_loop_project_root` and the boot reaper
    really do run in the same process, which is precisely how the live
    workspace got eaten (bt-2026-08-28-065825). A test that creates a worktree
    and immediately calls it an orphan was only ever passing because nothing
    checked liveness.

    A crashed session leaves its lock behind holding a STALE pid, so that is
    what is simulated — not a deleted lock, which would test a different case.
    """
    import json
    import subprocess

    from backend.core.ouroboros.governance.worktree_manager import (
        workspace_lock_path,
    )

    # A genuinely dead pid: spawn a trivial child, reap it, reuse its number.
    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    proc.wait()
    dead_pid = proc.pid

    for wt in worktrees:
        lock = workspace_lock_path(wt)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(
            json.dumps({
                "schema_version": "workspace_lock.1",
                "pid": dead_pid,
                "created_at": 0.0,
                "proc_start": None,
                "session_id": "bt-dead-session",
                "branch_name": "",
            }),
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_create_and_cleanup(tmp_path: Path) -> None:
    """create() produces a worktree directory; cleanup() removes it."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    wt_path = await mgr.create("test-branch-create-cleanup")

    assert wt_path.exists(), f"Worktree path should exist after create(): {wt_path}"
    assert wt_path.is_dir(), "Worktree path should be a directory"

    await mgr.cleanup(wt_path)

    assert not wt_path.exists(), f"Worktree path should be gone after cleanup(): {wt_path}"


@pytest.mark.asyncio
async def test_cleanup_nonexistent_path_is_safe(tmp_path: Path) -> None:
    """cleanup() on a path that never existed must not raise."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    ghost = tmp_path / "nonexistent-worktree"

    # Must not raise
    await mgr.cleanup(ghost)


def test_worktree_manager_has_create_and_cleanup() -> None:
    """Structural: WorktreeManager exposes async create() and async cleanup()."""
    assert hasattr(WorktreeManager, "create"), "WorktreeManager must have a 'create' method"
    assert hasattr(WorktreeManager, "cleanup"), "WorktreeManager must have a 'cleanup' method"

    assert inspect.iscoroutinefunction(WorktreeManager.create), (
        "WorktreeManager.create must be an async method"
    )
    assert inspect.iscoroutinefunction(WorktreeManager.cleanup), (
        "WorktreeManager.cleanup must be an async method"
    )


def test_worktree_manager_has_reap_orphans() -> None:
    """Structural: WorktreeManager exposes async reap_orphans()."""
    assert hasattr(WorktreeManager, "reap_orphans"), (
        "WorktreeManager must have a 'reap_orphans' method"
    )
    assert inspect.iscoroutinefunction(WorktreeManager.reap_orphans), (
        "WorktreeManager.reap_orphans must be an async method"
    )


# ---------------------------------------------------------------------------
# Reaper helpers
# ---------------------------------------------------------------------------

async def _git(repo: Path, *args: str) -> tuple[int, str]:
    """Run git against repo; return (rc, stdout+stderr). Test harness only."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, (out + err).decode()


async def _list_branches(repo: Path) -> list[str]:
    _, blob = await _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/"
    )
    return [ln.strip() for ln in blob.splitlines() if ln.strip()]


async def _list_worktree_paths(repo: Path) -> list[str]:
    _, blob = await _git(repo, "worktree", "list", "--porcelain")
    paths: list[str] = []
    for ln in blob.splitlines():
        if ln.startswith("worktree "):
            paths.append(ln.split(None, 1)[1].strip())
    return paths


# ---------------------------------------------------------------------------
# Reaper behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reap_orphans_on_clean_repo_returns_zero(tmp_path: Path) -> None:
    """Idempotent: a repo with no orphans reaps nothing and does not raise."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    reaped = await mgr.reap_orphans()
    assert reaped == 0


@pytest.mark.asyncio
async def test_reap_orphans_removes_registered_unit_worktree(tmp_path: Path) -> None:
    """A worktree with 'unit-' branch is reaped (dir + branch + registration)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    wt_path = await mgr.create("unit-abc-graph-xyz")
    assert wt_path.exists()
    _orphan(wt_path)

    pre_branches = await _list_branches(repo_root)
    assert "unit-abc-graph-xyz" in pre_branches

    reaped = await mgr.reap_orphans()
    assert reaped == 1

    assert not wt_path.exists(), "worktree directory must be gone"
    post_branches = await _list_branches(repo_root)
    assert "unit-abc-graph-xyz" not in post_branches, "branch must be deleted"
    post_paths = await _list_worktree_paths(repo_root)
    assert all("unit-abc-graph-xyz" not in p for p in post_paths), (
        "git worktree registration must be gone"
    )


@pytest.mark.asyncio
async def test_reap_orphans_removes_unregistered_on_disk_dir(tmp_path: Path) -> None:
    """A leftover directory under worktree_base that git never knew about is reaped."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    wt_base = tmp_path / "worktrees"
    mgr = WorktreeManager(repo_root=repo_root, worktree_base=wt_base)

    orphan = wt_base / "unit-leftover-from-crash"
    orphan.mkdir(parents=True)
    (orphan / "stale-file.txt").write_text("from a prior run")
    assert orphan.exists()

    reaped = await mgr.reap_orphans()
    assert reaped == 1
    assert not orphan.exists(), "unregistered on-disk orphan must be removed"


@pytest.mark.asyncio
async def test_reap_orphans_preserves_non_unit_worktrees(tmp_path: Path) -> None:
    """A non-'unit-' worktree (e.g. a user-created feature branch) is left alone."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    user_wt = await mgr.create("feature-do-not-touch")
    unit_wt = await mgr.create("unit-xyz-graph-1")
    _orphan(user_wt, unit_wt)

    reaped = await mgr.reap_orphans()
    assert reaped == 1
    assert user_wt.exists(), "non-unit worktree must be preserved"
    assert not unit_wt.exists(), "unit-prefixed worktree must be reaped"

    branches = await _list_branches(repo_root)
    assert "feature-do-not-touch" in branches
    assert "unit-xyz-graph-1" not in branches


@pytest.mark.asyncio
async def test_reap_orphans_deletes_orphan_branch_with_no_worktree(tmp_path: Path) -> None:
    """A 'unit-' branch left behind after its worktree was removed is deleted.

    Without this, resubmitting the same unit_id in a later session would
    fail with 'branch already exists' from git worktree add -b.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    rc, _ = await _git(repo_root, "branch", "unit-stale-branch-only")
    assert rc == 0
    pre = await _list_branches(repo_root)
    assert "unit-stale-branch-only" in pre

    mgr = WorktreeManager(repo_root=repo_root)
    await mgr.reap_orphans()

    post = await _list_branches(repo_root)
    assert "unit-stale-branch-only" not in post


@pytest.mark.asyncio
async def test_reap_orphans_idempotent(tmp_path: Path) -> None:
    """Calling reap twice on an already-reaped state returns 0 the second time."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    _orphan(await mgr.create("unit-first"))

    first = await mgr.reap_orphans()
    second = await mgr.reap_orphans()

    assert first == 1
    assert second == 0


# ===========================================================================
# Live-session self-protection (bt-2026-08-28-065825)
# ===========================================================================
#
# Slice 44 added the `ouroboros__auto__bt-` prefix to the reap set to clear
# real debris (62 stale checkouts, 492k files, 13GB). The CURRENT session's
# workspace carries that same prefix, and nothing told the reaper the
# difference between a previous session's corpse and the tree it is standing
# in. Measured:
#
#   23:59:37  [FileIsolation] routed project_root -> ...-065825-174b22
#   23:59:41  _git_worktree_remove: git -C ...-065825-174b22 worktree remove
#   23:59:58  reap_orphans: reaped 1 orphan worktree(s) at boot
#   00:08:25  every op reaching APPLY: "armed but unusable (no .git)"
#
# Four seconds after arming a valid worktree, boot deleted it. Because
# `effective_execution_root` fails closed at the APPLY boundary, the ops it
# destroyed were the ones that had travelled furthest.


@pytest.mark.asyncio
async def test_reap_orphans_never_eats_the_live_session_workspace(
    tmp_path: Path, monkeypatch,
) -> None:
    """The workspace named by JARVIS_AUTO_COMMIT_WORKSPACE is not debris."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    live = await mgr.create("ouroboros/auto/bt-live-session-aaa111")
    stale = await mgr.create("ouroboros/auto/bt-dead-session-bbb222")
    _orphan(stale)

    # Exactly how the live workspace announces itself in production.
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_WORKSPACE", str(live))

    reaped = await mgr.reap_orphans()

    assert live.exists(), (
        "the live session's own workspace was reaped — this is the husk bug"
    )
    assert (live / ".git").exists(), "live workspace must remain a work-area"
    assert not stale.exists(), "genuine debris must still be reaped"
    assert reaped == 1


@pytest.mark.asyncio
async def test_reap_orphans_keeps_the_live_branch(
    tmp_path: Path, monkeypatch,
) -> None:
    """Deleting the live branch is the 'branch already exists' class.

    `reap_dangling_auto_branches` already excludes `current_branch`; the
    branch sweep inside `reap_orphans` had no such notion, so it would delete
    the live session's branch out from under its own worktree.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    live = await mgr.create("ouroboros/auto/bt-keepme-ccc333")
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_WORKSPACE", str(live))

    await mgr.reap_orphans()

    branches = await _list_branches(repo_root)
    assert "ouroboros/auto/bt-keepme-ccc333" in branches


@pytest.mark.asyncio
async def test_reap_orphans_protects_an_explicitly_passed_path(
    tmp_path: Path, monkeypatch,
) -> None:
    """`protect_paths` covers callers that know more than the environment."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)
    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)

    mgr = WorktreeManager(repo_root=repo_root)
    keep = await mgr.create("ouroboros/auto/bt-explicit-ddd444")

    await mgr.reap_orphans(protect_paths=[keep])

    assert keep.exists(), "explicitly protected worktree must survive"


@pytest.mark.asyncio
async def test_unset_env_reaps_everything_as_before(tmp_path: Path, monkeypatch) -> None:
    """With nothing live, behaviour is byte-identical to before the guard."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)
    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)
    monkeypatch.delenv("JARVIS_OUROBOROS_SESSION_ID", raising=False)

    mgr = WorktreeManager(repo_root=repo_root)
    a = await mgr.create("ouroboros/auto/bt-old-1")
    b = await mgr.create("ouroboros/auto/bt-old-2")
    _orphan(a, b)

    reaped = await mgr.reap_orphans()

    assert reaped == 2
    assert not a.exists() and not b.exists()


# ===========================================================================
# Workspace liveness lock — cross-process protection
# ===========================================================================
#
# The env-derived guard protects only the workspace THIS process armed, so a
# second worker's tree was still fair game. The lock is on disk, so every
# reaper in every process can see it.


@pytest.mark.asyncio
async def test_create_writes_an_atomic_liveness_lock(tmp_path: Path) -> None:
    """Materialization stamps pid + start timestamp, unconditionally.

    Unconditionally is the point: the Ledger-Sovereignty ownership marker
    carries the same facts but `master_enabled()` defaults FALSE, so it is
    absent in a default configuration. Reaping safety cannot depend on an
    unrelated feature flag.
    """
    import json

    from backend.core.ouroboros.governance.worktree_manager import (
        workspace_lock_path,
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    wt = await mgr.create("unit-lock-shape")

    lock = workspace_lock_path(wt)
    assert lock.exists(), "create() must stamp a liveness lock"
    payload = json.loads(lock.read_text(encoding="utf-8"))
    import os as _os
    assert payload["pid"] == _os.getpid()
    assert payload["created_at"] > 0
    assert "proc_start" in payload
    # No partial writes left behind by the atomic replace.
    assert not list(lock.parent.glob(".workspace_lock.*.tmp"))


@pytest.mark.asyncio
async def test_live_lock_survives_reaping_by_another_manager(
    tmp_path: Path, monkeypatch,
) -> None:
    """A tree locked by a LIVE process is untouchable, env or no env.

    This is the multi-process race: worker A materializes a workspace, worker
    B boots and sweeps. B has never heard of A's env vars — the lock is the
    only thing that can stop it.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)
    # Deliberately unset: prove the protection comes from the LOCK, not from
    # the environment guard.
    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)
    monkeypatch.delenv("JARVIS_OUROBOROS_SESSION_ID", raising=False)

    worker_a = WorktreeManager(repo_root=repo_root)
    live = await worker_a.create("unit-worker-a-live")
    dead = await worker_a.create("unit-worker-b-dead")
    _orphan(dead)  # only this one's owner has exited

    worker_b = WorktreeManager(repo_root=repo_root)
    reaped = await worker_b.reap_orphans()

    assert live.exists(), "a live process's workspace was destroyed"
    assert not dead.exists(), "genuine debris must still be reaped"
    assert reaped == 1


@pytest.mark.asyncio
async def test_recycled_pid_does_not_read_as_a_live_owner(
    tmp_path: Path, monkeypatch,
) -> None:
    """A stale lock whose pid was reused must not protect forever.

    `os.kill(pid, 0)` alone cannot tell "my session is alive" from "my session
    died and the OS handed that number to something else". On a long-lived
    host that would make debris permanently unreapable. The recorded start
    time settles it.
    """
    import json
    import os as _os

    from backend.core.ouroboros.governance.worktree_manager import (
        workspace_lock_path,
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)
    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)

    mgr = WorktreeManager(repo_root=repo_root)
    wt = await mgr.create("unit-recycled-pid")

    # Our pid IS alive — but the lock claims it started at a different time,
    # which is exactly what a recycled pid looks like.
    lock = workspace_lock_path(wt)
    payload = json.loads(lock.read_text(encoding="utf-8"))
    if payload.get("proc_start") is None:
        pytest.skip("psutil unavailable — reuse detection is not active here")
    payload["proc_start"] = float(payload["proc_start"]) - 9999.0
    lock.write_text(json.dumps(payload), encoding="utf-8")
    assert payload["pid"] == _os.getpid()

    reaped = await mgr.reap_orphans()

    assert reaped == 1, "a recycled pid must not protect stale debris"
    assert not wt.exists()


@pytest.mark.asyncio
async def test_unreadable_lock_is_treated_as_reapable(tmp_path: Path, monkeypatch) -> None:
    """A torn or malformed lock means 'unprovable', which means reapable.

    The opposite polarity would let one corrupt byte pin 13GB of debris on
    disk forever — the exact problem Slice 44's prefix expansion existed to
    solve.
    """
    from backend.core.ouroboros.governance.worktree_manager import (
        workspace_lock_path,
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)
    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)

    mgr = WorktreeManager(repo_root=repo_root)
    wt = await mgr.create("unit-torn-lock")
    workspace_lock_path(wt).write_text("{not json", encoding="utf-8")

    reaped = await mgr.reap_orphans()

    assert reaped == 1
    assert not wt.exists()
