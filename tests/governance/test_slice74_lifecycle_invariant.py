"""Slice 74 — Immutable Lifecycle Boundary Invariant.

Telemetry (bt-2026-06-03-081821 Slice74Probe) proved the broker rendezvous works
(same singleton, subs=1, ~1s wake) — so the prior 25-min lag was a transient on
the COMPLETED/terminal write path, the prime suspect being a deduped ledger write
(`written=False`) that skipped the SSE broadcast at `orchestrator._record_ledger`.

The fix decouples the terminal SSE broadcast from the ledger's physical-write
dedup: a definitive terminal state MUST notify the system even when the ledger
deduped the row. Idempotency moves from the ledger gate to the notification
layer (`_terminal_publish_once`) so it broadcasts exactly once, never zero, never
twice — regardless of how many times `_record_ledger` fires for that state.
"""
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.ide_observability_stream import (
    publish_operation_terminal,
    _terminal_publish_once,
    _terminal_published_set,
    _terminal_published_order,
    reset_default_broker,
    TERMINAL_OPERATION_STATES,
)


class _State:
    def __init__(self, value: str):
        self.value = value


class _Ctx:
    def __init__(self, op_id: str):
        self.op_id = op_id
        self.phase = None
        self.phase_entered_at = None
        self.terminal_reason_code = ""


def _clear_idempotency():
    _terminal_published_set.clear()
    _terminal_published_order.clear()


def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_OP_LIFECYCLE_SSE_ENABLED", "true")
    reset_default_broker()
    _clear_idempotency()


# --- The core invariant: terminal broadcast fires regardless of ledger dedup ---

def test_terminal_broadcast_fires_for_terminal_state(monkeypatch):
    """A terminal state ('applied' = success) broadcasts — the publish no longer
    rides on the ledger's `written` flag (the orchestrator calls it
    unconditionally; this verifies the publisher itself always fires)."""
    _enable(monkeypatch)
    ev = publish_operation_terminal(_Ctx("op-s74-applied"), _State("applied"))
    assert ev is not None, "terminal broadcast must fire for 'applied'"


def test_terminal_broadcast_fires_for_failed(monkeypatch):
    _enable(monkeypatch)
    ev = publish_operation_terminal(_Ctx("op-s74-failed"), _State("failed"))
    assert ev is not None


def test_exactly_once_idempotency(monkeypatch):
    """Decoupled from `written`, the publisher must still fire EXACTLY once per
    (op_id, state) — a deduped storage write gets the first broadcast, a repeat
    invocation does NOT double-fire."""
    _enable(monkeypatch)
    ev1 = publish_operation_terminal(_Ctx("op-s74-once"), _State("applied"))
    ev2 = publish_operation_terminal(_Ctx("op-s74-once"), _State("applied"))
    assert ev1 is not None, "first terminal broadcast must fire"
    assert ev2 is None, "repeat (op_id, state) must be deduped at the notify layer"


def test_distinct_states_each_fire(monkeypatch):
    _enable(monkeypatch)
    a = publish_operation_terminal(_Ctx("op-s74-multi"), _State("applied"))
    f = publish_operation_terminal(_Ctx("op-s74-multi"), _State("failed"))
    assert a is not None and f is not None, "distinct terminal states each fire"


def test_non_terminal_state_is_noop(monkeypatch):
    _enable(monkeypatch)
    ev = publish_operation_terminal(_Ctx("op-s74-mid"), _State("generating"))
    assert ev is None, "non-terminal state must be a no-op"


def test_publish_once_helper_semantics():
    _clear_idempotency()
    assert _terminal_publish_once("op-x", "applied") is True
    assert _terminal_publish_once("op-x", "applied") is False  # repeat
    assert _terminal_publish_once("op-x", "failed") is True    # different state
    assert _terminal_publish_once("op-y", "applied") is True   # different op


def test_terminal_states_are_lowercase_canonical():
    # Guards the probe/test against the 'COMPLETED' mismatch that would have
    # silently missed the success terminal ('applied').
    assert "applied" in TERMINAL_OPERATION_STATES
    assert "completed" not in TERMINAL_OPERATION_STATES


# --- AST-pin: the orchestrator broadcast is decoupled from `written` ---

def test_orchestrator_publishes_terminal_outside_written_gate():
    src = (Path(__file__).resolve().parents[2]
           / "backend/core/ouroboros/governance/orchestrator.py").read_text()
    # The decoupled call must exist...
    assert "_s74_publish_terminal(ctx, state)" in src
    # ...and appear BEFORE the `if written:` block that follows the probe (i.e.
    # not gated by the ledger write result).
    pub_idx = src.index("_s74_publish_terminal(ctx, state)")
    # The next `if written:` after the LEDGER_TERMINAL probe must come AFTER the
    # decoupled publish.
    probe_idx = src.index("[Slice74Probe] LEDGER_TERMINAL")
    written_idx = src.index("if written:", probe_idx)
    assert pub_idx < written_idx, "terminal publish must be decoupled from `if written`"
