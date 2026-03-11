"""tests/governance/autonomy/test_feedback_engine_reactor.py

TDD tests for AutonomyFeedbackEngine — Reactor Event Consumption (Task 6, C+ Autonomous Loop).

Covers:
- model_promoted event generates backlog command
- unknown reactor event type ignored (no commands)
- duplicate reactor file not reprocessed
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandType,
)


def _write_reactor_event(
    event_dir: Path,
    filename: str = "reactor_001.json",
    *,
    event_type: str = "model_promoted",
    model_id: str = "prime-v2.3",
    previous_model_id: str = "prime-v2.2",
    description: str = "Model prime-v2.3 promoted after outperforming prime-v2.2 on eval suite",
    extra: dict | None = None,
) -> Path:
    """Write a reactor JSON event file into *event_dir* and return the path."""
    data: dict = {
        "event_type": event_type,
        "model_id": model_id,
        "previous_model_id": previous_model_id,
        "description": description,
    }
    if extra:
        data.update(extra)
    path = event_dir / filename
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _make_engine(tmp_path):
    """Build a FeedbackEngine with minimal config and return (engine, bus)."""
    from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
    from backend.core.ouroboros.governance.autonomy.feedback_engine import (
        AutonomyFeedbackEngine,
        FeedbackEngineConfig,
    )

    event_dir = tmp_path / "events"
    state_dir = tmp_path / "state"
    event_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)

    bus = CommandBus(maxsize=64)
    config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
    engine = AutonomyFeedbackEngine(command_bus=bus, config=config)
    return engine, bus, event_dir, state_dir


# ---------------------------------------------------------------------------
# model_promoted event generates backlog command
# ---------------------------------------------------------------------------


class TestModelPromotedGeneratesBacklogCommand:
    @pytest.mark.asyncio
    async def test_model_promoted_creates_generate_backlog_entry(self, tmp_path):
        """A reactor file with event_type=model_promoted should produce a
        GENERATE_BACKLOG_ENTRY command on the bus."""
        engine, bus, event_dir, _ = _make_engine(tmp_path)

        _write_reactor_event(event_dir)

        count = await engine.consume_reactor_events_once()
        assert count == 1
        assert bus.qsize() == 1

        cmd = await bus.get()
        assert cmd.command_type == CommandType.GENERATE_BACKLOG_ENTRY
        assert cmd.source_layer == "L2"
        assert cmd.target_layer == "L1"

    @pytest.mark.asyncio
    async def test_model_promoted_payload_shape(self, tmp_path):
        """The generated command's payload should carry the reactor event data."""
        engine, bus, event_dir, _ = _make_engine(tmp_path)

        _write_reactor_event(
            event_dir,
            model_id="prime-v3.0",
            previous_model_id="prime-v2.9",
            description="Promoted after eval gains",
        )

        count = await engine.consume_reactor_events_once()
        assert count == 1

        cmd = await bus.get()
        p = cmd.payload
        assert p["task_type"] == "code_improvement"
        assert p["source_event"] == "model_promoted"
        assert p["model_id"] == "prime-v3.0"
        assert p["previous_model_id"] == "prime-v2.9"
        assert p["description"] == "Promoted after eval gains"
        assert p["target_files"] == []
        assert p["repo"] == "jarvis"

    @pytest.mark.asyncio
    async def test_multiple_reactor_files_processed(self, tmp_path):
        """Multiple reactor files should each produce a command."""
        engine, bus, event_dir, _ = _make_engine(tmp_path)

        _write_reactor_event(event_dir, "reactor_001.json", model_id="v1")
        _write_reactor_event(event_dir, "reactor_002.json", model_id="v2")

        count = await engine.consume_reactor_events_once()
        assert count == 2
        assert bus.qsize() == 2

    @pytest.mark.asyncio
    async def test_malformed_reactor_file_skipped_gracefully(self, tmp_path):
        """A reactor file with bad JSON should be skipped without crashing."""
        engine, bus, event_dir, _ = _make_engine(tmp_path)

        # Bad JSON
        (event_dir / "reactor_bad.json").write_text("{corrupt!", encoding="utf-8")
        # Good file
        _write_reactor_event(event_dir, "reactor_good.json")

        count = await engine.consume_reactor_events_once()
        assert count == 1
        assert bus.qsize() == 1

    @pytest.mark.asyncio
    async def test_missing_event_dir_returns_zero(self, tmp_path):
        """If event_dir does not exist, return 0 without crashing."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (
            AutonomyFeedbackEngine,
            FeedbackEngineConfig,
        )

        event_dir = tmp_path / "nonexistent_events"
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        bus = CommandBus(maxsize=64)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        count = await engine.consume_reactor_events_once()
        assert count == 0


# ---------------------------------------------------------------------------
# Unknown reactor event type ignored (no commands)
# ---------------------------------------------------------------------------


class TestUnknownReactorEventIgnored:
    @pytest.mark.asyncio
    async def test_unknown_event_type_produces_no_command(self, tmp_path):
        """A reactor file with an unrecognized event_type should be silently skipped."""
        engine, bus, event_dir, _ = _make_engine(tmp_path)

        _write_reactor_event(
            event_dir,
            "reactor_mystery.json",
            event_type="model_evaluation_started",
        )

        count = await engine.consume_reactor_events_once()
        assert count == 0
        assert bus.qsize() == 0

    @pytest.mark.asyncio
    async def test_unknown_event_type_still_marked_as_seen(self, tmp_path):
        """Unknown event types should still be added to _seen_files so they
        are not re-read on the next scan."""
        engine, bus, event_dir, _ = _make_engine(tmp_path)

        _write_reactor_event(
            event_dir,
            "reactor_unknown.json",
            event_type="model_eval_done",
        )

        count1 = await engine.consume_reactor_events_once()
        assert count1 == 0

        # Second call: the file should be skipped
        count2 = await engine.consume_reactor_events_once()
        assert count2 == 0

    @pytest.mark.asyncio
    async def test_mix_of_known_and_unknown_events(self, tmp_path):
        """Only model_promoted events should generate commands; others are ignored."""
        engine, bus, event_dir, _ = _make_engine(tmp_path)

        _write_reactor_event(
            event_dir, "reactor_001.json",
            event_type="model_promoted", model_id="v1",
        )
        _write_reactor_event(
            event_dir, "reactor_002.json",
            event_type="training_started", model_id="v2",
        )
        _write_reactor_event(
            event_dir, "reactor_003.json",
            event_type="model_promoted", model_id="v3",
        )

        count = await engine.consume_reactor_events_once()
        assert count == 2  # only the two model_promoted events
        assert bus.qsize() == 2


# ---------------------------------------------------------------------------
# Duplicate reactor file not reprocessed
# ---------------------------------------------------------------------------


class TestDuplicateReactorFileNotReprocessed:
    @pytest.mark.asyncio
    async def test_same_file_not_reprocessed_on_second_call(self, tmp_path):
        """Already-seen reactor files should be skipped on subsequent calls."""
        engine, bus, event_dir, _ = _make_engine(tmp_path)

        _write_reactor_event(event_dir)

        count1 = await engine.consume_reactor_events_once()
        assert count1 == 1

        # Drain the bus
        await bus.get()
        assert bus.qsize() == 0

        # Second call: same file should be skipped
        count2 = await engine.consume_reactor_events_once()
        assert count2 == 0
        assert bus.qsize() == 0

    @pytest.mark.asyncio
    async def test_new_reactor_file_processed_after_old_skipped(self, tmp_path):
        """A new reactor file arriving after the first scan is processed,
        while the old one is still skipped."""
        engine, bus, event_dir, _ = _make_engine(tmp_path)

        _write_reactor_event(event_dir, "reactor_first.json", model_id="v1")

        count1 = await engine.consume_reactor_events_once()
        assert count1 == 1
        await bus.get()

        # Add a new file
        _write_reactor_event(event_dir, "reactor_second.json", model_id="v2")

        count2 = await engine.consume_reactor_events_once()
        assert count2 == 1
        assert bus.qsize() == 1

        cmd = await bus.get()
        assert cmd.payload["model_id"] == "v2"

    @pytest.mark.asyncio
    async def test_cursor_persisted_across_engine_restart(self, tmp_path):
        """A new engine instance loading from the same state_dir should skip
        reactor files already processed by the previous instance."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (
            AutonomyFeedbackEngine,
            FeedbackEngineConfig,
        )

        event_dir = tmp_path / "events"
        state_dir = tmp_path / "state"
        event_dir.mkdir()
        state_dir.mkdir()

        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)

        # Engine 1: process the reactor file
        bus1 = CommandBus(maxsize=64)
        engine1 = AutonomyFeedbackEngine(command_bus=bus1, config=config)
        _write_reactor_event(event_dir, "reactor_persisted.json")

        count1 = await engine1.consume_reactor_events_once()
        assert count1 == 1

        # Engine 2: new instance, same config — should load cursor and skip
        bus2 = CommandBus(maxsize=64)
        engine2 = AutonomyFeedbackEngine(command_bus=bus2, config=config)

        count2 = await engine2.consume_reactor_events_once()
        assert count2 == 0
        assert bus2.qsize() == 0

    @pytest.mark.asyncio
    async def test_reactor_and_curriculum_share_seen_files(self, tmp_path):
        """Reactor and curriculum both use the same cursor, so filenames
        from either namespace persist correctly across scans."""
        engine, bus, event_dir, state_dir = _make_engine(tmp_path)

        # Write both a curriculum and a reactor file
        curriculum_data = {
            "event_type": "curriculum_signal",
            "top_k": [{"task_type": "fix_test", "priority": 1, "failure_rate": 0.2}],
        }
        (event_dir / "curriculum_001.json").write_text(
            json.dumps(curriculum_data), encoding="utf-8",
        )
        _write_reactor_event(event_dir, "reactor_001.json")

        # Process both
        c_count = await engine.consume_curriculum_once()
        r_count = await engine.consume_reactor_events_once()
        assert c_count == 1
        assert r_count == 1

        # Verify both are in the cursor
        cursor_path = state_dir / "feedback_engine_cursor.json"
        assert cursor_path.exists()
        cursor_data = json.loads(cursor_path.read_text(encoding="utf-8"))
        assert "curriculum_001.json" in cursor_data["seen_files"]
        assert "reactor_001.json" in cursor_data["seen_files"]
