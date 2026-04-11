"""Tests for ClaudeProvider prompt-caching (Phase 3a).

Covers:
    * :meth:`ClaudeProvider._build_cached_system_blocks` — the single
      source-of-truth helper that decides whether to emit a cacheable
      block list or the bare system string.
    * :meth:`ClaudeProvider._record_cache_observation` — cumulative
      telemetry (hits/misses/tokens/$ saved).
    * :meth:`ClaudeProvider.get_cache_stats` — snapshot surface used
      by GovernedLoopService diagnostics.
    * Env-gate behaviour (``JARVIS_CLAUDE_PROMPT_CACHE_ENABLED`` and
      ``JARVIS_CLAUDE_PROMPT_CACHE_MIN_CHARS``).

No Anthropic API is ever called — these tests exercise only the
in-process helpers. Construction uses a dummy api_key because
:meth:`ClaudeProvider.__init__` doesn't actually validate or network
unless the lazy client is built.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterator

import pytest

from backend.core.ouroboros.governance.providers import (
    ClaudeProvider,
    _CLAUDE_INPUT_COST_PER_M,
    _CODEGEN_SYSTEM_PROMPT,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_CACHE_ENV_VARS = (
    "JARVIS_CLAUDE_PROMPT_CACHE_ENABLED",
    "JARVIS_CLAUDE_PROMPT_CACHE_MIN_CHARS",
)


@pytest.fixture(autouse=True)
def _clean_cache_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip cache-related env vars so each test starts from defaults."""
    for key in _CACHE_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    yield


def _make_provider(**overrides: Any) -> ClaudeProvider:
    """Build a ClaudeProvider with safe dummy args for unit tests."""
    kwargs: Dict[str, Any] = {"api_key": "test-key-not-used"}
    kwargs.update(overrides)
    return ClaudeProvider(**kwargs)


# ---------------------------------------------------------------------------
# _build_cached_system_blocks
# ---------------------------------------------------------------------------


class TestBuildCachedSystemBlocks:
    def test_long_prompt_returns_list_with_cache_control(self) -> None:
        p = _make_provider()
        # Default min-chars is 4096; hand over something well above.
        text = "x" * 5000
        result = p._build_cached_system_blocks(text)
        assert isinstance(result, list)
        assert len(result) == 1
        block = result[0]
        assert block["type"] == "text"
        assert block["text"] == text
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_short_prompt_returns_plain_string(self) -> None:
        p = _make_provider()
        result = p._build_cached_system_blocks("too short")
        assert result == "too short"
        assert isinstance(result, str)

    def test_exactly_at_threshold_returns_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_CLAUDE_PROMPT_CACHE_MIN_CHARS", "100")
        p = _make_provider()
        text = "a" * 100
        result = p._build_cached_system_blocks(text)
        assert isinstance(result, list)

    def test_just_below_threshold_returns_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_CLAUDE_PROMPT_CACHE_MIN_CHARS", "100")
        p = _make_provider()
        text = "a" * 99
        result = p._build_cached_system_blocks(text)
        assert result == text

    def test_env_disabled_always_returns_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_CLAUDE_PROMPT_CACHE_ENABLED", "false")
        p = _make_provider()
        text = "x" * 10000  # well above any sane threshold
        result = p._build_cached_system_blocks(text)
        assert result == text

    def test_empty_string_returns_empty_string(self) -> None:
        p = _make_provider()
        assert p._build_cached_system_blocks("") == ""

    def test_non_string_returns_unchanged(self) -> None:
        p = _make_provider()
        # Caller mistake — helper should not explode.
        sentinel: Any = ["already", "blocks"]
        assert p._build_cached_system_blocks(sentinel) is sentinel

    def test_real_codegen_prompt_is_cached(self) -> None:
        """The real _CODEGEN_SYSTEM_PROMPT should always hit the cache path.

        If this ever fails, someone has trimmed the system prompt below
        the 4096-char threshold and caching is effectively disabled.
        """
        p = _make_provider()
        result = p._build_cached_system_blocks(_CODEGEN_SYSTEM_PROMPT)
        assert isinstance(result, list), (
            f"System prompt is only {len(_CODEGEN_SYSTEM_PROMPT)} chars — "
            f"falls below cache threshold of {p._prompt_cache_min_chars}"
        )

    @pytest.mark.parametrize(
        "value, expected_enabled",
        [
            ("true", True),
            ("1", True),
            ("yes", True),
            ("on", True),
            ("TRUE", True),
            ("false", False),
            ("0", False),
            ("no", False),
            ("off", False),
            ("FALSE", False),
        ],
    )
    def test_env_enabled_parsing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
        expected_enabled: bool,
    ) -> None:
        monkeypatch.setenv("JARVIS_CLAUDE_PROMPT_CACHE_ENABLED", value)
        p = _make_provider()
        assert p._prompt_cache_enabled is expected_enabled

    def test_malformed_min_chars_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "JARVIS_CLAUDE_PROMPT_CACHE_MIN_CHARS", "not-a-number"
        )
        p = _make_provider()
        # Falls back to 4096 default on ValueError.
        assert p._prompt_cache_min_chars == 4096


# ---------------------------------------------------------------------------
# _record_cache_observation
# ---------------------------------------------------------------------------


