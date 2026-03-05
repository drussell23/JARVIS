"""Monotonic time helpers -- prevents new datetime.now() duration bugs.

All duration/elapsed calculations in JARVIS should use these helpers
instead of datetime.now() to avoid NTP clock adjustment corruption.
"""
import time


def monotonic_ms() -> int:
    """Current monotonic time in milliseconds."""
    return int(time.monotonic() * 1000)


def monotonic_s() -> float:
    """Current monotonic time in seconds."""
    return time.monotonic()


def elapsed_since_s(start_mono: float) -> float:
    """Seconds elapsed since a monotonic start time."""
    return time.monotonic() - start_mono


def elapsed_since_ms(start_mono_ms: int) -> int:
    """Milliseconds elapsed since a monotonic start time."""
    return int(time.monotonic() * 1000) - start_mono_ms
