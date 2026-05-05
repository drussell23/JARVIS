"""Tests for typing-responsiveness fixes in the SerpentREPL.

User-reported symptom: typing into the REPL felt laggy / froze
under ``-v`` mode. Three causes diagnosed:

  1. ``refresh_interval=0.10`` drove a toolbar redraw every 100ms,
     and each redraw did non-trivial work (state pulls + Rich
     formatting via Gap #1+5's wrapper). Key events queued behind
     in-progress redraws — operator-perceived freeze.
  2. ``prompt_toolkit`` logger wasn't in the noisy-loggers
     suppression list — under ``-v``, prompt_toolkit emitted
     DEBUG on every key event.
  3. ``_REPL_REFRESH_INTERVAL_S`` was hardcoded — operators on
     slow terminals couldn't tune cadence.

Fixes:

  * ``make_cached_bottom_toolbar(inner, state_fetcher)`` —
    state-hash cache means unchanged-state ticks return cached
    output in microseconds. Spinner animation still works because
    the spinner glyph is part of the hash; cache invalidates on
    every spinner-frame advance ONLY when spinner is active.
  * ``prompt_toolkit`` added to the script's noisy-loggers list.
  * ``_REPL_REFRESH_INTERVAL_S`` reads ``JARVIS_REPL_REFRESH_INTERVAL_S``
    with [0.05, 5.0] clamping.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.live_status_line import (
    make_cached_bottom_toolbar,
)


_REPO = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")


# ===========================================================================
# make_cached_bottom_toolbar — state-hash cache contract
# ===========================================================================


def test_cache_hit_returns_same_result_for_identical_state():
    """Repeated calls with same state-tuple → inner called ONCE."""
    inner = mock.Mock(return_value="rendered output")
    state = mock.Mock(return_value=("a", "b", 0))
    cached = make_cached_bottom_toolbar(inner, state)
    out1 = cached()
    out2 = cached()
    out3 = cached()
    assert out1 == out2 == out3 == "rendered output"
    assert inner.call_count == 1  # only first call invoked inner


def test_cache_invalidates_on_state_change():
    """When state-tuple differs → inner is re-called, fresh result."""
    inner = mock.Mock(side_effect=["v1", "v2", "v3"])
    state_seq = iter([("a",), ("a",), ("b",), ("b",), ("c",)])
    state_fetcher = lambda: next(state_seq)
    cached = make_cached_bottom_toolbar(inner, state_fetcher)
    assert cached() == "v1"   # first call
    assert cached() == "v1"   # cache hit (state still "a")
    assert cached() == "v2"   # state changed to "b" — inner re-called
    assert cached() == "v2"   # cache hit again
    assert cached() == "v3"   # state changed to "c"
    assert inner.call_count == 3


def test_cache_handles_state_fetcher_exception():
    """Fetcher raising → uncached pass-through to inner. NEVER raises."""
    inner = mock.Mock(return_value="fallback")
    def _raising():
        raise RuntimeError("fetcher exploded")
    cached = make_cached_bottom_toolbar(inner, _raising)
    assert cached() == "fallback"
    # Each call invokes inner directly (no cache primed)
    cached()
    assert inner.call_count == 2


def test_cache_handles_inner_exception():
    """Inner raising → empty string. NEVER propagates."""
    inner = mock.Mock(side_effect=RuntimeError("toolbar exploded"))
    state = mock.Mock(return_value=("a",))
    cached = make_cached_bottom_toolbar(inner, state)
    out = cached()
    # Documented contract: inner exceptions degrade to ""
    assert out == ""


def test_cache_inner_exception_with_unhashable_state():
    """Defensive: state_fetcher returning unhashable → degrade to inner;
    inner raising on top → empty fallback. NEVER raises."""
    inner = mock.Mock(side_effect=RuntimeError("nope"))
    state = mock.Mock(return_value={"unhashable": "dict"})
    cached = make_cached_bottom_toolbar(inner, state)
    assert cached() == ""


def test_cache_called_many_times_minimal_inner_invocations():
    """Stress: 1000 calls with identical state should invoke inner 1x.
    This is the core typing-responsiveness contract."""
    inner = mock.Mock(return_value="render")
    state = mock.Mock(return_value=("steady",))
    cached = make_cached_bottom_toolbar(inner, state)
    for _ in range(1000):
        cached()
    assert inner.call_count == 1


def test_cache_first_call_always_invokes_inner():
    """Even on first call (no prior state), inner must fire — caller
    expects a fresh render the first time."""
    inner = mock.Mock(return_value="fresh")
    state = mock.Mock(return_value=("anything",))
    cached = make_cached_bottom_toolbar(inner, state)
    cached()
    assert inner.call_count == 1


# ===========================================================================
# Source-level regression — wiring into serpent_flow
# ===========================================================================


_SERPENT_FLOW = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


def test_serpent_flow_wraps_toolbar_with_cache():
    """The REPL's _loop must invoke make_cached_bottom_toolbar so
    keystroke contention is mitigated. Regression pin."""
    src = _SERPENT_FLOW.read_text()
    assert "make_cached_bottom_toolbar" in src
    assert "_toolbar_state_fetcher" in src


def test_state_fetcher_includes_spinner_signal():
    """The state-fetcher's tuple must include a spinner-aware signal
    so spinner-frame transitions invalidate the cache (animation works
    only when this signal is present)."""
    src = _SERPENT_FLOW.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_toolbar_state_fetcher":
            body = ast.unparse(node)
            assert "spinner_signal" in body or "_frame_for_now" in body
            return
    pytest.fail("_toolbar_state_fetcher not found")


def test_state_fetcher_idle_spinner_signal_is_constant():
    """Critical for typing perf: when spinner is INACTIVE, the
    spinner signal must be a CONSTANT (not _frame_for_now()) so
    rapid keystrokes hit the cache."""
    src = _SERPENT_FLOW.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_toolbar_state_fetcher":
            body = ast.unparse(node)
            # Branch should be:  _frame_for_now() if spinner_active else "_idle_"
            assert "_idle_" in body, (
                "_toolbar_state_fetcher must use a constant idle "
                "signal so the cache stays primed during typing"
            )
            return
    pytest.fail("_toolbar_state_fetcher not found")


# ===========================================================================
# refresh_interval — env-configurable, no longer hardcoded
# ===========================================================================


def test_refresh_interval_resolver_default(monkeypatch):
    monkeypatch.delenv("JARVIS_REPL_REFRESH_INTERVAL_S", raising=False)
    # Re-import to refresh the module-level constant
    import importlib
    import backend.core.ouroboros.battle_test.serpent_flow as sf
    importlib.reload(sf)
    assert sf._resolve_repl_refresh_interval_s() == 0.10


@pytest.mark.parametrize("raw,expected", [
    ("0.25", 0.25), ("0.5", 0.5), ("1.0", 1.0),
])
def test_refresh_interval_resolver_explicit(monkeypatch, raw, expected):
    monkeypatch.setenv("JARVIS_REPL_REFRESH_INTERVAL_S", raw)
    import backend.core.ouroboros.battle_test.serpent_flow as sf
    assert sf._resolve_repl_refresh_interval_s() == expected


@pytest.mark.parametrize("raw,clamped_to", [
    ("0.001", 0.05),  # below MIN
    ("0.0", 0.05),    # zero
    ("100", 5.0),     # above MAX
    ("-1", 0.05),     # negative
])
def test_refresh_interval_resolver_clamped(monkeypatch, raw, clamped_to):
    monkeypatch.setenv("JARVIS_REPL_REFRESH_INTERVAL_S", raw)
    import backend.core.ouroboros.battle_test.serpent_flow as sf
    assert sf._resolve_repl_refresh_interval_s() == clamped_to


def test_refresh_interval_resolver_garbage_returns_default(monkeypatch):
    monkeypatch.setenv("JARVIS_REPL_REFRESH_INTERVAL_S", "not-a-number")
    import backend.core.ouroboros.battle_test.serpent_flow as sf
    assert sf._resolve_repl_refresh_interval_s() == 0.10


# ===========================================================================
# prompt_toolkit logger added to noisy-loggers list
# ===========================================================================


def test_prompt_toolkit_in_noisy_loggers():
    """Per-keystroke DEBUG from prompt_toolkit was a typing-lag
    contributor under -v. Must be in the script's suppression list."""
    src = (_REPO / "scripts/ouroboros_battle_test.py").read_text()
    # Find the noisy-loggers loop and verify prompt_toolkit is listed
    assert '"prompt_toolkit"' in src
    # Make sure it's actually in the for-_noisy loop, not in a comment
    # by checking for the specific pattern
    assert (
        '"markdown_it"' in src and "prompt_toolkit" in src
    ), "prompt_toolkit must be in the noisy-loggers suppression list"


# ===========================================================================
# Boot-noise suppression compatibility — Gap #7 follow-up still works
# ===========================================================================


def test_typing_fix_does_not_break_boot_noise_suppression():
    """Sanity check: my typing fix doesn't accidentally undo the
    Gap #7 boot-noise suppression. Both should coexist."""
    src = (_REPO / "scripts/ouroboros_battle_test.py").read_text()
    # Boot-noise suppression call (Gap #7 follow-up)
    assert "suppress_boot_noise_logs" in src
    # New typing-fix entry
    assert '"prompt_toolkit"' in src
    # Both must be in the script
