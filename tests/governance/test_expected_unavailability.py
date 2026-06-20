"""BudgetSynth log-severity classification: expected (autarky) vs abnormal
(breaker degradation). Live noise fix 2026-06-20 — claude=structurally_disabled
was logged WARNING-per-op despite being the operator-intended steady state."""
from __future__ import annotations

from backend.core.ouroboros.governance.provider_availability import (
    is_expected_unavailability,
)


def test_structurally_disabled_is_expected():
    # Operator autarky (JARVIS_PROVIDER_CLAUDE_DISABLED) → not a warning.
    assert is_expected_unavailability("structurally_disabled") is True


def test_breaker_disabled_is_expected():
    assert is_expected_unavailability("breaker_disabled") is True


def test_economic_breaker_is_abnormal():
    assert is_expected_unavailability("breaker_open_economic") is False
    assert is_expected_unavailability("breaker_open_economic_persisted") is False


def test_transport_breaker_is_abnormal():
    assert is_expected_unavailability("breaker_open_transport") is False


def test_generic_breaker_open_is_abnormal():
    assert is_expected_unavailability("breaker_open") is False


def test_half_open_probing_is_abnormal():
    assert is_expected_unavailability("half_open_probing") is False


def test_case_insensitive_and_whitespace():
    assert is_expected_unavailability("  STRUCTURALLY_DISABLED  ") is True


def test_empty_and_none_are_not_expected():
    assert is_expected_unavailability("") is False
    assert is_expected_unavailability(None) is False


def test_unknown_reason_defaults_abnormal():
    # An unrecognized reason errs toward WARNING (surface the unknown).
    assert is_expected_unavailability("some_new_reason") is False
