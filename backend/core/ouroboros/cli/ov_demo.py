"""`ov demo` — watch the cockpit without paying for it.

Thirty-odd PRs of cockpit work shipped this month, verified by unit tests and
one PTY probe. Almost none of it has been WATCHED running, because seeing it
meant booting the organism and spending model calls to make something happen.

So this drives the real surfaces with synthetic events. No provider, no
network, no tokens. Two questions it answers that nothing else does:

  ``ov demo board``      where does `ov` actually STAND — live / dark / dynamic
  ``ov demo transcript`` what does the CC-style deck LOOK like

The rule that makes it worth having
-----------------------------------
It calls the SAME renderers the cockpit calls. A demo with its own draw path
is worse than no demo: it agrees with itself while the cockpit is broken, and
that is the exact defect shape this codebase has hit repeatedly — a fake
modelling a superseded seam, a producer with no consumer, a completer
implementing the one method the library never calls. Every one passed its own
tests.

So `moltbook_inline.render_thread`, `verb_description.to_operator_voice` and
`progress_board.render_board` are imported and called, never reimplemented. If
a renderer regresses, this regresses with it — which is the entire point.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, List, Optional, Sequence

logger = logging.getLogger("Ouroboros.OvDemo")

__all__ = ["run_demo", "demo_scenes", "DEMO_HELP"]

DEMO_HELP = """ov demo -- watch the cockpit, no model calls

  ov demo             board + transcript
  ov demo board       what is LIVE vs DARK right now (derived from the tree)
  ov demo surfaces    which of the 3 surfaces can REACH each module
  ov demo transcript  the CC-style deck: ops, diffs, agora threads
  ov demo live        the COCKPIT running, driven by synthetic events
                      (needs a real terminal; --speed=N to fast-forward)
  ov demo scenes      list available scenes
