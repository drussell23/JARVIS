"""Task 7 — SensorGovernor operator-active hard-zero (spec §5.4).

Tests the pure helper ``_operator_hard_zero`` and the behavioral
path through ``SensorGovernor.request_budget`` when an
``operator_active_fn`` is injected.

Byte-identical invariant:
  * When ``JARVIS_OPERATOR_YIELD_ENABLED`` is off → helper always False.
  * When callable is None → no hard-zero regardless of env flag.
  * When callable returns False → no hard-zero.
"""
from __future__ import annotations

import os

import pytest

import backend.core.ouroboros.governance.sensor_governor as sg
from backend.core.ouroboros.governance.sensor_governor import (
    BudgetDecision,
    SensorBudgetSpec,
    SensorGovernor,
    Urgency,
    reset_default_governor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Wipe all governor env vars between tests."""
    for key in list(os.environ):
        if key.startswith("JARVIS_SENSOR_GOVERNOR") or key == "JARVIS_OPERATOR_YIELD_ENABLED":
            monkeypatch.delenv(key, raising=False)
    reset_default_governor()
    yield
    reset_default_governor()


def _make_spec(name: str = "TestSensor", cap: int = 10) -> SensorBudgetSpec:
    return SensorBudgetSpec(sensor_name=name, base_cap_per_hour=cap)


# ---------------------------------------------------------------------------
# Pure helper — _operator_hard_zero
# ---------------------------------------------------------------------------


class TestOperatorHardZeroHelper:

    def test_hard_zero_true_when_yield_enabled_and_active(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
        assert sg._operator_hard_zero(True) is True

    def test_hard_zero_false_when_yield_enabled_but_inactive(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
        assert sg._operator_hard_zero(False) is False

    def test_hard_zero_off_when_yield_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "false")
        # Even with operator active, yield disabled → never hard-zero
        assert sg._operator_hard_zero(True) is False

    def test_hard_zero_off_when_yield_env_missing(self, monkeypatch):
        monkeypatch.delenv("JARVIS_OPERATOR_YIELD_ENABLED", raising=False)
        # Default is false → never hard-zero
        assert sg._operator_hard_zero(True) is False

    def test_accepts_1_yes_on_as_truthy_flag(self, monkeypatch):
        for truthy in ("1", "yes", "on", "TRUE", "True"):
            monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", truthy)
            assert sg._operator_hard_zero(True) is True, f"failed for {truthy!r}"

    def test_operator_false_with_1_flag(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "1")
        assert sg._operator_hard_zero(False) is False


# ---------------------------------------------------------------------------
# Behavioral — SensorGovernor.request_budget with operator_active_fn
# ---------------------------------------------------------------------------


class TestGovernorOperatorYieldBehavioral:

    def test_request_budget_denied_when_operator_active(self, monkeypatch):
        """operator_active_fn=lambda: True + yield enabled → denied."""
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

        gov = SensorGovernor(
            posture_fn=lambda: None,
            signal_bundle_fn=lambda: None,
            operator_active_fn=lambda: True,
        )
        spec = _make_spec()
        gov.register(spec)

        decision = gov.request_budget("TestSensor", Urgency.STANDARD)
        assert isinstance(decision, BudgetDecision)
        assert decision.allowed is False
        assert decision.reason_code == "governor.operator_active_yield"

    def test_request_budget_allowed_when_operator_inactive(self, monkeypatch):
        """operator_active_fn=lambda: False → normal path (budget not exhausted)."""
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

        gov = SensorGovernor(
            posture_fn=lambda: None,
            signal_bundle_fn=lambda: None,
            operator_active_fn=lambda: False,
        )
        spec = _make_spec()
        gov.register(spec)

        decision = gov.request_budget("TestSensor", Urgency.STANDARD)
        assert decision.allowed is True

    def test_request_budget_allowed_when_callable_none(self, monkeypatch):
        """No operator_active_fn → byte-identical to pre-feature (allowed)."""
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

        gov = SensorGovernor(
            posture_fn=lambda: None,
            signal_bundle_fn=lambda: None,
        )
        spec = _make_spec()
        gov.register(spec)

        decision = gov.request_budget("TestSensor", Urgency.STANDARD)
        assert decision.allowed is True

    def test_request_budget_allowed_when_yield_disabled(self, monkeypatch):
        """operator_active_fn raises True but yield flag is off → allowed."""
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "false")

        gov = SensorGovernor(
            posture_fn=lambda: None,
            signal_bundle_fn=lambda: None,
            operator_active_fn=lambda: True,
        )
        spec = _make_spec()
        gov.register(spec)

        decision = gov.request_budget("TestSensor", Urgency.STANDARD)
        assert decision.allowed is True

    def test_operator_yield_takes_priority_over_remaining_budget(self, monkeypatch):
        """Hard-zero fires even when per-sensor budget still has headroom."""
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

        gov = SensorGovernor(
            posture_fn=lambda: None,
            signal_bundle_fn=lambda: None,
            operator_active_fn=lambda: True,
        )
        # Large budget so we wouldn't be denied for capacity reasons
        spec = _make_spec(cap=10_000)
        gov.register(spec)

        decision = gov.request_budget("TestSensor", Urgency.STANDARD)
        assert decision.allowed is False
        assert decision.reason_code == "governor.operator_active_yield"

    def test_operator_fn_exception_treated_as_inactive(self, monkeypatch):
        """Fail-soft: if operator_active_fn raises, treat as inactive (allow)."""
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

        def _raising_fn() -> bool:
            raise RuntimeError("broken callable")

        gov = SensorGovernor(
            posture_fn=lambda: None,
            signal_bundle_fn=lambda: None,
            operator_active_fn=_raising_fn,
        )
        spec = _make_spec()
        gov.register(spec)

        # Should NOT raise; should fall through to normal allowed path
        decision = gov.request_budget("TestSensor", Urgency.STANDARD)
        assert decision.allowed is True

    def test_governor_disabled_bypasses_operator_yield(self, monkeypatch):
        """Master governor flag off → always allowed, operator yield irrelevant."""
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "false")
        monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

        gov = SensorGovernor(
            posture_fn=lambda: None,
            signal_bundle_fn=lambda: None,
            operator_active_fn=lambda: True,
        )
        spec = _make_spec()
        gov.register(spec)

        decision = gov.request_budget("TestSensor", Urgency.STANDARD)
        assert decision.allowed is True
        assert decision.reason_code == "governor.disabled"
