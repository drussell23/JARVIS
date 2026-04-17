"""risk_tier_floor tests — MIN_RISK_TIER + PARANOIA_MODE + QUIET_HOURS.

These three env knobs compose to close the "SAFE_AUTO lands overnight"
blast-radius gap. Ordering: strictest applies. This suite confirms the
composition + the wrap-around hour math + the pass-through for unknown
tier inputs.
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest

from backend.core.ouroboros.governance import risk_tier_floor as rtf
from backend.core.ouroboros.governance.risk_tier_floor import (
    apply_floor_to_name,
    floor_reason,
    paranoia_mode_enabled,
    quiet_hours_active,
    recommended_floor,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_MIN_RISK_TIER") or key.startswith(
            "JARVIS_PARANOIA_MODE"
        ) or key.startswith("JARVIS_AUTO_APPLY_QUIET_HOURS"):
            monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# (1) Defaults
# ---------------------------------------------------------------------------


def test_no_envs_set_returns_none_floor():
    assert recommended_floor() is None


def test_no_envs_passes_tier_through():
    assert apply_floor_to_name("safe_auto") == ("safe_auto", None)
    assert apply_floor_to_name("approval_required") == ("approval_required", None)


def test_paranoia_default_off():
    assert paranoia_mode_enabled() is False


# ---------------------------------------------------------------------------
# (2) Explicit MIN_RISK_TIER
# ---------------------------------------------------------------------------


def test_min_tier_notify_apply_upgrades_safe_auto(monkeypatch):
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "notify_apply")
    effective, applied = apply_floor_to_name("safe_auto")
    assert effective == "notify_apply"
    assert applied == "notify_apply"


def test_min_tier_approval_required_upgrades_safe_auto(monkeypatch):
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
    effective, applied = apply_floor_to_name("safe_auto")
    assert effective == "approval_required"
    assert applied == "approval_required"


def test_min_tier_does_not_downgrade(monkeypatch):
    """Floor is strictly upward — already-high tier passes through."""
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "notify_apply")
    effective, applied = apply_floor_to_name("approval_required")
    assert effective == "approval_required"
    assert applied is None


def test_min_tier_unrecognized_ignored(monkeypatch):
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "totally_made_up")
    effective, applied = apply_floor_to_name("safe_auto")
    assert effective == "safe_auto"
    assert applied is None


def test_min_tier_case_insensitive(monkeypatch):
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "NOTIFY_APPLY")
    assert apply_floor_to_name("safe_auto")[0] == "notify_apply"


def test_unknown_input_tier_passes_through(monkeypatch):
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
    effective, applied = apply_floor_to_name("something_odd")
    assert effective == "something_odd"
    assert applied is None


# ---------------------------------------------------------------------------
# (3) PARANOIA_MODE shortcut
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_paranoia_mode_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_PARANOIA_MODE", val)
    assert paranoia_mode_enabled() is True


def test_paranoia_upgrades_safe_auto_to_notify_apply(monkeypatch):
    monkeypatch.setenv("JARVIS_PARANOIA_MODE", "1")
    effective, applied = apply_floor_to_name("safe_auto")
    assert effective == "notify_apply"
    assert applied == "notify_apply"


def test_paranoia_does_not_upgrade_beyond_notify_apply(monkeypatch):
    monkeypatch.setenv("JARVIS_PARANOIA_MODE", "1")
    # Already at notify_apply: no upgrade (paranoia floor equals).
    effective, applied = apply_floor_to_name("notify_apply")
    assert effective == "notify_apply"
    assert applied is None


# ---------------------------------------------------------------------------
# (4) QUIET_HOURS windows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,hour,expected", [
    ("9-17", 10, True),    # inside
    ("9-17", 8, False),    # before
    ("9-17", 17, False),   # boundary (end exclusive)
    ("9-17", 16, True),    # inside
    ("22-7", 23, True),    # wrap — after start
    ("22-7", 3, True),     # wrap — before end
    ("22-7", 7, False),    # wrap — boundary
    ("22-7", 12, False),   # wrap — middle of day
])
def test_quiet_hours_window(monkeypatch, raw, hour, expected):
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", raw)
    now = datetime(2026, 4, 17, hour, 30, 0)
    assert quiet_hours_active(now) is expected


def test_quiet_hours_unset_always_false():
    assert quiet_hours_active(datetime(2026, 4, 17, 3, 0)) is False


def test_quiet_hours_malformed_ignored(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "not-a-range")
    assert quiet_hours_active(datetime(2026, 4, 17, 3, 0)) is False


def test_quiet_hours_out_of_range_ignored(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "25-99")
    assert quiet_hours_active(datetime(2026, 4, 17, 3, 0)) is False


def test_quiet_hours_upgrades_safe_auto(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "22-7")
    now = datetime(2026, 4, 17, 3, 0)   # inside window
    effective = recommended_floor(now)
    assert effective == "notify_apply"


# ---------------------------------------------------------------------------
# (5) Composition — strictest wins
# ---------------------------------------------------------------------------


def test_min_tier_wins_over_paranoia_when_stricter(monkeypatch):
    """MIN_RISK_TIER=approval_required + PARANOIA=1 → approval_required."""
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
    monkeypatch.setenv("JARVIS_PARANOIA_MODE", "1")
    assert recommended_floor() == "approval_required"


def test_paranoia_wins_over_quiet_hours_outside_window(monkeypatch):
    monkeypatch.setenv("JARVIS_PARANOIA_MODE", "1")
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "22-7")
    # Noon — outside quiet hours; paranoia alone kicks in.
    now = datetime(2026, 4, 17, 12, 0)
    assert recommended_floor(now) == "notify_apply"


def test_stacked_paranoia_and_quiet_still_notify_apply(monkeypatch):
    """Both set, both upgrade to notify_apply — composition is idempotent."""
    monkeypatch.setenv("JARVIS_PARANOIA_MODE", "1")
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "22-7")
    now = datetime(2026, 4, 17, 3, 0)   # inside window
    assert recommended_floor(now) == "notify_apply"


def test_min_tier_stricter_than_paranoia_wins(monkeypatch):
    """MIN_RISK_TIER=approval_required + PARANOIA=1 (notify_apply)
    must collapse to approval_required."""
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
    monkeypatch.setenv("JARVIS_PARANOIA_MODE", "1")
    effective, applied = apply_floor_to_name("safe_auto")
    assert effective == "approval_required"
    assert applied == "approval_required"


# ---------------------------------------------------------------------------
# (6) floor_reason — human-readable explanation
# ---------------------------------------------------------------------------


def test_floor_reason_reports_min_tier(monkeypatch):
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "notify_apply")
    reason = floor_reason()
    assert "JARVIS_MIN_RISK_TIER" in reason
    assert "notify_apply" in reason


def test_floor_reason_reports_paranoia(monkeypatch):
    monkeypatch.setenv("JARVIS_PARANOIA_MODE", "1")
    reason = floor_reason()
    assert "PARANOIA" in reason or "paranoia" in reason.lower()


def test_floor_reason_reports_quiet_hours(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "22-7")
    now = datetime(2026, 4, 17, 3, 0)
    reason = floor_reason(now)
    assert "QUIET_HOURS" in reason
    assert "22-7" in reason


def test_floor_reason_empty_when_nothing_active():
    assert "no floor" in floor_reason().lower()


# ---------------------------------------------------------------------------
# (7) AST canary — orchestrator consumes the floor module
# ---------------------------------------------------------------------------


def test_orchestrator_applies_tier_floor():
    from pathlib import Path
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/orchestrator.py"
    ).read_text(encoding="utf-8")
    assert "apply_floor_to_name" in src
    assert "risk_tier_floor" in src


# ---------------------------------------------------------------------------
# (8) QUIET_HOURS_TZ — explicit IANA zone semantics (Track B)
# ---------------------------------------------------------------------------
#
# Before Track B, quiet_hours_active() used naive datetime.now() which
# silently adopted the host's local timezone — ambiguous across multi-
# operator deployments. Now: default UTC, TZ override via env.


def test_quiet_hours_default_tz_is_utc(monkeypatch):
    """With no TZ env set, naive `now` is interpreted as UTC."""
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "22-7")
    # 3 AM UTC (naive) — inside 22-7 window under UTC.
    now_naive = datetime(2026, 4, 17, 3, 0)
    assert quiet_hours_active(now_naive) is True
    # 12 noon UTC — outside.
    assert quiet_hours_active(datetime(2026, 4, 17, 12, 0)) is False


def test_quiet_hours_tz_america_la(monkeypatch):
    """Window is interpreted in the configured zone, not UTC."""
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "22-7")
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS_TZ", "America/Los_Angeles")
    # 03:00 UTC = 20:00 LA (PDT, UTC-7) — outside 22-7 LA window.
    utc_now = datetime(2026, 4, 17, 3, 0, tzinfo=timezone.utc)
    assert quiet_hours_active(utc_now) is False
    # 06:00 UTC = 23:00 LA previous day — INSIDE window.
    utc_late = datetime(2026, 4, 17, 6, 0, tzinfo=timezone.utc)
    assert quiet_hours_active(utc_late) is True


def test_quiet_hours_tz_utc_explicit(monkeypatch):
    """Explicit TZ=UTC behaves identical to unset."""
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "22-7")
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS_TZ", "UTC")
    now_naive = datetime(2026, 4, 17, 3, 0)
    assert quiet_hours_active(now_naive) is True
    # Aware UTC too.
    assert quiet_hours_active(
        datetime(2026, 4, 17, 3, 0, tzinfo=timezone.utc),
    ) is True


def test_quiet_hours_tz_malformed_falls_back_to_utc(monkeypatch):
    """Unknown IANA zone → fallback to UTC (DEBUG-logged, not raised)."""
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "22-7")
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS_TZ", "Not/A_Real_Zone")
    now_naive = datetime(2026, 4, 17, 3, 0)   # UTC 3 AM → inside 22-7
    assert quiet_hours_active(now_naive) is True


def test_quiet_hours_aware_datetime_converted_to_target_tz(monkeypatch):
    """Aware datetime passed in is correctly converted before hour read."""
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "9-17")
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS_TZ", "America/New_York")
    # 14:00 UTC = 10:00 EDT (UTC-4) — inside 9-17 NY window.
    utc_now = datetime(2026, 4, 17, 14, 0, tzinfo=timezone.utc)
    assert quiet_hours_active(utc_now) is True
    # 04:00 UTC = 00:00 EDT — outside.
    assert quiet_hours_active(
        datetime(2026, 4, 17, 4, 0, tzinfo=timezone.utc),
    ) is False


def test_floor_reason_reports_tz(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "22-7")
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS_TZ", "America/Los_Angeles")
    # Pick a time that's inside LA's 22-7 window.
    utc_late = datetime(2026, 4, 17, 6, 0, tzinfo=timezone.utc)
    reason = floor_reason(utc_late)
    assert "tz=America/Los_Angeles" in reason


def test_floor_reason_tz_defaults_to_utc_label(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTO_APPLY_QUIET_HOURS", "22-7")
    reason = floor_reason(datetime(2026, 4, 17, 3, 0))
    # No TZ env set — reason says tz=UTC by default.
    assert "tz=UTC" in reason


# Import timezone at top of the file's datetime import block for the
# above tests.
from datetime import timezone  # noqa: E402
