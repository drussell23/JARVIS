"""One place an operator's model choice lives, for every lane.

`/model` could pin DoubleWord and only pretend to pin the others, and the
asymmetry was structural rather than an oversight:

    DW      JARVIS_DW_PRIMARY_OVERRIDE   read PER CALL by the Override Matrix
    Claude  config.claude_model          read ONCE, in `GovernedLoopConfig.from_env`
    J-Prime active tier label            owned by the failover controller

So ``/model claude-opus-5`` on a running daemon set an environment variable
that nothing would read again until the next boot. The verb said so honestly —
"recorded and shown here, but that lane is selected by ROUTE" — which is the
right thing to say about a control that does not work, and the wrong thing to
settle for.

This is the missing half: a single accessor every lane consults at REQUEST
time, so one pin means one behaviour regardless of which family it names.

WHY AN ACCESSOR AND NOT A ROUTER
----------------------------------
It resolves nothing and ranks nothing. DoubleWord keeps its Sovereign
Context-Routing Override Matrix — rank-1 promotion, soft-lock on repeated
failure, entitlement filtering — because that machinery is correct and a
second copy would be a second answer to "which model is running". This only
answers "did the operator name one for this lane", and each lane decides what
to do about it.

LATENCY IS ALREADY BOUNDED — DO NOT ADD A SECOND TIMEOUT
----------------------------------------------------------
A pin can force a slow model onto a fast lane; `provider_topology` will admit
it even on a route the policy seals. That is deliberate ("Sovereign"), and the
resulting latency is ALREADY contained at the one place it should be:

    orchestrator.py:6337
        generation = await asyncio.wait_for(
            self._generator.generate(ctx, deadline),
            timeout=_gen_timeout + _OUTER_GATE_GRACE_S,
        )

with ``_gen_timeout`` selected per LANE — immediate 120s, standard 220s,
complex 240s, background/speculative 180s, each env-tunable — plus a 15s
grace. `wait_for` cancels the underlying task, so the loop is freed rather
than held. Wrapping a pinned call in a second QoS breaker would create two
timeout authorities that can disagree, and the tighter one would silently
become the real budget for reasons no log explains.

What was genuinely missing is ATTRIBUTION, not enforcement: a timeout on a
route the operator forced open read exactly like any other timeout.
:func:`breach_context` supplies that, so the telemetry can say the override
was the cause.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.SovereignOverride")

SOVEREIGN_OVERRIDE_SCHEMA_VERSION: str = "sovereign_override.1"

#: lane -> the env var that lane's selection is read from.
#:
#: DoubleWord's is the Override Matrix's own variable, deliberately: pinning DW
#: through this interface must be indistinguishable from pinning it the way the
#: matrix already understands, or the two would drift.
_LANE_ENV: Dict[str, str] = {
    "doubleword": "JARVIS_DW_PRIMARY_OVERRIDE",
    "claude": "JARVIS_SOVEREIGN_CLAUDE_MODEL",
    "j-prime": "JARVIS_SOVEREIGN_PRIME_MODEL",
}

__all__ = [
    "SOVEREIGN_OVERRIDE_SCHEMA_VERSION",
    "breach_context",
    "clear_pin",
    "lanes",
    "pinned_for",
    "set_pin",
]


def lanes() -> Tuple[str, ...]:
    """Every lane this interface can pin. NEVER raises."""
    return tuple(_LANE_ENV)


def pinned_for(lane: str) -> str:
    """The operator's pinned model for *lane*, or ``""``. NEVER raises.

    Read per call, which is the entire point: a pin that is only consulted at
    boot is a pin an operator cannot use on a running organism.
    """
    try:
        key = _LANE_ENV.get(str(lane or "").strip().lower())
        if not key:
            return ""
        return (os.environ.get(key) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def set_pin(lane: str, model: str) -> bool:
    """Pin *model* for *lane*. Returns False for an unknown lane. NEVER raises.

    Writes the environment rather than a private store, because the DW matrix
    already reads the environment and a second location would mean a pin that
    is true in one place and false in another.
    """
    try:
        key = _LANE_ENV.get(str(lane or "").strip().lower())
        if not key or not str(model or "").strip():
            return False
        os.environ[key] = str(model).strip()
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[Sovereign] set_pin degraded", exc_info=True)
        return False


def clear_pin(lane: Optional[str] = None) -> None:
    """Release one lane's pin, or every lane's. NEVER raises."""
    try:
        targets = ([str(lane).strip().lower()] if lane else list(_LANE_ENV))
        for name in targets:
            key = _LANE_ENV.get(name)
            if key:
                os.environ.pop(key, None)
    except Exception:  # noqa: BLE001
        logger.debug("[Sovereign] clear_pin degraded", exc_info=True)


def breach_context(route: str) -> Dict[str, object]:
    """Why a timeout on *route* may be the operator's doing. NEVER raises.

    Enforcement is not this module's job — `orchestrator.py:6337` already caps
    generation per lane and cancels the task. ATTRIBUTION is: a timeout on a
    route a pin forced open is indistinguishable, in the logs, from a timeout
    on a route the policy chose. One is the operator's decision coming due and
    the other is a provider problem, and they need opposite responses.

    Returns an empty dict when nothing was pinned, so a caller can splat it
    into a telemetry payload and add nothing in the ordinary case.
    """
    try:
        active = {lane: pinned_for(lane) for lane in _LANE_ENV
                  if pinned_for(lane)}
        if not active:
            return {}
        payload: Dict[str, object] = {
            "sovereign_pinned": active,
            "schema_version": SOVEREIGN_OVERRIDE_SCHEMA_VERSION,
        }
        try:
            from backend.core.ouroboros.governance.model_repl import (
                routes_opened_by_pin,
            )
            opened = routes_opened_by_pin()
            if opened:
                payload["routes_opened_by_pin"] = list(opened)
                payload["route_was_forced"] = (
                    str(route or "").strip().lower() in opened)
        except Exception:  # noqa: BLE001 — attribution is never load-bearing
            pass
        return payload
    except Exception:  # noqa: BLE001
        return {}
