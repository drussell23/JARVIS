"""Crash recovery: WAL replay on router restart."""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    UnifiedIntakeRouter,
    IntakeRouterConfig,
)
from backend.core.ouroboros.governance.intake.wal import WAL, WALEntry


def _env():
    return make_envelope(
        source="backlog",
        description="fix auth",
        target_files=("backend/core/auth.py",),
        repo="jarvis",
        confidence=0.8,
        urgency="normal",
        evidence={"signature": "unique_sig_crash"},
        requires_human_ack=False,
    )


async def test_wal_pending_entries_replayed_on_router_start(tmp_path):
    """Pending WAL entries from a previous run are re-ingested on start."""
    gls = MagicMock()
    gls.submit = AsyncMock()

    wal_path = tmp_path / ".jarvis" / "intake_wal.jsonl"
    wal_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-populate WAL with a pending entry (simulates crash mid-dispatch)
    env = _env()
    lease_id = "pre_crash_lease_001"
    env = env.with_lease(lease_id)
    wal = WAL(wal_path)
    wal.append(WALEntry(
        lease_id=lease_id,
        envelope_dict=env.to_dict(),
        status="pending",
        ts_monotonic=time.monotonic(),
        ts_utc="2026-03-08T00:00:00Z",
    ))

    config = IntakeRouterConfig(project_root=tmp_path, wal_path=wal_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()
    # Allow dispatch loop to process replayed entry
    await asyncio.sleep(0.2)
    await router.stop()

    # GLS.submit was called (replayed entry dispatched)
    assert gls.submit.call_count >= 1


async def test_acked_entries_not_replayed(tmp_path):
    """Entries with status='acked' are not re-dispatched on restart."""
    gls = MagicMock()
    gls.submit = AsyncMock()

    wal_path = tmp_path / ".jarvis" / "intake_wal.jsonl"
    wal_path.parent.mkdir(parents=True, exist_ok=True)

    env = _env()
    lease_id = "already_acked_001"
    env = env.with_lease(lease_id)
    wal = WAL(wal_path)
    wal.append(WALEntry(
        lease_id=lease_id,
        envelope_dict=env.to_dict(),
        status="pending",
        ts_monotonic=time.monotonic(),
        ts_utc="2026-03-08T00:00:00Z",
    ))
    wal.update_status(lease_id, "acked")  # Mark as acked before "restart"

    config = IntakeRouterConfig(project_root=tmp_path, wal_path=wal_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()
    await asyncio.sleep(0.15)
    await router.stop()

    # Should NOT be dispatched again
    assert gls.submit.call_count == 0


async def test_idempotent_key_not_double_dispatched(tmp_path):
    """Two envelopes with same dedup_key: second is deduplicated."""
    gls = MagicMock()
    submit_count = 0

    async def counting_submit(ctx, trigger_source=""):
        nonlocal submit_count
        submit_count += 1

    gls.submit = counting_submit

    config = IntakeRouterConfig(
        project_root=tmp_path, dedup_window_s=60.0
    )
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    e1 = make_envelope(
        source="backlog", description="fix x",
        target_files=("a.py",), repo="jarvis",
        confidence=0.8, urgency="normal",
        evidence={"signature": "idem_test"},
        requires_human_ack=False,
    )
    e2 = make_envelope(
        source="backlog", description="fix x",
        target_files=("a.py",), repo="jarvis",
        confidence=0.8, urgency="normal",
        evidence={"signature": "idem_test"},
        requires_human_ack=False,
    )
    # Same dedup_key (same signature + source + files)
    assert e1.dedup_key == e2.dedup_key

    r1 = await router.ingest(e1)
    r2 = await router.ingest(e2)
    assert r1 == "enqueued"
    assert r2 == "deduplicated"

    await asyncio.sleep(0.15)
    await router.stop()
    assert submit_count == 1
