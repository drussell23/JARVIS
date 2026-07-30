"""Who owns `Escape` right now — decided per keystroke, not at bind time.

Two bindings want the same key and both are right:

    Escape        dismiss the overlay covering the cockpit
    Escape Escape open the rewind menu

`prompt_toolkit` resolves that ambiguity by BUFFERING: on the first `Escape` it
waits to see whether a second arrives. `eager=True` opts out of the wait. So the
key is over-subscribed in a way no static flag can settle:

    eager=False   the panic overlay needs a timeout or a second key to close —
                  the overlay SAYS "esc dismisses" and appears not to listen
    eager=True    the `esc esc` sequence can never complete, because the first
                  Escape always fires — rewind becomes unreachable

Both are the same mistake: deciding at BIND time a question that only has an
answer at KEYSTROKE time. Whether `Escape` means "close this" depends entirely on
whether there is something to close.

Contextual eagerness
====================
`eager` accepts a FILTER, not only a bool. That is the whole fix, and it is
`prompt_toolkit`'s own mechanism rather than a way around it:

    eager = Condition(overlay_active)

With an overlay up, `Escape` is eager and closes it on the first press. With the
cockpit clear the condition is False, the binding is inactive, and the input
processor buffers `esc esc` exactly as it always did — the sequence works because
nothing is competing for the prefix, not because a timer was tuned. No custom
timeout loop, no second input processor, no polling.

Overlays REGISTER; the arbiter PULLS
====================================
The condition could have asked `ui._panic` directly, which is what the existing
`app:dismissPanic` filter does. That is right for one overlay and wrong the
moment there are three: the Iron Gate prompt and the diff preview are equally
dismissable, and a hardcoded list of them here would need editing every time the
cockpit grows a surface — `/narrate`'s producer list, again, in the one place
where being wrong means a key silently stops working.

So an overlay declares itself, with a Z so the ARBITER can answer "which one is
on top" rather than each overlay guessing. Adding an overlay needs no edit here
and none to the keymap.

Fail-open, deliberately
=======================
An `is_active` that raises is treated as NOT active. The consequence of guessing
wrong in that direction is a rewind menu opening when an overlay was up — a
visible, recoverable surprise. Guessing the other way makes `Escape` eager
forever, which silently deletes `esc esc` for the rest of the session and looks
exactly like the bug this module exists to end. When the state cannot be read,
the sequence keeps working.

NEVER raises. A keybinding that can break the REPL it is bound in is worse than
an unbound key.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.OverlayArbiter")

OVERLAY_ARBITER_SCHEMA_VERSION = "overlay_arbiter.v1"

#: Z-order constants for the overlays that exist today. Named rather than
#: numeric at the call sites, and ORDERED by how much the operator needs to deal
#: with the thing before anything else: a crash outranks a decision, which
#: outranks a preview they opened themselves.
Z_DIFF_PREVIEW = 100
Z_IRON_GATE = 200
Z_PANIC = 300


@dataclass(frozen=True)
class Overlay:
    """One dismissable surface, and how to ask about it.

    ``is_active`` is a CALLABLE rather than a flag because overlay state lives
    where the overlay lives — `AttachUI._panic`, a pending-gate table, a diff
    archive cursor. Copying it in here would mean a second source of truth that
    goes stale between frames.
    """

    name: str
    z: int
    is_active: Callable[[], bool]
    dismiss: Callable[[], None]


_LOCK = threading.RLock()
_OVERLAYS: Dict[str, Overlay] = {}


def register_overlay(
    name: str,
    *,
    z: int,
    is_active: Callable[[], bool],
    dismiss: Callable[[], None],
) -> bool:
    """Declare a dismissable overlay. Idempotent by name. NEVER raises.

    Re-registering a name REPLACES it, so a surface rebuilt on reconnect does not
    leave a stale predicate behind that reports an overlay nobody can see — which
    would hold `Escape` eager forever, the exact failure this module prevents.
    """
    try:
        if not name or not callable(is_active) or not callable(dismiss):
            return False
        with _LOCK:
            _OVERLAYS[str(name)] = Overlay(
                name=str(name), z=int(z), is_active=is_active, dismiss=dismiss,
            )
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[OverlayArbiter] register degraded: %s", name,
                     exc_info=True)
        return False


def unregister_overlay(name: str) -> bool:
    try:
        with _LOCK:
            return _OVERLAYS.pop(str(name), None) is not None
    except Exception:  # noqa: BLE001
        return False


def reset_for_tests() -> None:
    with _LOCK:
        _OVERLAYS.clear()


def _is_active(overlay: Overlay) -> bool:
    """Ask one overlay whether it is up. NEVER raises — see fail-open, above."""
    try:
        return bool(overlay.is_active())
    except Exception:  # noqa: BLE001
        logger.debug("[OverlayArbiter] %s could not report state",
                     overlay.name, exc_info=True)
        return False


def active_overlays() -> List[Overlay]:
    """Every overlay currently up, TOPMOST FIRST.

    Ties broken by name so the order is deterministic: two overlays at one Z
    would otherwise be dismissed in dict order, and "which one did Escape
    close" must not depend on registration sequence.
    """
    with _LOCK:
        snapshot = list(_OVERLAYS.values())
    return sorted(
        (o for o in snapshot if _is_active(o)),
        key=lambda o: (-o.z, o.name),
    )


def overlay_active() -> bool:
    """Is anything dismissable on screen? The filter the whole design turns on."""
    return bool(active_overlays())


def top_overlay() -> Optional[Overlay]:
    overlays = active_overlays()
    return overlays[0] if overlays else None


def dismiss_top() -> Optional[str]:
    """Close the topmost overlay. Returns its name, or None. NEVER raises.

    Exactly ONE per press, never a cascade: with a panic over a gate over a
    preview, an operator pressing Escape means "close the thing I am looking at",
    and clearing all three would discard two decisions they never saw. The next
    press takes the next one.

    A `dismiss` that raises reports None rather than claiming success — the
    overlay is still up, so saying otherwise would make the key look answered
    when the screen has not changed.
    """
    overlay = top_overlay()
    if overlay is None:
        return None
    try:
        overlay.dismiss()
        return overlay.name
    except Exception:  # noqa: BLE001
        logger.debug("[OverlayArbiter] %s refused to dismiss", overlay.name,
                     exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The binding
# ---------------------------------------------------------------------------


def install_escape_arbiter(
    kb: Any,
    *,
    rewind: Optional[Callable[[Any], None]] = None,
    context: str = "Chat",
) -> bool:
    """Bind contextual `Escape` and the `Esc-Esc` fallback. NEVER raises.

    Both go through `keymap.bind_action`, so each stays remappable in
    `keybindings.json` and shows up in `/keys` — the paradigm established when the
    panic overlay's advertised key was first made real. Nothing here reimplements
    a binding.

    The two filters are COMPLEMENTS on purpose. `Escape` is active and eager only
    while an overlay is up; the sequence is active only while none is. They can
    never both be live for the same keystroke, so the input processor is never
    asked to choose between a prefix and a completion — the ambiguity is removed
    rather than arbitrated.
    """
    try:
        from prompt_toolkit.filters import Condition

        from backend.core.ouroboros.battle_test.keymap import bind_action

        _showing = Condition(overlay_active)
        _clear = Condition(lambda: not overlay_active())

        def _dismiss(_event: Any) -> None:
            dismiss_top()

        bound = bind_action(
            kb, "app:dismissOverlay", ("escape",), _dismiss,
            context=context,
            filter=_showing,
            # The fix. A Filter, evaluated per keystroke — so `Escape` skips the
            # sequence wait only when there is something for it to close.
            eager=_showing,
            description="dismiss the overlay on top (panic, gate, diff)",
        )

        if rewind is not None:
            from backend.core.ouroboros.battle_test.rewind_menu import (
                REWIND_DEFAULT_KEYS,
            )
            bound = bind_action(
                kb, "app:rewind", REWIND_DEFAULT_KEYS, rewind,
                context=context,
                # Inactive while an overlay is up, so the eager Escape above has
                # no competing prefix to buffer against.
                filter=_clear,
                description="open the rewind menu (Esc-Esc)",
            ) or bound
        return bool(bound)
    except Exception:  # noqa: BLE001
        logger.debug("[OverlayArbiter] escape arbiter unavailable",
                     exc_info=True)
        return False


__all__ = [
    "OVERLAY_ARBITER_SCHEMA_VERSION",
    "Overlay",
    "Z_DIFF_PREVIEW",
    "Z_IRON_GATE",
    "Z_PANIC",
    "active_overlays",
    "dismiss_top",
    "install_escape_arbiter",
    "overlay_active",
    "register_overlay",
    "reset_for_tests",
    "top_overlay",
    "unregister_overlay",
]
