"""
Task #88 spine — thinking-aware TTFT rupture timeout.

Closes the root cause of v14-rev3/4/5 Claude fallback 0/30 failure:
the legacy 120s TTFT rupture cap fired while Claude was actively
producing ``thinking_delta`` events (filtered out by the SDK's
``stream.text_stream``).  Direct host probes confirmed the API is
healthy — bug was harness-side, in the rupture-watchdog cap.

This spine pins:

  * ``stream_rupture_timeout_s(thinking_enabled=False)`` keeps the
    legacy 120s default for non-thinking callers (backward compat).
  * ``stream_rupture_timeout_s(thinking_enabled=True)`` returns 360s
    by default, env-tunable via
    ``JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S``.
  * Env reads happen at call time (operator can flip mid-process).
  * ClaudeProvider invokes the function with the correct
    ``thinking_enabled=`` keyword based on its actual SDK kwargs.
  * FlagRegistry seeds the env knob.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.stream_rupture import (
    stream_rupture_timeout_s,
    stream_inter_chunk_timeout_s,
)


_RUPTURE_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "stream_rupture.py"
)
_PROVIDERS_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "providers.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "flag_registry_seed.py"
)


# ---------------------------------------------------------------------------
# Defaults (backward compat + new thinking-aware)
# ---------------------------------------------------------------------------


def test_non_thinking_default_unchanged(monkeypatch: pytest.MonkeyPatch):
    """Legacy 120s default for non-thinking callers — backward compat."""
    monkeypatch.delenv("JARVIS_STREAM_RUPTURE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S", raising=False)
    # No-keyword + explicit False both → 120s
    assert stream_rupture_timeout_s() == 120.0
    assert stream_rupture_timeout_s(thinking_enabled=False) == 120.0


def test_thinking_aware_default_is_widened(monkeypatch: pytest.MonkeyPatch):
    """Task #88 — thinking_enabled=True returns the wider default (360s)."""
    monkeypatch.delenv("JARVIS_STREAM_RUPTURE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S", raising=False)
    assert stream_rupture_timeout_s(thinking_enabled=True) == 360.0


def test_thinking_aware_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S", "600")
    assert stream_rupture_timeout_s(thinking_enabled=True) == 600.0


def test_legacy_env_override_still_works(monkeypatch: pytest.MonkeyPatch):
    """Non-thinking callers respect the legacy env var."""
    monkeypatch.setenv("JARVIS_STREAM_RUPTURE_TIMEOUT_S", "90")
    assert stream_rupture_timeout_s() == 90.0
    assert stream_rupture_timeout_s(thinking_enabled=False) == 90.0


def test_thinking_and_legacy_env_independent(monkeypatch: pytest.MonkeyPatch):
    """Each env knob controls its own branch — no cross-contamination."""
    monkeypatch.setenv("JARVIS_STREAM_RUPTURE_TIMEOUT_S", "60")
    monkeypatch.setenv("JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S", "900")
    assert stream_rupture_timeout_s(thinking_enabled=False) == 60.0
    assert stream_rupture_timeout_s(thinking_enabled=True) == 900.0


def test_inter_chunk_unchanged(monkeypatch: pytest.MonkeyPatch):
    """Phase-2 inter-chunk timeout is independent — Task #88 doesn't touch it."""
    monkeypatch.delenv("JARVIS_STREAM_INTER_CHUNK_TIMEOUT_S", raising=False)
    assert stream_inter_chunk_timeout_s() == 30.0


def test_env_read_at_call_time(monkeypatch: pytest.MonkeyPatch):
    """Env MUST be re-read on every call (operator hot-flip)."""
    monkeypatch.delenv("JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S", raising=False)
    v1 = stream_rupture_timeout_s(thinking_enabled=True)
    assert v1 == 360.0
    monkeypatch.setenv("JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S", "200")
    v2 = stream_rupture_timeout_s(thinking_enabled=True)
    assert v2 == 200.0


# ---------------------------------------------------------------------------
# AST pins — providers.py + seed
# ---------------------------------------------------------------------------


def test_ast_pin_providers_passes_thinking_enabled():
    """ClaudeProvider._do_stream MUST pass thinking_enabled= based on
    the SDK kwargs.  Without this, the widened TTFT wouldn't engage
    for thinking calls and the bug would silently regress.
    """
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    assert "_stream_rupture_timeout_s(" in src, (
        "providers.py must call _stream_rupture_timeout_s"
    )
    assert "thinking_enabled=" in src, (
        "ClaudeProvider must pass thinking_enabled= to "
        "_stream_rupture_timeout_s — Task #88 invariant"
    )
    # The boolean MUST be derived from the actual SDK kwargs in scope
    # ("thinking" in _stream_kwargs).  This pin makes the regression
    # path explicit.
    assert '"thinking" in _stream_kwargs' in src, (
        "thinking_enabled MUST be derived from _stream_kwargs to "
        "ensure the TTFT widening only engages for actual SDK "
        "thinking calls"
    )


def test_ast_pin_seed_has_thinking_ttft_flag():
    """The new env knob MUST be FlagRegistry-seeded."""
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S" in src, (
        "FlagRegistry seed missing JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S"
    )
    idx = src.find("JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S")
    window = src[idx:idx + 1200]
    assert "Category.TIMING" in window, (
        "JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S MUST be Category.TIMING"
    )
    assert "default=360" in window, (
        "Default MUST be 360s per Task #88 design"
    )


def test_ast_pin_stream_rupture_keeps_legacy_signature_compat():
    """Adding the thinking_enabled keyword MUST keep the legacy
    no-arg call signature working — non-thinking callers shouldn't
    need to know the new flag exists.
    """
    src = _RUPTURE_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "stream_rupture_timeout_s":
            fn = node
            break
    assert fn is not None, "stream_rupture_timeout_s must exist"
    # Positional args must be empty (signature: `(*, thinking_enabled=False)`)
    assert len(fn.args.args) == 0, (
        "stream_rupture_timeout_s must take only keyword-only args — "
        "no positional args, so legacy callers using `stream_rupture_timeout_s()` "
        "still work"
    )
    # Must have a kwonly arg called thinking_enabled with a default
    kwonly_names = [a.arg for a in fn.args.kwonlyargs]
    assert "thinking_enabled" in kwonly_names, (
        "stream_rupture_timeout_s must accept thinking_enabled as a "
        "keyword-only argument"
    )
    # Default must be False (preserves legacy behavior for unaware callers)
    default_idx = kwonly_names.index("thinking_enabled")
    default_node = fn.args.kw_defaults[default_idx]
    assert isinstance(default_node, ast.Constant) and default_node.value is False, (
        "thinking_enabled MUST default to False (legacy compat)"
    )
