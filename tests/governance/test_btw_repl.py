"""`/btw` verb surface — grammar, discovery, and the two ways in.

Two classes of defect live here and neither is caught by
:mod:`tests.governance.test_side_channel`:

**The grammar.** ``/btw cancel the doc storm and tell me why`` is a
question about cancelling. A prefix match would have silently tried to
withdraw a ticket named "the" and the operator's question would be gone.
The rule is that a subcommand is a WORD and a question is a SENTENCE —
the same shape-strictness ``operator_prompt_bridge.is_bare_verdict``
arrived at after the loose version approved an op because a goal
started with "go".

**The surface split.** The daemon's own terminal and an attached ``ov``
cockpit are two different verb ladders. ``/btw`` must be reachable from
both, and it is reachable from both only because they share the
auto-discovery registry — which is a property of wiring, not of this
module, so it is asserted here rather than assumed.
"""
from __future__ import annotations

import inspect
from typing import List

import pytest

from backend.core.ouroboros.governance import btw_repl
from backend.core.ouroboros.governance import side_channel as sc


@pytest.fixture(autouse=True)
def _clean():
    sc.reset_default_side_channel_for_tests()
    yield
    sc.reset_default_side_channel_for_tests()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "/btw", "btw", "/btw why?", "btw why?", "  /btw why?  ",
])
def test_matches_its_own_verb(line):
    assert btw_repl.dispatch_btw_command(line).matched


@pytest.mark.parametrize("line", [
    "/status", "/btwx", "btwx why", "", None, 42, "why /btw",
])
def test_declines_everything_else(line):
    result = btw_repl.dispatch_btw_command(line)
    assert not result.matched
    assert result.text == ""


# ---------------------------------------------------------------------------
# The grammar — a subcommand is a word, a question is a sentence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rest,expected", [
    ("help", ("help", "")),
    ("status", ("status", "")),
    ("stats", ("stats", "")),
    ("s-3", ("show", "s-3")),
    ("S-3", ("show", "s-3")),
    ("cancel s-3", ("cancel", "s-3")),
    ("withdraw s-12", ("cancel", "s-12")),
    ("show s-1", ("show", "s-1")),
])
def test_control_forms_are_recognised(rest, expected):
    assert btw_repl._control_form(rest) == expected


@pytest.mark.parametrize("rest", [
    "cancel the doc storm and tell me why",
    "status of the current op?",
    "show me what you are doing",
    "s-1 and also why",
    "help me understand the routing",
    "why did that route to DoubleWord?",
    "cancel s-3?",              # punctuation makes it prose, not a ref
    "cancel that",
])
def test_sentences_are_questions_not_subcommands(rest):
    """The safe direction. Mistaking a subcommand for a question costs
    one cheap call; mistaking a question for a subcommand eats it."""
    assert btw_repl._control_form(rest) is None


def test_a_sentence_starting_with_cancel_is_actually_asked():
    result = btw_repl.dispatch_btw_command(
        "/btw cancel the doc storm and tell me why",
    )
    assert result.ok
    ticket = sc.get_default_side_channel().lookup("s-1")
    assert ticket is not None
    assert ticket.text == "cancel the doc storm and tell me why"


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def test_help_answers_without_the_substrate(monkeypatch):
    """`/btw help` must work when everything else is broken — it is how
    an operator finds out what the verb even is."""
    import builtins
    real_import = builtins.__import__

    def _no_side_channel(name, globals=None, locals=None, fromlist=(),
                         level=0):
        # `from pkg import side_channel` calls __import__ with the
        # PACKAGE as `name` and the module in `fromlist` — matching on
        # `name` alone would never fire, and the test would pass by
        # accident.
        if "side_channel" in name or "side_channel" in (fromlist or ()):
            raise ImportError("substrate down")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _no_side_channel)
    result = btw_repl.dispatch_btw_command("/btw help")
    assert result.ok
    assert "/btw <question>" in result.text