"""



def _ensure_primed() -> bool:
    """Prime the verb registry once per process. Returns whether it is primed.

    The demo is a demo of CAPABILITY — "what can this cockpit do" — so it
    primes, exactly as the cockpit does at boot. The progress board deliberately
    does not, because a read-only status view must not change what the process
    has imported.

    Both scenes route through here. When they primed independently, `ov demo
    board` reported `verbs primed 0` and `ov demo transcript` reported 62 IN
    THE SAME RUN, because only one of them had asked.
    """
    try:
        from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
            prime_registry, registry_primed,
        )
        if not registry_primed():
            prime_registry()
        return bool(registry_primed())
    except Exception:  # noqa: BLE001
        logger.debug("[OvDemo] verb priming degraded", exc_info=True)
        return False

def _rule(console: Any, title: str) -> None:
    try:
        console.rule(f"[dim]{title}[/dim]")
    except Exception:  # noqa: BLE001 — a demo must never be the thing that breaks
        try:
            console.print(f"-- {title} --", markup=False)
        except Exception:  # noqa: BLE001
            pass


def _say(console: Any, text: str = "") -> None:
    try:
        console.print(text, markup=False, highlight=False)
    except Exception:  # noqa: BLE001
        pass


def _markup(console: Any, text: str = "") -> None:
    """Print a line the deck grammar composed — tags INTERPRETED.

    Separate from :func:`_say` on purpose. `_say` suppresses markup because
    board rows and error text are literal operator strings that must survive a
    stray bracket; deck lines are the opposite, and printing them through
    `_say` would show the operator `[grey50]847 lines[/grey50]`.
    """
    try:
        console.print(text, highlight=False)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Scene: the progress board
# ---------------------------------------------------------------------------


def scene_board(console: Any, argv: Sequence[str] = ()) -> int:
    """Where `ov` stands, derived from the tree rather than from a checklist.

    The scan is real (thousands of `ast.parse` calls, a few seconds). It is not
    cached across invocations on purpose: a status view that shows yesterday's
    answer is worse than one that takes four seconds, because the operator
    cannot tell which they are looking at.
    """
    try:
        from backend.core.ouroboros.battle_test.progress_board import (
            DARK, DYNAMIC_LIVE, ENTRY, LIVE, OFF, ProgressBoard, render_board,
        )
    except Exception as exc:  # noqa: BLE001
        _say(console, f"  progress board unavailable: {exc}")
        return 1

    _ensure_primed()
    _rule(console, "where ov stands")
    reading = ProgressBoard().read()
    for line in render_board(reading, limit=_limit(argv)):
        _say(console, line)

    # The states that are NOT plain live/dark carry the interesting news, and
    # a count line alone buries them.
    dynamic = reading.by_state(DYNAMIC_LIVE)
    if dynamic:
        _say(console)
        _say(console, "  discovered at runtime (no inbound import):")
        from backend.core.ouroboros.battle_test.progress_board import (
            terminal_width,
        )
        cols = terminal_width()
        # Same defect as render_board had: a padded column plus an unclipped
        # reason wraps the moment it meets a real terminal. Two formatters, one
        # bug — so both now ask the same source how wide the world is.
        namew = min(44, max((len(r.flag) for r in dynamic[:6]), default=20))
        for row in dynamic[:6]:
            room = max(8, cols - namew - 8)
            reason = row.reason if len(row.reason) <= room else (
                row.reason[: room - 1] + "…")
            _say(console, f"    ◇ {row.flag:<{namew}}  {reason}"[:cols])
    _say(console)
    verbs = (f"{len(reading.verbs)} verbs" if reading.verbs_primed
             else "verbs NOT primed")
    _say(console, f"  entry points {len(reading.by_state(ENTRY))}"
                  f" · off {len(reading.by_state(OFF))} · {verbs}")
    _say(console, "  dark = enabled, on disk, imported by nothing. A SIGNAL,")
    _say(console, "  not a defect count: plugin discovery and dynamic import")
    _say(console, "  remain invisible to a static graph.")
    return 0


def scene_surfaces(console: Any, argv: Sequence[str] = ()) -> int:
    """Which of `ov`'s three surfaces can reach a module.

    The board answers "is this imported by anything"; this answers "by
    WHICH surface", which is the question five separate shipped-but-invisible
    modules turned out to hinge on. Same discipline as the board: derived
    from the tree, never cached, and stated as a signal rather than a verdict.
    """
    try:
        from backend.core.ouroboros.battle_test.surface_reachability import (
            audit, render,
        )
    except Exception as exc:  # noqa: BLE001
        _say(console, f"  surface audit unavailable: {exc}")
        return 1
    _rule(console, "which surface can reach what")
    for line in render(audit(), limit=_limit(argv)):
        _say(console, line)
    return 0


def _limit(argv: Sequence[str]) -> int:
    for arg in argv:
        if arg.startswith("--limit="):
            try:
                return max(0, int(arg.split("=", 1)[1]))
            except (TypeError, ValueError):
                return 12
    return 12


# ---------------------------------------------------------------------------
# Scene: the transcript
# ---------------------------------------------------------------------------

#: A synthetic op, as BEATS rather than as finished lines: `(kind, args)`
#: pairs the deck grammar composes. Writing the lines out here is what let the
#: `⎿` results and the diff beneath them disagree about which column a
#: continuation body starts in — two literals, one column, nobody to notice.
#:
#: Deliberately a FAILING op: the deck's whole grammar — `⎿` results, an agora
#: thread reacting to a failure — only shows itself when something goes wrong,
#: and a demo of the happy path shows the least interesting third of it.
_SCRIPT: List[Any] = [
    ("act", ("Read", "backend/core/ouroboros/governance/risk_tier_floor.py")),
    ("det", ("Read 847 lines",)),
    ("act", ("Update", "risk_tier_floor.py")),
    ("det", ("Updated risk_tier_floor.py with 18 additions and 3 removals",)),
    ("diff", (412, "+", "    except RiskFloorConfigError:")),
    ("diff", (413, "+", "        raise")),
    ("diff", (414, "-", "    except Exception:  # noqa: BLE001")),
    ("act", ("Validate", "7759-86")),
    ("det", ("✗ 3 failed · test_scoped_paths, test_sandbox_dir", "crit")),
]


def _compose(kind: str, args: Sequence[Any]) -> str:
    """One beat -> one deck line, through the SHARED grammar.

    Every scene routes through here, so the transcript and the live cockpit
    cannot drift into two column disciplines — which is exactly what they had
    done: `  ⎿ ` (body at column 4) above `     + ` (body at column 5).
    """
    from backend.core.ouroboros.ui import deck_grammar as deck
    try:
        if kind == "act":
            # `(verb, arg, tone)` — tone is keyword-only on the grammar so a
            # call site cannot pass a colour where an argument belongs.
            return deck.action(args[0], args[1] if len(args) > 1 else "",
                               tone=(args[2] if len(args) > 2 else "ok"))
        if kind == "det":
            return deck.detail(args[0], tone=(args[1] if len(args) > 1
                                              else "muted"))
        if kind == "diff":
            return deck.diff(*args)
        if kind == "voice":
            return deck.voice(*args)
        return deck.blank()
    except Exception:  # noqa: BLE001 — a demo must never be what breaks
        logger.debug("[OvDemo] beat degraded: %s", kind, exc_info=True)
        return ""

#: The agora reacting to that failure, in the schema `moltbook_inline` expects.
_THREAD: List[dict] = [
    {"handle": "@the-pit", "op_id": "op-7759-86",
     "body": "three tests. you fixed the assert and broke the file."},
    {"handle": "@the-builder", "op_id": "op-7759-86",
     "body": "the containment check is right, the fixture is stale"},
    # No `⚔` in the body: `render_post` derives the glyph from `kind`, and
    # carrying it in the text too rendered `▸ ⚔ @cassandra  ⚔ REVIEW …`.
    # A glyph that means "contested" means it once per line or it is noise.
    {"handle": "@cassandra", "op_id": "op-7759-86", "kind": "conflict",
     "body": "REVIEW disagrees: that except clause still swallows I2"},
]


def scene_transcript(console: Any, argv: Sequence[str] = ()) -> int:
    """The CC-style deck, rendered by the cockpit's OWN renderers."""
    _rule(console, "transcript")
    # A blank line before each new action is the deck's only grouping cue.
    # Without it nine correct lines read as one paragraph, which is what
    # "it doesn't look like Claude Code" actually means most of the time.
    for i, (kind, args) in enumerate(_SCRIPT):
        if kind in ("act", "voice") and i:
            _markup(console, "")
        _markup(console, _compose(kind, args))

    try:
        from backend.core.ouroboros.battle_test.moltbook_inline import (
            render_thread,
        )
        # `_markup`, not `_say`: the thread renderer tints its glyph and
        # handle now, and printing that through a markup=False path shows the
        # operator `[#58B0F8]💬[/#58B0F8]` — and then wraps the line, because
        # the tags are counted as visible width.
        for line in render_thread(_THREAD, width=76):
            _markup(console, line)
    except Exception as exc:  # noqa: BLE001
        _say(console, f"  (agora unavailable: {exc})")

    _say(console)
    _rule(console, "palette")
    _demo_palette(console)
    return 0


