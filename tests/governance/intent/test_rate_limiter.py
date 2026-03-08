"""Tests for RateLimiter and RateLimiterConfig.

Validates throughput governance for JARVIS's Intent Engine (Layer 1).
RateLimiter enforces per-file cooldowns, per-signal cooldowns, and
hourly/daily operation caps to prevent runaway autonomous actions.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.intent.rate_limiter import (
    RateLimiter,
    RateLimiterConfig,
)


# ---------------------------------------------------------------------------
# RateLimiterConfig tests
# ---------------------------------------------------------------------------


class TestRateLimiterConfigFromEnv:
    """test_rate_limiter_config_from_env"""

    def test_defaults_without_env(self):
        cfg = RateLimiterConfig()
        assert cfg.max_ops_per_hour == 5
        assert cfg.max_ops_per_day == 20
        assert cfg.per_file_cooldown_s == 600.0
        assert cfg.per_signal_cooldown_s == 300.0

    def test_from_env_reads_env_vars(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("JARVIS_INTENT_MAX_OPS_HOUR", "10")
        monkeypatch.setenv("JARVIS_INTENT_MAX_OPS_DAY", "50")
        monkeypatch.setenv("JARVIS_INTENT_FILE_COOLDOWN_S", "120.5")
        monkeypatch.setenv("JARVIS_INTENT_SIGNAL_COOLDOWN_S", "60.0")

        cfg = RateLimiterConfig.from_env()
        assert cfg.max_ops_per_hour == 10
        assert cfg.max_ops_per_day == 50
        assert cfg.per_file_cooldown_s == 120.5
        assert cfg.per_signal_cooldown_s == 60.0

    def test_from_env_falls_back_to_defaults(self, monkeypatch: pytest.MonkeyPatch):
        # Ensure none of the env vars are set
        monkeypatch.delenv("JARVIS_INTENT_MAX_OPS_HOUR", raising=False)
        monkeypatch.delenv("JARVIS_INTENT_MAX_OPS_DAY", raising=False)
        monkeypatch.delenv("JARVIS_INTENT_FILE_COOLDOWN_S", raising=False)
        monkeypatch.delenv("JARVIS_INTENT_SIGNAL_COOLDOWN_S", raising=False)

        cfg = RateLimiterConfig.from_env()
        assert cfg.max_ops_per_hour == 5
        assert cfg.max_ops_per_day == 20
        assert cfg.per_file_cooldown_s == 600.0
        assert cfg.per_signal_cooldown_s == 300.0


# ---------------------------------------------------------------------------
# RateLimiter tests
# ---------------------------------------------------------------------------


class TestRateLimiterAllowsWithinLimits:
    """test_rate_limiter_allows_within_limits"""

    def test_first_operation_is_allowed(self):
        limiter = RateLimiter()
        allowed, reason = limiter.check("backend/core/foo.py")
        assert allowed is True
        assert reason == ""

    def test_different_file_after_record_is_allowed(self):
        limiter = RateLimiter()
        limiter.record("backend/core/foo.py")
        allowed, reason = limiter.check("backend/core/bar.py")
        assert allowed is True
        assert reason == ""


class TestRateLimiterPerFileCooldown:
    """test_rate_limiter_per_file_cooldown"""

    def test_same_file_blocked_within_cooldown(self):
        limiter = RateLimiter(RateLimiterConfig(per_file_cooldown_s=600.0))
        limiter.record("backend/core/foo.py")
        allowed, reason = limiter.check("backend/core/foo.py")
        assert allowed is False
        assert reason == "rate_limit:file_cooldown"


class TestRateLimiterDifferentFileNotBlocked:
    """test_rate_limiter_different_file_not_blocked"""

    def test_record_file_a_check_file_b_allowed(self):
        limiter = RateLimiter(RateLimiterConfig(per_file_cooldown_s=600.0))
        limiter.record("backend/core/a.py")
        allowed, reason = limiter.check("backend/core/b.py")
        assert allowed is True
        assert reason == ""


class TestRateLimiterHourlyCap:
    """test_rate_limiter_hourly_cap"""

    def test_blocked_after_exhausting_hourly_cap(self):
        cfg = RateLimiterConfig(
            max_ops_per_hour=3,
            max_ops_per_day=100,
            per_file_cooldown_s=0.0,  # disable file cooldown
            per_signal_cooldown_s=0.0,  # disable signal cooldown
        )
        limiter = RateLimiter(cfg)

        # Record 3 operations (each on a different file to avoid file cooldown)
        for i in range(3):
            limiter.record(f"file_{i}.py")

        # 4th should be blocked by hourly cap
        allowed, reason = limiter.check("file_new.py")
        assert allowed is False
        assert reason == "rate_limit:hourly_cap"


class TestRateLimiterDailyCap:
    """test_rate_limiter_daily_cap"""

    def test_blocked_after_exhausting_daily_cap(self):
        cfg = RateLimiterConfig(
            max_ops_per_hour=100,  # high hourly cap so it doesn't interfere
            max_ops_per_day=3,
            per_file_cooldown_s=0.0,
            per_signal_cooldown_s=0.0,
        )
        limiter = RateLimiter(cfg)

        # Record 3 operations
        for i in range(3):
            limiter.record(f"file_{i}.py")

        # 4th should be blocked by daily cap
        allowed, reason = limiter.check("file_new.py")
        assert allowed is False
        assert reason == "rate_limit:daily_cap"

    def test_daily_cap_checked_after_hourly(self):
        """Hourly cap is checked before daily cap, so if both are hit,
        hourly reason takes precedence."""
        cfg = RateLimiterConfig(
            max_ops_per_hour=2,
            max_ops_per_day=3,
            per_file_cooldown_s=0.0,
            per_signal_cooldown_s=0.0,
        )
        limiter = RateLimiter(cfg)

        for i in range(2):
            limiter.record(f"file_{i}.py")

        # Both hourly (2) and not-yet-daily (2<3) — hourly should fire first
        allowed, reason = limiter.check("file_new.py")
        assert allowed is False
        assert reason == "rate_limit:hourly_cap"


class TestRateLimiterPerSignalCooldown:
    """test_rate_limiter_per_signal_cooldown"""

    def test_same_signal_key_blocked_within_cooldown(self):
        cfg = RateLimiterConfig(
            per_file_cooldown_s=0.0,  # disable file cooldown
            per_signal_cooldown_s=300.0,
        )
        limiter = RateLimiter(cfg)
        limiter.record("backend/core/foo.py", signal_key="sig:abc")
        allowed, reason = limiter.check("backend/core/bar.py", signal_key="sig:abc")
        assert allowed is False
        assert reason == "rate_limit:signal_cooldown"

    def test_different_signal_key_allowed(self):
        cfg = RateLimiterConfig(
            per_file_cooldown_s=0.0,
            per_signal_cooldown_s=300.0,
        )
        limiter = RateLimiter(cfg)
        limiter.record("backend/core/foo.py", signal_key="sig:abc")
        allowed, reason = limiter.check("backend/core/bar.py", signal_key="sig:xyz")
        assert allowed is True
        assert reason == ""

    def test_no_signal_key_skips_signal_cooldown(self):
        cfg = RateLimiterConfig(
            per_file_cooldown_s=0.0,
            per_signal_cooldown_s=300.0,
        )
        limiter = RateLimiter(cfg)
        limiter.record("backend/core/foo.py")  # no signal_key
        allowed, reason = limiter.check("backend/core/bar.py")  # no signal_key
        assert allowed is True
        assert reason == ""
