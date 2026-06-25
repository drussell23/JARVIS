"""provider_quarantine.py — Rolling success-rate gradient + UPSTREAM QUARANTINE action.

Task T1 of the Autonomous Provider Quarantine Matrix.

When the DW provider enters a GLOBAL outage (every model timing out, both
transport lanes collapsed), the immortal queue would otherwise re-queue the op
forever (observed: dilation hops=77, hammering a degraded upstream).

This module supplies the pure ``degradation gradient``: it DEDUCES a global
outage from the FAILURE RATE over a rolling bounded window — NOT a hardcoded
retry count — and seals the op via [SOVEREIGN YIELD: UPSTREAM QUARANTINE] +
Cryo-DLQ.

Public API
----------
JARVIS_QUARANTINE_WINDOW (env, default 5)
    Size of the per-route rolling window.  Mirrors JARVIS_WATCHDOG_TRACKER_SIZE
    / JARVIS_RECURSION_LEDGER_SIZE discipline: env-tunable, never hardcoded.

JARVIS_PROVIDER_QUARANTINE_ENABLED (env, default "true")
    Master kill-switch.  When false, quarantine_op is a no-op that returns
    False so the caller falls back to legacy immortal-queue behaviour.

class ProviderHealthGradient
    Per-route bounded deque of boolean sweep outcomes.  Mirrors the
    AttemptLedger (recursion_dedup.py) + ReductionTracker (convergence_watchdog.py)
    bounded-deque/singleton pattern; no novel persistent store.

    record_sweep(route, *, success) -> None
    success_rate(route) -> float
    is_global_outage(route) -> bool
    reset(route) -> None

get_provider_health_gradient() -> ProviderHealthGradient
    Process-global singleton.

quarantine_enabled() -> bool

quarantine_op(ctx, *, route, telemetry) -> bool
    Terminal quarantine action.  Fail-soft: returns True if sealed, False on
    any error (caller falls back to legacy immortal queue).
    Lazy-imports convergence_watchdog and intake_dlq to avoid import cycles.
"""
from __future__ import annotations

import collections
import logging
import os
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env-driven configuration
# ---------------------------------------------------------------------------

_ENV_WINDOW = "JARVIS_QUARANTINE_WINDOW"
_ENV_ENABLED = "JARVIS_PROVIDER_QUARANTINE_ENABLED"

_DEFAULT_WINDOW = 5


def _window_size() -> int:
    """Return the rolling window size from env (default 5). Clamped to >= 1."""
    try:
        return max(1, int(os.environ.get(_ENV_WINDOW, _DEFAULT_WINDOW)))
    except (ValueError, TypeError):
        return _DEFAULT_WINDOW


def quarantine_enabled() -> bool:
    """Return True unless JARVIS_PROVIDER_QUARANTINE_ENABLED is explicitly falsy."""
    val = os.environ.get(_ENV_ENABLED, "true").strip().lower()
    return val not in ("false", "0", "no", "off")


# ---------------------------------------------------------------------------
# ProviderHealthGradient — bounded per-route rolling success window
# ---------------------------------------------------------------------------

