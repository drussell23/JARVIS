"""The handoff auditor, and the ratchet that keeps the cockpit honest.

Two kinds of test live here and they are doing different jobs.

The RATCHET (:class:`TestNoHookIsDropped`) runs the real audit over the real
tree and demands that every hook a sink accepts is consumed, forwarded to a
consumer, or explicitly discarded. It is the regression that `search_rows`
never had: a defect that stayed invisible for as long as its own test asserted
a substring of the caller's source.

The ANALYSER tests are pure AST over inline snippets — no filesystem, no
imports of the code under measurement. They exist because an instrument that
is confidently wrong is worse than no instrument, and every one of them is a
scenario the first draft of this module got wrong or would have.
"""
from __future__ import annotations

import ast

import pytest

from backend.core.ouroboros.ui import capability_handoff as ch


def _fn(source: str, name: str):
    """Parse a snippet and hand back one function node."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    raise AssertionError(f"no function {name!r} in snippet")


class TestConsumptionClassification:
    """READ / FORWARDED_ONLY / UNREAD / DECLARED_DROP / OPAQUE."""

    def test_a_body_that_uses_the_value_reads_it(self):
        fn = _fn("def sink(*, hook=None):\n"
                 "    if hook is not None:\n"
                 "        render(hook)\n", "sink")
        assert ch._classify(fn, "hook", set())[0] is ch.Consumption.READ

    def test_a_body_that_never_mentions_it_is_the_search_rows_defect(self):
        fn = _fn("def sink(*, hook=None):\n"
                 "    return build()\n", "sink")
        assert ch._classify(fn, "hook", set())[0] is ch.Consumption.UNREAD

    def test_handing_it_straight_to_another_sink_is_a_forward_not_a_read(self):
        """The distinction that keeps a pass-through wrapper off the defect
        list — and keeps a dropped hook from hiding behind one."""
        fn = _fn("def wrapper(*, hook=None):\n"
                 "    return inner(hook=hook)\n", "wrapper")
        kind, forwards = ch._classify(fn, "hook", {"inner"})
        assert kind is ch.Consumption.FORWARDED_ONLY
        assert forwards == ("inner",)

    def test_forwarding_to_something_that_is_not_a_sink_is_a_read(self):
        """`build_dynamic_rows(status_rows)` is consumption. Treating every
        call as a forward would report the whole cockpit as dropped."""
        fn = _fn("def sink(*, hook=None):\n"
                 "    return build_dynamic_rows(hook)\n", "sink")
        assert ch._classify(fn, "hook", {"inner"})[0] is ch.Consumption.READ

    def test_one_real_use_beats_any_number_of_forwards(self):
        fn = _fn("def sink(*, hook=None):\n"
                 "    inner(hook=hook)\n"
                 "    if hook:\n"
                 "        log(hook)\n", "sink")
        assert ch._classify(fn, "hook", {"inner"})[0] is ch.Consumption.READ

    def test_del_is_a_declared_discard_not_a_silent_drop(self):
        """`presentation_restraint.render_minimal_welcome` accepts
        ``session_id`` for caller compatibility and deletes it. Python already
        has this sentence; a decorator would be a second vocabulary for it."""
        fn = _fn("def sink(*, session_id=''):\n"
                 "    del session_id\n"
                 "    return 1\n", "sink")
        assert (ch._classify(fn, "session_id", set())[0]
                is ch.Consumption.DECLARED_DROP)

    def test_rebinding_a_parameter_is_not_consuming_it(self):
        """A Store is not a Load. A body that overwrites what it was handed
        has used the NAME, never the caller's value."""
        fn = _fn("def sink(*, hook=None):\n"
                 "    hook = 3\n", "sink")
        assert ch._classify(fn, "hook", set())[0] is ch.Consumption.UNREAD

    def test_a_closure_reading_the_enclosing_scope_counts(self):
        """`_toolbar_fragments` reads `toolbar` from the enclosing frame. A
        walker that stopped at the top level would call it UNREAD and report
        the toolbar — visibly working — as a dropped hook."""
        fn = _fn("def sink(*, toolbar=None):\n"
                 "    def frags():\n"
                 "        return toolbar()\n"
                 "    return frags\n", "sink")
        assert ch._classify(fn, "toolbar", set())[0] is ch.Consumption.READ


