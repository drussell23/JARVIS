"""Tests for email triage configuration and feature flags."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.config import TriageConfig, get_triage_config


class TestTriageConfigDefaults:
    """Default config values match the design spec."""

    def test_disabled_by_default(self):
        config = TriageConfig()
        assert config.enabled is False

    def test_tier_thresholds(self):
        config = TriageConfig()
        assert config.tier1_min == 85
        assert config.tier2_min == 65
        assert config.tier3_min == 35

    def test_quiet_hours(self):
        config = TriageConfig()
        assert config.quiet_start_hour == 23
        assert config.quiet_end_hour == 8

    def test_dedup_windows(self):
        config = TriageConfig()
        assert config.dedup_tier1_s == 900
        assert config.dedup_tier2_s == 3600

    def test_interrupt_budget(self):
        config = TriageConfig()
        assert config.max_interrupts_per_hour == 3
        assert config.max_interrupts_per_day == 12

    def test_summary_interval(self):
        config = TriageConfig()
        assert config.summary_interval_s == 1800

    def test_runner_settings(self):
        config = TriageConfig()
        assert config.poll_interval_s == 60.0
        assert config.max_emails_per_cycle == 25
        assert config.cycle_timeout_s == 30.0

    def test_gmail_labels(self):
        config = TriageConfig()
        assert config.label_tier1 == "jarvis/tier1_critical"
        assert config.label_tier2 == "jarvis/tier2_high"
        assert config.label_tier3 == "jarvis/tier3_review"
        assert config.label_tier4 == "jarvis/tier4_noise"

    def test_notification_flags_default_true(self):
        config = TriageConfig()
        assert config.notify_tier1 is True
        assert config.notify_tier2 is True

    def test_quarantine_default_false(self):
        config = TriageConfig()
        assert config.quarantine_tier4 is False


class TestTriageConfigFromEnv:
    """Config reads from environment variables."""

    def test_enabled_from_env(self, monkeypatch):
        monkeypatch.setenv("EMAIL_TRIAGE_ENABLED", "true")
        config = TriageConfig.from_env()
        assert config.enabled is True

    def test_poll_interval_from_env(self, monkeypatch):
        monkeypatch.setenv("EMAIL_TRIAGE_POLL_INTERVAL_S", "120")
        config = TriageConfig.from_env()
        assert config.poll_interval_s == 120.0

    def test_quiet_hours_from_env(self, monkeypatch):
        monkeypatch.setenv("EMAIL_TRIAGE_QUIET_START", "22")
        monkeypatch.setenv("EMAIL_TRIAGE_QUIET_END", "7")
        config = TriageConfig.from_env()
        assert config.quiet_start_hour == 22
        assert config.quiet_end_hour == 7

    def test_budget_from_env(self, monkeypatch):
        monkeypatch.setenv("EMAIL_TRIAGE_MAX_INTERRUPTS_HOUR", "5")
        monkeypatch.setenv("EMAIL_TRIAGE_MAX_INTERRUPTS_DAY", "20")
        config = TriageConfig.from_env()
        assert config.max_interrupts_per_hour == 5
        assert config.max_interrupts_per_day == 20

    def test_invalid_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("EMAIL_TRIAGE_POLL_INTERVAL_S", "not_a_number")
        config = TriageConfig.from_env()
        assert config.poll_interval_s == 60.0


class TestGetTriageConfig:
    """get_triage_config() returns singleton."""

    def test_returns_config(self):
        config = get_triage_config()
        assert isinstance(config, TriageConfig)

    def test_tier_label_for_score(self):
        config = TriageConfig()
        assert config.label_for_tier(1) == "jarvis/tier1_critical"
        assert config.label_for_tier(2) == "jarvis/tier2_high"
        assert config.label_for_tier(3) == "jarvis/tier3_review"
        assert config.label_for_tier(4) == "jarvis/tier4_noise"

    def test_tier_for_score(self):
        config = TriageConfig()
        assert config.tier_for_score(100) == 1
        assert config.tier_for_score(85) == 1
        assert config.tier_for_score(84) == 2
        assert config.tier_for_score(65) == 2
        assert config.tier_for_score(64) == 3
        assert config.tier_for_score(35) == 3
        assert config.tier_for_score(34) == 4
        assert config.tier_for_score(0) == 4
