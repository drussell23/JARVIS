"""Slice 231 — Provider availability telemetry snapshot (read-only client).

A deterministic, side-effect-free view of live provider health that the
urgency router consumes at the ROUTE phase to synthesize a budget profile
(see :func:`urgency_router.synthesize_budget_profile`). It exists to replace
the STATIC budget lookup table that blinded routing to infrastructure reality
— most acutely the IMMEDIATE route allocating the funded DW primary
``max_dw_wait_s: 0.0`` while the premium Claude fallback lane was out of credits.

Design invariants (load-bearing):

  * **Read-only / no probe consumption.** We read ``breaker.state`` and
    ``breaker.snapshot()`` only. We MUST NOT call ``should_allow_request()`` —
    that flickers ``True`` during a HALF_OPEN probe AND consumes the probe slot
    (the Slice 162 regression). A telemetry snapshot never mutates the system
    it observes.
  * **Fail-soft.** Any exception while sensing → conservative legacy-safe
    defaults (both providers assumed available). A sensing bug must never be
    able to starve dispatch.
  * **No I/O on the hot path beyond the existing cached ledger read.** Pure
    enum/dict inspection; sub-millisecond; no network, no LLM.

The snapshot carries a reserved ``dw_latency_p95_s`` field (default ``None``)
so a future latency-variance-aware refinement can plug in without a schema
change — it is unused by the v1 deterministic kernel.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from backend.core.ouroboros.governance.claude_circuit_breaker import (
    CircuitState,
    get_claude_circuit_breaker,
    is_enabled as claude_breaker_is_enabled,
)
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceKind,
    SurfaceVerdict,
)

# Mirror Slice 19a/22's structural-disable contract verbatim.
_CLAUDE_DISABLED_ENV_VAR = "JARVIS_PROVIDER_CLAUDE_DISABLED"
_TRUE_TOKENS = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ProviderAvailabilitySnapshot:
    """Immutable read-only view of live provider availability.

    ``claude_available`` is the budget-relevant predicate: True only when the
    premium fallback lane can be committed to right now. ``*_reason`` strings
    are forensic labels for the §7 observability log.
    """

    claude_available: bool
    claude_reason: str
    dw_healthy: bool
    dw_reason: str
    # Reserved for a future latency-variance-aware kernel (Slice 232). Unused
    # by the deterministic v1 synthesizer; defaults keep the schema additive.
    dw_latency_p95_s: Optional[float] = None


def _env_true(var: str) -> bool:
    return os.environ.get(var, "").strip().lower() in _TRUE_TOKENS


def _resolve_claude(
    *, breaker, breaker_enabled: bool, claude_disabled: bool,
) -> tuple[bool, str]:
    """Read-only Claude-lane availability + forensic reason."""
    if claude_disabled:
        return False, "structurally_disabled"
    if not breaker_enabled:
        # Breaker not authoritative → assume available (legacy behavior).
        return True, "breaker_disabled"

    state = breaker.state  # read-only property
    if state is CircuitState.CLOSED:
        return True, "closed"
    if state is CircuitState.HALF_OPEN:
        # Probing, not available. Committing an IMMEDIATE op to a half-open lane
        # just exhausts it (Slice 162).
        return False, "half_open_probing"

    # OPEN — distinguish why, read-only, via the telemetry snapshot.
    snap = breaker.snapshot()
    if int(snap.get("consecutive_economic_failures", 0) or 0) > 0:
        return False, "breaker_open_economic"
    if int(snap.get("consecutive_transport_failures", 0) or 0) > 0:
        return False, "breaker_open_transport"
    return False, "breaker_open"


def _resolve_dw(*, ledger) -> tuple[bool, str]:
    """Read-only DW direct-streaming surface health + forensic reason."""
    record = ledger.verdict_for(SurfaceKind.DIRECT_STREAMING)
    if record is None:
        # No evidence of a problem → legacy-safe healthy.
        return True, "unknown"
    verdict = record.verdict
    if verdict in (SurfaceVerdict.HEALTHY, SurfaceVerdict.UPSTREAM_DEGRADED):
        # UPSTREAM_DEGRADED: DW upstream is slow but its transport is usable —
        # and when Claude is down it is the only funded lane, so still "usable".
        return True, verdict.value
    return False, verdict.value


def collect_provider_availability(
    *,
    breaker=None,
    ledger=None,
    claude_disabled: Optional[bool] = None,
    breaker_enabled: Optional[bool] = None,
) -> ProviderAvailabilitySnapshot:
    """Build a :class:`ProviderAvailabilitySnapshot` from live health state.

    All dependencies are injectable (defaulting to the process singletons /
    env) so callers and tests can drive deterministic states without touching
    global state. NEVER raises — on any sensing failure returns conservative
    legacy-safe defaults (both providers available).
    """
    try:
        _breaker = breaker if breaker is not None else get_claude_circuit_breaker()
        _ledger = ledger if ledger is not None else SurfaceHealthLedger()
        _disabled = (
            claude_disabled
            if claude_disabled is not None
            else _env_true(_CLAUDE_DISABLED_ENV_VAR)
        )
        _benabled = (
            breaker_enabled
            if breaker_enabled is not None
            else claude_breaker_is_enabled()
        )
        claude_ok, claude_reason = _resolve_claude(
            breaker=_breaker, breaker_enabled=_benabled, claude_disabled=_disabled,
        )
        dw_ok, dw_reason = _resolve_dw(ledger=_ledger)
        return ProviderAvailabilitySnapshot(
            claude_available=claude_ok,
            claude_reason=claude_reason,
            dw_healthy=dw_ok,
            dw_reason=dw_reason,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft; never starve dispatch
        return ProviderAvailabilitySnapshot(
            claude_available=True,
            claude_reason=f"fail_soft:{type(exc).__name__}",
            dw_healthy=True,
            dw_reason=f"fail_soft:{type(exc).__name__}",
        )


__all__ = [
    "ProviderAvailabilitySnapshot",
    "collect_provider_availability",
]
