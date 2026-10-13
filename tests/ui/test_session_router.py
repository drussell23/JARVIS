"""Session-mode routing (FOREGROUND_SESSION §4, invariants 3, 4, 5).

3. every frame is routed exactly once, and nothing sidelined is unaccounted;
4. leaving a session restores flow and replays the background digest once;
5. a FATAL ambient event always reaches the operator.
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.core.ouroboros.ui.session_router import SIDELINE, TRANSCRIPT, SessionRouter


def _router(**kw):
    sev = {"boom": "FATAL", "careful": "WARN"}
    return SessionRouter(classify=lambda t: next((v for k, v in sev.items() if k in t), "INFO"), **kw)


def _blk(op, phase="GENERATE"):
    return SimpleNamespace(op_id=op, meta={"phase": phase})


def test_the_buffer_is_markup_a_raw_line_stays_literal_and_markup_renders():
    """/bg renders the buffer as markup. A raw line is inert data (escaped
    on entry, so its brackets print literally); a markup frame keeps its
    styling instead of printing as tag source."""
    from rich.text import Text

    r = _router()
    r.apply_mode({"session": "s-1"})
    r.route_line("list[int] [bold]not styling[/bold]")
    r.route_markup("[cyan]●[/] [bold]Operation[/bold]", addressed=False)
    raw, styled = (Text.from_markup(x) for x in r.background())
    assert raw.plain == "list[int] [bold]not styling[/bold]"
    assert styled.plain == "● Operation" and styled.spans


def test_outside_a_session_nothing_changes():
    r = _router()
    assert r.route_markup("anything", addressed=False) == TRANSCRIPT
    assert r.route_line("raw") == TRANSCRIPT
    assert r.route_block("op-1", ["x"]) == TRANSCRIPT
    blocks = [_blk("a"), _blk("b")]
    assert r.split_live(blocks) == (blocks, [])
    assert r.summary_row(2) == ""


def test_in_a_session_only_the_conversation_and_its_ops_are_foreground():
    r = _router()
    r.apply_mode({"session": "s-1", "owned_ops": ["op-mine"]})
    assert r.route_markup("the answer", addressed=True) == TRANSCRIPT
    assert r.route_markup("worker spawned", addressed=False) == SIDELINE
    assert r.route_block("op-mine", ["landed"]) == TRANSCRIPT
    assert r.route_block("op-other", ["a", "b"]) == SIDELINE
    shown, bg = r.split_live([_blk("op-mine"), _blk("op-other")])
    assert [b.op_id for b in shown] == ["op-mine"] and [b.op_id for b in bg] == ["op-other"]


def test_fatal_breaks_through_and_warnings_are_counted():
    r = _router()
    r.apply_mode({"session": "s-1"})
    assert r.route_markup("boom: daemon panic", addressed=False) == TRANSCRIPT
    assert r.route_markup("careful: memory high", addressed=False) == SIDELINE
    assert r.warnings == 1 and "careful" in r.last_warning
    assert "1 warning" in r.summary_row(0)


def test_every_sidelined_frame_is_accounted_even_after_the_buffer_wraps():
    r = _router()
    r.buffer = type(r.buffer)(maxlen=5)
    r.apply_mode({"session": "s-1"})
    for i in range(12):
        r.route_line(f"line {i}")
    r.route_block("op-x", ["b1", "b2"])
    assert r.sidelined == 14 and len(r.buffer) == 5
    bg = r.background()
    assert bg[0].startswith("… 9 earlier") and bg[-1] == "b2"


def test_leaving_replays_once_and_keeps_bg_until_the_next_session():
    r = _router()
    r.apply_mode({"session": "s-1"})
    r.route_line("something happened")
    replay = r.apply_mode({"session": ""})
    assert replay and "1 background line" in replay[0]
    assert r.apply_mode({"session": ""}) is None          # once
    assert r.route_line("after") == TRANSCRIPT              # flow restored
    assert r.background() == ["something happened"]         # /bg still answers
    r.apply_mode({"session": "s-2"})
    assert r.background() == [] and r.sidelined == 0


def test_a_background_op_leaving_the_live_area_counts_as_finished_not_landed():
    r = _router()
    r.apply_mode({"session": "s-1", "owned_ops": ["op-mine"]})
    r.split_live([_blk("op-mine", "VERIFY"), _blk("op-bg", "VERIFY")])
    r.split_live([])
    assert r.finished == {"op-bg"}
    assert "1 finished" in r.summary_row(0) and "landed" not in r.summary_row(0)


def test_the_manager_tells_the_cockpit_when_it_enters_and_leaves(tmp_path):
    from backend.core.ouroboros.governance import foreground_session as fs
    sent = []
    m = fs.SessionManager(emit=lambda *_: None, log_dir=tmp_path,
                          mode_notify=lambda c, sid, ops: sent.append((c, sid, ops)))
    s = m.start("ck1")
    m.end("ck1")
    assert sent == [("ck1", s.id, []), ("ck1", "", [])]