class TestRecordCacheObservation:
    def test_hit_increments_hit_counters(self) -> None:
        p = _make_provider()
        p._record_cache_observation(input_tokens=1000, cached_tokens=600)
        stats = p._cache_stats
        assert stats["total_calls"] == 1
        assert stats["hits"] == 1
        assert stats["misses"] == 0
        assert stats["cached_tokens"] == 600
        assert stats["uncached_tokens"] == 400
        assert stats["usd_saved"] > 0

    def test_miss_increments_miss_counter(self) -> None:
        p = _make_provider()
        p._record_cache_observation(input_tokens=500, cached_tokens=0)
        stats = p._cache_stats
        assert stats["total_calls"] == 1
        assert stats["hits"] == 0
        assert stats["misses"] == 1
        assert stats["cached_tokens"] == 0
        assert stats["uncached_tokens"] == 500
        assert stats["usd_saved"] == 0.0

    def test_usd_saved_matches_formula(self) -> None:
        p = _make_provider()
        cached = 100_000  # 100K tokens
        p._record_cache_observation(input_tokens=cached, cached_tokens=cached)
        # Formula: (cached / 1M) × (3.00 - 0.30) = 0.1 × 2.70 = 0.27
        expected = (cached / 1_000_000) * (_CLAUDE_INPUT_COST_PER_M - 0.30)
        assert p._cache_stats["usd_saved"] == pytest.approx(expected)

    def test_stats_accumulate_across_calls(self) -> None:
        p = _make_provider()
        p._record_cache_observation(input_tokens=1000, cached_tokens=400)
        p._record_cache_observation(input_tokens=2000, cached_tokens=1500)
        p._record_cache_observation(input_tokens=500, cached_tokens=0)  # miss
        stats = p._cache_stats
        assert stats["total_calls"] == 3
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["cached_tokens"] == 1900
        assert stats["uncached_tokens"] == 1600  # 600 + 500 + 500

    def test_cached_exceeding_input_is_clamped(self) -> None:
        """Defensive: if API returns cached>input (shouldn't happen), clamp."""
        p = _make_provider()
        p._record_cache_observation(input_tokens=100, cached_tokens=999)
        stats = p._cache_stats
        # Clamped to input_tokens — never negative uncached.
        assert stats["cached_tokens"] == 100
        assert stats["uncached_tokens"] == 0

    def test_non_numeric_args_are_swallowed(self) -> None:
        p = _make_provider()
        # type: ignore[arg-type] — deliberately wrong type
        p._record_cache_observation(input_tokens="nope", cached_tokens=None)  # type: ignore[arg-type]
        # No crash; no stats moved.
        assert p._cache_stats["total_calls"] == 0

    def test_none_inputs_treated_as_zero(self) -> None:
        p = _make_provider()
        p._record_cache_observation(input_tokens=0, cached_tokens=0)
        assert p._cache_stats["total_calls"] == 1
        assert p._cache_stats["misses"] == 1


# ---------------------------------------------------------------------------
# get_cache_stats
# ---------------------------------------------------------------------------


class TestGetCacheStats:
    def test_initial_snapshot_shape(self) -> None:
        p = _make_provider()
        stats = p.get_cache_stats()
        expected_keys = {
            "hits", "misses", "total_calls",
            "cached_tokens", "uncached_tokens",
            "usd_saved", "enabled", "min_chars",
            "hit_rate", "cache_coverage",
        }
        assert expected_keys.issubset(stats.keys())

    def test_initial_hit_rate_is_zero(self) -> None:
        p = _make_provider()
        stats = p.get_cache_stats()
        assert stats["hit_rate"] == 0.0
        assert stats["cache_coverage"] == 0.0

    def test_hit_rate_computed_correctly(self) -> None:
        p = _make_provider()
        p._record_cache_observation(1000, 500)  # hit
        p._record_cache_observation(1000, 500)  # hit
        p._record_cache_observation(1000, 0)    # miss
        stats = p.get_cache_stats()
        assert stats["hit_rate"] == pytest.approx(2 / 3)

    def test_cache_coverage_computed_correctly(self) -> None:
        p = _make_provider()
        p._record_cache_observation(1000, 600)
        p._record_cache_observation(1000, 400)
        stats = p.get_cache_stats()
        # 1000 cached of 2000 total input = 0.5
        assert stats["cache_coverage"] == pytest.approx(0.5)

    def test_snapshot_is_defensive_copy(self) -> None:
        """Callers must not be able to mutate the live counter dict."""
        p = _make_provider()
        p._record_cache_observation(1000, 500)
        snap = p.get_cache_stats()
        snap["hits"] = 9999  # try to poison the live dict
        fresh = p.get_cache_stats()
        assert fresh["hits"] == 1

    def test_enabled_and_min_chars_reflected_in_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_CLAUDE_PROMPT_CACHE_ENABLED", "false")
        monkeypatch.setenv("JARVIS_CLAUDE_PROMPT_CACHE_MIN_CHARS", "8192")
        p = _make_provider()
        stats = p.get_cache_stats()
        assert stats["enabled"] is False
        assert stats["min_chars"] == 8192


# ---------------------------------------------------------------------------
# Provider __init__ wiring
# ---------------------------------------------------------------------------


class TestInitWiring:
    def test_default_enabled(self) -> None:
        p = _make_provider()
        assert p._prompt_cache_enabled is True
        assert p._prompt_cache_min_chars == 4096

    def test_disable_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_CLAUDE_PROMPT_CACHE_ENABLED", "false")
        p = _make_provider()
        assert p._prompt_cache_enabled is False

    def test_custom_min_chars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_CLAUDE_PROMPT_CACHE_MIN_CHARS", "2048")
        p = _make_provider()
        assert p._prompt_cache_min_chars == 2048

    def test_negative_min_chars_clamped_to_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_CLAUDE_PROMPT_CACHE_MIN_CHARS", "-500")
        p = _make_provider()
        assert p._prompt_cache_min_chars == 0

    def test_stats_initialized_zero(self) -> None:
        p = _make_provider()
        stats = p._cache_stats
        for counter in ("hits", "misses", "total_calls", "cached_tokens",
                        "uncached_tokens"):
            assert stats[counter] == 0
        assert stats["usd_saved"] == 0.0