class TestSignatureShapes:
    """What counts as a hook, across every parameter spelling."""

    def test_keyword_only_with_default_is_an_optional_hook(self):
        hooks, opaque = ch._hook_names(
            _fn("def sink(mux, *, a=None, b=None):\n    pass\n", "sink"))
        assert opaque is False
        assert [(n, pos, req) for n, pos, req in hooks] == [
            ("mux", True, True), ("a", False, False), ("b", False, False)]

    def test_defaults_bind_to_the_tail_of_the_positional_list(self):
        """Off-by-one here would mark required params optional and invert
        every downstream judgement about what a caller must supply."""
        hooks, _ = ch._hook_names(
            _fn("def sink(a, b, c=1, d=2):\n    pass\n", "sink"))
        assert hooks == [("a", True, True), ("b", True, True),
                         ("c", True, False), ("d", True, False)]

    def test_a_kwargs_sink_is_opaque_rather_than_complete(self):
        """A signature with ``**kwargs`` accepts names it does not enumerate.
        Claiming a complete hook list there would be the exact species of
        confident wrongness this module exists to end."""
        _hooks, opaque = ch._hook_names(
            _fn("def sink(*, a=None, **rest):\n    pass\n", "sink"))
        assert opaque is True

    def test_async_sinks_are_found(self):
        """`run_bipartite_repl` is an `async def`. A walker that only knew
        `FunctionDef` would silently skip most of a cockpit."""
        fn = _fn("async def sink(*, a=None):\n    return a\n", "sink")
        assert ch._classify(fn, "a", set())[0] is ch.Consumption.READ

    def test_self_and_cls_are_not_hooks(self):
        hooks, _ = ch._hook_names(
            _fn("def method(self, *, a=None):\n    pass\n", "method"))
        assert [n for n, _p, _r in hooks] == ["a"]

    @pytest.mark.parametrize("dyn", ["locals()", "vars()", "eval('x')"])
    def test_dynamic_scope_access_makes_the_whole_sink_opaque(self, dyn):
        """A body reaching its own frame dynamically could consume anything.
        OPAQUE is the honest verdict; guessing either way would be a lie."""
        fn = _fn(f"def sink(*, a=None):\n    return {dyn}\n", "sink")
        assert ch._reads_opaquely(fn) is True


class TestCalleeResolution:
    def test_both_call_spellings_resolve_to_the_same_handoff(self):
        """``build(...)`` and ``module.build(...)`` are one relationship. An
        analyser recognising only one would report the other as a drop."""
        bare = ast.parse("build(x)").body[0].value
        dotted = ast.parse("mod.build(x)").body[0].value
        assert ch._callee_name(bare) == ch._callee_name(dotted) == "build"

    def test_an_unresolvable_callee_is_empty_not_a_guess(self):
        call = ast.parse("funcs['build'](x)").body[0].value
        assert ch._callee_name(call) == ""


class TestWaivers:
    """UNSET is not WAIVED — the provenance rule, applied to call sites."""

    def test_a_waiver_carries_its_reason(self):
        value = ast.parse("waived('input is inert in a demo')"
                          ).body[0].value
        assert ch._waiver_reason(value) == "input is inert in a demo"

    def test_an_empty_reason_is_not_a_waiver(self):
        """The reason IS the waiver. Without it the call says only "I typed
        something here", which is what silence already said."""
        assert ch._waiver_reason(ast.parse("waived('')").body[0].value) is None
        assert ch._waiver_reason(ast.parse("waived('  ')").body[0].value) is None

    def test_an_ordinary_value_is_a_fill_not_a_waiver(self):
        value = ast.parse("lambda: rows").body[0].value
        assert ch._waiver_reason(value) is None

    def test_the_analyser_matches_the_functions_real_name(self):
        """Matched via ``waived.__name__`` so a rename cannot leave the
        analyser hunting a spelling that no longer exists."""
        assert ch.WAIVER_CALLABLE_NAME == ch.waived.__name__

    def test_waived_returns_none_so_adopting_it_changes_nothing(self):
        """The whole safety argument: at runtime it is identical to omitting
        the argument, so a callee's ``is not None`` guard cannot tell."""
        assert ch.waived("any reason at all") is None


