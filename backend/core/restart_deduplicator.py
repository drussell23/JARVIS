"""RestartDeduplicator — P1-2 re-entrancy storm guard.

Prevents concurrent restart / recover calls for the same service from
stacking up when health-monitors, API handlers, and periodic timers all
fire simultaneously.

Design
------
* Per-service **in-flight latch**: only one coroutine runs at a time.
  Extra callers wait until the in-flight run finishes and then decide
  whether to run themselves (if the in-flight run succeeded they skip).
* Post-completion **cooldown window**: after a restart attempt (success
  or fail) the service is locked out for ``cooldown_s`` seconds, so a
  brief flap can't trigger a second restart immediately.
* ``reason`` / ``caller`` metadata is captured for audit; the first
  caller wins the latch and the rest receive the same Task future.
* All timing uses ``time.monotonic()`` — never ``time.time()``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

__all__ = [
    "CooldownActive",
    "RestartDeduplicator",
    "get_restart_deduplicator",
]

logger = logging.getLogger(__name__)

# Default post-attempt cooldown in seconds.
_DEFAULT_COOLDOWN_S: float = 30.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CooldownActive(Exception):
    """Raised when a restart request arrives while the cooldown is active."""

    def __init__(self, service: str, remaining_s: float) -> None:
        self.service = service
        self.remaining_s = remaining_s
        super().__init__(
            f"Service '{service}' is in cooldown for {remaining_s:.1f}s more"
        )


# ---------------------------------------------------------------------------
# Internal per-service state
# ---------------------------------------------------------------------------


@dataclass
class _ServiceState:
    """Mutable per-service latch/cooldown bookkeeping."""

    # Lock serialises check-and-set of the latch.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Shared Task returned to all concurrent callers while in-flight.
    in_flight: Optional[asyncio.Task] = None  # type: ignore[type-arg]
    # Monotonic timestamp of the last attempt completion (0 = never).
    last_attempt_mono: float = 0.0
    # Cooldown duration applied after the last attempt.
    active_cooldown_s: float = _DEFAULT_COOLDOWN_S


# ---------------------------------------------------------------------------
# RestartDeduplicator
# ---------------------------------------------------------------------------


class RestartDeduplicator:
    """Singleton guard for concurrent restart/recover requests.

    Parameters
    ----------
    default_cooldown_s:
        Default post-attempt cooldown.  Per-call overrides take precedence.
    """

    def __init__(self, default_cooldown_s: float = _DEFAULT_COOLDOWN_S) -> None:
        self._default_cooldown_s = default_cooldown_s
        self._services: Dict[str, _ServiceState] = {}
        # Module-level lock for _services dict mutation.
        self._meta_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def request(
        self,
        service: str,
        coro_factory: Callable[[], Awaitable[Any]],
        *,
        reason: str = "",
        caller: str = "",
        cooldown_s: Optional[float] = None,
    ) -> "asyncio.Task[Any]":
        """Request a restart/recover for *service*.

        If a restart is already in-flight, the existing Task is returned
        immediately (deduplicated).  If the service is in a post-attempt
        cooldown, :exc:`CooldownActive` is raised.

        Parameters
        ----------
        service:
            Logical service name (e.g. ``"gcp_vm"``).
        coro_factory:
            Zero-argument callable that returns a coroutine for the
            restart/recovery work.  Only called if this invocation wins
            the latch.
        reason:
            Human-readable reason string (for logging/audit).
        caller:
            Identifier of the subsystem making the request.
        cooldown_s:
            Override post-attempt cooldown for this call.  Defaults to
            the value provided at ``RestartDeduplicator`` construction.

        Returns
        -------
        asyncio.Task
            The Task doing the restart work.  Awaiting it from multiple
            callers is safe — they all share the same future.

        Raises
        ------
        CooldownActive
            If the service is still within its post-attempt cooldown.
        """
        effective_cooldown = (
            cooldown_s if cooldown_s is not None else self._default_cooldown_s
        )

        state = await self._get_or_create(service)

        async with state.lock:
            # --- Cooldown gate ---
            now = time.monotonic()
            elapsed = now - state.last_attempt_mono
            if state.last_attempt_mono > 0 and elapsed < state.active_cooldown_s:
                remaining = state.active_cooldown_s - elapsed
                logger.debug(
                    "[RestartDedup] %s cooldown active (%.1fs remaining) — "
                    "caller=%s reason=%s",
                    service, remaining, caller, reason,
                )
                raise CooldownActive(service, remaining)

            # --- In-flight latch ---
            if state.in_flight is not None and not state.in_flight.done():
                logger.debug(
                    "[RestartDedup] %s already in-flight — "
                    "returning shared Task (caller=%s)",
                    service, caller,
                )
                return state.in_flight

            # --- Win the latch; create a new Task ---
            logger.info(
                "[RestartDedup] %s — starting restart (caller=%s reason=%s)",
                service, caller, reason,
            )

            async def _guarded() -> Any:
                try:
                    return await coro_factory()
                finally:
                    async with state.lock:
                        state.last_attempt_mono = time.monotonic()
                        state.active_cooldown_s = effective_cooldown
                        state.in_flight = None

            task: "asyncio.Task[Any]" = asyncio.ensure_future(_guarded())
            state.in_flight = task
            return task

    def is_cooldown_active(self, service: str) -> bool:
        """Return True if *service* is in its post-attempt cooldown."""
        state = self._services.get(service)
        if state is None or state.last_attempt_mono == 0.0:
            return False
        elapsed = time.monotonic() - state.last_attempt_mono
        return elapsed < state.active_cooldown_s

    def cooldown_remaining(self, service: str) -> float:
        """Return remaining cooldown seconds for *service* (0.0 if none)."""
        state = self._services.get(service)
        if state is None or state.last_attempt_mono == 0.0:
            return 0.0
        elapsed = time.monotonic() - state.last_attempt_mono
        remaining = state.active_cooldown_s - elapsed
        return max(0.0, remaining)

    def reset_cooldown(self, service: str) -> None:
        """Forcibly clear the cooldown for *service* (e.g. after operator override)."""
        state = self._services.get(service)
        if state is not None:
            state.last_attempt_mono = 0.0
            logger.info("[RestartDedup] %s cooldown manually reset", service)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_or_create(self, service: str) -> _ServiceState:
        """Return (creating if needed) the _ServiceState for *service*."""
        if service in self._services:
            return self._services[service]
        async with self._meta_lock:
            # Double-checked under lock.
            if service not in self._services:
                self._services[service] = _ServiceState()
            return self._services[service]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_g_deduplicator: Optional[RestartDeduplicator] = None


def get_restart_deduplicator() -> RestartDeduplicator:
    """Return (lazily creating) the process-wide RestartDeduplicator."""
    global _g_deduplicator
    if _g_deduplicator is None:
        _g_deduplicator = RestartDeduplicator()
    return _g_deduplicator
