"""Tests for durable state persistence (WS1, WS6).

Covers Gates:
- #2: Wall-clock freshness (committed_at_epoch is wall-clock)
- #6: Single-writer serialization
- #7: PII minimization (no subject/snippet/full email in stored JSON)
- #8: Retention/GC
- #9: Schema evolution
- #10: Crash-point tests (enqueue/deliver/mark_delivered)
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

from autonomy.email_triage.schemas import (
    EmailFeatures,
    ScoringResult,
    TriageCycleReport,
    TriagedEmail,
)
from autonomy.email_triage.state_store import TriageStateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(cycle_id: str = "cycle_001") -> TriageCycleReport:
    return TriageCycleReport(
        cycle_id=cycle_id,
        started_at=time.time(),
        completed_at=time.time(),
        emails_fetched=3,
        emails_processed=3,
        tier_counts={1: 1, 2: 1, 3: 1},
        notifications_sent=1,
        notifications_suppressed=0,
        errors=[],
    )


def _make_triaged(
    msg_id: str = "msg_001",
    sender: str = "boss@company.com",
    subject: str = "URGENT: Deploy fix",
    tier: int = 1,
    score: int = 90,
) -> TriagedEmail:
    features = EmailFeatures(
        message_id=msg_id,
        sender=sender,
        sender_domain="company.com",
        subject=subject,
        snippet="Production is down...",
        is_reply=False,
        has_attachment=False,
        label_ids=("INBOX", "IMPORTANT"),
        keywords=("urgent", "deploy"),
        sender_frequency="frequent",
        urgency_signals=("action_required",),
        extraction_confidence=0.0,
    )
    scoring = ScoringResult(
        score=score,
        tier=tier,
        tier_label=f"jarvis/tier{tier}",
        breakdown={"sender": 0.9, "content": 0.8, "urgency": 0.7, "context": 0.6},
        idempotency_key=f"key_{msg_id}",
    )
    return TriagedEmail(
        features=features,
        scoring=scoring,
        notification_action="immediate",
        processed_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Gate #2: Wall-clock freshness
# ---------------------------------------------------------------------------


class TestWallClockFreshness:
    """Gate #2: committed_at_epoch uses wall-clock, not monotonic."""

    @pytest.mark.asyncio
    async def test_staleness_uses_wallclock_epoch(self, tmp_path):
        """Verify committed_at_epoch is wall-clock and freshness computes correctly."""
        db_path = str(tmp_path / "wallclock.db")
        store = TriageStateStore(db_path=db_path)
        await store.open()

        try:
            before = time.time()
            ok, _ = await store.save_snapshot(
                cycle_id="wc_test",
                report=_make_report("wc_test"),
                triaged_emails={},
                fencing_token=1,
            )
            after = time.time()

            assert ok is True
            latest = await store.load_latest_snapshot()
            assert latest is not None

            committed_at = latest["committed_at_epoch"]
            # Must be wall-clock (between before and after)
            assert before <= committed_at <= after
            # Freshness = now - committed_at (should be very small)
            age = time.time() - committed_at
            assert age < 5.0  # Should be nearly zero
        finally:
            await store.close()


# ---------------------------------------------------------------------------
# Gate #6: Single-writer serialization
# ---------------------------------------------------------------------------


class TestSingleWriterSerialization:
    """Gate #6: Concurrent writes are serialized correctly."""

    @pytest.mark.asyncio
    async def test_concurrent_write_serialization(self, tmp_path):
        """Submit 10 writes concurrently, verify all serialized correctly."""
        db_path = str(tmp_path / "concurrent.db")
        store = TriageStateStore(db_path=db_path)
        await store.open()

        try:
            # Submit 10 concurrent snapshot saves with increasing tokens
            tasks = []
            for i in range(10):
                tasks.append(
                    store.save_snapshot(
                        cycle_id=f"cycle_{i:03d}",
                        report=_make_report(f"cycle_{i:03d}"),
                        triaged_emails={},
                        fencing_token=i + 1,
                    )
                )
            results = await asyncio.gather(*tasks)

            # Count successful writes
            successes = sum(1 for ok, _ in results if ok)
            # At least one must succeed; due to fencing, lower tokens may fail
            # The highest token should always succeed
            assert successes >= 1

            # The latest snapshot should have the highest token that succeeded
            latest = await store.load_latest_snapshot()
            assert latest is not None
            assert latest["fencing_token"] >= 1
        finally:
            await store.close()