def _demo_palette(console: Any) -> None:
    """A few real verbs, described by the real normaliser.

    Verbs come from the LIVE registry, so if priming collapses — as it once did
    from 76 to 18 when an edit orphaned a decorator — this scene shrinks
    visibly instead of quietly rendering a shorter list nobody counts.
    """
    try:
        from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
            list_verbs,
        )
        from backend.core.ouroboros.battle_test.repl_completion import (
            describe_dispatcher,
        )
        _ensure_primed()
        verbs = list(list_verbs())
        if not verbs:
            _say(console, "  (no verbs primed — registry empty)")
            return
        for verb in verbs[:8]:
            desc = ""
            try:
                # The FULL cascade the real palette uses, not just its first
                # rung. Calling `to_operator_voice` directly rendered a blank
                # for every verb whose description lives in its module
                # docstring — the demo disagreeing with the surface it previews.
                desc = describe_dispatcher(_dispatcher_for(verb))[:46]
            except Exception:  # noqa: BLE001
                desc = ""
            _say(console, f"  /{verb:<20} {desc}")
        _say(console, f"  … {len(verbs)} verbs primed")
    except Exception as exc:  # noqa: BLE001
        _say(console, f"  (palette unavailable: {exc})")


def _dispatcher_for(verb: str) -> Any:
    from backend.core.ouroboros.battle_test import repl_dispatch_registry as reg
    table = getattr(reg, "_VERB_TO_DISPATCHER", {}) or {}
    return table.get(verb)



# ---------------------------------------------------------------------------
# Scene: live
# ---------------------------------------------------------------------------

#: Tool RESULTS, as the tools actually return them. Deliberately raw: the
#: registry's summarizers are what turn 847 lines into "847 lines read" and a
#: pytest tail into "command failed", and handing them a pre-summarized string
#: would demo the sentence rather than the summarizer.
_READ_RESULT = "\n".join(
    f"    # risk_tier_floor.py line {i}" for i in range(847)
)
#: A real search: 14 hits across 4 files, each one different. Under the
#: density budget on purpose — the elision belongs on the failure below,
#: where an operator actually wants to expand, not on a grep they skim.
_SEARCH_RESULT = "\n".join(
    f"backend/core/ouroboros/governance/{mod}.py:{line}:"
    f"    except Exception:  {tail}"
    for mod, line, tail in [
        ("risk_tier_floor", 412, "# noqa: BLE001"),
        ("risk_tier_floor", 488, "# floor resolution"),
        ("risk_tier_floor", 631, "# quiet-hours parse"),
        ("semantic_guardian", 204, "# pattern scan"),
        ("semantic_guardian", 377, "# AST walk"),
        ("semantic_guardian", 512, "# credential shape"),
        ("iron_gate", 118, "# exploration ledger"),
        ("iron_gate", 292, "# ascii strictness"),
        ("iron_gate", 401, "# retry feedback"),
        ("gate_runner", 87, "# attribution floor"),
        ("gate_runner", 156, "# test-only notify"),
        ("gate_runner", 233, "# containment probe"),
        ("gate_runner", 310, "# risk-tier compose"),
        ("gate_runner", 448, "# advisory ledger"),
    ]
)


