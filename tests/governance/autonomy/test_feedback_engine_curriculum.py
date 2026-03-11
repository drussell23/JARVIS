"""tests/governance/autonomy/test_feedback_engine_curriculum.py

TDD tests for AutonomyFeedbackEngine — Curriculum Consumption (Task 4, C+ Autonomous Loop).

Covers:
- Consuming curriculum generates backlog commands
- Duplicate curriculum file not reprocessed
- Cursor persisted across restart (new engine instance skips already-seen)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_curriculum(
    event_dir: Path,
    filename: str = "curriculum_001.json",
    *,
    top_k: list | None = None,
) -> Path:
    """Write a curriculum JSON file into *event_dir* and return the path."""
    if top_k is None:
        top_k = [
            {
                "task_type": "fix_flaky_test",
                "priority": 2,
                "failure_rate": 0.35,
            },
            {
                "task_type": "refactor_dead_import",
                "priority": 3,
                "failure_rate": 0.10,
            },
        ]
    data = {
        "event_type": "curriculum_signal",
        "top_k": top_k,
    }
    path = event_dir / filename
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Consuming curriculum generates backlog commands
# ---------------------------------------------------------------------------


class TestConsumeCurriculumGeneratesBacklogCommands:
    @pytest.mark.asyncio
    async def test_single_curriculum_file_creates_commands(self, tmp_path):
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandType,
        )
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (
            AutonomyFeedbackEngine,
            FeedbackEngineConfig,
        )

        event_dir = tmp_path / "events"
        state_dir = tmp_path / "state"
        event_dir.mkdir()
        state_dir.mkdir()

        bus = CommandBus(maxsize=64)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        _write_curriculum(event_dir)

        count = await engine.consume_curriculum_once()
        assert count == 2  # two entries in top_k
        assert bus.qsize() == 2

        # Verify the commands have the right shape
        cmd1 = await bus.get()
        assert cmd1.command_type == CommandType.GENERATE_BACKLOG_ENTRY
        assert cmd1.source_layer == "L2"
        assert cmd1.target_layer == "L1"
        assert cmd1.payload["task_type"] in ("fix_flaky_test", "refactor_dead_import")
        assert "priority" in cmd1.payload
        assert "failure_rate" in cmd1.payload
        assert cmd1.payload["repo"] == "jarvis"
        assert "source_curriculum_id" in cmd1.payload

    @pytest.mark.asyncio
    async def test_multiple_curriculum_files(self, tmp_path):
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (
            AutonomyFeedbackEngine,
            FeedbackEngineConfig,
        )

        event_dir = tmp_path / "events"
        state_dir = tmp_path / "state"
        event_dir.mkdir()
        state_dir.mkdir()

        bus = CommandBus(maxsize=64)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        _write_curriculum(event_dir, "curriculum_alpha.json", top_k=[
            {"task_type": "type_a", "priority": 1, "failure_rate": 0.5},
        ])
        _write_curriculum(event_dir, "curriculum_beta.json", top_k=[
            {"task_type": "type_b", "priority": 2, "failure_rate": 0.3},
        ])

        count = await engine.consume_curriculum_once()
        assert count == 2  # one entry from each file
        assert bus.qsize() == 2

    @pytest.mark.asyncio
    async def test_max_backlog_entries_per_curriculum_respected(self, tmp_path):
        """Only max_backlog_entries_per_curriculum items from each file's top_k are emitted."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (
            AutonomyFeedbackEngine,
            FeedbackEngineConfig,
        )

        event_dir = tmp_path / "events"
        state_dir = tmp_path / "state"
        event_dir.mkdir()
        state_dir.mkdir()

        # 10 entries but max is 3
        big_top_k = [
            {"task_type": f"task_{i}", "priority": i, "failure_rate": 0.1 * i}
            for i in range(10)
        ]
        _write_curriculum(event_dir, top_k=big_top_k)

        bus = CommandBus(maxsize=64)
        config = FeedbackEngineConfig(
            event_dir=event_dir,
            state_dir=state_dir,
            max_backlog_entries_per_curriculum=3,
        )
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        count = await engine.consume_curriculum_once()
        assert count == 3
        assert bus.qsize() == 3

    @pytest.mark.asyncio
    async def test_empty_event_dir_returns_zero(self, tmp_path):
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (
            AutonomyFeedbackEngine,
            FeedbackEngineConfig,
        )

        event_dir = tmp_path / "events"
        state_dir = tmp_path / "state"
        event_dir.mkdir()
        state_dir.mkdir()

        bus = CommandBus(maxsize=64)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        count = await engine.consume_curriculum_once()
        assert count == 0
        assert bus.qsize() == 0

    @pytest.mark.asyncio
    async def test_non_curriculum_files_ignored(self, tmp_path):
        """Files not matching curriculum_*.json pattern are ignored."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (
            AutonomyFeedbackEngine,
            FeedbackEngineConfig,
        )

        event_dir = tmp_path / "events"
        state_dir = tmp_path / "state"
        event_dir.mkdir()
        state_dir.mkdir()

        # Write a non-curriculum file
        (event_dir / "reactor_001.json").write_text(
            json.dumps({"event_type": "reactor_signal", "entries": []}),
            encoding="utf-8",
        )

        bus = CommandBus(maxsize=64)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        count = await engine.consume_curriculum_once()
        assert count == 0

    @pytest.mark.asyncio
    async def test_malformed_curriculum_file_skipped_gracefully(self, tmp_path):
        """A curriculum file with bad JSON should be skipped without crashing."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (
            AutonomyFeedbackEngine,
            FeedbackEngineConfig,
        )

        event_dir = tmp_path / "events"
        state_dir = tmp_path / "state"
        event_dir.mkdir()
        state_dir.mkdir()

        # Bad JSON
        (event_dir / "curriculum_bad.json").write_text("{invalid", encoding="utf-8")
        # Good file
        _write_curriculum(event_dir, "curriculum_good.json", top_k=[
            {"task_type": "ok_task", "priority": 1, "failure_rate": 0.2},
        ])

        bus = CommandBus(maxsize=64)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        count = await engine.consume_curriculum_once()
        # Only the good file's entry
        assert count == 1
        assert bus.qsize() == 1


