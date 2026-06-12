"""Slice 226 — Iron Gate / provider capability alignment.

ROOT CAUSE (live soak GOAL-001::file-00): a catch-22 between two independent,
complexity-keyed decisions:

  * doubleword_provider tool-skip: ``complexity in {trivial, simple}`` -> skip
    the Venom tool loop (cost optimization) -> the model gets NO read_file /
    search_code channels.
  * Iron Gate exploration floor (exploration_engine + orchestrator): a
    non-trivial op must make >=1 exploration call ("read the target file").

A ``simple`` op on a venom-eligible route (file-00: standard/roadmap) is thus
denied the tools, then REJECTED for not using them — ``exploration_insufficient:
0/1`` across both GENERATE attempts -> generation_failed, forever. The intended
escape hatch (preloaded-prompt credit) silently fails when the target file is
too large to inline (semantic_index.py is 3246 lines).

FIX: a single shared predicate, ``exploration_gate_demands_tools(complexity)``,
that both providers consult: when the Iron Gate will demand exploration for this
complexity, the tool loop must NOT be skipped on a complexity heuristic. trivial
stays exempt; BACKGROUND/SPECULATIVE keep skipping (route-based, preload-credit
path) — only the complexity-based simple-op skip is overridden.
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance.exploration_engine import (
    exploration_gate_demands_tools,
    compute_tool_loop_suppressed,
)


# ── the predicate ──────────────────────────────────────────────────────────

def test_gate_demands_tools_for_simple_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "true")
    assert exploration_gate_demands_tools("simple") is True


def test_gate_exempts_trivial(monkeypatch):
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "true")
    assert exploration_gate_demands_tools("trivial") is False


def test_gate_demands_tools_for_moderate_and_heavy(monkeypatch):
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "true")
    assert exploration_gate_demands_tools("moderate") is True
    assert exploration_gate_demands_tools("heavy") is True


def test_gate_off_demands_nothing(monkeypatch):
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "false")
    assert exploration_gate_demands_tools("simple") is False


def test_gate_empty_complexity_is_false(monkeypatch):
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "true")
    assert exploration_gate_demands_tools("") is False


def test_explicit_gate_enabled_param_overrides_env(monkeypatch):
    monkeypatch.delenv("JARVIS_EXPLORATION_GATE", raising=False)
    assert exploration_gate_demands_tools("simple", gate_enabled=False) is False
    assert exploration_gate_demands_tools("simple", gate_enabled=True) is True


# ── the unified tool-skip decision (the catch-22 fix) ──────────────────────

def test_file00_catch22_resolved(monkeypatch):
    """simple + standard route + gate on -> tools MUST stay available."""
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "true")
    suppressed = compute_tool_loop_suppressed(
        complexity="simple", route="standard",
        is_bg_terminal_worker=False, has_repair_context=False,
    )
    assert suppressed is False, "simple gated op must keep the tool loop"


def test_trivial_still_skips(monkeypatch):
    """trivial is gate-exempt -> still skipped (legacy cost optimization)."""
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "true")
    assert compute_tool_loop_suppressed(
        complexity="trivial", route="standard",
        is_bg_terminal_worker=False, has_repair_context=False,
    ) is True


def test_background_route_still_skips(monkeypatch):
    """BACKGROUND skip is route-based (preload-credit path) -> preserved."""
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "true")
    assert compute_tool_loop_suppressed(
        complexity="simple", route="background",
        is_bg_terminal_worker=False, has_repair_context=False,
    ) is True


def test_repair_context_still_skips(monkeypatch):
    """L2 single-shot fast path (Slice 9) must remain skipped."""
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "true")
    assert compute_tool_loop_suppressed(
        complexity="moderate", route="standard",
        is_bg_terminal_worker=False, has_repair_context=True,
    ) is True


def test_gate_off_simple_skips_legacy(monkeypatch):
    """Gate OFF -> byte-identical legacy: simple still skips."""
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "false")
    assert compute_tool_loop_suppressed(
        complexity="simple", route="standard",
        is_bg_terminal_worker=False, has_repair_context=False,
    ) is True


def test_moderate_never_skipped_regardless(monkeypatch):
    """moderate is already tool-eligible — unchanged."""
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "true")
    assert compute_tool_loop_suppressed(
        complexity="moderate", route="standard",
        is_bg_terminal_worker=False, has_repair_context=False,
    ) is False


def test_master_flag_off_is_legacy(monkeypatch):
    """JARVIS_GATE_ALIGNMENT_ENABLED=0 -> legacy simple-skip restored."""
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "true")
    monkeypatch.setenv("JARVIS_GATE_ALIGNMENT_ENABLED", "0")
    assert compute_tool_loop_suppressed(
        complexity="simple", route="standard",
        is_bg_terminal_worker=False, has_repair_context=False,
    ) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
