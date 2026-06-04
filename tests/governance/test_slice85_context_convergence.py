"""Slice 85 Phase 3 — cumulative convergence gate for the Venom tool loop.

Confirmed failure (sweep bt-2026-06-04-041943): a DW op explored **25 tool calls
across 8+ rounds / 486s / 114,800 input tokens and emitted 0 patches** — the
"114k wander". The tool loop already HAS convergence nudges, but both axes missed
this:

- Slice 3E final-write nudge: triggers on time-reserve or rounds_left<=1 — both
  fire only at the very END, after the wander already happened.
- Exploration-budget nudge (`_explore_only_rounds >= _max_exploration_rounds=5`):
  only counts rounds where EVERY tool is in the narrow
  ``_EXPLORATION_TOOLS = {read_file, search_code, get_callers}``. The wander's
  rounds mixed in ``glob_files`` / ``list_dir`` / ``git_log`` (all read-only
  exploration, just not in that set), so those rounds were NOT "exploration-only",
  the counter never reached 5, and the nudge NEVER fired (verified: 0 nudge events
  in the wander's log).

Fix: a CUMULATIVE, tool-agnostic axis. Count every read-only navigation call
across ALL rounds (a superset that mixed-tool wandering can't evade); once the
cumulative count crosses a threshold, fire the EXISTING Slice 3E nudge (reusing
its grace-round machinery — no new gate). Env-tunable, no hardcoded per-tool
limits.

NOT a new compaction utility: ``context_compaction.py`` (Gap #8) already bounds
single-round prompt size at 75% of the ceiling. The wander was a CONVERGENCE
problem (model kept choosing to explore), not a context-size problem — compaction
kept each round bounded while the cumulative exploration still ran away.
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.governance import tool_executor as te


# --- the read-only exploration superset (evasion-proof) ---

def test_readonly_exploration_superset_covers_wander_tools():
    s = te._READONLY_EXPLORATION_TOOLS
    # the narrow Iron-Gate set must be included...
    for t in ("read_file", "search_code", "get_callers"):
        assert t in s
    # ...PLUS the navigation tools the wander used to EVADE the old counter
    for t in ("glob_files", "list_dir", "git_log"):
        assert t in s, f"{t} must count toward convergence (the wander evaded via it)"


def test_mutating_tools_are_not_exploration():
    # tools that make progress must NOT count as exploration
    for t in ("edit_file", "write_file", "bash", "run_tests"):
        assert t not in te._READONLY_EXPLORATION_TOOLS


# --- the cumulative convergence threshold ---

def test_convergence_threshold_default(monkeypatch):
    monkeypatch.delenv("JARVIS_TOOL_LOOP_CONVERGENCE_EXPLORE_CALLS", raising=False)
    n = te._convergence_explore_call_threshold()
    assert isinstance(n, int) and n > 0
    # must trip well below the 25-call wander
    assert n < 25, "default threshold must catch the 25-call wander"


def test_convergence_threshold_env_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_LOOP_CONVERGENCE_EXPLORE_CALLS", "9")
    assert te._convergence_explore_call_threshold() == 9


def test_convergence_threshold_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_LOOP_CONVERGENCE_EXPLORE_CALLS", "garbage")
    assert te._convergence_explore_call_threshold() > 0


def test_convergence_threshold_zero_disables(monkeypatch):
    # 0 = opt-out (legacy behavior, never force-converge on cumulative axis)
    monkeypatch.setenv("JARVIS_TOOL_LOOP_CONVERGENCE_EXPLORE_CALLS", "0")
    assert te._convergence_explore_call_threshold() == 0


# --- the pure trigger decision ---

def test_cumulative_trigger_fires_at_threshold():
    # below threshold → keep exploring; at/above → force convergence
    assert te._should_force_convergence(cumulative_explore_calls=8, threshold=12) is False
    assert te._should_force_convergence(cumulative_explore_calls=12, threshold=12) is True
    assert te._should_force_convergence(cumulative_explore_calls=25, threshold=12) is True


def test_cumulative_trigger_disabled_when_threshold_zero():
    # threshold 0 must never fire (opt-out), even at a huge call count
    assert te._should_force_convergence(cumulative_explore_calls=999, threshold=0) is False


def test_wander_would_have_converged():
    # the actual wander: 25 cumulative read-only calls vs the default threshold
    thr = te._convergence_explore_call_threshold()
    assert te._should_force_convergence(cumulative_explore_calls=25, threshold=thr) is True


# --- wiring pins: the run() loop actually consumes the cumulative axis ---

def test_run_loop_tracks_cumulative_and_feeds_existing_nudge():
    src = inspect.getsource(te.ToolLoopCoordinator.run)
    assert "_cumulative_explore_calls" in src, "loop must track cumulative read-only calls"
    assert "_should_force_convergence(" in src, "loop must consult the cumulative trigger"
    # composes WITH the existing Slice 3E nudge (does not replace it)
    assert "_final_nudge_issued" in src
    assert "_trigger_context" in src or "context" in src.lower()
