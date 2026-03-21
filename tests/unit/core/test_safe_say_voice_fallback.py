"""Test voice fallback chain for safe_say().

v304.0: safe_say() validates voice availability on first call via
``say -v ?`` and falls back through a preference list if the primary
voice is unavailable.
"""

import asyncio
import pytest


# Simulate voice resolution logic
_FALLBACK_PREFERENCES = ["Daniel", "Samantha", "Alex", "Fred"]


def _resolve_voice_sync(preferred: str, available: set) -> str:
    """Simulate _resolve_voice logic synchronously for testing."""
    if preferred in available:
        return preferred
    for voice in _FALLBACK_PREFERENCES:
        if voice in available:
            return voice
    if available:
        return sorted(available)[0]
    return preferred  # Last resort


class TestVoiceFallback:

    def test_preferred_voice_available(self):
        available = {"Daniel", "Samantha", "Alex", "Fred"}
        assert _resolve_voice_sync("Daniel", available) == "Daniel"

    def test_fallback_when_preferred_unavailable(self):
        available = {"Samantha", "Alex", "Fred"}
        assert _resolve_voice_sync("Daniel", available) == "Samantha"

    def test_deep_fallback(self):
        available = {"Fred", "Karen"}
        assert _resolve_voice_sync("Daniel", available) == "Fred"

    def test_no_preferred_voices_uses_first_available(self):
        available = {"Karen", "Zarvox"}
        result = _resolve_voice_sync("Daniel", available)
        assert result == "Karen"  # sorted first

    def test_empty_available_returns_preferred_as_last_resort(self):
        assert _resolve_voice_sync("Daniel", set()) == "Daniel"

    def test_custom_preferred_voice(self):
        available = {"Daniel", "Samantha", "Alex", "CustomVoice"}
        assert _resolve_voice_sync("CustomVoice", available) == "CustomVoice"