_EDIT_RESULT = (
    "--- a/backend/core/ouroboros/governance/risk_tier_floor.py\n"
    "+++ b/backend/core/ouroboros/governance/risk_tier_floor.py\n"
    "@@ -410,7 +410,22 @@\n"
    "     try:\n"
    "         return _resolve_floor(raw)\n"
    "-    except Exception:  # noqa: BLE001\n"
    "-        return SAFE_AUTO\n"
    "+    except RiskFloorConfigError:\n"
    "+        # A malformed floor is the ONE case that must not degrade\n"
    "+        # quietly: silently returning SAFE_AUTO turns a broken config\n"
    "+        # into permission the operator never granted.\n"
    "+        raise\n"
)
#: The pytest tail as pytest actually prints it — long enough to exceed the
#: density budget, so the elision AND its `/expand t-N` recovery hint land on
#: the failure. That is where truncation matters: an operator reading a red
#: test wants the rest, and a demo that elides without showing the way back
#: teaches that elision is loss.
_PYTEST_FAIL = "\n".join([
    "============================= test session starts =====================",
    "collected 47 items",
    "",
    "tests/governance/test_risk_tier_floor.py ..F..F......F................",
    "",
    "================================== FAILURES ===========================",
    "_____________________________ test_scoped_paths _______________________",
    "",
    "    def test_scoped_paths():",
    "        floor = resolve_floor({'min_tier': 'notify_apply'})",
    ">       assert floor is NOTIFY_APPLY",
    "E       AssertionError: assert SAFE_AUTO is NOTIFY_APPLY",
    "",
    "tests/governance/test_risk_tier_floor.py:88: AssertionError",
    "_____________________________ test_sandbox_dir ________________________",
    "",
    "    def test_sandbox_dir(tmp_path):",
    "        with pytest.raises(BlockedPathError):",
    ">           _normalize(tmp_path / 'outside')",
    "E       Failed: DID NOT RAISE <class 'BlockedPathError'>",
    "",
    "tests/governance/test_risk_tier_floor.py:141: Failed",
    "___________________________ test_quiet_hours_tz _______________________",
    "",
    "    def test_quiet_hours_tz():",
    "        floor = resolve_floor({'quiet_hours': '22-06'}, tz='America/Denver')",
    ">       assert floor is NOTIFY_APPLY",
    "E       AssertionError: assert SAFE_AUTO is NOTIFY_APPLY",
    "",
    "tests/governance/test_risk_tier_floor.py:203: AssertionError",
    "=========================== short test summary info ===================",
    "FAILED tests/governance/test_risk_tier_floor.py::test_scoped_paths",
    "FAILED tests/governance/test_risk_tier_floor.py::test_sandbox_dir",
    "FAILED tests/governance/test_risk_tier_floor.py::test_quiet_hours_tz",
    "3 failed, 44 passed in 4.12s",
])
_PYTEST_PASS = (
    "tests/governance/test_risk_tier_floor.py::test_scoped_paths PASSED\n"
    "tests/governance/test_risk_tier_floor.py::test_sandbox_dir PASSED\n"
    "tests/governance/test_risk_tier_floor.py::test_quiet_hours_tz PASSED\n"
    "47 passed in 3.98s"
)

