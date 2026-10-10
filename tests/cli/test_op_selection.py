"""^X ^L selects an OP, not only a lane; focusing it expands its pipeline block.

The live area expands one op (ui/pipeline_view.arrange): the focused one,
else the one holding the model, else the newest. Focus therefore has to be
reachable for ops, and it is cockpit-side: there is no lane history to fetch.
"""
from __future__ import annotations

from typing import Any, List

import pytest

from backend.core.ouroboros.ui import pipeline_view as pv
from backend.core.ouroboros.ui.block_protocol import PIPELINE, TOOL, BlockClock


def _pipe(clock: BlockClock, op: str) -> Any:
    st = pv.PipelineState(op, sensor="TestFailure", summary=op)
    st.enter("GENERATE")
    return clock.open(PIPELINE, op_id=op, lines=pv.expanded_lines(st),
                      meta={"collapsed": pv.collapsed_line(st)})


@pytest.fixture()
def ui(monkeypatch):
    monkeypatch.setenv("JARVIS_COCKPIT_SELECTION", "1")
    from backend.core.ouroboros.cli.ov import AttachUI
    u = AttachUI()
    if u.fsm is None:
        pytest.skip("cockpit FSM unavailable")
    return u


def test_ops_are_selectable_ahead_of_lanes(ui) -> None:
    clock = BlockClock()
    ui._lanes = [{"lane": "unit/abc", "lines": 3, "age_s": 1}]
    ui.set_op_rows([_pipe(clock, "op-a"), clock.open(TOOL, op_id="op-a"), _pipe(clock, "op-b")])
    assert [r["lane"] for r in ui.fsm.rows()] == ["op-a", "op-b", "unit/abc"]
    assert ui.fsm.enter_select()
    lines = "\n".join(ui._mode_lines() or [])
    assert "TestFailure" in lines and "unit/abc" in lines


def test_focusing_an_op_expands_it_and_relayouts(ui) -> None:
    clock = BlockClock()
    blocks = [_pipe(clock, "op-a"), _pipe(clock, "op-b")]
    ui.set_op_rows(blocks)
    calls: List[int] = []
    ui.relayout_live = lambda: calls.append(1)
    ui.fsm.enter_select()
    ui.fsm.focus_selected()
    ui.refresh()
    assert ui.fsm.focused_lane == "op-a" and calls, "focus must re-arrange the live area"
    assert "expanded above" in "\n".join(ui._mode_lines() or [])
    out = pv.arrange(blocks, rows=None, focused_op=ui.fsm.focused_lane)
    assert out[:len(blocks[0].lines)] == list(blocks[0].lines)


def test_a_focused_op_that_ends_returns_to_flow(ui) -> None:
    from backend.core.ouroboros.battle_test.cockpit_fsm import MODE_FLOW
    clock = BlockClock()
    a = _pipe(clock, "op-a")
    ui.set_op_rows([a])
    ui.fsm.enter_select()
    ui.fsm.focus_selected()
    ui.set_op_rows([])
    assert ui.fsm.mode == MODE_FLOW
    assert "op-a finished" in (ui._flash_text or "")
