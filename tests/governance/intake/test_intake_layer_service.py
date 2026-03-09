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


def test_intake_layer_config_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("JARVIS_INTAKE_DEDUP_WINDOW_S", "120.0")
    config = IntakeLayerConfig.from_env()
    assert config.project_root == tmp_path
    assert config.dedup_window_s == 120.0