#: The scripted operation, as SECONDS-since-start paired with a BEAT.
#:
#: Beats are SEMANTIC EVENTS, not lines — `("tool", ("bash", cmd, output,
#: "error"))` rather than a pre-composed `⏺ Bash(…)`. That distinction is this
#: module's entire premise applied to the one scene an operator actually
#: WATCHES.
#:
#: Hand-written lines were a second draw path wearing the deck's clothes. They
#: agreed with themselves while `tool_render_registry` — the descriptor table
#: the cockpit renders EVERY tool call through, with its own per-tool icon,
#: argument summarizer, result summarizer, status glyph, density policy and
#: `t-N` expansion refs — went entirely unexercised. A demo that cannot show a
#: regression in the renderer it is demonstrating is decoration.
#:
#: So the payloads here are REAL: `read_file` returns 847 lines of text and
#: the registry's summarizer counts them; `bash` returns pytest output and the
#: summarizer decides it failed. Nothing in this list states a conclusion the
#: renderer is supposed to reach.
_LIVE_BEATS: List[Any] = [
    (0.4, "act", ("Signal", "test_failure · risk_tier_floor.py")),
    (1.0, "det", ("2 source loci · 1 test locus",)),

    (2.0, "tool", ("read_file",
                   "backend/core/ouroboros/governance/risk_tier_floor.py",
                   _READ_RESULT, "success", 310)),
    (3.4, "tool", ("search_code", "except Exception",
                   _SEARCH_RESULT, "success", 180)),

    (5.0, "voice", ("the vision floor raises, and the caller swallows it",)),

    # WHO is working — the roster the cockpit now mounts. Driven through the
    # real `AgentRoster`, so the agent view fills in as the demo runs instead
    # of being described in a line of text.
    (5.6, "agent", ("spawn", "sub-3f2a", "Explore",
                    "map every caller of the vision floor")),
    (6.0, "agent", ("spawn", "sub-9c11", "Review",
                    "try to refute the containment fix")),

    (7.2, "tool", ("edit_file", "backend/core/ouroboros/governance/"
                                "risk_tier_floor.py",
                   _EDIT_RESULT, "success", 90)),
    (9.2, "tool", ("bash",
                   "python3 -m pytest tests/governance/"
                   "test_risk_tier_floor.py -q",
                   _PYTEST_FAIL, "error", 4120)),

    (11.0, "agent", ("finish", "sub-3f2a", "finished",
                     "36 findings · 8 tools")),
    # The agora reacting, rendered by the REAL renderer — see `_agora_beats`.
    (11.6, "agora", ()),

    (15.2, "act", ("Repair", "L2 · iteration 1/5", "warn")),
    (16.8, "det", ("fixture rebuilt from the live seam",)),
    (17.4, "agent", ("finish", "sub-9c11", "finished",
                     "no counterexample · 12 tools")),
    (18.0, "tool", ("run_tests", "tests/governance/test_risk_tier_floor.py",
                    _PYTEST_PASS, "success", 3980)),

    (20.4, "act", ("Gate", "7759-86 · NOTIFY_APPLY")),
    (21.2, "det", ("applied · verified · committed 90706b8", "ok")),
    (22.4, "act", ("Complete", "7759-86 · 22.4s · $0.011")),
]

#: When each agora line lands, once the thread renderer has produced them.
#: A reply arriving a beat after the post is what makes the room read as
#: people talking rather than as a block of text that appeared.
_AGORA_AT = (11.6, 12.6, 13.8)


def _agora_beats() -> List[Any]:
    """The room's reaction, from `moltbook_inline` — never reimplemented here.

    The live scene used to carry these three lines as literals, which meant
    the one scene an operator actually WATCHES was the one scene that would
    keep looking right after the real renderer regressed. Calling it is the
    entire premise of this module.
    """
    try:
        from backend.core.ouroboros.battle_test.moltbook_inline import (
            render_thread,
        )
        lines = list(render_thread(_THREAD, width=76))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[OvDemo] agora degraded", exc_info=True)
        lines = [f"  (agora unavailable: {exc})"]
    return [(_AGORA_AT[min(i, len(_AGORA_AT) - 1)], line)
            for i, line in enumerate(lines)]


