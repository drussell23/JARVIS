"""Every in-flight op is one live pipeline block (ui/pipeline_view).

Pins the queue's invariant 7: every phase transition reaches the cockpit as
live state. The stages are the state machine's own, the block revises in place
as the op advances, and it leaves the live area (frozen empty) when the op
ends, because the closure line is the record.
"""
from __future__ import annotations

from typing import Any, List

import pytest

from backend.core.ouroboros.ui import pipeline_view as pv
from backend.core.ouroboros.ui.block_protocol import FINAL, LIVE, PIPELINE, BlockClock


def _plain(markup: str) -> str:
    from rich.text import Text
    return Text.from_markup(markup).plain


# --------------------------------------------------------------------------
# 1. the stages are derived, not retyped
# --------------------------------------------------------------------------

def test_stages_follow_the_state_machine() -> None:
    from backend.core.ouroboros.governance.op_context import OperationPhase
    names = [s.name for s in pv.stages()]
    order = [p.name for p in OperationPhase]
    assert names == sorted(names, key=order.index), "stages out of enum order"
    for terminal in ("COMPLETE", "CANCELLED", "EXPIRED", "POSTMORTEM"):
        assert terminal not in names
    assert "GENERATE_RETRY" not in names and "VALIDATE_RETRY" not in names


def test_a_retry_counts_toward_its_base_phase() -> None:
    assert pv.stage_of("GENERATE_RETRY") == "GENERATE"
    # VALIDATE_RETRY is reachable from VISUAL_VERIFY too; its base is the
    # earliest phase leading into it.
    assert pv.stage_of("VALIDATE_RETRY") == "VALIDATE"


def test_skippable_phases_appear_only_once_reached() -> None:
    optional = {s.name for s in pv.stages() if s.optional}
    assert {"CONTEXT_EXPANSION", "PLAN", "APPROVE", "VISUAL_VERIFY"} <= optional
    assert not optional & {"CLASSIFY", "ROUTE", "GENERATE", "VALIDATE", "GATE", "APPLY", "VERIFY"}
    st = pv.PipelineState("op-1", sensor="TestFailure")
    st.enter("CLASSIFY")
    strip = _plain(pv.expanded_lines(st)[1])
    assert "plan" not in strip and "approve" not in strip
    st.enter("ROUTE"); st.enter("PLAN")
    assert "plan" in _plain(pv.expanded_lines(st)[1])


def test_the_strip_marks_done_current_pending_and_retries() -> None:
    st = pv.PipelineState("op-1", sensor="TestFailure", summary="tests/x.py")
    for ph in ("CLASSIFY", "ROUTE", "GENERATE", "GENERATE_RETRY", "VALIDATE"):
        st.enter(ph)
    strip = _plain(pv.expanded_lines(st)[1])
    assert "✓ generate ×2" in strip
    assert "● validate" in strip
    assert "○ gate" in strip


def test_agents_render_as_a_tree_under_the_op() -> None:
    st = pv.PipelineState("op-1", sensor="DocStaleness")
    st.enter("GENERATE")
    st.provider, st.round_index, st.tool = "J-Prime", 2, 'Search(risk_tier)'
    lines = [_plain(x) for x in pv.expanded_lines(st, [("REVIEW", "reading 2 files")])]
    assert lines[2].strip().startswith("├ ● Venom") and "round 2" in lines[2]
    assert lines[3].strip().startswith("└ ● REVIEW")


# --------------------------------------------------------------------------
# 2. density: one op expanded, the rest one line, within the budget
# --------------------------------------------------------------------------

def _pipe(clock: BlockClock, op: str, *, generating: bool = False) -> Any:
    st = pv.PipelineState(op, sensor=op)
    st.enter("GENERATE")
    return clock.open(PIPELINE, op_id=op, lines=pv.expanded_lines(st),
                      meta={"collapsed": pv.collapsed_line(st), "generating": generating})


def test_one_op_is_expanded_and_the_others_collapse() -> None:
    clock = BlockClock()
    a, b, c = _pipe(clock, "a"), _pipe(clock, "b", generating=True), _pipe(clock, "c")
    out = pv.arrange([a, b, c], rows=None)
    assert out == [a.meta["collapsed"], *b.lines, c.meta["collapsed"]], \
        "the op holding the model is the expanded one"
    out = pv.arrange([a, b, c], rows=None, focused_op="c")
    assert out[-len(c.lines):] == list(c.lines), "focus wins over generating"


