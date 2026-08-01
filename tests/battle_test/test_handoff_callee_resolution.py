"""A name is not an identity — the auditor's sharpest blind spot.

`capability_handoff` matched a call to a sink by the BARE last segment of the
sink's qualname. There are SIX distinct ``compose`` definitions under the
audited roots, so every caller of any of them was judged against all of them.

Concretely: `serpent_flow` calls ``narrative_renderer.compose``, and the audit
reported it as failing to fill seven parameters of
``tool_render_view.compose`` — a different function that happens to share a
name. **Seven of eleven reported divergences came from that one collision**,
and the four real findings underneath (the daemon cockpit genuinely dropping
capabilities the attach client passes) were sitting in the same list. An
auditor wrong most of the time is an auditor nobody reads.

It is the same defect this codebase keeps rediscovering. The caller index that
read ``repair_engine`` 40% severed did so because it keyed on bare symbol
names and could not see ``self._config.repair_engine.run(...)``. Unqualified
names are not identities.

What matters in these tests is that the fix RESOLVES rather than SUPPRESSES:
dropping every ambiguous match would also take 11 findings to 4, and would be
indistinguishable from this by count alone.
"""
from __future__ import annotations

import ast

import pytest

from backend.core.ouroboros.ui import capability_handoff as ch


def _resolve(source: str, module: str = "pkg.mod", call_index: int = 0):
    """Resolve the Nth call in *source* exactly as the auditor would."""
    tree = ast.parse(source)
    is_init = module.endswith(".__init__")
    scope = ch._bindings_from(ch._direct_children(tree), module, is_init)
    local_defs = frozenset(
        n.name for n in ast.iter_child_nodes(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    return ch._resolve_callee(calls[call_index], (scope,), local_defs, module)


class TestTheCollisionItself:
    def test_two_composes_are_two_sinks(self):
        """THE regression, in miniature."""
        a = _resolve("from x.tool_render_view import compose\ncompose(1)")
        b = _resolve("from x.narrative_renderer import compose\ncompose(1)")
        assert a == "x.tool_render_view.compose"
        assert b == "x.narrative_renderer.compose"
        assert a != b

    def test_the_live_audit_no_longer_reports_the_false_seven(self):
        """Against the real tree, not a fixture."""
        divergences = ch.audit().divergence()
        composes = [d for d in divergences if d[0].endswith(".compose")]
        assert not composes, (
            f"compose collisions are back: {composes}")

    def test_the_real_findings_ARE_STILL_VISIBLE(self):
        """SUPERSEDED 2026-08-01: was ``<= divergent params``.

        When this was written the four `run_bipartite_repl` hooks were open
        divergences, and asserting their survival was how the resolver was
        held to resolving rather than suppressing — a blanket drop of
        ambiguous matches would also have taken 11 to 4.

        They are no longer divergences, and not because anything was
        suppressed: investigation showed the daemon declines three of them for
        three different real reasons, which are now DECLARED via
        ``waived(...)``, and that `title` was never a finding at all — `ov`
        passes the signature's own default, so omitting it is the identical
        call.

        The original purpose is kept and sharpened. Vanishing and being
        declined are different outcomes, and only one of them is acceptable:
        each hook must still be VISIBLE, carrying a reason a reader can check.
        """
        fills = ch.audit().fills
        waived = {
            f.hook: f.reason for f in fills
            if f.sink.endswith("run_bipartite_repl")
            and f.state is ch.FillState.WAIVED
        }
        assert {"on_mux", "seed", "watch_alive"} <= set(waived), (
            f"a declined hook went silent instead of declared: {waived}")
        for hook, reason in waived.items():
            assert reason and len(reason) > 20, (
                f"{hook} is waived with no usable reason — a waiver without "
                f"one is a shrug, which is the state it exists to replace")

    def test_title_was_never_a_finding(self):
        """`ov` passes ``title="◇ O+V · proactive canvas"``, which is
        character-for-character the sink's default. The daemon omits it and
        gets the same title. Reporting that as a dropped capability named a
        real surface and a real parameter and was still wrong."""
        fills = ch.audit().fills
        titles = [f for f in fills
                  if f.sink.endswith("run_bipartite_repl") and f.hook == "title"]
        assert titles, "the hook stopped being analysed entirely"
        assert any(f.state is ch.FillState.DEFAULTED for f in titles)
        assert not any(f.state is ch.FillState.FILLED for f in titles)

    def test_the_auditor_can_still_find_a_REAL_divergence(self):
        """Zero findings must mean "nothing diverges", never "the instrument
        stopped working" — the failure this fix already shipped once, when a
        recursion bug took the count to zero and read as a clean bill.

        A synthetic sink with one filling caller and one omitting caller must
        still be reported."""
        reading = ch.HandoffReading(
            sinks=(),
            fills=(
                ch.Fill("surface_a", "m.sink", "hook", ch.FillState.FILLED),
                ch.Fill("surface_b", "m.sink", "hook", ch.FillState.UNSET),
            ),
        )
        found = reading.divergence()
        assert len(found) == 1
        assert found[0][1] == "hook"
        assert found[0][2] == ("surface_a",) and found[0][3] == ("surface_b",)

    def test_a_defaulted_fill_does_not_manufacture_a_divergence(self):
        """The other half: DEFAULTED must not read as FILLED."""
        reading = ch.HandoffReading(
            sinks=(),
            fills=(
                ch.Fill("surface_a", "m.sink", "hook", ch.FillState.DEFAULTED),
                ch.Fill("surface_b", "m.sink", "hook", ch.FillState.UNSET),
            ),
        )
        assert reading.divergence() == []

    def test_the_collided_sink_is_still_ANALYSED(self):
        """The strongest check that this resolved rather than dropped: fills
        for `tool_render_view.compose` are still recorded, they are simply
        attributed to the caller that really makes them."""
        reading = ch.audit()
        fills = reading.fills() if callable(getattr(reading, "fills", None)) \
            else getattr(reading, "fills", [])
        composed = [f for f in fills if f.sink.endswith("tool_render_view.compose")]
        assert composed, "the sink stopped being analysed — that is suppression"


class TestImportForms:
    @pytest.mark.parametrize("source,expected", [
        ("from a.b import fn\nfn()", "a.b.fn"),
        ("from a.b import fn as g\ng()", "a.b.fn"),
        ("import a.b\na.b.fn()", None),          # `a` binds, `a.b.fn` is 2 hops
        ("import a\na.fn()", "a.fn"),
        ("import a.b as c\nc.fn()", "a.b.fn"),
    ])
    def test_each_spelling(self, source, expected):
        assert _resolve(source) == expected

    def test_a_local_def_binds_to_this_module(self):
        assert _resolve("def fn():\n    pass\nfn()") == "pkg.mod.fn"

    def test_an_import_shadows_a_same_named_local_def(self):
        """Python binds the LAST assignment at module scope; an explicit
        import of the name is the caller's stated intent."""
        assert _resolve("def fn():\n    pass\nfrom a.b import fn\nfn()") == \
            "a.b.fn"


class TestRelativeImports:
    def test_one_dot_resolves_against_the_package(self):
        assert _resolve("from .sibling import fn\nfn()",
                        module="pkg.sub.mod") == "pkg.sub.sibling.fn"

    def test_two_dots_climb(self):
        assert _resolve("from ..other import fn\nfn()",
                        module="pkg.sub.mod") == "pkg.other.fn"

    def test_a_bare_relative_import(self):
        assert _resolve("from . import fn\nfn()",
                        module="pkg.sub.mod") == "pkg.sub.fn"

    def test_escaping_the_tree_is_unresolvable_not_a_guess(self):
        assert _resolve("from ....way.up import fn\nfn()",
                        module="pkg.mod") is None


class TestWhatMustNOTResolve:
    """Every one of these MATCHED before, on the last path segment alone."""

    def test_a_method_call_is_not_a_module_sink(self):
        """`self.compose(...)` matched every module-level `compose` in the
        tree. This is the single largest source of the false positives."""
        assert _resolve("self.fn()") is None
        assert _resolve("cls.fn()") is None

    def test_a_call_through_an_unbound_name(self):
        assert _resolve("fn()") is None

    def test_a_call_on_an_instance_attribute(self):
        assert _resolve("obj.helper.fn()") is None

    def test_a_call_on_an_expression(self):
        assert _resolve("get_thing().fn()") is None

    def test_an_ambiguous_binding_refuses_to_guess(self):
        """Two imports of one name in one scope. Picking either would
        reintroduce a confident wrong answer, which is the whole defect."""
        source = ("from a.b import fn\n"
                  "from c.d import fn\n"
                  "fn()")
        assert _resolve(source) is None

    def test_a_star_import_binds_nothing(self):
        """`from x import *` cannot be resolved without importing x, and the
        analyser never executes the code it audits."""
        assert _resolve("from a.b import *\nfn()") is None


class TestScoping:
    def test_a_function_local_import_shadows_the_module_level_one(self):
        """This codebase imports inside functions constantly — the pattern
        that made the collision so damaging. A call means whichever binding
        encloses it."""
        source = (
            "from a.tool_render_view import compose\n"
            "def f():\n"
            "    from a.narrative_renderer import compose\n"
            "    return compose(1)\n"
        )
        tree = ast.parse(source)
        module_scope = ch._bindings_from(
            ch._direct_children(tree), "pkg.mod", False)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef))
        inner = ch._bindings_from(ch._direct_children(fn), "pkg.mod", False)
        call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call))
        resolved = ch._resolve_callee(
            call, (inner, module_scope), frozenset(), "pkg.mod")
        assert resolved == "a.narrative_renderer.compose"

    def test_nested_scopes_are_YIELDED_so_recursion_can_reach_them(self):
        """The bug the first draft of this fix shipped.

        `_direct_children` must hand back a nested def WITHOUT descending into
        it: the caller needs to see it in order to recurse with its own
        bindings. Dropping it meant no nested function was ever visited, so
        the audit scanned module level only — and went from 11 findings to
        ZERO, which reads exactly like a clean bill of health.
        """
        tree = ast.parse("def outer():\n    def inner():\n        pass\n")
        kids = ch._direct_children(tree)
        assert any(isinstance(k, ast.FunctionDef) and k.name == "outer"
                   for k in kids)
        outer = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "outer")
        assert any(isinstance(k, ast.FunctionDef) and k.name == "inner"
                   for k in ch._direct_children(outer))

    def test_a_nested_body_is_not_folded_into_its_parent(self):
        """A helper's local import must not shadow its parent's — that would
        create the ambiguity this exists to avoid."""
        tree = ast.parse(
            "def outer():\n"
            "    def inner():\n"
            "        from a.b import fn\n"
            "    fn()\n"
        )
        outer = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "outer")
        bindings = ch._bindings_from(
            ch._direct_children(outer), "pkg.mod", False)
        assert "fn" not in bindings


