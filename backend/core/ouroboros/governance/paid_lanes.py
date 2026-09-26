"""May this process spend money on a paid LLM lane right now? One answer.

## Why this exists

The local 30B is free and on the same machine; the paid lanes (Anthropic,
DoubleWord) cost money and, on 2026-09-26, had none behind their keys. Yet the
organism kept reaching for them, in three ways measured that day:

* ``_free_lane_active()`` answered "not free" merely because a key sat in
  ``.env``, so the local-first pre-route ``_try_local_primary`` never ran and
  cost-saving policies kept penalising the free lane;
* every failed local generation cascaded to a Claude "fallback" that could not
  answer (the SDK died before the request left the process), tripped a
  breaker, and parked the op in cooldown;
* about fifteen paths built their own Claude/DW clients — cockpit ``/ask``,
  narration, discovery probes every 120 s, a DW sentinel subprocess — and the
  one existing switch, ``JARVIS_PROVIDER_CLAUDE_DISABLED``, covered three of
  them, read independently by eight modules, and had no DoubleWord twin.

This module is the single authority. Everything that would build, probe or
call a paid lane asks it; nothing else reads those switches.

## What decides, in order

1. **Not a paid lane** (the local lane, J-Prime): always allowed.
2. **The operator's declaration** — ``JARVIS_PAID_LANES_ENABLED``. ``false``
   turns every paid lane off at once: never constructed, never probed, never
   called. Flip it back when the accounts are funded; nothing else changes.
3. **A per-provider switch** — ``JARVIS_PROVIDER_<NAME>_DISABLED``, derived
   from the provider's canonical name. ``JARVIS_PROVIDER_CLAUDE_DISABLED`` is
   the existing instance, now honoured everywhere it was meant to be.
4. **A credential** — the provider's key, or the Aegis daemon holding it.

That is :func:`paid_lane_configured` — declarative, deterministic, what BOOT
uses to decide whether to build a provider at all.

:func:`paid_lane_allowed` adds the ADAPTIVE half for dispatch time: a lane the
economic ledger has observed refusing for lack of money
(``economic_state.economic_view`` → ``ECONOMIC``) is treated as unavailable
while that observation stands, even when configured. The ledger's own window
lapses on its TTL, so a topped-up account is retried without a restart and
without anyone editing a flag.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

logger = logging.getLogger("Ouroboros.PaidLanes")

MASTER_ENV = "JARVIS_PAID_LANES_ENABLED"
_VERDICT_TTL_ENV = "JARVIS_PAID_LANE_VERDICT_TTL_S"
_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


@dataclass(frozen=True)
class PaidLane:
    """What makes a lane paid: its canonical name, the credential that funds
    it, and the names the codebase uses for it."""

    name: str
    credential_env: str
    aliases: Tuple[str, ...] = ()


#: The paid lanes. DATA, not code paths — a third vendor is one entry.
PAID_LANES: Tuple[PaidLane, ...] = (
    PaidLane("claude", "ANTHROPIC_API_KEY", ("anthropic", "claude-api")),
    PaidLane("doubleword", "DOUBLEWORD_API_KEY",
             ("dw", "doubleword-397b", "dw_rt")),
)

_BY_NAME: Dict[str, PaidLane] = {}
for _lane in PAID_LANES:
    for _n in (_lane.name, *_lane.aliases):
        _BY_NAME[_n] = _lane


def lane_for(provider: str) -> Optional[PaidLane]:
    """The paid lane ``provider`` names, or None for a free one. Model-id
    spellings (``doubleword-397b``, ``claude-sonnet-…``) resolve by prefix."""
    key = (provider or "").strip().lower()
    if key in _BY_NAME:
        return _BY_NAME[key]
    for name, lane in _BY_NAME.items():
        if key.startswith(name + "-"):
            return lane
    return None


class PaidLaneDisabled(RuntimeError):
    """Raised by a paid-lane client factory the authority has refused. Typed
    so callers that already degrade on a construction failure keep doing so,
    and so a log says WHY rather than looking like an outage."""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"paid lane {provider!r} disabled: {reason}")
        self.provider = provider
        self.reason = reason


@dataclass(frozen=True)
class LaneVerdict:
    allowed: bool
    reason: str


def _env_flag(name: str) -> Optional[bool]:
    raw = (os.environ.get(name, "") or "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return None


def paid_lanes_enabled() -> bool:
    """The operator's master declaration. Unset keeps the historical
    behaviour (paid lanes usable when credentialed); it is the operator who
    knows whether an account has money, and ``false`` is how they say so."""
    flag = _env_flag(MASTER_ENV)
    return True if flag is None else flag


def _aegis_holds_credentials() -> bool:
    try:
        from backend.core.ouroboros.aegis.client import is_enabled
        return bool(is_enabled())
    except Exception:  # noqa: BLE001
        return False


def switch_verdict(provider: str) -> LaneVerdict:
    """The operator's switches ONLY: the master declaration and the
    per-provider ``JARVIS_PROVIDER_<NAME>_DISABLED``. No credential, no
    ledger — this is what "is this lane structurally switched off?" meant to
    every reader of ``JARVIS_PROVIDER_CLAUDE_DISABLED``, now with the master
    switch folded in. NEVER raises."""
    lane = lane_for(provider)
    if lane is None:
        return LaneVerdict(True, "not a paid lane")
    if not paid_lanes_enabled():
        return LaneVerdict(False, f"{MASTER_ENV}=false")
    per = f"JARVIS_PROVIDER_{lane.name.upper()}_DISABLED"
    if _env_flag(per) is True:
        return LaneVerdict(False, f"{per}=true")
    return LaneVerdict(True, "switched on")


def paid_lane_switched_on(provider: str) -> bool:
    return switch_verdict(provider).allowed


def configured_verdict(provider: str) -> LaneVerdict:
    """Switches + a credential in the environment (or held by Aegis): "is
    there a paid lane on this host at all?". For cost questions such as
    ``_free_lane_active`` — never for a call site that holds its own
    provider, whose key may have been passed explicitly. NEVER raises."""
    base = switch_verdict(provider)
    lane = lane_for(provider)
    if not base.allowed or lane is None:
        return base
    if (os.environ.get(lane.credential_env) or "").strip() or _aegis_holds_credentials():
        return LaneVerdict(True, "configured")
    return LaneVerdict(False, f"no {lane.credential_env}")


def paid_lane_configured(provider: str) -> bool:
    return configured_verdict(provider).allowed


# -- the adaptive half: observed economic death ------------------------------

_verdict_cache: Dict[str, Tuple[float, LaneVerdict]] = {}
_cache_lock = threading.Lock()


def _verdict_ttl_s() -> float:
    try:
        return max(0.0, float(os.environ.get(_VERDICT_TTL_ENV, "") or 5.0))
    except ValueError:
        return 5.0


def _economically_dead(lane: PaidLane) -> Optional[str]:
    """The ledger's standing ECONOMIC observation for this lane, or None.
    Asked under every alias the ledger may have recorded it by."""
    try:
        from backend.core.ouroboros.governance.economic_state import (
            ECONOMIC, economic_view,
        )
        for name in (lane.name, *lane.aliases):
            view = economic_view(name) or {}
            if view.get("state") == ECONOMIC:
                return str(view.get("reason") or "observed out of funds")[:160]
    except Exception:  # noqa: BLE001 — an unreadable ledger never blocks a lane
        logger.debug("[PaidLanes] economic read degraded", exc_info=True)
    return None


def allowed_verdict(provider: str) -> LaneVerdict:
    """Dispatch-time verdict for a call site about to spend: switched on AND
    not observed unfunded. Credentials are the call site's own business (it
    may hold an explicitly-keyed provider). The ledger read is TTL-cached
    (``JARVIS_PAID_LANE_VERDICT_TTL_S``) because hot paths ask. NEVER raises."""
    base = switch_verdict(provider)
    lane = lane_for(provider)
    if not base.allowed or lane is None:
        return base
    now = time.monotonic()
    ttl = _verdict_ttl_s()
    with _cache_lock:
        hit = _verdict_cache.get(lane.name)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    dead = _economically_dead(lane)
    verdict = LaneVerdict(False, f"unfunded (observed): {dead}") if dead else base
    with _cache_lock:
        _verdict_cache[lane.name] = (now, verdict)
    return verdict


def paid_lane_allowed(provider: str) -> bool:
    return allowed_verdict(provider).allowed


def require_paid_lane(provider: str) -> None:
    """For client factories: raise :class:`PaidLaneDisabled` when refused."""
    verdict = allowed_verdict(provider)
    if not verdict.allowed:
        raise PaidLaneDisabled(provider, verdict.reason)


def any_paid_lane_configured() -> bool:
    return any(paid_lane_configured(lane.name) for lane in PAID_LANES)


def posture() -> Mapping[str, object]:
    """For surfaces (cockpit banner, status chip, /preflight): what the
    organism may spend on, and why. NEVER raises."""
    def _usable(name: str) -> LaneVerdict:
        configured = configured_verdict(name)
        return allowed_verdict(name) if configured.allowed else configured

    lanes = {lane.name: _usable(lane.name) for lane in PAID_LANES}
    usable = [n for n, v in lanes.items() if v.allowed]
    return {
        "mode": "paid+local" if usable else "local-only",
        "declared_off": not paid_lanes_enabled(),
        # The operator's DECLARATION, independent of what this host holds:
        # every paid lane switched off. A keyless host is local-only in
        # fact but has declared nothing, and a surface must not claim it.
        "declared_local": not any(
            paid_lane_switched_on(lane.name) for lane in PAID_LANES
        ),
        "lanes": {n: {"allowed": v.allowed, "reason": v.reason}
                  for n, v in lanes.items()},
    }


def reset_for_tests() -> None:
    with _cache_lock:
        _verdict_cache.clear()


__all__ = [
    "LaneVerdict", "MASTER_ENV", "PAID_LANES", "PaidLane", "PaidLaneDisabled",
    "allowed_verdict", "any_paid_lane_configured", "configured_verdict",
    "lane_for", "paid_lane_allowed", "paid_lane_configured",
    "paid_lane_switched_on", "paid_lanes_enabled", "posture",
    "require_paid_lane", "reset_for_tests", "switch_verdict",
]
