"""IntakeLayerService — lifecycle and config tests."""
import os
from pathlib import Path

from backend.core.ouroboros.governance.intake.intake_layer_service import (
    IntakeLayerConfig,
    IntakeLayerService,
    IntakeServiceState,
)


def test_intake_layer_config_defaults(tmp_path):
    config = IntakeLayerConfig(project_root=tmp_path)
    assert config.project_root == tmp_path
    assert config.dedup_window_s > 0
    assert config.backlog_scan_interval_s > 0
    assert config.miner_complexity_threshold > 0
    assert config.a_narrator_enabled is True
    assert config.miner_scan_paths == ["backend/", "tests/"]


def test_intake_layer_config_from_env_bool(monkeypatch):
    monkeypatch.setenv("JARVIS_INTAKE_A_NARRATOR_ENABLED", "false")
    config = IntakeLayerConfig.from_env()
    assert config.a_narrator_enabled is False


def test_intake_layer_config_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("JARVIS_INTAKE_DEDUP_WINDOW_S", "120.0")
    monkeypatch.setenv("JARVIS_INTAKE_MINER_SCAN_PATHS", "src/,lib/")
    config = IntakeLayerConfig.from_env()
    assert config.project_root == tmp_path
    assert config.dedup_window_s == 120.0
    assert config.miner_scan_paths == ["src/", "lib/"]


from unittest.mock import AsyncMock, MagicMock


async def test_service_initial_state(tmp_path):
    gls = MagicMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    assert svc.state is IntakeServiceState.INACTIVE


async def test_service_start_reaches_active(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    say_fn = AsyncMock(return_value=True)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=say_fn)
    await svc.start()
    assert svc.state in (IntakeServiceState.ACTIVE, IntakeServiceState.DEGRADED)
    await svc.stop()
    assert svc.state is IntakeServiceState.INACTIVE


async def test_service_start_idempotent(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.start()
    state_after_first = svc.state
    await svc.start()  # second call must be no-op
    assert svc.state is state_after_first
    await svc.stop()


async def test_service_health_keys(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.start()
    h = svc.health()
    assert "state" in h
    assert "queue_depth" in h
    assert "dead_letter_count" in h
    assert "per_source_rate" in h
    await svc.stop()


async def test_service_stop_from_inactive_is_noop(tmp_path):
    gls = MagicMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.stop()  # must not raise
    assert svc.state is IntakeServiceState.INACTIVE


async def test_service_start_failure_cleans_up(tmp_path):
    """On start failure, state is FAILED and router/sensors are cleaned up."""
    from unittest.mock import patch

    gls = MagicMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)

    # Make _build_components raise
    async def bad_build():
        raise RuntimeError("simulated build failure")

    with patch.object(svc, "_build_components", bad_build):
        try:
            await svc.start()
        except RuntimeError:
            pass

    assert svc.state is IntakeServiceState.FAILED
    assert svc._router is None
    assert svc._sensors == []


# ---------------------------------------------------------------------------
# IntakeNarrator tests (Task 4)
# ---------------------------------------------------------------------------

from backend.core.ouroboros.governance.intake.intake_layer_service import IntakeNarrator
from backend.core.ouroboros.governance.intake import make_envelope


async def test_a_narrator_speaks_for_voice_human():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="voice_human", description="fix auth now",
        target_files=("backend/auth.py",), repo="jarvis",
        confidence=0.95, urgency="critical",
        evidence={"signature": "voice_test_1"},
        requires_human_ack=False,
    )
    await narrator.on_envelope(env)
    say_fn.assert_called_once()
    text = say_fn.call_args.args[0]
    assert "command" in text.lower() or "voice" in text.lower()


async def test_a_narrator_silent_for_backlog():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="backlog", description="fix something",
        target_files=("backend/x.py",), repo="jarvis",
        confidence=0.7, urgency="normal",
        evidence={"signature": "backlog_1"},
        requires_human_ack=False,
    )
    await narrator.on_envelope(env)
    say_fn.assert_not_called()