def _tool_beats(at: float, args: Sequence[Any]) -> List[Any]:
    """One tool call → the lines the COCKPIT would draw for it.

    Routed through `tool_render_view.compose` — the same function
    `serpent_flow` calls for every real tool round — so this scene exercises
    the descriptor table, the per-tool icon, both summarizers, the status
    glyph, the density policy and the `t-N` expansion refs. A hand-written
    `⏺ Bash(…)` exercised none of them and would have kept looking correct
    through a regression in every one.

    Body lines are dealt out over the block's duration rather than at its
    timestamp: a tool that returns forty lines does not return them all in the
    same instant, and the rhythm is most of what the cockpit feels like.
    """
    out: List[Any] = []
    try:
        from backend.core.ouroboros.battle_test.tool_render_registry import (
            ToolStatus,
        )
        from backend.core.ouroboros.battle_test.tool_render_view import compose

        name, tool_args, result, status, duration_ms = (
            list(args) + [None] * 5
        )[:5]
        composed = compose(
            str(name), str(tool_args), str(result),
            status=ToolStatus.coerce(status),
            duration_ms=float(duration_ms or 0.0),
            op_id="7759-86",
            store=_body_store(),
        )
        out.append((at, ""))
        out.append((at, composed.header_markup))
        if composed.summary_markup:
            out.append((at + 0.35, composed.summary_markup))
        body = list(composed.body_lines_markup)
        # Spread across the second after the summary, however many there are.
        step = (0.9 / len(body)) if body else 0.0
        for i, line in enumerate(body):
            out.append((at + 0.5 + i * step, line))
        if composed.expansion_hint:
            out.append((at + 1.5, composed.expansion_hint))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[OvDemo] tool beat degraded", exc_info=True)
        out.append((at, f"  (tool render unavailable: {exc})"))
    return out


def _agent_beats(at: float, args: Sequence[Any]) -> List[Any]:
    """A dispatch or a return, through the REAL roster — at PLAYBACK time.

    Returned as a CALLABLE rather than a line, and that is the whole point.
    The roster is live state: it holds a start instant and reports elapsed
    against the clock. Driving it during composition would spawn and finish
    every agent in the same microsecond, so the demo would open with a roster
    already full of agents that each ran for `0s` — a surface that is
    populated before the first frame demonstrates nothing about filling in.

    Executed on the beat, the agent view does what it does in a real session:
    rows appear as work is dispatched, their durations climb while the deck
    scrolls past, and they resolve as results land.
    """

    def _run() -> Optional[str]:
        try:
            from backend.core.ouroboros.battle_test.agent_roster import (
                get_agent_roster,
            )
            roster = get_agent_roster()
            verb = str(args[0]) if args else ""
            if verb == "spawn":
                roster.spawn(str(args[1]), str(args[2]), str(args[3]))
                return None          # the roster row IS the output
            entry = roster.finish(str(args[1]), str(args[2]), str(args[3]))
            return roster.finished_notice(entry) or None
        except Exception:  # noqa: BLE001
            logger.debug("[OvDemo] agent beat degraded", exc_info=True)
            return None

    return [(at, ""), (at, _run)]


def _agent_view_rows() -> Any:
    """The daemon cockpit's OWN roster provider, or None.

    `serpent_flow._local_agent_rows` already reads the process singleton and
    sizes it to this terminal. Importing it means the demo cannot render a
    roster the cockpit would draw differently — a second provider here would
    be a second opinion about width, height budget and folding.
    """
    try:
        from backend.core.ouroboros.battle_test.serpent_flow import (
            _local_agent_rows,
        )
        return _local_agent_rows
    except Exception:  # noqa: BLE001
        logger.debug("[OvDemo] agent view unavailable", exc_info=True)
        return None


def _body_store() -> Any:
    """A real `BoundedBodyStore`, so `/expand t-N` refs in the demo are REFS.

    A None store still renders — `compose` degrades gracefully — but it emits
    no expansion hint, and the hint is exactly the affordance an operator
    needs to learn exists. Demonstrating an elision without its recovery path
    teaches that elision is loss.
    """
    global _STORE
    try:
        if _STORE is None:
            from backend.core.ouroboros.battle_test.tool_render_store import (
                BoundedBodyStore,
            )
            _STORE = BoundedBodyStore()
        return _STORE
    except Exception:  # noqa: BLE001
        return None


_STORE: Any = None


def compose_live_script() -> List[Any]:
    """`[(seconds, line), ...]` — the beats, composed and block-separated.

    Called fresh by :func:`scene_live` rather than baked at import, so a
    renderer swapped underneath (a test, a regression) is the one that shows.
    """
    out: List[Any] = []
    for i, (at, kind, args) in enumerate(_LIVE_BEATS):
        if kind == "agora":
            out.extend(_agora_beats())
            continue
        if kind == "tool":
            out.extend(_tool_beats(at, args))
            continue
        if kind == "agent":
            out.extend(_agent_beats(at, args))
            continue
        # The separator rides the SAME timestamp as the line it precedes, so
        # the block opens as one visual event instead of a gap that appears a
        # beat early and reads as the deck stalling.
        if kind in ("act", "voice") and i:
            out.append((at, ""))
        out.append((at, _compose(kind, args)))
    # Beats now emit blocks rather than single lines, and a block's body can
    # run past the next beat's timestamp. Sorted so the driver — which sleeps
    # against the schedule — never meets a target already in the past and
    # dumps the remainder at once.
    out.sort(key=lambda pair: pair[0])
    return out


