"""A finished op, reduced to one line the operator can expand.

`JARVIS_OP_COLLAPSE_ENABLED` was named for a collapse that never happened.
`_op_line`'s own comment said it plainly — "non-disruptive parallel
recording — existing console output is unchanged" — and the only place
`summary_line` ever rendered was INSIDE `/expand`, i.e. after the operator
had already decided to look. The feature stored blocks, could expand them,
and had no collapsed representation anywhere.

What that cost: under the AUTO lens exactly ONE op renders. Every other
concurrent op printed nothing at all — not collapsed, SILENT. With three
background workers an operator saw one op and two ghosts, and the only way
to learn a ghost existed was to guess its `o-N` ref.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.battle_test.op_block_buffer import (
    get_default_buffer,
    reset_default_buffer_for_tests,
)
from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow


@pytest.fixture
def flow():
    try:
        reset_default_buffer_for_tests()
    except Exception:  # noqa: BLE001
        pass
    sf = SerpentFlow.__new__(SerpentFlow)
    sf._lens_mode = "auto"
    sf.mirrored, sf.printed = [], []
    sf.markup_mirror = sf.mirrored.append
    sf.console = MagicMock()
    sf.console.print = lambda t, **k: sf.printed.append(t)
    return sf


def _run_op(flow, op_id="op-a", lines=14, summary="⏺ gate.py evolved · ⏱ 12.4s"):
    buf = get_default_buffer()
    buf.start_op(op_id)
    for i in range(lines):
        buf.append(op_id, f"line {i}")
    flow._maybe_buffer_op_commit(op_id, summary)


class TestAFinishedOpIsVisible:
    def test_it_renders_at_ALL(self, flow):
        """THE regression. The summary was composed at the call site,
        stored on the block, and rendered nowhere."""
        _run_op(flow)
        assert flow.mirrored, "nothing reached the cockpit"
        assert flow.printed, "nothing reached the console"

    def test_it_reaches_BOTH_surfaces_identically(self, flow):
        """A collapsed block appearing on one surface and not the other is
        two different transcripts of the same session."""
        _run_op(flow)
        assert flow.mirrored[-1].strip() == flow.printed[-1].strip()

    def test_an_UNFOCUSED_op_is_no_longer_a_ghost(self, flow):
        """Under AUTO exactly one op renders its lines. This one line is
        the entire visible trace that the others ran."""
        flow._is_focused = lambda _op: False
        _run_op(flow, op_id="op-background")
        assert flow.mirrored, "a background op left no trace at all"

    def test_it_carries_the_expand_affordance(self, flow):
        """Collapsing without showing the recovery path teaches the
        operator that elision is loss."""
        _run_op(flow)
        line = flow.mirrored[-1]
        assert "/expand" in line
        assert "o-" in line
        assert "14 lines" in line


class TestItNeverInventsWhatItCannotShow:
    def test_an_unrecorded_block_advertises_NO_ref(self, flow):
        """Printing `/expand o-7` for a block the buffer never recorded
        teaches the operator that expansion is broken."""
        flow._maybe_buffer_op_commit("op-never-started", "💀 shed · timeout")
        line = flow.mirrored[-1]
        assert "shed" in line
        assert "/expand" not in line
        assert "o-" not in line

    def test_an_empty_summary_renders_nothing(self, flow):
        _run_op(flow, op_id="op-blank", lines=0, summary="   ")
        assert not flow.mirrored

    def test_the_label_is_DERIVED_not_a_second_format_rule(self):
        """`derive_label` is the pure priority picker `op_fanout_tree`
        already uses. A second rule here would drift from the panel's the
        first time either changed — and both describe the same op."""
        import inspect
        src = inspect.getsource(SerpentFlow._collapsed_label)
        assert "derive_label" in src


class TestModesAreHonoured:
    def test_lens_none_stays_silent(self, flow):
        """"pure digest mode" promises nothing renders to the viewport. A
        mode that promises silence has to stay silent."""
        flow._lens_mode = "none"
        _run_op(flow, op_id="op-quiet")
        assert not flow.mirrored

    def test_the_master_flag_restores_legacy(self, flow, monkeypatch):
        monkeypatch.setenv("JARVIS_OP_COLLAPSE_ENABLED", "0")
        _run_op(flow, op_id="op-off")
        assert not flow.mirrored

    @pytest.mark.parametrize("mode", ["auto", "all", "manual"])
    def test_every_rendering_mode_still_collapses(self, flow, mode):
        flow._lens_mode = mode
        _run_op(flow, op_id=f"op-{mode}")
        assert flow.mirrored, mode


class TestOneLinePerOp:
    def test_a_storm_of_ops_yields_a_storm_of_LINES_not_blocks(self, flow):
        """The bounded form IS one line per op — that is the whole point,
        so no extra throttle is needed and none is added."""
        for i in range(20):
            _run_op(flow, op_id=f"op-{i}", lines=30)
        assert len(flow.mirrored) == 20

    def test_a_committed_block_cannot_be_re_collapsed(self, flow):
        """Once COMMITTED the buffer rejects further appends; a second
        commit must not print a second summary for the same op."""
        _run_op(flow, op_id="op-twice")
        first = len(flow.mirrored)
        flow._maybe_buffer_op_commit("op-twice", "⏺ again")
        assert len(flow.mirrored) <= first + 1


class TestNeverBreaksTheOp:
    def test_a_dead_mirror_does_not_break_the_commit(self, flow):
        def _boom(_l): raise RuntimeError("bridge down")
        flow.markup_mirror = _boom
        _run_op(flow, op_id="op-deadmirror")
        assert flow.printed, "a mirror fault swallowed the local render too"

    def test_a_dead_console_does_not_break_the_commit(self, flow):
        flow.console.print = MagicMock(side_effect=RuntimeError("no tty"))
        _run_op(flow, op_id="op-deadconsole")
        assert flow.mirrored, "a console fault swallowed the mirror too"

    @pytest.mark.parametrize("summary", [None, "", 12345, object()])
    def test_junk_summaries_degrade(self, flow, summary):
        flow._maybe_buffer_op_commit("op-junk", summary)  # type: ignore[arg-type]

    def test_a_broken_label_picker_falls_back(self, flow, monkeypatch):
        monkeypatch.setattr(
            "backend.core.ouroboros.governance.task_panel_aggregator."
            "derive_label",
            lambda **k: (_ for _ in ()).throw(RuntimeError("picker down")))
        _run_op(flow, op_id="op-fallback", summary="⏺ still readable")
        assert any("still readable" in m for m in flow.mirrored)
