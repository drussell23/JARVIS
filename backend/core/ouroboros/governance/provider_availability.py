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
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from backend.core.ouroboros.governance.claude_circuit_breaker import (
    CircuitState,
    get_claude_circuit_breaker,
    is_enabled as claude_breaker_is_enabled,
    _read_breaker_state,  # authoritative persisted-funding reader (TTL-aware, fail-soft)
)
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceKind,
    SurfaceVerdict,
)

# Mirror Slice 19a/22's structural-disable contract verbatim.
_CLAUDE_DISABLED_ENV_VAR = "JARVIS_PROVIDER_CLAUDE_DISABLED"
_TRUE_TOKENS = {"1", "true", "yes", "on"}

# Slice 232 — cold-start funding signal. The persisted breaker state is read at
# most once per this window so the hot route path is zero-I/O in steady state
# (non-blocking) while still picking up state changes within a few seconds.
_PERSIST_CACHE_ENV_VAR = "JARVIS_PROVIDER_AVAILABILITY_PERSIST_CACHE_S"
_DEFAULT_PERSIST_CACHE_S = 5.0
_persist_cache_lock = threading.Lock()
_persist_cache = {"ts": -1.0e9, "down": False}


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


def _persist_cache_ttl_s() -> float:
    try:
        raw = os.environ.get(_PERSIST_CACHE_ENV_VAR, "").strip()
        return max(0.0, float(raw)) if raw else _DEFAULT_PERSIST_CACHE_S
    except (TypeError, ValueError):
        return _DEFAULT_PERSIST_CACHE_S


def _persisted_claude_economic_down(
    reader: Optional[Callable[[], Optional[int]]] = None,
    *,
    now: Optional[float] = None,
) -> bool:
    """True iff the AUTHORITATIVE persisted breaker state shows a FRESH economic OPEN.

    Reuses ``claude_circuit_breaker._read_breaker_state`` (the single source of
    truth — already TTL-aware and fail-soft) so the Claude-funding verdict is
    populated from cold boot (op #1), INDEPENDENT of the persist ENABLE flag (the
    reader does not gate on it). This closes the Slice-231 timing gap where the
    in-memory breaker boots CLOSED and only trips OPEN mid-GENERATE — too late
    for the pre-dispatch budget lift.

    Fail-soft: any error → ``False`` (no signal; the live breaker still trips on
    the first 402 — graceful degradation, never a crash). When *reader* is
    injected the cache is bypassed for deterministic testing; the default path
    caches briefly so the hot route path is zero-I/O in steady state.
    """
    if reader is not None:
        try:
            return reader() is not None
        except Exception:  # noqa: BLE001 — fail-soft, never poison the snapshot
            return False
    _now = now if now is not None else time.monotonic()
    ttl = _persist_cache_ttl_s()
    with _persist_cache_lock:
        if (_now - _persist_cache["ts"]) <= ttl:
            return bool(_persist_cache["down"])
        try:
            down = _read_breaker_state() is not None
        except Exception:  # noqa: BLE001
            down = False
        _persist_cache["ts"] = _now
        _persist_cache["down"] = down
        return down


def _resolve_claude(
    *, breaker, breaker_enabled: bool, claude_disabled: bool,
    persisted_reader: Optional[Callable[[], Optional[int]]] = None,
) -> tuple[bool, str]:
    """Read-only Claude-lane availability + forensic reason."""
    if claude_disabled:
        return False, "structurally_disabled"
    if not breaker_enabled:
        # Breaker not authoritative → assume available (legacy behavior).
        return True, "breaker_disabled"

    state = breaker.state  # read-only property
    if state is CircuitState.CLOSED:
        # Slice 232 — cold-start funding signal. The in-memory breaker boots
        # CLOSED, but the persisted source of truth may already record a fresh
        # economic OPEN (out-of-credits surviving a restart). Honor it so the
        # pre-dispatch lift fires from op #1 rather than after the first 402.
        if _persisted_claude_economic_down(persisted_reader):
            return False, "breaker_open_economic_persisted"
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
    persisted_reader: Optional[Callable[[], Optional[int]]] = None,
) -> ProviderAvailabilitySnapshot:
    """Build a :class:`ProviderAvailabilitySnapshot` from live health state.

    All dependencies are injectable (defaulting to the process singletons /
    env) so callers and tests can drive deterministic states without touching
    global state. ``persisted_reader`` overrides the cold-start funding reader
    (defaults to the cached real persisted-breaker reader). NEVER raises — on
    any sensing failure returns conservative legacy-safe defaults (both
    providers available).
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
            persisted_reader=persisted_reader,
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