class ProviderHealthGradient:
    """Tracks per-route dispatch sweep outcomes in a bounded deque.

    The outage trigger is a RATE (velocity/gradient) over a rolling window —
    never a hardcoded retry count.  An outage is declared only when:
      1. The window is FULL (>= maxlen samples), AND
      2. success_rate == 0.0 (absolute zero across a full programmatic sweep).

    The maxlen is read from JARVIS_QUARANTINE_WINDOW at instantiation time
    (mirrors recursion_dedup.AttemptLedger and convergence_watchdog.ReductionTracker).
    """

    def __init__(self) -> None:
        # Dict[route, deque[bool]] — each deque is bounded to _window_size().
        # Window size is read lazily per-route on first record so env changes
        # made between module import and first use are honoured (mirrors the
        # pattern where env-driven configuration is not baked in at import time).
        self._windows: Dict[str, collections.deque] = {}

    def _get_window(self, route: str) -> collections.deque:
        """Return (creating if needed) the bounded deque for *route*.

        The maxlen is resolved from the env at first-use time for the route
        so that JARVIS_QUARANTINE_WINDOW set after module import is honoured.
        """
        if route not in self._windows:
            self._windows[route] = collections.deque(maxlen=_window_size())
        return self._windows[route]

    def record_sweep(self, route: str, *, success: bool) -> None:
        """Push one sweep outcome into the route window. Fail-soft."""
        try:
            self._get_window(route).append(bool(success))
        except Exception:  # pragma: no cover
            logger.debug("[ProviderQuarantine] record_sweep fail-soft", exc_info=True)

    def success_rate(self, route: str) -> float:
        """Return the fraction of True entries in the window (0.0..1.0).

        Empty window -> 1.0 (assume healthy until proven otherwise).
        Fail-soft: any exception returns 1.0.
        """
        try:
            dq = self._get_window(route)
            if not dq:
                return 1.0
            return sum(1 for v in dq if v) / len(dq)
        except Exception:  # pragma: no cover
            logger.debug("[ProviderQuarantine] success_rate fail-soft", exc_info=True)
            return 1.0

    def window_full(self, route: str) -> bool:
        """Return True iff the rolling window for *route* has reached maxlen.

        Public predicate so consumers (e.g. the Failover FSM's recovery gate)
        never reach into the private ``_get_window`` deque. Fail-soft: any
        exception returns False (conservative -- not yet enough evidence).
        """
        try:
            dq = self._get_window(route)
            maxlen = dq.maxlen
            return maxlen is not None and len(dq) >= maxlen
        except Exception:  # pragma: no cover
            logger.debug("[ProviderQuarantine] window_full fail-soft", exc_info=True)
            return False

    def is_global_outage(self, route: str) -> bool:
        """Return True iff the window is FULL AND success_rate == 0.0.

        This is the velocity/gradient gate — NOT a hardcoded retry-N.
        Fail-soft: any exception returns False.
        """
        try:
            dq = self._get_window(route)
            maxlen = dq.maxlen  # set at deque creation via _window_size()
            if maxlen is None or len(dq) < maxlen:
                # Window not yet full; not enough evidence to declare outage.
                return False
            return self.success_rate(route) == 0.0
        except Exception:  # pragma: no cover
            logger.debug(
                "[ProviderQuarantine] is_global_outage fail-soft", exc_info=True
            )
            return False

    def reset(self, route: str) -> None:
        """Clear the window for *route*. Fail-soft."""
        try:
            if route in self._windows:
                self._windows[route].clear()
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# Process-global singleton (mirrors get_reduction_tracker pattern)
# ---------------------------------------------------------------------------

_PROVIDER_HEALTH_GRADIENT_SINGLETON: ProviderHealthGradient | None = None


def get_provider_health_gradient() -> ProviderHealthGradient:
    """Return the process-global ProviderHealthGradient, creating on first use."""
    global _PROVIDER_HEALTH_GRADIENT_SINGLETON
    if _PROVIDER_HEALTH_GRADIENT_SINGLETON is None:
        _PROVIDER_HEALTH_GRADIENT_SINGLETON = ProviderHealthGradient()
    return _PROVIDER_HEALTH_GRADIENT_SINGLETON


# ---------------------------------------------------------------------------
# Lazy import helpers — avoid circular import cycles
# ---------------------------------------------------------------------------

def _import_emit_sovereign_yield() -> Callable:  # type: ignore[type-arg]
    """Lazy import of emit_sovereign_yield from convergence_watchdog."""
    from backend.core.ouroboros.governance.convergence_watchdog import (  # noqa: PLC0415
        emit_sovereign_yield,
    )
    return emit_sovereign_yield


def _import_append_dlq() -> Callable:  # type: ignore[type-arg]
    """Lazy import of append_dlq from intake_dlq."""
    from backend.core.ouroboros.governance.intake_dlq import (  # noqa: PLC0415
        append_dlq,
    )
    return append_dlq


# ---------------------------------------------------------------------------
# quarantine_op — terminal quarantine action
# ---------------------------------------------------------------------------

