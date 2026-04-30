"""Phase-Aware Heartbeats regression — stream-tick activity hook keeps
ActivityMonitor's freshness signal accurate during long GENERATEs.

Pins:
  * ``LoopRuntimeContext.last_activity_at_utc`` exists and defaults to
    construction time.
  * ``preemption_fsm.transition()`` updates BOTH
    ``last_transition_at_utc`` and ``last_activity_at_utc`` (transitions
    imply activity).
  * ``providers.set_stream_activity_callback`` registers / clears the
    module-level hook.
  * ``providers._emit_stream_activity(op_id)`` calls the registered
    callback with the op_id and is a cheap no-op when unregistered.
  * Failures inside the callback NEVER propagate (best-effort guarantee).
  * The chunk-interval throttle env knob
    ``JARVIS_STREAM_ACTIVITY_CHUNK_INTERVAL`` is honored.
  * ActivityMonitor freshness uses ``max(last_transition,
    last_activity)`` — a streaming op stays fresh even after the
    phase-transition timer goes stale.

These pins close the regression vector observed in soaks v1/v2/v3:
``--idle-timeout 3600`` fired at ~1h because ops streaming tokens for
multi-minute GENERATEs had no activity heartbeat between phase
transitions.

Authority Invariant
-------------------
Tests import only from the modules under test plus stdlib. No
orchestrator / phase_runners / iron_gate imports.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest


# -----------------------------------------------------------------------
# § A — Field exists + defaults
# -----------------------------------------------------------------------


def test_loop_runtime_context_has_last_activity_at_utc():
    from backend.core.ouroboros.governance.contracts.fsm_contract import (
        LoopRuntimeContext,
    )
    ctx = LoopRuntimeContext(op_id="op-test")
    # Field exists
    assert hasattr(ctx, "last_activity_at_utc")
    # Default is a recent UTC datetime (construction-time)
    assert isinstance(ctx.last_activity_at_utc, datetime)
    assert ctx.last_activity_at_utc.tzinfo is timezone.utc
    # last_activity defaults equal-or-near last_transition
    delta = abs(
        (ctx.last_activity_at_utc - ctx.last_transition_at_utc).total_seconds()
    )
    assert delta < 1.0


# -----------------------------------------------------------------------
# § B — Stream activity callback
# -----------------------------------------------------------------------


def test_set_stream_activity_callback_registers_and_clears():
    from backend.core.ouroboros.governance import providers
    pulses = []

    def cb(op_id: str) -> None:
        pulses.append(op_id)

    providers.set_stream_activity_callback(cb)
    providers._emit_stream_activity("op-1")
    providers._emit_stream_activity("op-2")
    assert pulses == ["op-1", "op-2"]

    # Clear
    providers.set_stream_activity_callback(None)
    providers._emit_stream_activity("op-3")
    assert pulses == ["op-1", "op-2"]  # unchanged


def test_emit_stream_activity_is_no_op_when_unregistered():
    from backend.core.ouroboros.governance import providers
    providers.set_stream_activity_callback(None)
    # Should not raise
    providers._emit_stream_activity("op-x")
    providers._emit_stream_activity("")


def test_emit_stream_activity_swallows_callback_exceptions():
    from backend.core.ouroboros.governance import providers

    def bad_cb(op_id: str) -> None:
        raise RuntimeError("provider should not see this")

    providers.set_stream_activity_callback(bad_cb)
    try:
        # Best-effort: failures must NEVER propagate
        providers._emit_stream_activity("op-z")
    finally:
        providers.set_stream_activity_callback(None)


def test_emit_stream_activity_skips_empty_op_id():
    from backend.core.ouroboros.governance import providers
    pulses = []
    providers.set_stream_activity_callback(lambda op_id: pulses.append(op_id))
    try:
        providers._emit_stream_activity("")
        providers._emit_stream_activity(None)  # type: ignore[arg-type]
        assert pulses == []
    finally:
        providers.set_stream_activity_callback(None)


# -----------------------------------------------------------------------
# § C — Chunk interval throttle env knob
# -----------------------------------------------------------------------


def test_chunk_interval_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_STREAM_ACTIVITY_CHUNK_INTERVAL", "32")
    import backend.core.ouroboros.governance.providers as _providers
    importlib.reload(_providers)
    assert _providers._STREAM_ACTIVITY_CHUNK_INTERVAL == 32


def test_chunk_interval_default(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_STREAM_ACTIVITY_CHUNK_INTERVAL", raising=False,
    )
    import backend.core.ouroboros.governance.providers as _providers
    importlib.reload(_providers)
    assert _providers._STREAM_ACTIVITY_CHUNK_INTERVAL == 8


# -----------------------------------------------------------------------
# § D — Phase transitions also bump activity (preemption_fsm)
# -----------------------------------------------------------------------


def test_phase_transition_bumps_both_timestamps():
    """Bytes-pin: preemption_fsm.transition must update BOTH
    last_transition_at_utc and last_activity_at_utc. Without this, a
    transition would advance the phase clock but leave the activity
    clock stale — defeating the freshness signal."""
    import pathlib
    src = pathlib.Path(
        "backend/core/ouroboros/governance/preemption_fsm.py"
    ).read_text()
    # Both assignments must be present in the same function
    assert "ctx.last_transition_at_utc = ti.now_utc" in src
    assert "ctx.last_activity_at_utc = ti.now_utc" in src


# -----------------------------------------------------------------------
# § E — ActivityMonitor uses max(last_transition, last_activity)
# -----------------------------------------------------------------------


def test_activity_monitor_uses_max_of_both_timestamps():
    """Bytes-pin: harness ActivityMonitor must use the maximum of
    last_transition_at_utc and last_activity_at_utc when evaluating
    op freshness. Without this, a streaming op that bumps activity
    but not transition would still look stale."""
    import pathlib
    src = pathlib.Path(
        "backend/core/ouroboros/battle_test/harness.py"
    ).read_text()
    # The ActivityMonitor must read both fields and combine via max.
    assert "last_activity_at_utc" in src
    assert "max(last_transition, last_activity)" in src


# -----------------------------------------------------------------------
# § F — Authority invariant
# -----------------------------------------------------------------------


def test_authority_invariant_no_orchestrator_imports():
    """This test module must not pull in orchestrator / phase_runners /
    iron_gate / change_engine. Bytes-pinned at the source-file level."""
    import pathlib
    src = pathlib.Path(__file__).read_text()
    forbidden = (
        "orchestrator", "phase_runners", "iron_gate",
        "change_engine", "candidate_generator",
    )
    for tok in forbidden:
        assert f"import {tok}" not in src, f"forbidden import: {tok}"
        assert f"from backend.core.ouroboros.governance.{tok}" not in src
