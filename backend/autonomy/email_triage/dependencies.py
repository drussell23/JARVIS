"""Dependency resolution with lazy imports and exponential backoff.

Manages three triage dependencies:
  - workspace_agent (required) -- GoogleWorkspaceAgent singleton
  - router          (optional) -- PrimeRouter singleton
  - notifier        (optional) -- notify_user callable

Each dependency is tracked via ``DependencyHealth``.  The ``DependencyResolver``
drives lazy resolution with configurable exponential backoff on failure.
"""

from __future__ import annotations

import inspect
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.events import (
    EVENT_DEPENDENCY_DEGRADED,
    EVENT_DEPENDENCY_UNAVAILABLE,
    emit_triage_event,
)

logger = logging.getLogger("jarvis.email_triage.dependencies")


# ---------------------------------------------------------------------------
# DependencyHealth
# ---------------------------------------------------------------------------


@dataclass
class DependencyHealth:
    """Per-dependency resolution state with backoff tracking.

    All timestamps use ``time.monotonic()`` so they are immune to
    wall-clock adjustments.
    """

    name: str
    required: bool
    resolved: bool = False
    instance: Any = None
    last_resolve_at: float = 0.0
    last_resolve_error: Optional[str] = None
    consecutive_failures: int = 0
    next_attempt_at: float = 0.0

    def record_success(self, instance: Any) -> None:
        """Mark dependency as resolved and reset failure counters."""
        self.resolved = True
        self.instance = instance
        self.consecutive_failures = 0
        self.last_resolve_error = None
        self.last_resolve_at = time.monotonic()

    def record_failure(self, error: str, base_s: float, max_s: float) -> None:
        """Record a resolution failure and compute the next backoff window.

        Backoff formula::

            delay = min(base_s * 2^consecutive_failures, max_s) * uniform(0.8, 1.2)

        The jitter factor prevents thundering-herd retries when multiple
        subsystems share the same dependency.
        """
        self.resolved = False
        self.instance = None
        self.consecutive_failures += 1
        self.last_resolve_error = error
        self.last_resolve_at = time.monotonic()

        raw_delay = min(base_s * (2 ** self.consecutive_failures), max_s)
        jitter = random.uniform(0.8, 1.2)
        self.next_attempt_at = time.monotonic() + raw_delay * jitter

    def invalidate(self, error: str, base_s: float, max_s: float) -> None:
        """Invalidate a previously resolved dependency.

        Delegates to :meth:`record_failure` so that backoff is applied
        before the next resolution attempt.
        """
        self.record_failure(error, base_s, max_s)

    def can_attempt(self) -> bool:
        """Return True if a resolution attempt is permitted now.

        A dep is attemptable when it is **not** already resolved **and**
        the monotonic clock has passed the backoff deadline.
        """
        if self.resolved:
            return False
        return time.monotonic() >= self.next_attempt_at


# ---------------------------------------------------------------------------
# Resolver functions (module-level, lazy imports)
# ---------------------------------------------------------------------------


def _resolve_workspace_agent() -> Any:
    """Lazily import and return the GoogleWorkspaceAgent singleton."""
    from neural_mesh.agents.google_workspace_agent import get_google_workspace_agent

    agent = get_google_workspace_agent()
    if agent is None:
        raise RuntimeError("GoogleWorkspaceAgent singleton not initialized")
    return agent


def _resolve_router() -> Any:
    """Lazily import and return the PrimeRouter singleton."""
    from core.prime_router import get_prime_router

    router = get_prime_router()
    if router is None:
        raise RuntimeError("PrimeRouter singleton not initialized")
    return router


def _resolve_notifier() -> Any:
    """Lazily import and return the notify_user callable."""
    from agi_os.notification_bridge import notify_user

    return notify_user


# ---------------------------------------------------------------------------
# Resolver registry: dep name -> (resolver_fn_name, required)
#
# Stores function *names* (not references) so that ``unittest.mock.patch``
# on the module-level function works correctly in tests.
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, tuple] = {
    "workspace_agent": ("_resolve_workspace_agent", True),
    "router": ("_resolve_router", False),
    "notifier": ("_resolve_notifier", False),
}

