"""Cybernetic Reanimation — the asynchronous resilience nervous system (Spec 2).

Wires the surviving resilience organs to the live event stream WITHOUT coupling
to the 102K-line kernel: every collaborator is duck-typed (any bus with
``subscribe``/``emit``, any organ exposing an async ``on_pressure`` handler), so
this module is unit-testable with fakes and never triggers the kernel's
import-time side effects.

Three parts:
  * ``PressureSignalEmitter`` — turns raw, level-based observations into typed,
    EDGE-TRIGGERED signals. A signal fires only on a state TRANSITION (rising:
    became-active; falling: cleared), never on every poll — so a sustained
    pressure does not spam the organs.
  * ``EventActivationDispatcher`` — subscribes to the SupervisorEventBus and
    routes pressure signals to the registered organs entirely asynchronously and
    fail-soft (one slow/broken organ never blocks or breaks the others).
  * Shadow Mode (``JARVIS_RESILIENCE_SHADOW_MODE``, default-TRUE) — the fail-safe.
    A reanimated organ wraps every DANGEROUS action (process kill, load shed) in
    ``shadow_guard``: in shadow mode it logs ``[SHADOW MODE] Would have ...`` and
    yields the trapped sentinel instead of executing. The muscle wakes and
    reasons, but cannot act on the world until the operator clears shadow mode.

Pure/async, env-driven, NEVER raises on the dispatch hot path.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("jarvis.cybernetic_reanimation")

_ENV_SHADOW = "JARVIS_RESILIENCE_SHADOW_MODE"


class PressureSignalType(str, Enum):
    """The typed, edge-triggered signals the nervous system carries."""
    RESOURCE_PRESSURE = "resource_pressure"      # CPU/mem/fd headroom crossed a threshold
    ANOMALY_DETECTED = "anomaly_detected"        # behavioural anomaly / health breach
    COMPONENT_DEGRADED = "component_degraded"    # a subsystem reported degraded


class SignalEdge(str, Enum):
    RISING = "rising"    # condition became active
    FALLING = "falling"  # condition cleared


@dataclass(frozen=True)
class PressureSignal:
    """An immutable, edge-triggered pressure signal."""
    type: PressureSignalType
    source: str
    edge: SignalEdge
    severity: str = "warning"          # info | warning | critical
    detail: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shadow Mode — the fail-safe
# ---------------------------------------------------------------------------

class _ShadowTrapped:
    """Singleton sentinel returned by shadow_guard when an action is trapped."""
    __slots__ = ()
    def __repr__(self) -> str:  # noqa: D401
        return "<SHADOW_TRAPPED>"


SHADOW_TRAPPED = _ShadowTrapped()


def resilience_shadow_mode_enabled() -> bool:
    """Master fail-safe (default-TRUE). While on, reanimated organs reason but do
    NOT execute world-affecting actions. NEVER raises."""
    try:
        return os.getenv(_ENV_SHADOW, "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return True  # fail SAFE — default to shadow on error


def shadow_guard(
    action_desc: str,
    execute: Callable[[], Any],
    *,
    log: Optional[logging.Logger] = None,
) -> Any:
    """Gate a DANGEROUS action behind shadow mode. In shadow mode: log
    ``[SHADOW MODE] Would have <action_desc>`` and return ``SHADOW_TRAPPED``
    WITHOUT calling ``execute``. Otherwise: return ``execute()``. The single
    chokepoint every reanimated organ routes its kill/shed/restart through."""
    _log = log or logger
    if resilience_shadow_mode_enabled():
        _log.warning("[SHADOW MODE] Would have %s", action_desc)
        return SHADOW_TRAPPED
    return execute()


async def shadow_guard_async(
    action_desc: str,
    execute: Callable[[], Awaitable[Any]],
    *,
    log: Optional[logging.Logger] = None,
) -> Any:
    """Async variant of :func:`shadow_guard` for coroutine actions."""
    _log = log or logger
    if resilience_shadow_mode_enabled():
        _log.warning("[SHADOW MODE] Would have %s", action_desc)
        return SHADOW_TRAPPED
    return await execute()


# ---------------------------------------------------------------------------
# PressureSignalEmitter — level observations -> edge-triggered signals
# ---------------------------------------------------------------------------

class PressureSignalEmitter:
    """Converts level-based observations into edge-triggered :class:`PressureSignal`
    emissions. ``emit_fn`` is any callable taking a PressureSignal (e.g. the
    SupervisorEventBus.emit bridge). State is tracked per (type, source) so a
    sustained condition fires exactly one RISING edge and, on clearing, one
    FALLING edge."""

    def __init__(self, emit_fn: Callable[[PressureSignal], Any]) -> None:
        self._emit = emit_fn
        self._active: Dict[Tuple[PressureSignalType, str], bool] = {}

    def observe(
        self,
        signal_type: PressureSignalType,
        source: str,
        active: bool,
        *,
        severity: str = "warning",
        detail: Optional[Dict[str, Any]] = None,
    ) -> Optional[PressureSignal]:
        """Record a level observation. Emit + return a signal ONLY on an edge
        (transition); return None when the state is unchanged. NEVER raises into
        the caller (a broken emit_fn is swallowed after the state update)."""
        key = (signal_type, source)
        was = self._active.get(key, False)
        if bool(active) == was:
            return None  # no edge — stay silent
        self._active[key] = bool(active)
        sig = PressureSignal(
            type=signal_type,
            source=source,
            edge=SignalEdge.RISING if active else SignalEdge.FALLING,
            severity=severity,
            detail=dict(detail or {}),
        )
        try:
            self._emit(sig)
        except Exception:  # noqa: BLE001 — emission is best-effort
            logger.debug("[Reanimation] emit_fn raised for %s", sig, exc_info=True)
        return sig

    def reset(self) -> None:
        self._active.clear()


# ---------------------------------------------------------------------------
# EventActivationDispatcher — route signals to reanimated organs (async)
# ---------------------------------------------------------------------------

OrganHandler = Callable[[PressureSignal], Awaitable[Any]]


class EventActivationDispatcher:
    """Routes :class:`PressureSignal` s to registered resilience organs,
    asynchronously and fail-soft. Each organ subscribes to the signal types it
    cares about; an organ that raises or hangs never breaks the others."""

    def __init__(self) -> None:
        self._organs: Dict[PressureSignalType, List[Tuple[str, OrganHandler]]] = {}

    def register_organ(
        self,
        name: str,
        handler: OrganHandler,
        signal_types: List[PressureSignalType],
    ) -> None:
        """Wake an organ: route the given signal types to its async handler."""
        for st in signal_types:
            self._organs.setdefault(st, []).append((name, handler))
        logger.info(
            "[Reanimation] organ %s registered for %s",
            name, [s.value for s in signal_types],
        )

    def organ_count(self) -> int:
        return len({n for hs in self._organs.values() for (n, _) in hs})

    async def dispatch(self, signal: PressureSignal) -> int:
        """Deliver a signal to every organ subscribed to its type. Returns the
        number of organs invoked. Fail-soft: per-organ exceptions are logged +
        swallowed. NEVER raises."""
        handlers = list(self._organs.get(signal.type, ()))
        delivered = 0
        for name, handler in handlers:
            try:
                await handler(signal)
                delivered += 1
            except Exception:  # noqa: BLE001 — one organ never breaks the bus
                logger.exception("[Reanimation] organ %s raised on %s", name, signal.type)
        return delivered

    def attach_to_bus(self, bus: Any, *, extract: Callable[[Any], Optional[PressureSignal]]) -> None:
        """Bridge a SupervisorEventBus to this dispatcher. ``extract`` maps a raw
        bus event to a PressureSignal (or None to ignore). The subscription is
        non-blocking; dispatch is scheduled on the running loop. NEVER raises."""
        import asyncio

        def _on_event(event: Any) -> None:
            try:
                sig = extract(event)
                if sig is None:
                    return
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return  # no loop — bus events only meaningful under the async kernel
                loop.create_task(self.dispatch(sig))
            except Exception:  # noqa: BLE001
                logger.debug("[Reanimation] bus bridge swallowed an event", exc_info=True)

        bus.subscribe(_on_event)
