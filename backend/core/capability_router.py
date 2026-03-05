# backend/core/capability_router.py
"""
CapabilityRouter - Routes requests based on available capabilities.

This module provides:
- CircuitBreaker: Per-provider circuit breaker for fault tolerance
- RoutingDecision: Detailed routing decision with fallback info
- CapabilityRouter: Routes requests to healthy providers with automatic fallback

Usage:
    from backend.core.capability_router import CapabilityRouter, get_capability_router
    from backend.core.component_registry import get_component_registry

    # Create router backed by registry
    registry = get_component_registry()
    router = CapabilityRouter(registry)

    # Check capability availability
    if router.is_capability_available("inference"):
        provider = await router.route("inference")

    # Call with automatic fallback
    result = await router.call_with_fallback(
        "inference",
        prompt="Hello"
    )
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.component_registry import ComponentRegistry

logger = logging.getLogger("jarvis.capability_router")


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"        # Normal operation - requests flow through
    OPEN = "open"            # Failing - reject requests immediately
    HALF_OPEN = "half_open"  # Testing if recovered - allow limited requests


@dataclass
class CircuitBreaker:
    """
    Per-provider circuit breaker for fault tolerance.

    Implements the circuit breaker pattern to prevent cascading failures.
    When a provider fails repeatedly, the circuit opens and requests are
    rejected without calling the provider. After a timeout, the circuit
    transitions to half-open state to test if the provider has recovered.

    Attributes:
        provider: Name of the provider this breaker protects
        state: Current circuit state (CLOSED, OPEN, HALF_OPEN)
        failure_count: Consecutive failures since last success
        success_count: Consecutive successes in half-open state
        last_failure: Timestamp of most recent failure
        last_state_change: Timestamp of last state transition
        failure_threshold: Failures needed to open circuit
        success_threshold: Successes needed to close from half-open
        timeout_seconds: Time before testing recovery
    """
    provider: str
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure: Optional[datetime] = None
    last_state_change: Optional[datetime] = None

    # Configuration
    failure_threshold: int = 5
    success_threshold: int = 3  # Successes needed to close from half-open
    timeout_seconds: float = 60.0  # Time before trying again

    def record_success(self) -> None:
        """
        Record successful call.

        Resets failure count and increments success count.
        If in half-open state, closes circuit after enough successes.
        """
        self.failure_count = 0
        self.success_count += 1

        if self.state == CircuitState.HALF_OPEN:
            if self.success_count >= self.success_threshold:
                self.state = CircuitState.CLOSED
                self.last_state_change = datetime.now()
                logger.info(f"Circuit breaker for {self.provider} CLOSED (recovered)")

    def record_failure(self) -> None:
        """
        Record failed call.

        Increments failure count and resets success count.
        If in closed state and threshold reached, opens circuit.
        If in half-open state, reopens circuit.
        """
        self.failure_count += 1
        self.success_count = 0
        self.last_failure = datetime.now()

        if self.state == CircuitState.CLOSED:
            if self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.last_state_change = datetime.now()
                logger.warning(
                    f"Circuit breaker for {self.provider} OPEN "
                    f"(failures: {self.failure_count})"
                )
        elif self.state == CircuitState.HALF_OPEN:
            # Failure in half-open state reopens circuit
            self.state = CircuitState.OPEN
            self.last_state_change = datetime.now()
            logger.warning(
                f"Circuit breaker for {self.provider} re-OPEN "
                f"(failed during recovery test)"
            )

    def can_execute(self) -> bool:
        """
        Check if execution is allowed.

        Returns:
            True if request can proceed, False if circuit is open
        """
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if timeout has passed
            if self.last_state_change:
                elapsed = (datetime.now() - self.last_state_change).total_seconds()
                if elapsed >= self.timeout_seconds:
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    self.last_state_change = datetime.now()
                    logger.info(
                        f"Circuit breaker for {self.provider} HALF_OPEN (testing)"
                    )
                    return True
            return False

        # HALF_OPEN allows requests to test if provider has recovered
        return True

    def reset(self) -> None:
        """Reset circuit breaker to initial state."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure = None
        self.last_state_change = datetime.now()
        logger.info(f"Circuit breaker for {self.provider} RESET")


