"""Tests for JarvisCU orchestrator — local Computer Use with 3-layer cascade.

Tests cover:
- Full run: plan + execute all steps successfully
- Partial failure: some steps fail, result reflects partial completion
- Retry logic: failed steps are retried up to MAX_RETRIES
- Timeout enforcement
- Singleton pattern (get_instance / set_instance)
- Layer usage tracking
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Lightweight stand-in types for CUStep and StepResult
# (Task 1 & 2 may not exist yet — these mirror the expected contracts)
# ---------------------------------------------------------------------------

@dataclass
class _FakeCUStep:
    """Mirrors the CUStep dataclass from cu_task_planner."""
    step_id: str = ""
    action: str = ""
    target: str = ""
    value: str = ""
    description: str = ""


@dataclass
class _FakeStepResult:
    """Mirrors the StepResult dataclass from cu_step_executor."""
    step_id: str = ""
    success: bool = False
    layer_used: str = ""
    latency_ms: float = 0.0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_steps(n: int) -> List[_FakeCUStep]:
    """Create n fake CUStep objects."""
    return [
        _FakeCUStep(
            step_id=f"step-{i}",
            action="click",
            target=f"button-{i}",
            description=f"Click button {i}",
        )
        for i in range(n)
    ]


def _make_success_result(step_id: str, layer: str = "accessibility") -> _FakeStepResult:
    return _FakeStepResult(
        step_id=step_id,
        success=True,
        layer_used=layer,
        latency_ms=5.0,
    )


def _make_failure_result(step_id: str, error: str = "element not found") -> _FakeStepResult:
    return _FakeStepResult(
        step_id=step_id,
        success=False,
        layer_used="claude_vision",
        latency_ms=1200.0,
        error=error,
    )


def _black_frame() -> np.ndarray:
    return np.zeros((1080, 1920, 4), dtype=np.uint8)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestJarvisCURun:
    """Test JarvisCU.run() orchestration."""

    @pytest.fixture(autouse=True)
    def _patch_imports(self):
        """Patch the CUTaskPlanner, CUStepExecutor, and ShmFrameReader
        so tests never touch real screen capture or API calls."""
        with (
            patch("backend.vision.jarvis_cu.CUTaskPlanner") as mock_planner_cls,
            patch("backend.vision.jarvis_cu.CUStepExecutor") as mock_executor_cls,
            patch("backend.vision.jarvis_cu.ShmFrameReader") as mock_shm_cls,
        ):
            self.mock_planner = AsyncMock()
            self.mock_executor = AsyncMock()
            self.mock_shm = MagicMock()

            mock_planner_cls.return_value = self.mock_planner
            mock_executor_cls.return_value = self.mock_executor
            self.mock_shm.open.return_value = False  # SHM not available by default
            mock_shm_cls.return_value = self.mock_shm

            yield

    def _build_cu(self):
        """Create a fresh JarvisCU instance (bypasses singleton for testing)."""
        from backend.vision.jarvis_cu import JarvisCU
        # Reset singleton so each test gets a fresh instance
        JarvisCU._instance = None
        return JarvisCU()

    async def test_run_plans_and_executes_all_steps(self):
        """Happy path: planner returns 3 steps, all execute successfully."""
        cu = self._build_cu()
        steps = _make_steps(3)
        self.mock_planner.plan_goal.return_value = steps

        # Each step succeeds on the accessibility layer
        self.mock_executor.execute_step.side_effect = [
            _make_success_result("step-0", "accessibility"),
            _make_success_result("step-1", "doubleword"),
            _make_success_result("step-2", "claude_vision"),
        ]

        result = await cu.run("Open Safari and search for cats")

        assert result["success"] is True
        assert result["steps_completed"] == 3
        assert result["steps_total"] == 3
        assert len(result["step_results"]) == 3
        assert result["error"] is None
        assert result["elapsed_s"] >= 0
        # Layer tracking
        assert result["layers_used"]["accessibility"] == 1
        assert result["layers_used"]["doubleword"] == 1
        assert result["layers_used"]["claude_vision"] == 1

    async def test_partial_failure_reports_correctly(self):
        """Step 1 succeeds, step 2 fails permanently -- result shows partial."""
        cu = self._build_cu()
        steps = _make_steps(2)
        self.mock_planner.plan_goal.return_value = steps

        # Step 0 succeeds, step 1 always fails (even retries)
        self.mock_executor.execute_step.side_effect = [
            _make_success_result("step-0"),
            _make_failure_result("step-1"),
            _make_failure_result("step-1"),  # retry 1
        ]

        with patch.dict("os.environ", {"JARVIS_CU_MAX_RETRIES": "1"}):
            cu = self._build_cu()
            result = await cu.run("Do two things")

        assert result["success"] is False
        assert result["steps_completed"] == 1
        assert result["steps_total"] == 2
        assert result["error"] is not None
        assert "step-1" in result["error"]

    async def test_retry_logic_succeeds_on_second_attempt(self):
        """Step fails first attempt, succeeds on retry."""
        cu = self._build_cu()
        steps = _make_steps(1)
        self.mock_planner.plan_goal.return_value = steps

        # First attempt fails, retry succeeds
        self.mock_executor.execute_step.side_effect = [
            _make_failure_result("step-0"),
            _make_success_result("step-0", "doubleword"),
        ]

        with patch.dict("os.environ", {"JARVIS_CU_MAX_RETRIES": "2"}):
            cu = self._build_cu()
            result = await cu.run("Click the button")

        assert result["success"] is True
        assert result["steps_completed"] == 1

    async def test_planner_returns_empty_steps(self):
        """If planner returns no steps, run reports success with 0 steps."""
        cu = self._build_cu()
        self.mock_planner.plan_goal.return_value = []

        result = await cu.run("Do nothing")

        assert result["success"] is True
        assert result["steps_completed"] == 0
        assert result["steps_total"] == 0

    async def test_planner_exception_returns_error(self):
        """If planner raises, run catches and reports error."""
        cu = self._build_cu()
        self.mock_planner.plan_goal.side_effect = RuntimeError("API down")

        result = await cu.run("Break things")

        assert result["success"] is False
        assert "API down" in result["error"]

    async def test_timeout_enforced(self):
        """Steps that exceed TIMEOUT_S cause early termination."""
        cu = self._build_cu()
        steps = _make_steps(5)
        self.mock_planner.plan_goal.return_value = steps

        call_count = 0

        async def slow_execute(step, frame):
            nonlocal call_count
            call_count += 1
            # Simulate passage of time by monkey-patching the start time
            # We'll use the environment variable approach instead
            return _make_success_result(step.step_id)

        self.mock_executor.execute_step.side_effect = slow_execute

        # Use a very short timeout so it triggers
        with patch.dict("os.environ", {"JARVIS_CU_TIMEOUT_S": "0.001"}):
            cu = self._build_cu()
            # Patch time.monotonic to simulate time passing
            original_monotonic = time.monotonic
            call_idx = 0

            def advancing_monotonic():
                nonlocal call_idx
                call_idx += 1
                # First call is the start timestamp; subsequent calls advance quickly
                return original_monotonic() + (call_idx * 1.0)

            with patch("backend.vision.jarvis_cu.time") as mock_time:
                # First call: start time = 0
                # Subsequent calls: always past timeout
                mock_time.monotonic.side_effect = [0.0, 0.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0]
                mock_time.sleep = MagicMock()  # No-op sleep
                result = await cu.run("Lots of steps")

        # Should have timed out before completing all 5
        assert result["success"] is False
        assert "timeout" in result["error"].lower()


class TestJarvisCUSingleton:
    """Test singleton pattern."""

    def _reset(self):
        from backend.vision.jarvis_cu import JarvisCU
        JarvisCU._instance = None

    @pytest.fixture(autouse=True)
    def _patch_imports(self):
        with (
            patch("backend.vision.jarvis_cu.CUTaskPlanner"),
            patch("backend.vision.jarvis_cu.CUStepExecutor"),
            patch("backend.vision.jarvis_cu.ShmFrameReader") as mock_shm_cls,
        ):
            mock_shm_cls.return_value.open.return_value = False
            self._reset()
            yield
            self._reset()

    def test_get_instance_returns_none_before_creation(self):
        from backend.vision.jarvis_cu import JarvisCU
        assert JarvisCU.get_instance() is None

    def test_set_instance_and_get_instance(self):
        from backend.vision.jarvis_cu import JarvisCU
        cu = JarvisCU()
        JarvisCU.set_instance(cu)
        assert JarvisCU.get_instance() is cu

    def test_constructor_self_registers(self):
        from backend.vision.jarvis_cu import JarvisCU
        cu = JarvisCU()
        assert JarvisCU.get_instance() is cu


class TestJarvisCULayerTracking:
    """Verify layers_used dict accumulates correctly."""

    @pytest.fixture(autouse=True)
    def _patch_imports(self):
        with (
            patch("backend.vision.jarvis_cu.CUTaskPlanner") as mock_planner_cls,
            patch("backend.vision.jarvis_cu.CUStepExecutor") as mock_executor_cls,
            patch("backend.vision.jarvis_cu.ShmFrameReader") as mock_shm_cls,
        ):
            self.mock_planner = AsyncMock()
            self.mock_executor = AsyncMock()
            mock_planner_cls.return_value = self.mock_planner
            mock_executor_cls.return_value = self.mock_executor
            mock_shm_cls.return_value.open.return_value = False
            yield

    async def test_layer_counts_aggregate(self):
        from backend.vision.jarvis_cu import JarvisCU
        JarvisCU._instance = None
        cu = JarvisCU()

        steps = _make_steps(4)
        self.mock_planner.plan_goal.return_value = steps
        self.mock_executor.execute_step.side_effect = [
            _make_success_result("step-0", "accessibility"),
            _make_success_result("step-1", "accessibility"),
            _make_success_result("step-2", "doubleword"),
            _make_success_result("step-3", "accessibility"),
        ]

        result = await cu.run("Four steps")

        assert result["layers_used"]["accessibility"] == 3
        assert result["layers_used"]["doubleword"] == 1
        assert result["layers_used"].get("claude_vision", 0) == 0
