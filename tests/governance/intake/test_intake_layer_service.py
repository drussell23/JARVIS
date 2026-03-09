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
