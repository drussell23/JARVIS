"""Risk-tier floor: paranoia-mode knobs that FORBID SAFE_AUTO.

The existing ``JARVIS_RISK_CEILING`` env knob sets an upward escalation
floor — it can push SAFE_AUTO to NOTIFY_APPLY, etc. This module ADDS
two further paranoia controls the operator can flip before going to
sleep:

* ``JARVIS_MIN_RISK_TIER={safe_auto|notify_apply|approval_required}``
    Absolute floor. If the risk engine classifies SAFE_AUTO but the
    floor is ``notify_apply``, the classification is upgraded. Same
    for ``approval_required`` forcing every op to pause for the human.

* ``JARVIS_PARANOIA_MODE=1``
    Convenience shortcut equivalent to ``MIN_RISK_TIER=notify_apply``.
    When set, the floor is active even if ``JARVIS_MIN_RISK_TIER`` is
    unset.

* ``JARVIS_AUTO_APPLY_QUIET_HOURS=<start>-<end>``
    Time-of-day window during which ``MIN_RISK_TIER=notify_apply`` is
    implicitly active. Supports wrap-around (``22-7`` → 10 PM to 7 AM).
    Uses the local timezone (``datetime.now()``).

Authority invariant: this module is pure-read. It consumes env vars +
the current time and returns a recommended floor. The caller (the
risk-engine wrapper) is responsible for applying the floor to the
classification.

Orderings are *safer-is-higher*:

    SAFE_AUTO < NOTIFY_APPLY < APPROVAL_REQUIRED < BLOCKED

So a floor of ``NOTIFY_APPLY`` means "never go below NOTIFY_APPLY" —
i.e. no SAFE_AUTO auto-applies.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional, Tuple

logger = logging.getLogger("Ouroboros.RiskFloor")

_ENV_MIN_TIER = "JARVIS_MIN_RISK_TIER"
_ENV_PARANOIA = "JARVIS_PARANOIA_MODE"
_ENV_QUIET_HOURS = "JARVIS_AUTO_APPLY_QUIET_HOURS"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Canonical ordering — safer-is-higher. Used only for comparison; the
# orchestrator continues to import RiskTier from risk_engine for its
# own handling so this module stays dependency-free.
_ORDER = {
    "safe_auto": 0,
    "notify_apply": 1,
    "approval_required": 2,
    "blocked": 3,
}


def _norm_tier(s: str) -> str:
    return (s or "").strip().lower()


def paranoia_mode_enabled() -> bool:
    return os.environ.get(_ENV_PARANOIA, "0").strip().lower() in _TRUTHY


def _env_floor() -> Optional[str]:
    """Read the explicit ``JARVIS_MIN_RISK_TIER`` env, normalized.

    Returns None when unset or malformed. Recognised values are
    ``"safe_auto"``, ``"notify_apply"``, ``"approval_required"``,
    ``"blocked"`` (case-insensitive).
    """
    raw = _norm_tier(os.environ.get(_ENV_MIN_TIER, ""))
    if not raw:
        return None
    if raw not in _ORDER:
        logger.debug(
            "[RiskFloor] unrecognised %s=%r — ignoring",
            _ENV_MIN_TIER, raw,
        )
        return None
    return raw


def _parse_quiet_hours(raw: str) -> Optional[Tuple[int, int]]:
    """Parse ``<start>-<end>`` where each side is 0-23. Returns
    ``(start, end)`` or ``None`` when malformed.
    """
    if not raw:
        return None
    parts = raw.strip().split("-", 1)
    if len(parts) != 2:
        return None
    try:
        start = int(parts[0].strip())
        end = int(parts[1].strip())
    except ValueError:
        return None
    if not (0 <= start < 24 and 0 <= end < 24):
        return None
    return (start, end)


def quiet_hours_active(now: Optional[datetime] = None) -> bool:
    """True when ``JARVIS_AUTO_APPLY_QUIET_HOURS`` is set and the current
    local hour falls inside the configured window.

    Supports wrap-around: ``22-7`` means 22:00-06:59 local. A window
    of ``9-17`` means 09:00-16:59. Equal start/end is treated as the
    whole day (rare — operator wanted 24h paranoia, should use
    ``MIN_RISK_TIER`` instead, but we honor it).
    """
    raw = os.environ.get(_ENV_QUIET_HOURS, "").strip()
    if not raw:
        return False
    parsed = _parse_quiet_hours(raw)
    if parsed is None:
        return False
    start, end = parsed
    hour = (now or datetime.now()).hour
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    # Wrap-around — e.g. 22-7 means 22,23,0,1,2,3,4,5,6.
    return hour >= start or hour < end


def recommended_floor(now: Optional[datetime] = None) -> Optional[str]:
    """Compose the three signals into a single floor recommendation.

    Ordering (strictest wins):
        1. ``JARVIS_MIN_RISK_TIER`` explicit value
        2. ``JARVIS_PARANOIA_MODE=1`` implies ``notify_apply``
        3. Active quiet-hours window implies ``notify_apply``

    Returns the normalised tier name or ``None`` when nothing applies.
    """
    explicit = _env_floor()
    candidates: list = []
    if explicit is not None:
        candidates.append(explicit)
    if paranoia_mode_enabled():
        candidates.append("notify_apply")
    if quiet_hours_active(now):
        candidates.append("notify_apply")
    if not candidates:
        return None
    # Pick the strictest — highest ordinal wins.
    return max(candidates, key=lambda t: _ORDER.get(t, 0))


def apply_floor_to_name(
    tier_name: str, *, now: Optional[datetime] = None,
) -> Tuple[str, Optional[str]]:
    """Apply the recommended floor to a tier *name*.

    Returns ``(effective_tier_name, applied_floor_or_None)``. When the
    floor is stricter than the input tier, ``effective_tier_name`` is
    the floor; otherwise the input passes through untouched.
    ``applied_floor_or_None`` is non-None only when the floor actually
    upgraded the tier — useful for observability logging.

    Unknown input tier names pass through unchanged.
    """
    raw_in = _norm_tier(tier_name)
    if raw_in not in _ORDER:
        return (tier_name, None)
    floor = recommended_floor(now)
    if floor is None:
        return (tier_name, None)
    if _ORDER[floor] <= _ORDER[raw_in]:
        return (tier_name, None)
    return (floor, floor)


def floor_reason(now: Optional[datetime] = None) -> str:
    """Human-readable explanation of why the floor fires.

    Used by the orchestrator when logging a tier upgrade so the operator
    can tell *which* knob triggered the upgrade.
    """
    bits: list = []
    explicit = _env_floor()
    if explicit is not None:
        bits.append(f"{_ENV_MIN_TIER}={explicit}")
    if paranoia_mode_enabled():
        bits.append(f"{_ENV_PARANOIA}=1")
    if quiet_hours_active(now):
        raw = os.environ.get(_ENV_QUIET_HOURS, "").strip()
        bits.append(f"{_ENV_QUIET_HOURS}={raw} active")
    if not bits:
        return "(no floor active)"
    return ", ".join(bits)
