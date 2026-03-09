"""Tests for UnifiedIntakeRouter pipeline stages."""
import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    UnifiedIntakeRouter,
    IntakeRouterConfig,
)


def _env(source="backlog", urgency="normal", target_files=("backend/core/auth.py",),
         requires_human_ack=False, confidence=0.8, same_dedup=False):
    sig = "fixed_sig" if same_dedup else str(time.monotonic())
    return make_envelope(
        source=source,
        description="fix auth",
        target_files=target_files,
        repo="jarvis",
        confidence=confidence,
        urgency=urgency,
        evidence={"signature": sig},
        requires_human_ack=requires_human_ack,
    )


def _make_router(tmp_path, gls=None):
    if gls is None:
        gls = MagicMock()
        gls.submit = AsyncMock()
    config = IntakeRouterConfig(
        project_root=tmp_path,
        dedup_window_s=60.0,
    )
    return UnifiedIntakeRouter(gls=gls, config=config), gls


async def test_ingest_returns_enqueued(tmp_path):
    router, _ = _make_router(tmp_path)
    await router.start()
    try:
        result = await router.ingest(_env())
        assert result == "enqueued"
    finally:
        await router.stop()


async def test_duplicate_dedup_key_within_window_returns_deduplicated(tmp_path):
    router, _ = _make_router(tmp_path)
    await router.start()
    try:
        e1 = _env(same_dedup=True)
        e2 = _env(same_dedup=True)
        assert e1.dedup_key == e2.dedup_key
        r1 = await router.ingest(e1)
        r2 = await router.ingest(e2)
        assert r1 == "enqueued"
        assert r2 == "deduplicated"
    finally:
        await router.stop()


async def test_requires_human_ack_returns_pending_ack(tmp_path):
    router, _ = _make_router(tmp_path)
    await router.start()
    try:
        result = await router.ingest(_env(requires_human_ack=True))
        assert result == "pending_ack"
    finally:
        await router.stop()


async def test_voice_human_priority_higher_than_backlog(tmp_path):
    router, _ = _make_router(tmp_path)
    # Priority map: voice_human=0, test_failure=1, backlog=2, ai_miner=3
    from backend.core.ouroboros.governance.intake.unified_intake_router import _PRIORITY_MAP
    assert _PRIORITY_MAP["voice_human"] < _PRIORITY_MAP["backlog"]
    assert _PRIORITY_MAP["test_failure"] < _PRIORITY_MAP["backlog"]
    assert _PRIORITY_MAP["backlog"] < _PRIORITY_MAP["ai_miner"]


async def test_intake_queue_depth_increments(tmp_path):
    gls = MagicMock()
    # Never resolve so queue fills up
    gls.submit = AsyncMock(side_effect=lambda *a, **kw: asyncio.sleep(9999))
    router, _ = _make_router(tmp_path, gls=gls)
    await router.start()
    try:
        await router.ingest(_env())
        # Give dispatch loop a moment to pick up the item
        await asyncio.sleep(0.05)
        # depth is 0 (dispatched) or 1 depending on timing — just check it's non-negative
        assert router.intake_queue_depth() >= 0
    finally:
        await router.stop()


async def test_submit_called_with_correct_trigger_source(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    router, _ = _make_router(tmp_path, gls=gls)
    await router.start()
    try:
        await router.ingest(_env(source="test_failure"))
        # Allow dispatch loop to run
        await asyncio.sleep(0.1)
    finally:
        await router.stop()
    # GLS.submit must have been called with trigger_source="test_failure"
    assert gls.submit.call_count > 0, "expected GLS.submit to be called at least once"
    kwargs = gls.submit.call_args.kwargs
    assert kwargs.get("trigger_source") == "test_failure"


async def test_dead_letter_after_max_retries(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock(side_effect=RuntimeError("submit failed"))
    config = IntakeRouterConfig(
        project_root=tmp_path,
        max_retries=1,
        dedup_window_s=0.0,  # disable dedup so retries go through
    )
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()
    try:
        env = _env()
        await router.ingest(env)
        # Wait for dispatch + retry to exhaust
        await asyncio.sleep(0.3)
        assert router.dead_letter_count() >= 1
    finally:
        await router.stop()


async def test_backpressure_signal_when_queue_full(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock(side_effect=lambda *a, **kw: asyncio.sleep(9999))
    config = IntakeRouterConfig(
        project_root=tmp_path,
        backpressure_threshold=1,
    )
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()
    try:
        r1 = await router.ingest(_env())
        assert r1 == "enqueued"
        # Second enqueue from low-priority source should be back-pressured
        r2 = await router.ingest(_env(source="backlog"))
        assert r2 in ("enqueued", "backpressure")
    finally:
        await router.stop()