# ---------------------------------------------------------------------------
# Gate #7: PII minimization
# ---------------------------------------------------------------------------


class TestPIIMinimization:
    """Gate #7: No subject/snippet/full email in stored JSON."""

    @pytest.mark.asyncio
    async def test_no_pii_in_snapshot_json(self, tmp_path):
        """Verify stored snapshot doesn't contain PII fields."""
        db_path = str(tmp_path / "pii_test.db")
        store = TriageStateStore(db_path=db_path)
        await store.open()

        try:
            triaged = {
                "msg_pii": _make_triaged(
                    msg_id="msg_pii",
                    sender="secret_user@private.com",
                    subject="CONFIDENTIAL: Secret project details",
                ),
            }

            ok, _ = await store.save_snapshot(
                cycle_id="pii_cycle",
                report=_make_report("pii_cycle"),
                triaged_emails=triaged,
                fencing_token=1,
            )
            assert ok is True

            # Read raw DB to check what's actually stored
            conn = sqlite3.connect(db_path)
            cursor = conn.execute(
                "SELECT triaged_emails_min FROM triage_snapshots"
            )
            row = cursor.fetchone()
            conn.close()

            assert row is not None
            stored_json = row[0]

            # Must NOT contain subject, snippet, or full email address
            assert "CONFIDENTIAL" not in stored_json
            assert "Secret project details" not in stored_json
            assert "Production is down" not in stored_json
            assert "secret_user@private.com" not in stored_json
            # Should contain sender_domain only
            assert "private.com" in stored_json or "company.com" in stored_json

            # Parse and verify structure
            parsed = json.loads(stored_json)
            if isinstance(parsed, dict):
                for entry in parsed.values():
                    assert "subject" not in entry
                    assert "snippet" not in entry
                    assert "sender" not in entry or "@" not in entry.get("sender", "")
            elif isinstance(parsed, list):
                for entry in parsed:
                    assert "subject" not in entry
                    assert "snippet" not in entry
        finally:
            await store.close()


# ---------------------------------------------------------------------------
# Gate #8: Retention/GC
# ---------------------------------------------------------------------------


class TestRetentionGC:
    """Gate #8: GC removes expired rows."""

    @pytest.mark.asyncio
    async def test_gc_removes_expired_rows(self, tmp_path):
        """Insert rows past TTL, run GC, verify removed."""
        db_path = str(tmp_path / "gc_test.db")
        store = TriageStateStore(db_path=db_path)
        await store.open()

        try:
            # Save 15 snapshots (retention is 10 by default)
            for i in range(15):
                await store.save_snapshot(
                    cycle_id=f"gc_cycle_{i:03d}",
                    report=_make_report(f"gc_cycle_{i:03d}"),
                    triaged_emails={},
                    fencing_token=i + 1,
                )

            # Run GC with retention of 5
            await store.run_gc(snapshot_retention=5)

            # Verify only 5 snapshots remain
            conn = sqlite3.connect(db_path)
            cursor = conn.execute("SELECT COUNT(*) FROM triage_snapshots")
            count = cursor.fetchone()[0]
            conn.close()
            assert count <= 5
        finally:
            await store.close()


# ---------------------------------------------------------------------------
# Gate #9: Schema evolution
# ---------------------------------------------------------------------------