#: Composed once at import for introspection (`--speed` docs, tests). The live
#: scene recomposes; this is a snapshot, never the thing that renders.
_LIVE_SCRIPT = compose_live_script()

#: Windows during which the toolbar shows a token counter climbing, because in
#: the real cockpit that is the ONLY sign the organism is thinking. A static
#: spinner and a moving number read completely differently at 2am.
_GENERATING = ((5.0, 9.2), (15.2, 18.0))


def live_speed(argv: Sequence[str]) -> float:
    for arg in argv:
        if arg.startswith("--speed="):
            try:
                return max(0.1, min(20.0, float(arg.split("=", 1)[1])))
            except (TypeError, ValueError):
                return 1.0
    return 1.0


def _live_toolbar(started: Callable[[], float], clock: Callable[[], float]):
    """The pulse line, built from the CANONICAL frame source.

    `ui.theme.ouroboros_frame` is what `serpent_flow` and `attach_heartbeat`
    already render, so every surface shows the same frame at the same instant.
    A second spinner here would drift against the real one and the demo would
    be teaching the wrong rhythm.
    """
    def _render() -> str:
        try:
            from backend.core.ouroboros.ui.theme import ouroboros_frame
            now = clock()
            elapsed = max(0.0, now - started())
            glyph = ouroboros_frame(now)
            phase = "Synthesizing" if any(
                lo <= elapsed <= hi for lo, hi in _GENERATING) else "Watching"
            # Derived from elapsed rather than accumulated, so a dropped frame
            # cannot desynchronise the number from the clock beside it.
            tokens = int(elapsed * 780)
            mins, secs = divmod(int(elapsed), 60)
            return (f"{glyph} {phase}… ({mins}m {secs}s · ↓ {tokens/1000:.1f}k "
                    f"tokens · DEMO) — q to quit")
        except Exception:  # noqa: BLE001
            return "ov demo live — q to quit"
    return _render


async def _drive(mux: Any, app: Any, speed: float,
                 started: Callable[[], float],
                 clock: Callable[[], float],
                 script: Optional[Sequence[Any]] = None) -> None:
    """Feed the script in real time, then idle until the operator quits.

    Sleeps against the SCHEDULE rather than accumulating per-step delays: a
    slow frame would otherwise push every later line further out, and the
    rhythm — the thing being demonstrated — would decay over the run.
    """
    import asyncio
    for at, line in (script if script is not None else _LIVE_SCRIPT):
        target = started() + (at / speed)
        while True:
            gap = target - clock()
            if gap <= 0:
                break
            await asyncio.sleep(min(gap, 0.05))
        try:
            # A beat is either a LINE or an ACTION. Actions exist because
            # some surfaces are live state rather than text — the agent
            # roster measures elapsed against the clock, so it has to be
            # driven on the beat, not populated during composition. An
            # action may return a line to push, or nothing at all when its
            # only effect is the state a mounted surface is already drawing.
            if callable(line):
                produced = line()
                app.invalidate()          # the state it changed is drawn
                if not produced:
                    continue              # its only effect was that state
                line = produced
            mux.push_raw(line)
            app.invalidate()
        except Exception:  # noqa: BLE001
            logger.debug("[OvDemo] live push degraded", exc_info=True)
    # The toolbar keeps pulsing after the script ends. Exiting on the last
    # line would deny the operator the chance to look at the finished deck,
    # which is most of what they came to see.
    while True:
        await asyncio.sleep(0.2)
        try:
            app.invalidate()
        except Exception:  # noqa: BLE001
            return



def _live_exit_bindings() -> Any:
    """Keys that end the demo. NEVER raises.

    The toolbar promised "q to quit" and q did NOT quit: the cockpit owns a
    text input, so the keystroke went into the prompt buffer. A surface that
    advertises an exit it does not honour is worse than one that advertises
    nothing — the operator's next move is SIGKILL.

    `q` only fires on an EMPTY buffer, so typing a word containing q still
    works; Ctrl-C and Ctrl-D always fire, because an escape hatch that can be
    disabled by the state of a text field is not an escape hatch.
    """
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.filters import Condition

    kb = KeyBindings()

    @Condition
    def _buffer_empty() -> bool:
        try:
            from prompt_toolkit.application.current import get_app
            return not get_app().current_buffer.text.strip()
        except Exception:  # noqa: BLE001
            return True

    def _quit(event) -> None:  # noqa: ANN001
        event.app.exit()

    kb.add("q", filter=_buffer_empty)(_quit)
    kb.add("escape", filter=_buffer_empty, eager=True)(_quit)
    kb.add("c-c")(_quit)
    kb.add("c-d")(_quit)
    return kb

