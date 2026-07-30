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

Entering PINS the page
----------------------
It does now, and that is a change from how this shipped. `CanvasViewport`
originally derived `following` from `_offset <= 0`, so "pinned to the tail"
and "showing the newest line" were one state with one variable and there was
no honest way to stop following without also moving the view — which would
have thrown away the line the operator pressed the key while reading. This
module documented that limitation rather than faking a pin.

The viewport now carries a real `paused` flag, so entering freezes the page
WITHOUT moving it: arriving output is held, `new_since_paused` counts what
landed, and the sentence the operator was reading stays where it was. That
is CC's stated property — "scrolling up pauses auto-follow so new output
doesn't pull you back to the bottom" — with the explicit half now available
as well as the scroll-implied one.

Leaving returns to the tail AND resumes, which works and matters: the
organism keeps running while the operator reads, and an exit that left the
view in history would hand back a cockpit that looks live while showing
minutes-old output.

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
#: Where the claim walk is standing, in RING coordinates.
#:
#: Without it, each press re-derives its starting point from the newest
#: VISIBLE line — and after landing on a claim that line is below the claim,
#: so searching backward finds the same one again and `C` sticks. Every
#: `n`/`N` an operator has used carries a cursor for exactly this reason.
#:
#: None means "not walking": the next press starts from where the eye is.
_CLAIM_CURSOR: List[Optional[int]] = [None]
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
    """Enter the viewer and PIN the page. NEVER raises; True if it changed.

    Deliberately does not MOVE the view — entering where the operator was
    reading is the whole contract, and a viewer that jumped on entry would
    discard the line they pressed the key while looking at. It does now
    freeze it: `CanvasViewport.pause` holds arriving output without touching
    the offset, which is the distinction the derived `following` could not
    express.
    """
    with _LOCK:
        changed = not _ACTIVE[0]
        _ACTIVE[0] = True
        _CLAIM_CURSOR[0] = None
    if changed:
        _pause_viewport()
        _repaint()
    return changed


def _pause_viewport() -> None:
    """Hold auto-follow without moving the view. NEVER raises."""
    try:
        vp = _viewport()
        if vp is not None and hasattr(vp, "pause"):
            vp.pause()
    except Exception:  # noqa: BLE001
        logger.debug("[TranscriptMode] pause degraded", exc_info=True)


def exit_transcript_mode() -> bool:
    """Leave the viewer, return to the tail and resume following.

    Returning to the tail is not optional. The organism keeps working while
    the operator reads; an exit that left the view in history would hand
    back a cockpit that looks live and is showing minutes-old output.
    """
    with _LOCK:
        changed = bool(_ACTIVE[0])
        _ACTIVE[0] = False
        _CLAIM_CURSOR[0] = None
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
        _CLAIM_CURSOR[0] = None


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


def _all_lines() -> list:
    """Every RETAINED line, oldest first. NEVER raises.

    The full ring, not the rendered window — and that distinction is the
    whole feature. Classifying only what is drawn finds claims that are
    already on screen, so `c` would move nowhere and look broken. The point
    of walking claims is reaching the ones twenty thousand lines back.
    """
    try:
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            get_active_canvas,
        )
        canvas = get_active_canvas()
        if canvas is None:
            return []
        return list(canvas._buffer.snapshot() or ())
    except Exception:  # noqa: BLE001
        return []


def _visible_index(total: int, budget: int, offset: int) -> int:
    """Buffer index of the NEWEST visible line. NEVER raises.

    The one place the two coordinate systems meet: the viewport counts back
    from the tail, the ring counts forward from the oldest. Everything else
    stays in ring coordinates so there is only ever this one conversion to
    get wrong.
    """
    try:
        return max(0, int(total) - max(0, int(offset)) - 1)
    except Exception:  # noqa: BLE001
        return 0


def _seek_to_index(vp: Any, index: int, total: int, budget: int) -> None:
    """Scroll so buffer line ``index`` is on screen. NEVER raises.

    Placed a third of a screen down rather than at an edge: a claim pinned to
    the top row has no context above it, and one pinned to the bottom is
    about to be pushed off by the next arriving line.

    Expressed as a RELATIVE scroll because the viewport owns its clamping —
    an absolute offset computed here would be stale against a ring that drops
    lines as it rotates.
    """
    try:
        if vp is None or total <= 0:
            return
        lead = max(1, int(budget) // 3)
        end = min(int(total), max(1, int(index) + 1 + lead))
        want = max(0, int(total) - end)
        delta = int(getattr(vp, "offset", 0) or 0) - want
        # `scroll` takes NEGATIVE for older. `want > offset` means going
        # BACK, so the difference is negated exactly once, here — the same
        # sign trap `CanvasViewport.page` records having been got backwards.
        if delta:
            vp.scroll(delta, total=total, budget=budget)
    except Exception:  # noqa: BLE001
        pass


def _say_no_claims(summary: str) -> None:
    """Tell the operator the search found nothing, and what IS there.

    A jump key that silently does nothing is indistinguishable from a broken
    one — the same reason an unknown `/expand` ref lists what is available.
    """
    try:
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            get_active_canvas,
        )
        canvas = get_active_canvas()
        if canvas is None:
            return
        # `push_raw`, not `emit` — see `transcript_hatches._flash_clear_hint`.
        canvas.push_raw(
            f"  [dim]no unobserved claims · {summary}[/dim]" if summary else
            "  [dim]no unobserved claims retained — everything on record "
            "was observed or derived[/dim]")
    except Exception:  # noqa: BLE001
        pass


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

        # --- beyond CC: jump by EPISTEMIC STATE -------------------------
        #
        # CC's viewer jumps between search matches and prompts. It cannot
        # jump between claims, because nothing in it records which sentences
        # were observed and which were asserted. O+V marks every one at the
        # `_op_line` chokepoint and has never navigated by it.
        #
        # Pressing `c` through a session asks the question no other tool can
        # answer: show me every place this thing asserted something it did
        # not observe.
        def _claim(direction: int) -> Any:
            def _handler(event: Any) -> None:
                try:
                    from backend.core.ouroboros.battle_test.epistemic_filter import (  # noqa: E501
                        epistemic_filter_enabled, next_claim_row, summarise,
                    )
                    if not epistemic_filter_enabled():
                        return
                    lines = _all_lines()
                    if not lines:
                        return
                    vp = _viewport()
                    total, budget = _metrics()
                    with _LOCK:
                        cursor = _CLAIM_CURSOR[0]
                    here = cursor if cursor is not None else _visible_index(
                        total or len(lines), budget,
                        int(getattr(vp, "offset", 0) or 0),
                    )
                    target = next_claim_row(lines, here, direction)
                    if target is None:
                        _say_no_claims(summarise(lines))
                        return
                    with _LOCK:
                        _CLAIM_CURSOR[0] = target
                    _seek_to_index(vp, target, total or len(lines), budget)
                    _repaint()
                except Exception:  # noqa: BLE001
                    logger.debug("[TranscriptMode] claim jump degraded",
                                 exc_info=True)
            return _handler

        for action, keys, direction, desc in (
            ("transcript:nextClaim", ("c",), +1,
             "jump to the next unobserved claim"),
            ("transcript:prevClaim", ("C",), -1,
             "jump to the previous unobserved claim"),
        ):
            bound += bind_action(
                kb, action, keys, _claim(direction),
                context="Transcript", filter=inside, description=desc,
            )

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
