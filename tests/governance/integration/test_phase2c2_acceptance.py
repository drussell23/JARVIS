"""
Phase 2C.2/2C.3 acceptance tests.

AC1: IntakeLayerService starts and reaches ACTIVE/DEGRADED
AC2: VoiceNarrator (B) appears in CommProtocol transports (import fix verified)
AC3: A-narrator fires for voice_human; silent for backlog and ai_miner
AC4: Voice command envelope reaches GLS.submit() within 1s
AC5: IntakeLayerService.stop() drains cleanly; GLS.stop() is not called by intake
AC6: health() returns required keys with correct types
AC7: Intake symbols importable from both intake and governance packages
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake import (
    IntakeLayerConfig,
    IntakeLayerService,
    IntakeServiceState,
    IntakeNarrator,
    make_envelope,
)


# ── AC1: IntakeLayerService starts ──────────────────────────────────────────

async def test_ac1_service_starts_active(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.start()
    assert svc.state in (IntakeServiceState.ACTIVE, IntakeServiceState.DEGRADED)
    await svc.stop()
    assert svc.state is IntakeServiceState.INACTIVE


# ── AC2: VoiceNarrator wired in CommProtocol ────────────────────────────────

def test_ac2_voice_narrator_in_comm_protocol():
    from backend.core.ouroboros.governance.integration import _build_comm_protocol
    protocol = _build_comm_protocol()
    transport_types = [type(t).__name__ for t in protocol._transports]
    assert "VoiceNarrator" in transport_types, (
        f"VoiceNarrator not wired. Transports: {transport_types}"
    )


# ── AC3: A-narrator salience policy ─────────────────────────────────────────

async def test_ac3_a_narrator_voice_human_speaks():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="voice_human", description="deploy the fix now",
        target_files=("backend/auth.py",), repo="jarvis",
        confidence=0.95, urgency="critical",
        evidence={"signature": "ac3_voice"},
        requires_human_ack=False,
    )
    await narrator.on_envelope(env)
    say_fn.assert_called_once()


async def test_ac3_a_narrator_backlog_silent():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="backlog", description="fix something low-pri",
        target_files=("backend/x.py",), repo="jarvis",
        confidence=0.7, urgency="normal",
        evidence={"signature": "ac3_backlog"},
        requires_human_ack=False,
    )
    await narrator.on_envelope(env)
    say_fn.assert_not_called()


async def test_ac3_a_narrator_ai_miner_silent():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="ai_miner", description="refactor complex func",
        target_files=("backend/complex.py",), repo="jarvis",
        confidence=0.4, urgency="low",
        evidence={"signature": "ac3_miner"},
        requires_human_ack=True,
    )
    await narrator.on_envelope(env)
    say_fn.assert_not_called()


# ── AC4: Voice command reaches GLS.submit within 1s ─────────────────────────

async def test_ac4_voice_command_reaches_gls(tmp_path):
    submitted = []

    async def mock_submit(ctx, trigger_source=""):
        submitted.append(ctx.op_id)

    gls = MagicMock()
    gls.submit = mock_submit

    config = IntakeLayerConfig(project_root=tmp_path, dedup_window_s=60.0)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.start()

    # Inject a voice command directly into the router
    env = make_envelope(
        source="voice_human", description="fix auth module",
        target_files=("backend/core/auth.py",), repo="jarvis",
        confidence=0.95, urgency="critical",
        evidence={"signature": "ac4_direct"},
        requires_human_ack=False,
    )
    await svc._router.ingest(env)
    await asyncio.sleep(0.5)  # well within 1s
    await svc.stop()

    assert len(submitted) == 1
    assert submitted[0] == env.causal_id


# ── AC5: Stop order — intake stops cleanly, GLS.stop() not called by intake ─

async def test_ac5_intake_stop_does_not_call_gls_stop(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    gls.stop = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.start()
    await svc.stop()
    assert svc.state is IntakeServiceState.INACTIVE
    gls.stop.assert_not_called()


# ── AC6: health() keys and types ─────────────────────────────────────────────

async def test_ac6_health_keys(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.start()
    h = svc.health()
    assert isinstance(h["state"], str)
    assert isinstance(h["queue_depth"], int)
    assert isinstance(h["dead_letter_count"], int)
    assert isinstance(h["per_source_rate"], dict)
    assert isinstance(h["uptime_s"], float)
    await svc.stop()


# ── AC7: Symbol imports from both packages ────────────────────────────────────

def test_ac7_intake_package_exports():
    from backend.core.ouroboros.governance.intake import (
        IntakeLayerConfig as ILC,
        IntakeLayerService as ILS,
        IntakeServiceState as ISS,
        IntakeNarrator as IN,
    )
    assert ILC is IntakeLayerConfig
    assert ILS is IntakeLayerService
    assert ISS is IntakeServiceState
    assert IN is IntakeNarrator


def test_ac7_governance_package_exports():
    from backend.core.ouroboros.governance import (
        IntakeLayerConfig as ILC,
        IntakeLayerService as ILS,
        IntakeServiceState as ISS,
        IntakeNarrator as IN,
    )
    assert ILC is IntakeLayerConfig
    assert ILS is IntakeLayerService
    assert ISS is IntakeServiceState
    assert IN is IntakeNarrator