def test_a_broken_substrate_is_reported_not_raised(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _no_side_channel(name, globals=None, locals=None, fromlist=(),
                         level=0):
        # `from pkg import side_channel` calls __import__ with the
        # PACKAGE as `name` and the module in `fromlist` — matching on
        # `name` alone would never fire, and the test would pass by
        # accident.
        if "side_channel" in name or "side_channel" in (fromlist or ()):
            raise ImportError("substrate down")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _no_side_channel)
    result = btw_repl.dispatch_btw_command("/btw why?")
    assert result.matched and not result.ok
    assert "unavailable" in result.text


def test_submitting_returns_a_ticket_ref():
    result = btw_repl.dispatch_btw_command("/btw why is that slow?")
    assert result.ok
    assert "s-1" in result.text
    assert "without interrupting" in result.text


def test_a_repeat_is_folded_not_charged_twice():
    btw_repl.dispatch_btw_command("/btw why is that slow?")
    second = btw_repl.dispatch_btw_command("/btw why is that slow?")
    assert second.ok
    assert "already asked" in second.text
    assert len(sc.get_default_side_channel().all_refs()) == 1


def test_refusal_names_what_to_do(monkeypatch):
    monkeypatch.setenv(sc.ENV_QUEUE_DEPTH, "1")
    sc.reset_default_side_channel_for_tests()
    assert btw_repl.dispatch_btw_command("/btw one").ok
    refused = btw_repl.dispatch_btw_command("/btw two")
    assert not refused.ok
    assert "queue full" in refused.text


def test_empty_verb_lists_the_ledger():
    empty = btw_repl.dispatch_btw_command("/btw")
    assert empty.ok
    assert "no side questions yet" in empty.text
    btw_repl.dispatch_btw_command("/btw first question")
    listed = btw_repl.dispatch_btw_command("/btw")
    assert "s-1" in listed.text
    assert "first question" in listed.text
    assert "1 waiting" in listed.text


def test_status_reports_the_lane_and_the_paid_gate():
    result = btw_repl.dispatch_btw_command("/btw status")
    assert result.ok
    for token in ("lane", "waiting", "admission", "grounding readers",
                  "answering substrate"):
        assert token in result.text


def test_status_never_reports_forced_on_an_idle_lane(monkeypatch):
    """The probe-ticket bug: a synthetic ticket built with the dataclass
    default reads as having waited since the epoch, which is past every
    ceiling — so an idle lane would report FORCED."""
    monkeypatch.setattr(sc, "_inflight_op_count", lambda: 99)
    monkeypatch.setenv(sc.ENV_OPS_HEADROOM, "1")
    monkeypatch.setenv(sc.ENV_DEFER_MAX_S, "600")
    assert sc.assess_admission_now().state is sc.AdmissionState.DEFER


def test_cancel_and_show_round_trip():
    btw_repl.dispatch_btw_command("/btw a question")
    shown = btw_repl.dispatch_btw_command("/btw s-1")
    assert "a question" in shown.text
    cancelled = btw_repl.dispatch_btw_command("/btw cancel s-1")
    assert cancelled.ok and "withdrew" in cancelled.text
    again = btw_repl.dispatch_btw_command("/btw cancel s-1")
    assert not again.ok
    assert "not a waiting side question" in again.text


def test_show_of_an_unknown_ref_is_a_friendly_line():
    result = btw_repl.dispatch_btw_command("/btw s-99")
    assert result.ok
    assert "no such side question" in result.text


def test_the_session_is_captured_at_dispatch():
    """The ContextVar is per-task; the worker that answers later is a
    different task and would read None."""
    from backend.core.ouroboros.battle_test.attach_session import (
        session_scope,
    )
    with session_scope("cockpit-z"):
        btw_repl.dispatch_btw_command("/btw who is asking?")
    assert sc.get_default_side_channel().lookup("s-1").session == "cockpit-z"


def test_dispatcher_is_sync():
    """It runs on the operator input queue's single consumer. A
    coroutine that awaited a provider here would rebuild the exact
    blocking path the lane exists to remove."""
    assert not inspect.iscoroutinefunction(btw_repl.dispatch_btw_command)


# ---------------------------------------------------------------------------
# Reachability — both surfaces, via the shared registry
# ---------------------------------------------------------------------------


