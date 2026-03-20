"""ECAPA Lifecycle Facade — single authoritative ECAPA lifecycle owner.

This module implements the ``EcapaFacade`` class, a state-machine-driven
singleton that manages the full lifecycle of ECAPA speaker-embedding
backends (local ECAPA-TDNN and cloud endpoints).  All callers should go
through the facade rather than touching backend wrappers directly.

Key design points:
- ``start()`` is non-blocking — heavy probe/load work runs in a background task.
- ``stop()`` cancels all background work and resets to UNINITIALIZED.
- ``extract_embedding()`` routes to the active backend with back-pressure.
- ``check_capability()`` is a synchronous pure lookup.
- ``subscribe()`` enables telemetry subscribers for state-change events.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from backend.core.ecapa_types import (
    CapabilityCheck,
    EcapaFacadeConfig,
    EcapaOverloadError,
    EcapaState,
    EcapaStateEvent,
    EcapaTier,
    EcapaUnavailableError,
    EmbeddingResult,
    STATE_TO_TIER,
    VoiceCapability,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legal transitions table
# ---------------------------------------------------------------------------

_LEGAL_TRANSITIONS: Dict[Tuple[EcapaState, EcapaState], bool] = {
    (EcapaState.UNINITIALIZED, EcapaState.PROBING): True,
    (EcapaState.PROBING, EcapaState.LOADING): True,
    (EcapaState.PROBING, EcapaState.READY): True,        # cloud immediate
    (EcapaState.PROBING, EcapaState.UNAVAILABLE): True,
    (EcapaState.LOADING, EcapaState.READY): True,
    (EcapaState.LOADING, EcapaState.UNAVAILABLE): True,
    (EcapaState.READY, EcapaState.DEGRADED): True,
    (EcapaState.READY, EcapaState.READY): True,           # intra-state backend switch
    (EcapaState.DEGRADED, EcapaState.READY): True,
    (EcapaState.DEGRADED, EcapaState.UNAVAILABLE): True,
    (EcapaState.UNAVAILABLE, EcapaState.RECOVERING): True,
    (EcapaState.RECOVERING, EcapaState.READY): True,
    (EcapaState.RECOVERING, EcapaState.UNAVAILABLE): True,
}

# ---------------------------------------------------------------------------
# Capability tables per tier
# ---------------------------------------------------------------------------

# Capabilities allowed at each tier.  Missing entries default to denied.
_ALWAYS_ALLOWED: Set[VoiceCapability] = {
    VoiceCapability.BASIC_COMMAND,
    VoiceCapability.PROFILE_READ,
    VoiceCapability.PASSWORD_FALLBACK,
}

_READY_ALLOWED: Set[VoiceCapability] = set(VoiceCapability)

_DEGRADED_ALLOWED: Set[VoiceCapability] = {
    VoiceCapability.BASIC_COMMAND,
    VoiceCapability.PROFILE_READ,
    VoiceCapability.PASSWORD_FALLBACK,
    VoiceCapability.VOICE_UNLOCK,
    VoiceCapability.AUTH_COMMAND,
    VoiceCapability.EXTRACT_EMBEDDING,
}

_DEGRADED_CONSTRAINED: Dict[VoiceCapability, Dict[str, Any]] = {
    VoiceCapability.VOICE_UNLOCK: {"confidence_boost_required": 0.05},
    VoiceCapability.AUTH_COMMAND: {"secondary_confirmation": True},
}


# ---------------------------------------------------------------------------
# EcapaFacade
# ---------------------------------------------------------------------------


class EcapaFacade:
    """Single authoritative ECAPA lifecycle owner.

    Parameters
    ----------
    registry:
        Model registry object. Must expose ``get_wrapper(name)`` returning an
        object with ``is_loaded``, ``load()``, and ``extract()`` attributes.
    cloud_client:
        Optional cloud embedding client with ``health_check()`` and
        ``extract_embedding()`` async methods.
    config:
        ``EcapaFacadeConfig`` instance; defaults are read from env vars.
    """

    def __init__(
        self,
        registry: Any,
        cloud_client: Any = None,
        config: Optional[EcapaFacadeConfig] = None,
    ) -> None:
        self._registry = registry
        self._cloud_client = cloud_client
        self._config = config or EcapaFacadeConfig.from_env()

        # State machine
        self._state = EcapaState.UNINITIALIZED
        self._active_backend: Optional[str] = None
        self._state_lock = asyncio.Lock()
        self._ready_event = asyncio.Event()

        # Background lifecycle
        self._bg_tasks: List[asyncio.Task] = []

        # Telemetry
        self._subscribers: List[Callable] = []

        # Circuit breaker counters
        self._consecutive_failures = 0
        self._consecutive_successes = 0

        # Back-pressure — counter-based (no TOCTOU race)
        self._max_concurrent = self._config.max_concurrent_extractions
        self._inflight_count = 0
        self._inflight_lock = asyncio.Lock()

        # Timing
        self._started_at: Optional[float] = None

        # Root cause ID propagation (Issue 5)
        self._current_root_cause_id: Optional[str] = None
        self._last_root_cause_id: Optional[str] = None

    # -- public read-only properties -----------------------------------------

    @property
    def state(self) -> EcapaState:
        return self._state

    @property
    def tier(self) -> EcapaTier:
        return STATE_TO_TIER[self._state]

    @property
    def active_backend(self) -> Optional[str]:
        return self._active_backend

    # -- lifecycle methods ---------------------------------------------------

    async def start(self) -> None:
        """Start the lifecycle probe.  Non-blocking and idempotent."""
        if self._state != EcapaState.UNINITIALIZED:
            return

        self._started_at = time.monotonic()
        await self._try_transition(
            EcapaState.PROBING,
            reason="start() called — probing backends",
            warning_code="ECAPA_W001",
        )
        task = asyncio.get_event_loop().create_task(self._probe_and_load())
        self._bg_tasks.append(task)

    async def stop(self) -> None:
        """Cancel all background work and reset to UNINITIALIZED.

        This method bypasses the legal transition table so it works from any
        state.
        """
        # Cancel all background tasks
        for task in self._bg_tasks:
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

        # Force state back to UNINITIALIZED (bypass transition table)
        old_state = self._state
        self._state = EcapaState.UNINITIALIZED
        self._active_backend = None
        self._ready_event.clear()
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._inflight_count = 0
        self._started_at = None

        self._current_root_cause_id = None

        if old_state != EcapaState.UNINITIALIZED:
            await self._emit_event(
                EcapaStateEvent.make(
                    previous_state=old_state,
                    new_state=EcapaState.UNINITIALIZED,
                    active_backend=None,
                    reason="stop() called",
                    warning_code="ECAPA_STOP",
                )
            )

    async def ensure_ready(self, timeout: float = 30.0) -> bool:
        """Wait until the facade reaches a usable tier.

        Returns True if the tier is READY or DEGRADED within *timeout*
        seconds.  Returns False if timeout elapses or the facade is stuck
        in UNAVAILABLE.
        """
        if self.tier in (EcapaTier.READY, EcapaTier.DEGRADED):
            return True

        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return self.tier in (EcapaTier.READY, EcapaTier.DEGRADED)
        except asyncio.TimeoutError:
            return self.tier in (EcapaTier.READY, EcapaTier.DEGRADED)

    async def extract_embedding(self, audio: Any) -> EmbeddingResult:
        """Extract a speaker embedding from raw audio.

        Raises
        ------
        EcapaUnavailableError
            If the facade is not in a usable tier.
        EcapaOverloadError
            If back-pressure limits are exceeded.
        """
        if self.tier == EcapaTier.UNAVAILABLE:
            raise EcapaUnavailableError(
                f"ECAPA backend unavailable (state={self._state.value})"
            )

        # Atomic counter-based back-pressure (no TOCTOU race)
        async with self._inflight_lock:
            if self._inflight_count >= self._max_concurrent:
                raise EcapaOverloadError(
                    retry_after_s=1.0,
                    message=(
                        f"ECAPA at capacity: {self._max_concurrent} "
                        "concurrent extractions already running"
                    ),
                )
            self._inflight_count += 1

        t0 = time.monotonic()
        try:
            embedding = await self._route_extraction(audio)
            latency_ms = (time.monotonic() - t0) * 1000.0
            self._consecutive_failures = 0
            self._consecutive_successes += 1
            return EmbeddingResult(
                embedding=embedding,
                backend=self._active_backend or "unknown",
                latency_ms=latency_ms,
                from_cache=False,
                dimension=len(embedding) if embedding is not None else 0,
                error=None,
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000.0
            self._consecutive_failures += 1
            self._consecutive_successes = 0

            # Emit extraction failure event with derived root cause
            await self._emit_event(
                EcapaStateEvent.make(
                    previous_state=self._state,
                    new_state=self._state,
                    active_backend=self._active_backend,
                    reason=f"Extraction failed: {type(exc).__name__}: {exc}",
                    warning_code="ECAPA_W005",
                    error_class=type(exc).__name__,
                    latency_ms=latency_ms,
                    root_cause_id=self._current_root_cause_id,
                )
            )

            # Check failure threshold -> DEGRADED
            if (
                self._state == EcapaState.READY
                and self._consecutive_failures >= self._config.failure_threshold
            ):
                await self._try_transition(
                    EcapaState.DEGRADED,
                    reason=f"{self._consecutive_failures} consecutive extraction failures",
                    warning_code="ECAPA_W006",
                    error_class=type(exc).__name__,
                )

            raise
        finally:
            async with self._inflight_lock:
                self._inflight_count -= 1

    def check_capability(self, capability: VoiceCapability) -> CapabilityCheck:
        """Synchronous capability admission check based on current tier."""
        tier = self.tier

        if tier == EcapaTier.READY:
            if capability in _READY_ALLOWED:
                return CapabilityCheck(
                    allowed=True,
                    tier=tier,
                    reason_code="READY",
                )
            return CapabilityCheck(
                allowed=False,
                tier=tier,
                reason_code="UNKNOWN_CAPABILITY",
            )

        if tier == EcapaTier.DEGRADED:
            if capability in _DEGRADED_ALLOWED:
                constraints = _DEGRADED_CONSTRAINED.get(capability, {})
                return CapabilityCheck(
                    allowed=True,
                    tier=tier,
                    reason_code="DEGRADED",
                    constraints=constraints,
                )
            return CapabilityCheck(
                allowed=False,
                tier=tier,
                reason_code="DEGRADED_DENIED",
                fallback="password_fallback",
            )

        # UNAVAILABLE
        if capability in _ALWAYS_ALLOWED:
            return CapabilityCheck(
                allowed=True,
                tier=tier,
                reason_code="ALWAYS_ALLOWED",
            )
        return CapabilityCheck(
            allowed=False,
            tier=tier,
            reason_code="UNAVAILABLE",
            fallback="password_fallback",
        )

    def get_status(self) -> Dict[str, Any]:
        """Return a snapshot of current facade status."""
        uptime: Optional[float] = None
        if self._started_at is not None:
            uptime = time.monotonic() - self._started_at

        return {
            "state": self._state.value,
            "tier": self.tier.value,
            "active_backend": self._active_backend,
            "uptime_s": uptime,
            "consecutive_failures": self._consecutive_failures,
            "consecutive_successes": self._consecutive_successes,
        }

    def subscribe(self, callback: Callable) -> Callable:
        """Register a telemetry subscriber.  Returns an unsubscribe callable."""
        self._subscribers.append(callback)

        def _unsub():
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return _unsub

    # -- internal state machine ----------------------------------------------

    async def _try_transition(
        self,
        new_state: EcapaState,
        *,
        reason: str,
        warning_code: str = "ECAPA_STATE_CHANGE",
        error_class: Optional[str] = None,
        latency_ms: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        root_cause_id: Optional[str] = None,
    ) -> bool:
        """Attempt a state transition under the state lock.

        Returns True if the transition was legal and executed, False otherwise.

        Parameters
        ----------
        root_cause_id:
            If provided, reuse this ID for the event (derived event).
            If ``None``, a new root cause ID is generated (root event)
            and stored as ``_current_root_cause_id``.
        """
        async with self._state_lock:
            key = (self._state, new_state)
            if key not in _LEGAL_TRANSITIONS:
                logger.warning(
                    "Illegal ECAPA transition %s -> %s: %s",
                    self._state.value,
                    new_state.value,
                    reason,
                )
                return False

            old_state = self._state
            self._state = new_state

            # Update ready event
            new_tier = STATE_TO_TIER[new_state]
            if new_tier in (EcapaTier.READY, EcapaTier.DEGRADED):
                self._ready_event.set()
            else:
                self._ready_event.clear()

            # Root cause ID: new transitions generate a new root cause,
            # derived events reuse the provided one.
            effective_root_cause_id = root_cause_id
            if effective_root_cause_id is None:
                effective_root_cause_id = str(uuid.uuid4())
                self._current_root_cause_id = effective_root_cause_id

            event = EcapaStateEvent.make(
                previous_state=old_state,
                new_state=new_state,
                active_backend=self._active_backend,
                reason=reason,
                warning_code=warning_code,
                error_class=error_class,
                latency_ms=latency_ms,
                metadata=metadata,
                root_cause_id=effective_root_cause_id,
            )
            self._last_root_cause_id = event.root_cause_id

        # Emit outside the lock to avoid holding it during subscriber calls
        await self._emit_event(event)
        return True

    async def _probe_and_load(self) -> None:
        """Background task: probe backends and bring facade to READY or UNAVAILABLE."""
        try:
            cloud_healthy = False
            local_loaded = False

            # Concurrent probe: cloud and local
            cloud_task = None
            if self._cloud_client is not None:
                cloud_task = asyncio.ensure_future(self._probe_cloud())

            local_task = asyncio.ensure_future(self._probe_local())

            # Wait for both probes with timeout
            probe_timeout = self._config.probe_timeout_s

            if cloud_task is not None:
                try:
                    cloud_healthy = await asyncio.wait_for(
                        asyncio.shield(cloud_task), timeout=probe_timeout
                    )
                except (asyncio.TimeoutError, Exception):
                    cloud_healthy = False

            try:
                local_loaded = await asyncio.wait_for(
                    asyncio.shield(local_task), timeout=probe_timeout
                )
            except (asyncio.TimeoutError, Exception):
                local_loaded = False

            # Decision: prefer cloud if healthy (lower latency path), then local
            if cloud_healthy:
                self._active_backend = "cloud"
                await self._try_transition(
                    EcapaState.READY,
                    reason="Cloud backend healthy",
                    warning_code="ECAPA_W003",
                )
                return

            if local_loaded:
                self._active_backend = "local"
                await self._try_transition(
                    EcapaState.READY,
                    reason="Local backend loaded",
                    warning_code="ECAPA_W003",
                )
                return

            # Neither probe succeeded — try loading local
            await self._try_transition(
                EcapaState.LOADING,
                reason="Probes failed, attempting local load",
                warning_code="ECAPA_STATE_CHANGE",
            )

            try:
                load_ok = await asyncio.wait_for(
                    self._load_local(),
                    timeout=self._config.local_load_timeout_s,
                )
                if load_ok:
                    self._active_backend = "local"
                    await self._try_transition(
                        EcapaState.READY,
                        reason="Local backend loaded after explicit load()",
                        warning_code="ECAPA_W004",
                    )
                    return
            except (asyncio.TimeoutError, Exception) as exc:
                logger.warning("Local load failed: %s", exc)

            # Everything failed
            await self._try_transition(
                EcapaState.UNAVAILABLE,
                reason="All backends exhausted",
                warning_code="ECAPA_W002",
                error_class="NoBackendAvailable",
            )
            # Launch reprobe loop when entering UNAVAILABLE
            self._launch_reprobe_loop()

        except asyncio.CancelledError:
            logger.debug("Probe-and-load task cancelled")
            raise
        except Exception:
            logger.exception("Unexpected error in _probe_and_load")
            # Best effort: mark unavailable
            try:
                await self._try_transition(
                    EcapaState.UNAVAILABLE,
                    reason="Unexpected probe error",
                    warning_code="ECAPA_W002",
                )
                self._launch_reprobe_loop()
            except Exception:
                pass

    async def _probe_cloud(self) -> bool:
        """Check if the cloud client is healthy."""
        try:
            if self._cloud_client is None:
                return False
            return await self._cloud_client.health_check()
        except Exception:
            return False

    async def _probe_local(self) -> bool:
        """Check if the local ECAPA wrapper is already loaded."""
        try:
            wrapper = self._registry.get_wrapper("ecapa_tdnn")
            return bool(wrapper.is_loaded)
        except Exception:
            return False

    async def _load_local(self) -> bool:
        """Attempt to load the local ECAPA wrapper."""
        try:
            wrapper = self._registry.get_wrapper("ecapa_tdnn")
            result = await wrapper.load()
            return bool(result)
        except Exception:
            return False

    async def _route_extraction(self, audio: Any) -> Any:
        """Route extraction to the currently active backend."""
        if self._active_backend == "cloud":
            return await self._cloud_client.extract_embedding(audio)
        elif self._active_backend == "local":
            wrapper = self._registry.get_wrapper("ecapa_tdnn")
            return await wrapper.extract(audio)
        else:
            raise EcapaUnavailableError("No active backend for extraction")

    async def _emit_event(self, event: EcapaStateEvent) -> None:
        """Emit an event to all subscribers via fire-and-forget tasks.

        Each subscriber call is wrapped in ``asyncio.create_task`` so that:
        - Slow subscribers do not block the state machine.
        - Exceptions are logged but do not propagate.
        """
        for sub in list(self._subscribers):
            asyncio.create_task(self._run_subscriber(sub, event))

    @staticmethod
    async def _run_subscriber(sub: Callable, event: EcapaStateEvent) -> None:
        """Invoke a single subscriber, handling sync and async callbacks."""
        try:
            result = sub(event)
            # Support both sync and async subscribers
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Subscriber %r raised during event emission", sub)

    # -- reprobe loop (Issue 2) -----------------------------------------------

    def _launch_reprobe_loop(self) -> None:
        """Start the reprobe background task if not already running."""
        # Only launch when UNAVAILABLE
        if self._state != EcapaState.UNAVAILABLE:
            return
        task = asyncio.ensure_future(self._reprobe_loop())
        self._bg_tasks.append(task)

    async def _reprobe_loop(self) -> None:
        """Background loop: exponential-backoff reprobing while UNAVAILABLE.

        On success: UNAVAILABLE -> RECOVERING, then test N successes -> READY.
        On budget exhaustion: emit ECAPA_W015 and stop.
        """
        budget_remaining = self._config.reprobe_budget
        backoff = self._config.reprobe_interval_s

        try:
            while self._state == EcapaState.UNAVAILABLE and budget_remaining > 0:
                await asyncio.sleep(backoff)

                # If state changed (e.g., stop() was called), bail out
                if self._state != EcapaState.UNAVAILABLE:
                    return

                # Lightweight probe
                cloud_ok = False
                local_ok = False
                try:
                    cloud_ok = await asyncio.wait_for(
                        self._probe_cloud(),
                        timeout=self._config.probe_timeout_s,
                    )
                except (asyncio.TimeoutError, Exception):
                    pass

                if not cloud_ok:
                    try:
                        local_ok = await asyncio.wait_for(
                            self._probe_local(),
                            timeout=self._config.probe_timeout_s,
                        )
                    except (asyncio.TimeoutError, Exception):
                        pass

                if cloud_ok or local_ok:
                    # Found a candidate
                    candidate_backend = "cloud" if cloud_ok else "local"
                    await self._emit_event(
                        EcapaStateEvent.make(
                            previous_state=self._state,
                            new_state=self._state,
                            active_backend=candidate_backend,
                            reason=f"Reprobe found candidate: {candidate_backend}",
                            warning_code="ECAPA_W009",
                            root_cause_id=self._current_root_cause_id,
                        )
                    )

                    self._active_backend = candidate_backend
                    ok = await self._try_transition(
                        EcapaState.RECOVERING,
                        reason=f"Reprobe found {candidate_backend}",
                        warning_code="ECAPA_W009",
                    )
                    if ok:
                        # Test N consecutive successes before promoting to READY
                        success_count = 0
                        while success_count < self._config.recovery_threshold:
                            try:
                                if candidate_backend == "cloud":
                                    healthy = await self._probe_cloud()
                                else:
                                    healthy = await self._probe_local()
                                if healthy:
                                    success_count += 1
                                else:
                                    # Recovery failed
                                    break
                            except Exception:
                                break

                            if success_count < self._config.recovery_threshold:
                                await asyncio.sleep(self._config.reprobe_interval_s)

                        if success_count >= self._config.recovery_threshold:
                            await self._try_transition(
                                EcapaState.READY,
                                reason=f"Recovery verified: {success_count} consecutive successes",
                                warning_code="ECAPA_W007",
                            )
                            return  # Fully recovered
                        else:
                            # Fall back to UNAVAILABLE
                            await self._try_transition(
                                EcapaState.UNAVAILABLE,
                                reason="Recovery verification failed",
                                warning_code="ECAPA_W008",
                            )
                            # Continue reprobe loop
                    # Reset backoff on any probe success
                    backoff = self._config.reprobe_interval_s
                else:
                    # Probe failed
                    budget_remaining -= 1
                    backoff = min(backoff * 2, self._config.reprobe_max_backoff_s)

            # Budget exhausted
            if budget_remaining <= 0 and self._state == EcapaState.UNAVAILABLE:
                logger.warning(
                    "ECAPA reprobe budget exhausted (%d attempts)",
                    self._config.reprobe_budget,
                )
                await self._emit_event(
                    EcapaStateEvent.make(
                        previous_state=self._state,
                        new_state=self._state,
                        active_backend=None,
                        reason=f"Reprobe budget exhausted after {self._config.reprobe_budget} attempts",
                        warning_code="ECAPA_W015",
                        root_cause_id=self._current_root_cause_id,
                    )
                )

        except asyncio.CancelledError:
            logger.debug("Reprobe loop cancelled")
            raise
        except Exception:
            logger.exception("Unexpected error in _reprobe_loop")


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_facade_instance: Optional[EcapaFacade] = None
_facade_lock = asyncio.Lock()


async def get_ecapa_facade(
    registry: Any = None,
    cloud_client: Any = None,
    config: Optional[EcapaFacadeConfig] = None,
) -> EcapaFacade:
    """Return the module-level ``EcapaFacade`` singleton.

    On the first call, *registry* is required.  Subsequent calls return the
    existing instance regardless of arguments.
    """
    global _facade_instance
    if _facade_instance is not None:
        return _facade_instance

    async with _facade_lock:
        if _facade_instance is not None:
            return _facade_instance
        if registry is None:
            raise ValueError("registry required for first facade creation")
        _facade_instance = EcapaFacade(
            registry=registry,
            cloud_client=cloud_client,
            config=config,
        )
        return _facade_instance


def _reset_facade() -> None:
    """Reset the singleton for testing.  Not for production use."""
    global _facade_instance
    _facade_instance = None