class TestTheBlindSpotIsReported:
    def test_unresolved_calls_are_recorded_not_silently_dropped(self):
        """A tool that quietly discards what it cannot resolve looks identical
        to one with nothing to find. `unresolved_calls()` is the difference,
        and it is the same distinction between "unprovable" and "proven" that
        the liveness work landed today."""
        ch.audit()
        assert isinstance(ch.unresolved_calls(), tuple)

    def test_the_report_names_module_callee_and_line(self):
        ch.audit()
        for row in ch.unresolved_calls():
            assert len(row) == 3
            module, callee, line = row
            assert isinstance(module, str) and module
            assert isinstance(callee, str) and callee
            assert isinstance(line, int)


class TestNeverRaises:
    @pytest.mark.parametrize("hostile", [
        "", "   ", "fn(", "def f(:\n  pass", "\x00", "λ()",
    ])
    def test_malformed_sources_do_not_break_the_audit(self, hostile):
        try:
            tree = ast.parse(hostile)
        except SyntaxError:
            return                      # the auditor's _parse returns None
        assert isinstance(
            ch._bindings_from(ch._direct_children(tree), "m", False), dict)

    def test_resolution_never_raises_on_odd_nodes(self):
        for src in ("(lambda: 1)()", "[f for f in x][0]()", "d['k']()"):
            calls = [n for n in ast.walk(ast.parse(src))
                     if isinstance(n, ast.Call)]
            for call in calls:
                assert ch._resolve_callee(
                    call, ({},), frozenset(), "m") in (None,) or True
