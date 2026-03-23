"""Tests for LifecycleVoiceNarrator."""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from backend.core.supervisor.lifecycle_narrator import (
    LifecycleVoiceNarrator,
    NarrationPriority,
    NarrationItem,
    _time_greeting,
)


class TestNarrationPriority:
    def test_ordering(self):
        assert NarrationPriority.LOW.value < NarrationPriority.NORMAL.value
        assert NarrationPriority.NORMAL.value < NarrationPriority.HIGH.value
        assert NarrationPriority.HIGH.value < NarrationPriority.CRITICAL.value


class TestNarrationItem:
    def test_frozen(self):
        item = NarrationItem(text="hello", priority=NarrationPriority.NORMAL)
        with pytest.raises(AttributeError):
            item.text = "modified"


class TestTimeGreeting:
    def test_returns_string_with_name(self):
        greeting = _time_greeting("Derek")
        assert "Derek" in greeting
        assert isinstance(greeting, str)
        assert len(greeting) > 5


class TestLifecycleVoiceNarrator:
    def test_disabled_narrator_does_nothing(self):
        n = LifecycleVoiceNarrator(enabled=False)
        n.enqueue("hello", NarrationPriority.HIGH)
        assert n._queue.qsize() == 0

    def test_enqueue_adds_to_queue(self):
        n = LifecycleVoiceNarrator(enabled=True)
        n.enqueue("test message", NarrationPriority.NORMAL, category="test")
        assert n._queue.qsize() == 1

    def test_enqueue_dedup(self):
        n = LifecycleVoiceNarrator(enabled=True)
        n._recent_texts.append("duplicate")
        n.enqueue("duplicate", NarrationPriority.NORMAL)
        assert n._queue.qsize() == 0

    def test_enqueue_empty_string_ignored(self):
        n = LifecycleVoiceNarrator(enabled=True)
        n.enqueue("", NarrationPriority.HIGH)
        assert n._queue.qsize() == 0

    def test_narrate_zone_known(self):
        n = LifecycleVoiceNarrator(enabled=True)
        n.narrate_zone("backend")
        assert n._queue.qsize() == 1

    def test_narrate_zone_unknown(self):
        n = LifecycleVoiceNarrator(enabled=True)
        n.narrate_zone("nonexistent_zone")
        assert n._queue.qsize() == 0

    def test_narrate_startup_complete_once(self):
        n = LifecycleVoiceNarrator(enabled=True)
        n.narrate_startup_complete(15.0)
        assert n._queue.qsize() == 1
        n.narrate_startup_complete(15.0)
        assert n._queue.qsize() == 1  # not doubled

    def test_health_snapshot(self):
        n = LifecycleVoiceNarrator(enabled=True)
        h = n.health()
        assert "enabled" in h
        assert "running" in h
        assert "queue_depth" in h
        assert h["enabled"] is True
        assert h["running"] is False

    def test_add_hook(self):
        n = LifecycleVoiceNarrator(enabled=True)
        calls = []
        n.add_hook(lambda text: calls.append(text))
        assert len(n._on_narrate_hooks) == 1

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self):
        n = LifecycleVoiceNarrator(enabled=True)
        with patch("backend.core.supervisor.lifecycle_narrator.get_lifecycle_narrator", return_value=n):
            await n.start()
            assert n._task is not None
            assert not n._task.done()
            await n.stop()
            assert n._task is None

    @pytest.mark.asyncio
    async def test_envelope_lifecycle_transition_ignored(self):
        """v305.0: Routine lifecycle transitions are no longer narrated
        (they caused false-positive announcements). Only fault.raised
        and task.completed events are narrated now."""
        n = LifecycleVoiceNarrator(enabled=True)

        class FakeEnvelope:
            event_schema = "lifecycle.transition@1.0.0"
            payload = {"from_state": "PROBING", "to_state": "READY"}

        await n._on_envelope(FakeEnvelope())
        assert n._queue.qsize() == 0  # no longer narrated

    @pytest.mark.asyncio
    async def test_envelope_task_completed(self):
        """v305.0: task.completed events ARE narrated."""
        n = LifecycleVoiceNarrator(enabled=True)

        class FakeEnvelope:
            event_schema = "task.completed@1.0.0"
            payload = {"summary": "Done. Searched YouTube for NBA.", "success": True}

        await n._on_envelope(FakeEnvelope())
        assert n._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_envelope_fault_raised(self):
        n = LifecycleVoiceNarrator(enabled=True)

        class FakeEnvelope:
            event_schema = "fault.raised@1.0.0"
            payload = {"fault_class": "connection_refused"}

        await n._on_envelope(FakeEnvelope())
        assert n._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_envelope_unknown_schema_ignored(self):
        n = LifecycleVoiceNarrator(enabled=True)

        class FakeEnvelope:
            event_schema = "unknown.thing@1.0.0"
            payload = {}

        await n._on_envelope(FakeEnvelope())
        assert n._queue.qsize() == 0
