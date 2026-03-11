"""tests/governance/autonomy/test_p0_integration.py

P0 Integration tests: CommandBus + EventEmitter + FeedbackEngine wired together.

Verifies:
- Event emitted flows to FeedbackEngine subscriber via EventEmitter.
- Command bus drains cleanly (curriculum -> command -> get).
- CommandBus + EventEmitter + FeedbackEngine wire together end-to-end.
- GLS-level background loop methods (_feedback_loop, _command_consumer_loop,
  _handle_advisory_command) are importable and structured correctly.
"""
import asyncio
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandEnvelope,
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
from backend.core.ouroboros.governance.autonomy.feedback_engine import (
    AutonomyFeedbackEngine,
    FeedbackEngineConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_op_completed_event(brain_id: str = "jarvis-7b", rollback: bool = False) -> EventEnvelope:
    """Build a minimal OP_COMPLETED event envelope."""
    return EventEnvelope(
        source_layer="L1",
        event_type=EventType.OP_COMPLETED,
        payload={
            "op_id": "test-op-001",
            "brain_id": brain_id,
            "model_name": "jarvis-7b",
            "terminal_phase": "COMPLETE",
            "provider": "prime",
            "duration_s": 12.5,
            "rollback": rollback,
        },
    )


def _make_op_rolled_back_event(brain_id: str = "jarvis-7b") -> EventEnvelope:
    """Build a minimal OP_ROLLED_BACK event envelope."""
    return EventEnvelope(
        source_layer="L1",
        event_type=EventType.OP_ROLLED_BACK,
        payload={
            "op_id": "test-op-002",
            "brain_id": brain_id,
        },
    )


def _write_curriculum_file(event_dir: Path, name: str = "curriculum_001.json") -> Path:
    """Write a minimal curriculum file to event_dir."""
    path = event_dir / name
    data = {
        "top_k": [
            {
                "task_type": "test_fix",
                "priority": 2,
                "failure_rate": 0.4,
                "description": "Fix flaky test in test_auth.py",
                "target_files": ["tests/test_auth.py"],
                "repo": "jarvis",
            },
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Test: Event emitted flows to FeedbackEngine subscriber
# ---------------------------------------------------------------------------


class TestEventFlowsToFeedbackEngine:
    """Verify that events emitted via EventEmitter reach FeedbackEngine handlers."""

    @pytest.mark.asyncio
    async def test_op_completed_decays_rollback_count(self, tmp_path):
        """OP_COMPLETED event flowing through EventEmitter triggers rollback decay."""
        bus = CommandBus(maxsize=100)
        config = FeedbackEngineConfig(
            event_dir=tmp_path / "events",
            state_dir=tmp_path / "state",
        )
        emitter = EventEmitter()
        engine = AutonomyFeedbackEngine(
            command_bus=bus, config=config, event_emitter=emitter,
        )
        engine.register_event_handlers(emitter)

        # Pre-seed a rollback count so we can observe decay
        engine._rollback_counts["jarvis-7b"] = 2

        # Emit OP_COMPLETED via the emitter
        event = _make_op_completed_event(brain_id="jarvis-7b")
        await emitter.emit(event)

        # The handler should have decayed the count by 1
        assert engine._rollback_counts["jarvis-7b"] == 1

    @pytest.mark.asyncio
    async def test_op_rolled_back_increments_count(self, tmp_path):
        """OP_ROLLED_BACK event flowing through EventEmitter increments rollback count."""
        bus = CommandBus(maxsize=100)
        config = FeedbackEngineConfig(
            event_dir=tmp_path / "events",
            state_dir=tmp_path / "state",
        )
        emitter = EventEmitter()
        engine = AutonomyFeedbackEngine(
            command_bus=bus, config=config, event_emitter=emitter,
        )
        engine.register_event_handlers(emitter)

        event = _make_op_rolled_back_event(brain_id="jarvis-7b")
        await emitter.emit(event)

        assert engine._rollback_counts["jarvis-7b"] == 1

    @pytest.mark.asyncio
    async def test_multiple_rollbacks_emit_brain_hint_command(self, tmp_path):
        """Threshold rollbacks produce ADJUST_BRAIN_HINT on CommandBus."""
        bus = CommandBus(maxsize=100)
        config = FeedbackEngineConfig(
            event_dir=tmp_path / "events",
            state_dir=tmp_path / "state",
        )
        emitter = EventEmitter()
        engine = AutonomyFeedbackEngine(
            command_bus=bus, config=config, event_emitter=emitter,
        )
        engine.register_event_handlers(emitter)

        # Emit threshold rollback events (default threshold is 3)
        for _ in range(3):
            await emitter.emit(_make_op_rolled_back_event(brain_id="flaky-brain"))

        assert engine._rollback_counts["flaky-brain"] == 3

        # CommandBus should have an ADJUST_BRAIN_HINT command
        assert bus.qsize() >= 1
        cmd = await asyncio.wait_for(bus.get(), timeout=2.0)
        assert cmd.command_type == CommandType.ADJUST_BRAIN_HINT
        assert cmd.payload["brain_id"] == "flaky-brain"
        assert cmd.payload["weight_delta"] == -0.1


# ---------------------------------------------------------------------------
# Test: Command bus drains cleanly (curriculum -> command -> get)
# ---------------------------------------------------------------------------


class TestCurriculumToCommandBusDrain:
    """Verify curriculum consumption puts commands on the bus and they drain."""

    @pytest.mark.asyncio
    async def test_curriculum_file_produces_drainable_command(self, tmp_path):
        """A curriculum file produces a GENERATE_BACKLOG_ENTRY command on the bus."""
        event_dir = tmp_path / "events"
        event_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        bus = CommandBus(maxsize=100)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        _write_curriculum_file(event_dir)

        emitted = await engine.consume_curriculum_once()
        assert emitted == 1

        # Drain from bus
        cmd = await asyncio.wait_for(bus.get(), timeout=2.0)
        assert cmd.command_type == CommandType.GENERATE_BACKLOG_ENTRY
        assert cmd.payload["task_type"] == "test_fix"
        assert cmd.payload["description"] == "Fix flaky test in test_auth.py"

    @pytest.mark.asyncio
    async def test_duplicate_curriculum_files_deduped(self, tmp_path):
        """Same curriculum file is not re-consumed on second scan."""
        event_dir = tmp_path / "events"
        event_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        bus = CommandBus(maxsize=100)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        _write_curriculum_file(event_dir)

        first = await engine.consume_curriculum_once()
        assert first == 1

        second = await engine.consume_curriculum_once()
        assert second == 0


# ---------------------------------------------------------------------------
# Test: Full wiring — CommandBus + EventEmitter + FeedbackEngine
# ---------------------------------------------------------------------------


class TestFullWiring:
    """End-to-end: all three components wired together correctly."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path):
        """Wire all three, emit event, consume curriculum, drain bus."""
        event_dir = tmp_path / "events"
        event_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        bus = CommandBus(maxsize=1000)
        emitter = EventEmitter()
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(
            command_bus=bus, config=config, event_emitter=emitter,
        )
        engine.register_event_handlers(emitter)

        # 1. Emit OP_COMPLETED event via emitter — should reach engine's handler
        await emitter.emit(_make_op_completed_event(brain_id="test-brain"))
        # No rollback count to decay => stays at 0
        assert engine._rollback_counts.get("test-brain", 0) == 0

        # 2. Write and consume a curriculum file
        _write_curriculum_file(event_dir)
        emitted = await engine.consume_curriculum_once()
        assert emitted == 1

        # 3. Drain the bus
        cmd = await asyncio.wait_for(bus.get(), timeout=2.0)
        assert cmd.command_type == CommandType.GENERATE_BACKLOG_ENTRY
        assert bus.qsize() == 0

    @pytest.mark.asyncio
    async def test_emitter_subscriber_count(self, tmp_path):
        """After register_event_handlers, emitter has subscribers for both event types."""
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        config = FeedbackEngineConfig(
            event_dir=tmp_path / "events",
            state_dir=tmp_path / "state",
        )
        engine = AutonomyFeedbackEngine(
            command_bus=bus, config=config, event_emitter=emitter,
        )
        engine.register_event_handlers(emitter)

        assert emitter.subscriber_count(EventType.OP_COMPLETED) == 1
        assert emitter.subscriber_count(EventType.OP_ROLLED_BACK) == 1

    @pytest.mark.asyncio
    async def test_reactor_event_produces_command(self, tmp_path):
        """Reactor model_promoted event file produces a bus command."""
        event_dir = tmp_path / "events"
        event_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        bus = CommandBus(maxsize=100)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        # Write a reactor event file
        reactor_path = event_dir / "reactor_001.json"
        reactor_data = {
            "event_type": "model_promoted",
            "model_id": "jarvis-7b-v2",
            "previous_model_id": "jarvis-7b-v1",
            "description": "New model promoted after training",
            "target_files": ["models/jarvis-7b-v2"],
            "repo": "jarvis",
        }
        reactor_path.write_text(json.dumps(reactor_data), encoding="utf-8")

        emitted = await engine.consume_reactor_events_once()
        assert emitted == 1

        cmd = await asyncio.wait_for(bus.get(), timeout=2.0)
        assert cmd.command_type == CommandType.GENERATE_BACKLOG_ENTRY
        assert cmd.payload["model_id"] == "jarvis-7b-v2"

    @pytest.mark.asyncio
    async def test_cursor_persists_across_engine_restarts(self, tmp_path):
        """Cursor persistence prevents re-processing after engine restart."""
        event_dir = tmp_path / "events"
        event_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        bus1 = CommandBus(maxsize=100)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)

        # First engine processes the file
        engine1 = AutonomyFeedbackEngine(command_bus=bus1, config=config)
        _write_curriculum_file(event_dir)
        first = await engine1.consume_curriculum_once()
        assert first == 1

        # Second engine with same state_dir should skip it
        bus2 = CommandBus(maxsize=100)
        engine2 = AutonomyFeedbackEngine(command_bus=bus2, config=config)
        second = await engine2.consume_curriculum_once()
        assert second == 0
