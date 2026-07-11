"""Regression spine — Worker Lifeline (2026-07-11 OOM RCA).

The class under test: spawn workers (Oracle IPC, FS process pool)
outliving a non-gracefully-dead parent forever (caught live: 33.9 GB /
29 h orphan) or ratcheting footprint unboundedly while rss-scoped
monitors read them as healthy. The lifeline is the in-worker kernel-
truth sentinel that self-terminates on either condition.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from backend.core.ouroboros.governance import worker_lifeline as wl


# ---------------------------------------------------------------------------
# Pure decision core
# ---------------------------------------------------------------------------


def test_decision_healthy() -> None:
    assert wl._tick_decision(100, 100, 512.0, 4096.0) is None


def test_decision_orphaned_on_ppid_drift() -> None:
    verdict = wl._tick_decision(100, 1, 512.0, 4096.0)
    assert verdict is not None
    code, reason = verdict
    assert code == wl.EXIT_CODE_ORPHANED
    assert "orphaned" in reason


def test_decision_orphan_outranks_footprint() -> None:
    # Reparented AND over budget → orphan verdict (root cause first).
    code, _ = wl._tick_decision(100, 1, 99999.0, 10.0)
    assert code == wl.EXIT_CODE_ORPHANED


def test_decision_footprint_cap() -> None:
    verdict = wl._tick_decision(100, 100, 5000.0, 4096.0)
    assert verdict is not None
    code, reason = verdict
    assert code == wl.EXIT_CODE_FOOTPRINT_CAP
    assert "footprint_cap" in reason


def test_decision_cap_disarmed_when_unresolvable() -> None:
    # cap None (no override + RAM unknowable) → footprint never fires.
    assert wl._tick_decision(100, 100, 99999.0, None) is None
    # probe failure (footprint None) → no false fire either.
    assert wl._tick_decision(100, 100, None, 1024.0) is None


# ---------------------------------------------------------------------------
# Budget resolution — adaptive, env-driven
# ---------------------------------------------------------------------------


def test_cap_absolute_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_WORKER_FOOTPRINT_CAP_MB", "2048")
    monkeypatch.setenv("JARVIS_WORKER_FOOTPRINT_CAP_FRACTION", "0.5")
    assert wl.resolve_footprint_cap_mb() == 2048.0


def test_cap_fraction_of_total_ram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_WORKER_FOOTPRINT_CAP_MB", raising=False)
    monkeypatch.delenv("JARVIS_WORKER_FOOTPRINT_CAP_FRACTION", raising=False)
    psutil = pytest.importorskip("psutil")
    total_mb = psutil.virtual_memory().total / (1024.0 * 1024.0)
    cap = wl.resolve_footprint_cap_mb()
    assert cap is not None
    assert abs(cap - total_mb * 0.25) < 1.0


def test_cap_fraction_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_WORKER_FOOTPRINT_CAP_MB", raising=False)
    monkeypatch.setenv("JARVIS_WORKER_FOOTPRINT_CAP_FRACTION", "5.0")
    psutil = pytest.importorskip("psutil")
    total_mb = psutil.virtual_memory().total / (1024.0 * 1024.0)
    cap = wl.resolve_footprint_cap_mb()
    assert cap is not None
    assert cap <= total_mb * 0.90 + 1.0


# ---------------------------------------------------------------------------
# Arm surface
# ---------------------------------------------------------------------------


def _reset_armed_state() -> None:
    thread = wl._ARMED_THREAD
    if thread is not None:
        stop = getattr(thread, "_lifeline_stop", None)
        if stop is not None:
            stop.set()
        thread.join(timeout=2.0)
    wl._ARMED_THREAD = None


def test_arm_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_WORKER_LIFELINE_ENABLED", "false")
    assert wl.arm_worker_lifeline("test") is None


def test_arm_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_WORKER_LIFELINE_ENABLED", raising=False)
    try:
        t1 = wl.arm_worker_lifeline("test", exit_fn=lambda code: None)
        t2 = wl.arm_worker_lifeline("test", exit_fn=lambda code: None)
        assert t1 is not None and t1 is t2
        assert t1.daemon is True
    finally:
        _reset_armed_state()


def test_pool_initializer_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("arm exploded")

    monkeypatch.setattr(wl, "arm_worker_lifeline", _boom)
    wl.pool_worker_initializer("fs_process_pool")  # must not raise


def test_loop_fires_injected_exit_on_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_WORKER_LIFELINE_ENABLED", raising=False)
    fired: list = []
    # Simulate reparenting: armed ppid recorded from os.getppid(); make
    # the loop observe a different value on its first tick.
    monkeypatch.setattr(wl.os, "getppid", lambda: 424242)
    try:
        thread = wl.arm_worker_lifeline(
            "test", exit_fn=fired.append, interval_s=0.05,
        )
        assert thread is not None
        deadline = time.monotonic() + 5.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.02)
        # armed_ppid == 424242 too (same fake) — so force drift instead:
        # not fired is the healthy outcome here; now flip the fake.
        if not fired:
            monkeypatch.setattr(wl.os, "getppid", lambda: 1)
            deadline = time.monotonic() + 5.0
            while not fired and time.monotonic() < deadline:
                time.sleep(0.02)
        assert fired == [wl.EXIT_CODE_ORPHANED]
    finally:
        _reset_armed_state()


# ---------------------------------------------------------------------------
# Live integration — the 29h-orphan class must die in seconds
# ---------------------------------------------------------------------------


_ORPHAN_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, {repo!r})
    import multiprocessing as mp

    def worker_main(pid_file, parent_pid):
        from backend.core.ouroboros.governance.worker_lifeline import (
            arm_worker_lifeline,
        )
        os.environ.pop("JARVIS_WORKER_LIFELINE_ENABLED", None)
        os.environ["JARVIS_WORKER_LIFELINE_INTERVAL_S"] = "1"
        # Real wiring: the parent shipped its own pid at spawn time, so
        # the baseline is correct even if we boot after the parent died.
        arm_worker_lifeline("orphan-itest", expected_parent_pid=parent_pid)
        with open(pid_file, "w") as fh:
            fh.write(str(os.getpid()))
        time.sleep(120)  # busy parent-blind worker; lifeline must kill us

    if __name__ == "__main__":
        ctx = mp.get_context("spawn")
        proc = ctx.Process(
            target=worker_main,
            args=(sys.argv[1], os.getpid()),
            daemon=False,
        )
        proc.start()
        os._exit(0)  # parent dies NON-gracefully: no daemon cleanup runs
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="posix ppid semantics")
def test_orphaned_spawn_worker_self_terminates(tmp_path) -> None:
    repo = os.getcwd()
    script = tmp_path / "orphan_repro.py"
    pid_file = tmp_path / "worker.pid"
    script.write_text(_ORPHAN_SCRIPT.format(repo=repo), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(script), str(pid_file)],
        capture_output=True, text=True, timeout=60,
    )
    deadline = time.monotonic() + 30.0
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert pid_file.exists(), (
        f"orphan worker never wrote its pid file: {out.stderr[-500:]}"
    )
    # The worker may still be mid-write; poll until the content parses.
    worker_pid = 0
    while time.monotonic() < deadline:
        raw = pid_file.read_text().strip()
        if raw:
            worker_pid = int(raw)
            break
        time.sleep(0.05)
    assert worker_pid > 0, "pid file stayed empty"

    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    deadline = time.monotonic() + 20.0
    while _alive(worker_pid) and time.monotonic() < deadline:
        time.sleep(0.25)
    still_alive = _alive(worker_pid)
    if still_alive:
        os.kill(worker_pid, 15)  # never leak the repro worker
    assert not still_alive, (
        f"orphaned worker {worker_pid} survived parent death — the "
        "29h-orphan class is NOT closed"
    )
