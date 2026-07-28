"""Esc stops what YOU asked for — not the organism.

`request_cancel` and the `/cancel <op-id>` verb both predate this. What was
missing is a keystroke: interrupting meant reading an op id off a moving
screen and typing it, at exactly the moment you want something to stop.

The scope decision is the load-bearing one. A bare cancel that reached
autonomous work would let ONE keystroke kill a soak — and an operator who
discovers that stops trusting the key. So Esc targets operator-initiated ops
only, narrow by construction rather than by warning.

Layering came free: the existing Esc binding is filtered to `not_flow`
(leave SELECT/FOCUS), so FLOW was unclaimed. One key, two meanings,
disambiguated by what is on screen rather than by a modifier to remember.
"""
from __future__ import annotations

import ast
from collections import deque
from pathlib import Path
from typing import Any, List

import pytest

_REPO = Path(__file__).resolve().parents[2]
_OV = _REPO / "backend/core/ouroboros/cli/ov.py"
_GLS = _REPO / "backend/core/ouroboros/governance/governed_loop_service.py"
_FLOW = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


class _GLS:
    """The two methods the interrupt path needs, on a real-ish shape."""

    def __init__(self) -> None:
        self._active_ops: set = set()
        self._cancel_requested: set = set()

    # bound methods lifted from the real service
    from backend.core.ouroboros.governance.governed_loop_service import (  # noqa: E402
        GovernedLoopService as _Real,
    )
    note_operator_op = _Real.note_operator_op
    operator_ops_active = _Real.operator_ops_active
    request_cancel = _Real.request_cancel


# --------------------------------------------------------------------------
# 1. scope — it cancels YOUR work
# --------------------------------------------------------------------------

def test_only_operator_ops_are_offered_as_targets() -> None:
    """THE decision. Autonomous work must survive a keystroke."""
    gls = _GLS()
    gls._active_ops = {"op-mine", "op-autonomous-sensor"}
    gls.note_operator_op("op-mine")
    assert gls.operator_ops_active() == ["op-mine"]


def test_the_most_recent_op_is_first() -> None:
    """Esc means "stop the thing I just started", not an arbitrary one."""
    gls = _GLS()
    gls._active_ops = {"op-a", "op-b"}
    gls.note_operator_op("op-a")
    gls.note_operator_op("op-b")
    assert gls.operator_ops_active()[0] == "op-b"


def test_a_finished_op_is_never_offered() -> None:
    """Cancelling something already done would report success and change
    nothing — worse than reporting nothing to cancel."""
    gls = _GLS()
    gls.note_operator_op("op-done")
    gls._active_ops = set()
    assert gls.operator_ops_active() == []


def test_nothing_of_mine_running_yields_no_target() -> None:
    gls = _GLS()
    gls._active_ops = {"op-autonomous"}
    assert gls.operator_ops_active() == []


def test_the_recency_ring_is_bounded() -> None:
    """A recency hint for interrupt targeting, not a ledger."""
    gls = _GLS()
    for i in range(200):
        gls.note_operator_op(f"op-{i}")
    assert len(gls._operator_ops) <= 16
    assert isinstance(gls._operator_ops, deque)


def test_duplicates_do_not_multiply_targets() -> None:
    gls = _GLS()
    gls._active_ops = {"op-x"}
    for _ in range(5):
        gls.note_operator_op("op-x")
    assert gls.operator_ops_active() == ["op-x"]


@pytest.mark.parametrize("junk", ["", None])
def test_noting_junk_never_raises(junk: Any) -> None:
    gls = _GLS()
    gls.note_operator_op(junk)
    assert gls.operator_ops_active() == []


def test_a_cancel_actually_registers() -> None:
    """The path end to end: recorded → offered → cancelled."""
    gls = _GLS()
    gls._active_ops = {"op-mine"}
    gls.note_operator_op("op-mine")
    assert gls.request_cancel(gls.operator_ops_active()[0]) is True
    assert "op-mine" in gls._cancel_requested


# --------------------------------------------------------------------------
# 2. a bare cancel means "mine"
# --------------------------------------------------------------------------

def _cancel_body() -> str:
    for node in ast.walk(ast.parse(_FLOW.read_text())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name == "_handle_cancel":
            return ast.unparse(node)
    return ""


def test_bare_cancel_resolves_to_operator_ops() -> None:
    body = _cancel_body()
    assert body, "_handle_cancel is gone"
    assert "operator_ops_active" in body


def test_bare_cancel_with_nothing_of_yours_says_so() -> None:
    """And says autonomous work continues — otherwise silence reads as "it
    didn't work"."""
    body = _cancel_body()
    assert "nothing of yours is running" in body
    assert "autonomous work keeps going" in body


def test_an_explicit_op_id_still_targets_that_op() -> None:
    """`/cancel <op-id>` predates this and must be untouched."""
    body = _cancel_body()
    assert "request_cancel" in body


# --------------------------------------------------------------------------
# 3. the keystroke
# --------------------------------------------------------------------------

def _ov_src() -> str:
    return _OV.read_text()


def test_esc_in_flow_interrupts() -> None:
    """Esc-in-FLOW now routes through the remappable keymap — the pin
    follows the seam: the action is declared with the escape default and
    the SAME in_flow_working gate."""
    src = _ov_src()
    block = src.split('"chat:interrupt"')[1][:200]
    assert '("escape",)' in block and "filter=in_flow_working" in block


def test_the_existing_escape_still_leaves_select_and_focus() -> None:
    """One key, two meanings — and the older one must not be displaced."""
    src = _ov_src()
    block = src.split('"deck:escape"')[1][:200]
    assert '("escape",)' in block and "filter=not_flow" in block


def test_the_two_bindings_do_not_overlap() -> None:
    """`not_flow` and `in_flow_working` are disjoint by construction, so no
    precedence rule has to be remembered."""
    src = _ov_src()
    assert "def in_flow_working() -> bool:" in src
    body = src.split("def in_flow_working() -> bool:")[1][:600]
    assert "MODE_FLOW" in body


def test_an_idle_esc_does_nothing() -> None:
    """A key that answers when it was not asked trains the operator to ignore
    it."""
    body = _ov_src().split("def in_flow_working() -> bool:")[1][:600]
    assert '_active_ops' in body


def test_it_reuses_the_cancel_verb_rather_than_a_new_frame() -> None:
    """The keystroke and the typed verb resolve through ONE path, so they
    cannot drift apart."""
    src = _ov_src()
    assert 'client.send_input("/cancel")' in src


def test_the_operator_gets_feedback() -> None:
    """An interrupt with no acknowledgement is indistinguishable from a
    dropped keystroke."""
    assert 'ui.flash("interrupting' in _ov_src()


def test_the_harness_records_the_op_it_dispatches() -> None:
    """Recorded BEFORE dispatch resolves: Esc must be able to target an op
    the moment the operator sees "dispatched"."""
    import ast

    # AST, not a character window. A fixed slice measures how much prose sits
    # between two calls — which is not the invariant, and is the mistake this
    # test made on its first run.
    src = (_REPO / "backend/core/ouroboros/battle_test/harness.py").read_text()
    body = ""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and \
                node.name == "_submit_operator_goal":
            body = ast.unparse(node)
            break
    assert body, "_submit_operator_goal is gone"
    assert "note_operator_op" in body
    assert body.index("note_operator_op") < body.index("submit(envelope)")
