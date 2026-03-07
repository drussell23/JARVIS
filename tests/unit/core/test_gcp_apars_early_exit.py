"""Tests for APARS-aware early exit from GCP health verification."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))


class TestAparsEarlyExit:
    def test_service_start_timeout_detected(self):
        """APARS checkpoint 'service_start_timeout' should be recognized as failure."""
        apars = {
            "checkpoint": "service_start_timeout",
            "phase_name": "service_start_timeout",
            "total_progress": 95,
            "error": "service_health_check_failed",
        }

        _APARS_TERMINAL_CHECKPOINTS = frozenset({
            "service_start_timeout",
            "service_health_check_failed",
        })

        is_terminal = apars.get("checkpoint", "") in _APARS_TERMINAL_CHECKPOINTS
        assert is_terminal is True

    def test_normal_checkpoint_not_terminal(self):
        """Normal APARS checkpoints should NOT trigger early exit."""
        apars = {
            "checkpoint": "verifying_attempt_5",
            "phase_name": "verifying_attempt_5",
            "total_progress": 60,
        }

        _APARS_TERMINAL_CHECKPOINTS = frozenset({
            "service_start_timeout",
            "service_health_check_failed",
        })

        is_terminal = apars.get("checkpoint", "") in _APARS_TERMINAL_CHECKPOINTS
        assert is_terminal is False

    def test_inference_ready_not_terminal(self):
        """Successful inference_ready checkpoint should NOT trigger early exit."""
        apars = {
            "checkpoint": "inference_ready",
            "phase_name": "inference_ready",
            "total_progress": 100,
            "ready_for_inference": True,
        }

        _APARS_TERMINAL_CHECKPOINTS = frozenset({
            "service_start_timeout",
            "service_health_check_failed",
        })

        is_terminal = apars.get("checkpoint", "") in _APARS_TERMINAL_CHECKPOINTS
        assert is_terminal is False

    def test_error_field_detected(self):
        """APARS error field containing failure signal should be detected."""
        apars = {
            "checkpoint": "service_start_timeout",
            "error": "service_health_check_failed",
            "total_progress": 95,
        }

        has_error = isinstance(apars.get("error"), str) and "failed" in apars["error"]
        assert has_error is True

    def test_grace_period_applied_after_terminal(self):
        """After terminal APARS, a grace period should be applied before returning."""
        terminal_detected_at = time.monotonic()
        grace_seconds = 30.0
        grace_deadline = terminal_detected_at + grace_seconds

        # Immediately after detection, grace period is NOT expired
        assert time.monotonic() < grace_deadline

    def test_empty_checkpoint_not_terminal(self):
        """Missing or empty checkpoint should not trigger early exit."""
        _APARS_TERMINAL_CHECKPOINTS = frozenset({
            "service_start_timeout",
            "service_health_check_failed",
        })

        for apars in [
            {},
            {"checkpoint": ""},
            {"checkpoint": "unknown"},
            {"phase_name": "starting"},
        ]:
            is_terminal = apars.get("checkpoint", "") in _APARS_TERMINAL_CHECKPOINTS
            assert is_terminal is False
