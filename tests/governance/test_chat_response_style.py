"""A reply in O+V's voice, not a dump of how the reply was decided.

What the operator got for a plain-language goal::

    [chat] · turn: chat-1dc4650228e7 · session: repl
      intent: ACTION_REQUEST (conf=0.67)
      action: backlog_dispatch
      reasons: action_verb
      message: read project_ov_unified_ipc_transceiver.md and build it
      reason: action verb match (conf=0.67)
    [chat] => logged-backlog-chat-1dc4650228e7

Six lines, none of which answer what was asked. It is the CLASSIFIER'S state —
its confidence, its matched heuristic, its routing choice — presented as the
response, with the operator's own words echoed back under a field name.

Same defect as docstrings leaking into the palette as help: maintainer-facing
text in an operator-facing surface. Same fix: keep the information, change who
it addresses and where it lives.
"""
from __future__ import annotations

from typing import Any

import pytest

from backend.core.ouroboros.governance.chat_response_style import (
    ACTION_VOICE,
    acknowledge,
    compose_reply,
    trace_lines,
)


# --------------------------------------------------------------------------
# 1. the reply answers, it does not report on itself
# --------------------------------------------------------------------------

def test_the_classifier_state_is_not_the_reply() -> None:
    """THE defect. None of intent / confidence / heuristic belongs in the
    answer to a question."""
    out = compose_reply(
        "backlog_dispatch", receipt="backlog-chat-1dc",
        turn_id="chat-1dc", intent="ACTION_REQUEST",
        confidence=0.67, reasons=["action_verb"],
    )
    for leak in ("ACTION_REQUEST", "0.67", "action_verb", "intent:", "conf="):
        assert leak not in out, f"{leak!r} leaked into the operator reply"


def test_the_operator_message_is_not_echoed_back() -> None:
    """They just typed it. Repeating it under a field label is what made the
    old output read like a form-submission receipt."""
    msg = "read project_ov_unified_ipc_transceiver.md and build it"
    assert msg not in compose_reply("backlog_dispatch", message=msg)


def test_the_reply_opens_with_a_sentence() -> None:
    out = compose_reply("backlog_dispatch", receipt="r")
    first = out.splitlines()[0]
    assert first.startswith("⏺")
    assert "backlog" in first.lower()
    assert ":" not in first, "a field separator is not a sentence"


def test_it_is_short() -> None:
    """Six lines of internals outweigh the answer no matter how styled."""
    out = compose_reply("backlog_dispatch", receipt="backlog-chat-1dc",
                        turn_id="chat-1dc", intent="ACTION_REQUEST",
                        confidence=0.67, reasons=["action_verb"])
    assert len(out.splitlines()) <= 2


# --------------------------------------------------------------------------
# 2. queued work says it is queued
# --------------------------------------------------------------------------

def test_deferred_work_is_declared_not_disguised() -> None:
    """`backlog_dispatch` hands the goal to a sensor that polls later.
    "logged" reads as done; silence reads as ignored. THIS was the real
    complaint behind "it doesn't start doing the process"."""
    out = compose_reply("backlog_dispatch", receipt="backlog-chat-1dc")
    assert "queued" in out
    assert "Backlog sensor" in out, "nothing says what will collect it"
    assert "logged" not in out


def test_the_receipt_survives_so_the_work_is_traceable() -> None:
    out = compose_reply("backlog_dispatch", receipt="backlog-chat-1dc")
    assert "backlog-chat-1dc" in out


def test_immediate_work_is_not_labelled_queued() -> None:
    """The inverse matters: calling live work "queued" would teach the
    operator to distrust the word."""
    out = compose_reply("subagent_explore", steps=["spawned explorer"])
    assert "queued" not in out


@pytest.mark.parametrize("action", sorted(ACTION_VOICE))
def test_every_known_action_has_a_voice(action: str) -> None:
    """Adding a route means adding a line to ACTION_VOICE — not teaching a
    renderer about a new concept."""
    phrase, deferred = ACTION_VOICE[action]
    assert isinstance(deferred, bool)
    if action != "social_ack":
        assert phrase, f"{action} has no operator-facing phrasing"


def test_an_unknown_action_still_says_something_true() -> None:
    """A new route must degrade to honest, not to blank."""
    out = compose_reply("some_future_route", receipt="r")
    assert "some_future_route" in out


# --------------------------------------------------------------------------
# 3. the reasoning is kept — as trace, below the floor
# --------------------------------------------------------------------------

def test_trace_is_hidden_by_default_and_returns_on_request() -> None:
    """Not deleted: routing bugs are diagnosed from exactly these fields."""
    kw: Any = dict(receipt="r", turn_id="chat-1dc", intent="ACTION_REQUEST",
                   confidence=0.67, reasons=["action_verb"])
    assert "0.67" not in compose_reply("backlog_dispatch", **kw)
    verbose = compose_reply("backlog_dispatch", verbose=True, **kw)
    assert "0.67" in verbose and "action_verb" in verbose


def test_trace_uses_the_verbose_glyph_from_the_style_guide() -> None:
    """§04: `·` is trace. §05's severity ladder — which /breadcrumbs already
    filters by — then hides it without this module knowing how."""
    line = trace_lines(intent="ACTION_REQUEST", confidence=0.5)[0]
    assert line.strip().startswith("·")


def test_trace_collapses_to_one_line() -> None:
    assert len(trace_lines(
        turn_id="chat-1dc4650228e7", session_id="repl",
        intent="ACTION_REQUEST", confidence=0.67,
        reasons=["action_verb", "imperative"], reason="action verb match",
    )) == 1