def _failover_provider_override() -> str:
    """The provider to pin a Cryo-DLQ-sealed op to on a DW outage (Gap 3b).

    Default ``"gcp-jprime"`` -- the awakened J-Prime failover node. Env-tunable
    (``JARVIS_FAILOVER_PROVIDER_OVERRIDE``); empty string disables the stamp
    (legacy: replay re-cascades through the normal path). NEVER raises.
    """
    return (
        os.environ.get("JARVIS_FAILOVER_PROVIDER_OVERRIDE", "gcp-jprime") or ""
    ).strip()


def _stamp_provider_override(ctx: Any) -> Any:
    """Stamp ``provider_override`` on *ctx* so the Cryo-DLQ seal carries the
    Gap-3b J-Prime pin. Handles frozen dataclasses via ``dataclasses.replace``.

    Returns the (possibly new) ctx to seal. Fail-soft: on any error returns the
    original ctx unchanged (the seal still happens; the op is never lost -- it
    just replays via the legacy path instead of the J-Prime pin).
    """
    override = _failover_provider_override()
    if not override:
        return ctx
    try:
        import dataclasses  # noqa: PLC0415

        if dataclasses.is_dataclass(ctx) and any(
            f.name == "provider_override" for f in dataclasses.fields(ctx)
        ):
            return dataclasses.replace(ctx, provider_override=override)
        # Non-dataclass / mutable: best-effort attribute set.
        setattr(ctx, "provider_override", override)
        return ctx
    except Exception:  # noqa: BLE001 -- never block the seal
        logger.debug(
            "[ProviderQuarantine] provider_override stamp fail-soft", exc_info=True
        )
        return ctx


def quarantine_op(ctx: Any, *, route: str, telemetry: dict) -> bool:
    """Seal a context op into the Cryo-DLQ via [SOVEREIGN YIELD: UPSTREAM QUARANTINE].

    Steps:
      (a) emit_sovereign_yield — logs [SOVEREIGN YIELD: UPSTREAM QUARANTINE]
      (a.5) stamp provider_override="gcp-jprime" (Gap 3b) so replay routes the
            op straight to the awakened J-Prime node, NOT the dead DW lane
      (b) append_dlq — persists to intake_dlq.jsonl with reason
          "upstream_quarantine:dw_global_outage"

    Returns True if both steps succeed, False on any error (never raises).
    The caller falls back to legacy immortal-queue behaviour on False.

    Lazy imports both callees to avoid import cycles.  All errors are caught
    and logged at DEBUG; the function is fully fail-soft.
    """
    try:
        op_id: str = getattr(ctx, "op_id", "") or ""

        # (a) Emit sovereign yield telemetry.
        try:
            emit_fn = _import_emit_sovereign_yield()
            emit_fn(
                op_id,
                lineage_id=op_id,
                ratio=0.0,
                consecutive_stalls=0,
                parent_chars=0,
                child_chars=0,
                tier="provider",
                # Space form so the emitted marker matches the documented/grepped
                # [SOVEREIGN YIELD: UPSTREAM QUARANTINE] (parity with LANE COLLAPSE /
                # UNRESOLVABLE PATH and the Sentinel/watcher patterns).
                reason="UPSTREAM QUARANTINE",
            )
        except Exception:
            logger.debug(
                "[ProviderQuarantine] emit_sovereign_yield fail-soft", exc_info=True
            )
            # Non-fatal: continue to DLQ step.

        # Attach telemetry to ctx best-effort so the DLQ envelope carries it.
        try:
            if hasattr(ctx, "dw_telemetry"):
                ctx.dw_telemetry = telemetry
        except Exception:  # pragma: no cover
            pass

        # (a.5) Stamp the J-Prime provider_override (Gap 3b) BEFORE sealing so
        # the persisted envelope carries the pin and replay routes to the
        # awakened node, NOT the dead DW lane.
        ctx = _stamp_provider_override(ctx)

        # (b) Append to Cryo-DLQ.
        append_fn = _import_append_dlq()
        append_fn(ctx, reason="upstream_quarantine:dw_global_outage")

        return True

    except Exception:
        logger.debug(
            "[ProviderQuarantine] quarantine_op fail-soft", exc_info=True
        )
        return False
