"""The out-of-process memory guard: when it stops the kernel, and when it must not."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from backend.core import memory_guard as mg
from backend.core.memory_types import PressurePolicy, PressureTier


def _guard(tmp_path, stopped):
    async def stop(pid):
        stopped.append(pid)
        return True

    guard = mg.MemoryGuard(tmp_path / "journal.jsonl", holder=lambda: 4242,
                           stop_kernel=stop, siblings=lambda: set())
    guard._policy = PressurePolicy.for_host(64.0, 16.0)
    return guard


def _reading(guard, *, pct, kernel_mb, mono, commit_free=60.0, commit_level="ok",
             tree_cap=None):
    policy = guard._policy
    trip = mg.trip_tier()
    tier = policy.classify(pct, guard.state.tier)
    guard.state.tier = tier
    return mg.Reading(
        at="t", mono=mono, host_total_gb=64.0, host_used_pct=pct, tier=tier.name,
        policy=policy.version, trip_enter_pct=policy.enter_thresholds[trip],
        trip_exit_pct=policy.exit_thresholds[trip], commit_free_pct=commit_free,
        commit_used_gb=64.0, commit_limit_gb=160.0, commit_level=commit_level,
        kernel_pid=4242, kernel_procs=3, kernel_rss_mb=kernel_mb, kernel_private_mb=kernel_mb,
        tree_warn_mb=None if tree_cap is None else tree_cap * 0.85, tree_cap_mb=tree_cap,
    )


def test_a_breach_must_hold_for_the_dwell(tmp_path):
    guard = _guard(tmp_path, [])
    dwell = mg.dwell_s(guard._policy)
    trip_pct = guard._policy.enter_thresholds[mg.trip_tier()]
    assert guard.judge(_reading(guard, pct=trip_pct + 1, kernel_mb=20000, mono=0.0)) is None
    breach = guard.judge(_reading(guard, pct=trip_pct + 1, kernel_mb=20000, mono=dwell))
    assert breach and breach["resolves"]


def test_a_reading_that_clears_resets_the_dwell(tmp_path):
    guard = _guard(tmp_path, [])
    dwell = mg.dwell_s(guard._policy)
    trip_pct = guard._policy.enter_thresholds[mg.trip_tier()]
    guard.judge(_reading(guard, pct=trip_pct + 1, kernel_mb=20000, mono=0.0))
    guard.judge(_reading(guard, pct=50.0, kernel_mb=20000, mono=dwell / 2))
    assert guard.judge(_reading(guard, pct=trip_pct + 1, kernel_mb=20000, mono=dwell)) is None


def test_a_breach_the_kernel_is_not_causing_leaves_it_running(tmp_path):
    """Stopping a kernel holding 500 MB would not bring a full host back under
    the exit line: it costs JARVIS and saves nothing."""
    guard = _guard(tmp_path, [])
    dwell = mg.dwell_s(guard._policy)
    trip_pct = guard._policy.enter_thresholds[mg.trip_tier()]
    guard.judge(_reading(guard, pct=trip_pct + 2, kernel_mb=500, mono=0.0))
    breach = guard.judge(_reading(guard, pct=trip_pct + 2, kernel_mb=500, mono=dwell))
    assert breach and not breach["resolves"]


def test_a_kernel_over_its_own_tree_budget_is_the_cause(tmp_path):
    guard = _guard(tmp_path, [])
    dwell = mg.dwell_s(guard._policy)
    guard.judge(_reading(guard, pct=40.0, kernel_mb=9000, mono=0.0, tree_cap=8000))
    breach = guard.judge(_reading(guard, pct=40.0, kernel_mb=9000, mono=dwell, tree_cap=8000))
    assert breach and breach["resolves"] and "budget cap" in breach["reason"]


def test_host_commit_trips_on_the_gates_level(tmp_path):
    guard = _guard(tmp_path, [])
    dwell = mg.dwell_s(guard._policy)
    level = mg.commit_trip_level().value
    guard.judge(_reading(guard, pct=40.0, kernel_mb=60000, mono=0.0, commit_free=1.0,
                         commit_level=level))
    breach = guard.judge(_reading(guard, pct=40.0, kernel_mb=60000, mono=dwell,
                                  commit_free=1.0, commit_level=level))
    assert breach and "commit" in breach["reason"]


def test_tick_stops_the_kernel_once_and_journals_it(tmp_path, monkeypatch):
    stopped = []
    guard = _guard(tmp_path, stopped)
    trip_pct = guard._policy.enter_thresholds[mg.trip_tier()]
    clock = iter([0.0, mg.dwell_s(guard._policy), mg.dwell_s(guard._policy) * 2])

    def sample():
        return _reading(guard, pct=trip_pct + 3, kernel_mb=30000, mono=next(clock))

    monkeypatch.setattr(guard, "sample", sample)
    monkeypatch.setattr(mg, "top_consumers", lambda n: [{"name": "python", "pid": 4242,
                                                         "private_mb": 30000, "rss_mb": 30000}])
    asyncio.run(guard.tick())
    asyncio.run(guard.tick())
    assert stopped == [4242]
    kinds = [json.loads(line)["kind"] for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert "stopping_kernel" in kinds and "kernel_stopped" in kinds


def test_an_unattributed_breach_is_journalled_once_per_episode(tmp_path, monkeypatch):
    stopped = []
    guard = _guard(tmp_path, stopped)
    trip_pct = guard._policy.enter_thresholds[mg.trip_tier()]
    dwell = mg.dwell_s(guard._policy)
    ticks = iter(range(10))
    monkeypatch.setattr(guard, "sample", lambda: _reading(
        guard, pct=trip_pct + 3, kernel_mb=300, mono=next(ticks) * dwell))
    monkeypatch.setattr(mg, "top_consumers", lambda n: [])
    for _ in range(5):
        asyncio.run(guard.tick())
    kinds = [json.loads(line)["kind"] for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert stopped == [] and kinds.count("breach_not_kernel") == 1


def test_report_reads_the_journal_back(tmp_path, monkeypatch):
    guard = _guard(tmp_path, [])
    ticks = iter(range(10))
    monkeypatch.setattr(guard, "sample", lambda: _reading(
        guard, pct=40.0, kernel_mb=1000 + 10 * next(ticks), mono=float(next(ticks))))
    for _ in range(4):
        asyncio.run(guard.tick())
    summary = mg.report(tmp_path / "journal.jsonl")
    assert summary["kernels"]["4242"]["ticks"] == 4
    assert summary["kernels"]["4242"]["slope_mb_per_h"] > 0


def test_the_kernel_lock_holder_is_judged_by_pid_and_start_time(tmp_path):
    from backend.core.daemon_handover import kernel_holder
    from backend.core.process_identity import process_started_at

    lock = tmp_path / "kernel.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at": process_started_at(os.getpid())}))
    assert kernel_holder(lock) == os.getpid()
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at": 1.0}))  # a recycled pid
    assert kernel_holder(lock) is None
    lock.write_text("not json")
    assert kernel_holder(lock) is None


def test_stopping_a_tree_spares_the_caller_inside_it(tmp_path):
    """The guard is its kernel's child: stopping the kernel must not stop the
    guard mid-stop."""
    from backend.core import process_identity as pi

    marker = tmp_path / "survived"
    # The root is named by pid, not os.getppid(): on Windows a venv's
    # python.exe is a launcher, so the caller's parent is a launcher that
    # sits between it and the root -- exactly the guard's position.
    code = (
        "import os, sys, time;"
        "sys.path.insert(0, os.getcwd());"
        "from backend.core import process_identity as pi;"
        "pi.kill_guard_enabled = lambda: False;"
        "pi.kill_process_tree(int(sys.argv[1]), timeout=5);"
        f"open(r'{marker}', 'w').write('ok')"
    )
    parent = subprocess.Popen([sys.executable, "-c",
                               "import os, subprocess, sys, time;"
                               f"subprocess.Popen([sys.executable, '-c', {code!r}, str(os.getpid())]);"
                               "time.sleep(60)"],
                              cwd=os.getcwd())
    try:
        parent.wait(timeout=30)
        deadline = time.time() + 30
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.1)
        assert marker.exists(), "the caller was killed with the tree it stopped"
    finally:
        if parent.poll() is None:
            parent.kill()


@pytest.mark.skipif(sys.platform != "win32", reason="the native read exists on the Windows host")
def test_host_commit_is_read_natively_on_windows():
    from backend.core.ouroboros.governance import host_commit_probe as hp

    assert hp.available()
    sample = hp.read_host_commit()
    assert sample is not None and 0 < sample.free_kb <= sample.limit_kb
    assert 0 < sample.phys_free_kb <= sample.phys_total_kb
