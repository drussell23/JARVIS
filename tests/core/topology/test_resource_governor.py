"""Tests for PIDController and ResourceGovernor."""
import asyncio
import time

import pytest

from backend.core.topology.resource_governor import PIDController, ResourceGovernor


class TestPIDController:
    def test_default_params(self):
        pid = PIDController()
        assert pid.target_cpu_fraction == 0.40
        assert pid.Kp == 0.5
        assert pid.Ki == 0.1
        assert pid.Kd == 0.05
        assert pid.min_concurrency == 1
        assert pid.max_concurrency == 8

    def test_underloaded_increases_concurrency(self):
        pid = PIDController()
        result = pid.update(0.10)
        assert result >= pid.min_concurrency
        assert result <= pid.max_concurrency
        baseline = (pid.min_concurrency + pid.max_concurrency) // 2
        assert result >= baseline

    def test_overloaded_decreases_concurrency(self):
        pid = PIDController()
        result = pid.update(0.80)
        assert result >= pid.min_concurrency
        assert result <= pid.max_concurrency
        baseline = (pid.min_concurrency + pid.max_concurrency) // 2
        assert result <= baseline

    def test_at_target_stays_near_baseline(self):
        pid = PIDController()
        result = pid.update(0.40)
        baseline = (pid.min_concurrency + pid.max_concurrency) // 2
        assert abs(result - baseline) <= 1

    def test_never_exceeds_bounds(self):
        pid = PIDController()
        for cpu in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
            result = pid.update(cpu)
            assert pid.min_concurrency <= result <= pid.max_concurrency

    def test_anti_windup_clamp(self):
        pid = PIDController()
        for _ in range(1000):
            pid.update(1.0)
        assert -10.0 <= pid._integral <= 10.0

    def test_integral_accumulates_on_sustained_error(self):
        pid = PIDController()
        pid.update(0.10)
        time.sleep(0.01)
        pid.update(0.10)
        assert pid._integral > 0

    def test_custom_params(self):
        pid = PIDController(
            target_cpu_fraction=0.60, Kp=1.0, Ki=0.2, Kd=0.1,
            min_concurrency=2, max_concurrency=16,
        )
        assert pid.target_cpu_fraction == 0.60
        assert pid.max_concurrency == 16


class TestResourceGovernor:
    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self):
        pid = PIDController()
        sem = asyncio.Semaphore(4)
        gov = ResourceGovernor(pid, sem)
        await gov.start()
        assert gov._task is not None
        assert not gov._task.done()
        await gov.stop()
        assert gov._task is None

    @pytest.mark.asyncio
    async def test_stop_idempotent(self):
        pid = PIDController()
        sem = asyncio.Semaphore(4)
        gov = ResourceGovernor(pid, sem)
        await gov.stop()
        assert gov._task is None

    @pytest.mark.asyncio
    async def test_governor_adjusts_concurrency(self):
        pid = PIDController()
        sem = asyncio.Semaphore(4)
        gov = ResourceGovernor(pid, sem, poll_interval=0.05)
        await gov.start()
        await asyncio.sleep(0.15)
        await gov.stop()
        assert pid._prev_time > 0

    @pytest.mark.asyncio
    async def test_burst_window_uses_fast_interval(self):
        """During the first BURST_WINDOW_S seconds, governor polls at 1s interval."""
        pid = PIDController()
        sem = asyncio.Semaphore(4)
        gov = ResourceGovernor(pid, sem, poll_interval=5.0)
        assert gov.BURST_INTERVAL_S == 1.0
        assert gov.BURST_WINDOW_S == 30.0
        await gov.start()
        # Governor should be using burst interval (1s) not normal (5s)
        # so within 2s we should get at least 1 PID update
        await asyncio.sleep(1.5)
        await gov.stop()
        # PID should have been called at least once during burst window
        assert pid._prev_time > 0

    @pytest.mark.asyncio
    async def test_started_at_is_set(self):
        pid = PIDController()
        sem = asyncio.Semaphore(4)
        gov = ResourceGovernor(pid, sem)
        assert gov._started_at == 0.0
        await gov.start()
        assert gov._started_at > 0.0
        await gov.stop()
