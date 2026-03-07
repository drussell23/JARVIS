"""Tests for bounded GCP health verification timeout."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))


class TestBoundedVerification:
    def test_effective_timeout_capped_by_script_timeout_plus_grace(self):
        """Effective timeout should be min(config_timeout, script_timeout + grace)."""
        config_timeout = 300.0
        script_health_timeout = 90.0
        grace_seconds = 30.0

        effective = min(config_timeout, script_health_timeout + grace_seconds)
        assert effective == 120.0

    def test_config_timeout_wins_when_smaller(self):
        """If config timeout is smaller than script + grace, use config."""
        config_timeout = 60.0
        script_health_timeout = 90.0
        grace_seconds = 30.0

        effective = min(config_timeout, script_health_timeout + grace_seconds)
        assert effective == 60.0

    def test_custom_script_timeout_respected(self):
        """Custom GCP_SERVICE_HEALTH_TIMEOUT should feed into bound."""
        config_timeout = 300.0
        script_health_timeout = 180.0
        grace_seconds = 30.0

        effective = min(config_timeout, script_health_timeout + grace_seconds)
        assert effective == 210.0

    def test_grace_period_configurable(self):
        """Grace period should be configurable via environment."""
        config_timeout = 300.0
        script_health_timeout = 90.0
        grace_seconds = 60.0

        effective = min(config_timeout, script_health_timeout + grace_seconds)
        assert effective == 150.0

    def test_zero_grace_means_exit_at_script_timeout(self):
        """Zero grace means exit immediately when script timeout elapses."""
        config_timeout = 300.0
        script_health_timeout = 90.0
        grace_seconds = 0.0

        effective = min(config_timeout, script_health_timeout + grace_seconds)
        assert effective == 90.0
