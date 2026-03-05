"""Tests for the UMF SQLite WAL dedup ledger.

Covers reserve/commit/abort semantics, TTL expiration, concurrent
reservations, and basic get/compact operations.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from backend.core.umf.dedup_ledger import SqliteDedupLedger
from backend.core.umf.types import ReserveResult


@pytest_asyncio.fixture
async def ledger(tmp_path):
    """Create and start a ledger backed by a temp SQLite file."""
    db = tmp_path / "dedup.db"
    ldg = SqliteDedupLedger(db)
    await ldg.start()
    yield ldg
    await ldg.stop()


class TestDedupLedger:
    """Seven tests covering dedup ledger reserve/commit/abort semantics."""

    @pytest.mark.asyncio
    async def test_reserve_new_key_returns_reserved(self, ledger: SqliteDedupLedger):
        """Reserving a fresh idempotency key returns RESERVED."""
        result = await ledger.reserve("key-1", "msg-1", ttl_ms=30_000)
        assert result is ReserveResult.reserved

    @pytest.mark.asyncio
    async def test_duplicate_key_returns_duplicate(self, ledger: SqliteDedupLedger):
        """Reserving the same idempotency key twice returns DUPLICATE."""
        await ledger.reserve("key-dup", "msg-a", ttl_ms=30_000)
        result = await ledger.reserve("key-dup", "msg-b", ttl_ms=30_000)
        assert result is ReserveResult.duplicate

    @pytest.mark.asyncio
    async def test_commit_marks_entry(self, ledger: SqliteDedupLedger):
        """Committing a reserved entry sets committed=1 and records the effect hash."""
        await ledger.reserve("key-c", "msg-c", ttl_ms=30_000)
        await ledger.commit("msg-c", effect_hash="sha256:abc123")

        row = await ledger.get("msg-c")
        assert row is not None
        assert row["committed"] == 1
        assert row["effect_hash"] == "sha256:abc123"

    @pytest.mark.asyncio
    async def test_abort_allows_replay(self, ledger: SqliteDedupLedger):
        """Aborting a reserved entry allows the same idempotency key to be re-reserved."""
        await ledger.reserve("key-ab", "msg-ab", ttl_ms=30_000)
        await ledger.abort("msg-ab", reason="handler crashed")

        # Verify aborted
        row = await ledger.get("msg-ab")
        assert row is not None
        assert row["aborted"] == 1
        assert row["abort_reason"] == "handler crashed"

        # Re-reserve with a new message_id succeeds
        result = await ledger.reserve("key-ab", "msg-ab-2", ttl_ms=30_000)
        assert result is ReserveResult.reserved

    @pytest.mark.asyncio
    async def test_ttl_expiration_allows_reuse(self, ledger: SqliteDedupLedger):
        """After TTL expiry, compact removes the row and re-reserve succeeds."""
        await ledger.reserve("key-ttl", "msg-ttl", ttl_ms=1)
        await asyncio.sleep(0.010)  # 10 ms -- well past 1 ms TTL

        count = await ledger.compact()
        assert count >= 1

        result = await ledger.reserve("key-ttl", "msg-ttl-2", ttl_ms=30_000)
        assert result is ReserveResult.reserved

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, ledger: SqliteDedupLedger):
        """Getting a message_id that does not exist returns None."""
        row = await ledger.get("no-such-msg")
        assert row is None

    @pytest.mark.asyncio
    async def test_concurrent_reserves_one_winner(self, ledger: SqliteDedupLedger):
        """Three concurrent reserves on the same key yield exactly one RESERVED."""
        results = await asyncio.gather(
            ledger.reserve("key-race", "msg-r1", ttl_ms=30_000),
            ledger.reserve("key-race", "msg-r2", ttl_ms=30_000),
            ledger.reserve("key-race", "msg-r3", ttl_ms=30_000),
        )
        reserved_count = sum(1 for r in results if r is ReserveResult.reserved)
        assert reserved_count == 1, (
            f"Expected exactly 1 RESERVED, got {reserved_count}: {results}"
        )