# Module reference kept for dynamic lookup of resolver functions.
import sys as _sys


# ---------------------------------------------------------------------------
# DependencyResolver
# ---------------------------------------------------------------------------


class DependencyResolver:
    """Manages resolution of all triage dependencies with lazy imports
    and exponential backoff.

    Supports injectable overrides for testing -- any non-None constructor
    arg bypasses the lazy resolver entirely.
    """

    def __init__(
        self,
        config: TriageConfig,
        workspace_agent: Any = None,
        router: Any = None,
        notifier: Any = None,
    ) -> None:
        self._config = config
        self._deps: Dict[str, DependencyHealth] = {}

        overrides = {
            "workspace_agent": workspace_agent,
            "router": router,
            "notifier": notifier,
        }

        for name, (_, required) in _REGISTRY.items():
            dep = DependencyHealth(name=name, required=required)
            override = overrides.get(name)
            if override is not None:
                dep.record_success(override)
            self._deps[name] = dep

    async def resolve_all(self) -> None:
        """Attempt to resolve every unresolved dependency that is not in backoff.

        On success the dependency is marked resolved.  On failure an
        ``EVENT_DEPENDENCY_UNAVAILABLE`` event is emitted and the
        backoff timer advances.
        """
        for name, dep in self._deps.items():
            if dep.resolved:
                continue
            if not dep.can_attempt():
                continue

            fn_name = _REGISTRY[name][0]
            _this_module = _sys.modules[__name__]
            resolver_fn: Callable[[], Any] = getattr(_this_module, fn_name)
            try:
                result = resolver_fn()
                # Handle async resolver functions (e.g. get_prime_router)
                if inspect.isawaitable(result):
                    instance = await result
                else:
                    instance = result
                dep.record_success(instance)
                logger.info("Dependency %s resolved successfully", name)
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
                dep.record_failure(
                    error_msg,
                    base_s=self._config.dep_backoff_base_s,
                    max_s=self._config.dep_backoff_max_s,
                )
                emit_triage_event(
                    EVENT_DEPENDENCY_UNAVAILABLE,
                    {
                        "dependency": name,
                        "required": dep.required,
                        "error": error_msg,
                        "consecutive_failures": dep.consecutive_failures,
                    },
                )
                logger.warning(
                    "Dependency %s resolution failed (%d consecutive): %s",
                    name,
                    dep.consecutive_failures,
                    error_msg,
                )

    def get(self, name: str) -> Any:
        """Return the resolved instance for *name*, or ``None``."""
        dep = self._deps.get(name)
        if dep is None or not dep.resolved:
            return None
        return dep.instance

    def invalidate(self, name: str, error: str) -> None:
        """Invalidate a dependency and emit an unavailability event."""
        dep = self._deps.get(name)
        if dep is None:
            return
        dep.invalidate(
            error,
            base_s=self._config.dep_backoff_base_s,
            max_s=self._config.dep_backoff_max_s,
        )
        emit_triage_event(
            EVENT_DEPENDENCY_UNAVAILABLE,
            {
                "dependency": name,
                "required": dep.required,
                "error": error,
                "consecutive_failures": dep.consecutive_failures,
            },
        )

    def report_degraded(self, name: str, reason: str) -> None:
        """Emit an ``EVENT_DEPENDENCY_DEGRADED`` for a resolved but limited dep."""
        emit_triage_event(
            EVENT_DEPENDENCY_DEGRADED,
            {
                "dependency": name,
                "reason": reason,
            },
        )

    def health_summary(self) -> dict:
        """Return a snapshot of all dependency health states."""
        import time as _time
        return {
            name: {
                "resolved": dep.resolved,
                "required": dep.required,
                "consecutive_failures": dep.consecutive_failures,
                "last_error": dep.last_resolve_error,
                "backoff_remaining_s": round(
                    max(0.0, dep.next_attempt_at - _time.monotonic()), 2
                ) if not dep.resolved else 0.0,
            }
            for name, dep in self._deps.items()
        }
