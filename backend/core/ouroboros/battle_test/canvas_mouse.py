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


#: A URL worth offering to open. HTTP(S) ONLY, and that restriction is the
#: security boundary rather than a convenience: the transcript carries
#: MODEL-AUTHORED text, so this is the one surface that takes something the
#: organism wrote and hands it to the operating system. `file://` would open
#: arbitrary local paths, and the `javascript:` / `data:` family is a
#: browser-side execution vector. Neither is worth a convenience.
_URL_RE = re.compile(r"\bhttps?://[^\s<>\"'`)\]]+")

#: A path-shaped token. Deliberately loose, because the containment check —
#: not the pattern — is what makes opening safe. A pattern strict enough to
#: be a security control would also miss half the paths the transcript
#: prints; a permissive pattern plus "must resolve INSIDE the repo and must
#: already exist" is both safer and more useful.
_PATH_RE = re.compile(r"[\w./\-]*[\w\-]+\.[A-Za-z][\w]{0,9}\b")


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


# ---------------------------------------------------------------------------
# Cmd/Ctrl+click — the one surface that hands model text to the OS
# ---------------------------------------------------------------------------


def _repo_root() -> Any:
    from pathlib import Path
    return Path(__file__).resolve().parents[4]


def target_in_line(line: Any, *, root: Any = None) -> Optional[tuple]:
    """``("url"|"path", value)`` a line offers to open, or None. NEVER raises.

    Containment, not pattern-matching, is what makes this safe. The
    transcript is MODEL-AUTHORED, so a path is offered only when it resolves
    INSIDE the repository and already EXISTS on disk — which rules out
    `/etc/passwd`, `~/.ssh/id_rsa` and `../../` traversal without needing a
    regex clever enough to enumerate them. A file that does not exist is not
    openable anyway, so the check costs nothing an operator would miss.

    URLs are restricted to http(s) at the pattern, because there the scheme
    IS the capability: `file://` reaches the local disk and `javascript:` is
    execution.
    """
    try:
        from pathlib import Path

        text = str(line or "")
        try:
            from backend.core.ouroboros.battle_test.append_only import (
                strip_ansi,
            )
            text = strip_ansi(text)
        except Exception:  # noqa: BLE001
            pass

        found = _URL_RE.search(text)
        if found:
            # Trailing punctuation belongs to the sentence, not the URL.
            return ("url", found.group(0).rstrip(".,;:!?"))

        base = Path(root) if root is not None else _repo_root()
        try:
            base = base.resolve()
        except Exception:  # noqa: BLE001
            return None
        for match in _PATH_RE.finditer(text):
            raw = match.group(0).strip()
            if not raw or raw.startswith("-"):
                continue
            try:
                candidate = (base / raw).resolve()
            except Exception:  # noqa: BLE001
                continue
            # `is_relative_to` on the RESOLVED path, so `..` segments and
            # symlinks are both answered by the same check rather than by a
            # string prefix whose shape an attacker chooses. Available since
            # 3.9, which is this repo's floor.
            if candidate.is_relative_to(base) and candidate.is_file():
                return ("path", str(candidate))
        return None
    except Exception:  # noqa: BLE001
        return None


def open_target(kind: Any, value: Any) -> bool:
    """Hand one target to the platform opener. NEVER raises; True if launched.

    `subprocess` with an ARGV LIST and never a shell string: the value came
    from the transcript, and a shell would make every metacharacter in it
    executable. The opener is chosen from `sys.platform` rather than hardcoded
    so this is not macOS-only.

    Fire-and-forget: the operator asked to open something, not to wait for it,
    and a browser cold-start must not stall the render loop that dispatched
    the click.
    """
    try:
        import subprocess
        import sys

        target = str(value or "")
        if not target:
            return False
        if str(kind) == "url" and not target.startswith(("http://", "https://")):
            return False        # belt and braces — the pattern already forbids it
        if sys.platform == "darwin":
            argv = ["open", target]
        elif sys.platform.startswith("win"):
            argv = ["cmd", "/c", "start", "", target]
        else:
            argv = ["xdg-open", target]
        subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[CanvasMouse] open degraded", exc_info=True)
        return False


def _is_open_gesture(mouse_event: Any) -> bool:
    """Did the operator ask to OPEN rather than to expand? NEVER raises.

    Claude Code documents the awkward truth here: "the terminal mouse
    protocol has no way to encode the Cmd key, so Claude Code receives it as
    a plain click." So this asks for any modifier the protocol CAN carry —
    Control or Alt — and does not pretend Cmd is available. An operator on a
    terminal that sends neither still has `/expand` and the path in the line
    to copy; what they must never get is a plain click silently launching
    something.
    """
    try:
        from prompt_toolkit.mouse_events import MouseModifier

        mods = getattr(mouse_event, "modifiers", None) or frozenset()
        return bool(mods & {MouseModifier.CONTROL, MouseModifier.ALT})
    except Exception:  # noqa: BLE001
        return False


