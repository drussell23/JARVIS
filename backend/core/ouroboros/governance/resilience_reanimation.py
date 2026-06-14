"""Cybernetic Reanimation (Phase C) — bus→activation bridge + pressure emitters.

Standalone + injectable: never imports unified_supervisor at module scope, so it
is unit-testable in environments where the kernel import is blocked. The kernel
constructs the layer behind the JARVIS_RESILIENCE_REANIMATION_ENABLED flag and
passes its live SupervisorEventBus + SystemServiceRegistry.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Iterable

logger = logging.getLogger("resilience_reanimation")


class EventActivationDispatcher:
    """Subscribes to the supervisor event bus and activates registry services
    whose ActivationContract.trigger_events match the emitted event type.

    Adds NO new policy — the registry's gates (dependency/budget/backoff/rate)
    remain authoritative. This is the missing wire, nothing more.
    """

    def __init__(self, event_bus: Any, service_registry: Any) -> None:
        self._bus = event_bus
        self._registry = service_registry
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._bus.subscribe(self._on_event)
        self._started = True
        logger.info("[Reanimation] dispatcher subscribed to event bus")

    async def _on_event(self, event: Any) -> None:
        try:
            etype = event.event_type.value
        except Exception:  # noqa: BLE001 — malformed event, ignore
            return
        try:
            descriptors = list(self._registry.iter_event_driven())
        except Exception as err:  # noqa: BLE001 — fail-soft
            logger.warning("[Reanimation] registry iteration failed: %r", err)
            return
        activated = []
        for desc in descriptors:
            contract = getattr(desc, "activation_contract", None)
            triggers = getattr(contract, "trigger_events", None) or []
            if etype not in triggers:
                continue
            name = getattr(desc, "name", "")
            try:
                ok = await self._registry.activate_service(name)
                if ok:
                    activated.append(name)
            except Exception as err:  # noqa: BLE001 — isolate per service
                logger.warning(
                    "[Reanimation] activate_service(%s) failed: %r", name, err
                )
        if activated:
            logger.info(
                "[Reanimation] event=%s activated=%s", etype, activated
            )


class PressureSignalEmitter:
    """Edge-triggered pressure sampler. Emits a typed event only when a signal
    transitions from below to above its threshold (never every tick). Fail-soft.
    """

    def __init__(self, sampler, emit, thresholds, signal_event):
        self._sampler = sampler          # () -> {signal: level}
        self._emit = emit                # (event_type_value, payload) -> None
        self._thresholds = dict(thresholds)
        self._signal_event = dict(signal_event)
        self._above = {}                 # signal -> bool (last state)

    async def tick(self) -> None:
        try:
            sample = self._sampler() or {}
        except Exception as err:  # noqa: BLE001 — fail-soft
            logger.warning("[Reanimation] pressure sample failed: %r", err)
            return
        for signal, level in sample.items():
            thr = self._thresholds.get(signal)
            if thr is None:
                continue
            now_above = level >= thr
            was_above = self._above.get(signal, False)
            if now_above and not was_above:
                etype = self._signal_event.get(signal)
                if etype:
                    try:
                        self._emit(etype, {"signal": signal, "level": level})
                    except Exception as err:  # noqa: BLE001
                        logger.warning("[Reanimation] emit failed: %r", err)
            self._above[signal] = now_above
