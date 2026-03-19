# tests/unit/core/test_vm_lifecycle_manager.py
"""Tests for VMLifecycleManager — v298.0 (T1–T20)."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# --- helpers -------------------------------------------------------------------

def make_test_config(tmp_path: Path, **overrides) -> "VMLifecycleConfig":  # noqa: F821
    from backend.core.vm_lifecycle_manager import VMLifecycleConfig
    defaults = dict(
        inactivity_threshold_s=0.3,
        idle_grace_s=0.1,
        warming_await_timeout_s=2.0,
        max_uptime_s=None,
        quiet_hours=None,
        quiet_hours_threshold_factor=0.25,
        drain_hard_cap_s=600.0,
        warm_max_strikes=3,
        lease_dir=tmp_path,
        strict_drain=True,
    )
    defaults.update(overrides)
    return VMLifecycleConfig(**defaults)


def make_mock_controller(start_returns=(True, None, None)):
    from backend.core.vm_lifecycle_manager import VMController
    ctrl = MagicMock()
    ctrl.start_vm = AsyncMock(return_value=start_returns)
    ctrl.stop_vm = AsyncMock()
    ctrl.get_vm_host_port = MagicMock(return_value=("127.0.0.1", 8000))
    ctrl.notify_vm_unreachable = MagicMock()
    return ctrl


class _RecordingSink:
    def __init__(self):
        self.events: List = []
    async def emit(self, event) -> None:
        self.events.append(event)


# --- T16: LifecycleLease stale PID overwrite -----------------------------------

def test_lifecycle_lease_stale_pid_overwrite(tmp_path):
    """T16 — stale PID in lease file → overwrite succeeds."""
    from backend.core.vm_lifecycle_manager import LifecycleLease
    lease = LifecycleLease(lease_dir=tmp_path)
    # Write a lease with a dead PID (999999999 is extremely unlikely to exist)
    import json
    lease_file = tmp_path / "vm_lifecycle.lease"
    lease_file.write_text(json.dumps({"pid": 999999999, "session_id": "dead", "acquired_at": 0.0}))
    session_id = lease.acquire()
    assert session_id != "dead"
    assert len(session_id) > 8
    lease.release()


# --- T17: LifecycleLease live PID → DualAuthorityError -------------------------

def test_lifecycle_lease_live_pid_dual_authority(tmp_path):
    """T17 — live PID in lease file → DualAuthorityError raised."""
    import json
    from backend.core.vm_lifecycle_manager import LifecycleLease, DualAuthorityError
    lease_file = tmp_path / "vm_lifecycle.lease"
    # Write our own PID as an "incumbent" (simulate another process)
    other_pid = os.getpid()  # same PID means same process, treated as live
    # Use a different test approach: mock os.kill to simulate a live process
    lease_file.write_text(json.dumps({
        "pid": other_pid,
        "session_id": "incumbent_session",
        "acquired_at": time.time(),
    }))
    lease = LifecycleLease(lease_dir=tmp_path)
    with patch("os.getpid", return_value=other_pid + 1):  # different PID
        with pytest.raises(DualAuthorityError) as exc_info:
            lease.acquire()
    assert exc_info.value.incumbent_session_id == "incumbent_session"


# --- T18: Unregistered caller raises UnregisteredActivitySourceError -----------

@pytest.mark.asyncio
async def test_unregistered_caller_raises(tmp_path):
    """T18 — unknown caller_id with strict_drain=True → raises."""
    from backend.core.vm_lifecycle_manager import (
        VMLifecycleManager, UnregisteredActivitySourceError, VMFsmState,
    )
    config = make_test_config(tmp_path, strict_drain=True)
    ctrl = make_mock_controller()
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    # Warm to READY so record_activity_from doesn't hit COLD guard
    await mgr.ensure_warmed("test")
    assert mgr.state == VMFsmState.READY
    with pytest.raises(UnregisteredActivitySourceError):
        mgr.record_activity_from("totally.unknown.caller")
    await mgr.stop()


# --- T1: COLD → WARMING → READY -----------------------------------------------

@pytest.mark.asyncio
async def test_ensure_warmed_cold_to_ready(tmp_path):
    """T1 — ensure_warmed() drives COLD→WARMING→READY via single entrypoint."""
    from backend.core.vm_lifecycle_manager import VMLifecycleManager, VMFsmState
    config = make_test_config(tmp_path)
    ctrl = make_mock_controller(start_returns=(True, None, None))
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    result = await mgr.ensure_warmed("test_boot")
    assert result is True
    assert mgr.state == VMFsmState.READY
    assert ctrl.start_vm.call_count == 1
    await mgr.stop()


# --- T2: Concurrent ensure_warmed collapses to one start ----------------------

@pytest.mark.asyncio
async def test_concurrent_ensure_warmed_collapses(tmp_path):
    """T2 — two concurrent ensure_warmed() calls → exactly one VM start."""
    from backend.core.vm_lifecycle_manager import VMLifecycleManager, VMFsmState
    config = make_test_config(tmp_path)

    start_count = 0

    async def _slow_start_vm():
        nonlocal start_count
        start_count += 1
        await asyncio.sleep(0.05)
        return (True, None, None)

    ctrl = make_mock_controller()
    ctrl.start_vm = _slow_start_vm
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    r1, r2 = await asyncio.gather(
        mgr.ensure_warmed("caller_a"),
        mgr.ensure_warmed("caller_b"),
    )
    assert r1 is True
    assert r2 is True
    assert mgr.state == VMFsmState.READY
    assert start_count == 1, f"Expected exactly 1 VM start, got {start_count}"
    await mgr.stop()


# --- T15: Restart consistency — full COLD→READY→STOPPING→COLD→READY ----------

@pytest.mark.asyncio
async def test_restart_consistency(tmp_path):
    """T15 — second warm cycle after STOPPING→COLD succeeds cleanly."""
    from backend.core.vm_lifecycle_manager import VMLifecycleManager, VMFsmState
    config = make_test_config(tmp_path, inactivity_threshold_s=0.05, idle_grace_s=0.02)
    ctrl = make_mock_controller()
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()

    # First warm cycle
    await mgr.ensure_warmed("first")
    assert mgr.state == VMFsmState.READY

    # Trigger shutdown
    await mgr.request_shutdown("test_restart")
    # Allow STOPPING→COLD
    await asyncio.sleep(0.05)
    assert mgr.state == VMFsmState.COLD

    # Second warm cycle
    result = await mgr.ensure_warmed("second")
    assert result is True
    assert mgr.state == VMFsmState.READY
    await mgr.stop()


# --- T3: MEANINGFUL resets idle timer -----------------------------------------

@pytest.mark.asyncio
async def test_meaningful_activity_resets_idle_timer(tmp_path):
    """T3 — record_activity_from(MEANINGFUL) resets _last_meaningful_mono."""
    from backend.core.vm_lifecycle_manager import VMLifecycleManager, VMFsmState
    config = make_test_config(tmp_path, inactivity_threshold_s=10.0)
    ctrl = make_mock_controller()
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    await mgr.ensure_warmed("t3")
    before = mgr._last_meaningful_mono
    await asyncio.sleep(0.02)
    mgr.record_activity_from("prime_client.execute_request")
    after = mgr._last_meaningful_mono
    assert after > before, "MEANINGFUL call must advance _last_meaningful_mono"
    assert mgr.state == VMFsmState.READY
    await mgr.stop()


# --- T4: NON_MEANINGFUL does NOT reset idle timer -----------------------------

@pytest.mark.asyncio
async def test_non_meaningful_does_not_reset_idle_timer(tmp_path):
    """T4 — health probe call does NOT change _last_meaningful_mono."""
    from backend.core.vm_lifecycle_manager import VMLifecycleManager, VMFsmState
    config = make_test_config(tmp_path, inactivity_threshold_s=10.0)
    ctrl = make_mock_controller()
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    await mgr.ensure_warmed("t4")
    before = mgr._last_meaningful_mono
    await asyncio.sleep(0.01)
    mgr.record_activity_from("health_probe.probe_health")
    after = mgr._last_meaningful_mono
    assert after == before, "NON_MEANINGFUL must not change _last_meaningful_mono"
    await mgr.stop()


# --- T5: 1000 health probe calls → no idle reset ------------------------------

@pytest.mark.asyncio
async def test_health_probe_1000_calls_no_idle_reset(tmp_path):
    """T5 — 1000 probe_health calls → _last_meaningful_mono unchanged."""
    from backend.core.vm_lifecycle_manager import VMLifecycleManager
    config = make_test_config(tmp_path, inactivity_threshold_s=10.0)
    ctrl = make_mock_controller()
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    await mgr.ensure_warmed("t5")
    baseline = mgr._last_meaningful_mono
    for _ in range(1000):
        mgr.record_activity_from("health_probe.probe_health")
    assert mgr._last_meaningful_mono == baseline
    await mgr.stop()


# --- T6: Health probe in IDLE_GRACE → STOPPING proceeds -----------------------

@pytest.mark.asyncio
async def test_health_probe_does_not_block_stopping(tmp_path):
    """T6 — NON_MEANINGFUL work_slot in IDLE_GRACE → STOPPING proceeds unblocked."""
    from backend.core.vm_lifecycle_manager import (
        VMLifecycleManager, VMFsmState, ActivityClass,
    )
    config = make_test_config(tmp_path, inactivity_threshold_s=0.05, idle_grace_s=0.3)
    ctrl = make_mock_controller()
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    await mgr.ensure_warmed("t6")
    # Allow idle timer to fire → IDLE_GRACE
    await asyncio.sleep(0.12)
    assert mgr.state == VMFsmState.IDLE_GRACE
    # Start a NON_MEANINGFUL slot (health probe)
    entered = False
    exited = False
    async def _probe():
        nonlocal entered, exited
        async with mgr.work_slot(ActivityClass.NON_MEANINGFUL, description="health_probe.probe_health"):
            entered = True
            await asyncio.sleep(0.5)  # holds slot for 500ms — much longer than grace
            exited = True
    probe_task = asyncio.create_task(_probe())
    await asyncio.sleep(0.01)
    assert entered is True
    # NON_MEANINGFUL slot must not block STOPPING — drain check ignores it
    # Manually call grace_and_drain_complete path
    assert mgr._drain_clear() is True  # _meaningful_count == 0 despite probe running
    probe_task.cancel()
    try:
        await probe_task
    except asyncio.CancelledError:
        pass
    await mgr.stop()


# --- T7: MEANINGFUL slot blocks STOPPING --------------------------------------

@pytest.mark.asyncio
async def test_meaningful_drain_blocks_stopping(tmp_path):
    """T7 — MEANINGFUL work_slot held → drain not clear → STOPPING waits."""
    from backend.core.vm_lifecycle_manager import (
        VMLifecycleManager, VMFsmState, ActivityClass,
    )
    config = make_test_config(tmp_path, inactivity_threshold_s=0.05, idle_grace_s=0.05)
    ctrl = make_mock_controller()
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    await mgr.ensure_warmed("t7")

    slot_released = asyncio.Event()
    slot_entered = asyncio.Event()

    async def _hold_slot():
        async with mgr.work_slot(ActivityClass.MEANINGFUL, description="prime_client.execute_request"):
            slot_entered.set()
            await slot_released.wait()

    task = asyncio.create_task(_hold_slot())
    await slot_entered.wait()
    assert not mgr._drain_clear(), "MEANINGFUL slot in flight → drain not clear"
    slot_released.set()
    await task
    assert mgr._drain_clear()
    await mgr.stop()


# --- T8: drain_clear_event release triggers STOPPING -------------------------

@pytest.mark.asyncio
async def test_drain_event_driven_releases_stopping(tmp_path):
    """T8 — releasing MEANINGFUL slot sets _drain_clear_event → STOPPING fires."""
    from backend.core.vm_lifecycle_manager import (
        VMLifecycleManager, VMFsmState, ActivityClass,
    )
    config = make_test_config(tmp_path, inactivity_threshold_s=0.05, idle_grace_s=0.05)
    ctrl = make_mock_controller()
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    await mgr.ensure_warmed("t8")

    slot_released = asyncio.Event()

    async def _hold():
        async with mgr.work_slot(ActivityClass.MEANINGFUL, description="prime_client.execute_request"):
            await slot_released.wait()

    task = asyncio.create_task(_hold())
    await asyncio.sleep(0.12)  # allow idle timer to fire
    assert mgr.state in (VMFsmState.IDLE_GRACE, VMFsmState.IN_USE)
    slot_released.set()
    await task
    # After slot released, give time for grace → drain → STOPPING → COLD
    await asyncio.sleep(0.3)
    assert mgr.state == VMFsmState.COLD
    await mgr.stop()


# --- T9: IDLE_GRACE + new work_slot(MEANINGFUL) → IN_USE, grace cancelled ----

@pytest.mark.asyncio
async def test_idle_grace_cancelled_by_new_work(tmp_path):
    """T9 — MEANINGFUL work_slot during IDLE_GRACE → IN_USE + grace cancelled."""
    from backend.core.vm_lifecycle_manager import (
        VMLifecycleManager, VMFsmState, ActivityClass,
    )
    config = make_test_config(tmp_path, inactivity_threshold_s=0.05, idle_grace_s=2.0)
    ctrl = make_mock_controller()
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    await mgr.ensure_warmed("t9")
    # Let idle timer fire
    await asyncio.sleep(0.12)
    assert mgr.state == VMFsmState.IDLE_GRACE
    async with mgr.work_slot(ActivityClass.MEANINGFUL, description="prime_client.execute_request"):
        assert mgr.state == VMFsmState.IN_USE
        assert mgr._grace_period_task is None or mgr._grace_period_task.done() or mgr._grace_period_task.cancelled()
    await mgr.stop()


# --- T10: work_slot WARMING bounded-await success -----------------------------

@pytest.mark.asyncio
async def test_work_slot_warming_bounded_await_success(tmp_path):
    """T10 — work_slot called during WARMING → bounded-await → READY → proceeds."""
    from backend.core.vm_lifecycle_manager import (
        VMLifecycleManager, VMFsmState, ActivityClass,
    )
    config = make_test_config(tmp_path, warming_await_timeout_s=2.0)

    async def _slow_start():
        await asyncio.sleep(0.1)
        return (True, None, None)

    ctrl = make_mock_controller()
    ctrl.start_vm = _slow_start
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()

    warm_task = asyncio.create_task(mgr.ensure_warmed("t10"))
    await asyncio.sleep(0.01)  # ensure we're in WARMING
    assert mgr.state == VMFsmState.WARMING

    # work_slot should bounded-await and succeed once READY
    async with mgr.work_slot(ActivityClass.MEANINGFUL, description="prime_client.execute_request"):
        assert mgr.state == VMFsmState.IN_USE

    await warm_task
    await mgr.stop()


# --- T11: work_slot WARMING timeout → VMNotReadyError with recovery -----------

@pytest.mark.asyncio
async def test_work_slot_warming_timeout_taxonomy_recovery(tmp_path):
    """T11 — warming_await_timeout elapses → VMNotReadyError.recovery from _RECOVERY_MATRIX."""
    from backend.core.vm_lifecycle_manager import (
        VMLifecycleManager, VMFsmState, ActivityClass, VMNotReadyError,
    )
    config = make_test_config(tmp_path, warming_await_timeout_s=0.05)

    async def _very_slow_start():
        await asyncio.sleep(5.0)
        return (True, None, None)

    ctrl = make_mock_controller()
    ctrl.start_vm = _very_slow_start
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    asyncio.create_task(mgr.ensure_warmed("t11"))
    await asyncio.sleep(0.01)
    assert mgr.state == VMFsmState.WARMING

    with pytest.raises(VMNotReadyError) as exc_info:
        async with mgr.work_slot(ActivityClass.MEANINGFUL, description="prime_client.execute_request"):
            pass
    assert exc_info.value.recovery is not None, "VMNotReadyError must carry a recovery strategy"
    await mgr.stop()


# --- T12: work_slot COLD + prior failure → VMNotReadyError with recovery ------

@pytest.mark.asyncio
async def test_work_slot_cold_taxonomy_recovery(tmp_path):
    """T12 — COLD state after prior failure → VMNotReadyError.recovery from matrix."""
    from backend.core.vm_lifecycle_manager import (
        VMLifecycleManager, VMFsmState, ActivityClass, VMNotReadyError,
    )
    from backend.core.gcp_readiness_lease import HandshakeStep, ReadinessFailureClass
    config = make_test_config(tmp_path)

    ctrl = make_mock_controller(start_returns=(False, HandshakeStep.HEALTH, ReadinessFailureClass.TRANSIENT_INFRA))
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    # Trigger a failure to populate _last_warming_failure
    await mgr.ensure_warmed("t12_fail")
    assert mgr.state == VMFsmState.COLD
    assert mgr._last_warming_failure is not None

    with pytest.raises(VMNotReadyError) as exc_info:
        async with mgr.work_slot(ActivityClass.MEANINGFUL, description="prime_client.execute_request"):
            pass
    assert exc_info.value.recovery is not None
    await mgr.stop()
