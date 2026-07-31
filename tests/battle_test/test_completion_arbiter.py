"""Regression spine for Tab arbitration.

The reported bug: the prompt shows grey ghost text (``wha`` offers ``what is
O+V?``) and Tab does nothing. Both halves were behaving as designed —
prompt_toolkit gives the COMPLETER Tab and the SUGGESTION ``→``, and the verb
completer correctly declines to fire on prose. What was missing was an arbiter
deciding which source Tab means right now.

The tests split three ways:

* **The ladder** — a pure function, so priority order is verified without a
  terminal, an Application, or prompt_toolkit at all.
* **The shared predicate** — one definition of "completion territory",
  consumed by BOTH completers and the arbiter. Two copies would drift the
  first time a trigger character changed, and the symptom would be a dead Tab
  again.
* **Surface parity** — the arbiter must mount on every surface the operator
  can type into. A capability present on one and absent on another is the
  class of defect that left `search_rows` dark on the shipping client.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.completion_arbiter import (
    ACTION_ACCEPT_WORD,
    ACTION_SMART_COMPLETE,
    _first_word,
    arbiter_enabled,
    install_completion_arbiter,
    resolve_tab_action,
)
from backend.core.ouroboros.battle_test.repl_completion import (
    completion_would_trigger,
    mention_completion_triggers,
    slash_completion_triggers,
)

REPO = Path(__file__).resolve().parents[2]
OV = REPO / "backend/core/ouroboros/cli/ov.py"
OV_DEMO = REPO / "backend/core/ouroboros/cli/ov_demo.py"
COMPLETION = REPO / "backend/core/ouroboros/battle_test/repl_completion.py"


def _ladder(**kw):
    base = dict(menu_open=False, text_before_cursor="", has_suggestion=False,
                cursor_at_end=True)
    base.update(kw)
    return resolve_tab_action(**base)


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def test_the_reported_bug_now_accepts():
    """`wha` + visible ghost text + Tab == accept. This is the report."""
    assert _ladder(text_before_cursor="wha", has_suggestion=True) == "accept"


def test_an_open_menu_is_never_stolen_from():
    """Tab must not accept a suggestion out from under a visible menu."""
    assert _ladder(menu_open=True, text_before_cursor="/mem",
                   has_suggestion=True) == "menu"


def test_verb_completion_still_wins_over_history():
    """The fix must not trade one broken half for the other.

    Binding Tab straight to "accept suggestion" would have stopped `/mem`
    completing to `/memory` — the case Tab already got right.
    """
    assert _ladder(text_before_cursor="/mem", has_suggestion=True) == "complete"


def test_mention_completion_wins_over_history():
    assert _ladder(text_before_cursor="@backend/co",
                   has_suggestion=True) == "complete"


def test_suggestion_is_only_accepted_at_the_very_end():
    """prompt_toolkit renders ghost text only at the end of the buffer.
    Accepting from mid-line would splice history into a sentence."""
    assert _ladder(text_before_cursor="wha", has_suggestion=True,
                   cursor_at_end=False) == "indent"


def test_empty_buffer_opens_the_palette():
    """The cheapest discoverability affordance a CLI has."""
    assert _ladder(text_before_cursor="") == "complete"
    assert _ladder(text_before_cursor="   ") == "complete"


def test_prose_with_nothing_to_offer_indents():
    assert _ladder(text_before_cursor="hello world") == "indent"


def test_ladder_is_pure_and_never_raises():
    for bad in (None, 123, object()):
        assert resolve_tab_action(
            menu_open=False, text_before_cursor=bad,  # type: ignore[arg-type]
            has_suggestion=False, cursor_at_end=True) in (
                "menu", "complete", "accept", "indent")


def test_trigger_predicate_failure_falls_through_to_accept(monkeypatch):
    """Fail-CLOSED to "not completion territory".

    If the predicate cannot be consulted, the arbiter must still accept a
    visible suggestion — guessing "completion" would restore the dead Tab,
    which is the bug.
    """
    import backend.core.ouroboros.battle_test.completion_arbiter as ca

    def boom(_):
        raise RuntimeError("predicate unavailable")

    monkeypatch.setattr(ca, "_would_trigger_completion", boom)
    assert ca.resolve_tab_action(
        menu_open=False, text_before_cursor="wha", has_suggestion=True,
        cursor_at_end=True) == "indent"


# ---------------------------------------------------------------------------
# The shared predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("/mem", True),
    ("/", True),
    ("wha", False),
    ("what is O+V?", False),
    ("", False),
    ("@backend", True),
    ("@", True),
    ("hello @src/a b", False),   # mention token already finished
    ("x@y", True),               # unfinished token after an @
])
def test_completion_territory(text, expected):
    assert completion_would_trigger(text) is expected


def test_predicate_is_the_union_of_its_two_halves():
    for text in ("/a", "@a", "plain", "", "a @b"):
        assert completion_would_trigger(text) is (
            slash_completion_triggers(text) or mention_completion_triggers(text))


def test_completers_consume_the_predicate_rather_than_copying_it():
    """The anti-drift guard, and the reason this bug is not re-reachable.

    Both completers must CALL the shared predicate. An inline
    ``text.startswith("/")`` inside a completer would be a second definition
    of "completion territory", and the arbiter would disagree with it the
    first time a trigger character was added.
    """
    tree = ast.parse(COMPLETION.read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "slash_completion_triggers" in called
    assert "mention_completion_triggers" in called


def test_predicates_never_raise():
    for bad in (None, 123, object()):
        assert completion_would_trigger(bad) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Word-wise acceptance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("what is O+V?", "what"),
    (" is O+V?", " is"),
    ("  leading", "  leading"),
    ("single", "single"),
    ("", ""),
])
def test_first_word_takes_the_separator_with_the_word(text, expected):
    """Repeated presses must WALK forward, not stall on the space."""
    assert _first_word(text) == expected


def test_word_acceptance_converges_on_the_whole_suggestion():
    remaining = "what is O+V?"
    accepted = ""
    for _ in range(10):
        word = _first_word(remaining)
        if not word:
            break
        accepted += word
        remaining = remaining[len(word):]
    assert accepted == "what is O+V?"


# ---------------------------------------------------------------------------
# Installation + surface parity
# ---------------------------------------------------------------------------


def _kb():
    from prompt_toolkit.key_binding import KeyBindings
    return KeyBindings()


def test_install_binds_and_is_idempotent():
    """A surface that mounts twice (attach / detach / re-attach) must not
    stack handlers and accept a suggestion twice on one keypress."""
    kb = _kb()
    assert install_completion_arbiter(kb) is True
    first = len(kb.bindings)
    assert install_completion_arbiter(kb) is True
    assert len(kb.bindings) == first


def test_install_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_COMPLETION_ARBITER_ENABLED", "0")
    kb = _kb()
    assert install_completion_arbiter(kb) is False
    assert len(kb.bindings) == 0


def test_install_never_raises_on_a_bad_kb():
    assert install_completion_arbiter(None) is False
    assert install_completion_arbiter(object()) is False


def test_actions_are_registered_as_remappable():
    """Tab is a DEFAULT, not a constant — `/keys` lists it and
    keybindings.json can move it."""
    from backend.core.ouroboros.battle_test.keymap import action_catalog
    install_completion_arbiter(_kb())
    actions = {spec.action for spec in action_catalog()}
    assert ACTION_SMART_COMPLETE in actions
    assert ACTION_ACCEPT_WORD in actions


def test_arbiter_mounts_on_every_typing_surface():
    """Surface parity.

    `ov.py` mounts it in `_client_extra_bindings` — the ONE action set both
    attach surfaces share, so binding at either call site would have fixed
    the surface someone looked at and left the other dead. `ov_demo.py`
    mounts its own, so the demo cannot silently diverge on the first
    interaction an operator tries.
    """
    ov = OV.read_text(encoding="utf-8")
    demo = OV_DEMO.read_text(encoding="utf-8")
    assert "install_completion_arbiter" in ov
    assert "install_completion_arbiter" in demo

    # In ov.py it must sit inside the SHARED builder, not at a call site.
    tree = ast.parse(ov)
    shared = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_client_extra_bindings"]
    assert shared, "_client_extra_bindings vanished"
    inner = {n.func.id for n in ast.walk(shared[0])
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "install_completion_arbiter" in inner


def test_enabled_by_default():
    assert arbiter_enabled() is True


# ---------------------------------------------------------------------------
# Live fire — a REAL PromptSession, a REAL AutoSuggest, a REAL Tab
# ---------------------------------------------------------------------------


def _drive(typed: str, history_lines=("what is O+V?",)):
    """Run a real PromptSession over a pipe and return what Enter submitted.

    Everything above verifies the DECISION. This verifies the wiring: real
    Buffer, real AutoSuggestFromHistory, real key-binding dispatch, real Tab
    keystroke. The composition layer is where this bug lived — the ladder
    was never wrong, it simply was not reachable from the key — so a test
    that stops at the pure function would have passed against the bug.
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.output import DummyOutput

    history = InMemoryHistory()
    for line in history_lines:
        history.append_string(line)

    kb = KeyBindings()
    install_completion_arbiter(kb)

    with create_pipe_input() as inp:
        inp.send_text(typed)
        session = PromptSession(
            input=inp, output=DummyOutput(), history=history,
            auto_suggest=AutoSuggestFromHistory(), key_bindings=kb,
        )
        return session.prompt()


