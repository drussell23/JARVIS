"""A keystroke that can tighten the gate, and can never loosen it.

Claude Code cycles permission modes with `Shift+Tab`, one of which is "bypass
permissions on". That direction is not offered here, and the asymmetry is the
whole design rather than a limitation of it.

`risk_tier_floor` already composes several floors — `JARVIS_MIN_RISK_TIER`,
`JARVIS_PARANOIA_MODE`, quiet hours, the VisionSensor floor — and resolves
them STRICTEST WINS. That composition is the safety property: no input can
make the organism more permissive than another input demanded. A keystroke
that bypassed it would not be a new mode; it would be a hole in the one rule
every other floor is built on.

So the session override is simply another input to that same composition. It
can raise the floor as high as `blocked` and it cannot lower it below what
the configuration already demands — `Shift+Tab` moves within the envelope the
operator's config allows, never outside it.

Why tightening is the useful direction anyway
----------------------------------------------
The moment an operator wants a keystroke here is when they are about to do
something they do not fully trust: a broad refactor, an unfamiliar repo, a
run they will not be watching. That is a request for MORE friction, and it is
exactly the request a config file cannot serve, because it arrives in the
middle of a session.

Wanting less friction is real too, and it is not urgent in the same way: it
is a decision about how this machine should behave, which is what
configuration is for and where it can be reviewed.

Mid-flight operations keep the rules they were gated under
-----------------------------------------------------------
The floor is read when a gate is EVALUATED. An operation already past its
gate is not re-judged, and that is deliberate: retroactively tightening
something already approved would strand work the operator explicitly allowed,
and retroactively loosening is the hole above wearing a different hat. The
change applies to what is gated next.

Session-scoped, deliberately
----------------------------
It resets when the cockpit detaches. A keystroke made about one screenful of
work must not silently become this machine's permanent policy — the same
reasoning that keeps the checklist toggle out of the env master. Persisting a
security posture is a config edit, and config edits are visible.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional

logger = logging.getLogger("Ouroboros.SessionRiskFloor")

__all__ = [
    "session_floor", "cycle_session_floor", "set_session_floor",
    "clear_session_floor", "session_floor_label", "session_cycle_enabled",
]

_LOCK = threading.RLock()
_OVERRIDE: Optional[str] = None

#: The cycle, loosest to strictest. `safe_auto` is absent ON PURPOSE: it is
#: the most permissive tier, and offering it here would let the keystroke ask
#: for less friction than the configuration demands. Composition would refuse
#: it anyway — this simply does not pretend to offer it.
_CYCLE: List[Optional[str]] = [None, "notify_apply", "approval_required",
                               "blocked"]

_LABELS = {
    None: "config",
    "notify_apply": "notify",
    "approval_required": "approve",
    "blocked": "blocked",
}


def session_cycle_enabled() -> bool:
    """Default ON. Off, the floor is configuration-only."""
    return os.environ.get(
        "JARVIS_SESSION_RISK_CYCLE_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def session_floor() -> Optional[str]:
    """The session's requested floor, or None when following config.

    Read by `risk_tier_floor` as ONE MORE INPUT to its strictest-wins
    composition — never as an override of it. That is what makes it
    structurally incapable of loosening anything.
    """
    if not session_cycle_enabled():
        return None
    with _LOCK:
        return _OVERRIDE


def set_session_floor(tier: Optional[str]) -> Optional[str]:
    """Set the floor explicitly. Unknown tiers are refused, not guessed."""
    global _OVERRIDE
    try:
        if tier is None:
            with _LOCK:
                _OVERRIDE = None
            return None
        candidate = str(tier).strip().lower()
        if candidate not in [t for t in _CYCLE if t]:
            logger.debug("[SessionFloor] refused unknown tier %r", tier)
            return session_floor()
        with _LOCK:
            _OVERRIDE = candidate
        logger.info("[SessionFloor] session floor set to %s", candidate)
        return candidate
    except Exception:  # noqa: BLE001
        return session_floor()


def cycle_session_floor() -> Optional[str]:
    """Shift+Tab. Advances one step and wraps back to following config.

    Wrapping to `None` rather than to `safe_auto` is what keeps the cycle
    honest: "back to what I configured" is a real destination, and "less
    strict than I configured" is not one this key can reach.
    """
    global _OVERRIDE
    try:
        if not session_cycle_enabled():
            return None
        with _LOCK:
            index = _CYCLE.index(_OVERRIDE) if _OVERRIDE in _CYCLE else 0
            _OVERRIDE = _CYCLE[(index + 1) % len(_CYCLE)]
            current = _OVERRIDE
        logger.info("[SessionFloor] cycled to %s", current or "config")
        return current
    except Exception:  # noqa: BLE001
        logger.debug("[SessionFloor] cycle degraded", exc_info=True)
        return None


def clear_session_floor() -> None:
    """Detach, or a new session. The override never outlives the cockpit."""
    global _OVERRIDE
    with _LOCK:
        _OVERRIDE = None


def session_floor_label() -> str:
    """``⇧⇥ approve`` for the toolbar, or "" while following config.

    Silent when following configuration: a permanent badge saying "normal" is
    chrome, and chrome is not read. The moment it says anything, it means the
    operator changed something — which is exactly when they need to see it.
    """
    try:
        current = session_floor()
        if current is None:
            return ""
        return f"⇧⇥ {_LABELS.get(current, current)}"
    except Exception:  # noqa: BLE001
        return ""
