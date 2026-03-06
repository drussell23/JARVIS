"""Tests for UMF dead letter queue (Task 20)."""
import asyncio
import pytest


class TestDeadLetterQueue:

    @pytest.mark.asyncio
    async def test_add_and_retrieve(self, tmp_path):
        from backend.core.umf.dead_letter_queue import DeadLetterQueue
        dlq = DeadLetterQueue(storage_dir=tmp_path / "dlq")
        dlq.start()
        await dlq.add(message_id="msg-1", reason="handler_timeout",
                       payload={"test": True})
        entries = dlq.list_entries()
        assert len(entries) == 1
        assert entries[0]["message_id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_no_oscillation(self, tmp_path):
        from backend.core.umf.dead_letter_queue import DeadLetterQueue
        dlq = DeadLetterQueue(storage_dir=tmp_path / "dlq")
        dlq.start()
        await dlq.add(message_id="msg-1", reason="poison", payload={})
        await dlq.add(message_id="msg-1", reason="poison", payload={})
        entries = dlq.list_entries()
        assert len(entries) == 1  # same message_id only stored once

    @pytest.mark.asyncio
    async def test_cleanup_old_entries(self, tmp_path):
        from backend.core.umf.dead_letter_queue import DeadLetterQueue
        dlq = DeadLetterQueue(storage_dir=tmp_path / "dlq", max_age_s=0.01)
        dlq.start()
        await dlq.add(message_id="msg-1", reason="test", payload={})
        await asyncio.sleep(0.02)
        removed = dlq.cleanup()
        assert removed >= 1

    @pytest.mark.asyncio
    async def test_entry_contains_reason(self, tmp_path):
        from backend.core.umf.dead_letter_queue import DeadLetterQueue
        dlq = DeadLetterQueue(storage_dir=tmp_path / "dlq")
        dlq.start()
        await dlq.add(message_id="msg-2", reason="circuit_open", payload={"x": 1})
        entries = dlq.list_entries()
        assert entries[0]["reason"] == "circuit_open"
        assert entries[0]["payload"] == {"x": 1}
