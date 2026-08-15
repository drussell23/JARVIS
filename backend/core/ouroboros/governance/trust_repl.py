"""``/trust`` REPL — the operator's autonomy dial, one keystroke away.

Auto-discovered by ``repl_dispatch_registry`` via the naming cage. Cycles
or sets the risk-tier floor (``risk_tier_floor.py``): the runtime value IS
the ``JARVIS_MIN_RISK_TIER`` environment variable — every gate already
reads it fresh per operation, so setting it here changes the very next
GATE decision with no new plumbing and no second source of truth.

``Shift+Tab`` in an attached cockpit sends ``/trust cycle`` through the
normal input path, so the keystroke and the typed verb are ONE code path:
mirrored output, distributed history, and the status-line chip all update
everywhere the same way.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TRUST_REPL_SCHEMA_VERSION: str = "trust_repl.1"

__verb_help__ = {
    "trust": "cycle or set the autonomy trust dial (risk-tier floor)",
}

_ENV_MIN_TIER = "JARVIS_MIN_RISK_TIER"

#: The dial POSITION, distinct from the floor it asserts. Two rungs may
#: assert one floor (`explore` and `watch` both floor at approval_required)
#: and must still be distinguishable to the operator.
_ENV_DIAL_POSITION = "JARVIS_PROACTIVE_MODE_POSITION"

#: The dial's positions. When PRD §30's ladder is enabled these come from
#: `proactive_mode.reachable()` — computed from live capability, so a rung
#: the host cannot honour is never offered. Master-off, the shipped three
#: remain, byte-identically.
#:
#: NOT a second vocabulary: `proactive_mode.LADDER` is the single source and
#: this is a projection of it. Two tuples that could disagree is how the dial
#: and the gate start meaning different things by the same word.
_LEGACY_CYCLE = ("safe_auto", "notify_apply", "approval_required")


def _cycle_positions() -> tuple:
    """Reachable rung names, loosest → strictest. NEVER raises."""
    try:
        from backend.core.ouroboros.governance import proactive_mode as _pm
        if _pm.is_enabled():
            return tuple(p.name for p in _pm.reachable())
    except Exception:  # noqa: BLE001 — the dial must survive §30 being absent
        pass
    return _LEGACY_CYCLE


#: Retained for the module's own membership checks; the LIVE set is
#: `_cycle_positions()`.
_CYCLE = _LEGACY_CYCLE

_GLYPH = {
    "safe_auto": "🟢",
    "notify_apply": "🟡",
    "approval_required": "🟠",
}

_HELP = (
    "/trust — the autonomy trust dial (risk-tier floor)\n"
    "\n"
    "Subcommands:\n"
    "  /trust                    current floor + what it means\n"
    "  /trust cycle              next position (Shift+Tab does this)\n"
    "  /trust safe_auto          no floor — tiers as classified\n"
    "  /trust notify_apply       every apply shows a diff first\n"
    "  /trust approval_required  nothing lands without a human\n"
    "  /trust status             alias for bare /trust\n"
    "  /trust help               this text\n"
    "\n"
    "The floor COMPOSES with paranoia mode and quiet hours — strictest\n"
    "wins (risk_tier_floor.py). This changes the very next GATE decision.\n"
)


@dataclass(frozen=True)
class TrustReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = TRUST_REPL_SCHEMA_VERSION


def current_floor() -> str:
    """The dial's position, normalized. NEVER raises."""
    positions = _cycle_positions()
    dial = os.environ.get(_ENV_DIAL_POSITION, "").strip().lower()
    if dial in positions:
        return dial
    raw = os.environ.get(_ENV_MIN_TIER, "").strip().lower()
    return raw if raw in positions else "safe_auto"


def floor_chip() -> str:
    """The status-line chip: empty at rest so the line stays calm, loud
    when the operator has raised the floor. NEVER raises."""
    tier = current_floor()
    if tier == "safe_auto":
        return ""
    return f"{_glyph_for(tier)}⛨ {tier}"


def _describe(tier: str) -> str:
    rung = _rung(tier)
    if rung is not None:
        return rung.summary
    return {
        "safe_auto": "no floor — green auto-applies, yellow previews, "
                     "orange waits for you",
        "notify_apply": "floor NOTIFY_APPLY — every apply shows its diff "
                        "before landing",
        "approval_required": "floor APPROVAL_REQUIRED — nothing lands "
                             "without a human verdict",
    }.get(tier, tier)


