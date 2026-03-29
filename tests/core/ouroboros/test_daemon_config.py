"""Tests for DaemonConfig — environment-driven Ouroboros Daemon configuration."""
import pytest

from backend.core.ouroboros.daemon_config import DaemonConfig


class TestDaemonConfigDefaults:
    def test_defaults(self):
        cfg = DaemonConfig()
        assert cfg.daemon_enabled is True
        assert cfg.vital_scan_timeout_s == 30.0
        assert cfg.spinal_timeout_s == 10.0
        assert cfg.rem_enabled is True
        assert cfg.rem_cycle_timeout_s == 300.0
        assert cfg.rem_epoch_timeout_s == 1800.0
        assert cfg.rem_max_agents == 30
        assert cfg.rem_max_findings_per_epoch == 10
        assert cfg.rem_cooldown_s == 3600.0
        assert cfg.rem_idle_eligible_s == 60.0
        assert cfg.exploration_model_enabled is False
        assert cfg.exploration_model_rpm == 10


class TestDaemonConfigEnvOverride:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("OUROBOROS_DAEMON_ENABLED", "false")
        monkeypatch.setenv("OUROBOROS_VITAL_SCAN_TIMEOUT_S", "45.5")
        monkeypatch.setenv("OUROBOROS_REM_MAX_AGENTS", "20")
        monkeypatch.setenv("OUROBOROS_EXPLORATION_MODEL_ENABLED", "true")
        monkeypatch.setenv("OUROBOROS_EXPLORATION_MODEL_RPM", "5")

        cfg = DaemonConfig.from_env()

        assert cfg.daemon_enabled is False
        assert cfg.vital_scan_timeout_s == 45.5
        assert cfg.rem_max_agents == 20
        assert cfg.exploration_model_enabled is True
        assert cfg.exploration_model_rpm == 5

    def test_env_override_rem_fields(self, monkeypatch):
        monkeypatch.setenv("OUROBOROS_REM_ENABLED", "0")
        monkeypatch.setenv("OUROBOROS_REM_CYCLE_TIMEOUT_S", "600.0")
        monkeypatch.setenv("OUROBOROS_REM_EPOCH_TIMEOUT_S", "3600.0")
        monkeypatch.setenv("OUROBOROS_REM_MAX_FINDINGS", "25")
        monkeypatch.setenv("OUROBOROS_REM_COOLDOWN_S", "7200.0")
        monkeypatch.setenv("OUROBOROS_REM_IDLE_ELIGIBLE_S", "120.0")
        monkeypatch.setenv("OUROBOROS_SPINAL_TIMEOUT_S", "20.0")

        cfg = DaemonConfig.from_env()

        assert cfg.rem_enabled is False
        assert cfg.rem_cycle_timeout_s == 600.0
        assert cfg.rem_epoch_timeout_s == 3600.0
        assert cfg.rem_max_findings_per_epoch == 25
        assert cfg.rem_cooldown_s == 7200.0
        assert cfg.rem_idle_eligible_s == 120.0
        assert cfg.spinal_timeout_s == 20.0


class TestDaemonConfigBooleanParsing:
    """Verify all documented truthy and falsy string values are handled."""

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("OUROBOROS_DAEMON_ENABLED", value)
        cfg = DaemonConfig.from_env()
        assert cfg.daemon_enabled is True

    @pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no"])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("OUROBOROS_DAEMON_ENABLED", value)
        cfg = DaemonConfig.from_env()
        assert cfg.daemon_enabled is False

    def test_invalid_bool_raises(self, monkeypatch):
        monkeypatch.setenv("OUROBOROS_DAEMON_ENABLED", "maybe")
        with pytest.raises(ValueError, match="OUROBOROS_DAEMON_ENABLED"):
            DaemonConfig.from_env()


class TestDaemonConfigFrozen:
    def test_is_frozen(self):
        cfg = DaemonConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.rem_max_agents = 99  # type: ignore[misc]
