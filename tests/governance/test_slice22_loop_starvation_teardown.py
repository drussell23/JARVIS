"""Slice 22 — loop-starvation hardening + orphan-cascade teardown.

The bt-2026-07-15-154242 soak was SIGKILLed by the external watchdog for
heartbeat_stale (event-loop starvation), and the hard halt orphaned the Aegis
daemon (no lifeline armed) — one instance ran 9.6 h polling DoubleWord, part of
a 14-process graveyard.

Two fronts pinned here:
  * ORPHAN CASCADE (mandate 4): child_reaper registry + signal-safe SIGTERM→
    SIGKILL cascade, wired into the graceful shutdown, the LoopDeadman pre-exit
    (os._exit path), and an atexit fallback; the Aegis daemon arms its own
    WorkerLifeline (--parent-pid) as the pure-SIGKILL-of-parent backstop.
  * COOPERATIVE OFFLOAD (mandates 1-3): the intake cognitive-load disk I/O is
    dispatched off the loop; a cooperative yield is inserted before the heavy
    ingest phase.
"""
from __future__ import annotations

import inspect
import subprocess
import sys
import time

import pytest

from backend.core.ouroboros.governance import child_reaper as CR


@pytest.fixture(autouse=True)
def _clean():
    CR.reset_for_tests()
    yield
    CR.reset_for_tests()


# ═══════════════════════════════════════════════════════════════════════
# child_reaper — the cascade primitive
# ═══════════════════════════════════════════════════════════════════════


def _spawn_sleeper(seconds: int = 60) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"]
    )


def test_register_and_snapshot():
    p = _spawn_sleeper()
    try:
        CR.register_child(p.pid, role="unit")
        snap = dict(CR.registered_children())
        assert snap.get(p.pid) == "unit"
    finally:
        p.kill()


def test_cascade_terminates_real_child():
    p = _spawn_sleeper()
    CR.register_child(p.pid, role="aegis_daemon")
    n = CR.cascade_terminate(grace_s=0.3)
    assert n == 1
    time.sleep(0.4)
    assert p.poll() is not None, "registered child must be dead after cascade"
    assert CR.registered_children() == (), "registry cleared after cascade"


def test_cascade_hard_path_no_grace_still_kills():
    """The LoopDeadman path passes hard=True (no grace sleep) — a child that
    ignores SIGTERM must still be SIGKILLed."""
    # A child that traps SIGTERM and keeps running.
    p = subprocess.Popen([
        sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)",
    ])
    CR.register_child(p.pid, role="stubborn")
    time.sleep(0.2)  # let it install the SIGTERM trap
    CR.cascade_terminate(hard=True)
    time.sleep(0.3)
    assert p.poll() is not None, "hard cascade must SIGKILL a SIGTERM-ignorer"


def test_cascade_idempotent_and_ghost_safe():
    assert CR.cascade_terminate() == 0  # empty registry
    CR.register_child(999999, role="ghost")  # non-existent pid
    assert CR.cascade_terminate() == 0  # not alive → not signalled
    assert CR.registered_children() == ()


def test_unregister():
    CR.register_child(4242, role="x")
    CR.unregister_child(4242)
    assert CR.registered_children() == ()


def test_never_raises_on_garbage():
    for bad in (0, -1, "notapid", None):
        CR.register_child(bad, role="bad")  # type: ignore[arg-type]
    # nothing valid registered; cascade is a clean no-op
    assert CR.cascade_terminate() == 0


def test_grace_is_env_bounded(monkeypatch):
    monkeypatch.setenv("JARVIS_CHILD_REAPER_GRACE_S", "999")
    assert CR._resolve_grace_s() == 30.0  # clamped
    monkeypatch.setenv("JARVIS_CHILD_REAPER_GRACE_S", "-5")
    assert CR._resolve_grace_s() == 0.0
    monkeypatch.setenv("JARVIS_CHILD_REAPER_GRACE_S", "garbage")
    assert CR._resolve_grace_s() == CR._DEFAULT_GRACE_S


# ═══════════════════════════════════════════════════════════════════════
# Wiring pins — the cascade is on every teardown seam
# ═══════════════════════════════════════════════════════════════════════


def test_loop_deadman_cascades_before_os_exit():
    """The hard-halt seam: LoopDeadman must cascade children BEFORE os._exit(75)
    (os._exit bypasses atexit, so this is the only in-process reap on a wedge)."""
    import backend.core.ouroboros.governance.loop_deadman as LD
    src = inspect.getsource(LD)
    cascade = src.find("cascade_terminate(hard=True)")
    exit_call = src.rfind("os._exit(75)")
    assert cascade != -1, "LoopDeadman must call the hard cascade"
    assert cascade < exit_call, "cascade must precede os._exit(75)"


def test_harness_graceful_shutdown_cascades():
    import backend.core.ouroboros.battle_test.harness as H
    src = inspect.getsource(H)
    assert "cascade_terminate()" in src
    assert "shutdown_fs_process_pool" in src
    assert "arm_atexit_cascade" in src


def test_aegis_spawn_registers_and_passes_parent_pid():
    import backend.core.ouroboros.aegis.preflight as PF
    src = inspect.getsource(PF._spawn_daemon)
    assert "--parent-pid" in src, "spawner must ship the parent pid"
    assert "register_child" in src, "spawner must register the daemon for reaping"
    assert 'role="aegis_daemon"' in src


def test_aegis_daemon_arms_lifeline():
    import backend.core.ouroboros.aegis.daemon as D
    main_src = inspect.getsource(D.main)
    assert "arm_worker_lifeline" in main_src
    assert '"aegis_daemon"' in main_src
    # the --parent-pid arg exists
    args_src = inspect.getsource(D._parse_args)
    assert "--parent-pid" in args_src


# ═══════════════════════════════════════════════════════════════════════
# Cooperative offload — mandates 1-3
# ═══════════════════════════════════════════════════════════════════════


def test_ingest_offloads_cognitive_load_and_yields():
    import backend.core.ouroboros.governance.intake.unified_intake_router as R
    src = inspect.getsource(R.UnifiedIntakeRouter._ingest_impl)
    # cognitive-load disk I/O dispatched off the loop, fail-soft to inline.
    assert "_offload_cl(evaluate_cognitive_load" in src
    assert "_is_offload_error_cl" in src
    # a cooperative yield precedes the heavy WAL+offload phase.
    assert "cooperative_yield" in src


def test_offload_and_yield_primitives_exist():
    """DRY pin — Slice 22 composes the existing Slice-12S/12U substrate, not a
    new one."""
    from backend.core.ouroboros.governance.cooperative_fs_io import (
        is_offload_error,
        offload,
    )
    from backend.core.ouroboros.governance.event_loop_governance import (
        cooperative_yield,
    )
    assert callable(offload) and callable(is_offload_error)
    assert callable(cooperative_yield)


def test_worker_lifeline_arm_accepts_role_and_parent():
    """The aegis daemon reuses the EXISTING lifeline arming API (no new
    orphan-detection mechanism)."""
    from backend.core.ouroboros.governance.worker_lifeline import (
        arm_worker_lifeline,
    )
    sig = inspect.signature(arm_worker_lifeline)
    assert "expected_parent_pid" in sig.parameters
