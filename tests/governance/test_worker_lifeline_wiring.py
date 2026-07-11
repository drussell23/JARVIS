"""Wiring spine — worker_lifeline must have LIVE callers on the
default path (feedback_orchestrator_wiring_invariant_checklist: a
guard with zero callers is theater). Verifies all three spawn sites
arm it: FS process pool (initializer + recycling), oracle_ipc worker,
subprocess_compute.run_worker_loop.
"""
from __future__ import annotations

import os
import sys

import pytest

from backend.core.ouroboros.governance import cooperative_fs_io as cfio
from backend.core.ouroboros.governance import worker_lifeline as wl


@pytest.fixture()
def fresh_pool_state(monkeypatch: pytest.MonkeyPatch):
    """Isolate the module-level pool singleton per test."""
    monkeypatch.setattr(cfio, "_FS_PROCESS_POOL", None)
    yield
    pool = cfio._FS_PROCESS_POOL
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
        monkeypatch.setattr(cfio, "_FS_PROCESS_POOL", None)


# ---------------------------------------------------------------------------
# FS process pool — initializer + recycling knob
# ---------------------------------------------------------------------------


def test_fs_pool_arms_lifeline_initializer(fresh_pool_state) -> None:
    pool = cfio._get_fs_process_pool()
    assert getattr(pool, "_initializer", None) is wl.pool_worker_initializer
    initargs = getattr(pool, "_initargs", ())
    assert initargs and initargs[0] == "fs_process_pool"
    # The parent pid shipped at pool build — the arm-after-death seam.
    assert initargs[1] == os.getpid()


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="max_tasks_per_child is a 3.11+ ProcessPoolExecutor feature",
)
def test_fs_pool_recycles_workers_by_default(fresh_pool_state) -> None:
    pool = cfio._get_fs_process_pool()
    assert getattr(pool, "_max_tasks_per_child", None) == 64


def test_fs_pool_recycling_env_disable(
    fresh_pool_state, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_FS_POOL_MAX_TASKS_PER_CHILD", "0")
    assert cfio.fs_pool_max_tasks_per_child() is None
    pool = cfio._get_fs_process_pool()
    assert getattr(pool, "_max_tasks_per_child", None) is None


def test_fs_pool_recycling_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_FS_POOL_MAX_TASKS_PER_CHILD", "7")
    if sys.version_info >= (3, 11):
        assert cfio.fs_pool_max_tasks_per_child() == 7
    else:
        assert cfio.fs_pool_max_tasks_per_child() is None


def test_fs_pool_offload_survives_lifeline_wiring(fresh_pool_state) -> None:
    """End-to-end: a real cpu_bound offload through the armed pool
    still round-trips (the initializer must not poison workers)."""
    import asyncio

    async def _go():
        return await cfio.offload(len, "lifeline", cpu_bound=True)

    result = asyncio.run(_go())
    assert result == 8


# ---------------------------------------------------------------------------
# oracle_ipc worker — arm at entry, parent pid shipped at spawn
# ---------------------------------------------------------------------------


def test_oracle_worker_main_arms_lifeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.core.ouroboros import oracle_ipc

    armed: list = []

    def _fake_arm(role: str, **kwargs):
        armed.append((role, kwargs.get("expected_parent_pid")))
        return None

    monkeypatch.setattr(wl, "arm_worker_lifeline", _fake_arm)

    async def _noop_async(conn):  # noqa: ARG001
        return None

    monkeypatch.setattr(oracle_ipc, "_oracle_worker_async", _noop_async)

    class _FakeConn:
        def send(self, msg):  # noqa: ANN001
            pass

        def close(self):
            pass

    oracle_ipc._oracle_worker_main(_FakeConn(), 424242)
    assert armed == [("oracle_ipc", 424242)]


def test_oracle_default_spawn_ships_parent_pid() -> None:
    """AST-free source pin: _default_spawn must pass os.getpid() into
    the worker args (the lifeline baseline)."""
    import inspect

    from backend.core.ouroboros import oracle_ipc

    src = inspect.getsource(oracle_ipc._default_spawn)
    assert "os.getpid()" in src, (
        "_default_spawn no longer ships the parent pid — the oracle "
        "worker's lifeline baseline breaks on the arm-after-death race"
    )


# ---------------------------------------------------------------------------
# subprocess_compute.run_worker_loop — arm at worker entry
# ---------------------------------------------------------------------------


def test_run_worker_loop_arms_lifeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.core.ouroboros.governance import subprocess_compute as sc

    armed: list = []
    monkeypatch.setattr(
        wl, "arm_worker_lifeline",
        lambda role, **kw: armed.append(role) or None,
    )

    class _EOFConn:
        def send(self, msg):  # noqa: ANN001
            pass

        def recv(self):
            raise EOFError

    sc.run_worker_loop(_EOFConn(), {})
    assert armed == ["subprocess_compute"]
