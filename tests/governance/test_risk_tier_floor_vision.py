"""Regression spine for VisionSensor recognition in ``risk_tier_floor``.

Task 6 of the VisionSensor + Visual VERIFY implementation plan. Pins
Invariant I2:

    Vision-originated ops never reach ``safe_auto``. Floor is
    ``notify_apply``. Enforced by ``risk_tier_floor.py`` via a new
    ``SignalSource.VISION_SENSOR`` rule, not by polite convention.
    ``JARVIS_VISION_SENSOR_RISK_FLOOR`` env tunable upward only
    (``notify_apply`` → ``approval_required`` / ``blocked``), never
    downward.

Spec: ``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§Invariant I2 + §Policy Layer → "When supervisor must not auto-act".
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance.intent.signals import SignalSource
from backend.core.ouroboros.governance.risk_tier_floor import (
    _ENV_VISION_FLOOR,
    _VISION_SENSOR_HARD_FLOOR,
    _VISION_SENSOR_SOURCE,
    _vision_floor_from_env,
    apply_floor_to_name,
    floor_reason,
    recommended_floor,
)


# ---------------------------------------------------------------------------
# Fixtures — ensure each test starts with a clean env
# ---------------------------------------------------------------------------


_ENV_KEYS = (
    "JARVIS_MIN_RISK_TIER",
    "JARVIS_PARANOIA_MODE",
    "JARVIS_AUTO_APPLY_QUIET_HOURS",
    "JARVIS_AUTO_APPLY_QUIET_HOURS_TZ",
    _ENV_VISION_FLOOR,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear every knob this module reads before each test."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# Module-level constants pinned
# ---------------------------------------------------------------------------


def test_vision_sensor_source_string_matches_enum_value():
    """The module stringly-matches ``"vision_sensor"`` to avoid an
    import cycle. The contract: the string must equal
    ``SignalSource.VISION_SENSOR.value`` exactly."""
    assert _VISION_SENSOR_SOURCE == SignalSource.VISION_SENSOR.value


def test_vision_sensor_hard_floor_is_notify_apply():
    assert _VISION_SENSOR_HARD_FLOOR == "notify_apply"


# ---------------------------------------------------------------------------
# _vision_floor_from_env — env parsing + I2 enforcement
# ---------------------------------------------------------------------------


def test_vision_floor_defaults_to_notify_apply_when_env_unset():
    assert _vision_floor_from_env() == "notify_apply"


@pytest.mark.parametrize("tier", ["notify_apply", "approval_required", "blocked"])
def test_vision_floor_accepts_upward_values(monkeypatch, tier):
    monkeypatch.setenv(_ENV_VISION_FLOOR, tier)
    assert _vision_floor_from_env() == tier


def test_vision_floor_case_insensitive(monkeypatch):
    monkeypatch.setenv(_ENV_VISION_FLOOR, "APPROVAL_REQUIRED")
    assert _vision_floor_from_env() == "approval_required"


def test_vision_floor_whitespace_tolerant(monkeypatch):
    monkeypatch.setenv(_ENV_VISION_FLOOR, "  notify_apply  ")
    assert _vision_floor_from_env() == "notify_apply"


def test_vision_floor_rejects_safe_auto(monkeypatch):
    monkeypatch.setenv(_ENV_VISION_FLOOR, "safe_auto")
    with pytest.raises(ValueError, match="cannot be lower than 'notify_apply'"):
        _vision_floor_from_env()


def test_vision_floor_rejects_safe_auto_uppercase(monkeypatch):
    # Case-insensitive rejection — no "spell it SAFE_AUTO to bypass" loophole.
    monkeypatch.setenv(_ENV_VISION_FLOOR, "SAFE_AUTO")
    with pytest.raises(ValueError, match="cannot be lower"):
        _vision_floor_from_env()


def test_vision_floor_ignores_unknown_values(monkeypatch):
    """Typo → debug log + default. Only an *explicit* weaker tier raises."""
    monkeypatch.setenv(_ENV_VISION_FLOOR, "maximum_paranoia")
    assert _vision_floor_from_env() == "notify_apply"


# ---------------------------------------------------------------------------
# recommended_floor — signal_source plumbing
# ---------------------------------------------------------------------------


def test_recommended_floor_without_signal_source_returns_none_when_env_clean():
    assert recommended_floor() is None


def test_recommended_floor_vision_source_forces_notify_apply_when_env_unset():
    floor = recommended_floor(signal_source=SignalSource.VISION_SENSOR.value)
    assert floor == "notify_apply"


def test_recommended_floor_vision_source_accepts_enum_member():
    # StrEnum member interop — passing the enum itself must work.
    floor = recommended_floor(signal_source=SignalSource.VISION_SENSOR)
    assert floor == "notify_apply"


def test_recommended_floor_vision_source_env_upgrade_to_approval_required(monkeypatch):
    monkeypatch.setenv(_ENV_VISION_FLOOR, "approval_required")
    floor = recommended_floor(signal_source=SignalSource.VISION_SENSOR.value)
    assert floor == "approval_required"


def test_recommended_floor_vision_source_env_upgrade_to_blocked(monkeypatch):
    monkeypatch.setenv(_ENV_VISION_FLOOR, "blocked")
    floor = recommended_floor(signal_source=SignalSource.VISION_SENSOR.value)
    assert floor == "blocked"


def test_recommended_floor_vision_source_rejects_weaker_env(monkeypatch):
    monkeypatch.setenv(_ENV_VISION_FLOOR, "safe_auto")
    with pytest.raises(ValueError, match="cannot be lower than 'notify_apply'"):
        recommended_floor(signal_source=SignalSource.VISION_SENSOR.value)


def test_recommended_floor_non_vision_signal_source_ignored():
    """Other sources must not accidentally trip the vision floor logic."""
    floor = recommended_floor(signal_source="test_failure")
    assert floor is None
    floor = recommended_floor(signal_source="ai_miner")
    assert floor is None


def test_recommended_floor_vision_source_case_insensitive():
    assert (
        recommended_floor(signal_source="VISION_SENSOR")
        == recommended_floor(signal_source="vision_sensor")
    )


def test_recommended_floor_vision_composes_with_min_risk_tier_strictest_wins(
    monkeypatch,
):
    # Hard floor from explicit env is already approval_required.
    # Vision source also would apply notify_apply. Strictest (approval_required)
    # must win via the existing composition.
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
    floor = recommended_floor(signal_source="vision_sensor")
    assert floor == "approval_required"


def test_recommended_floor_vision_composes_with_paranoia_stays_notify_apply(
    monkeypatch,
):
    # Both imply notify_apply — result is notify_apply, not stronger.
    monkeypatch.setenv("JARVIS_PARANOIA_MODE", "1")
    floor = recommended_floor(signal_source="vision_sensor")
    assert floor == "notify_apply"


def test_recommended_floor_vision_with_upward_env_beats_paranoia(monkeypatch):
    # Vision env says blocked; paranoia says notify_apply → blocked wins.
    monkeypatch.setenv("JARVIS_PARANOIA_MODE", "1")
    monkeypatch.setenv(_ENV_VISION_FLOOR, "blocked")
    floor = recommended_floor(signal_source="vision_sensor")
    assert floor == "blocked"


# ---------------------------------------------------------------------------
# apply_floor_to_name — vision source upgrade behavior
# ---------------------------------------------------------------------------


def test_apply_floor_upgrades_safe_auto_when_vision_source():
    effective, applied = apply_floor_to_name(
        "safe_auto", signal_source=SignalSource.VISION_SENSOR.value,
    )
    assert effective == "notify_apply"
    assert applied == "notify_apply"


def test_apply_floor_leaves_notify_apply_unchanged_when_vision_source():
    effective, applied = apply_floor_to_name(
        "notify_apply", signal_source="vision_sensor",
    )
    assert effective == "notify_apply"
    assert applied is None  # no upgrade happened


def test_apply_floor_does_not_downgrade_stronger_tier():
    effective, applied = apply_floor_to_name(
        "approval_required", signal_source="vision_sensor",
    )
    assert effective == "approval_required"
    assert applied is None


def test_apply_floor_upgrades_to_blocked_when_env_says_blocked(monkeypatch):
    monkeypatch.setenv(_ENV_VISION_FLOOR, "blocked")
    effective, applied = apply_floor_to_name(
        "safe_auto", signal_source="vision_sensor",
    )
    assert effective == "blocked"
    assert applied == "blocked"


def test_apply_floor_non_vision_signal_passes_through_safe_auto():
    # Non-vision signal + clean env → safe_auto stays safe_auto.
    effective, applied = apply_floor_to_name("safe_auto", signal_source="test_failure")
    assert effective == "safe_auto"
    assert applied is None


def test_apply_floor_raises_when_vision_env_weaker(monkeypatch):
    monkeypatch.setenv(_ENV_VISION_FLOOR, "safe_auto")
    with pytest.raises(ValueError, match="cannot be lower"):
        apply_floor_to_name("safe_auto", signal_source="vision_sensor")


def test_apply_floor_unknown_input_tier_passes_through():
    effective, applied = apply_floor_to_name("bogus_tier", signal_source="vision_sensor")
    assert effective == "bogus_tier"
    assert applied is None


# ---------------------------------------------------------------------------
# floor_reason — vision observability
# ---------------------------------------------------------------------------


def test_floor_reason_mentions_vision_source_when_passed():
    reason = floor_reason(signal_source="vision_sensor")
    assert "vision_sensor" in reason
    assert "notify_apply" in reason
    assert "I2" in reason


def test_floor_reason_vision_with_invalid_env_flags_invalid(monkeypatch):
    monkeypatch.setenv(_ENV_VISION_FLOOR, "safe_auto")
    reason = floor_reason(signal_source="vision_sensor")
    assert "INVALID" in reason
    assert "safe_auto" in reason


def test_floor_reason_no_signal_returns_no_floor_active_when_env_clean():
    assert floor_reason() == "(no floor active)"


def test_floor_reason_non_vision_signal_source_does_not_add_vision_line():
    reason = floor_reason(signal_source="test_failure")
    assert "vision_sensor" not in reason


# ---------------------------------------------------------------------------
# Backward compatibility — existing callers unchanged
# ---------------------------------------------------------------------------


def test_recommended_floor_back_compat_no_kwarg():
    """Existing callers that never pass ``signal_source`` must still get
    ``None`` when the environment is clean, matching pre-Task-6 behavior.
    """
    assert recommended_floor() is None


def test_apply_floor_back_compat_no_kwarg():
    effective, applied = apply_floor_to_name("safe_auto")
    assert effective == "safe_auto"
    assert applied is None


def test_floor_reason_back_compat_no_kwarg():
    assert floor_reason() == "(no floor active)"
