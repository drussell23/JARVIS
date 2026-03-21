"""Tests for DeadEndClassifier, SentinelOutcome, and ExplorationSentinel."""
import asyncio
import os
import tempfile

import pytest

from backend.core.topology.sentinel import (
    DeadEndClass,
    DeadEndClassifier,
    ExplorationSentinel,
    SentinelOutcome,
)
from backend.core.topology.curiosity_engine import CuriosityTarget
from backend.core.topology.topology_map import CapabilityNode
from backend.core.topology.hardware_env import ComputeTier, HardwareEnvironmentState


def _make_hardware():
    return HardwareEnvironmentState(
        os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
        ram_available_mb=8192, compute_tier=ComputeTier.LOCAL_CPU, gpu=None,
        hostname="test", python_version="3.11.0",
        max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
    )


def _make_target():
    node = CapabilityNode(name="test_cap", domain="test_domain", repo_owner="jarvis")
    return CuriosityTarget(
        capability=node, ucb_score=1.5, entropy_score=0.8,
        feasibility_score=1.0, rationale="test rationale",
    )


class TestDeadEndClass:
    def test_enum_values(self):
        assert DeadEndClass.PAYWALL == "paywall"
        assert DeadEndClass.DEPRECATED_API == "deprecated_api"
        assert DeadEndClass.TIMEOUT == "timeout"
        assert DeadEndClass.INFINITE_LOOP == "infinite_loop"
        assert DeadEndClass.RESOURCE_EXHAUSTION == "resource_exhaust"
        assert DeadEndClass.SANDBOX_VIOLATION == "sandbox_violation"
        assert DeadEndClass.CLEAN_SUCCESS == "clean_success"


class TestSentinelOutcome:
    def test_frozen(self):
        outcome = SentinelOutcome(
            dead_end_class=DeadEndClass.CLEAN_SUCCESS,
            capability_name="test", elapsed_seconds=10.0,
            partial_findings="found stuff", unwind_actions_taken=["none"],
        )
        with pytest.raises(AttributeError):
            outcome.dead_end_class = DeadEndClass.TIMEOUT


class TestDeadEndClassifier:
    def test_classify_402_as_paywall(self):
        assert DeadEndClassifier.classify_http_error(402) == DeadEndClass.PAYWALL

    def test_classify_403_as_paywall(self):
        assert DeadEndClassifier.classify_http_error(403) == DeadEndClass.PAYWALL

    def test_classify_410_as_deprecated(self):
        assert DeadEndClassifier.classify_http_error(410) == DeadEndClass.DEPRECATED_API

    def test_classify_200_returns_none(self):
        assert DeadEndClassifier.classify_http_error(200) is None

    def test_classify_500_returns_none(self):
        assert DeadEndClassifier.classify_http_error(500) is None

    def test_classify_memory_error(self):
        assert DeadEndClassifier.classify_exception(MemoryError()) == DeadEndClass.RESOURCE_EXHAUSTION

    def test_classify_timeout_error(self):
        assert DeadEndClassifier.classify_exception(TimeoutError()) == DeadEndClass.TIMEOUT

    def test_classify_cancelled_error(self):
        assert DeadEndClassifier.classify_exception(asyncio.CancelledError()) == DeadEndClass.TIMEOUT

    def test_classify_permission_error(self):
        assert DeadEndClassifier.classify_exception(PermissionError()) == DeadEndClass.SANDBOX_VIOLATION

    def test_classify_unknown_defaults_to_timeout(self):
        assert DeadEndClassifier.classify_exception(ValueError("something")) == DeadEndClass.TIMEOUT


class TestExplorationSentinel:
    @pytest.mark.asyncio
    async def test_context_manager_lifecycle(self):
        target = _make_target()
        hw = _make_hardware()
        async with ExplorationSentinel(target, hw, max_runtime_seconds=5.0) as sentinel:
            assert sentinel._governor is not None

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout_outcome(self):
        target = _make_target()
        hw = _make_hardware()
        sentinel = ExplorationSentinel(target, hw, max_runtime_seconds=0.1)
        async def hang():
            await asyncio.sleep(100)
            return ""
        sentinel._explore = hang
        async with sentinel:
            outcome = await sentinel.run()
        assert outcome.dead_end_class == DeadEndClass.TIMEOUT
        assert outcome.capability_name == "test_cap"

    @pytest.mark.asyncio
    async def test_success_returns_clean_success(self):
        target = _make_target()
        hw = _make_hardware()
        sentinel = ExplorationSentinel(target, hw, max_runtime_seconds=5.0)
        async def succeed():
            return "found integration docs"
        sentinel._explore = succeed
        async with sentinel:
            outcome = await sentinel.run()
        assert outcome.dead_end_class == DeadEndClass.CLEAN_SUCCESS
        assert outcome.partial_findings == "found integration docs"

    @pytest.mark.asyncio
    async def test_exception_returns_classified_outcome(self):
        target = _make_target()
        hw = _make_hardware()
        sentinel = ExplorationSentinel(target, hw, max_runtime_seconds=5.0)
        async def explode():
            raise MemoryError("OOM")
        sentinel._explore = explode
        async with sentinel:
            outcome = await sentinel.run()
        assert outcome.dead_end_class == DeadEndClass.RESOURCE_EXHAUSTION

    @pytest.mark.asyncio
    async def test_cleanup_scratch_on_exit(self):
        target = _make_target()
        hw = _make_hardware()
        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel = ExplorationSentinel(target, hw, max_runtime_seconds=5.0)
            sentinel._scratch_path = os.path.join(tmpdir, "scratch")
            os.makedirs(sentinel._scratch_path)
            async def fail():
                raise ValueError("test failure")
            sentinel._explore = fail
            async with sentinel:
                await sentinel.run()
            assert not os.path.exists(sentinel._scratch_path)
