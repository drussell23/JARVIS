"""
JARVIS Capability-Based Readiness Registry (v295.0)
=====================================================
Replaces the single boolean "ready or not" signal with a domain-partitioned
capability graph that knows:

  - Which domains exist and their dependency order
  - Which domains are satisfied, degraded, or unavailable
  - What the minimum bar is to advertise "ready" to the frontend

Domains (ordered by dependency):
  BACKEND_HTTP   — FastAPI process is accepting HTTP connections
  WEBSOCKET      — WS endpoint is live and accepting upgrade
  MODEL_ROUTER   — At least one inference provider is routed and healthy
  VOICE          — STT + TTS pipeline is functional (optional for base ready)
  TRINITY        — Memory / reasoning system is connected (optional for base ready)
  UI             — Frontend loading page has confirmed its own readiness

A "fully operational" system has all six domains UP.
A "minimally operational" system has BACKEND_HTTP + WEBSOCKET + MODEL_ROUTER UP.
Domains may be individually DEGRADED (available but impaired) or BLOCKED (hard dep missing).

Usage (daemon path):
    registry = get_capability_registry()
    registry.mark_satisfied(CapabilityDomain.BACKEND_HTTP, detail="port 8010 accepting")
    registry.mark_degraded(CapabilityDomain.VOICE, detail="TTS unavailable")

    if registry.is_minimally_operational():
        # safe to advertise ready
        ...

    payload = registry.to_health_dict()   # for /health/ready endpoint
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Callable, Dict, List, Optional, Set

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain definitions
# ---------------------------------------------------------------------------

class CapabilityDomain(str, Enum):
    BACKEND_HTTP   = "backend_http"
    WEBSOCKET      = "websocket"
    MODEL_ROUTER   = "model_router"
    VOICE          = "voice"
    TRINITY        = "trinity"
    UI             = "ui"


class DomainStatus(str, Enum):
    PENDING    = "pending"     # Not yet evaluated
    SATISFIED  = "satisfied"   # Fully operational
    DEGRADED   = "degraded"    # Operational with reduced capability
    UNAVAILABLE = "unavailable" # Hard failure / dependency missing
    BLOCKED    = "blocked"     # Dependency not yet met


# Domains required for minimal operational advertisement.
# All three must reach at least DEGRADED to call is_minimally_operational() True.
_MINIMUM_OPERATIONAL_DOMAINS: frozenset[CapabilityDomain] = frozenset({
    CapabilityDomain.BACKEND_HTTP,
    CapabilityDomain.WEBSOCKET,
    CapabilityDomain.MODEL_ROUTER,
})

# Dependency graph: a domain may not become SATISFIED until all its deps
# are at least DEGRADED.  Checked only for informational BLOCKED status —
# it does NOT prevent calling mark_satisfied() externally.
_DEPENDENCIES: Dict[CapabilityDomain, List[CapabilityDomain]] = {
    CapabilityDomain.BACKEND_HTTP:  [],
    CapabilityDomain.WEBSOCKET:     [CapabilityDomain.BACKEND_HTTP],
    CapabilityDomain.MODEL_ROUTER:  [CapabilityDomain.BACKEND_HTTP],
    CapabilityDomain.VOICE:         [CapabilityDomain.BACKEND_HTTP],
    CapabilityDomain.TRINITY:       [CapabilityDomain.MODEL_ROUTER],
    CapabilityDomain.UI:            [CapabilityDomain.WEBSOCKET],
}


# ---------------------------------------------------------------------------
# Domain state record
# ---------------------------------------------------------------------------

class _DomainState:
    __slots__ = ("status", "detail", "since_monotonic", "since_wall")

    def __init__(self) -> None:
        self.status: DomainStatus = DomainStatus.PENDING
        self.detail: str = ""
        self.since_monotonic: float = 0.0
        self.since_wall: float = 0.0

    def update(self, status: DomainStatus, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        self.since_monotonic = time.monotonic()
        self.since_wall = time.time()

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "detail": self.detail,
            "since": self.since_wall,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class CapabilityRegistry:
    """Thread-safe (and asyncio-safe) domain capability tracker.

    Safe to call from:
      - The asyncio event loop (non-blocking property reads)
      - Background threads (all mutations go through threading.Lock acquired
        synchronously — never blocks the event loop meaningfully)
    """

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._domains: Dict[CapabilityDomain, _DomainState] = {
            d: _DomainState() for d in CapabilityDomain
        }
        self._listeners: List[Callable[[CapabilityDomain, DomainStatus, DomainStatus], None]] = []
        self._ready_event: Optional[asyncio.Event] = None  # lazily bound to event loop

    # ------------------------------------------------------------------
    # Mutation API
    # ------------------------------------------------------------------

    def mark_satisfied(self, domain: CapabilityDomain, *, detail: str = "") -> None:
        self._set(domain, DomainStatus.SATISFIED, detail)

    def mark_degraded(self, domain: CapabilityDomain, *, detail: str = "") -> None:
        self._set(domain, DomainStatus.DEGRADED, detail)

    def mark_unavailable(self, domain: CapabilityDomain, *, detail: str = "") -> None:
        self._set(domain, DomainStatus.UNAVAILABLE, detail)

    def mark_pending(self, domain: CapabilityDomain, *, detail: str = "") -> None:
        self._set(domain, DomainStatus.PENDING, detail)

    def _set(self, domain: CapabilityDomain, status: DomainStatus, detail: str) -> None:
        with self._lock:
            old_status = self._domains[domain].status
            self._domains[domain].update(status, detail)
            _log.info(
                "[CapabilityRegistry] %s: %s → %s  (%s)",
                domain.value, old_status.value, status.value, detail or "—",
            )

        # Notify listeners outside the lock to avoid deadlock
        for listener in list(self._listeners):
            try:
                listener(domain, old_status, status)
            except Exception:
                pass

        # Signal asyncio waiters if minimally operational
        if self.is_minimally_operational() and self._ready_event is not None:
            try:
                loop = self._ready_event._loop  # type: ignore[attr-defined]
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(self._ready_event.set)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Readiness queries
    # ------------------------------------------------------------------

    def status_of(self, domain: CapabilityDomain) -> DomainStatus:
        with self._lock:
            return self._domains[domain].status

    def is_domain_up(self, domain: CapabilityDomain) -> bool:
        """Returns True if the domain is SATISFIED or DEGRADED."""
        s = self.status_of(domain)
        return s in (DomainStatus.SATISFIED, DomainStatus.DEGRADED)

    def is_minimally_operational(self) -> bool:
        """True iff all minimum-bar domains are SATISFIED or DEGRADED."""
        with self._lock:
            return all(
                self._domains[d].status in (DomainStatus.SATISFIED, DomainStatus.DEGRADED)
                for d in _MINIMUM_OPERATIONAL_DOMAINS
            )

    def is_fully_operational(self) -> bool:
        """True iff ALL domains are SATISFIED."""
        with self._lock:
            return all(
                self._domains[d].status == DomainStatus.SATISFIED
                for d in CapabilityDomain
            )

    def blocked_domains(self) -> Set[CapabilityDomain]:
        """Return domains whose deps are unmet (informational only)."""
        blocked: Set[CapabilityDomain] = set()
        with self._lock:
            for domain, deps in _DEPENDENCIES.items():
                state = self._domains[domain]
                if state.status == DomainStatus.PENDING:
                    for dep in deps:
                        dep_state = self._domains[dep]
                        if dep_state.status not in (
                            DomainStatus.SATISFIED, DomainStatus.DEGRADED
                        ):
                            blocked.add(domain)
                            break
        return blocked

    # ------------------------------------------------------------------
    # Async wait
    # ------------------------------------------------------------------

    def bind_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind an asyncio.Event to allow await_minimal_operational().

        Call once from the asyncio event loop coroutine after
        the event loop is running.
        """
        self._ready_event = asyncio.Event()
        # If already satisfied, set immediately
        if self.is_minimally_operational():
            self._ready_event.set()

    async def await_minimal_operational(self, timeout: float = 60.0) -> bool:
        """Async-wait until is_minimally_operational() or timeout elapses."""
        if self._ready_event is None:
            raise RuntimeError(
                "bind_event_loop() must be called before await_minimal_operational()"
            )
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------------
    # Serialisation (for /health/ready endpoint)
    # ------------------------------------------------------------------

    def to_health_dict(self) -> dict:
        """Return a structured dict suitable for the /health/ready endpoint."""
        with self._lock:
            domains_out = {
                d.value: self._domains[d].to_dict()
                for d in CapabilityDomain
            }
        return {
            "minimally_operational": self.is_minimally_operational(),
            "fully_operational": self.is_fully_operational(),
            "domains": domains_out,
            "blocked_domains": [d.value for d in self.blocked_domains()],
        }

    # ------------------------------------------------------------------
    # Observer subscription
    # ------------------------------------------------------------------

    def subscribe(
        self,
        listener: Callable[[CapabilityDomain, DomainStatus, DomainStatus], None],
    ) -> None:
        """Subscribe to domain status changes.

        Callback signature: (domain, old_status, new_status) -> None.
        Callbacks are invoked outside the internal lock.
        """
        self._listeners.append(listener)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: Optional[CapabilityRegistry] = None
_registry_lock = __import__("threading").Lock()


def get_capability_registry() -> CapabilityRegistry:
    """Return the process-wide CapabilityRegistry singleton."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = CapabilityRegistry()
    return _registry


def reset_capability_registry() -> None:
    """Reset the singleton — for use in tests only."""
    global _registry
    with _registry_lock:
        _registry = None
