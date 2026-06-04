"""Slice 55 — dynamic reasoning_effort + HeavyProber convergence.

Builds on Slice 54 (reasoning_effort=none unlock). Two changes:
  * effort is now DERIVED from task_complexity (leaf → none/cheap, core → CoT
    buffer), with JARVIS_DW_REASONING_EFFORT kept as an explicit override.
  * HeavyProber/surface-health probes send reasoning_effort so they stop
    false-flagging done_before_content (which forced needless batch routing).
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.doubleword_provider import (
    _reasoning_effort_for,
    _reasoning_request_params,
)


def test_complexity_maps_to_effort(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_REASONING_EFFORT", raising=False)
    assert _reasoning_effort_for("trivial") == "none"
    assert _reasoning_effort_for("simple") == "none"
    assert _reasoning_effort_for("moderate") == "low"
    assert _reasoning_effort_for("complex") == "medium"
    # Slice 84 — heavy_code/architectural map to "high" in the table, but
    # _reasoning_effort_for now clamps to the DW-serveable ceiling (default
    # "medium"): effort=high ruptures DW's chunked stream (ClientPayloadError:
    # TransferEncodingError, verified by direct probe 2026-06-03).
    assert _reasoning_effort_for("heavy_code") == "medium"


def test_unknown_complexity_defaults_none(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_REASONING_EFFORT", raising=False)
    assert _reasoning_effort_for("") == "none"
    assert _reasoning_effort_for("banana") == "none"


def test_env_override_wins_over_complexity(monkeypatch):
    # The env remains an explicit operator override / kill-switch — NOT removed.
    monkeypatch.setenv("JARVIS_DW_REASONING_EFFORT", "low")
    assert _reasoning_effort_for("heavy_code") == "low"
    assert _reasoning_effort_for("trivial") == "low"


def test_request_params_carry_complexity_derived_effort(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_REASONING_EFFORT", raising=False)
    assert _reasoning_request_params(complexity="complex")["reasoning_effort"] == "medium"
    # none → also carries the (harmless) enable_thinking belt-and-braces
    p = _reasoning_request_params(complexity="trivial")
    assert p["reasoning_effort"] == "none"
    assert p.get("chat_template_kwargs") == {"enable_thinking": False}
    # explicit effort arg still wins over complexity
    assert _reasoning_request_params(effort="high", complexity="trivial")["reasoning_effort"] == "high"


def test_heavyprober_sends_reasoning_effort():
    """Wiring pin (Slice 45 lesson): the boot health probe must send
    reasoning_effort or it keeps false-flagging done_before_content on
    reasoning models and forces needless batch routing."""
    import inspect

    import backend.core.ouroboros.governance.dw_heavy_probe as hp

    src = inspect.getsource(hp)
    assert '"reasoning_effort"' in src, "HeavyProber probe body must send reasoning_effort"
