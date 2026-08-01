"""A convention wired for DISPLAY but not for EXECUTION.

`repl_completion._HANDLER_PREFIX` is ``"_handle_"``, and `discover_verbs`
walks `SerpentREPL` for those methods to BUILD THE PALETTE. So the codebase
already treats ``_handle_<verb>`` as the definition of a verb.

It never dispatched on it. Execution was 30 hand-written
``self._handle_*(...)`` calls inside a 495-line ladder of 48 branches, and
``grep`` for a generic route returned nothing. A convention half-wired that
way drifts in exactly one direction, and it had: **`_handle_trace` is
discovered, appears in the palette, and had no path to run.**

Two more consequences visible from the same measurement:

  * ``/cost`` and ``/posture`` have BOTH a ladder branch and a registered
    module dispatcher. The ladder runs first and returns, so the module
    version is unreachable — while the palette describes the module version.
    ``/cost`` is advertised as "per phase and per provider" (that is
    `cost_repl`'s sentence) and the operator gets `_print_cost`'s session
    counters instead.
  * every new REPL-local verb costs a hand-written branch, which is why the
    method is 495 lines.

The fallback is placed AFTER the auto-dispatch registry on purpose. Before it,
a `_handle_<verb>` would shadow a registered module dispatcher — creating a
second instance of the very defect the two shadows above already are.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

import pytest

_SOURCE = Path(
    "backend/core/ouroboros/battle_test/serpent_flow.py"
).resolve()


def _repl_class():
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentREPL
    return SerpentREPL


class _Console:
    def __init__(self):
        self.lines = []

    def print(self, *a, **k):
        self.lines.append(" ".join(str(x) for x in a))


class _Flow:
    def __init__(self):
        self.console = _Console()


def _bare_repl():
    """A SerpentREPL with only what the dispatcher touches.

    Constructed without ``__init__`` deliberately: the real one boots a
    cockpit's worth of collaborators, and this is a test about routing.
    """
    cls = _repl_class()
    repl = cls.__new__(cls)
    repl._flow = _Flow()
    repl._on_command = None
    return repl


# ---------------------------------------------------------------------------
# 1. the gap that motivated it
# ---------------------------------------------------------------------------


class TestEveryDiscoveredVerbCanRun:
    def test_no_handler_is_discovered_without_a_route(self):
        """THE invariant. A verb in the palette that cannot be dispatched is a
        row the operator can select and nothing happens.

        Asserted over the live class, so a `_handle_*` added tomorrow is
        covered by a test written today.
        """
        cls = _repl_class()
        handlers = {
            name[len("_handle_"):] for name in dir(cls)
            if name.startswith("_handle_") and callable(getattr(cls, name, None))
        }
        assert handlers, "the discovery convention itself vanished"

        source = _SOURCE.read_text(encoding="utf-8", errors="replace")
        hand_routed = set(re.findall(r"self\._handle_([a-z_]+)\(", source))
        repl = _bare_repl()

        unreachable = []
        for verb in sorted(handlers):
            if verb in hand_routed:
                continue                      # the ladder owns it
            handler = getattr(repl, f"_handle_{verb}", None)
            params = [
                p for p in inspect.signature(handler).parameters.values()
                if p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)
            ]
            required = [p for p in params
                        if p.default is inspect.Parameter.empty]
            if len(required) > 1:
                continue                      # bespoke, needs parsing
            # Everything else MUST be reachable through the generic route.
            if not asyncio.run(repl._dispatch_discovered_verb(f"/{verb}")):
                unreachable.append(verb)
        assert not unreachable, (
            f"discovered but undispatchable: {unreachable}")

    def test_trace_specifically_now_routes(self):
        """The concrete instance: `_handle_trace` was in the palette with no
        caller anywhere in the file."""
        repl = _bare_repl()
        assert hasattr(repl, "_handle_trace")
        assert asyncio.run(repl._dispatch_discovered_verb("/trace"))


# ---------------------------------------------------------------------------
# 2. signature adaptation — by shape, not by a table
# ---------------------------------------------------------------------------


class TestSignatureAdaptation:
    def test_a_line_taking_handler_receives_the_line(self):
        repl = _bare_repl()
        seen = []
        repl._handle_probe = lambda line: seen.append(line)
        assert asyncio.run(repl._dispatch_discovered_verb("/probe a b"))
        assert seen == ["/probe a b"]

    def test_a_zero_arg_handler_is_called_bare(self):
        repl = _bare_repl()
        seen = []
        repl._handle_probe = lambda: seen.append("called")
        assert asyncio.run(repl._dispatch_discovered_verb("/probe"))
        assert seen == ["called"]

    def test_an_async_handler_is_awaited(self):
        repl = _bare_repl()
        seen = []

        async def _h(line):
            await asyncio.sleep(0)
            seen.append(line)

        repl._handle_probe = _h
        assert asyncio.run(repl._dispatch_discovered_verb("/probe x"))
        assert seen == ["/probe x"]

    def test_a_multi_required_arg_handler_is_LEFT_to_the_ladder(self):
        """`_handle_cancel(op_id, immediate)` needs parsing the ladder already
        does. Guessing at the split would be worse than declining."""
        repl = _bare_repl()
        repl._handle_probe = lambda op_id, immediate: None
        assert asyncio.run(repl._dispatch_discovered_verb("/probe x")) is False

    def test_defaulted_args_do_not_count_as_required(self):
        repl = _bare_repl()
        seen = []
        repl._handle_probe = lambda line, extra=None: seen.append(line)
        assert asyncio.run(repl._dispatch_discovered_verb("/probe q"))
        assert seen == ["/probe q"]


# ---------------------------------------------------------------------------
# 3. the getattr is on operator-controlled text
# ---------------------------------------------------------------------------


class TestNameConfinement:
    @pytest.mark.parametrize("hostile", [
        "/__class__", "/_flow", "/../x", "/a-b", "/a.b", "/A", "/1abc",
        "/", "//", "/ ", "", "   ", "/a b/../c",
    ])
    def test_hostile_verbs_never_reach_an_attribute(self, hostile):
        """``getattr(self, "_handle_" + text)`` on unconstrained input reaches
        attributes that are not verbs. The prefix alone does not save you —
        ``_handle_`` plus arbitrary text is still arbitrary — so the name is
        constrained to a lowercase identifier first."""
        repl = _bare_repl()
        assert asyncio.run(repl._dispatch_discovered_verb(hostile)) is False

    def test_a_non_callable_attribute_is_refused(self):
        repl = _bare_repl()
        repl._handle_probe = "not a function"
        assert asyncio.run(repl._dispatch_discovered_verb("/probe")) is False

    def test_an_unknown_verb_falls_through_to_the_typo_path(self):
        """Returning True would swallow the line and the operator would get
        silence instead of a suggestion."""
        repl = _bare_repl()
        assert asyncio.run(
            repl._dispatch_discovered_verb("/definitelynotaverb")) is False


# ---------------------------------------------------------------------------
# 4. failure containment
# ---------------------------------------------------------------------------


class TestNeverKillsTheRepl:
    def test_a_throwing_handler_is_reported_not_raised(self):
        repl = _bare_repl()

        def _boom(line):
            raise RuntimeError("handler exploded")

        repl._handle_probe = _boom
        assert asyncio.run(repl._dispatch_discovered_verb("/probe")) is True
        assert any("exploded" in row for row in repl._flow.console.lines)

    def test_a_throwing_async_handler_is_contained(self):
        repl = _bare_repl()

        async def _boom(line):
            raise RuntimeError("async exploded")

        repl._handle_probe = _boom
        assert asyncio.run(repl._dispatch_discovered_verb("/probe")) is True

    def test_a_broken_console_does_not_re_raise(self):
        repl = _bare_repl()

        class _Bad:
            def print(self, *a, **k):
                raise RuntimeError("console down")

        repl._flow.console = _Bad()
        repl._handle_probe = lambda line: (_ for _ in ()).throw(
            RuntimeError("x"))
        assert asyncio.run(repl._dispatch_discovered_verb("/probe")) is True


# ---------------------------------------------------------------------------
# 5. ordering — the fallback must not become a new shadow
# ---------------------------------------------------------------------------


class TestItRunsAfterTheRegistry:
    def test_the_call_site_is_below_the_auto_dispatch(self):
        """Placement is the whole safety argument.

        Above `_try_dispatch`, a `_handle_<verb>` would shadow a registered
        module dispatcher — the exact defect `/cost` and `/posture` already
        are. Asserted on ORDER in the source because that is what decides it.
        """
        source = _SOURCE.read_text(encoding="utf-8", errors="replace")
        registry = source.index("_try_dispatch(line)")
        fallback = source.index("_dispatch_discovered_verb(line)")
        assert registry < fallback, (
            "the generic fallback moved above the auto-dispatch registry and "
            "now shadows every registered module dispatcher")

    def test_the_known_shadows_are_recorded(self):
        """`/cost` and `/posture` have a ladder branch AND a registered
        dispatcher; the ladder wins and the palette describes the loser.

        Recorded rather than silently fixed: choosing which implementation an
        operator should get is a product decision. `_print_cost` shows this
        session's counters, `cost_repl` shows the per-phase/per-provider
        rollup, and both are real answers to different questions.
        """
        from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
            _VERB_TO_DISPATCHER, prime_registry,
        )
        prime_registry()
        source = _SOURCE.read_text(encoding="utf-8", errors="replace")
        ladder = set(re.findall(r'line in \("([a-z_]+)", "/\1"\)', source))
        shadows = sorted(v for v in ladder if v in _VERB_TO_DISPATCHER)
        assert shadows == ["cost", "posture"], (
            f"the shadow set changed: {shadows}. A NEW shadow means a verb's "
            f"palette description now describes code that cannot run.")