async def test_the_registry_discovers_the_verb():
    from backend.core.ouroboros.battle_test import repl_dispatch_registry as reg
    reg.reset_registry_for_tests()
    reg.prime_registry(force=True)
    assert "btw" in reg.list_dispatchable_verbs()
    outcome = await reg.try_dispatch("/btw help")
    assert outcome.matched and outcome.ok
    assert "/btw <question>" in outcome.text
    reg.reset_registry_for_tests()


def test_the_harness_ladder_falls_through_to_the_registry():
    """THE surface split. Without this branch a verb the daemon's own
    terminal answers is logged as 'Unknown REPL command' when typed into
    the cockpit the operator is actually watching — the same invisible
    class as the 59 verbs that rendered to a terminal nobody read."""
    import ast
    import pathlib
    source = pathlib.Path(
        "backend/core/ouroboros/battle_test/harness.py",
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef)
         and n.name == "_try_registry_verb"),
        None,
    )
    assert method is not None, "_try_registry_verb was removed"
    body = ast.dump(method)
    assert "try_dispatch" in body
    # And it must actually be WIRED into the command ladder, not merely
    # present — a method with no caller is the inert shape this repo has
    # paid for repeatedly.
    handler = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_handle_repl_command"
    )
    assert "_try_registry_verb" in ast.dump(handler)


def test_expand_routes_the_s_prefix():
    """An operator handed an `s-N` reaches for `/expand`. A ref family
    with one member that does not answer there has a hole in it."""
    import ast
    import pathlib
    source = pathlib.Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py",
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_handle_expand"
    )
    dumped = ast.dump(handler)
    assert "'s-'" in dumped or '"s-"' in dumped
    assert "_expand_side_question" in dumped
    # And the renderer delegates rather than re-implementing, so the two
    # surfaces cannot drift into two accounts of one ticket.
    renderer = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "_expand_side_question"
    )
    assert "render_ref" in ast.dump(renderer)


def test_serpent_flow_publishes_its_console_as_the_answer_sink():
    """A deferred answer has no console of its own. The producer must
    publish itself, and it must resolve `console` at CALL time — the
    harness swaps that attribute for the spooled mirror after boot."""
    import ast
    import pathlib
    source = pathlib.Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py",
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls: List[ast.Call] = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "set_answer_sink"
    ]
    assert calls, "set_answer_sink is never called"
    # A lambda, not a bound reference: `set_answer_sink(self.console.print)`
    # would capture the pre-swap console and render only to the daemon.
    assert any(isinstance(c.args[0], ast.Lambda) for c in calls if c.args)


def test_no_parallel_answering_substrate():
    """The lane SCHEDULES; `fast_path_qa` ANSWERS. A second provider
    client here would be a second opinion about cost, budget, and what
    the answerer is."""
    import ast
    import pathlib
    source = pathlib.Path(sc.__file__).read_text(encoding="utf-8")
    modules: List[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(a.name for a in node.names)
    assert not any("anthropic" in m for m in modules)
    assert not any("providers" in m for m in modules)
    assert any("fast_path_qa" in m for m in modules)

def test_the_verb_is_declared_to_the_help_registry():
    """Dispatching correctly and being findable nowhere is the same
    shape as the sixteen verbs `/help` lost when a hand-written list
    said what existed and the code said something else."""
    from backend.core.ouroboros.governance.help_dispatcher import (
        VerbRegistry, dispatch_help_command, reset_default_verb_registry,
    )
    registry = VerbRegistry()
    assert btw_repl.register_verbs(registry) == 1
    spec = registry.get("/btw")
    assert spec is not None
    assert "without interrupting" in spec.one_line
    # `/help btw` and `/btw help` must be ONE explanation, not two.
    assert spec.resolve_help() == btw_repl._HELP

    reset_default_verb_registry()
    index = getattr(dispatch_help_command("/help verbs"), "text", "")
    assert "/btw" in index
    detail = getattr(dispatch_help_command("/help btw"), "text", "")
    assert "/btw <question>" in detail
    reset_default_verb_registry()