def test_overflow_collapses_to_a_count_not_a_crop() -> None:
    clock = BlockClock()
    blocks = [_pipe(clock, f"op{i}") for i in range(30)]
    out = pv.arrange(blocks, rows=20)
    assert len(out) <= int(20 * pv.live_share())
    assert "/agents" in _plain(out[-1]) and "more" in _plain(out[-1])
    assert out[-1 - len(blocks[-1].lines):-1] == list(blocks[-1].lines), \
        "the expanded op survives whole"


# --------------------------------------------------------------------------
# 3. the production transport publishes it, from the authoritative signal
# --------------------------------------------------------------------------

class _Console:
    def print(self, *_a: Any, **_k: Any) -> None:
        pass


@pytest.fixture()
def transport():
    from backend.core.ouroboros.governance.claude_style_transport import ClaudeStyleTransport
    t = ClaudeStyleTransport(_Console())
    sent: List[Any] = []
    t.block_sink = sent.append
    t.sent = sent
    yield t
    t.shutdown()


def _pipes(sent: List[Any]) -> List[Any]:
    return [b for b in sent if b.kind == PIPELINE]


def test_a_real_phase_transition_revises_the_ops_block(transport) -> None:
    from backend.core.ouroboros.governance.op_context import _notify_phase_transition
    transport._handle_intent("op-777", {"goal": "fix it", "target_files": ["a.py"]})
    _notify_phase_transition("op-777", "GENERATE")
    _notify_phase_transition("op-777", "VALIDATE")
    blocks = _pipes(transport.sent)
    assert blocks and all(b.state == LIVE for b in blocks)
    assert len({b.id for b in blocks}) == 1, "one block per op, revised in place"
    assert [b.rev for b in blocks] == sorted(b.rev for b in blocks)
    assert "● validate" in _plain(blocks[-1].lines[1])


def test_the_block_leaves_the_live_area_when_the_op_ends(transport) -> None:
    from backend.core.ouroboros.governance.op_context import _notify_phase_transition
    transport._handle_intent("op-9", {"goal": "x"})
    _notify_phase_transition("op-9", "GENERATE")
    transport._handle_decision("op-9", {"outcome": "completed"})
    last = _pipes(transport.sent)[-1]
    assert last.state == FINAL and last.lines == (), \
        "frozen EMPTY: the closure line is the record, the block was the view"


def test_a_gate_verdict_joins_the_block(transport) -> None:
    transport._handle_intent("op-g", {"goal": "x"})
    transport._handle_decision("op-g", {"outcome": "notify_apply"})
    assert "notify apply" in _plain("\n".join(_pipes(transport.sent)[-1].lines)).lower()


def test_no_block_renderer_no_pipeline_frames() -> None:
    from backend.core.ouroboros.governance.claude_style_transport import ClaudeStyleTransport
    from backend.core.ouroboros.governance.op_context import _notify_phase_transition
    t = ClaudeStyleTransport(_Console())
    try:
        _notify_phase_transition("op-z", "GENERATE")
        assert not t._pipelines
    finally:
        t.shutdown()


def test_shutdown_releases_the_phase_observer() -> None:
    """The observer list is process-global; a transport that has shut down
    must not keep receiving transitions."""
    from backend.core.ouroboros.governance import op_context
    from backend.core.ouroboros.governance.claude_style_transport import ClaudeStyleTransport
    before = len(op_context._PHASE_TRANSITION_OBSERVERS)
    t = ClaudeStyleTransport(_Console())
    assert len(op_context._PHASE_TRANSITION_OBSERVERS) == before + 1
    t.shutdown()
    assert len(op_context._PHASE_TRANSITION_OBSERVERS) == before


def test_the_legend_uses_the_marks_the_strip_draws() -> None:
    from backend.core.ouroboros.ui.theme import mark
    text = "\n".join(pv.legend())
    for word in ("op", "agent", "gate"):
        assert f"  {word} " in text
    for name in ("action", "check", "pending", "cross"):
        assert mark(name) in text


def test_question_mark_shows_the_legend_with_the_keys() -> None:
    import inspect
    from backend.core.ouroboros.cli import ov
    src = inspect.getsource(ov)
    block = src.split("def _show_help(event: Any) -> None:", 1)[1].split("bind_action(", 1)[0]
    assert "legend()" in block
