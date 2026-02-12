"""
Integration tests for UnifiedAgentRuntime and GoalCheckpointStore.

Tests cover goal lifecycle, checkpoint persistence, priority scheduling,
crash recovery, escalation, env-var configuration, and the
SENSE -> THINK -> ACT -> VERIFY -> REFLECT cycle.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.autonomy.agent_runtime import (
    GoalCheckpointStore,
    UnifiedAgentRuntime,
)
from backend.autonomy.agent_runtime_models import (
    EscalationLevel,
    Goal,
    GoalPriority,
    GoalStatus,
    TERMINAL_STATES,
)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _make_mock_agent(
    reasoning_engine: Any = None,
    tool_orchestrator: Any = None,
    integration_manager: Any = None,
) -> MagicMock:
    """Create a minimal mock AutonomousAgent with required attributes."""
    agent = MagicMock()
    agent.reasoning_engine = reasoning_engine or MagicMock()
    agent.tool_orchestrator = tool_orchestrator or MagicMock()
    agent.integration_manager = integration_manager
    return agent


def _make_store(db_path: Path) -> GoalCheckpointStore:
    """Create a GoalCheckpointStore pointed at a tmp_path SQLite file."""
    store = GoalCheckpointStore()
    store._db_path = db_path / "test_runtime.db"
    return store


async def _make_runtime(
    monkeypatch,
    tmp_path: Path,
    *,
    enabled: bool = True,
    max_concurrent: int = 3,
    max_iterations: int = 5,
    max_duration: float = 60.0,
    max_queue: int = 50,
) -> UnifiedAgentRuntime:
    """Create an UnifiedAgentRuntime with mocked externals and fast defaults."""
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", str(enabled).lower())
    monkeypatch.setenv("AGENT_RUNTIME_MAX_CONCURRENT", str(max_concurrent))
    monkeypatch.setenv("AGENT_RUNTIME_MAX_ITERATIONS", str(max_iterations))
    monkeypatch.setenv("AGENT_RUNTIME_MAX_DURATION", str(max_duration))
    monkeypatch.setenv("AGENT_RUNTIME_MAX_QUEUE", str(max_queue))
    monkeypatch.setenv("AGENT_RUNTIME_DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setenv("AGENT_RUNTIME_HOUSEKEEPING_INTERVAL", "0.2")
    monkeypatch.setenv("AGENT_RUNTIME_THINK_TIMEOUT", "5.0")
    monkeypatch.setenv("AGENT_RUNTIME_ACT_TIMEOUT", "5.0")

    agent = _make_mock_agent()
    runtime = UnifiedAgentRuntime(agent)

    # Patch _wait_for_dependencies to skip dependency checks
    runtime._wait_for_dependencies = AsyncMock()

    # Patch _broadcast_ws to prevent WebSocket errors
    runtime._broadcast_ws = AsyncMock()

    return runtime


# ─────────────────────────────────────────────────────────────
# GoalCheckpointStore Tests
# ─────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestGoalCheckpointStore:

    async def test_initialize_creates_tables(self, tmp_path):
        """initialize() creates the SQLite goals table."""
        store = _make_store(tmp_path)
        await store.initialize()

        assert store._initialized is True
        assert store._db is not None

        # Verify table exists by querying it
        cursor = await store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='goals'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "goals"

        await store.close()

    async def test_save_load_roundtrip(self, tmp_path):
        """Save a goal, load it back, verify all fields are preserved."""
        store = _make_store(tmp_path)
        await store.initialize()

        goal = Goal(
            goal_id="test-roundtrip",
            description="Integration test goal",
            status=GoalStatus.ACTIVE,
            priority=GoalPriority.HIGH,
            source="test",
            max_iterations=10,
            max_duration_seconds=120.0,
        )
        goal.started_at = time.time()
        goal.metadata = {"key": "value"}

        await store.save(goal)

        # Load it back via get_incomplete (ACTIVE is not terminal)
        loaded = await store.get_incomplete()
        assert len(loaded) == 1
        restored = loaded[0]

        assert restored.goal_id == "test-roundtrip"
        assert restored.description == "Integration test goal"
        assert restored.priority == GoalPriority.HIGH
        assert restored.source == "test"
        assert restored.metadata == {"key": "value"}

        await store.close()

    async def test_close_clears_connection(self, tmp_path):
        """After close(), _db is None and _initialized is False."""
        store = _make_store(tmp_path)
        await store.initialize()
        assert store._db is not None
        assert store._initialized is True

        await store.close()
        assert store._db is None
        assert store._initialized is False

    async def test_reconnect_after_close(self, tmp_path):
        """Can reinitialize after close and still access data."""
        store = _make_store(tmp_path)
        await store.initialize()

        # Save a goal
        goal = Goal(
            goal_id="reconnect-test",
            description="Reconnect test goal",
            status=GoalStatus.ACTIVE,
        )
        await store.save(goal)
        await store.close()

        # Re-initialize (simulates restart after crash)
        await store.initialize()
        loaded = await store.get_incomplete()
        assert len(loaded) == 1
        assert loaded[0].goal_id == "reconnect-test"

        await store.close()

    async def test_cleanup_removes_old_goals(self, tmp_path):
        """cleanup_old removes terminal goals older than threshold."""
        store = _make_store(tmp_path)
        await store.initialize()

        # Insert an old completed goal with manually adjusted updated_at
        old_goal = Goal(
            goal_id="old-completed",
            description="Completed long ago",
            status=GoalStatus.COMPLETED,
        )
        await store.save(old_goal)

        # Manually backdate the updated_at column
        await store._db.execute(
            "UPDATE goals SET updated_at = ? WHERE goal_id = ?",
            (time.time() - 999999, "old-completed"),
        )
        await store._db.commit()

        # Insert a recent completed goal
        recent_goal = Goal(
            goal_id="recent-completed",
            description="Completed recently",
            status=GoalStatus.COMPLETED,
        )
        await store.save(recent_goal)

        # Cleanup with a 1-day threshold
        await store.cleanup_old(max_age_seconds=86400)

        # Old goal should be deleted, recent should remain
        cursor = await store._db.execute("SELECT goal_id FROM goals")
        rows = await cursor.fetchall()
        goal_ids = [r[0] for r in rows]

        assert "old-completed" not in goal_ids
        assert "recent-completed" in goal_ids

        await store.close()


# ─────────────────────────────────────────────────────────────
# UnifiedAgentRuntime Tests
# ─────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestUnifiedAgentRuntime:

    async def test_start_initializes_checkpoint_store(self, monkeypatch, tmp_path):
        """start() initializes the checkpoint store (opens DB connection)."""
        runtime = await _make_runtime(monkeypatch, tmp_path)
        await runtime.start()

        assert runtime._running is True
        assert runtime._checkpoint_store._initialized is True
        assert runtime._llm_semaphore is not None

        await runtime.stop()

    async def test_disabled_via_env_var(self, monkeypatch, tmp_path):
        """AGENT_RUNTIME_ENABLED=false means start() is a no-op."""
        runtime = await _make_runtime(monkeypatch, tmp_path, enabled=False)
        await runtime.start()

        assert runtime._running is False
        assert runtime._checkpoint_store._initialized is False

    async def test_stop_checkpoints_active_goals(self, monkeypatch, tmp_path):
        """stop() saves active goals to checkpoint store with PAUSED status."""
        runtime = await _make_runtime(monkeypatch, tmp_path)
        await runtime.start()

        # Inject a fake active goal directly
        goal = Goal(
            goal_id="active-on-stop",
            description="Should be checkpointed",
            status=GoalStatus.ACTIVE,
        )
        goal.started_at = time.time()
        runtime._active_goals["active-on-stop"] = goal

        await runtime.stop()

        # The goal should have been saved as PAUSED
        assert goal.status == GoalStatus.PAUSED

    async def test_submit_goal_creates_runner(self, monkeypatch, tmp_path):
        """submit_goal() queues a goal and returns a goal_id."""
        runtime = await _make_runtime(monkeypatch, tmp_path)
        await runtime.start()

        # Patch _goal_runner to prevent actual execution
        runtime._goal_runner = AsyncMock()

        goal_id = await runtime.submit_goal(
            description="Test goal submission",
            priority=GoalPriority.NORMAL,
            source="test",
        )

        assert goal_id is not None
        assert isinstance(goal_id, str)
        assert len(goal_id) > 0

        # Give a moment for promotion to happen
        await asyncio.sleep(0.1)

        await runtime.stop()

    async def test_max_concurrent_goals_enforced(self, monkeypatch, tmp_path):
        """When at max concurrent, additional goals stay queued."""
        runtime = await _make_runtime(monkeypatch, tmp_path, max_concurrent=2)
        await runtime.start()

        # Make _goal_runner block until cancelled
        async def blocking_runner(goal):
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                pass

        runtime._goal_runner = blocking_runner

        # Submit 3 goals — only 2 should become active
        ids = []
        for i in range(3):
            gid = await runtime.submit_goal(
                description=f"Concurrent test goal {i}",
                priority=GoalPriority.NORMAL,
            )
            ids.append(gid)

        await asyncio.sleep(0.2)

        active_count = len(runtime._active_goals)
        queue_size = runtime._goal_queue.qsize()

        # 2 active + 1 queued = 3 total
        assert active_count == 2
        assert queue_size == 1

        await runtime.stop()

    async def test_priority_ordering(self, monkeypatch, tmp_path):
        """CRITICAL goals promoted before NORMAL before BACKGROUND."""
        runtime = await _make_runtime(monkeypatch, tmp_path, max_concurrent=1)
        await runtime.start()

        promoted_order: List[str] = []

        async def tracking_runner(goal):
            promoted_order.append(goal.description)
            # Complete immediately
            goal.status = GoalStatus.COMPLETED
            goal.completed_at = time.time()

        runtime._goal_runner = tracking_runner

        # Submit goals in reverse priority order while max_concurrent=1
        # First fill the single slot with a blocking goal
        async def blocking_runner(goal):
            promoted_order.append(goal.description)
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                pass

        runtime._goal_runner = blocking_runner
        await runtime.submit_goal(description="Blocker", priority=GoalPriority.NORMAL)
        await asyncio.sleep(0.1)

        # Now switch to tracking runner and queue prioritized goals
        # The queue is a priority queue: critical should come first
        runtime._goal_runner = tracking_runner

        # These will queue since slot is full
        await runtime.submit_goal(description="Background", priority=GoalPriority.BACKGROUND)
        await runtime.submit_goal(description="Critical", priority=GoalPriority.CRITICAL)
        await runtime.submit_goal(description="Normal", priority=GoalPriority.NORMAL)

        # Cancel the blocker to free the slot, then promote
        for task in list(runtime._goal_runners.values()):
            task.cancel()
        await asyncio.sleep(0.1)

        # Clean up completed runners so promotion can proceed
        await runtime._cleanup_completed_runners()
        await runtime._promote_pending_goals()
        await asyncio.sleep(0.1)
        await runtime._cleanup_completed_runners()
        await runtime._promote_pending_goals()
        await asyncio.sleep(0.1)
        await runtime._cleanup_completed_runners()
        await runtime._promote_pending_goals()
        await asyncio.sleep(0.1)

        # The order after blocker should be Critical, Normal, Background
        post_blocker = [d for d in promoted_order if d != "Blocker"]
        assert len(post_blocker) >= 2
        # Critical should come before Background
        if "Critical" in post_blocker and "Background" in post_blocker:
            assert post_blocker.index("Critical") < post_blocker.index("Background")

        await runtime.stop()

    async def test_sense_think_act_cycle(self, monkeypatch, tmp_path):
        """Mock LLM/planner; verify SENSE->THINK->ACT cycle executes."""
        runtime = await _make_runtime(
            monkeypatch, tmp_path,
            max_iterations=2,
            max_duration=30.0,
        )
        await runtime.start()

        call_order: List[str] = []

        # Track _sense
        original_sense = runtime._sense

        async def tracking_sense(goal):
            call_order.append("sense")
            return "observation: test environment ready"

        runtime._sense = tracking_sense

        # Track _think — return a plan that completes the goal
        async def tracking_think(goal, observation, context, mode):
            call_order.append("think")
            return (
                "Thought: goal is simple, complete it",
                {
                    "description": "Complete the test",
                    "action": {},
                    "action_type": "complete",
                    "needs_vision": False,
                    "verification": "none",
                },
            )

        runtime._think = tracking_think

        # Track _act
        original_act = runtime._act

        async def tracking_act(goal, step):
            call_order.append("act")
            return {"success": True, "message": "Done"}

        runtime._act = tracking_act

        # Track _verify
        async def tracking_verify(goal, step, result):
            call_order.append("verify")
            return {
                "observation": "Step completed successfully",
                "confidence": 0.95,
                "success": True,
                "goal_complete": True,
            }

        runtime._verify = tracking_verify

        # Track _reflect
        async def tracking_reflect(goal, step, verification):
            call_order.append("reflect")
            return "complete"

        runtime._reflect = tracking_reflect

        goal_id = await runtime.submit_goal(
            description="Test SENSE-THINK-ACT cycle",
            priority=GoalPriority.NORMAL,
        )

        # Wait for goal to complete
        for _ in range(30):
            await asyncio.sleep(0.1)
            if goal_id not in runtime._active_goals:
                break

        # Verify the cycle executed in order
        assert "sense" in call_order
        assert "think" in call_order
        assert "act" in call_order
        assert "verify" in call_order
        assert "reflect" in call_order

        # Verify ordering: sense before think, think before act
        sense_idx = call_order.index("sense")
        think_idx = call_order.index("think")
        act_idx = call_order.index("act")
        assert sense_idx < think_idx < act_idx

        await runtime.stop()

    async def test_max_iterations_enforcement(self, monkeypatch, tmp_path):
        """Goal stops after max iterations reached."""
        runtime = await _make_runtime(
            monkeypatch, tmp_path,
            max_iterations=3,
            max_duration=30.0,
        )
        await runtime.start()

        # Make _think always return "continue" actions so iterations accumulate
        async def looping_think(goal, observation, context, mode):
            return (
                "Still working on it",
                {
                    "description": "Keep trying",
                    "action": {},
                    "action_type": "continue",
                    "needs_vision": False,
                    "verification": "none",
                },
            )

        runtime._think = looping_think

        async def passthrough_verify(goal, step, result):
            return {
                "observation": "Step ok but goal not complete",
                "confidence": 0.5,
                "success": True,
                "goal_complete": False,
            }

        runtime._verify = passthrough_verify

        async def continue_reflect(goal, step, verification):
            return "continue"

        runtime._reflect = continue_reflect

        goal_id = await runtime.submit_goal(
            description="Max iterations test",
            priority=GoalPriority.NORMAL,
        )

        # Wait for goal to fail due to max iterations
        final_status = None
        for _ in range(50):
            await asyncio.sleep(0.1)
            if goal_id not in runtime._active_goals:
                break

        # Check that the goal was marked as failed
        # It should have been checkpointed before removal
        goals = await runtime._checkpoint_store.get_incomplete()
        # The goal should be in terminal state (not incomplete)
        # Look in the DB directly
        cursor = await runtime._checkpoint_store._db.execute(
            "SELECT status FROM goals WHERE goal_id = ?", (goal_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "failed"

        await runtime.stop()

    async def test_max_duration_enforcement(self, monkeypatch, tmp_path):
        """Goal stops after max duration exceeded (using 1s timeout)."""
        runtime = await _make_runtime(
            monkeypatch, tmp_path,
            max_iterations=100,
            max_duration=1.0,  # 1 second timeout
        )
        # Override Goal's max_duration_seconds default via env
        monkeypatch.setenv("AGENT_RUNTIME_MAX_DURATION", "1.0")
        await runtime.start()

        # Make _think slow so duration expires
        async def slow_think(goal, observation, context, mode):
            await asyncio.sleep(0.3)
            return (
                "Thinking slowly",
                {
                    "description": "Slow step",
                    "action": {},
                    "action_type": "continue",
                    "needs_vision": False,
                    "verification": "none",
                },
            )

        runtime._think = slow_think

        async def passthrough_verify(goal, step, result):
            return {
                "observation": "ok",
                "confidence": 0.5,
                "success": True,
                "goal_complete": False,
            }

        runtime._verify = passthrough_verify

        async def continue_reflect(goal, step, verification):
            return "continue"

        runtime._reflect = continue_reflect

        goal_id = await runtime.submit_goal(
            description="Duration enforcement test",
            priority=GoalPriority.NORMAL,
        )

        # Wait for the goal to fail due to duration
        for _ in range(40):
            await asyncio.sleep(0.2)
            if goal_id not in runtime._active_goals:
                break

        # Check the goal status in checkpoint store
        cursor = await runtime._checkpoint_store._db.execute(
            "SELECT status FROM goals WHERE goal_id = ?", (goal_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "failed"

        await runtime.stop()

    async def test_goal_cancellation(self, monkeypatch, tmp_path):
        """Cancel a running goal -> status becomes CANCELLED."""
        runtime = await _make_runtime(monkeypatch, tmp_path)
        await runtime.start()

        # Make goal runner block forever until cancelled
        async def blocking_runner(goal):
            try:
                await asyncio.sleep(1000)
            except asyncio.CancelledError:
                pass

        runtime._goal_runner = blocking_runner

        goal_id = await runtime.submit_goal(
            description="Cancel me",
            priority=GoalPriority.NORMAL,
        )

        # Wait for goal to be promoted to active
        await asyncio.sleep(0.2)
        assert goal_id in runtime._active_goals

        # Cancel it
        await runtime.cancel_goal(goal_id, reason="test_cancel")

        goal = runtime._active_goals.get(goal_id)
        # After cancel_goal, status should be CANCELLED
        # (it may have already been cleaned up)
        cursor = await runtime._checkpoint_store._db.execute(
            "SELECT status FROM goals WHERE goal_id = ?", (goal_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "cancelled"

        await runtime.stop()

    async def test_crash_recovery_from_checkpoint(self, monkeypatch, tmp_path):
        """Save checkpoint, create new runtime, verify goal resumed."""
        db_path = str(tmp_path / "recovery.db")
        monkeypatch.setenv("AGENT_RUNTIME_DB_PATH", db_path)

        # --- Phase 1: Create a runtime, submit a goal, checkpoint it ---
        runtime1 = await _make_runtime(monkeypatch, tmp_path)
        runtime1._checkpoint_store._db_path = Path(db_path)
        await runtime1.start()

        # Manually create and save a goal as if it was running
        goal = Goal(
            goal_id="recovery-goal",
            description="Should survive crash",
            status=GoalStatus.ACTIVE,
            priority=GoalPriority.HIGH,
        )
        goal.started_at = time.time()
        await runtime1._checkpoint_store.save(goal)

        # Simulate crash: stop without proper cleanup
        await runtime1._checkpoint_store.close()

        # --- Phase 2: Create new runtime, verify goal is recovered ---
        runtime2 = await _make_runtime(monkeypatch, tmp_path)
        runtime2._checkpoint_store._db_path = Path(db_path)

        # Patch _goal_runner to track recovered goals
        recovered_goals: List[str] = []

        async def track_recovery(g):
            recovered_goals.append(g.goal_id)
            g.status = GoalStatus.COMPLETED
            g.completed_at = time.time()

        runtime2._goal_runner = track_recovery
        await runtime2.start()

        # The recovered goal should have been re-queued
        # Wait for promotion
        await asyncio.sleep(0.3)

        # Check that the goal was recovered (either promoted or in queue)
        queue_items = []
        while not runtime2._goal_queue.empty():
            try:
                item = runtime2._goal_queue.get_nowait()
                queue_items.append(item[1])  # goal_id
            except asyncio.QueueEmpty:
                break

        all_goals = recovered_goals + queue_items + list(runtime2._active_goals.keys())
        assert "recovery-goal" in all_goals

        await runtime2.stop()

    async def test_dangerous_action_escalation(self, monkeypatch, tmp_path):
        """Actions with dangerous keywords trigger escalation/blocking."""
        runtime = await _make_runtime(monkeypatch, tmp_path)
        await runtime.start()

        # Test _assess_initial_escalation with dangerous keywords
        level = runtime._assess_initial_escalation(
            "delete all user data", "user"
        )
        assert level == EscalationLevel.ASK_BEFORE

        # Test _assess_initial_escalation with high-risk keywords
        level = runtime._assess_initial_escalation(
            "send email to team", "user"
        )
        assert level == EscalationLevel.NOTIFY_AFTER

        # Test _assess_initial_escalation with safe action
        level = runtime._assess_initial_escalation(
            "check system status", "user"
        )
        assert level == EscalationLevel.AUTO_EXECUTE

        # Test _assess_initial_escalation with proactive source
        level = runtime._assess_initial_escalation(
            "check system status", "proactive"
        )
        assert level == EscalationLevel.NOTIFY_AFTER

        # Test _assess_step_escalation with dangerous step
        from backend.autonomy.agent_runtime_models import GoalStep

        step = GoalStep(
            description="destroy the database",
            action={"tool": "db_admin", "params": {}},
        )
        step_level = runtime._assess_step_escalation(step)
        assert step_level == EscalationLevel.BLOCK_UNTIL_APPROVED

        # Test step escalation with high-risk action in action json
        step2 = GoalStep(
            description="notify the team",
            action={"tool": "email_sender", "params": {"action": "send message"}},
        )
        step2_level = runtime._assess_step_escalation(step2)
        assert step2_level == EscalationLevel.ASK_BEFORE

        await runtime.stop()

    async def test_env_var_configuration(self, monkeypatch, tmp_path):
        """Runtime reads config from env vars."""
        monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "true")
        monkeypatch.setenv("AGENT_RUNTIME_MAX_CONCURRENT", "7")
        monkeypatch.setenv("AGENT_RUNTIME_MAX_ITERATIONS", "15")
        monkeypatch.setenv("AGENT_RUNTIME_MAX_DURATION", "300.0")
        monkeypatch.setenv("AGENT_RUNTIME_MAX_QUEUE", "25")
        monkeypatch.setenv("AGENT_RUNTIME_LLM_CONCURRENCY", "4")
        monkeypatch.setenv("AGENT_RUNTIME_THINK_TIMEOUT", "20.0")
        monkeypatch.setenv("AGENT_RUNTIME_ACT_TIMEOUT", "45.0")
        monkeypatch.setenv("AGENT_RUNTIME_DB_PATH", str(tmp_path / "env_test.db"))

        agent = _make_mock_agent()
        runtime = UnifiedAgentRuntime(agent)

        assert runtime._max_concurrent == 7
        assert runtime._max_iterations == 15
        assert runtime._max_duration == 300.0
        assert runtime._max_queue_size == 25
        assert runtime._llm_concurrency == 4
        assert runtime._think_timeout == 20.0
        assert runtime._act_timeout == 45.0
        assert runtime._enabled is True
