"""tests/unit/core/test_nuance_fixes.py — Tests for Nuances 1–12 fix modules."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Nuance 1: cancellation_shield
# ---------------------------------------------------------------------------

from backend.core.cancellation_shield import (
    CancellationShieldError,
    check_not_cancelled,
    shield_cancellation,
)


class TestCancellationShield:
    @pytest.mark.asyncio
    async def test_successful_coro_passes_through(self):
        result = await shield_cancellation(_coro_ok(42), "svc")
        assert result == 42

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        async def _raises():
            raise asyncio.CancelledError("direct cancel")

        with pytest.raises(asyncio.CancelledError):
            await shield_cancellation(_raises(), "svc")

    @pytest.mark.asyncio
    async def test_regular_exception_propagates(self):
        async def _fails():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await shield_cancellation(_fails(), "svc")

    def test_check_not_cancelled_no_op_outside_loop(self):
        # Outside async context — should not raise
        check_not_cancelled("svc")

    @pytest.mark.asyncio
    async def test_check_not_cancelled_no_op_when_not_cancelling(self):
        # Inside async context, task not cancelled — should not raise
        check_not_cancelled("svc")

    def test_cancellation_shield_error_attributes(self):
        err = CancellationShieldError("neural_mesh")
        assert err.component == "neural_mesh"
        assert "neural_mesh" in str(err)


async def _coro_ok(value: int) -> int:
    await asyncio.sleep(0)
    return value


# ---------------------------------------------------------------------------
# Nuance 2: stale_enforcement_s in PhaseConfig
# ---------------------------------------------------------------------------

from backend.core.startup_phase_manager import PhaseConfig, PhasePolicy, StartupPhaseManager, TaskOutcome


class TestStaleEnforcement:
    @pytest.mark.asyncio
    async def test_stale_enforcement_cancels_task_without_beacon(self):
        async def _never_heartbeats():
            await asyncio.sleep(99)

        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig(
                "test",
                timeout_s=5.0,
                component_timeout_s=5.0,
                stale_enforcement_s=0.05,  # very short stale threshold
            ),
            {"slow": _never_heartbeats},
        )
        assert result.failure_count == 1
        assert result.failed[0].outcome == TaskOutcome.TIMED_OUT
        assert "stale" in (result.failed[0].error or "").lower()

    @pytest.mark.asyncio
    async def test_no_stale_enforcement_when_field_is_none(self):
        async def _fast():
            pass

        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig("test", timeout_s=5.0, stale_enforcement_s=None),
            {"fast": _fast},
        )
        assert result.success_count == 1


# ---------------------------------------------------------------------------
# Nuance 3+9: startup_memory_gate prospective pressure
# ---------------------------------------------------------------------------

from backend.core.startup_memory_gate import MemoryGate, MemoryGateRefused, MemoryPressureLevel, ComponentMemoryBudget


class TestProspectivePressure:
    def _gate(self, total_mib: float = 10240.0) -> MemoryGate:
        g = MemoryGate()
        g._total_mib = total_mib
        return g

    def test_effective_pressure_safe_no_alloc(self):
        g = self._gate()
        # 3000 MiB free / 10240 ≈ 29% → SAFE
        p = g._effective_pressure(3000.0, 0.0)
        assert p == MemoryPressureLevel.SAFE

    def test_effective_pressure_raises_to_critical(self):
        g = self._gate()
        # 3000 free, but allocating 2500 MiB leaves only 500 MiB (4.9%) → OOM_IMMINENT
        p = g._effective_pressure(3000.0, 2500.0)
        assert p in (MemoryPressureLevel.CRITICAL, MemoryPressureLevel.OOM_IMMINENT)

    def test_effective_pressure_stays_safe_with_small_alloc(self):
        g = self._gate()
        # 5000 free, alloc 64 MiB → still > 20%
        p = g._effective_pressure(5000.0, 64.0)
        assert p == MemoryPressureLevel.SAFE

    @pytest.mark.asyncio
    async def test_check_sheds_when_prospective_critical(self):
        g = self._gate()
        g.declare(ComponentMemoryBudget("model", required_mib=2500.0, optional=True))
        # 3000 free is SAFE currently but AFTER 2500 alloc → ~5% (CRITICAL/OOM)
        with patch("backend.core.startup_memory_gate._free_mib", return_value=3000.0):
            with pytest.raises(MemoryGateRefused):
                await g.check("model")


# ---------------------------------------------------------------------------
# Nuance 4: shutdown_event
# ---------------------------------------------------------------------------

from backend.core.shutdown_event import ShutdownEvent, get_shutdown_event


class TestShutdownEvent:
    def test_not_set_initially(self):
        ev = ShutdownEvent()
        assert not ev.is_set()

    def test_set_and_is_set(self):
        ev = ShutdownEvent()
        ev.set()
        assert ev.is_set()

    def test_clear_resets(self):
        ev = ShutdownEvent()
        ev.set()
        ev.clear()
        assert not ev.is_set()

    def test_wait_sync_returns_true_when_set(self):
        ev = ShutdownEvent()
        ev.set()
        assert ev.wait_sync(timeout_s=0.1) is True

    def test_wait_sync_returns_false_on_timeout(self):
        ev = ShutdownEvent()
        assert ev.wait_sync(timeout_s=0.01) is False

    @pytest.mark.asyncio
    async def test_async_wait_returns_when_set(self):
        ev = ShutdownEvent()

        async def _setter():
            await asyncio.sleep(0.05)
            ev.set()

        asyncio.ensure_future(_setter())
        await asyncio.wait_for(ev.wait(poll_interval_s=0.01), timeout=1.0)
        assert ev.is_set()

    def test_set_callable_from_non_async_context(self):
        # Simulate signal handler calling set() before event loop is running
        ev = ShutdownEvent()
        ev.set()  # must not raise
        assert ev.is_set()

    def test_singleton_is_reused(self):
        a = get_shutdown_event()
        b = get_shutdown_event()
        assert a is b


# ---------------------------------------------------------------------------
# Nuance 5+11: dms_escalation_ledger
# ---------------------------------------------------------------------------

from backend.core.dms_escalation_ledger import DmsEscalationLedger, get_dms_escalation_ledger


class TestDmsEscalationLedger:
    def _ledger(self, tmp_path: Path, interval_s: float = 0.1) -> DmsEscalationLedger:
        return DmsEscalationLedger(
            ledger_path=tmp_path / "ledger.json",
            escalation_interval_s=interval_s,
        )

    def test_should_escalate_false_after_recent_heartbeat(self, tmp_path):
        led = self._ledger(tmp_path, interval_s=60.0)
        led.reset_escalation_timer("svc")
        # Just reset — should NOT escalate
        assert led.should_escalate("svc") is False

    def test_should_escalate_true_after_interval_no_heartbeat(self, tmp_path):
        led = self._ledger(tmp_path, interval_s=0.05)
        led._get_or_create("svc")
        # Force last_heartbeat into the past
        led._records["svc"].last_heartbeat_mono -= 1.0
        led._records["svc"].last_escalation_mono -= 1.0
        time.sleep(0.06)
        assert led.should_escalate("svc") is True

    def test_record_restart_increments_count(self, tmp_path):
        led = self._ledger(tmp_path)
        count = led.record_restart("svc")
        assert count == 1
        count = led.record_restart("svc")
        assert count == 2

    def test_restart_count_persists_across_instances(self, tmp_path):
        path = tmp_path / "ledger.json"
        led1 = DmsEscalationLedger(ledger_path=path)
        led1.record_restart("svc")
        led1.record_restart("svc")
        led2 = DmsEscalationLedger(ledger_path=path)
        assert led2.get_restart_count("svc") == 2

    def test_get_restart_count_zero_for_new(self, tmp_path):
        led = self._ledger(tmp_path)
        assert led.get_restart_count("new_svc") == 0

    def test_reset_component_clears_timer(self, tmp_path):
        led = self._ledger(tmp_path, interval_s=0.05)
        led._records.setdefault("svc", led._get_or_create("svc"))
        led._records["svc"].last_heartbeat_mono -= 1.0
        led._records["svc"].last_escalation_mono -= 1.0
        led.reset_component("svc")
        # After reset, timer is fresh — should not escalate
        assert led.should_escalate("svc") is False

    def test_flush_creates_file(self, tmp_path):
        led = self._ledger(tmp_path)
        led.record_restart("svc")
        assert (tmp_path / "ledger.json").exists()

    def test_singleton_reused(self):
        a = get_dms_escalation_ledger()
        b = get_dms_escalation_ledger()
        assert a is b


# ---------------------------------------------------------------------------
# Nuance 6: io_phase on StartupConcurrencyBudget
# ---------------------------------------------------------------------------

from backend.core.startup_concurrency_budget import HeavyTaskCategory, StartupConcurrencyBudget


class TestIoPhase:
    @pytest.mark.asyncio
    async def test_io_phase_releases_and_reacquires_slot(self):
        budget = StartupConcurrencyBudget(max_concurrent=1)
        async with budget.acquire(HeavyTaskCategory.GCP_PROVISION, "vm") as slot:
            # Before io_phase: 1 active slot, semaphore locked (count=0)
            assert budget.active_count == 1
            async with budget.io_phase(slot):
                # During io_phase: semaphore released — another acquire could proceed
                pass
            # After io_phase: semaphore reacquired, still 1 active slot
            assert budget.active_count == 1
        assert budget.active_count == 0

    @pytest.mark.asyncio
    async def test_io_phase_allows_other_acquires_during_io(self):
        budget = StartupConcurrencyBudget(max_concurrent=1)
        second_acquired = asyncio.Event()

        async with budget.acquire(HeavyTaskCategory.GCP_PROVISION, "vm1") as slot:
            async with budget.io_phase(slot):
                # Semaphore is released — vm2 can now acquire
                async with budget.acquire(HeavyTaskCategory.GCP_PROVISION, "vm2"):
                    second_acquired.set()
        assert second_acquired.is_set()

    @pytest.mark.asyncio
    async def test_io_phase_invalid_slot_raises(self):
        budget = StartupConcurrencyBudget(max_concurrent=1)
        async with budget.acquire(HeavyTaskCategory.GCP_PROVISION, "vm") as slot:
            pass  # slot no longer active after context exit
        with pytest.raises(RuntimeError, match="not currently active"):
            async with budget.io_phase(slot):
                pass


# ---------------------------------------------------------------------------
# Nuance 7: blocking_executor
# ---------------------------------------------------------------------------

from backend.core.blocking_executor import blocking_init, run_blocking, get_blocking_executor


class TestBlockingExecutor:
    @pytest.mark.asyncio
    async def test_run_blocking_executes_sync_fn(self):
        def _sync():
            return 42

        result = await run_blocking(_sync)
        assert result == 42

    @pytest.mark.asyncio
    async def test_run_blocking_passes_args(self):
        def _add(a, b):
            return a + b

        result = await run_blocking(_add, 3, 4)
        assert result == 7

    @pytest.mark.asyncio
    async def test_blocking_init_decorator(self):
        @blocking_init
        def _compute(x: int) -> int:
            return x * 2

        result = await _compute(21)
        assert result == 42

    @pytest.mark.asyncio
    async def test_blocking_init_preserves_name(self):
        @blocking_init
        def my_func():
            pass

        assert my_func.__name__ == "my_func"

    def test_get_blocking_executor_is_reused(self):
        e1 = get_blocking_executor()
        e2 = get_blocking_executor()
        assert e1 is e2

    @pytest.mark.asyncio
    async def test_run_blocking_does_not_block_loop(self):
        import threading
        thread_ids = set()

        def _blocking():
            thread_ids.add(threading.current_thread().name)
            return True

        # Also track the loop's thread
        thread_ids.add(threading.current_thread().name)
        await run_blocking(_blocking)
        # Blocking ran on a DIFFERENT thread
        assert len(thread_ids) == 2


# ---------------------------------------------------------------------------
# Nuance 8: reset() on StartupConcurrencyBudget
# ---------------------------------------------------------------------------


class TestConcurrencyBudgetReset:
    @pytest.mark.asyncio
    async def test_reset_clears_active_slots(self):
        budget = StartupConcurrencyBudget(max_concurrent=2)
        budget._ensure_primitives()
        # Manually "leak" a slot (simulating CancelledError bypass)
        from backend.core.startup_concurrency_budget import TaskSlot
        slot = TaskSlot(HeavyTaskCategory.ML_INIT, "leaked")
        budget._active.append(slot)
        budget.reset()
        assert budget.active_count == 0

    @pytest.mark.asyncio
    async def test_reset_allows_fresh_acquires(self):
        budget = StartupConcurrencyBudget(max_concurrent=1)
        budget._ensure_primitives()
        # Simulate leaked slot by exhausting the semaphore
        await budget._semaphore.acquire()  # type: ignore[union-attr]
        budget.reset()
        # After reset, a fresh acquire should succeed immediately
        async with budget.acquire(HeavyTaskCategory.ML_INIT, "clean"):
            pass  # should not hang


# ---------------------------------------------------------------------------
# Nuance 10: boot_contract_checker
# ---------------------------------------------------------------------------

from backend.core.boot_contract_checker import (
    BootContractChecker,
    BootContractResult,
    BootContractViolation,
    get_boot_contract_checker,
)
from backend.core.compatibility_matrix import CompatibilityMatrix, DEFAULT_RULES


class TestBootContractChecker:
    def _checker(self) -> BootContractChecker:
        return BootContractChecker(matrix=CompatibilityMatrix(DEFAULT_RULES))

    def test_run_all_passes_with_compatible_versions(self):
        checker = self._checker()
        result = checker.run_all({"jarvis": "2.3.0", "prime": "2.2.0"})
        assert result.passed is True
        assert isinstance(result.duration_s, float)

    def test_run_all_raises_on_incompatible_versions(self):
        checker = self._checker()
        with pytest.raises(BootContractViolation) as exc_info:
            checker.run_all({"jarvis": "2.3.0", "prime": "3.0.0"})
        assert len(exc_info.value.blocking_violations) > 0

    def test_run_all_empty_versions_passes(self):
        checker = self._checker()
        result = checker.run_all({})
        assert result.passed is True

    def test_boot_contract_result_bool(self):
        assert bool(BootContractResult(passed=True))
        assert not bool(BootContractResult(passed=False))

    def test_boot_contract_violation_message(self):
        err = BootContractViolation(["v1", "v2"], ["v2"])
        assert "v2" in str(err)
        assert err.blocking_violations == ["v2"]

    def test_singleton_reused(self):
        a = get_boot_contract_checker()
        b = get_boot_contract_checker()
        assert a is b


# ---------------------------------------------------------------------------
# Nuance 12: objc_safe_preloader
# ---------------------------------------------------------------------------

from backend.core.objc_safe_preloader import ObjcPreloadResult, is_preloaded, preload_objc_modules


class TestObjcSafePreloader:
    def test_skips_on_non_macos(self):
        with patch("backend.core.objc_safe_preloader._is_macos", return_value=False):
            result = preload_objc_modules()
        assert result.skipped is True

    def test_preload_result_all_succeeded(self):
        r = ObjcPreloadResult({"Quartz": True, "Foundation": True}, skipped=False)
        assert r.all_succeeded is True

    def test_preload_result_failed_modules(self):
        r = ObjcPreloadResult({"Quartz": True, "Foundation": False}, skipped=False)
        assert r.failed_modules == ["Foundation"]

    def test_must_be_called_from_main_thread(self):
        # We're on the main thread in tests, so this should not raise
        with patch("backend.core.objc_safe_preloader._is_macos", return_value=False):
            preload_objc_modules()  # should not raise

    def test_is_preloaded_reflects_state(self):
        # After preload on non-macOS (which is a no-op), is_preloaded() reflects state
        import backend.core.objc_safe_preloader as osp
        original = osp._PRELOADED
        try:
            osp._PRELOADED = True
            assert is_preloaded() is True
        finally:
            osp._PRELOADED = original
