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
        from backend.core.ouroboros.battle_test.verb_description import (
            to_operator_voice,
        )
        _ensure_primed()
        verbs = list(list_verbs())
        if not verbs:
            _say(console, "  (no verbs primed — registry empty)")
            return
        for verb in verbs[:8]:
            desc = ""
            try:
                fn = _dispatcher_for(verb)
                desc = to_operator_voice(getattr(fn, "__doc__", ""), verb, 46)
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
# Router
# ---------------------------------------------------------------------------

#: scene name -> handler. A table, not an if-chain, so `ov demo scenes` is
#: derived from what actually exists and cannot drift from it.
_SCENES: "dict[str, Callable[[Any, Sequence[str]], int]]" = {
    "board": scene_board,
    "transcript": scene_transcript,
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
        for name in demo_scenes():
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
