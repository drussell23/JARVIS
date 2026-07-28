"""The turn spinner — one live row, bound to the operator's own question.

Pins the four properties that make it a TURN spinner rather than a second
toolbar:

  1. it exists only while a turn is open (an idle cockpit is unchanged);
  2. it enriches from the heartbeat ONLY when that frame describes THIS
     turn — an autonomous op that predates the question can never lend
     its token count to it;
  3. it resolves on every path (reply, interrupt, ceiling) — a spinner
     that can outlive its answer reads as "still working" forever;
  4. one row, whatever the traffic: a second question extends it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import turn_spinner as ts


def _spinner(**kw):
    clock = {"t": 1000.0}
    state = {"hb": None, "emitted": []}
    sp = ts.TurnSpinner(
        heartbeat_fn=lambda: state["hb"],
        now_fn=lambda: clock["t"],
        emit_fn=state["emitted"].append,
        **kw,
    )
    return sp, clock, state


# --------------------------------------------------------------------------
# 1. presence
# --------------------------------------------------------------------------

def test_idle_renders_nothing() -> None:
    sp, _clock, _state = _spinner()
    assert sp.render() == ""
    assert sp.active is False


def test_open_renders_a_local_clock_immediately() -> None:
    sp, clock, _state = _spinner()
    sp.open("what is O+V?")
    clock["t"] += 4
    row = sp.render()
    assert "4s" in row and "…" in row
    assert sp.active


def test_master_flag_off_never_opens(monkeypatch) -> None:
    monkeypatch.setenv(ts.MASTER_FLAG_ENV_VAR, "false")
    sp, _clock, _state = _spinner()
    assert sp.open("hi") is False
    assert sp.render() == ""


def test_verb_is_stable_per_question() -> None:
    """A verb re-rolled on every repaint would flicker between words."""
    sp1, _c1, _s1 = _spinner()
    sp2, _c2, _s2 = _spinner()
    sp1.open("identical question")
    sp2.open("identical question")
    assert sp1.render().split("…")[0] == sp2.render().split("…")[0]


def test_operator_can_extend_the_vocabulary(monkeypatch) -> None:
    monkeypatch.setenv(ts.VERBS_ENV_VAR, "Brooding, Percolating")
    assert "Brooding" in ts.turn_verbs()
    assert "Percolating" in ts.turn_verbs()


# --------------------------------------------------------------------------
# 2. turn-scoped honesty — THE property
# --------------------------------------------------------------------------

def test_heartbeat_inside_this_turn_is_adopted() -> None:
    sp, clock, state = _spinner()
    sp.open("what is O+V?")
    clock["t"] += 5
    state["hb"] = {
        "active": True, "verb": "Synthesizing", "elapsed_s": 4.4,
        "tokens_total": 1530, "provider_label": "DW-397B",
    }
    row = sp.render()
    assert "Synthesizing" in row
    assert "1.5k tokens" in row
    assert "DW-397B" in row


def test_heartbeat_that_predates_the_turn_is_refused() -> None:
    """An autonomous op already running when the operator hit Enter must
    never lend its token count to their question."""
    sp, clock, state = _spinner()
    sp.open("what is O+V?")
    clock["t"] += 3
    state["hb"] = {
        "active": True, "verb": "Repairing", "elapsed_s": 900.0,
        "tokens_total": 99999, "provider_label": "Claude",
    }
    row = sp.render()
    assert "Repairing" not in row
    assert "99999" not in row and "100.0k" not in row
    assert "3s" in row              # the honest local clock survives


def test_inactive_heartbeat_is_ignored() -> None:
    sp, clock, state = _spinner()
    sp.open("q")
    clock["t"] += 2
    state["hb"] = {"active": False, "verb": "Idle", "elapsed_s": 1.0}
    assert "Idle" not in sp.render()


def test_malformed_heartbeat_degrades_to_local_clock() -> None:
    sp, clock, state = _spinner()
    sp.open("q")
    clock["t"] += 2
    for bad in (None, "not-a-dict", {}, {"active": True, "elapsed_s": "x"}):
        state["hb"] = bad
        assert "2s" in sp.render()


# --------------------------------------------------------------------------
# 3. total resolution
# --------------------------------------------------------------------------

def test_reply_closes_the_turn_and_leaves_a_tombstone() -> None:
    sp, clock, state = _spinner()
    sp.open("q")
    clock["t"] += 6
    sp.note_reply()
    assert sp.active is False
    assert sp.render() == ""
    assert any("thought for 6s" in line for line in state["emitted"])


def test_a_fast_turn_leaves_no_tombstone() -> None:
    """Sub-threshold turns narrating their own duration is noise."""
    sp, clock, state = _spinner()
    sp.open("q")
    clock["t"] += 0.4
    sp.note_reply()
    assert state["emitted"] == []


def test_ceiling_resolves_an_unanswered_turn(monkeypatch) -> None:
    monkeypatch.setenv(ts.MAX_TURN_ENV_VAR, "30")
    sp, clock, state = _spinner()
    sp.open("q")
    clock["t"] += 31
    assert sp.render() == ""            # tick() fires from the render path
    assert sp.active is False
    assert any("no answer" in line for line in state["emitted"])
    # honest about uncertainty: the daemon may still be working
    assert any("may still be working" in line
               for line in state["emitted"])


def test_interrupt_resolves_with_its_own_voice() -> None:
    sp, clock, state = _spinner()
    sp.open("q")
    clock["t"] += 9
    sp.close(reason="interrupted")
    assert sp.active is False
    assert any("interrupted after 9s" in line for line in state["emitted"])


def test_close_is_idempotent() -> None:
    sp, clock, state = _spinner()
    sp.open("q")
    clock["t"] += 5
    sp.close()
    sp.close()
    sp.note_reply()
    assert len(state["emitted"]) == 1


def test_reply_without_an_open_turn_is_a_noop() -> None:
    sp, _clock, state = _spinner()
    sp.note_reply()
    assert sp.active is False and state["emitted"] == []


# --------------------------------------------------------------------------
# 4. one row, whatever the traffic
# --------------------------------------------------------------------------

def test_second_question_extends_rather_than_restarts() -> None:
    sp, clock, _state = _spinner()
    sp.open("first")
    clock["t"] += 10
    sp.open("second")
    assert sp.pending == 2
    row = sp.render()
    assert "10s" in row              # the ORIGINAL wait, not a reset clock
    assert "2 queued" in row


def test_row_closes_only_when_the_last_answer_lands() -> None:
    sp, clock, _state = _spinner()
    sp.open("first")
    sp.open("second")
    sp.note_reply()
    assert sp.active is True
    sp.note_reply()
    assert sp.active is False


# --------------------------------------------------------------------------
# 5. rendering + wiring
# --------------------------------------------------------------------------

def test_markup_to_ansi_emits_escapes_and_survives_junk() -> None:
    out = ts._markup_to_ansi("[#43d6d0]hello[/#43d6d0]")
    assert "hello" in out
    assert "\x1b[" in out
    assert "hello" in ts._markup_to_ansi("[unclosed hello")


def test_row_is_a_conditional_container_not_a_reserved_line() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.layout import ConditionalContainer
    sp, _clock, _state = _spinner()
    row = ts.build_turn_row(sp)
    assert isinstance(row, ConditionalContainer)


def test_both_cockpits_mount_the_row() -> None:
    import inspect
    from backend.core.ouroboros.battle_test import bipartite_layout
    src = inspect.getsource(bipartite_layout.build_bipartite_application)
    # mounted ABOVE the prompt, like Claude Code
    assert src.index("build_turn_row") < src.index("rows += [_rule(), prompt")
    ov_src = Path(
        __import__(
            "backend.core.ouroboros.cli.ov", fromlist=["x"],
        ).__file__
    ).read_text()
    assert "ui.turn_spinner = TurnSpinner(" in ov_src
    assert 'turn_spinner=getattr(ui, "turn_spinner", None),' in ov_src


def test_client_opens_on_send_and_closes_on_addressed_reply() -> None:
    ov_src = Path(
        __import__(
            "backend.core.ouroboros.cli.ov", fromlist=["x"],
        ).__file__
    ).read_text()
    # opens where the line is proven to be leaving for the daemon
    route = ov_src.split("def _route_operator_line")[1].split("\ndef ")[0]
    assert "spinner.open(text)" in route
    # closes on the ADDRESSED branch only — ambient work must not close it
    sink = ov_src.split("def _markup_sink")[1].split("\n        client =")[0]
    assert "spinner.note_reply()" in sink
    assert sink.index("spinner.note_reply()") < sink.index("ui.on_ambient")


def test_daemon_cockpit_closes_on_its_own_dispatch() -> None:
    import inspect
    from backend.core.ouroboros.battle_test import serpent_flow
    src = inspect.getsource(serpent_flow.SerpentREPL._loop)
    assert "_turn_spinner" in src
    assert "add_done_callback" in src