def test_the_turn_id_shows_its_distinguishing_END() -> None:
    """Chat ids share a prefix — the same lesson the op-id digest learned."""
    line = trace_lines(turn_id="chat-1dc4650228e7", intent="X")[0]
    assert "1dc4650228e7" in line and "chat-1dc4650228e7" not in line


def test_trace_with_nothing_to_say_emits_nothing() -> None:
    assert trace_lines() == []


# --------------------------------------------------------------------------
# 4. a social turn is a reply, not an event
# --------------------------------------------------------------------------

def test_a_greeting_renders_as_speech() -> None:
    out = compose_reply("social_ack", social_reply="Hey.")
    assert out == "⏺ Hey."
    assert "queued" not in out and "social_ack" not in out


def test_a_social_turn_carries_no_chrome() -> None:
    """Wrapping "Hey." in op chrome would make it look like a system event."""
    assert "⎿" not in compose_reply("social_ack", social_reply="Hey.")


# --------------------------------------------------------------------------
# 5. it cannot swallow a response
# --------------------------------------------------------------------------

@pytest.mark.parametrize("junk", [None, 42, object(), ["x"]])
def test_composition_never_raises(junk: Any) -> None:
    assert isinstance(compose_reply(junk, receipt="r"), str)  # type: ignore[arg-type]


def test_a_formatting_fault_still_yields_the_receipt() -> None:
    """A style bug must never cost the operator a real answer."""
    out = compose_reply("backlog_dispatch", receipt="backlog-chat-1dc",
                        reasons=None)  # type: ignore[arg-type]
    assert "backlog-chat-1dc" in out


def test_acknowledge_alone_is_safe() -> None:
    assert isinstance(acknowledge("anything"), str)


# --------------------------------------------------------------------------
# 6. wiring — and what deliberately did NOT change
# --------------------------------------------------------------------------

def test_render_decision_is_kept_for_chat_why() -> None:
    """`/chat why <turn>` is where the routing internals ARE the answer. The
    old renderer is correct there and must survive."""
    from backend.core.ouroboros.governance.chat_repl_dispatcher import (
        render_decision,
    )
    assert callable(render_decision)


def test_the_reply_path_uses_the_composer() -> None:
    import inspect
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "backend/core/ouroboros/governance/chat_repl_dispatcher.py"
           ).read_text()
    assert "_compose_operator_reply(" in src
    assert '[chat] => ' not in src, "the raw receipt line is still being emitted"


def test_the_trace_floor_is_read_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So a live `/breadcrumbs verbose` takes effect on the next reply rather
    than the next restart."""
    from backend.core.ouroboros.governance import chat_repl_dispatcher as d

    monkeypatch.delenv("JARVIS_CHAT_TRACE", raising=False)
    assert d._chat_trace_enabled() is False
    monkeypatch.setenv("JARVIS_CHAT_TRACE", "verbose")
    assert d._chat_trace_enabled() is True


# --------------------------------------------------------------------------
# 7. immediate dispatch — "on it", not "queued"  (2026-07-27)
# --------------------------------------------------------------------------

def test_a_running_op_says_on_it_not_queued() -> None:
    """An op-id receipt means intake accepted the goal and a worker has it.
    Calling that "queued" is the same dishonesty in the opposite direction."""
    out = compose_reply("backlog_dispatch", receipt="op-019fa4d2-246e-7759-86")
    assert "on it" in out
    assert "queued" not in out
    assert "Backlog sensor" not in out


def test_a_filed_goal_still_says_queued() -> None:
    """The fallback path is unchanged — and must stay honest about waiting."""
    out = compose_reply("backlog_dispatch", receipt="chat:chat-1dc")
    assert "queued" in out and "Backlog sensor" in out
    # Asserted on the ACKNOWLEDGEMENT LINE, not the whole reply: "on it" is a
    # substring of "…pick it up on its next sweep", so a naive `not in`
    # flags correct output as broken.
    assert out.splitlines()[0] == "⏺ adding that to the backlog"


def test_the_two_paths_are_told_apart_by_the_RECEIPT() -> None:
    """Not by a second action token: the classifier cannot predict which path
    the executor will manage, and asking it to would put routing knowledge in
    the wrong layer."""
    from backend.core.ouroboros.governance.chat_response_style import (
        _dispatched_now,
    )
    assert _dispatched_now("op-019fa4d2-246e") is True
    assert _dispatched_now("chat:chat-1dc") is False
    assert _dispatched_now("") is False
    assert _dispatched_now("error-append-failed-x") is False


def test_the_operator_gets_the_ref_the_chrome_will_narrate_under() -> None:
    """So the ⏺/⎿ op lines that follow are recognisably the answer to what
    they just typed, rather than unrelated autonomous traffic."""
    out = compose_reply("backlog_dispatch", receipt="op-019fa4d2-246e-7759-86")
    assert "7759-86" in out


def test_the_full_uuid_is_not_repeated_underneath() -> None:
    """The digest work removed UUID noise everywhere else; this must not
    reintroduce it."""
    out = compose_reply("backlog_dispatch", receipt="op-019fa4d2-246e-7759-86")
    assert out.count("019fa4d2") == 0


def test_immediate_replies_stay_two_lines() -> None:
    out = compose_reply("backlog_dispatch", receipt="op-019fa4d2-246e-7759-86")
    assert len(out.splitlines()) == 2
