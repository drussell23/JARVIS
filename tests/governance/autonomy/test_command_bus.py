"""tests/governance/autonomy/test_command_bus.py

TDD tests for CommandBus (Task 2: Priority Queue with Dedup).

Covers:
- put and get single command
- priority ordering: safety before optimization
- duplicate idempotency key dropped
- expired command dropped on get
- backpressure when full (try_put returns False)
- qsize reflects pending
"""
from __future__ import annotations

import asyncio
import time

import pytest


def _make_cmd(
    command_type=None,
    payload=None,
    ttl_s: float = 30.0,
    source_layer: str = "L2",
    target_layer: str = "L1",
    idempotency_key: str = "",
):
    """Helper to build CommandEnvelope with minimal boilerplate."""
    from backend.core.ouroboros.governance.autonomy.autonomy_types import (
        CommandEnvelope,
        CommandType,
    )

    return CommandEnvelope(
        source_layer=source_layer,
        target_layer=target_layer,
        command_type=command_type or CommandType.GENERATE_BACKLOG_ENTRY,
        payload=payload or {},
        ttl_s=ttl_s,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# put and get single command
# ---------------------------------------------------------------------------


class TestPutGetSingle:
    @pytest.mark.asyncio
    async def test_put_and_get_single_command(self):
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)
        cmd = _make_cmd()
        result = await bus.put(cmd)
        assert result is True

        got = await bus.get()
        assert got is cmd

    @pytest.mark.asyncio
    async def test_get_blocks_until_put(self):
        """get() should block when empty and unblock when a command arrives."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)
        cmd = _make_cmd()

        async def delayed_put():
            await asyncio.sleep(0.05)
            await bus.put(cmd)

        task = asyncio.create_task(delayed_put())
        got = await asyncio.wait_for(bus.get(), timeout=2.0)
        assert got is cmd
        await task


# ---------------------------------------------------------------------------
# priority ordering
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    @pytest.mark.asyncio
    async def test_safety_before_optimization(self):
        """Safety commands (priority 0) should dequeue before optimization (priority 3)."""
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandType,
        )
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)

        # Put optimization first, then safety
        opt_cmd = _make_cmd(
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload={"tag": "opt"},
        )
        safety_cmd = _make_cmd(
            command_type=CommandType.REPORT_ROLLBACK_CAUSE,
            payload={"tag": "safety"},
            source_layer="L3",
        )

        await bus.put(opt_cmd)
        await bus.put(safety_cmd)

        # Safety should come out first despite being put second
        first = await bus.get()
        second = await bus.get()
        assert first.priority <= second.priority
        assert first.payload.get("tag") == "safety"
        assert second.payload.get("tag") == "opt"

    @pytest.mark.asyncio
    async def test_fifo_within_same_priority(self):
        """Commands at the same priority level should dequeue in FIFO order."""
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandType,
        )
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)

        cmd_a = _make_cmd(
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload={"order": "first"},
        )
        cmd_b = _make_cmd(
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload={"order": "second"},
        )

        await bus.put(cmd_a)
        await bus.put(cmd_b)

        first = await bus.get()
        second = await bus.get()
        assert first.payload["order"] == "first"
        assert second.payload["order"] == "second"

    @pytest.mark.asyncio
    async def test_three_tier_priority_ordering(self):
        """Verify ordering across three priority levels: safety > operational > learning."""
        from backend.core.ouroboros.governance.autonomy.autonomy_types import (
            CommandType,
        )
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)

        learning_cmd = _make_cmd(
            command_type=CommandType.GENERATE_BACKLOG_ENTRY,
            payload={"tier": "learning"},
        )
        operational_cmd = _make_cmd(
            command_type=CommandType.REQUEST_MODE_SWITCH,
            payload={"tier": "operational"},
        )
        safety_cmd = _make_cmd(
            command_type=CommandType.REPORT_ROLLBACK_CAUSE,
            payload={"tier": "safety"},
            source_layer="L3",
        )

        # Insert in reverse priority order
        await bus.put(learning_cmd)
        await bus.put(operational_cmd)
        await bus.put(safety_cmd)

        first = await bus.get()
        second = await bus.get()
        third = await bus.get()
        assert first.payload["tier"] == "safety"
        assert second.payload["tier"] == "operational"
        assert third.payload["tier"] == "learning"


# ---------------------------------------------------------------------------
# duplicate idempotency key dropped
# ---------------------------------------------------------------------------


class TestDeduplication:
    @pytest.mark.asyncio
    async def test_duplicate_idempotency_key_dropped(self):
        """put() returns False and does not enqueue a duplicate command."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)
        cmd = _make_cmd(payload={"unique": "value"})

        assert await bus.put(cmd) is True
        assert bus.qsize() == 1

        # Same idempotency key (identical envelope fields)
        dup = _make_cmd(payload={"unique": "value"})
        assert cmd.idempotency_key == dup.idempotency_key
        assert await bus.put(dup) is False
        assert bus.qsize() == 1

    @pytest.mark.asyncio
    async def test_different_payload_not_duplicate(self):
        """Commands with different payloads should both be accepted."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)

        cmd_a = _make_cmd(payload={"val": "alpha"})
        cmd_b = _make_cmd(payload={"val": "beta"})

        assert await bus.put(cmd_a) is True
        assert await bus.put(cmd_b) is True
        assert bus.qsize() == 2

    @pytest.mark.asyncio
    async def test_explicit_idempotency_key_dedup(self):
        """Commands with explicit matching idempotency keys are detected as duplicates."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)

        cmd_a = _make_cmd(payload={"a": 1}, idempotency_key="custom-key-123")
        cmd_b = _make_cmd(payload={"b": 2}, idempotency_key="custom-key-123")

        assert await bus.put(cmd_a) is True
        assert await bus.put(cmd_b) is False
        assert bus.qsize() == 1