def install_canvas_mouse(
    control: Any,
    rows_fn: Callable[[], Sequence[Any]],
    submit: Callable[[str], Any],
    notify: Optional[Callable[[str], Any]] = None,
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
                kind = getattr(mouse_event, "event_type", None)
                point = getattr(mouse_event, "position", None)
                at = (getattr(point, "y", None), getattr(point, "x", None))

                # --- selection: press anchors, drag extends ---------------
                if kind is MouseEventType.MOUSE_DOWN:
                    _drag_start(at)
                    raise _Unhandled     # pt still gets its own default
                if kind is MouseEventType.MOUSE_MOVE:
                    if _drag_extend(at):
                        return None      # consumed: this IS the drag
                    raise _Unhandled

                # MOUSE_UP, not DOWN: a click is a press AND a release in the
                # same place, and acting on the press would fire while the
                # operator is still deciding — including at the start of a
                # drag they meant as a selection.
                if kind is not MouseEventType.MOUSE_UP:
                    raise _Unhandled

                # A release that ENDED A DRAG is a selection, not a click.
                # Checked before every other gesture: the press that began it
                # was over some line, and treating the release as a click
                # would expand whatever the drag happened to start on.
                copied = _drag_finish(at, rows_fn, notify)
                if copied:
                    return None
                row = getattr(getattr(mouse_event, "position", None), "y", None)
                rows = list(rows_fn() or ())
                # A MODIFIED click asks to open, and is checked FIRST because
                # a line can offer both — `⏺ Read(backend/x.py) · /expand t-3`
                # is a path and an expansion, and the modifier is the operator
                # saying which one they meant.
                if _is_open_gesture(mouse_event):
                    target = target_in_line(
                        rows[row] if isinstance(row, int)
                        and 0 <= row < len(rows) else "")
                    if target and open_target(*target):
                        return None
                    raise _Unhandled
                line = resolve_click(rows, row)
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


# ---------------------------------------------------------------------------
# drag-to-select
# ---------------------------------------------------------------------------


def _valid(at: Any) -> bool:
    return (isinstance(at, tuple) and len(at) == 2
            and isinstance(at[0], int) and isinstance(at[1], int))


def _drag_start(at: Any) -> None:
    """Anchor a possible selection. NEVER raises.

    Every press anchors, because a press cannot yet be distinguished from
    the start of a drag. An anchor with no movement stays `empty`, which is
    exactly what lets the release fall through to click-to-expand.
    """
    try:
        from backend.core.ouroboros.battle_test.canvas_selection import (
            Selection, selection_enabled, set_current_selection,
        )
        if not selection_enabled() or not _valid(at):
            return
        set_current_selection(Selection((at[0], at[1])))
    except Exception:  # noqa: BLE001
        pass


def _drag_extend(at: Any) -> bool:
    """Grow the selection under a held button. True when consumed."""
    try:
        from backend.core.ouroboros.battle_test.canvas_selection import (
            current_selection, set_current_selection,
        )
        sel = current_selection()
        if sel is None or not sel.active or not _valid(at):
            return False
        set_current_selection(sel.extend_to(at[0], at[1]))
        return True
    except Exception:  # noqa: BLE001
        return False


def _drag_finish(at: Any, rows_fn: Any, notify: Any) -> bool:
    """End a drag, copying what it covered. True when it WAS a drag.

    False for a press-and-release in one place, which is the whole reason
    the anchor is kept even for a plain click: the release has to be able to
    tell a selection from a click, and only the anchor knows.

    The selection is CLEARED once copied. A highlight that outlives the
    gesture is a cockpit asserting the operator still has something selected
    when they have moved on — and the next click would extend it.
    """
    try:
        from backend.core.ouroboros.battle_test.canvas_selection import (
            copy_on_release, current_selection, extract_text,
            set_current_selection,
        )
        sel = current_selection()
        if sel is None:
            return False
        if _valid(at):
            sel = sel.extend_to(at[0], at[1])
        if sel.empty:
            set_current_selection(None)
            return False           # a click, not a drag
        text = extract_text(list(rows_fn() or ()), sel)
        set_current_selection(None)
        if not text or not copy_on_release():
            return True            # it WAS a drag; there was nothing to copy
        from backend.core.ouroboros.battle_test.clipboard_write import (
            copy_text, describe_path,
        )
        path = copy_text(text)
        if notify is not None:
            lines = text.count("\n") + 1
            where = describe_path(path or "") if path else (
                "no clipboard tool available — nothing copied")
            try:
                notify(f"{where} · {lines} line{'s' if lines != 1 else ''}")
            except Exception:  # noqa: BLE001
                pass
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[CanvasMouse] drag finish degraded", exc_info=True)
        return False
