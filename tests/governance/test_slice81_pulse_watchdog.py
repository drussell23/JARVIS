"""Slice 81 — pulse-aware monitoring + convergence hardening.

Root cause (EVAL-2 sweep #4): Slice 80 gave a complex op `budget=1287s`, but the
harness stale-op detector (`_OP_STALE_THRESHOLD_S=600s`) declared it stale before
it finished → `stop_reason=stale_ops_detected` shut the session down mid-flight,
muddying the ledger.

Verify-first findings:
  * The streaming heartbeat is ALREADY wired (`_emit_stream_activity` →
    `last_activity_at_utc`, consumed by the Move-2-v4 `max(transition, activity)`
    freshness check). So the bug is CALIBRATION, not a missing pulse: the 600s
    threshold was shorter than the new max per-op budget, and a long NON-streaming
    phase (VERIFY pytest / heavy tool exec) has no heartbeat to lean on.
  * The teardown (`cross_repo_cleanup._sync_emergency_cleanup`) is ALREADY bounded
    (Slice 48: `join(timeout=budget_s)` + abandon) — the tombstone is the
    backstop working, not a fresh wedge. Not re-fixed.

Fix: raise the stale-threshold default (600→1200) so it exceeds typical adaptive
budgets, document the calibration invariant (a soak with large budgets must set
the threshold above its per-op ceiling), and raise the Claude base output budget
(16384→32768) so large single-file rewrites don't truncate mid-patch.
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.battle_test import harness as _harness
from backend.core.ouroboros.governance import providers as _providers


# --- Phase 1: stale-threshold calibration ---

def test_stale_threshold_default_raised_to_1200():
    src = inspect.getsource(_harness)
    assert 'os.environ.get("OUROBOROS_OP_STALE_THRESHOLD_S", "1200")' in src, (
        "the stale-threshold default must be 1200 (was 600 — shorter than "
        "Slice 80 adaptive budgets)"
    )


def test_stale_threshold_is_env_overridable():
    # the soak sets this above its per-op budget ceiling
    src = inspect.getsource(_harness)
    assert "OUROBOROS_OP_STALE_THRESHOLD_S" in src


def test_streaming_heartbeat_is_wired_into_the_stream_loop():
    # the pulse producer already exists — confirm it is actually CALLED (not
    # just defined), so a streaming phase stays fresh.
    src = inspect.getsource(_providers)
    assert "_emit_stream_activity(" in src
    # called at least twice (the content-delta + the keepalive sites)
    assert src.count("_emit_stream_activity(") >= 3  # 1 def + >=2 call sites


# --- Phase 2: Claude base output budget ---

def test_claude_base_max_tokens_raised_to_32768():
    src = inspect.getsource(_providers)
    assert "max_tokens: int = 32768" in src, (
        "the Claude base output budget must be 32768 (was 16384 — truncated "
        "large single-file rewrites mid-patch)"
    )


def test_claude_output_ceiling_is_env_tunable():
    src = inspect.getsource(_providers)
    assert "JARVIS_CLAUDE_MAX_OUTPUT_TOKENS" in src


# --- the calibration invariant (the load-bearing relationship) ---

def test_calibration_invariant_documented():
    # the relationship threshold > per-op-budget-ceiling must be documented so a
    # future soak doesn't silently re-break it.
    src = inspect.getsource(_harness)
    assert "JARVIS_ADAPTIVE_GEN_WALL_FRACTION" in src
    assert "stale" in src.lower() and "budget" in src.lower()