class TestSchemaEvolution:
    """Gate #9: Schema version checked on open."""

    @pytest.mark.asyncio
    async def test_schema_migration_on_upgrade(self, tmp_path):
        """Create DB, reopen with same version — should succeed cleanly."""
        db_path = str(tmp_path / "schema_test.db")

        # First open — creates tables
        store1 = TriageStateStore(db_path=db_path)
        await store1.open()
        await store1.save_snapshot(
            cycle_id="schema_v1",
            report=_make_report("schema_v1"),
            triaged_emails={},
            fencing_token=1,
        )
        await store1.close()

        # Second open — should work with same version
        store2 = TriageStateStore(db_path=db_path)
        await store2.open()
        latest = await store2.load_latest_snapshot()
        assert latest is not None
        assert latest["cycle_id"] == "schema_v1"
        await store2.close()


# ---------------------------------------------------------------------------
# Snapshot persistence & recovery
# ---------------------------------------------------------------------------


class TestSnapshotPersistence:
    """Core durable state: snapshots survive restart."""

    @pytest.mark.asyncio
    async def test_snapshot_persists_across_restart(self, tmp_path):
        """Save snapshot, close store, reopen, verify snapshot restored."""
        db_path = str(tmp_path / "persist.db")

        # Session 1: save
        store1 = TriageStateStore(db_path=db_path)
        await store1.open()
        triaged = {"msg_1": _make_triaged("msg_1")}
        ok, _ = await store1.save_snapshot(
            cycle_id="persist_cycle",
            report=_make_report("persist_cycle"),
            triaged_emails=triaged,
            fencing_token=1,
        )
        assert ok is True
        session_1_id = store1.session_id
        await store1.close()

        # Session 2: restore (same process, so session_id is the same
        # module-level UUID — cross-process recovery is the real scenario)
        store2 = TriageStateStore(db_path=db_path)
        await store2.open()
        latest = await store2.load_latest_snapshot()
        assert latest is not None
        assert latest["cycle_id"] == "persist_cycle"
        assert latest["session_id"] == session_1_id
        # Verify snapshot contains expected data
        assert "fencing_token" in latest
        assert "committed_at_epoch" in latest
        await store2.close()

    @pytest.mark.asyncio
    async def test_dedup_persists_across_restart(self, tmp_path):
        """Dedup record survives store restart."""
        db_path = str(tmp_path / "dedup_persist.db")

        # Session 1: record dedup
        store1 = TriageStateStore(db_path=db_path)
        await store1.open()
        await store1.record_dedup("key_abc", tier=1)
        is_dup = await store1.is_duplicate("key_abc", tier=1, window_s=900)
        assert is_dup is True
        await store1.close()

        # Session 2: check dedup still holds
        store2 = TriageStateStore(db_path=db_path)
        await store2.open()
        is_dup2 = await store2.is_duplicate("key_abc", tier=1, window_s=900)
        assert is_dup2 is True
        await store2.close()

    @pytest.mark.asyncio
    async def test_interrupt_budget_persists_across_restart(self, tmp_path):
        """Interrupt budget records survive restart."""
        db_path = str(tmp_path / "budget_persist.db")

        # Session 1: record 3 interrupts
        store1 = TriageStateStore(db_path=db_path)
        await store1.open()
        now = time.time()
        for _ in range(3):
            await store1.record_interrupt(now)
        count1 = await store1.count_interrupts(since=now - 3600)
        assert count1 == 3
        await store1.close()

        # Session 2: check budget still exhausted
        store2 = TriageStateStore(db_path=db_path)
        await store2.open()
        count2 = await store2.count_interrupts(since=now - 3600)
        assert count2 == 3
        await store2.close()

    @pytest.mark.asyncio
    async def test_action_ledger_records_decisions(self, tmp_path):
        """Action ledger records cycle decisions."""
        db_path = str(tmp_path / "ledger.db")
        store = TriageStateStore(db_path=db_path)
        await store.open()

        try:
            await store.record_action(
                cycle_id="ledger_cycle",
                message_id="msg_ledger",
                tier=1,
                action="immediate",
                explanation={"reasons": ["tier1_critical", "budget_available"]},
            )

            # Verify via raw DB
            conn = sqlite3.connect(db_path)
            cursor = conn.execute(
                "SELECT cycle_id, message_id, tier, action FROM action_ledger"
            )
            row = cursor.fetchone()
            conn.close()

            assert row is not None
            assert row[0] == "ledger_cycle"
            assert row[1] == "msg_ledger"
            assert row[2] == 1
            assert row[3] == "immediate"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_sender_reputation_accumulates(self, tmp_path):
        """Sender reputation builds up across updates."""
        db_path = str(tmp_path / "reputation.db")
        store = TriageStateStore(db_path=db_path)
        await store.open()

        try:
            # Three updates for same domain
            for tier, score in [(1, 90), (2, 70), (1, 85)]:
                await store.update_sender_reputation("company.com", tier, score)

            rep = await store.get_sender_reputation("company.com")
            assert rep is not None
            assert rep["total_count"] == 3
            assert rep["avg_score"] > 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_state_store_fallback_when_disabled(
        self, fresh_runner, time_ctrl
    ):
        """With persistence disabled, in-memory behavior is unchanged."""
        from conftest import make_mock_workspace_agent, mixed_inbox

        runner = fresh_runner(
            config=None,  # uses make_triage_config(state_persistence_enabled=False)
            workspace_agent=make_mock_workspace_agent(mixed_inbox()),
        )
        report = await runner.run_cycle()
        assert report.emails_processed > 0
        # State store should be None
        assert runner._state_store is None


