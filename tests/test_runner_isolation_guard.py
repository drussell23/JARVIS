"""The test-isolation guard, proven against real processes, signals and git.

See tests/support/isolation_guard.py for why each mechanism exists. The
guard is installed session-wide by tests/conftest.py, so these tests exercise
the SAME fence every other test runs under.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.support import isolation_guard as iso

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


@pytest.fixture()
def fence(_runner_fence):
    _runner_fence.drain()
    yield _runner_fence
    _runner_fence.drain()


# --------------------------------------------------------------------------
# The fence
# --------------------------------------------------------------------------

def test_os_exit_in_the_runner_becomes_SystemExit_not_a_dead_run(fence):
    with pytest.raises(SystemExit) as exc:
        os._exit(75)
    assert exc.value.code == 75
    [v] = fence.drain()
    assert v.what == "os._exit" and v.detail == "75"


def test_a_watchdog_thread_calling_os_exit_just_ends_its_thread(fence):
    """The measured case: a harness watchdog armed by one test fired
    os._exit(75) minutes later, in another test's window."""
    t = threading.Thread(target=lambda: os._exit(75), name="shutdown-watchdog")
    t.start()
    t.join(5)
    assert not t.is_alive()
    [v] = fence.drain()
    assert v.thread == "shutdown-watchdog"


def test_a_signal_to_the_runners_own_group_is_refused(fence):
    os.killpg(os.getpgrp(), signal.SIGTERM)
    os.kill(0, signal.SIGTERM)
    kinds = [v.what for v in fence.drain()]
    assert kinds == ["os.killpg", "os.kill"]


def test_the_shutdown_reaper_cannot_sigkill_the_runner(fence):
    """The measured case (rc=137): harness._arm_shutdown_deadline's reaper
    thread sends SIGKILL to its own pid 25 s after a test arms it."""
    t = threading.Thread(target=lambda: os.kill(os.getpid(), signal.SIGKILL),
                         name="ov-shutdown-watchdog")
    t.start()
    t.join(5)
    [v] = fence.drain()
    assert "(the runner), SIGKILL" in v.detail and v.thread == "ov-shutdown-watchdog"


def test_a_self_signal_with_a_handler_still_arrives(fence):
    """Tests that exercise their own signal handler are legitimate."""
    got = []
    previous = signal.signal(signal.SIGUSR1, lambda *_: got.append(1))
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        time.sleep(0.1)
    finally:
        signal.signal(signal.SIGUSR1, previous)
    assert got == [1] and fence.drain() == []


def test_signals_to_other_groups_still_work(fence):
    child = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        os.killpg(child.pid, signal.SIGTERM)
        assert child.wait(10) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
    assert fence.drain() == []


def test_a_forked_child_can_still_os_exit(fence):
    """A multiprocessing fork child inherits the patch and MUST exit for real,
    or it would carry on running the test session."""
    pid = os.fork()
    if pid == 0:
        os._exit(7)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 7
    assert fence.drain() == []


# --------------------------------------------------------------------------
# Leaked children
# --------------------------------------------------------------------------

def test_a_leaked_session_leader_is_reaped_through_its_own_group(fence):
    before = iso.child_pids()
    leader = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, time; subprocess.Popen(['sleep','60']); time.sleep(60)"],
        start_new_session=True,
    )
    time.sleep(0.5)
    report = iso.reap_new_children(before, fence)
    assert report.reaped
    assert leader.wait(10) is not None
    assert iso.child_pids() - before == set()
    assert fence.drain() == []            # nothing touched the runner's group


def test_a_leak_in_the_runners_group_is_reaped_alone(fence):
    before = iso.child_pids()
    same_group = subprocess.Popen(["sleep", "60"])
    assert os.getpgid(same_group.pid) == os.getpgrp()
    report = iso.reap_new_children(before, fence)
    assert report.reaped and same_group.wait(10) is not None
    assert fence.drain() == []            # signalled by pid, never by group


def test_multiprocessing_pool_workers_are_not_leaks(fence):
    from concurrent.futures import ProcessPoolExecutor
    before = iso.child_pids()
    with ProcessPoolExecutor(max_workers=1) as pool:
        assert pool.submit(pow, 2, 5).result(30) == 32
        assert iso.reap_new_children(before, fence).reaped == []


# --------------------------------------------------------------------------
# Stale git locks
# --------------------------------------------------------------------------

def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_an_unheld_lock_is_removed(tmp_path):
    root = _repo(tmp_path)
    lock = root / ".git" / "index.lock"
    lock.write_text("")
    report = iso.sweep_git_locks(iso.git_admin_dirs(root))
    assert report.removed == [str(lock)] and not lock.exists()


def test_a_lock_a_live_process_holds_is_left_alone(tmp_path):
    root = _repo(tmp_path)
    lock = root / ".git" / "index.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         f"import time; f=open({str(lock)!r},'w'); print('open', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "open"
        report = iso.sweep_git_locks(iso.git_admin_dirs(root))
        assert lock.exists() and report.removed == []
        assert str(holder.pid) in report.held[0]
    finally:
        holder.kill()
        holder.wait(10)
    assert iso.sweep_git_locks(iso.git_admin_dirs(root)).removed == [str(lock)]


def test_linked_worktree_locks_are_covered(tmp_path):
    root = _repo(tmp_path)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "seed"], check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "--detach", str(wt)],
                   check=True)
    admin = [d for d in iso.git_admin_dirs(root) if d.parent.name == "worktrees"]
    assert admin, "the linked worktree's admin dir must be swept too"
    (admin[0] / "index.lock").write_text("")
    assert iso.sweep_git_locks(iso.git_admin_dirs(root)).removed


def test_without_proc_nothing_is_removed(tmp_path, monkeypatch):
    """Absence of evidence is not a dead holder."""
    root = _repo(tmp_path)
    lock = root / ".git" / "index.lock"
    lock.write_text("")
    monkeypatch.setattr(iso, "lock_holders", lambda _lock: None)
    report = iso.sweep_git_locks(iso.git_admin_dirs(root))
    assert lock.exists() and report.unverifiable == [str(lock)]


# --------------------------------------------------------------------------
# The production root of the exit hang
# --------------------------------------------------------------------------

class _Releasable:
    def __init__(self):
        self.released = 0

    async def release_resources(self):
        self.released += 1


def test_an_unadopted_oracle_is_released_and_an_adopted_one_is_not():
    """An Oracle abandoned mid-initialize kept its aiosqlite connection thread
    (non-daemon) alive, so the process could not exit."""
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopService,
    )
    svc = GovernedLoopService.__new__(GovernedLoopService)
    abandoned, adopted = _Releasable(), _Releasable()
    svc._oracle = adopted
    asyncio.run(svc._release_unadopted_oracle(abandoned))
    asyncio.run(svc._release_unadopted_oracle(adopted))
    asyncio.run(svc._release_unadopted_oracle(None))
    assert (abandoned.released, adopted.released) == (1, 0)


def test_oracle_release_does_not_save_a_half_built_graph():
    """shutdown() = save + release; the init-failure path must only release."""
    import inspect

    from backend.core.ouroboros.oracle import TheOracle
    release = inspect.getsource(TheOracle.release_resources)
    assert "_save_cache" not in release
    assert "release_resources()" in inspect.getsource(TheOracle.shutdown)