# ---------------------------------------------------------------------------
# expired command dropped on get
# ---------------------------------------------------------------------------


class TestExpiredCommandDropped:
    @pytest.mark.asyncio
    async def test_expired_command_silently_skipped(self):
        """get() should skip expired commands and return the next valid one."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)

        # Expired command (TTL=0 means immediate expiry)
        expired_cmd = _make_cmd(ttl_s=0.0, payload={"status": "expired"})
        # Small sleep to ensure monotonic time advances
        await asyncio.sleep(0.01)

        # Valid command
        valid_cmd = _make_cmd(ttl_s=30.0, payload={"status": "valid"})

        await bus.put(expired_cmd)
        await bus.put(valid_cmd)

        # Should skip expired and return valid
        got = await bus.get()
        assert got.payload["status"] == "valid"

    @pytest.mark.asyncio
    async def test_all_expired_blocks_until_valid(self):
        """If all enqueued commands are expired, get() should block until a valid one arrives."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)

        expired_cmd = _make_cmd(ttl_s=0.0, payload={"status": "expired"})
        await asyncio.sleep(0.01)
        await bus.put(expired_cmd)

        valid_cmd = _make_cmd(ttl_s=30.0, payload={"status": "valid"})

        async def delayed_put():
            await asyncio.sleep(0.05)
            await bus.put(valid_cmd)

        task = asyncio.create_task(delayed_put())
        got = await asyncio.wait_for(bus.get(), timeout=2.0)
        assert got.payload["status"] == "valid"
        await task


# ---------------------------------------------------------------------------
# backpressure when full
# ---------------------------------------------------------------------------


class TestBackpressure:
    @pytest.mark.asyncio
    async def test_try_put_returns_false_when_full(self):
        """try_put() returns False when the bus is at capacity."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=2)

        cmd_a = _make_cmd(payload={"id": "a"})
        cmd_b = _make_cmd(payload={"id": "b"})
        cmd_c = _make_cmd(payload={"id": "c"})

        assert bus.try_put(cmd_a) is True
        assert bus.try_put(cmd_b) is True
        # Bus is full
        assert bus.try_put(cmd_c) is False

    @pytest.mark.asyncio
    async def test_async_put_returns_false_when_full(self):
        """async put() returns False when full (non-blocking, does not wait)."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=2)

        cmd_a = _make_cmd(payload={"id": "a"})
        cmd_b = _make_cmd(payload={"id": "b"})
        cmd_c = _make_cmd(payload={"id": "c"})

        assert await bus.put(cmd_a) is True
        assert await bus.put(cmd_b) is True
        assert await bus.put(cmd_c) is False

    @pytest.mark.asyncio
    async def test_put_succeeds_after_get_frees_space(self):
        """After consuming a command, space is freed for a new put."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=1)

        cmd_a = _make_cmd(payload={"id": "a"})
        cmd_b = _make_cmd(payload={"id": "b"})

        assert await bus.put(cmd_a) is True
        assert await bus.put(cmd_b) is False  # full

        await bus.get()  # free space
        assert await bus.put(cmd_b) is True

    @pytest.mark.asyncio
    async def test_try_put_dedup_takes_precedence_over_backpressure(self):
        """Duplicate check should happen before capacity check."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=2)

        cmd = _make_cmd(payload={"id": "x"})
        assert bus.try_put(cmd) is True

        # Same idempotency key — should return False for duplicate, not backpressure
        dup = _make_cmd(payload={"id": "x"})
        assert bus.try_put(dup) is False
        # Verify only 1 item in queue
        assert bus.qsize() == 1


# ---------------------------------------------------------------------------
# qsize reflects pending
# ---------------------------------------------------------------------------


class TestQsize:
    @pytest.mark.asyncio
    async def test_qsize_starts_at_zero(self):
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)
        assert bus.qsize() == 0

    @pytest.mark.asyncio
    async def test_qsize_increments_on_put(self):
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)
        await bus.put(_make_cmd(payload={"i": 1}))
        assert bus.qsize() == 1
        await bus.put(_make_cmd(payload={"i": 2}))
        assert bus.qsize() == 2

    @pytest.mark.asyncio
    async def test_qsize_decrements_on_get(self):
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)
        await bus.put(_make_cmd(payload={"i": 1}))
        await bus.put(_make_cmd(payload={"i": 2}))
        assert bus.qsize() == 2

        await bus.get()
        assert bus.qsize() == 1

        await bus.get()
        assert bus.qsize() == 0

    @pytest.mark.asyncio
    async def test_qsize_not_affected_by_duplicate(self):
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)
        cmd = _make_cmd(payload={"val": "same"})
        await bus.put(cmd)
        assert bus.qsize() == 1

        dup = _make_cmd(payload={"val": "same"})
        await bus.put(dup)
        assert bus.qsize() == 1  # unchanged

    @pytest.mark.asyncio
    async def test_qsize_counts_expired_until_dequeued(self):
        """Expired commands still count in qsize until they are actually dequeued and discarded."""
        from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

        bus = CommandBus(maxsize=16)
        expired = _make_cmd(ttl_s=0.0, payload={"status": "expired"})
        await asyncio.sleep(0.01)
        await bus.put(expired)
        # Expired command is still in the heap
        assert bus.qsize() == 1

        valid = _make_cmd(ttl_s=30.0, payload={"status": "valid"})
        await bus.put(valid)
        assert bus.qsize() == 2

        # get() should discard expired and return valid
        got = await bus.get()
        assert got.payload["status"] == "valid"
        # Expired was discarded during get, valid was returned
        assert bus.qsize() == 0
