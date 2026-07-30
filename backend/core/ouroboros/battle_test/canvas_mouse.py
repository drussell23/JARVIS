"""Clicking a transcript line that says it has more to show.

Claude Code: "Click a collapsed tool result to expand it and see the full
output. Click again to collapse. **Only messages that have more to show are
clickable.**"

`mouse_support` was already on — the wheel scrolls — but the cockpit had ZERO
`MouseEventType` handlers, so every click fell on the floor. That was the
largest single parity gap measured against CC's documented surface: 1 of 13.

The affordance is already on the screen
---------------------------------------
This needed no new addressing scheme and no line→ref index to keep in sync,
because the transcript already PRINTS the ref on any line that has more
behind it:

    ⎿ 41 lines parked · /expand t-3

That text exists so an operator can read the ref and type the verb. A click
is doing that for them. Which means "has more to show" is not a property this
module has to be told — it is a property it can SEE, and it is exactly the
same property CC uses to decide what is clickable.

The consequence worth stating: a producer that starts emitting a new ref
family becomes clickable for free, and one that stops printing refs becomes
un-clickable for free. Neither needs an edit here. An index would have had to
be updated by every producer, and the day one forgot, a line would look
clickable and do nothing — which is worse than not being clickable at all.

Rows are indexed against RENDERED output
----------------------------------------
`mouse_event.position.y` is relative to the control's own content, and that
content is the canvas Panel — border rows, padding and all. So the row is
resolved against the rendered ANSI lines rather than against the mux's
logical line list. That is exact by construction: row Y is whatever is on
row Y. Deriving it instead — logical lines, plus the anchor padding, plus the
panel's top border, minus the scroll offset — is four assumptions that must
all stay true, and a click landing one line off is the kind of defect an
operator reports as "it expanded the wrong thing".

Border rows carry no ref, so they are simply not clickable. The arithmetic
never has to be right because it is never done.

A click IS the verb
-------------------
Resolution ends by submitting `/expand <ref>` through the surface's own
`on_accept` — the same callable a typed line goes through. So the daemon
cockpit, the attach client and the demo each route a click exactly the way
they already route typing, with no second dispatch path to drift, and
`/expand`'s whole ref family (`t-` `d-` `o-` `n-` `p-` `q-` `b-`) works on
day one because none of it is reimplemented here.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, List, Optional, Sequence

logger = logging.getLogger("Ouroboros.CanvasMouse")

CANVAS_MOUSE_SCHEMA_VERSION: str = "canvas_mouse.1"

__all__ = [
    "CANVAS_MOUSE_SCHEMA_VERSION",
    "canvas_mouse_enabled",
    "clickable_rows",
    "install_canvas_mouse",
    "ref_at_row",
    "ref_in_line",
]

#: The explicit affordance: a line that TELLS the operator how to expand it.
#: Preferred over a bare ref because it is unambiguous — the line is offering
#: the action, not merely mentioning an artifact.
_EXPAND_RE = re.compile(r"/expand\s+([a-z]-\d+)")

#: A bare ref, for surfaces that print one without the verb (the moltbook
#: feed appends `n-4` alone). Second, so a line carrying both is resolved by
#: the offer rather than by whatever ref happens to appear first in it.
#:
#: The `\d` is deliberate and load-bearing: refs are `letter-digits`, and a
#: looser pattern matches ordinary prose — "a well-known b-tree" would become
#: a clickable line that expands nothing.
_BARE_REF_RE = re.compile(r"(?:^|[\s·|(\[])([a-z]-\d+)(?=$|[\s·|)\].,])")


def canvas_mouse_enabled() -> bool:
    """``JARVIS_CANVAS_MOUSE_ENABLED`` (default true). NEVER raises.

    Separate from `JARVIS_DISABLE_MOUSE`, which turns CAPTURE off entirely
    and costs the operator wheel scrolling too. This switch keeps the wheel
    and stands down only the click handling — the same split CC draws with
    `CLAUDE_CODE_DISABLE_MOUSE_CLICKS` versus `CLAUDE_CODE_DISABLE_MOUSE`.
    """
    return os.environ.get(
        "JARVIS_CANVAS_MOUSE_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def ref_in_line(line: Any) -> Optional[str]:
    """The ref a line OFFERS, or None. Pure. NEVER raises.

    ANSI is stripped through `append_only.strip_ansi` rather than a third
    regex in this file — the canvas is drawn as ANSI, and two strippers that
    disagree about an escape sequence would disagree about what a line says.
    """
    try:
        text = str(line or "")
        try:
            from backend.core.ouroboros.battle_test.append_only import (
                strip_ansi,
            )
            text = strip_ansi(text)
        except Exception:  # noqa: BLE001
            pass
        offered = _EXPAND_RE.search(text)
        if offered:
            return offered.group(1)
        bare = _BARE_REF_RE.search(text)
        return bare.group(1) if bare else None
    except Exception:  # noqa: BLE001
        return None


def ref_at_row(rows: Sequence[Any], row: Any) -> Optional[str]:
    """The ref on a rendered row, or None. Pure. NEVER raises.

    Out-of-range is None rather than an error: a click can land on the frame
    during a resize, between the render that sized it and the one that filled
    it, and a cockpit must not fault on a mis-timed click.
    """
    try:
        index = int(row)
        if index < 0 or index >= len(rows):
            return None
        return ref_in_line(rows[index])
    except (TypeError, ValueError):
        return None
    except Exception:  # NEVER let a click break a frame  # noqa: BLE001
        return None


def clickable_rows(rows: Sequence[Any]) -> List[int]:
    """Every row index that offers a ref. NEVER raises.

    Exposed for a future hover highlight — CC highlights the row under the
    cursor — and because it makes "which lines are clickable" answerable in a
    test without a terminal.
    """
    try:
        return [i for i, line in enumerate(rows) if ref_in_line(line)]
    except Exception:  # noqa: BLE001
        return []


def resolve_click(rows: Sequence[Any], row: Any) -> Optional[str]:
    """The line to SUBMIT for a click, or None. Pure. NEVER raises."""
    ref = ref_at_row(rows, row)
    return f"/expand {ref}" if ref else None


def install_canvas_mouse(
    control: Any,
    rows_fn: Callable[[], Sequence[Any]],
    submit: Callable[[str], Any],
) -> bool:
    """Give a control click-to-expand. NEVER raises; True when installed.

    Wraps rather than replaces the existing `mouse_handler`, and returns
    ``NotImplemented`` for everything it does not consume. That return value
    is load-bearing: prompt_toolkit reads it as "not handled" and applies its
    own default, which is what turns wheel events into scrolling. A handler
    that returned None for unhandled events would silently take the wheel
    away — trading the one mouse capability the cockpit already had for the
    one it was adding.
    """
    try:
        if control is None or not canvas_mouse_enabled():
            return False
        from prompt_toolkit.mouse_events import MouseEventType

        previous = getattr(control, "mouse_handler", None)

        def _handler(mouse_event: Any) -> Any:
            try:
                # MOUSE_UP, not DOWN: a click is a press AND a release in the
                # same place, and acting on the press would fire while the
                # operator is still deciding — including at the start of a
                # drag they meant as a selection.
                if getattr(mouse_event, "event_type", None) is not (
                    MouseEventType.MOUSE_UP
                ):
                    raise _Unhandled
                row = getattr(getattr(mouse_event, "position", None), "y", None)
                line = resolve_click(list(rows_fn() or ()), row)
                if not line:
                    raise _Unhandled
                submit(line)
                return None
            except _Unhandled:
                pass
            except Exception:  # noqa: BLE001 — a click must never break a frame
                logger.debug("[CanvasMouse] click degraded", exc_info=True)
            if callable(previous):
                try:
                    return previous(mouse_event)
                except Exception:  # noqa: BLE001
                    return NotImplemented
            return NotImplemented

        control.mouse_handler = _handler
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[CanvasMouse] install degraded", exc_info=True)
        return False


class _Unhandled(Exception):
    """Internal: this event is not ours. Never escapes the handler."""
