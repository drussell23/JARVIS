#!/usr/bin/env python3
"""Synthetic pressure injector — live-fire validation of the Cybernetic Reanimation chain.

SAFETY: this NEVER degrades the host. It does not spike real CPU/RAM; it dispatches a
single, precise, synthetic ``PressureSignal`` straight into the LIVE in-process
``EventActivationDispatcher`` (the reanimation muscle), so the chain fires under fully
controlled input.

IN-PROCESS ONLY (load-bearing constraint). The reanimation dispatcher and the
``SupervisorEventBus`` are per-process singletons. This module resolves the *booted*
kernel via ``backend.kernel.get_kernel_instance()`` and reaches
``kernel._resilience_dispatcher`` — which only exists inside the running supervisor
process. A standalone ``python synthetic_pressure_injector.py`` in a SEPARATE process
finds no booted kernel and no-ops with a clear message — by design. It must be invoked
in-process (e.g. from a flag-gated REPL debug verb or a harness post-boot hook).

Signal → chain it exercises:
  * ``component_degraded`` / ``anomaly_detected``
        → SelfHealingOrchestrator.check_and_remediate → _execute_remediation
        → shadow_guard_async TRAP (shadow mode) → PendingShadowAction stashed
        → SHADOW_ACTION_TRAPPED telemetry on the StreamEventBroker
        → ``/endorse`` [y/N] surfaces in the live REPL.   <-- the full HITL chain
  * ``resource_pressure``
        → LoadShedding / AutoScaling / GracefulDegradation react (recompute).
        NOTE: a raw resource_pressure does NOT stash a /endorse-able trap — for that
        leg, prefer the threshold-env path (set JARVIS_PRESSURE_CPU_THRESHOLD low) so
        the REAL sampler fires it; this injector's value is the component_degraded leg.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("synthetic_pressure_injector")

# signal-name → PressureSignalType attribute name
_SIGNAL_MAP = {
    "resource_pressure": "RESOURCE_PRESSURE",
    "component_degraded": "COMPONENT_DEGRADED",
    "anomaly_detected": "ANOMALY_DETECTED",
}


def _resolve_kernel(kernel: Optional[Any]) -> Optional[Any]:
    """Return the live booted kernel (passed in, or via the modular singleton)."""
    if kernel is not None:
        return kernel
    try:
        from backend.kernel import get_kernel_instance  # modular kernel package
        return get_kernel_instance()
    except Exception as err:  # noqa: BLE001 — diagnostic, never raise
        logger.error("[INJECT] could not resolve live kernel singleton: %r", err)
        return None


async def inject_pressure(
    signal: str = "component_degraded",
    source: str = "jarvis-prime",
    *,
    kernel: Optional[Any] = None,
    severity: str = "critical",
) -> Optional[int]:
    """Dispatch ONE synthetic, edge-triggered ``PressureSignal`` into the live
    reanimation dispatcher. Returns the number of organs the signal reached, or
    ``None`` if not injectable (no kernel / reanimation not ignited). NEVER raises.

    Must be awaited IN-PROCESS inside the running supervisor.
    """
    sig_key = (signal or "").strip().lower()
    if sig_key not in _SIGNAL_MAP:
        logger.error("[INJECT] unknown signal %r (use one of %s)",
                     signal, list(_SIGNAL_MAP))
        return None

    k = _resolve_kernel(kernel)
    if k is None:
        logger.error("[INJECT] no live kernel — run IN-PROCESS inside the booted "
                     "supervisor (a standalone process has no kernel).")
        return None

    dispatcher = getattr(k, "_resilience_dispatcher", None)
    if dispatcher is None:
        logger.error("[INJECT] reanimation not ignited — boot with "
                     "JARVIS_RESILIENCE_REANIMATION_ENABLED=true (kernel has no "
                     "_resilience_dispatcher).")
        return None

    try:
        from backend.core.cybernetic_reanimation import (
            PressureSignal, PressureSignalType, SignalEdge,
        )
    except Exception as err:  # noqa: BLE001
        logger.error("[INJECT] cybernetic_reanimation import failed: %r", err)
        return None

    sig = PressureSignal(
        type=getattr(PressureSignalType, _SIGNAL_MAP[sig_key]),
        source=source,
        edge=SignalEdge.RISING,
        severity=severity,
        detail={
            "synthetic": True,
            "injector": "live-fire-validation",
            "host_degraded": False,  # we never spike real hardware
        },
    )
    try:
        reached = await dispatcher.dispatch(sig)
    except Exception as err:  # noqa: BLE001 — dispatcher is fail-soft; report and stop
        logger.error("[INJECT] dispatch raised (unexpected — dispatcher is fail-soft): %r", err)
        return None

    logger.warning(
        "[INJECT] dispatched synthetic %s (source=%s, severity=%s) -> %s organ(s) "
        "reached. If shadow mode is ON and this was component_degraded/anomaly, a "
        "PendingShadowAction is now awaiting /endorse.",
        sig_key, source, severity, reached,
    )
    return reached


if __name__ == "__main__":  # standalone guard — explains the in-process requirement
    import sys
    sys.stderr.write(
        "synthetic_pressure_injector is IN-PROCESS tooling and cannot run standalone:\n"
        "  the reanimation dispatcher is a per-process singleton inside the live\n"
        "  supervisor. Invoke `await inject_pressure(...)` from inside the booted\n"
        "  process (a flag-gated REPL debug verb or a harness post-boot hook), not\n"
        "  as `python synthetic_pressure_injector.py`.\n"
    )
    raise SystemExit(2)
