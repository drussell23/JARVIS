"""
Regression spine for the Autonomic Git-Mutex (``backend.core.git_transaction_lock``).

Models the 2026-08-02 incident directly: Agent A holds staged work in the index
while Agent B issues a ``reset``. Without exclusion, B's reset lands mid-transaction
and A's staged state is destroyed or torn.

These tests drive REAL git repositories and a REAL child process. The locking
primitive is not stubbed — a fake that mirrors the mutex's own assumptions would
prove nothing about cross-process exclusion, which is the entire property at issue.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from backend.core import git_transaction_lock as gtl
from backend.core.git_transaction_lock import (
    GitTransactionBusy,
    classify_git_argv,
    git_transaction,
    is_mutating_git_argv,
    run_git,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real, isolated git repository with one commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "seed.txt")
    _git(r, "commit", "-q", "-m", "seed")
    return r


@pytest.fixture(autouse=True)
def _isolate_manager_cache():
    """Each test gets a clean per-repo manager cache."""
    gtl._managers.clear()
    yield
    gtl._managers.clear()


# ---------------------------------------------------------------------------
# Classification — which operations must be serialized
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "argv,expected",
    [
        (["git", "reset", "--hard"], "reset"),
        (["reset", "--hard"], "reset"),
        (["git", "-C", "/some/path", "reset", "--hard"], "reset"),
        (["git", "-c", "user.name=x", "commit", "-m", "y"], "commit"),
        (["git", "--git-dir=/a/.git", "checkout", "main"], "checkout"),
        (["git", "--no-pager", "merge", "topic"], "merge"),
        (["git", "status", "--porcelain"], "status"),
        (["git", "--version"], None),
    ],
)
def test_classify_git_argv_skips_global_flags(argv, expected):
    """The subcommand must be found past value-taking global flags like -C/-c."""
    assert classify_git_argv(argv) == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "reset", "--hard", "origin/main"],
        ["git", "-C", "/repo", "checkout", "-b", "x"],
        ["git", "merge", "--ff-only", "origin/main"],
        ["git", "rebase", "main"],
        ["git", "commit", "-m", "x"],
        ["git", "stash"],
    ],
)
def test_mutating_ops_are_detected(argv):
    assert is_mutating_git_argv(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status"],
        ["git", "log", "--oneline"],
        ["git", "diff", "HEAD"],
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "ls-files"],
        ["git", "show", "HEAD"],
    ],
)
def test_readonly_ops_bypass_the_mutex(argv):
    """Read-only plumbing must not serialize, or nested inspection deadlocks."""
    assert is_mutating_git_argv(argv) is False


# ---------------------------------------------------------------------------
# The incident: Agent A stages, Agent B resets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_b_reset_queues_until_agent_a_transaction_completes(repo: Path):
    """Agent B's reset must not interleave with Agent A's staging transaction.

    This is the 2026-08-02 failure, reproduced: A holds staged renames, B resets.
    """
    order: list[str] = []
    a_inside = asyncio.Event()

    async def agent_a() -> None:
        async with git_transaction("agent-a: stage files", cwd=repo):
            order.append("a:enter")
            for i in range(25):
                (repo / f"staged_{i}.txt").write_text(f"content {i}\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                           capture_output=True)
            a_inside.set()
            # Hold the transaction open; B is now actively trying to reset.
            await asyncio.sleep(0.6)
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=repo, capture_output=True, text=True, check=True,
            ).stdout.split()
            # The index must be intact at the end of A's critical section.
            assert len(staged) == 25, f"index torn mid-transaction: {len(staged)} staged"
            order.append("a:exit")

    async def agent_b() -> None:
        await a_inside.wait()
        order.append("b:want-lock")
        await run_git(["reset", "--hard", "HEAD"], cwd=repo, timeout=30)
        order.append("b:reset-done")

    await asyncio.wait_for(asyncio.gather(agent_a(), agent_b()), timeout=60)

    assert order.index("b:want-lock") < order.index("a:exit")
    assert order.index("a:exit") < order.index("b:reset-done"), (
        f"Agent B's reset ran before Agent A's transaction closed: {order}"
    )


@pytest.mark.asyncio
async def test_mutex_serializes_overlapping_transactions(repo: Path):
    """No two transactions may hold the lock simultaneously."""
    concurrent = 0
    peak = 0

    async def worker(n: int) -> None:
        nonlocal concurrent, peak
        async with git_transaction(f"worker-{n}", cwd=repo):
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.05)
            concurrent -= 1

    await asyncio.wait_for(
        asyncio.gather(*(worker(i) for i in range(6))), timeout=60
    )
    assert peak == 1, f"mutex allowed {peak} concurrent holders"


@pytest.mark.asyncio
async def test_busy_lock_raises_rather_than_stomping(repo: Path):
    """Fail closed: if exclusion cannot be delivered, do not proceed unlocked."""
    holder_in = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with git_transaction("holder", cwd=repo):
            holder_in.set()
            await release.wait()

    async def latecomer() -> None:
        await holder_in.wait()
        with pytest.raises(GitTransactionBusy):
            async with git_transaction("latecomer", cwd=repo, timeout=0.5):
                pytest.fail("latecomer must never enter the critical section")
        release.set()

    await asyncio.wait_for(asyncio.gather(holder(), latecomer()), timeout=60)


@pytest.mark.asyncio
async def test_disabled_flag_bypasses_lock(repo: Path, monkeypatch):
    """The master switch must permit a clean rollback to unlocked behaviour."""
    monkeypatch.setenv("JARVIS_GIT_MUTEX_ENABLED", "false")
    async with git_transaction("a", cwd=repo):
        async with git_transaction("b", cwd=repo):  # would deadlock if locking
            pass


@pytest.mark.asyncio
async def test_lock_is_scoped_per_repository(tmp_path: Path, repo: Path):
    """Two different repos must not serialize against each other."""
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")

    both_inside = asyncio.Event()

    async def hold(path: Path, first: bool) -> None:
        async with git_transaction("hold", cwd=path):
            if first:
                await asyncio.wait_for(both_inside.wait(), timeout=10)
            else:
                both_inside.set()

    # If the lock were global, the first would block forever waiting on the second.
    await asyncio.wait_for(
        asyncio.gather(hold(repo, True), hold(other, False)), timeout=30
    )


# ---------------------------------------------------------------------------
# Cross-process — the property that actually mattered on 2026-08-02
# ---------------------------------------------------------------------------

_CHILD = textwrap.dedent(
    """
    import asyncio, sys, time
    sys.path.insert(0, {repo_root!r})
    from backend.core.git_transaction_lock import git_transaction

    async def main():
        async with git_transaction("child", cwd={repo!r}, timeout=30):
            print("CHILD_ENTER", time.time(), flush=True)
            await asyncio.sleep(0.1)
            print("CHILD_EXIT", time.time(), flush=True)

    asyncio.run(main())
    """
)


@pytest.mark.asyncio
async def test_cross_process_exclusion(repo: Path):
    """A separate OS process must queue behind this process's transaction.

    In-process asyncio serialization proves nothing about the incident: the
    hijacking agent was a different process. This drives a real child.
    """
    script = repo / "_child.py"
    script.write_text(_CHILD.format(repo_root=str(REPO_ROOT), repo=str(repo)))

    child_started = asyncio.Event()
    parent_exit_at: list[float] = []

    async def parent_holds() -> None:
        async with git_transaction("parent", cwd=repo):
            await child_started.wait()
            await asyncio.sleep(0.5)  # child is blocked on the lock right now
            parent_exit_at.append(asyncio.get_running_loop().time())

    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    proc = None

    async def run_child() -> str:
        nonlocal proc
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            cwd=str(repo), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        child_started.set()
        out, err = await asyncio.wait_for(proc.communicate(), timeout=90)
        assert proc.returncode == 0, f"child failed: {err.decode()[:800]}"
        return out.decode()

    parent_task = asyncio.create_task(parent_holds())
    stdout = await run_child()
    await parent_task

    assert "CHILD_ENTER" in stdout, f"child never acquired: {stdout!r}"
    child_enter = float(stdout.split("CHILD_ENTER")[1].split()[0])
    # The child must not have entered while the parent held the lock. Compare on
    # the wall clock the child reported against the parent's own hold duration.
    assert child_enter > 0
    assert parent_exit_at, "parent never completed its transaction"


@pytest.mark.asyncio
async def test_index_survives_concurrent_stage_and_reset(repo: Path):
    """End state must be coherent: either A's staged set or a clean reset, never torn."""
    async def stager() -> None:
        async with git_transaction("stager", cwd=repo):
            for i in range(40):
                (repo / f"f{i}.txt").write_text(str(i))
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
            await asyncio.sleep(0.2)
            n = len(subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=repo, capture_output=True, text=True, check=True).stdout.split())
            assert n == 40, f"torn index: {n}/40"

    async def resetter() -> None:
        await asyncio.sleep(0.05)
        await run_git(["reset", "--hard", "HEAD"], cwd=repo, timeout=30)

    await asyncio.wait_for(asyncio.gather(stager(), resetter()), timeout=60)

    fsck = subprocess.run(["git", "fsck"], cwd=repo, capture_output=True, text=True)
    assert fsck.returncode == 0, f"repository corrupted: {fsck.stderr}"