def _rung(name: str):
    """The ladder rung for a dial position, or None. NEVER raises."""
    try:
        from backend.core.ouroboros.governance import proactive_mode as _pm
        if _pm.is_enabled():
            return _pm.position(name)
    except Exception:  # noqa: BLE001
        pass
    return None


def _glyph_for(tier: str) -> str:
    rung = _rung(tier)
    return rung.glyph if rung is not None else _GLYPH.get(tier, "")


def _set_floor(tier: str) -> str:
    """Move the dial. Writes the rung's RISK FLOOR, never its name.

    Load-bearing distinction. `explore` and `watch` are ladder positions, not
    risk tiers — `risk_tier_floor` accepts only
    ``safe_auto|notify_apply|approval_required``, so writing "explore" into
    ``JARVIS_MIN_RISK_TIER`` would leave the floor UNPARSEABLE and silently
    resolve to no floor at all: the strictest rungs on the dial would become
    the loosest in effect. `Position.risk_floor` exists exactly to carry this
    mapping, and both new rungs assert ``approval_required``.
    """
    rung = _rung(tier)
    floor = rung.risk_floor if rung is not None else (
        None if tier == "safe_auto" else tier)
    if floor is None:
        os.environ.pop(_ENV_MIN_TIER, None)
    else:
        os.environ[_ENV_MIN_TIER] = floor
    # The dial's own position is remembered separately from the floor it
    # asserts, so `explore` and `approval_required` stay distinguishable
    # even though they assert the same floor.
    os.environ[_ENV_DIAL_POSITION] = tier
    logger.info("[Trust] dial -> %s (risk floor %s)", tier, floor or "none")
    return f"{_glyph_for(tier)} trust dial → {tier}: {_describe(tier)}"


def dispatch_trust_command(line: str) -> TrustReplDispatchResult:
    """Cycle or set the autonomy trust dial.

    Operator: show or change how much the organism may do without you —
    cycle with Shift+Tab, or set a floor by name.
    """
    try:
        tokens = (line or "").strip().lstrip("/").split()
        sub = tokens[1].lower() if len(tokens) > 1 else ""

        if sub in ("help", "-h", "--help"):
            return TrustReplDispatchResult(ok=True, text=_HELP)

        if sub == "cycle":
            # CLAMPED, never wrapped (PRD §30.11 Q2, operator decision).
            # Wrapping means one accidental keypress moves from maximum
            # caution to maximum autonomy; a dial whose worst misfire is
            # "nothing happened" is strictly better than one whose worst
            # misfire is "everything is permitted".
            positions = _cycle_positions()
            here = (positions.index(current_floor())
                    if current_floor() in positions else 0)
            if here >= len(positions) - 1:
                tier = positions[here]
                return TrustReplDispatchResult(
                    ok=True,
                    text=(f"{_glyph_for(tier)} trust: already at the "
                          f"strictest position ({tier}) — cycle does not "
                          f"wrap. Set a looser one by name."),
                )
            return TrustReplDispatchResult(
                ok=True, text=_set_floor(positions[here + 1]))

        if sub in _cycle_positions():
            return TrustReplDispatchResult(ok=True, text=_set_floor(sub))

        if sub in ("", "status"):
            tier = current_floor()
            extras = []
            try:
                from backend.core.ouroboros.governance.risk_tier_floor import (  # noqa: E501
                    paranoia_mode_enabled,
                    quiet_hours_active,
                )
                if paranoia_mode_enabled():
                    extras.append("paranoia mode ON")
                if quiet_hours_active():
                    extras.append("quiet hours active")
            except Exception:  # noqa: BLE001
                pass
            tail = f"  ({', '.join(extras)})" if extras else ""
            return TrustReplDispatchResult(
                ok=True,
                text=(f"{_glyph_for(tier)} trust: {tier} — "
                      f"{_describe(tier)}{tail}"),
            )

        return TrustReplDispatchResult(
            ok=False,
            text=f"unknown /trust subcommand {sub!r} — try /trust help",
        )
    except Exception:  # noqa: BLE001
        logger.debug("[TrustRepl] dispatch degraded", exc_info=True)
        return TrustReplDispatchResult(ok=False, text="trust dial unavailable")


__all__ = [
    "TRUST_REPL_SCHEMA_VERSION",
    "TrustReplDispatchResult",
    "current_floor",
    "dispatch_trust_command",
    "floor_chip",
]
