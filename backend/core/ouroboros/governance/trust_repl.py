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

#: The dial's positions, loosest → strictest. ``safe_auto`` is the
#: no-floor resting state (green ops auto-apply as classified).
_CYCLE = ("safe_auto", "notify_apply", "approval_required")

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
    raw = os.environ.get(_ENV_MIN_TIER, "").strip().lower()
    return raw if raw in _CYCLE else "safe_auto"


def floor_chip() -> str:
    """The status-line chip: empty at rest so the line stays calm, loud
    when the operator has raised the floor. NEVER raises."""
    tier = current_floor()
    if tier == "safe_auto":
        return ""
    return f"{_GLYPH.get(tier, '')}⛨ {tier}"


def _describe(tier: str) -> str:
    return {
        "safe_auto": "no floor — green auto-applies, yellow previews, "
                     "orange waits for you",
        "notify_apply": "floor NOTIFY_APPLY — every apply shows its diff "
                        "before landing",
        "approval_required": "floor APPROVAL_REQUIRED — nothing lands "
                             "without a human verdict",
    }.get(tier, tier)


def _set_floor(tier: str) -> str:
    if tier == "safe_auto":
        os.environ.pop(_ENV_MIN_TIER, None)
    else:
        os.environ[_ENV_MIN_TIER] = tier
    logger.info("[Trust] risk-tier floor -> %s", tier)
    return f"{_GLYPH.get(tier, '')} trust dial → {tier}: {_describe(tier)}"


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
            here = _CYCLE.index(current_floor())
            nxt = _CYCLE[(here + 1) % len(_CYCLE)]
            return TrustReplDispatchResult(ok=True, text=_set_floor(nxt))

        if sub in _CYCLE:
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
                text=(f"{_GLYPH.get(tier, '')} trust: {tier} — "
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
