# tests/unit/core/test_budget_reservation.py
"""Tests for atomic budget reservation protocol."""
import asyncio
import os
import pytest
from backend.core.orchestration_journal import OrchestrationJournal


@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease(f"test:{os.getpid()}")
    yield j
    await j.close()


class TestBudgetReservation:
    @pytest.mark.asyncio
    async def test_reserve_within_budget(self, journal):
        ok = journal.reserve_budget(0.30, "op_1", daily_budget=1.00)
        assert ok

    @pytest.mark.asyncio
    async def test_reserve_exceeding_budget(self, journal):
        journal.reserve_budget(0.80, "op_1", daily_budget=1.00)
        ok = journal.reserve_budget(0.30, "op_2", daily_budget=1.00)
        assert not ok

    @pytest.mark.asyncio
    async def test_commit_budget(self, journal):
        journal.reserve_budget(0.30, "op_1", daily_budget=1.00)
        journal.commit_budget("op_1", actual_cost=0.25)
        entries = await journal.replay_from(0)
        commits = [e for e in entries if e["action"] == "budget_committed"]
        assert len(commits) == 1

    @pytest.mark.asyncio
    async def test_release_budget_frees_capacity(self, journal):
        journal.reserve_budget(0.80, "op_1", daily_budget=1.00)
        journal.release_budget("op_1")
        ok = journal.reserve_budget(0.80, "op_2", daily_budget=1.00)
        assert ok

    @pytest.mark.asyncio
    async def test_idempotent_reserve(self, journal):
        seq1 = journal.reserve_budget(0.30, "op_1", daily_budget=1.00)
        seq2 = journal.reserve_budget(0.30, "op_1", daily_budget=1.00)
        assert seq1 == seq2  # Same op_id -> idempotent

    @pytest.mark.asyncio
    async def test_concurrent_reservations_serialized(self, journal):
        """Two concurrent reserve calls cannot both succeed if total exceeds budget."""
        results = []

        async def reserve(op_id):
            ok = journal.reserve_budget(0.60, op_id, daily_budget=1.00)
            results.append((op_id, ok))

        await asyncio.gather(reserve("op_a"), reserve("op_b"))
        approved = [r for r in results if r[1]]
        assert len(approved) == 1, f"Expected 1 approval, got {len(approved)}: {results}"

    @pytest.mark.asyncio
    async def test_calculate_available_budget(self, journal):
        journal.reserve_budget(0.30, "op_1", daily_budget=1.00)
        journal.commit_budget("op_1", actual_cost=0.25)
        journal.reserve_budget(0.20, "op_2", daily_budget=1.00)
        available = journal.calculate_available_budget(daily_budget=1.00)
        # Committed: $0.25 + reserved (uncommitted): $0.20 = $0.45 used
        assert 0.54 <= available <= 0.56