def scene_live(console: Any, argv: Sequence[str] = ()) -> int:
    """The cockpit, running, driven by synthetic events.

    Boots the REAL `build_bipartite_application` — the same Application `ov`
    attaches to, with the region container mounted — and pushes timed events
    through `push_raw`, the same seam the daemon bridge uses. Nothing here
    renders anything itself.
    """
    import asyncio
    import sys

    # A real interactive TTY is REQUIRED, and saying so beats degrading
    # silently. prompt_toolkit needs a terminal to take over; without one it
    # either raises or draws nothing, and a demo that appears to do nothing is
    # indistinguishable from a broken one.
    if not (sys.__stdin__ is not None and sys.__stdin__.isatty()
            and sys.__stdout__ is not None and sys.__stdout__.isatty()):
        _say(console, "  ov demo live needs an interactive terminal.")
        _say(console, "  (piped or captured output cannot be taken over —")
        _say(console, "   run it directly in your terminal, not through a pipe)")
        return 64

    try:
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout, build_bipartite_application,
        )
    except Exception as exc:  # noqa: BLE001
        _say(console, f"  cockpit unavailable: {exc}")
        return 1

    clock = __import__("time").monotonic
    start = [clock()]
    speed = live_speed(argv)

    mux = BipartiteLayout()
    app = build_bipartite_application(
        mux,
        on_accept=lambda text: None,          # input is inert in a demo
        toolbar=_live_toolbar(lambda: start[0], clock),
        extra_key_bindings=_live_exit_bindings(),
        # The agent view, from `serpent_flow`'s own in-process provider. The
        # scene already drives the roster; without the mount it would fill a
        # surface nobody is drawing, which is the exact shape of the bug this
        # scene exists to make visible. Reused rather than reimplemented, so
        # the demo and the daemon cockpit render it identically.
        agent_rows=_agent_view_rows(),
    )
    try:
        mux.set_invalidate(app.invalidate)
    except Exception:  # noqa: BLE001
        pass

    script = compose_live_script()

    async def _main() -> None:
        start[0] = clock()
        driver = asyncio.ensure_future(
            _drive(mux, app, speed, lambda: start[0], clock, script))
        try:
            await app.run_async()
        finally:
            # The driver holds a reference to the Application; leaving it
            # running after the app exits keeps invalidating a dead surface
            # and the process never returns.
            driver.cancel()
            try:
                await driver
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        _say(console, f"  live scene ended: {type(exc).__name__}: {exc}")
        return 1
    return 0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

#: scene name -> handler. A table, not an if-chain, so `ov demo scenes` is
#: derived from what actually exists and cannot drift from it.
_SCENES: "dict[str, Callable[[Any, Sequence[str]], int]]" = {
    "board": scene_board,
    "surfaces": scene_surfaces,
    "transcript": scene_transcript,
    # NOT in the default all-scenes run: it takes over the screen and
    # waits for a keypress, so it must be asked for by name.
    "live": scene_live,
}


def demo_scenes() -> Sequence[str]:
    return tuple(sorted(_SCENES))


def run_demo(console: Any, argv: Optional[Sequence[str]] = None) -> int:
    """`ov demo [scene] [--limit=N]`. Returns a process exit code."""
    args = list(argv or ())
    positional = [a for a in args if not a.startswith("-")]

    if any(a in ("-h", "--help", "help") for a in args):
        _say(console, DEMO_HELP)
        return 0
    if positional and positional[0] == "scenes":
        for name in demo_scenes():
            _say(console, f"  {name}")
        return 0

    if not positional:
        rc = 0
        for name in (n for n in demo_scenes() if n != "live"):
            rc |= _SCENES[name](console, args)
            _say(console)
        return rc

    scene = positional[0]
    handler = _SCENES.get(scene)
    if handler is None:
        # Refuse, never silently ignore — the same discipline `ov doctor` uses
        # for unknown flags (EX_USAGE).
        near = [n for n in demo_scenes() if n.startswith(scene[:3])]
        hint = f" — did you mean {near[0]!r}?" if near else ""
        _say(console, f"unknown scene {scene!r}{hint}"
                      f" (known: {', '.join(demo_scenes())})")
        return 64
    return handler(console, args)
