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
    **Defaults to UTC** when ``JARVIS_AUTO_APPLY_QUIET_HOURS_TZ`` is
    unset — implicit local-wall-clock semantics are ambiguous across
    multi-operator deployments (Manifesto §4: clarity over convenience).

* ``JARVIS_AUTO_APPLY_QUIET_HOURS_TZ=UTC|America/Los_Angeles|...``
    IANA timezone name used to interpret ``QUIET_HOURS``. Absent /
    malformed → falls back to UTC with a DEBUG log. An explicit
    ``UTC`` pass-through keeps the default documented.

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
from datetime import datetime, timezone
from typing import Optional, Tuple

logger = logging.getLogger("Ouroboros.RiskFloor")

_ENV_MIN_TIER = "JARVIS_MIN_RISK_TIER"
_ENV_PARANOIA = "JARVIS_PARANOIA_MODE"
_ENV_QUIET_HOURS = "JARVIS_AUTO_APPLY_QUIET_HOURS"
_ENV_QUIET_HOURS_TZ = "JARVIS_AUTO_APPLY_QUIET_HOURS_TZ"

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


def _resolve_tz():
    """Resolve JARVIS_AUTO_APPLY_QUIET_HOURS_TZ to a tzinfo. Falls back
    to UTC when unset / malformed. Emits one DEBUG line on fallback so
    operators can spot a typo without spam.

    Uses stdlib ``zoneinfo`` (Python 3.9+); if ZoneInfoNotFoundError or
    import failure occurs, returns UTC. Returning a concrete tzinfo
    keeps ``datetime.now(tz=...)`` call sites simple.
    """
    raw = os.environ.get(_ENV_QUIET_HOURS_TZ, "").strip()
    if not raw or raw.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except Exception:  # noqa: BLE001
        logger.debug(
            "[RiskFloor] zoneinfo unavailable — falling back to UTC",
        )
        return timezone.utc
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        logger.debug(
            "[RiskFloor] unknown IANA zone %r — falling back to UTC",
            raw,
        )
        return timezone.utc
    except Exception:  # noqa: BLE001
        logger.debug(
            "[RiskFloor] zone resolution raised — falling back to UTC",
            exc_info=True,
        )
        return timezone.utc


def quiet_hours_active(now: Optional[datetime] = None) -> bool:
    """True when ``JARVIS_AUTO_APPLY_QUIET_HOURS`` is set and the current
    wall-clock hour (in ``QUIET_HOURS_TZ``, defaulting to UTC) falls
    inside the configured window.

    Supports wrap-around: ``22-7`` means 22:00-06:59 in the resolved
    zone. A window of ``9-17`` means 09:00-16:59. Equal start/end is
    treated as the whole day (rare — operator wanted 24h paranoia,
    should use ``MIN_RISK_TIER`` instead, but we honor it).

    Parameters
    ----------
    now:
        Optional explicit datetime for testing. May be naive (assumed
        UTC) or aware. When aware, it's converted to the resolved zone
        before extracting the hour.
    """
    raw = os.environ.get(_ENV_QUIET_HOURS, "").strip()
    if not raw:
        return False
    parsed = _parse_quiet_hours(raw)
    if parsed is None:
        return False
    start, end = parsed

    tz = _resolve_tz()
    if now is None:
        # Default: current time in the resolved zone (UTC by default).
        hour = datetime.now(tz=tz).hour
    elif now.tzinfo is None:
        # Naive datetime — historically this was "local time". The new
        # contract: naive is treated as UTC, then converted if TZ set.
        aware = now.replace(tzinfo=timezone.utc)
        hour = aware.astimezone(tz).hour
    else:
        hour = now.astimezone(tz).hour

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
        tz_env = os.environ.get(_ENV_QUIET_HOURS_TZ, "").strip() or "UTC"
        bits.append(f"{_ENV_QUIET_HOURS}={raw} (tz={tz_env}) active")
    if not bits:
        return "(no floor active)"
    return ", ".join(bits)
