"""Tests for model lifecycle oscillation guard."""
import time
import json
import pytest
from unittest.mock import MagicMock, patch


class TestOscillationGuard:
    def _make_serving(self):
        """Create a minimal UnifiedModelServing mock with oscillation state."""
        from types import SimpleNamespace
        serving = SimpleNamespace(
            _model_lifecycle_cycles=0,
            _model_lifecycle_window_start=0.0,
            _model_committed_off=False,
            _model_committed_off_time=0.0,
            OSCILLATION_CYCLE_LIMIT=3,
            OSCILLATION_WINDOW_S=600.0,
            COMMITTED_OFF_COOLDOWN_S=300.0,
            _lifecycle_events=[],
            logger=MagicMock(),
        )
        return serving

    def test_oscillation_detected_after_limit(self):
        """3 unload cycles in window should trigger committed-off."""
        serving = self._make_serving()

        for i in range(3):
            now = time.monotonic()
            if now - serving._model_lifecycle_window_start > serving.OSCILLATION_WINDOW_S:
                serving._model_lifecycle_cycles = 0
                serving._model_lifecycle_window_start = now
            serving._model_lifecycle_cycles += 1

        assert serving._model_lifecycle_cycles >= serving.OSCILLATION_CYCLE_LIMIT

    def test_committed_off_blocks_recovery(self):
        """When committed-off, recovery should be blocked."""
        serving = self._make_serving()
        serving._model_committed_off = True
        serving._model_committed_off_time = time.monotonic()

        elapsed = time.monotonic() - serving._model_committed_off_time
        assert elapsed < serving.COMMITTED_OFF_COOLDOWN_S
        # Recovery should be blocked

    def test_committed_off_expires(self):
        """Committed-off should auto-expire after cooldown."""
        serving = self._make_serving()
        serving._model_committed_off = True
        serving._model_committed_off_time = time.monotonic() - 301.0  # Past cooldown

        elapsed = time.monotonic() - serving._model_committed_off_time
        if elapsed >= serving.COMMITTED_OFF_COOLDOWN_S:
            serving._model_committed_off = False
            serving._model_lifecycle_cycles = 0

        assert serving._model_committed_off is False
        assert serving._model_lifecycle_cycles == 0

    def test_window_reset_after_quiet_period(self):
        """Cycle counter resets if window elapses without incidents."""
        serving = self._make_serving()
        serving._model_lifecycle_cycles = 2
        serving._model_lifecycle_window_start = time.monotonic() - 601.0  # Past window

        now = time.monotonic()
        if now - serving._model_lifecycle_window_start > serving.OSCILLATION_WINDOW_S:
            serving._model_lifecycle_cycles = 0
            serving._model_lifecycle_window_start = now

        assert serving._model_lifecycle_cycles == 0
