"""The transcript viewer as a MODE — Claude Code's `Ctrl+O`.

What was already here, and what was missing
-------------------------------------------
`transcript_hatches` already binds most of CC's viewer keys — `/` search,
`n`/`N`, `{`/`}`, `[` to native scrollback, `v` to `$EDITOR`. What it did not
have is the thing that makes them a VIEWER rather than a set of shortcuts:
a state you are in.

It stood in a mode's place with a proxy — "printable hatch keys are live
while the operator is scrolled back" — and that proxy is load-bearing,
because `[`, `v`, `{` and `}` are characters someone might genuinely type.
It is also silent. Nothing tells the operator the keys changed meaning, the
doorway (PgUp) is not the doorway CC teaches, and `j`/`k`/`g`/`G`/`Space`
were never bound at all because at the live tail they would type as
themselves.

An explicit mode answers all of that at once: inside it every key is
unambiguous, so the whole `less`-style table becomes bindable, and there is
somewhere honest to put `?`.

What this mode does NOT do
--------------------------
It does not pause auto-follow on entry, and that is a property of the
viewport rather than an omission here. `CanvasViewport.following` is
DERIVED — `self._offset <= 0` — so "pinned to the tail" and "showing the
newest line" are one state with one variable. There is no honest way to
stop following without also moving the view, and moving it would throw away
the line the operator pressed the key while reading, which is the only line
they certainly care about.

So entering at the live tail gives a keyboard mode, not a frozen page: new
output still arrives and the view still follows until the operator scrolls.
One press of `k` pauses it, because any non-zero offset is what "paused"
MEANS here. CC does better — "scrolling up pauses auto-follow so new output
doesn't pull you back to the bottom" is a real independent flag there — and
matching it needs a `paused` field on the viewport plus an audit of every
consumer of `following` (the status line, the tail hint, the emit path).
That is its own change; claiming it in this one would be a pin that does
not pin.

Leaving DOES return to the tail, which works and matters: the organism keeps
running while the operator reads, and an exit that left the view in history
would hand back a cockpit that looks live and is showing minutes-old output.

Scrolled-back is still a way IN (unchanged, so nobody's habit breaks) but no
longer the only one, and the mode is the way the operator is TOLD they are
in a viewer.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.TranscriptMode")

__all__ = [
    "TRANSCRIPT_MODE_SCHEMA_VERSION",
    "enter_transcript_mode",
    "exit_transcript_mode",
    "install_transcript_mode_bindings",
    "is_transcript_mode",
    "shortcut_panel",
    "toggle_transcript_mode",
    "transcript_surface_active",
]

TRANSCRIPT_MODE_SCHEMA_VERSION: str = "transcript_mode.1"

#: CC's own viewer table, as (keys, what it does). Declared ONCE and read
#: back by both the binder and `?`, so the panel cannot advertise a key
#: nothing binds — the defect `roster_hint` already exists to prevent.
_VIEWER_ACTIONS: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("transcript:lineUp", ("k", "up"), "scroll one line up"),
    ("transcript:lineDown", ("j", "down"), "scroll one line down"),
    ("transcript:halfUp", ("ctrl+u",), "scroll half a page up"),
    ("transcript:halfDown", ("ctrl+d",), "scroll half a page down"),
    ("transcript:pageUp", ("ctrl+b", "b"), "scroll a full page up"),
    ("transcript:pageDown", ("ctrl+f", "space"), "scroll a full page down"),
    ("transcript:top", ("g",), "jump to the top"),
    ("transcript:bottom", ("G",), "jump to the bottom"),
)

_ACTIVE = [False]
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def is_transcript_mode() -> bool:
    """Whether the viewer owns the keyboard right now. NEVER raises."""
    with _LOCK:
        return bool(_ACTIVE[0])


def transcript_surface_active() -> bool:
    """The viewer OR a scrolled-back canvas. NEVER raises.

    The filter the printable hatch keys use. Both are states in which `[`,
    `v`, `{` and `}` cannot be something the operator meant to type, and
    widening rather than replacing means the pre-existing scroll-back habit
    keeps working exactly as it did.
    """
    try:
        from backend.core.ouroboros.battle_test.transcript_hatches import (
            is_scrolled_back,
        )
        return is_transcript_mode() or is_scrolled_back()
    except Exception:  # noqa: BLE001
        return is_transcript_mode()


def enter_transcript_mode() -> bool:
    """Enter the viewer. NEVER raises; True if it changed.

    Deliberately does NOT move the view. Entering where the operator was
    reading is the whole contract — a viewer that jumped on entry would
    discard the line they pressed the key while looking at. See the module
    docstring for why it cannot pause auto-follow either.
    """
    with _LOCK:
        changed = not _ACTIVE[0]
        _ACTIVE[0] = True
    if changed:
        _repaint()
    return changed


def exit_transcript_mode() -> bool:
    """Leave the viewer, return to the tail and resume following.

    Returning to the tail is not optional. The organism keeps working while
    the operator reads; an exit that left the view in history would hand
    back a cockpit that looks live and is showing minutes-old output.
    """
    with _LOCK:
        changed = bool(_ACTIVE[0])
        _ACTIVE[0] = False
    if changed:
        _resume_viewport()
    return changed


def toggle_transcript_mode() -> bool:
    """Flip the mode; returns the new state. NEVER raises."""
    if is_transcript_mode():
        exit_transcript_mode()
        return False
    enter_transcript_mode()
    return True


def reset_transcript_mode_for_tests() -> None:
    with _LOCK:
        _ACTIVE[0] = False


# ---------------------------------------------------------------------------
# the viewport, reached the way the hatches already reach it
# ---------------------------------------------------------------------------


def _viewport() -> Optional[Any]:
    try:
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            get_active_canvas,
        )
        canvas = get_active_canvas()
        return getattr(canvas, "_viewport", None) if canvas else None
    except Exception:  # noqa: BLE001
        return None


def _metrics() -> Tuple[int, int]:
    """(total lines, visible budget) for the live canvas, or (0, 0)."""
    try:
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            get_active_canvas,
        )
        canvas = get_active_canvas()
        if canvas is None:
            return 0, 0
        total, budget = canvas.scroll_metrics()
        return int(total), int(budget)
    except Exception:  # noqa: BLE001
        return 0, 0


def _repaint() -> None:
    try:
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            get_active_canvas,
        )
        canvas = get_active_canvas()
        if canvas is not None:
            canvas._invalidate_now()
    except Exception:  # noqa: BLE001
        pass


def _resume_viewport() -> None:
    try:
        vp = _viewport()
        if vp is not None:
            vp.to_bottom()
        _repaint()
    except Exception:  # noqa: BLE001
        logger.debug("[TranscriptMode] resume degraded", exc_info=True)


# ---------------------------------------------------------------------------
# `?`
# ---------------------------------------------------------------------------


def shortcut_panel() -> List[str]:
    """The viewer's own key reference — CC's `?` inside the viewer.

    Composed from the declarations rather than written beside them, and from
    the EFFECTIVE keys rather than the defaults, so an operator who remapped
    a key in keybindings.json is shown the key they actually have. A panel
    that confidently lists someone else's bindings is worse than no panel.

    NEVER raises: the panel degrades to the declared defaults.
    """
    rows: List[str] = ["  transcript viewer"]
    try:
        from backend.core.ouroboros.battle_test.keymap import (
            effective_key_sequences,
        )
    except Exception:  # noqa: BLE001
        effective_key_sequences = None  # type: ignore[assignment]

    def _keys(action: str, defaults: Tuple[str, ...]) -> str:
        if effective_key_sequences is None:
            return " / ".join(defaults)
        try:
            seqs = effective_key_sequences(
                action, defaults, context="Transcript",
            )
            shown = [
                " ".join(_pretty(k) for k in seq) for seq in seqs
            ]
            return " / ".join(shown) if shown else " / ".join(defaults)
        except Exception:  # noqa: BLE001
            return " / ".join(defaults)

    try:
        for action, defaults, meaning in _VIEWER_ACTIONS:
            rows.append(f"    {_keys(action, defaults):<18} {meaning}")
        for action, defaults, meaning in (
            ("transcript:search", ("/",), "search"),
            ("transcript:nextMatch", ("n",), "next match"),
            ("transcript:prevMatch", ("N",), "previous match"),
            ("transcript:prevBlock", ("{",), "previous ⏺/❯ block"),
            ("transcript:nextBlock", ("}",), "next ⏺/❯ block"),
            ("transcript:dump", ("[",), "write to native scrollback"),
            ("transcript:editor", ("v",), "open in $EDITOR"),
            ("transcript:exit", ("q", "escape"), "leave the viewer"),
        ):
            rows.append(f"    {_keys(action, defaults):<18} {meaning}")
    except Exception:  # noqa: BLE001
        logger.debug("[TranscriptMode] panel degraded", exc_info=True)
    return rows


def _pretty(key: Any) -> str:
    """One key, as an operator would say it aloud.

    Handles BOTH spellings, because both reach here: `effective_key_sequences`
    returns prompt_toolkit's wire form (``"c-u"``, ``" "``) while a raw
    ``Keys`` member stringifies as ``"Keys.ControlU"``. The first cut only
    knew the second, so the panel printed ``c-u`` and rendered the space key
    as nothing at all — a shortcut reference with a blank in it.
    """
    try:
        name = str(getattr(key, "name", None) or key).rsplit(".", 1)[-1]
        if name == " " or name == "space":
            return "space"
        if name.startswith("c-") and len(name) > 2:
            return f"ctrl+{name[2:]}"
        if name.startswith("Control") and len(name) > 7:
            return f"ctrl+{name[7:].lower()}"
        return {
            "Up": "↑", "Down": "↓", "up": "↑", "down": "↓",
            "Escape": "esc", "escape": "esc",
        }.get(name, name)
    except Exception:  # noqa: BLE001
        return str(key)


# ---------------------------------------------------------------------------
# the bindings
# ---------------------------------------------------------------------------


def install_transcript_mode_bindings(
    kb: Any,
    notify: Optional[Any] = None,
) -> bool:
    """Mount Ctrl+O and the viewer's key table. NEVER raises.

    Returns True when ≥1 key bound. ``notify`` renders the `?` panel and the
    mode's own messages — a flash on the attach client, a console line on
    the daemon — because this module owns no surface and must not grow one.
    """
    try:
        from prompt_toolkit.filters import Condition

        from backend.core.ouroboros.battle_test.keymap import bind_action

        if kb is None:
            return False
        inside = Condition(is_transcript_mode)
        bound = 0

        def _say(lines: Any) -> None:
            if notify is None:
                return
            try:
                notify(lines)
            except Exception:  # noqa: BLE001
                pass

        # --- the doorway -------------------------------------------------
        def _toggle(event: Any) -> None:
            if toggle_transcript_mode():
                _say("transcript — ? for keys, q to leave")
            else:
                _say("live")

        bound += bind_action(
            kb, "transcript:toggle", ("ctrl+o",), _toggle, context="Global",
            description="enter/leave the transcript viewer",
        )

        def _exit(event: Any) -> None:
            exit_transcript_mode()
            _say("live")

        # `eager`, so `q` and `Escape` leave the viewer rather than waiting
        # to see whether they begin a longer sequence — an exit key that
        # hesitates reads as a cockpit that has hung.
        bound += bind_action(
            kb, "transcript:exit", ("q", "escape"), _exit,
            context="Transcript", filter=inside, eager=inside,
            description="leave the transcript viewer",
        )

        def _help(event: Any) -> None:
            _say(shortcut_panel())

        bound += bind_action(
            kb, "transcript:help", ("?",), _help,
            context="Transcript", filter=inside,
            description="show the viewer's keys",
        )

        # --- the less-style table ---------------------------------------
        def _mover(kind: str, direction: int) -> Any:
            def _handler(event: Any) -> None:
                try:
                    vp = _viewport()
                    if vp is None:
                        return
                    total, budget = _metrics()
                    if kind == "line":
                        vp.scroll(direction, total=total, budget=budget)
                    elif kind == "half":
                        vp.scroll(
                            direction * max(1, budget // 2),
                            total=total, budget=budget,
                        )
                    elif kind == "page":
                        # SIGN FLIP. `CanvasViewport.page` takes +1 for PgUp
                        # (older) while `scroll` takes NEGATIVE for older —
                        # the two disagree, and its own docstring records the
                        # first cut of the PgUp binding getting this wrong and
                        # reading as a dead key. `direction` is negative-for-
                        # older here to match `scroll`, so it inverts once,
                        # here, rather than each caller remembering.
                        vp.page(-direction, total=total, budget=budget)
                    elif kind == "top":
                        vp.to_top(total=total, budget=budget)
                    else:
                        vp.to_bottom()
                    _repaint()
                except Exception:  # noqa: BLE001
                    logger.debug("[TranscriptMode] move degraded",
                                 exc_info=True)
            return _handler

        _KINDS = {
            "transcript:lineUp": ("line", -1),
            "transcript:lineDown": ("line", +1),
            "transcript:halfUp": ("half", -1),
            "transcript:halfDown": ("half", +1),
            "transcript:pageUp": ("page", -1),
            "transcript:pageDown": ("page", +1),
            "transcript:top": ("top", -1),
            "transcript:bottom": ("bottom", +1),
        }
        for action, defaults, _meaning in _VIEWER_ACTIONS:
            kind, direction = _KINDS[action]
            bound += bind_action(
                kb, action, defaults, _mover(kind, direction),
                context="Transcript", filter=inside,
                description=_meaning,
            )
        return bound > 0
    except Exception:  # noqa: BLE001
        logger.debug("[TranscriptMode] bindings unavailable", exc_info=True)
        return False
