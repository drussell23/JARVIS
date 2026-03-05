"""Tests for monotonic time helpers."""
import time
import pytest
from backend.core.time_utils import monotonic_ms, monotonic_s, elapsed_since_s, elapsed_since_ms


class TestTimeUtils:
    def test_monotonic_s_returns_float(self):
        result = monotonic_s()
        assert isinstance(result, float)
        assert result > 0

    def test_monotonic_ms_returns_int(self):
        result = monotonic_ms()
        assert isinstance(result, int)
        assert result > 0

    def test_elapsed_since_s(self):
        start = monotonic_s()
        time.sleep(0.05)
        elapsed = elapsed_since_s(start)
        assert 0.04 < elapsed < 0.5

    def test_elapsed_since_ms(self):
        start = monotonic_ms()
        time.sleep(0.05)
        elapsed = elapsed_since_ms(start)
        assert 40 < elapsed < 500

    def test_monotonic_s_is_monotonic(self):
        """Values must never decrease."""
        samples = [monotonic_s() for _ in range(100)]
        for i in range(1, len(samples)):
            assert samples[i] >= samples[i - 1]
