"""Sovereign Remediation Matrix (Slice 255) — genuine, kernel-bound self-healing actions.

The Cybernetic Reanimation wiring instantiated ``SelfHealingOrchestrator`` with NO
registered remediation handlers, so ``_execute_remediation`` always short-circuited
("No handler registered") → the ``shadow_guard`` trap never fired → ``/endorse`` was
dead in production (a wired-but-inert nervous system; live-fire finding, Slice 255).

This module registers REAL remediations bound directly to the kernel's existing
physical capabilities — NOT hollow mocks. Each handler is the genuine dangerous
action; the existing ``SelfHealingOrchestrator._execute_remediation`` routes every
call through ``shadow_guard`` first, so under ``JARVIS_RESILIENCE_SHADOW_MODE`` the
action is TRAPPED (logged + stashed for ``/endorse``) and only executes for real
once the Sovereign Host endorses it.

Strategy → genuine capability:
  RESTART / FAILOVER → ``kernel._trinity.restart_component(component)`` (real Trinity
                       component process restart; failover = restart the failing one)
  SCALE_DOWN         → ``GracefulDegradationManager._check_resources()`` organ
                       (real: re-evaluates pressure + disables low-priority features)
  ISOLATE            → ``AdvancedCircuitBreaker.record_failure()`` organ
                       (real: trips the breaker → blocks the failing component)
  ROLLBACK           → ``kernel._rollback_coordinator.rollback(component)`` if present
  NOTIFY_ONLY        → benign notify (non-dangerous; logged)

Decoupled by structural typing: takes the live ``self_healing`` organ + kernel +
organs (duck-typed) — NEVER imports ``unified_supervisor``. All handlers are
async ``(component: str) -> bool`` and fail-soft (a missing capability logs + returns
False; it never raises into the orchestrator).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("remediation_arsenal")


def _resolve(obj: Optional[Any], *path: str) -> Optional[Callable[..., Any]]:
    """Walk an attribute path (fail-soft) and return the final callable, else None."""
    cur = obj
    for name in path:
        if cur is None:
            return None
        cur = getattr(cur, name, None)
    return cur if callable(cur) else None


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def build_remediation_handlers(
    *, kernel: Optional[Any] = None, organs: Optional[Dict[str, Any]] = None
) -> Dict[str, Callable[[str], Any]]:
    """Return ``{strategy_value: async handler}`` bound to real kernel capabilities."""
    organs = organs or {}
    grace = organs.get("GracefulDegradationManager")
    breaker = organs.get("AdvancedCircuitBreaker")

    async def _restart(component: str) -> bool:
        fn = _resolve(kernel, "_trinity", "restart_component")
        if fn is None:
            logger.warning("[Arsenal] RESTART: kernel._trinity.restart_component unavailable")
            return False
        return bool(await _maybe_await(fn(component)))

    async def _failover(component: str) -> bool:
        # Genuine failover = restart the failing Trinity component to recover it.
        return await _restart(component)

    async def _scale_down(component: str) -> bool:
        fn = _resolve(grace, "_check_resources")
        if fn is None:
            logger.warning("[Arsenal] SCALE_DOWN: GracefulDegradationManager._check_resources unavailable")
            return False
        await _maybe_await(fn())
        return True

    async def _isolate(component: str) -> bool:
        fn = _resolve(breaker, "record_failure")
        if fn is None:
            logger.warning("[Arsenal] ISOLATE: AdvancedCircuitBreaker.record_failure unavailable")
            return False
        # Trip the breaker for the failing component → isolates it from traffic.
        await _maybe_await(fn(RuntimeError(f"isolate:{component}")))
        return True

    async def _rollback(component: str) -> bool:
        fn = _resolve(kernel, "_rollback_coordinator", "rollback")
        if fn is None:
            logger.warning("[Arsenal] ROLLBACK: kernel._rollback_coordinator.rollback unavailable")
            return False
        return bool(await _maybe_await(fn(component)))

    async def _notify_only(component: str) -> bool:
        # Non-dangerous: just record the concern. Genuine (it really logs/notifies).
        logger.info("[Arsenal] NOTIFY_ONLY: component=%s flagged (no mutating action)", component)
        return True

    return {
        "restart": _restart,
        "failover": _failover,
        "scale_down": _scale_down,
        "isolate": _isolate,
        "rollback": _rollback,
        "notify_only": _notify_only,
    }


def register_remediation_arsenal(
    self_healing: Any,
    *,
    kernel: Optional[Any] = None,
    organs: Optional[Dict[str, Any]] = None,
) -> int:
    """Register the genuine remediation arsenal onto a live ``SelfHealingOrchestrator``.

    Returns the number of handlers registered. NEVER raises (fail-soft) — a wiring
    failure must not break boot; it just leaves SelfHealing without that handler.
    Idempotent-friendly: re-registering overwrites the same strategy keys.
    """
    if self_healing is None:
        logger.warning("[Arsenal] no SelfHealingOrchestrator — nothing to arm")
        return 0
    register = getattr(self_healing, "register_handler", None)
    strat_enum = getattr(type(self_healing), "RemediationStrategy", None)
    if not callable(register) or strat_enum is None:
        logger.warning("[Arsenal] SelfHealing has no register_handler/RemediationStrategy")
        return 0

    handlers = build_remediation_handlers(kernel=kernel, organs=organs)
    count = 0
    for strat in strat_enum:  # iterate the real enum so we only register valid strategies
        handler = handlers.get(strat.value)
        if handler is None:
            continue
        try:
            register(strat, handler)
            count += 1
        except Exception as err:  # noqa: BLE001 — fail-soft per strategy
            logger.warning("[Arsenal] register %s failed: %r", strat.value, err)
    logger.info("[Arsenal] armed SelfHealingOrchestrator with %d genuine remediations", count)
    return count
