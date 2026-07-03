"""Tests for DoublewordProvider prompt caching (cache_control on the stable
prefix) + opt-in Zero Data Retention (ZDR).

DoubleWord is O+V's PRIMARY provider. It supports Anthropic-style
``cache_control`` markers on content blocks (5m / 1h TTL, ~90% off cached
input tokens). This module proves:

    * ``_dw_shape_cached_system`` marks ONLY when enabled AND the prefix meets
      the ~1024-token floor; else returns the plain string (byte-identical).
    * ``_dw_prompt_cache_ttl`` maps ``5m`` / ``1h`` (default ``1h``).
    * The Slice-131 ``stable_prefix_out`` split is reused via
      ``force_prefix_split`` so ONLY the stable tool catalog + output schema
      ride the cached block — the volatile per-op content (incl. the
      "Recent Development Momentum" git-log digest) stays OUTSIDE it.
    * ``DoublewordProvider._dw_build_system_content`` folds the stable prefix
      into the cached system block (RT) and is byte-identical legacy when off.
    * ZDR: ``JARVIS_DW_ZDR_ENABLED=true`` emits the ZDR request header; false
      → absent. Fail-soft throughout.

No DoubleWord API is ever called — these exercise only in-process helpers.
"""

from __future__ import annotations

import os
from typing import Any, Iterator, List, Tuple

import pytest

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance import doubleword_provider as dw
from backend.core.ouroboros.governance.doubleword_provider import (
    DoublewordProvider,
    _DW_PROMPT_CACHE_MIN_CHARS_DEFAULT,
    _DW_ZDR_HEADER,
    _DW_ZDR_HEADER_VALUE,
    _dw_apply_zdr,
    _dw_prompt_cache_enabled,
    _dw_prompt_cache_min_chars,
    _dw_prompt_cache_ttl,
    _dw_shape_cached_system,
    dw_zdr_enabled,
    dw_zdr_request_headers,
)