# ---------------------------------------------------------------------------
# Duplicate curriculum file not reprocessed
# ---------------------------------------------------------------------------


class TestDuplicateCurriculumNotReprocessed:
    @pytest.mark.asyncio
    async def test_same_file_not_reprocessed_on_second_call(self, tmp_path):
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (
            AutonomyFeedbackEngine,
            FeedbackEngineConfig,
        )

        event_dir = tmp_path / "events"
        state_dir = tmp_path / "state"
        event_dir.mkdir()
        state_dir.mkdir()

        bus = CommandBus(maxsize=64)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        _write_curriculum(event_dir)

        count1 = await engine.consume_curriculum_once()
        assert count1 == 2

        # Drain the bus so we can measure new enqueues
        await bus.get()
        await bus.get()
        assert bus.qsize() == 0

        # Second call should skip the already-seen file
        count2 = await engine.consume_curriculum_once()
        assert count2 == 0
        assert bus.qsize() == 0

    @pytest.mark.asyncio
    async def test_new_file_processed_after_old_skipped(self, tmp_path):
        """A new file arriving after the first scan is processed, old one still skipped."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (
            AutonomyFeedbackEngine,
            FeedbackEngineConfig,
        )

        event_dir = tmp_path / "events"
        state_dir = tmp_path / "state"
        event_dir.mkdir()
        state_dir.mkdir()

        bus = CommandBus(maxsize=64)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        _write_curriculum(event_dir, "curriculum_first.json", top_k=[
            {"task_type": "first", "priority": 1, "failure_rate": 0.1},
        ])

        count1 = await engine.consume_curriculum_once()
        assert count1 == 1

        # Drain
        await bus.get()

        # Add a second file
        _write_curriculum(event_dir, "curriculum_second.json", top_k=[
            {"task_type": "second", "priority": 2, "failure_rate": 0.2},
        ])

        count2 = await engine.consume_curriculum_once()
        assert count2 == 1  # only the new file
        assert bus.qsize() == 1

        cmd = await bus.get()
        assert cmd.payload["task_type"] == "second"


# ---------------------------------------------------------------------------
# Cursor persisted across restart (new engine instance skips already-seen)
# ---------------------------------------------------------------------------


class TestCursorPersistedAcrossRestart:
    @pytest.mark.asyncio
    async def test_new_engine_instance_skips_already_processed(self, tmp_path):
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

        # Engine 1: process the file
        bus1 = CommandBus(maxsize=64)
        engine1 = AutonomyFeedbackEngine(command_bus=bus1, config=config)

        _write_curriculum(event_dir, "curriculum_persisted.json", top_k=[
            {"task_type": "persisted_task", "priority": 1, "failure_rate": 0.5},
        ])

        count1 = await engine1.consume_curriculum_once()
        assert count1 == 1

        # Engine 2: new instance, same config — should load cursor and skip
        bus2 = CommandBus(maxsize=64)
        engine2 = AutonomyFeedbackEngine(command_bus=bus2, config=config)

        count2 = await engine2.consume_curriculum_once()
        assert count2 == 0
        assert bus2.qsize() == 0

    @pytest.mark.asyncio
    async def test_cursor_file_is_valid_json(self, tmp_path):
        """The persisted cursor file should be valid JSON with a seen_files list."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (
            AutonomyFeedbackEngine,
            FeedbackEngineConfig,
        )

        event_dir = tmp_path / "events"
        state_dir = tmp_path / "state"
        event_dir.mkdir()
        state_dir.mkdir()

        bus = CommandBus(maxsize=64)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        _write_curriculum(event_dir, "curriculum_x.json", top_k=[
            {"task_type": "x", "priority": 1, "failure_rate": 0.1},
        ])
        await engine.consume_curriculum_once()

        cursor_path = state_dir / "feedback_engine_cursor.json"
        assert cursor_path.exists()
        cursor_data = json.loads(cursor_path.read_text(encoding="utf-8"))
        assert "seen_files" in cursor_data
        assert "curriculum_x.json" in cursor_data["seen_files"]

    @pytest.mark.asyncio
    async def test_corrupted_cursor_treated_as_fresh_start(self, tmp_path):
        """If the cursor file is corrupted, the engine should start fresh."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
        from backend.core.ouroboros.governance.autonomy.feedback_engine import (
            AutonomyFeedbackEngine,
            FeedbackEngineConfig,
        )

        event_dir = tmp_path / "events"
        state_dir = tmp_path / "state"
        event_dir.mkdir()
        state_dir.mkdir()

        # Write corrupted cursor
        cursor_path = state_dir / "feedback_engine_cursor.json"
        cursor_path.write_text("{corrupt!!!", encoding="utf-8")

        bus = CommandBus(maxsize=64)
        config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
        engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

        _write_curriculum(event_dir, "curriculum_after_corrupt.json", top_k=[
            {"task_type": "recovered", "priority": 1, "failure_rate": 0.2},
        ])

        count = await engine.consume_curriculum_once()
        assert count == 1
        assert bus.qsize() == 1