class TestForwardChains:
    """A hop is not a drop, and a drop behind a hop is still a drop."""

    def _hook(self, sink, name, kind, forwards=()):
        return ch.Hook(name=name, sink=sink, positional=False, required=False,
                       consumption=kind, forwards_to=tuple(forwards))

    def test_a_forward_into_a_consumer_is_consumed(self):
        wrapper = self._hook("m.wrapper", "h",
                             ch.Consumption.FORWARDED_ONLY, ["builder"])
        builder = self._hook("m.builder", "h", ch.Consumption.READ)
        by_qual = {"m.wrapper.h": wrapper, "m.builder.h": builder}
        assert ch.effective_consumption(wrapper, by_qual) is ch.Consumption.READ

    def test_a_forward_into_a_dropper_reports_the_drop(self):
        """The `search_rows` chain exactly: ov → wrapper → builder → floor."""
        wrapper = self._hook("m.wrapper", "h",
                             ch.Consumption.FORWARDED_ONLY, ["builder"])
        builder = self._hook("m.builder", "h", ch.Consumption.UNREAD)
        by_qual = {"m.wrapper.h": wrapper, "m.builder.h": builder}
        assert (ch.effective_consumption(wrapper, by_qual)
                is ch.Consumption.UNREAD)

    def test_an_unresolvable_far_end_is_opaque_not_a_defect(self):
        """"I cannot see the far end" and "the far end drops it" are
        different findings, and only one of them is a bug."""
        wrapper = self._hook("m.wrapper", "h",
                             ch.Consumption.FORWARDED_ONLY, ["elsewhere"])
        assert (ch.effective_consumption(wrapper, {"m.wrapper.h": wrapper})
                is ch.Consumption.OPAQUE)

    def test_mutual_forwarding_terminates(self):
        """Two overloads forwarding to each other is legal. A naive walk
        would not return."""
        a = self._hook("m.a", "h", ch.Consumption.FORWARDED_ONLY, ["b"])
        b = self._hook("m.b", "h", ch.Consumption.FORWARDED_ONLY, ["a"])
        assert ch.effective_consumption(a, {"m.a.h": a, "m.b.h": b}) \
            is ch.Consumption.OPAQUE

    def test_any_consuming_branch_redeems_a_two_way_forward(self):
        w = self._hook("m.w", "h", ch.Consumption.FORWARDED_ONLY,
                       ["good", "bad"])
        good = self._hook("m.good", "h", ch.Consumption.READ)
        bad = self._hook("m.bad", "h", ch.Consumption.UNREAD)
        by_qual = {"m.w.h": w, "m.good.h": good, "m.bad.h": bad}
        assert ch.effective_consumption(w, by_qual) is ch.Consumption.READ


class TestFillStates:
    """What a call site says about each hook it was offered."""

    def _hooks(self, *names, positional=()):
        return [ch.Hook(name=n, sink="m.sink", positional=(n in positional),
                        required=False, consumption=ch.Consumption.READ)
                for n in names]

    def _call(self, source):
        return ast.parse(source).body[0].value

    def test_a_keyword_argument_is_a_fill(self):
        fills = ch._fills_for_call("surf", self._call("sink(a=1)"),
                                   self._hooks("a"))
        assert fills[0].state is ch.FillState.FILLED

    def test_an_omitted_hook_is_unset(self):
        fills = ch._fills_for_call("surf", self._call("sink()"),
                                   self._hooks("a"))
        assert fills[0].state is ch.FillState.UNSET

    def test_a_positional_argument_still_counts(self):
        """`ov demo live` passes its mux positionally. Keyword-only matching
        would report the cockpit's own multiplexer as unset."""
        fills = ch._fills_for_call("surf", self._call("sink(mux)"),
                                   self._hooks("mux", positional=("mux",)))
        assert fills[0].state is ch.FillState.FILLED

    def test_a_splatting_caller_is_opaque_never_a_failure(self):
        """``sink(**opts)`` may well fill it; no call site enumerates the
        name, so neither pass nor fail would be honest."""
        fills = ch._fills_for_call("surf", self._call("sink(**opts)"),
                                   self._hooks("a"))
        assert fills[0].state is ch.FillState.OPAQUE

    def test_a_waiver_is_accounted_for_and_keeps_its_reason(self):
        fills = ch._fills_for_call(
            "surf", self._call("sink(a=waived('nothing to complete against'))"),
            self._hooks("a"))
        assert fills[0].state is ch.FillState.WAIVED
        assert fills[0].reason == "nothing to complete against"


