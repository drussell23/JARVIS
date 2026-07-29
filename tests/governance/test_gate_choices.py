"""One permission grammar, composed rather than written down.

The controller accepts four verbs — /allow, /always, /deny <reason>, /pause.
What the operator SAW was a hand-written string, and there were three of
them. They had already drifted: the phase-boundary hint listed three verbs
and omitted /always, while the per-tool-call renderer listed four. So at one
of O+V's two live permission surfaces the operator was never told that
"allow and remember" exists — a capability built, tested, persisted, live,
and unmentioned.

The tests that matter are the ones where being wrong looks like being right:
a number that means something different from what was rendered, and a label
that promises a wider grant than the store actually writes.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.gate_choices import (
    GateChoice,
    GateChoiceSet,
    compose_gate_choices,
    describe_grant_scope,
    remembered_grant_ttl_days,
    render_choices,
    render_verbs,
    resolve_answer,
)
from backend.core.ouroboros.governance.inline_approval import ReasonProvenance


def _full():
    return compose_gate_choices(
        question="Do you want to allow edit_file?",
        grant_scope=describe_grant_scope(
            tool="edit_file", target_path="backend/gate.py"),
    )


def _reduced():
    """A phase-boundary prompt: no tool, so no describable grant."""
    return compose_gate_choices(grant_scope=describe_grant_scope())


class TestTheNumberMeansWhatWasRendered:
    def test_the_SAME_number_differs_between_prompts(self):
        """THE safety property. A global {"2": ALWAYS} map would be wrong
        in the dangerous direction — turning a keystroke into a persisted
        grant on a prompt that never offered one."""
        assert resolve_answer("2", _full()).verb == "/always"
        assert resolve_answer("2", _reduced()).verb == "/deny"

    def test_out_of_range_chooses_NOTHING(self):
        """Not the nearest, not the first. An operator who typed 9 at a
        4-choice prompt has not chosen."""
        answer = resolve_answer("9", _full())
        assert answer.choice is None
        assert answer.verb == ""

    def test_every_ordinal_maps_to_its_rendered_row(self):
        cs = _full()
        for i, choice in enumerate(cs.choices, start=1):
            assert resolve_answer(str(i), cs).choice is choice

    def test_empty_chooses_nothing_unless_the_prompt_promised(self):
        """The `[Y/n]` lesson: the default belongs to whoever promised it."""
        assert resolve_answer("", _full()).choice is None
        first = _full().choices[0]
        assert resolve_answer("", _full(), empty_means=first).choice is first


class TestTheVerbsStillWork:
    @pytest.mark.parametrize(("typed", "verb"), [
        ("/allow", "/allow"), ("allow", "/allow"), ("y", "/allow"),
        ("/always", "/always"), ("always", "/always"),
        ("/deny", "/deny"), ("n", "/deny"), ("no", "/deny"),
        ("/pause", "/pause"), ("w", "/pause"), ("defer", "/pause"),
    ])
    def test_the_number_is_an_addition_not_a_replacement(self, typed, verb):
        """An operator who has learned /always must never have to count
        rows, and every script and keybinding keeps working."""
        assert resolve_answer(typed, _full()).verb == verb

    def test_garbage_attaches_its_words_to_nothing(self):
        answer = resolve_answer("wat this is not a verb", _full())
        assert answer.choice is None and answer.reason == ""


class TestTheReasonSurvives:
    def test_a_numbered_rejection_carries_its_reason(self):
        answer = resolve_answer("3 it widens the permission gate", _full())
        assert answer.verb == "/deny"
        assert answer.reason == "it widens the permission gate"
        assert answer.provenance is ReasonProvenance.STATED

    def test_a_bare_choice_states_nothing(self):
        assert resolve_answer("3", _full()).is_stated is False

    def test_only_rejection_INVITES_one(self):
        """Prompting for a justification on approval trains operators to
        type nothing, which is how a reason capture dies."""
        wants = [c.verb for c in _full().choices if c.wants_reason]
        assert wants == ["/deny"]

    def test_a_pasted_second_line_is_not_a_reason(self):
        assert resolve_answer("3\nrm -rf /", _full()).reason == ""


class TestTheLabelIsTrue:
    def test_it_names_the_ACTUAL_grant_not_forever(self):
        """/always is called always and is not: tool + exact path + repo,
        expiring on the store's TTL. A label reading "always allow" would
        overstate what the operator is agreeing to."""
        scope = describe_grant_scope(
            tool="edit_file", target_path="backend/gate.py")
        assert scope is not None
        assert "exact path" in scope
        assert "this repo" in scope
        assert "d" in scope.split("·")[-1]          # a window is stated

    def test_bash_grants_name_a_COMMAND_not_a_path(self):
        scope = describe_grant_scope(tool="bash", arg_preview="pytest -q")
        assert "exact command" in scope

    def test_the_ttl_is_ASKED_not_restated(self, monkeypatch):
        """A duplicated default drifts the first time the real one is
        tuned, and the label would promise a window the grant lacks."""
        monkeypatch.setenv("JARVIS_REMEMBERED_ALLOW_TTL_DAYS", "3")
        assert remembered_grant_ttl_days() == 3.0
        assert "3d" in describe_grant_scope(
            tool="edit_file", target_path="a.py")

    def test_an_UNNAMEABLE_scope_removes_the_option(self):
        """A phase-boundary projection carries no tool. Offering to "not
        ask again" about a boundary whose edges cannot be shown is worse
        than one fewer option — the same rule that reports an unmeasurable
        blast radius as unknown rather than capping it."""
        assert describe_grant_scope(target_path="backend/gate.py") is None
        assert "/always" not in [c.verb for c in _reduced().choices]

    def test_the_qualifiers_survive_a_narrow_terminal(self):
        """Truncation eats the tail — which is exactly where "in this repo
        · 30d" lives, leaving a label that reads BROADER than the grant."""
        for width in (120, 80, 60, 40):
            body = " ".join(render_choices(_full(), width=width))
            assert "in this repo" in body, width
            assert "30d" in body, width