async def test_a_narrator_silent_for_ai_miner():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="ai_miner", description="refactor complex.py",
        target_files=("backend/complex.py",), repo="jarvis",
        confidence=0.4, urgency="low",
        evidence={"signature": "miner_1"},
        requires_human_ack=True,
    )
    await narrator.on_envelope(env)
    say_fn.assert_not_called()


async def test_a_narrator_speaks_test_failure_above_threshold():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0, test_failure_min_count=2)
    for i in range(2):
        env = make_envelope(
            source="test_failure", description=f"test fail {i}",
            target_files=("tests/test_x.py",), repo="jarvis",
            confidence=0.9, urgency="high",
            evidence={"signature": f"tf_{i}"},
            requires_human_ack=False,
        )
        await narrator.on_envelope(env)
    assert say_fn.call_count >= 1


async def test_a_narrator_silent_for_test_failure_below_threshold():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0, test_failure_min_count=3)
    env = make_envelope(
        source="test_failure", description="one failure",
        target_files=("tests/test_x.py",), repo="jarvis",
        confidence=0.9, urgency="high",
        evidence={"signature": "tf_below"},
        requires_human_ack=False,
    )
    await narrator.on_envelope(env)
    say_fn.assert_not_called()


async def test_a_narrator_debounce_suppresses_rapid_voice():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=999.0)
    for i in range(3):
        env = make_envelope(
            source="voice_human", description=f"command {i}",
            target_files=("backend/auth.py",), repo="jarvis",
            confidence=0.95, urgency="critical",
            evidence={"signature": f"v_{i}"},
            requires_human_ack=False,
        )
        await narrator.on_envelope(env)
    assert say_fn.call_count == 1


async def test_a_narrator_say_fn_failure_is_swallowed():
    """say_fn failure must not raise — narrator swallows it."""
    say_fn = AsyncMock(side_effect=RuntimeError("tts failed"))
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="voice_human", description="fix something",
        target_files=("backend/auth.py",), repo="jarvis",
        confidence=0.95, urgency="critical",
        evidence={"signature": "fail_test"},
        requires_human_ack=False,
    )
    await narrator.on_envelope(env)  # must not raise
    say_fn.assert_called_once()


# ---------------------------------------------------------------------------
# UnifiedIntakeRouter._on_ingest_hook tests (Task 5)
# ---------------------------------------------------------------------------

import asyncio

from backend.core.ouroboros.governance.intake import (
    UnifiedIntakeRouter, IntakeRouterConfig,
)


async def test_router_on_ingest_hook_called(tmp_path):
    """UnifiedIntakeRouter calls _on_ingest_hook after successful enqueue."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeRouterConfig(project_root=tmp_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    hooked_envelopes = []

    async def hook(env):
        hooked_envelopes.append(env)

    router._on_ingest_hook = hook

    env = make_envelope(
        source="voice_human", description="test hook",
        target_files=("a.py",), repo="jarvis",
        confidence=0.9, urgency="critical",
        evidence={"signature": "hook_test_t5"},
        requires_human_ack=False,
    )
    await router.ingest(env)
    await asyncio.sleep(0.05)
    await router.stop()

    assert len(hooked_envelopes) == 1
    assert hooked_envelopes[0].causal_id == env.causal_id


async def test_router_hook_not_called_on_dedup(tmp_path):
    """Hook must NOT fire for deduplicated envelopes."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeRouterConfig(project_root=tmp_path, dedup_window_s=60.0)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    hook_count = 0

    async def hook(env):
        nonlocal hook_count
        hook_count += 1

    router._on_ingest_hook = hook

    env = make_envelope(
        source="backlog", description="fix y dedup hook",
        target_files=("backend/y.py",), repo="jarvis",
        confidence=0.8, urgency="normal",
        evidence={"signature": "dedup_hook_test"},
        requires_human_ack=False,
    )
    r1 = await router.ingest(env)
    r2 = await router.ingest(env)  # duplicate
    assert r1 == "enqueued"
    assert r2 == "deduplicated"
    await router.stop()

    assert hook_count == 1  # hook fires once for first, not for duplicate