@dataclass
class RoutingDecision:
    """
    Result of a routing decision.

    Contains all information about how a capability request was routed,
    including whether a fallback was used and why.

    Attributes:
        capability: The capability that was requested
        provider: Name of the selected provider, or None if unavailable
        is_fallback: Whether the selected provider is a fallback
        fallback_reason: Explanation of why fallback was used
        circuit_state: State of the circuit breaker for the provider
    """
    capability: str
    provider: Optional[str]
    is_fallback: bool
    fallback_reason: Optional[str] = None
    circuit_state: Optional[CircuitState] = None


class CapabilityRouter:
    """Compatibility shim -- delegates to ModelRouter via UnifiedModelServing.

    The CapabilityRouter's routing logic has been absorbed into
    ModelRouter (v290.1). This shim exists for import compatibility only.

    New code should use get_model_serving().router directly.
    """

    def __init__(self, registry: 'ComponentRegistry' = None):
        self.registry = registry
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}
        self._provider_callables: Dict[str, Callable[..., Awaitable[Any]]] = {}
        self._fallback_callables: Dict[str, Callable[..., Awaitable[Any]]] = {}
        self._capability_providers: Dict[str, List[str]] = {}
        self.logger = logging.getLogger("jarvis.capability_router")
        self.logger.info("CapabilityRouter: using ModelRouter delegation shim")

    # -----------------------------------------------------------------
    # Registration (preserved for backward compatibility)
    # -----------------------------------------------------------------

    def register_provider_callable(
        self,
        capability: str,
        provider: str,
        callable: Callable[..., Awaitable[Any]]
    ) -> None:
        """Register callable for a provider (preserved for compatibility)."""
        key = f"{capability}:{provider}"
        self._provider_callables[key] = callable
        self.logger.debug(f"Registered callable for {key}")

    def register_fallback_callable(
        self,
        capability: str,
        callable: Callable[..., Awaitable[Any]]
    ) -> None:
        """Register fallback callable (preserved for compatibility)."""
        self._fallback_callables[capability] = callable
        self.logger.debug(f"Registered fallback callable for {capability}")

    def register_provider(self, capability: str, provider: str) -> None:
        """Register a provider for a capability (enterprise_hooks compat)."""
        if capability not in self._capability_providers:
            self._capability_providers[capability] = []
        if provider not in self._capability_providers[capability]:
            self._capability_providers[capability].insert(0, provider)
            self.logger.debug(f"Registered provider {provider} for capability {capability}")

    def register_fallback(self, capability: str, provider: str) -> None:
        """Register a fallback provider for a capability."""
        if capability not in self._capability_providers:
            self._capability_providers[capability] = []
        if provider not in self._capability_providers[capability]:
            self._capability_providers[capability].append(provider)
            self.logger.debug(f"Registered fallback {provider} for capability {capability}")

    # -----------------------------------------------------------------
    # Queries
    # -----------------------------------------------------------------

    def is_capability_available(self, capability: str) -> bool:
        """Check via registry if available."""
        if self.registry and hasattr(self.registry, 'has_capability'):
            return self.registry.has_capability(capability)
        if self.registry and hasattr(self.registry, 'is_capability_available'):
            return self.registry.is_capability_available(capability)
        return False

    async def route(self, capability: str) -> Optional[str]:
        """Delegate to ModelRouter when possible, fallback to local callables."""
        decision = await self.get_routing_decision(capability)
        return decision.provider

    async def get_routing_decision(
        self, capability: str, preferred_provider: Optional[str] = None
    ) -> RoutingDecision:
        """Get routing decision, delegating to ModelRouter when available."""
        try:
            from backend.intelligence.unified_model_serving import get_model_serving
            serving = await get_model_serving()
            if serving and hasattr(serving, 'router'):
                # Use preferred_provider if given, otherwise report delegation
                provider = preferred_provider or "model_router"
                return RoutingDecision(
                    capability=capability,
                    provider=provider,
                    is_fallback=False,
                )
        except Exception:
            self.logger.debug(f"CapabilityRouter shim: delegation failed for {capability}")

        # Fallback to local callable if registered
        key = next((k for k in self._provider_callables if k.startswith(f"{capability}:")), None)
        if key:
            return RoutingDecision(
                capability=capability,
                provider=key.split(":")[1],
                is_fallback=False,
            )
        return RoutingDecision(
            capability=capability,
            provider=None,
            is_fallback=True,
            fallback_reason="no_provider_available",
        )

    async def call_with_fallback(
        self,
        capability: str,
        *args,
        **kwargs
    ) -> Any:
        """Call with fallback -- preserved for backward compatibility."""
        decision = await self.get_routing_decision(capability)

        if decision.provider:
            key = f"{capability}:{decision.provider}"
            if key in self._provider_callables:
                try:
                    breaker = self._get_or_create_breaker(decision.provider)
                    result = await self._provider_callables[key](*args, **kwargs)
                    breaker.record_success()
                    return result
                except Exception as e:
                    breaker = self._get_or_create_breaker(decision.provider)
                    breaker.record_failure()
                    self.logger.warning(f"Provider {decision.provider} failed: {e}")

        # Use fallback
        if capability in self._fallback_callables:
            self.logger.info(f"Using fallback for capability {capability}")
            return await self._fallback_callables[capability](*args, **kwargs)

        raise RuntimeError(
            f"No provider or fallback available for capability {capability}"
        )

    def get_fallback_chain(self, capability: str) -> List[str]:
        """Get ordered list of fallback providers for a capability."""
        chain = []
        if self.registry:
            primary = self.registry.get_provider(capability)
            if primary:
                chain.append(primary)
                try:
                    defn = self.registry.get(primary)
                    if hasattr(defn, 'fallback_for_capabilities'):
                        for cap, fallback in defn.fallback_for_capabilities.items():
                            if cap == capability or capability in cap:
                                if fallback not in chain:
                                    chain.append(fallback)
                except (KeyError, AttributeError):
                    pass
        if capability in self._fallback_callables:
            chain.append("fallback")
        return chain

    # -----------------------------------------------------------------
    # Circuit breaker operations (preserved for compatibility)
    # -----------------------------------------------------------------

    def _get_or_create_breaker(self, provider: str) -> CircuitBreaker:
        """Get or create circuit breaker for provider."""
        if provider not in self._circuit_breakers:
            self._circuit_breakers[provider] = CircuitBreaker(provider=provider)
        return self._circuit_breakers[provider]

    def reset_circuit_breaker(self, provider: str) -> None:
        """Reset circuit breaker for a provider."""
        if provider in self._circuit_breakers:
            self._circuit_breakers[provider].reset()

    def record_success(self, capability: str) -> None:
        """Record a successful call for a capability."""
        provider = None
        if self.registry:
            provider = self.registry.get_provider(capability)
        if not provider:
            providers = self._capability_providers.get(capability, [])
            if providers:
                provider = providers[0]
        if provider:
            breaker = self._get_or_create_breaker(provider)
            breaker.record_success()

    def record_failure(self, capability: str, error: Exception) -> None:
        """Record a failed call for a capability."""
        provider = None
        if self.registry:
            provider = self.registry.get_provider(capability)
        if not provider:
            providers = self._capability_providers.get(capability, [])
            if providers:
                provider = providers[0]
        if provider:
            breaker = self._get_or_create_breaker(provider)
            breaker.record_failure()
            self.logger.warning(f"Recorded failure for {capability} via {provider}: {error}")

    def get_circuit_breaker_status_for_capability(
        self, capability: str
    ) -> Optional[Dict[str, Any]]:
        """Get circuit breaker status for a specific capability."""
        provider = None
        if self.registry:
            provider = self.registry.get_provider(capability)
        if not provider:
            providers = self._capability_providers.get(capability, [])
            if providers:
                provider = providers[0]
        if provider and provider in self._circuit_breakers:
            cb = self._circuit_breakers[provider]
            return {
                "provider": provider,
                "state": cb.state.value,
                "failure_count": cb.failure_count,
                "success_count": cb.success_count,
                "last_failure": cb.last_failure.isoformat() if cb.last_failure else None,
                "last_state_change": (
                    cb.last_state_change.isoformat() if cb.last_state_change else None
                ),
            }
        return None

    def get_circuit_breaker_status(
        self, capability: Optional[str] = None
    ) -> Union[Dict[str, Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Get status of circuit breakers."""
        if capability is not None:
            return self.get_circuit_breaker_status_for_capability(capability)
        return {
            name: {
                "state": cb.state.value,
                "failure_count": cb.failure_count,
                "success_count": cb.success_count,
                "last_failure": cb.last_failure.isoformat() if cb.last_failure else None,
                "last_state_change": (
                    cb.last_state_change.isoformat() if cb.last_state_change else None
                ),
            }
            for name, cb in self._circuit_breakers.items()
        }


def get_capability_router(registry: 'ComponentRegistry') -> CapabilityRouter:
    """
    Factory function for CapabilityRouter.

    Args:
        registry: ComponentRegistry for provider lookup

    Returns:
        CapabilityRouter instance
    """
    return CapabilityRouter(registry)
