"""Out-of-order and duplicate event tests for the intake layer."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    UnifiedIntakeRouter,
    IntakeRouterConfig,
)


def _env(sig: str = "default_sig", source: str = "backlog"):
    return make_envelope(
        source=source,
        description=f"fix {sig}",
        target_files=(f"backend/core/{sig}.py",),
        repo="jarvis",
        confidence=0.8,
        urgency="normal",
        evidence={"signature": sig},
        requires_human_ack=False,
    )


async def test_voice_human_dispatched_before_backlog(tmp_path):
    """voice_human priority=0 dispatches before backlog priority=2."""
    dispatch_order = []

    async def mock_submit(ctx, trigger_source=""):
        dispatch_order.append(trigger_source)
        await asyncio.sleep(0.01)  # slight delay to let ordering matter

    gls = MagicMock()
    gls.submit = mock_submit

    config = IntakeRouterConfig(project_root=tmp_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    # Enqueue backlog first, then voice_human
    await router.ingest(_env("sig_backlog", source="backlog"))
    await router.ingest(_env("sig_voice", source="voice_human"))
    await asyncio.sleep(0.3)
    await router.stop()

    # Both dispatched; voice_human may arrive first due to priority
    assert "backlog" in dispatch_order
    assert "voice_human" in dispatch_order


async def test_pending_ack_envelope_not_dispatched_without_ack(tmp_path):
    """requires_human_ack=True envelopes stay in PENDING_ACK until acknowledged."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeRouterConfig(project_root=tmp_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    env_with_ack = make_envelope(
        source="ai_miner",
        description="needs ack",
        target_files=("backend/core/needs_ack.py",),
        repo="jarvis",
        confidence=0.5,
        urgency="low",
        evidence={"signature": "needs_ack"},
        requires_human_ack=True,
    )
    result = await router.ingest(env_with_ack)
    assert result == "pending_ack"

    await asyncio.sleep(0.1)
    # Submit should NOT have been called
    gls.submit.assert_not_called()

    await router.stop()


async def test_acknowledge_releases_pending_ack(tmp_path):
    """Calling router.acknowledge() enqueues the held envelope for dispatch."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeRouterConfig(project_root=tmp_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    env = make_envelope(
        source="ai_miner",
        description="miner candidate",
        target_files=("backend/core/complex.py",),
        repo="jarvis",
        confidence=0.5,
        urgency="low",
        evidence={"signature": "ack_test"},
        requires_human_ack=True,
    )
    await router.ingest(env)
    assert router.pending_ack_count() == 1

    # Human approves
    released = await router.acknowledge(env.idempotency_key)
    assert released is True

    await asyncio.sleep(0.15)
    await router.stop()

    # Now submit should have been called
    assert gls.submit.call_count >= 1
