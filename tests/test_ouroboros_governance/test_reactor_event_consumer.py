"""Tests for ReactorEventConsumer -- bidirectional Reactor-Core bridge."""

import asyncio
import json

import pytest
from typing import Optional
from unittest.mock import AsyncMock, patch

from backend.core.ouroboros.governance.reactor_event_consumer import (
    ReactorEventConsumer,
)
from backend.core.ouroboros.cross_repo import (
    CrossRepoEvent,
    EventType,
    RepoType,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_event_bus():
    """Mock CrossRepoEventBus with async emit."""
    bus = AsyncMock()
    bus.emit = AsyncMock()
    return bus


@pytest.fixture
def inbox_dir(tmp_path):
    """Temporary inbox directory for tests."""
    return tmp_path / "reactor-inbox"


@pytest.fixture
def consumer(mock_event_bus, inbox_dir):
    """ReactorEventConsumer wired to mocks."""
    return ReactorEventConsumer(
        event_bus=mock_event_bus,
        inbox_dir=inbox_dir,
        poll_interval_s=0.1,
    )


def _make_event_dict(
    event_id: str = "evt_test_001",
    event_type: str = "training_complete",
    source: str = "reactor",
    target: str = "jarvis",
    payload: Optional[dict] = None,
) -> dict:
    """Build a valid event dict for writing to a JSON file."""
    return {
        "id": event_id,
        "type": event_type,
        "source_repo": source,
        "target_repo": target,
        "payload": payload or {"model_path": "/models/latest.gguf"},
        "timestamp": 1711000000.0,
        "processed": False,
        "retry_count": 0,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReactorEventConsumerStart:
    @pytest.mark.asyncio
    async def test_start_creates_inbox_directories(self, consumer, inbox_dir):
        """start() creates pending/processed/failed subdirectories."""
        await consumer.start()
        try:
            assert (inbox_dir / "pending").is_dir()
            assert (inbox_dir / "processed").is_dir()
            assert (inbox_dir / "failed").is_dir()
        finally:
            await consumer.stop()

    @pytest.mark.asyncio
    async def test_start_sets_running_flag(self, consumer):
        """start() sets _running = True."""
        await consumer.start()
        try:
            assert consumer._running is True
        finally:
            await consumer.stop()


class TestReactorEventConsumerStop:
    @pytest.mark.asyncio
    async def test_stop_clears_running_flag(self, consumer):
        """stop() sets _running = False and cancels the poll task."""
        await consumer.start()
        await consumer.stop()
        assert consumer._running is False
        assert consumer._poll_task.cancelled() or consumer._poll_task.done()


class TestReactorEventConsumerProcessing:
    @pytest.mark.asyncio
    async def test_processes_valid_event_and_moves_to_processed(
        self, consumer, mock_event_bus, inbox_dir
    ):
        """A valid JSON event file is re-emitted and moved to processed/."""
        # Prepare the inbox
        await consumer.start()
        await asyncio.sleep(0.05)  # let poll loop initialize

        # Write a valid event
        pending = inbox_dir / "pending"
        event_data = _make_event_dict()
        event_file = pending / "evt_test_001.json"
        event_file.write_text(json.dumps(event_data))

        # Wait for one poll cycle
        await asyncio.sleep(0.3)
        await consumer.stop()

        # Verify event was emitted to the bus
        assert mock_event_bus.emit.await_count >= 1
        emitted_event = mock_event_bus.emit.call_args[0][0]
        assert isinstance(emitted_event, CrossRepoEvent)
        assert emitted_event.id == "evt_test_001"
        assert emitted_event.type == EventType.TRAINING_COMPLETE

        # Verify file was moved to processed
        assert not event_file.exists()
        assert (inbox_dir / "processed" / "evt_test_001.json").exists()

    @pytest.mark.asyncio
    async def test_handles_malformed_json_moves_to_failed(
        self, consumer, mock_event_bus, inbox_dir
    ):
        """Malformed JSON is moved to failed/ without crashing."""
        await consumer.start()
        await asyncio.sleep(0.05)

        pending = inbox_dir / "pending"
        bad_file = pending / "bad_event.json"
        bad_file.write_text("{not valid json at all")

        await asyncio.sleep(0.3)
        await consumer.stop()

        # Should not have emitted anything
        mock_event_bus.emit.assert_not_awaited()

        # File should be in failed/
        assert not bad_file.exists()
        assert (inbox_dir / "failed" / "bad_event.json").exists()

    @pytest.mark.asyncio
    async def test_re_emits_event_into_jarvis_bus(
        self, consumer, mock_event_bus, inbox_dir
    ):
        """Processed events are re-emitted into JARVIS's CrossRepoEventBus."""
        await consumer.start()
        await asyncio.sleep(0.05)

        pending = inbox_dir / "pending"
        event_data = _make_event_dict(
            event_id="exp_abc123",
            event_type="experience_generated",
            payload={"experience": "learned something"},
        )
        (pending / "exp_abc123.json").write_text(json.dumps(event_data))

        await asyncio.sleep(0.3)
        await consumer.stop()

        mock_event_bus.emit.assert_awaited()
        emitted = mock_event_bus.emit.call_args[0][0]
        assert emitted.type == EventType.EXPERIENCE_GENERATED
        assert emitted.payload["experience"] == "learned something"

    @pytest.mark.asyncio
    async def test_handles_emit_failure_gracefully(
        self, consumer, mock_event_bus, inbox_dir
    ):
        """If bus.emit raises, the event goes to failed/ and counter increments."""
        mock_event_bus.emit.side_effect = RuntimeError("bus down")

        await consumer.start()
        await asyncio.sleep(0.05)

        pending = inbox_dir / "pending"
        event_data = _make_event_dict(event_id="fail_evt")
        (pending / "fail_evt.json").write_text(json.dumps(event_data))

        await asyncio.sleep(0.3)
        await consumer.stop()

        assert consumer._events_failed >= 1
        assert (inbox_dir / "failed" / "fail_evt.json").exists()


class TestReactorEventConsumerHealth:
    @pytest.mark.asyncio
    async def test_health_returns_correct_metrics(
        self, consumer, mock_event_bus, inbox_dir
    ):
        """health() reflects processed/failed counts and running state."""
        await consumer.start()
        await asyncio.sleep(0.05)

        # Process one good event
        pending = inbox_dir / "pending"
        event_data = _make_event_dict(event_id="h_evt_1")
        (pending / "h_evt_1.json").write_text(json.dumps(event_data))

        await asyncio.sleep(0.3)

        health = consumer.health()
        assert health["running"] is True
        assert health["events_processed"] >= 1
        assert health["events_failed"] == 0
        assert health["inbox_dir"] == str(inbox_dir)

        await consumer.stop()

        health_after = consumer.health()
        assert health_after["running"] is False

    def test_health_before_start(self, consumer, inbox_dir):
        """health() works before start() is called."""
        health = consumer.health()
        assert health["running"] is False
        assert health["events_processed"] == 0
        assert health["events_failed"] == 0
        assert health["inbox_dir"] == str(inbox_dir)


class TestReactorEventConsumerOffload:
    """Tier-2 batch 1 row 8 — the pending-dir glob in ``_poll_loop``
    routes through the ``cooperative_fs_io`` substrate."""

    @pytest.mark.asyncio
    async def test_poll_loop_routes_glob_through_offload(
        self, consumer, mock_event_bus, inbox_dir,
    ):
        """(a) Spy: the pending glob must route through offload()."""
        import backend.core.ouroboros.governance.cooperative_fs_io as fsio

        calls = []
        real_offload = fsio.offload

        async def _spy_offload(fn, *a, **k):
            calls.append(1)
            return await real_offload(fn, *a, **k)

        with patch(
            "backend.core.ouroboros.governance.cooperative_fs_io.offload",
            side_effect=_spy_offload,
        ):
            await consumer.start()
            await asyncio.sleep(0.05)

            pending = inbox_dir / "pending"
            event_data = _make_event_dict(event_id="spy_evt")
            (pending / "spy_evt.json").write_text(json.dumps(event_data))

            await asyncio.sleep(0.3)
            await consumer.stop()

        assert calls, "_poll_loop did not route the glob through offload()"
        assert mock_event_bus.emit.await_count >= 1

    @pytest.mark.asyncio
    async def test_poll_loop_fail_soft_on_offload_error(
        self, consumer, mock_event_bus, inbox_dir,
    ):
        """(c) Fail-soft: OffloadError degrades to an empty pending
        list for that cycle — the poll loop must not crash/exit."""
        import backend.core.ouroboros.governance.cooperative_fs_io as fsio

        async def _boom_offload(fn, *a, **k):
            return fsio.OffloadError(
                fn_name="glob", exc_type="OSError",
                message="simulated", cpu_bound=False,
            )

        with patch(
            "backend.core.ouroboros.governance.cooperative_fs_io.offload",
            side_effect=_boom_offload,
        ):
            await consumer.start()
            await asyncio.sleep(0.05)

            pending = inbox_dir / "pending"
            event_data = _make_event_dict(event_id="unseen_evt")
            (pending / "unseen_evt.json").write_text(json.dumps(event_data))

            await asyncio.sleep(0.3)
            assert consumer._running is True  # loop survived the fault
            await consumer.stop()

        # Degraded — nothing was ever seen because the glob always
        # failed soft to an empty list.
        mock_event_bus.emit.assert_not_awaited()