class TestTheHintHasOneSource:
    def test_the_verb_line_is_derived_from_the_choices(self):
        assert "/always" in render_verbs(_full())
        assert "/always" not in render_verbs(_reduced())

    def test_the_shipped_hint_no_longer_hides_always(self):
        """The regression: this constant was hand-written and had lost
        /always while its sibling kept it."""
        from backend.core.ouroboros.governance import (
            inline_prompt_gate_renderer as r,
        )
        for verb in ("/allow", "/deny", "/pause"):
            assert verb in r.PROMPT_ACTIONS_HINT

    def test_neither_live_renderer_writes_the_vocabulary_by_hand(self):
        """Two hands writing one vocabulary IS the defect."""
        import inspect
        from backend.core.ouroboros.governance import inline_permission_repl
        src = inspect.getsource(
            inline_permission_repl.ConsoleInlineRenderer.format_block)
        assert "/allow   /deny" not in src


class TestItReachesTheLiveDispatcher:
    """A grammar nothing dispatches is decoration."""

    @staticmethod
    def _controller(tool="edit_file", path="backend/gate.py"):
        from backend.core.ouroboros.governance.inline_permission import (
            InlineDecision, InlineGateVerdict,
        )
        from backend.core.ouroboros.governance.inline_permission_prompt import (
            InlinePromptController, InlinePromptRequest,
        )
        c = InlinePromptController()
        c.request(InlinePromptRequest(
            prompt_id="p-1", op_id="op-1", call_id="c-1", tool=tool,
            arg_fingerprint="f", arg_preview="patch it", target_path=path,
            verdict=InlineGateVerdict(
                decision=InlineDecision.ASK, rule_id="ask.x",
                reason="outside scope", ruleset_version="1"),
            timeout_s=30.0))
        return c

    def _dispatch(self, line, controller):
        from backend.core.ouroboros.governance.inline_permission_repl import (
            dispatch_inline_command,
        )
        return dispatch_inline_command(line, controller=controller,
                                       reviewer="derek")

    @pytest.mark.parametrize(("typed", "expect"), [
        ("1", "allow-once"), ("2", "allow-always"),
        ("3 it widens the gate", "denied"), ("4", "paused"),
    ])
    def test_a_number_resolves_a_real_prompt(self, typed, expect):
        res = self._dispatch(typed, self._controller())
        assert res.ok and expect in res.text

    def test_the_number_follows_THAT_prompts_options(self):
        res = self._dispatch("2", self._controller(tool="", path=""))
        assert "denied" in res.text, "2 must not mean always here"

    def test_out_of_range_leaves_the_prompt_pending(self):
        c = self._controller()
        res = self._dispatch("9", c)
        assert not res.ok
        assert c.pending_ids() == ["p-1"], "a fat finger decided a prompt"

    def test_a_number_with_nothing_pending_falls_THROUGH(self):
        from backend.core.ouroboros.governance.inline_permission_prompt import (
            InlinePromptController,
        )
        res = self._dispatch("2", InlinePromptController())
        assert res.matched is False, "ordinary REPL input was swallowed"

    def test_the_number_delegates_rather_than_reimplements(self):
        import inspect
        from backend.core.ouroboros.governance import inline_permission_repl
        src = inspect.getsource(inline_permission_repl._handle_ordinal)
        for handler in ("_handle_allow", "_handle_deny", "_handle_pause"):
            assert handler in src


class TestNeverRaises:
    @pytest.mark.parametrize("call", [
        lambda: resolve_answer(None, GateChoiceSet()),
        lambda: resolve_answer("1", GateChoiceSet()),
        lambda: render_choices(GateChoiceSet()),
        lambda: render_verbs(GateChoiceSet()),
        lambda: compose_gate_choices(extra=[None]),    # type: ignore[list-item]
    ])
    def test_junk_degrades(self, call):
        assert call() is not None

    def test_an_unnameable_scope_is_None_not_a_guess(self):
        """None is the CONTRACT here, not a degradation: a scope that
        cannot be described must not be described. Every other junk input
        returns an empty-but-usable value; this one returns nothing on
        purpose, and the caller drops the option."""
        for junk in (object(), None, "", 42):
            assert describe_grant_scope(tool=junk) is None  # type: ignore

    def test_a_degraded_resolver_never_allows(self, monkeypatch):
        """Fail CLOSED. A permission prompt decided by a broken parser is
        the one outcome worse than an unanswered one."""
        from backend.core.ouroboros.governance import inline_permission_repl

        def _boom(*a, **k):
            raise RuntimeError("resolver down")

        monkeypatch.setattr(
            "backend.core.ouroboros.governance.gate_choices.resolve_answer",
            _boom)
        c = TestItReachesTheLiveDispatcher._controller()
        res = inline_permission_repl.dispatch_inline_command(
            "1", controller=c, reviewer="derek")
        assert not res.ok
        assert c.pending_ids() == ["p-1"]

    def test_the_master_flag_falls_back_to_verbs(self, monkeypatch):
        monkeypatch.setenv("JARVIS_NUMBERED_GATE_CHOICES_ENABLED", "0")
        assert render_choices(_full()) == []