_CACHE_ENV_VARS = (
    "JARVIS_DW_PROMPT_CACHE_ENABLED",
    "JARVIS_DW_PROMPT_CACHE_TTL",
    "JARVIS_DW_PROMPT_CACHE_MIN_CHARS",
    "JARVIS_DW_ZDR_ENABLED",
    "JARVIS_PROMPT_PREFIX_CACHE_ENABLED",
    "JARVIS_CLAUDE_PROMPT_CACHE_MIN_CHARS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in _CACHE_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    yield


def _provider() -> DoublewordProvider:
    return DoublewordProvider(api_key="test-key-not-used")


def _ctx(
    *,
    description: str = "Update the widget renderer",
    target_files: Tuple[str, ...] = ("tests/test_utils.py",),
    session_lessons: str = "",
    human_instructions: str = "",
) -> OperationContext:
    import dataclasses

    c = OperationContext.create(
        target_files=target_files,
        description=description,
        op_id="op-dw-cache-001",
    )
    # OperationContext is frozen — ``session_lessons`` / ``human_instructions``
    # land in the VOLATILE parts of the prompt.
    return dataclasses.replace(
        c,
        session_lessons=session_lessons,
        human_instructions=human_instructions,
        provider_route="standard",
    )


# ---------------------------------------------------------------------------
# _dw_shape_cached_system
# ---------------------------------------------------------------------------


class TestShapeCachedSystem:
    def test_enabled_long_prefix_marks_cache_control(self) -> None:
        text = "S" * 5000
        out = _dw_shape_cached_system(
            text, enabled=True, min_chars=4096, ttl="1h"
        )
        assert isinstance(out, list) and len(out) == 1
        block = out[0]
        assert block["type"] == "text"
        assert block["text"] == text  # byte-identical text
        assert block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_ttl_5m_maps_to_marker(self) -> None:
        out = _dw_shape_cached_system(
            "S" * 5000, enabled=True, min_chars=4096, ttl="5m"
        )
        assert out[0]["cache_control"]["ttl"] == "5m"

    def test_disabled_returns_plain_string(self) -> None:
        text = "S" * 5000
        out = _dw_shape_cached_system(
            text, enabled=False, min_chars=4096, ttl="1h"
        )
        assert out == text and isinstance(out, str)

    def test_below_floor_returns_plain_string(self) -> None:
        text = "short"
        out = _dw_shape_cached_system(
            text, enabled=True, min_chars=4096, ttl="1h"
        )
        assert out == text and isinstance(out, str)

    def test_empty_and_non_str_passthrough(self) -> None:
        assert _dw_shape_cached_system(
            "", enabled=True, min_chars=0, ttl="1h"
        ) == ""
        sentinel = {"not": "a string"}
        assert _dw_shape_cached_system(
            sentinel, enabled=True, min_chars=0, ttl="1h"
        ) is sentinel

    def test_fail_soft_on_bad_min_chars(self) -> None:
        # min_chars that can't be int() → helper must fail soft to the text.
        text = "S" * 5000
        out = _dw_shape_cached_system(
            text, enabled=True, min_chars="oops", ttl="1h"  # type: ignore[arg-type]
        )
        assert out == text and isinstance(out, str)


# ---------------------------------------------------------------------------
# env-driven config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_cache_enabled_default_false(self) -> None:
        assert _dw_prompt_cache_enabled() is False

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE"])
    def test_cache_enabled_truthy(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        monkeypatch.setenv("JARVIS_DW_PROMPT_CACHE_ENABLED", val)
        assert _dw_prompt_cache_enabled() is True

    def test_ttl_default_1h(self) -> None:
        assert _dw_prompt_cache_ttl() == "1h"

    def test_ttl_5m(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_DW_PROMPT_CACHE_TTL", "5m")
        assert _dw_prompt_cache_ttl() == "5m"

    def test_ttl_invalid_falls_back_1h(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_DW_PROMPT_CACHE_TTL", "30m")
        assert _dw_prompt_cache_ttl() == "1h"

    def test_min_chars_default_is_1024_token_floor(self) -> None:
        assert _dw_prompt_cache_min_chars() == _DW_PROMPT_CACHE_MIN_CHARS_DEFAULT
        assert _DW_PROMPT_CACHE_MIN_CHARS_DEFAULT >= 4096  # ~1024 tokens

    def test_min_chars_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_DW_PROMPT_CACHE_MIN_CHARS", "9000")
        assert _dw_prompt_cache_min_chars() == 9000

    def test_min_chars_invalid_falls_back_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_DW_PROMPT_CACHE_MIN_CHARS", "notanint")
        assert _dw_prompt_cache_min_chars() == _DW_PROMPT_CACHE_MIN_CHARS_DEFAULT


class TestSharedMinCharsHelper:
    """The DW floor reuses providers.prompt_cache_min_chars (DRY)."""

    def test_shared_helper_clamps_and_fails_soft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.core.ouroboros.governance.providers import (
            prompt_cache_min_chars,
        )
        assert prompt_cache_min_chars("SOME_UNSET_VAR", 0) == 0
        monkeypatch.setenv("SOME_VAR", "-50")
        assert prompt_cache_min_chars("SOME_VAR", 0) == 0  # clamp >= 0
        monkeypatch.setenv("SOME_VAR", "garbage")
        assert prompt_cache_min_chars("SOME_VAR", 7) == 7  # fail soft


# ---------------------------------------------------------------------------
# Slice-131 stable-prefix split reuse (force_prefix_split)
# ---------------------------------------------------------------------------


class TestForcePrefixSplit:
    def test_force_split_diverts_tools_and_schema_without_global_flag(
        self,
    ) -> None:
        """DW can request the split via force_prefix_split even when the global
        JARVIS_PROMPT_PREFIX_CACHE_ENABLED is OFF."""
        from backend.core.ouroboros.governance.providers import (
            _build_lean_codegen_prompt,
        )
        assert os.environ.get("JARVIS_PROMPT_PREFIX_CACHE_ENABLED") is None
        sink: List[str] = []
        user_prompt = _build_lean_codegen_prompt(
            _ctx(),
            stable_prefix_out=sink,
            force_prefix_split=True,
        )
        joined = "\n\n".join(sink)
        # The stable OUTPUT SCHEMA rode into the cached prefix, not the user prompt.
        assert "Output Schema" in joined
        assert "Output Schema" not in user_prompt

    def test_no_split_when_flag_off_and_no_force(self) -> None:
        """Byte-identical legacy: schema stays in the user prompt."""
        from backend.core.ouroboros.governance.providers import (
            _build_lean_codegen_prompt,
        )
        sink: List[str] = []
        user_prompt = _build_lean_codegen_prompt(
            _ctx(),
            stable_prefix_out=sink,
            force_prefix_split=False,
        )
        assert sink == []
        assert "Output Schema" in user_prompt

    def test_momentum_digest_excluded_from_cached_prefix(self) -> None:
        """Regression: volatile per-op content (the 'Recent Development
        Momentum' git-log digest) must NEVER enter the cached stable prefix."""
        from backend.core.ouroboros.governance.providers import (
            _build_lean_codegen_prompt,
        )
        sentinel = "MOMENTUM_SENTINEL_a1b2c3d4"
        momentum = f"## Recent Development Momentum\n\n{sentinel}"
        ctx = _ctx(session_lessons=momentum)
        sink: List[str] = []
        user_prompt = _build_lean_codegen_prompt(
            ctx, stable_prefix_out=sink, force_prefix_split=True
        )
        joined = "\n\n".join(sink)
        assert sentinel not in joined  # NOT in the cached block
        assert sentinel in user_prompt  # stays in the volatile user prompt


# ---------------------------------------------------------------------------
# DoublewordProvider._dw_build_system_content
# ---------------------------------------------------------------------------


class TestBuildSystemContent:
    def test_off_returns_plain_string_byte_identical(self) -> None:
        p = _provider()
        base = "SYSTEM PROMPT"
        out = p._dw_build_system_content(base, [])
        assert out == base and isinstance(out, str)

    def test_on_with_large_stable_prefix_marks_cache_control(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_DW_PROMPT_CACHE_ENABLED", "true")
        p = _provider()
        base = "SYSTEM PROMPT"
        prefix = ["TOOLS" + "x" * 3000, "SCHEMA" + "y" * 3000]
        out = p._dw_build_system_content(base, prefix)
        assert isinstance(out, list) and len(out) == 1
        block = out[0]
        assert block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        # Marked text = base + folded stable prefix, byte-identical content.
        assert block["text"].startswith(base)
        assert "TOOLS" in block["text"] and "SCHEMA" in block["text"]

    def test_on_but_prefix_below_floor_stays_plain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_DW_PROMPT_CACHE_ENABLED", "true")
        p = _provider()
        # Empty prefix + short base → below the ~4096 floor → no marker (no-op),
        # mirrors the batch/full-builder path where there's no split.
        out = p._dw_build_system_content("short system", [])
        assert out == "short system" and isinstance(out, str)

    def test_on_ttl_5m_threads_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_DW_PROMPT_CACHE_ENABLED", "true")
        monkeypatch.setenv("JARVIS_DW_PROMPT_CACHE_TTL", "5m")
        p = _provider()
        out = p._dw_build_system_content("base", ["z" * 5000])
        assert out[0]["cache_control"]["ttl"] == "5m"

    def test_momentum_not_in_marked_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: the split keeps momentum in the user prompt, so the
        cache_control-marked system block never contains it."""
        from backend.core.ouroboros.governance.providers import (
            _build_lean_codegen_prompt,
        )
        monkeypatch.setenv("JARVIS_DW_PROMPT_CACHE_ENABLED", "true")
        p = _provider()
        sentinel = "MOMENTUM_SENTINEL_deadbeef"
        ctx = _ctx(
            session_lessons=f"## Recent Development Momentum\n\n{sentinel}"
        )
        sink: List[str] = []
        _build_lean_codegen_prompt(
            ctx, stable_prefix_out=sink, force_prefix_split=True
        )
        marked = p._dw_build_system_content("SYSTEM", sink)
        # sink is large (tool catalog + schema) → marked as a block list.
        assert isinstance(marked, list)
        assert sentinel not in marked[0]["text"]


# ---------------------------------------------------------------------------
# Zero Data Retention
# ---------------------------------------------------------------------------


class TestZdr:
    def test_zdr_disabled_default(self) -> None:
        assert dw_zdr_enabled() is False
        assert dw_zdr_request_headers() == {}

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on"])
    def test_zdr_enabled_sets_header(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        monkeypatch.setenv("JARVIS_DW_ZDR_ENABLED", val)
        assert dw_zdr_enabled() is True
        hdrs = dw_zdr_request_headers()
        assert hdrs == {_DW_ZDR_HEADER: _DW_ZDR_HEADER_VALUE}

    def test_apply_zdr_merges_into_existing_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_DW_ZDR_ENABLED", "true")
        base = {"Authorization": "Bearer x"}
        merged = _dw_apply_zdr(base)
        assert merged["Authorization"] == "Bearer x"
        assert merged[_DW_ZDR_HEADER] == _DW_ZDR_HEADER_VALUE

    def test_apply_zdr_noop_when_disabled(self) -> None:
        base = {"Authorization": "Bearer x"}
        merged = _dw_apply_zdr(base)
        assert merged == base
        assert _DW_ZDR_HEADER not in merged

    def test_zdr_header_constant_documented(self) -> None:
        # A single, clearly-named constant that is trivial to correct once
        # DoubleWord (Meryem) confirms the exact flag.
        assert isinstance(_DW_ZDR_HEADER, str) and _DW_ZDR_HEADER
        assert _DW_ZDR_HEADER_VALUE == "true"


# ---------------------------------------------------------------------------
# Master-off byte-identical guarantee (integration proxy)
# ---------------------------------------------------------------------------


class TestMasterOffByteIdentical:
    def test_system_content_is_plain_string_when_master_off(self) -> None:
        """With caching OFF the RT/batch system message content is the plain
        legacy string — no content-block list, no cache_control keys."""
        p = _provider()
        # RT path passes an empty sink when off; batch always passes [].
        assert isinstance(p._dw_build_system_content("SYS", []), str)