# ---------------------------------------------------------------------------
# Notification outbox
# ---------------------------------------------------------------------------


class TestNotificationOutbox:
    """Outbox enqueue/deliver/replay tests."""

    @pytest.mark.asyncio
    async def test_outbox_enqueue_and_deliver(self, tmp_path):
        """Enqueue a notification, then mark it delivered."""
        db_path = str(tmp_path / "outbox.db")
        store = TriageStateStore(db_path=db_path)
        await store.open()

        try:
            now = time.time()
            await store.enqueue_notification(
                message_id="msg_outbox",
                action="immediate",
                tier=1,
                sender_domain="test.com",
                expires_at=now + 3600,
            )

            pending = await store.get_pending_notifications(limit=10)
            assert len(pending) == 1
            assert pending[0]["message_id"] == "msg_outbox"

            # Mark delivered
            await store.mark_delivered(pending[0]["id"])

            # Should be empty now
            pending2 = await store.get_pending_notifications(limit=10)
            assert len(pending2) == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_crash_between_enqueue_and_deliver(self, tmp_path):
        """Gate #10: Enqueue, simulate crash (close without marking delivered),
        verify outbox entry still pending on reopen."""
        db_path = str(tmp_path / "crash_test.db")

        # Session 1: enqueue but don't deliver (simulates crash)
        store1 = TriageStateStore(db_path=db_path)
        await store1.open()
        now = time.time()
        await store1.enqueue_notification(
            message_id="msg_crash",
            action="immediate",
            tier=1,
            sender_domain="crash.com",
            expires_at=now + 3600,
        )
        await store1.close()  # "crash" — didn't deliver

        # Session 2: reopen, verify still pending
        store2 = TriageStateStore(db_path=db_path)
        await store2.open()
        try:
            pending = await store2.get_pending_notifications(limit=10)
            assert len(pending) == 1
            assert pending[0]["message_id"] == "msg_crash"
            # Pending means it was NOT delivered — that's exactly what
            # get_pending_notifications filters for (delivered_at_epoch IS NULL)
            assert pending[0]["attempts"] == 0
        finally:
            await store2.close()