def test_live_tab_accepts_the_ghost_text():
    """The screenshot, reproduced and fixed: type `wha`, press Tab, get the
    whole remembered line."""
    assert _drive("wha\t\r") == "what is O+V?"


def test_live_tab_without_a_suggestion_indents():
    assert _drive("zzz\t\r", history_lines=()) == "zzz  "


def test_live_alt_f_accepts_one_word():
    assert _drive("wha\x1bf\r") == "what"


def test_live_typing_is_unaffected():
    assert _drive("plain text\r") == "plain text"


def test_no_bare_letter_or_escape_is_hijacked():
    """`bind_action` treats each element of default_keys as an ALTERNATIVE.

    Passing ("escape", "f") therefore bound bare `escape` AND bare `f` —
    hijacking the letter f mid-word and the Escape key the overlay arbiter
    depends on. Chords must be ONE space-joined string. Caught by live fire,
    not by any decision-level test, which is why this pin exists.
    """
    kb = _kb()
    install_completion_arbiter(kb)
    for binding in kb.bindings:
        keys = tuple(str(getattr(k, "value", k)) for k in binding.keys)
        assert keys != ("f",), "bare `f` is bound — typing f would fire it"
        assert keys != ("escape",), "bare Escape is bound — overlays break"
    seqs = {tuple(str(getattr(k, "value", k)) for k in b.keys)
            for b in kb.bindings}
    assert ("escape", "f") in seqs, "the Alt-F chord did not bind"
    assert ("c-i",) in seqs, "Tab did not bind"
