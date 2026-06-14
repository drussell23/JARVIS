"""Cybernetic Reanimation (Phase C) — bus→activation bridge + pressure emitters.

Standalone + injectable: never imports unified_supervisor at module scope, so it
is unit-testable in environments where the kernel import is blocked. The kernel
constructs the layer behind the JARVIS_RESILIENCE_REANIMATION_ENABLED flag and
passes its live SupervisorEventBus + SystemServiceRegistry.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger("resilience_reanimation")


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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


# ===========================================================================
# C.3 — Organ adapters: thin, fail-soft bus→organ-method bridges
# ===========================================================================
#
# Each adapter wraps ONE real resilience organ and exposes a single
# ``async on_event(payload: dict)`` coroutine that maps the event payload to
# the organ's real reaction method. Adapters add NO policy and own NO state —
# they are the missing wire between an emitted event and a dormant organ's
# real action method. Every organ call is wrapped: on error we log + continue
# (a dead organ must never wedge the bus fan-out).
#
# Payload contract (produced by PressureSignalEmitter / feedback emitters):
#   resource_pressure  -> {"signal","level"[, "memory"]}
#   anomaly_detected   -> {"category","features"[, "metadata"]}
#   component_degraded -> {"component","health_score","failure_probability",
#                          "degraded"[, "metrics"]}


class _OrganAdapter:
    """Common fail-soft scaffold for organ adapters."""

    name: str = "organ"
    #: event_type.value strings this adapter reacts to.
    trigger_events: tuple = ()

    def __init__(self, organ: Any) -> None:
        self._organ = organ

    async def _safe(self, coro_or_value: Any) -> Any:
        """Await a value if it is awaitable; swallow + log on failure."""
        try:
            if asyncio.iscoroutine(coro_or_value):
                return await coro_or_value
            return coro_or_value
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — adapters are fail-soft
            logger.warning(
                "[Reanimation] %s organ call failed: %r", self.name, err
            )
            return None

    async def on_event(self, payload: dict) -> None:  # pragma: no cover - base
        raise NotImplementedError


class GracefulDegradationAdapter(_OrganAdapter):
    """resource_pressure -> GracefulDegradationManager._check_resources().

    The manager samples real resource pressure itself and adjusts its
    degradation level; the event is the *trigger* to re-evaluate now rather
    than wait for the next poll interval.
    """

    name = "graceful_degradation"
    trigger_events = ("resource_pressure",)

    async def on_event(self, payload: dict) -> None:
        try:
            await self._safe(self._organ._check_resources())
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.warning("[Reanimation] %s on_event failed: %r", self.name, err)


class LoadSheddingAdapter(_OrganAdapter):
    """resource_pressure -> LoadSheddingController.record_load(level)."""

    name = "load_shedding"
    trigger_events = ("resource_pressure",)

    async def on_event(self, payload: dict) -> None:
        try:
            level = float(payload.get("level", 0.0) or 0.0)
            await self._safe(self._organ.record_load(level))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.warning("[Reanimation] %s on_event failed: %r", self.name, err)


class AutoScalingAdapter(_OrganAdapter):
    """resource_pressure -> AutoScalingController.record_metrics(); evaluate().

    ``level`` is the primary pressure signal (cpu); ``memory`` (optional, a
    0..1 fraction) maps to memory_percent. Fractions are scaled to percent
    because the organ expects percentages.
    """

    name = "auto_scaling"
    trigger_events = ("resource_pressure",)

    async def on_event(self, payload: dict) -> None:
        try:
            cpu = float(payload.get("level", 0.0) or 0.0) * 100.0
            mem = float(payload.get("memory", 0.0) or 0.0) * 100.0
            await self._safe(
                self._organ.record_metrics(cpu_percent=cpu, memory_percent=mem)
            )
            await self._safe(self._organ.evaluate())
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.warning("[Reanimation] %s on_event failed: %r", self.name, err)


class AnomalyDetectorAdapter(_OrganAdapter):
    """anomaly_detected -> AnomalyDetector.record_observation(category, features)."""

    name = "anomaly_detector"
    trigger_events = ("anomaly_detected",)

    async def on_event(self, payload: dict) -> None:
        try:
            category = payload.get("category", "unknown")
            features = payload.get("features") or {}
            await self._safe(self._organ.record_observation(category, features))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.warning("[Reanimation] %s on_event failed: %r", self.name, err)


class ProcessHealthPredictorAdapter(_OrganAdapter):
    """component_degraded -> ProcessHealthPredictor.record_metrics(component, metrics)."""

    name = "health_predictor"
    trigger_events = ("component_degraded",)

    async def on_event(self, payload: dict) -> None:
        try:
            component = payload.get("component", "unknown")
            metrics = payload.get("metrics") or {}
            await self._safe(self._organ.record_metrics(component, metrics))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.warning("[Reanimation] %s on_event failed: %r", self.name, err)


class SelfHealingAdapter(_OrganAdapter):
    """component_degraded -> SelfHealingOrchestrator.check_and_remediate(...)."""

    name = "self_healing"
    trigger_events = ("component_degraded",)

    async def on_event(self, payload: dict) -> None:
        try:
            component = payload.get("component", "unknown")
            health = float(payload.get("health_score", 1.0) or 0.0)
            fail_prob = float(payload.get("failure_probability", 0.0) or 0.0)
            await self._safe(
                self._organ.check_and_remediate(component, health, fail_prob)
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.warning("[Reanimation] %s on_event failed: %r", self.name, err)


class CircuitBreakerAdapter(_OrganAdapter):
    """component_degraded -> AdvancedCircuitBreaker.record_failure()/record_success().

    A degraded component is a failure signal for the breaker; a healthy
    component_degraded=False event records a success (recovery hint).
    """

    name = "circuit_breaker"
    trigger_events = ("component_degraded",)

    async def on_event(self, payload: dict) -> None:
        try:
            if payload.get("degraded", True):
                await self._safe(self._organ.record_failure())
            else:
                await self._safe(self._organ.record_success())
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.warning("[Reanimation] %s on_event failed: %r", self.name, err)


# Default contract trigger mapping per organ key. Mirrors each adapter's
# ``trigger_events``; used both for adapter fan-out and for the Activation
# contracts registered on the service registry.
_ADAPTER_FACTORIES: Dict[str, Any] = {
    "graceful_degradation": GracefulDegradationAdapter,
    "load_shedding": LoadSheddingAdapter,
    "auto_scaling": AutoScalingAdapter,
    "anomaly_detector": AnomalyDetectorAdapter,
    "health_predictor": ProcessHealthPredictorAdapter,
    "self_healing": SelfHealingAdapter,
    "circuit_breaker": CircuitBreakerAdapter,
}


class _MiniContract:
    """Duck-typed ActivationContract for the standalone (no-kernel) path.

    When the real ``ActivationContract`` dataclass is injectable (kernel path)
    the layer prefers it; in unit tests / sandbox we attach this minimal
    stand-in carrying just ``trigger_events`` so ``iter_event_driven()`` and
    the EventActivationDispatcher can still match it.
    """

    def __init__(self, trigger_events: List[str]) -> None:
        self.trigger_events = list(trigger_events)
        self.dependency_gate: List[str] = []


class _MiniDescriptor:
    """Duck-typed ServiceDescriptor stand-in for the standalone path."""

    def __init__(self, name: str, contract: Any) -> None:
        self.name = name
        self.activation_contract = contract
        self.activation_mode = "event_driven"


class ReanimationLayer:
    """Wires the 7 resilience organs into the event bus + service registry.

    On ``wire()`` it:
      1. Builds one adapter per enabled organ.
      2. Subscribes a single fan-out handler to the bus that, per event,
         dispatches to every adapter whose ``trigger_events`` contains the
         event type, passing the payload extracted from event metadata.
      3. Registers one descriptor per enabled organ carrying an
         ActivationContract(trigger_events=...) so the C.1
         EventActivationDispatcher can lazy-activate dormant organs.

    Per-organ gating: ``JARVIS_REANIMATE_<ORGAN>_ENABLED`` (default true).
    The ``enabled_flags`` constructor arg overrides env (test seam).

    ``descriptor_factory`` / ``contract_factory`` are optional injection seams
    so the kernel can pass the real ``ServiceDescriptor`` / ``ActivationContract``
    constructors; absent them the layer uses duck-typed stand-ins (sandbox-safe).
    """

    def __init__(
        self,
        event_bus: Any,
        service_registry: Any,
        organs: Dict[str, Any],
        *,
        enabled_flags: Optional[Dict[str, bool]] = None,
        descriptor_factory: Optional[Callable[..., Any]] = None,
        contract_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._bus = event_bus
        self._registry = service_registry
        self._organs = dict(organs)
        self._enabled_flags = dict(enabled_flags or {})
        self._descriptor_factory = descriptor_factory
        self._contract_factory = contract_factory
        self._adapters: Dict[str, _OrganAdapter] = {}
        self._wired = False

    # -- gating ------------------------------------------------------------
    def _organ_enabled(self, key: str) -> bool:
        if key in self._enabled_flags:
            return bool(self._enabled_flags[key])
        return _env_flag(f"JARVIS_REANIMATE_{key.upper()}_ENABLED", True)

    # -- wiring ------------------------------------------------------------
    def wire(self) -> None:
        if self._wired:
            return
        for key, factory in _ADAPTER_FACTORIES.items():
            organ = self._organs.get(key)
            if organ is None:
                continue
            if not self._organ_enabled(key):
                logger.info("[Reanimation] organ %s disabled by flag", key)
                continue
            adapter = factory(organ)
            self._adapters[key] = adapter
            self._register_contract(key, list(adapter.trigger_events))
        if self._adapters:
            self._bus.subscribe(self._on_event)
        self._wired = True
        logger.info(
            "[Reanimation] layer wired: organs=%s", sorted(self._adapters)
        )

    def _register_contract(self, key: str, triggers: List[str]) -> None:
        try:
            if self._contract_factory is not None:
                contract = self._contract_factory(
                    trigger_events=triggers, dependency_gate=[]
                )
            else:
                contract = _MiniContract(triggers)
            if self._descriptor_factory is not None:
                desc = self._descriptor_factory(key, contract)
            else:
                desc = _MiniDescriptor(key, contract)
            self._registry.register(desc)
        except Exception as err:  # noqa: BLE001 — registration must never wedge boot
            logger.warning(
                "[Reanimation] register contract for %s failed: %r", key, err
            )

    @staticmethod
    def _extract_payload(event: Any) -> dict:
        meta = getattr(event, "metadata", None)
        if isinstance(meta, dict):
            return meta
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict):
            return payload
        return {}

    async def _on_event(self, event: Any) -> None:
        try:
            etype = event.event_type.value
        except Exception:  # noqa: BLE001 — malformed event
            return
        payload = self._extract_payload(event)
        for adapter in self._adapters.values():
            if etype not in adapter.trigger_events:
                continue
            try:
                await adapter.on_event(payload)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — isolate per adapter
                logger.warning(
                    "[Reanimation] adapter %s dispatch failed: %r",
                    adapter.name, err,
                )
