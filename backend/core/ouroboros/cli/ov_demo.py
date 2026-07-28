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

#: A synthetic op. Deliberately a FAILING one: the deck's whole grammar —
#: `⎿` results, an agora thread reacting to a failure — only shows itself when
#: something goes wrong, and a demo of the happy path shows the least
#: interesting third of the surface.
_SCRIPT: List[Any] = [
    ("op", "⏺ Read(backend/core/ouroboros/governance/risk_tier_floor.py)"),
    ("res", "⎿ 847 lines"),
    ("op", "⏺ Update(risk_tier_floor.py)"),
    ("res", "⎿ +18 -3"),
    ("diff", "+    except RiskFloorConfigError:"),
    ("diff", "+        raise"),
    ("diff", "-    except Exception:  # noqa: BLE001"),
    ("op", "⏺ Validate(7759-86)"),
    ("res", "⎿ ✗ 3 failed · test_scoped_paths, test_sandbox_dir"),
]

#: The agora reacting to that failure, in the schema `moltbook_inline` expects.
_THREAD: List[dict] = [
    {"handle": "@the-pit", "op_id": "op-7759-86",
     "body": "three tests. you fixed the assert and broke the file."},
    {"handle": "@the-builder", "op_id": "op-7759-86",
     "body": "the containment check is right, the fixture is stale"},
    {"handle": "@cassandra", "op_id": "op-7759-86", "kind": "conflict",
     "body": "⚔ REVIEW disagrees: that except clause still swallows I2"},
]


def scene_transcript(console: Any, argv: Sequence[str] = ()) -> int:
    """The CC-style deck, rendered by the cockpit's OWN renderers."""
    _rule(console, "transcript")
    for kind, text in _SCRIPT:
        if kind == "op":
            _say(console, text)
        elif kind == "res":
            _say(console, f"  {text}")
        else:
            _say(console, f"     {text}")

    try:
        from backend.core.ouroboros.battle_test.moltbook_inline import (
            render_thread,
        )
        for line in render_thread(_THREAD, width=76):
            _say(console, line)
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

#: A scripted operation, as SECONDS-since-start paired with a deck line.
#: Timings are the point — the deck's rhythm is most of what the cockpit feels
#: like, and a burst of lines arriving at once looks nothing like an organism
#: working. Kept as data so the shape can be tuned without touching the driver.
_LIVE_SCRIPT = [
    (0.4, "⏺ Signal(test_failure) · risk_tier_floor.py"),
    (1.0, "  ⎿ 2 source loci · 1 test locus"),
    (2.0, "⏺ Read(governance/risk_tier_floor.py)"),
    (2.8, "  ⎿ 847 lines"),
    (3.4, "⏺ Search(\"except Exception\")"),
    (4.2, "  ⎿ 19 matches in 1 file"),
    (5.0, "🗣 the vision floor raises, and the caller swallows it"),
    (6.2, "⏺ Update(risk_tier_floor.py)"),
    (7.0, "  ⎿ +18 -3"),
    (7.4, "     + except RiskFloorConfigError:"),
    (7.7, "     +     raise"),
    (8.0, "     - except Exception:  # noqa: BLE001"),
    (9.2, "⏺ Validate(7759-86)"),
    (10.8, "  ⎿ ✗ 3 failed · test_scoped_paths, test_sandbox_dir"),
    (11.6, "  💬 @the-pit  three tests. you fixed the assert and broke the file."),
    (12.6, "     ▸ @the-builder  the containment check is right, the fixture is stale"),
    (13.8, "     ▸ ⚔ @cassandra  REVIEW disagrees: that clause still swallows I2"),
    (15.2, "⏺ Repair(L2) · iteration 1/5"),
    (16.8, "  ⎿ fixture rebuilt from the live seam"),
    (18.0, "⏺ Validate(7759-86)"),
    (19.4, "  ⎿ ✓ 47 passed"),
    (20.4, "⏺ Gate(7759-86) · NOTIFY_APPLY"),
    (21.2, "  ⎿ applied · verified · committed 90706b8"),
    (22.4, "⏺ Complete(7759-86) · 22.4s · $0.011"),
]

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
                 clock: Callable[[], float]) -> None:
    """Feed the script in real time, then idle until the operator quits.

    Sleeps against the SCHEDULE rather than accumulating per-step delays: a
    slow frame would otherwise push every later line further out, and the
    rhythm — the thing being demonstrated — would decay over the run.
    """
    import asyncio
    for at, line in _LIVE_SCRIPT:
        target = started() + (at / speed)
        while True:
            gap = target - clock()
            if gap <= 0:
                break
            await asyncio.sleep(min(gap, 0.05))
        try:
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
    )
    try:
        mux.set_invalidate(app.invalidate)
    except Exception:  # noqa: BLE001
        pass

    async def _main() -> None:
        start[0] = clock()
        driver = asyncio.ensure_future(
            _drive(mux, app, speed, lambda: start[0], clock))
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
