"""The completion menu and the gate, made remappable.

Both surfaces WORKED and neither could be rebound. `/keys` could not list
them, `keybindings.json` could not move them, and an operator who had
remapped everything else found these four keys immovable for no reason they
could see. Claude Code declares both as first-class contexts —
`autocomplete:accept|dismiss|previous|next` and `confirm:yes|no` — so this is
the last of its interactive-action catalog ov had no answer for.

Overriding, not declaring
-------------------------
Every other action in this cockpit was NEW: nothing was bound, so declaring
it was the whole job. These keys are already bound, inside prompt_toolkit's
own `basic`/`emacs` bindings, and that makes the work a different shape.

The lever is the FILTER. prompt_toolkit resolves a keystroke to the last
matching binding whose filter passes, so a binding registered after pt's and
gated on `has_completions` wins exactly while the menu is open and is absent
otherwise. Nothing is unbound, nothing is monkeypatched, and with the menu
closed `Tab` and the arrows behave precisely as they did.

Why the gate is not simply CC's dialog
--------------------------------------
CC's `Confirmation` context is a MODAL — while it is up, nothing else can
receive a key, so binding bare `y` and `n` there is free. ov's NOTIFY_APPLY
gate is a STRIP drawn over a live prompt the operator can still type into.
Binding `y` unconditionally would mean an operator writing "yes, rerun the
suite" accepted a patch on the first character.

So the confirm keys additionally require an EMPTY BUFFER. With anything
typed they are ordinary characters; on an empty prompt with a gate pending
they are the answer to it. That restriction is not in CC because CC does not
have this problem.

`Escape` is deliberately NOT bound to `confirm:no`. It already means
interrupt-or-dismiss and the overlay arbiter owns it; a third meaning that
appears only while a gate is up is how an operator learns to distrust the
key. `n` says no, and `/reject` always works.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger("Ouroboros.MenuBindings")

MENU_BINDINGS_SCHEMA_VERSION: str = "menu_bindings.1"

__all__ = [
    "MENU_BINDINGS_SCHEMA_VERSION",
    "gate_is_pending",
    "install_completion_actions",
    "install_confirm_actions",
    "menu_bindings_enabled",
]


def menu_bindings_enabled() -> bool:
    """``JARVIS_MENU_BINDINGS_ENABLED`` (default true). NEVER raises.

    Off, prompt_toolkit's own bindings are left entirely alone — which is
    what the surface did before, so the rollback is exact.
    """
    return os.environ.get(
        "JARVIS_MENU_BINDINGS_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def gate_is_pending() -> bool:
    """Is a NOTIFY_APPLY window open right now? NEVER raises.

    Read from `pending_apply.snapshot`, which returns None when idle and
    drops expired rows where the clock that set them lives — so this reader
    never decides whether a window has closed. A confirm key that answered a
    gate which had already auto-applied would be worse than one that did
    nothing.
    """
    try:
        from backend.core.ouroboros.battle_test.pending_apply import snapshot
        snap = snapshot()
        return bool(snap and snap.get("rows"))
    except Exception:  # noqa: BLE001
        return False


def _buffer_empty() -> bool:
    """NEVER raises. False when it cannot tell — the safe direction, because
    a confirm key must not fire on a prompt that might have text in it."""
    try:
        from prompt_toolkit.application.current import get_app_or_none
        app = get_app_or_none()
        if app is None or app.current_buffer is None:
            return False
        return not app.current_buffer.text.strip()
    except Exception:  # noqa: BLE001
        return False


def install_completion_actions(kb: Any) -> int:
    """Make the completion menu's four keys remappable. NEVER raises.

    Returns the number bound. Each is gated on `has_completions`, so with the
    menu closed prompt_toolkit's own handling is untouched — `Tab` still
    indents or completes, and the arrows still walk history.
    """
    if kb is None or not menu_bindings_enabled():
        return 0
    try:
        from prompt_toolkit.filters import has_completions

        from backend.core.ouroboros.battle_test.keymap import bind_action

        def _buf(event: Any) -> Any:
            return getattr(event, "current_buffer", None) or (
                event.app.current_buffer if getattr(event, "app", None) else None)

        def _accept(event: Any) -> None:
            try:
                buf = _buf(event)
                state = getattr(buf, "complete_state", None)
                current = getattr(state, "current_completion", None)
                if current is not None:
                    buf.apply_completion(current)
                else:
                    # Nothing HIGHLIGHTED yet — the menu is open but the
                    # operator has not walked it. Selecting the first entry is
                    # what every shell does and what makes one Tab enough.
                    buf.complete_next()
            except Exception:  # noqa: BLE001
                pass

        def _dismiss(event: Any) -> None:
            try:
                buf = _buf(event)
                if buf is not None:
                    buf.cancel_completion()
            except Exception:  # noqa: BLE001
                pass

        def _walk(step: int) -> Callable[[Any], None]:
            def _handler(event: Any) -> None:
                try:
                    buf = _buf(event)
                    if buf is None:
                        return
                    if step > 0:
                        buf.complete_next()
                    else:
                        buf.complete_previous()
                except Exception:  # noqa: BLE001
                    pass
            return _handler

        bound = 0
        for action, keys, handler, desc in (
            ("autocomplete:accept", ("tab",), _accept, "accept the suggestion"),
            ("autocomplete:dismiss", ("escape",), _dismiss, "close the menu"),
            ("autocomplete:next", ("down",), _walk(+1), "next suggestion"),
            ("autocomplete:previous", ("up",), _walk(-1), "previous suggestion"),
        ):
            bound += bind_action(
                kb, action, keys, handler, context="Autocomplete",
                filter=has_completions, description=desc,
            )
        return bound
    except Exception:  # noqa: BLE001
        logger.debug("[MenuBindings] completion actions unavailable",
                     exc_info=True)
        return 0


def install_confirm_actions(
    kb: Any,
    submit: Optional[Callable[[str], Any]] = None,
) -> int:
    """Answer a pending gate with one key. NEVER raises; returns the count.

    `y` accepts and `n` rejects, but ONLY on an empty prompt with a gate
    actually open — see the module docstring for why that guard does not
    exist in CC. `Enter` joins `y` for the same reason CC binds it there: on
    an empty prompt it submits nothing, so the only thing it can mean while a
    gate is up is "the obvious answer".

    Both route through `submit` — the same callable a typed `/accept` goes
    through — so the risk tier, the audit trail and the countdown clear
    exactly as they do for the verb. A key that bypassed the verb would be a
    second approval path, and this is the one surface where two paths that
    disagree is a governance problem rather than a UI one.
    """
    if kb is None or submit is None or not menu_bindings_enabled():
        return 0
    try:
        from prompt_toolkit.filters import Condition

        from backend.core.ouroboros.battle_test.keymap import bind_action

        @Condition
        def _answerable() -> bool:
            return gate_is_pending() and _buffer_empty()

        def _answer(verb: str) -> Callable[[Any], None]:
            def _handler(event: Any) -> None:
                try:
                    submit(verb)
                except Exception:  # noqa: BLE001
                    logger.debug("[MenuBindings] %s degraded", verb,
                                 exc_info=True)
            return _handler

        bound = 0
        bound += bind_action(
            kb, "confirm:yes", ("y", "enter"), _answer("/accept"),
            context="Confirmation", filter=_answerable,
            description="accept the pending apply (empty prompt only)",
        )
        bound += bind_action(
            kb, "confirm:no", ("n",), _answer("/reject"),
            context="Confirmation", filter=_answerable,
            description="reject the pending apply (empty prompt only)",
        )
        return bound
    except Exception:  # noqa: BLE001
        logger.debug("[MenuBindings] confirm actions unavailable",
                     exc_info=True)
        return 0
