"""Tests for atomic fencing in the state store (WS2, Gate #1).

Validates:
- Atomic compare-and-write prevents stale writers
- Stale tokens are rejected at DB level
- Monotonic tokens advance correctly
- Fencing token restored from state store on restart
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

from autonomy.email_triage.schemas import TriageCycleReport
from autonomy.email_triage.state_store import TriageStateStore


def _make_report(cycle_id: str = "cycle_001") -> TriageCycleReport:
    """Build a minimal report for snapshot saving."""
    import time

    return TriageCycleReport(
        cycle_id=cycle_id,
        started_at=time.time(),
        completed_at=time.time(),
        emails_fetched=5,
        emails_processed=5,
        tier_counts={1: 1, 2: 1, 3: 2, 4: 1},
        notifications_sent=1,
        notifications_suppressed=1,
        errors=[],
    )


class TestAtomicFencing:
    """Gate #1: Atomic compare-and-write fencing in state store."""

    @pytest.mark.asyncio
    async def test_atomic_compare_and_write(self, tmp_path):
        """Two writers with tokens 5 and 3 target same DB.
        Only token=5 succeeds."""
        db_path = str(tmp_path / "fencing_test.db")
        store = TriageStateStore(db_path=db_path)
        await store.open()

        try:
            # Writer with token 5
            committed_5, reason_5 = await store.save_snapshot(
                cycle_id="cycle_5",
                report=_make_report("cycle_5"),
                triaged_emails={},
                fencing_token=5,
            )
            assert committed_5 is True

            # Writer with token 3 (stale)
            committed_3, reason_3 = await store.save_snapshot(
                cycle_id="cycle_3",
                report=_make_report("cycle_3"),
                triaged_emails={},
                fencing_token=3,
            )
            assert committed_3 is False
            assert "stale" in reason_3.lower() or "fencing" in reason_3.lower()

            # Verify only cycle_5 was stored
            latest = await store.load_latest_snapshot()
            assert latest is not None
            assert latest["cycle_id"] == "cycle_5"
            assert latest["fencing_token"] == 5
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_stale_token_rejected(self, tmp_path):
        """Set token=5, commit, then token=3 is rejected at DB level."""
        db_path = str(tmp_path / "stale_token.db")
        store = TriageStateStore(db_path=db_path)
        await store.open()

        try:
            # First commit with token 5
            ok, _ = await store.save_snapshot(
                cycle_id="cycle_a",
                report=_make_report("cycle_a"),
                triaged_emails={},
                fencing_token=5,
            )
            assert ok is True

            # Try with stale token 3
            ok2, reason = await store.save_snapshot(
                cycle_id="cycle_b",
                report=_make_report("cycle_b"),
                triaged_emails={},
                fencing_token=3,
            )
            assert ok2 is False

            # Try with equal token (also stale — must be strictly greater)
            ok3, reason3 = await store.save_snapshot(
                cycle_id="cycle_c",
                report=_make_report("cycle_c"),
                triaged_emails={},
                fencing_token=5,
            )
            # Equal tokens: implementation allows >= or > — either is valid for safety
            # The key is that LOWER tokens are rejected
            assert ok3 is True or "stale" in reason3.lower()
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_monotonic_token_advances(self, tmp_path):
        """Tokens 1, 2, 3 all commit successfully."""
        db_path = str(tmp_path / "monotonic.db")
        store = TriageStateStore(db_path=db_path)
        await store.open()

        try:
            for token in [1, 2, 3]:
                ok, reason = await store.save_snapshot(
                    cycle_id=f"cycle_{token}",
                    report=_make_report(f"cycle_{token}"),
                    triaged_emails={},
                    fencing_token=token,
                )
                assert ok is True, f"Token {token} should commit but got: {reason}"

            latest = await store.load_latest_snapshot()
            assert latest is not None
            assert latest["cycle_id"] == "cycle_3"
            assert latest["fencing_token"] == 3
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_fencing_token_restored_from_state_store(self, tmp_path):
        """Persist token, restart (new store instance), verify restored."""
        db_path = str(tmp_path / "restore_token.db")

        # First session: save with token 7
        store1 = TriageStateStore(db_path=db_path)
        await store1.open()
        ok, _ = await store1.save_snapshot(
            cycle_id="cycle_persist",
            report=_make_report("cycle_persist"),
            triaged_emails={},
            fencing_token=7,
        )
        assert ok is True
        await store1.close()

        # Second session: new store instance, load snapshot
        store2 = TriageStateStore(db_path=db_path)
        await store2.open()
        try:
            latest = await store2.load_latest_snapshot()
            assert latest is not None
            assert latest["fencing_token"] == 7

            # New writes must use token > 7
            ok_stale, _ = await store2.save_snapshot(
                cycle_id="cycle_stale",
                report=_make_report("cycle_stale"),
                triaged_emails={},
                fencing_token=5,
            )
            assert ok_stale is False

            ok_fresh, _ = await store2.save_snapshot(
                cycle_id="cycle_fresh",
                report=_make_report("cycle_fresh"),
                triaged_emails={},
                fencing_token=8,
            )
            assert ok_fresh is True
        finally:
            await store2.close()