class TestDivergenceIsTheSignal:
    def test_only_disagreement_between_surfaces_is_reported(self):
        """A hook NOBODY fills is an unused option, not a demo gap. Reporting
        every unfilled optional produced 102 rows of which four mattered."""
        reading = ch.HandoffReading(fills=[
            ch.Fill("client", "m.sink", "used", ch.FillState.FILLED),
            ch.Fill("demo", "m.sink", "used", ch.FillState.UNSET),
            ch.Fill("client", "m.sink", "ignored", ch.FillState.UNSET),
            ch.Fill("demo", "m.sink", "ignored", ch.FillState.UNSET),
        ])
        assert [(h, f, u) for _s, h, f, u in reading.divergence()] == [
            ("used", ("client",), ("demo",))]

    def test_a_declared_waiver_is_not_a_divergence(self):
        """The point of waiving: a surface that has decided is no longer a
        finding, so the list shrinks to things nobody has thought about."""
        reading = ch.HandoffReading(fills=[
            ch.Fill("client", "m.sink", "h", ch.FillState.FILLED),
            ch.Fill("demo", "m.sink", "h", ch.FillState.WAIVED,
                    reason="input is inert here"),
        ])
        assert reading.divergence() == []

    def test_coverage_counts_waivers_as_accounted_for(self):
        reading = ch.HandoffReading(fills=[
            ch.Fill("demo", "m.sink", "a", ch.FillState.FILLED),
            ch.Fill("demo", "m.sink", "b", ch.FillState.WAIVED, reason="why"),
            ch.Fill("demo", "m.sink", "c", ch.FillState.UNSET),
        ])
        assert reading.coverage("demo") == (2, 3)


class TestNoHookIsDropped:
    """THE RATCHET. The regression `search_rows` never had.

    Runs the real analyser over the real tree. A new hook that is accepted and
    never read fails here, at authoring time, instead of shipping dark and
    being found by an operator months later asking why a feature they paid for
    is invisible.
    """

    def test_every_hook_a_sink_accepts_is_accounted_for(self):
        reading = ch.audit()
        assert reading.sinks, "no sinks discovered — the audit found nothing"
        dropped = reading.dropped()
        assert not dropped, (
            "these hooks are accepted by a sink and consumed by nothing it "
            "forwards to, so the capability is dark for EVERY caller:\n"
            + "\n".join(f"  {h.sink}({h.name})" for h in dropped)
            + "\n\nEither consume the value, forward it to something that "
              "does, or `del` the parameter to declare the discard."
        )

    def test_the_cockpit_builder_is_among_the_discovered_sinks(self):
        """Guards the ratchet itself. Discovery is by SHAPE, so a refactor
        that drops the hook count below the threshold would empty the audit
        and turn the test above green by measuring nothing."""
        quals = {s.qualname for s in ch.audit().sinks}
        assert any(q.endswith("bipartite_layout.build_bipartite_application")
                   for q in quals), (
            f"the cockpit builder is no longer being audited: {sorted(quals)}")

    def test_a_disabled_audit_reports_nothing_rather_than_passing(self, monkeypatch):
        """Master-off must yield an EMPTY reading, so a suite that silently
        ran with the flag unset cannot look like a clean bill of health."""
        monkeypatch.setenv(ch.MASTER_FLAG_ENV_VAR, "0")
        reading = ch.audit()
        assert reading.sinks == [] and reading.hooks == []

    def test_render_never_raises_on_an_empty_reading(self):
        assert ch.render(ch.HandoffReading())
