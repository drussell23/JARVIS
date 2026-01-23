#!/usr/bin/env python3
"""
JARVIS Restart Coordinator v1.0
================================

Async-safe restart coordination system for JARVIS Supervisor.

This module provides a thread-safe, async-compatible mechanism for signaling
restart requests from anywhere in the supervisor without using sys.exit()
(which doesn't work properly from async tasks).

Architecture:
    Components → RestartCoordinator.request_restart() → Signal Set
                                                       ↓
    Supervisor ← RestartCoordinator.wait_for_restart() ← Signal Received
                                                       ↓
    Supervisor → Terminate child process → Restart loop

Key Features:
- Async-safe signaling via asyncio.Event()
- Non-blocking restart requests
- Restart metadata (reason, source, urgency)
- Cancellable restarts with cleanup
- Multiple concurrent request handling
- Priority-based restart scheduling
- Integration with existing supervisor architecture

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional, List

logger = logging.getLogger(__name__)

# v108.4: Startup grace period to prevent hot-reload restarts during startup
STARTUP_GRACE_PERIOD_SECONDS = float(os.getenv("JARVIS_RESTART_GRACE_PERIOD", "120"))


class RestartUrgency(str, Enum):
    """Restart urgency levels."""
    LOW = "low"           # Can wait for user activity pause
    MEDIUM = "medium"     # Should restart soon
    HIGH = "high"         # Restart immediately
    CRITICAL = "critical" # Emergency restart (security issue)


class RestartSource(str, Enum):
    """Source of restart request."""
    LOCAL_CHANGES = "local_changes"     # Code changes detected
    REMOTE_UPDATE = "remote_update"     # GitHub update available
    USER_REQUEST = "user_request"       # User explicitly requested
    HEALTH_CHECK = "health_check"       # Health monitor triggered
    CONFIG_CHANGE = "config_change"     # Configuration file changed
    DEPENDENCY_UPDATE = "dependency"    # Dependency changed
    SCHEDULED = "scheduled"             # Scheduled maintenance
    INTERNAL = "internal"               # Internal system decision


@dataclass
class RestartRequest:
    """
    Restart request with metadata.

    Attributes:
        source: Who/what requested the restart
        reason: Human-readable reason
        urgency: How urgent is this restart
        requested_at: When the request was made
        countdown_seconds: Delay before restart (for user awareness)
        cancellable: Whether user can cancel this restart
        metadata: Additional context data
    """
    source: RestartSource
    reason: str
    urgency: RestartUrgency = RestartUrgency.MEDIUM
    requested_at: datetime = field(default_factory=datetime.now)
    countdown_seconds: int = 5
    cancellable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "source": self.source.value,
            "reason": self.reason,
            "urgency": self.urgency.value,
            "requested_at": self.requested_at.isoformat(),
            "countdown_seconds": self.countdown_seconds,
            "cancellable": self.cancellable,
            "metadata": self.metadata,
        }


class RestartCoordinator:
    """
    Async-safe restart coordination for JARVIS Supervisor.

    This coordinator provides a centralized mechanism for components
    to request restarts without using sys.exit() directly (which
    doesn't work from async tasks).

    Features:
    - Async-safe signaling via asyncio.Event
    - Request queueing and prioritization
    - Cancellation support
    - Callback system for notifications
    - Countdown management with user awareness

    Usage:
        >>> coordinator = get_restart_coordinator()
        >>> await coordinator.request_restart(
        ...     source=RestartSource.LOCAL_CHANGES,
        ...     reason="Code changes detected",
        ...     countdown_seconds=5,
        ... )

        # In supervisor:
        >>> request = await coordinator.wait_for_restart()
        >>> if request:
        ...     # Handle restart
    """

    def __init__(self):
        """Initialize the restart coordinator."""
        # Core signaling
        self._restart_event = asyncio.Event()
        self._lock = asyncio.Lock()

        # Request management
        self._pending_requests: List[RestartRequest] = []
        self._current_request: Optional[RestartRequest] = None
        self._cancelled = False

        # Countdown management
        self._countdown_task: Optional[asyncio.Task] = None
        self._countdown_remaining: int = 0

        # Callbacks
        self._on_restart_requested: List[Callable[[RestartRequest], None]] = []
        self._on_restart_cancelled: List[Callable[[RestartRequest], None]] = []
        self._on_countdown_tick: List[Callable[[int, RestartRequest], None]] = []

        # State
        self._is_restarting = False
        self._initialized = False

        # v108.4: Track startup time for grace period
        self._startup_time = time.time()
        self._grace_period_ended = False

        logger.info("🔄 Restart coordinator initialized")

    async def initialize(self) -> None:
        """Initialize the coordinator (call once at startup)."""
        if self._initialized:
            return

        self._initialized = True
        logger.debug("Restart coordinator ready")

    async def request_restart(
        self,
        source: RestartSource,
        reason: str,
        urgency: RestartUrgency = RestartUrgency.MEDIUM,
        countdown_seconds: int = 5,
        cancellable: bool = True,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        Request a system restart.

        This is the primary method for components to request a restart.
        The request is queued and the coordinator handles signaling
        the supervisor properly.

        Args:
            source: What component is requesting the restart
            reason: Human-readable reason for the restart
            urgency: How urgent this restart is
            countdown_seconds: Delay before restart (0 = immediate)
            cancellable: Whether user can cancel this restart
            metadata: Additional context data

        Returns:
            True if request was accepted, False if already restarting
        """
        if self._is_restarting and urgency != RestartUrgency.CRITICAL:
            logger.debug(f"Restart already in progress, ignoring {source.value} request")
            return False

        # v108.4: Skip non-critical restarts during startup grace period
        # This prevents hot-reload from killing JARVIS before it's fully started
        if urgency not in (RestartUrgency.HIGH, RestartUrgency.CRITICAL):
            elapsed = time.time() - self._startup_time
            if elapsed < STARTUP_GRACE_PERIOD_SECONDS:
                if not self._grace_period_ended:
                    logger.info(
                        f"[v108.4] ⏳ Startup grace period active ({elapsed:.0f}s/{STARTUP_GRACE_PERIOD_SECONDS:.0f}s) - "
                        f"deferring {source.value} restart: {reason}"
                    )
                return False
            elif not self._grace_period_ended:
                self._grace_period_ended = True
                logger.info(f"[v108.4] ⏰ Startup grace period ended after {elapsed:.0f}s - restarts now allowed")

        async with self._lock:
            request = RestartRequest(
                source=source,
                reason=reason,
                urgency=urgency,
                countdown_seconds=countdown_seconds,
                cancellable=cancellable,
                metadata=metadata or {},
            )

            # Check if this request supersedes current one
            if self._current_request:
                if urgency.value >= self._current_request.urgency.value:
                    # Cancel current, use new
                    await self._cancel_countdown()
                    self._current_request = request
                else:
                    # Queue for later
                    self._pending_requests.append(request)
                    logger.info(f"🔄 Restart request queued: {reason}")
                    return True
            else:
                self._current_request = request

            self._cancelled = False
            self._is_restarting = True

            logger.info(f"🔄 Restart requested: {reason} (source={source.value}, urgency={urgency.value})")

            # Notify callbacks
            for callback in self._on_restart_requested:
                try:
                    callback(request)
                except Exception as e:
                    logger.error(f"Restart callback error: {e}")

            # Handle countdown
            if countdown_seconds > 0 and urgency != RestartUrgency.CRITICAL:
                # Start countdown task
                self._countdown_task = asyncio.create_task(
                    self._run_countdown(request)
                )
            else:
                # Immediate restart
                self._restart_event.set()

            return True

    async def _run_countdown(self, request: RestartRequest) -> None:
        """
        Run the countdown before signaling restart.

        This allows user awareness and potential cancellation.
        """
        self._countdown_remaining = request.countdown_seconds

        try:
            while self._countdown_remaining > 0:
                # Notify countdown tick
                for callback in self._on_countdown_tick:
                    try:
                        callback(self._countdown_remaining, request)
                    except Exception as e:
                        logger.debug(f"Countdown tick callback error: {e}")

                logger.info(f"⏱️ Restarting in {self._countdown_remaining}s: {request.reason}")

                await asyncio.sleep(1)
                self._countdown_remaining -= 1

                # Check if cancelled
                if self._cancelled:
                    logger.info("🚫 Restart countdown cancelled")
                    return

            # Countdown complete - signal restart
            if not self._cancelled:
                logger.info(f"🔄 Countdown complete, signaling restart")
                self._restart_event.set()

        except asyncio.CancelledError:
            logger.debug("Countdown task cancelled")
            raise
        except Exception as e:
            logger.error(f"Countdown error: {e}")
            # On error, still trigger restart for safety
            self._restart_event.set()

    async def _cancel_countdown(self) -> None:
        """Cancel the current countdown task."""
        if self._countdown_task and not self._countdown_task.done():
            self._countdown_task.cancel()
            try:
                await self._countdown_task
            except asyncio.CancelledError:
                pass
            self._countdown_task = None

    def cancel_restart(self) -> bool:
        """
        Cancel a pending restart.

        Returns:
            True if restart was cancelled, False if not cancellable
        """
        if not self._current_request:
            return False

        if not self._current_request.cancellable:
            logger.warning("🚫 Restart is not cancellable")
            return False

        self._cancelled = True
        self._is_restarting = False
        request = self._current_request
        self._current_request = None

        # Clear the event if set
        self._restart_event.clear()

        logger.info(f"🚫 Restart cancelled: {request.reason}")

        # Notify callbacks
        for callback in self._on_restart_cancelled:
            try:
                callback(request)
            except Exception as e:
                logger.error(f"Cancel callback error: {e}")

        return True

    async def wait_for_restart(self, timeout: Optional[float] = None) -> Optional[RestartRequest]:
        """
        Wait for a restart request.

        This is called by the supervisor to wait for restart signals.

        Args:
            timeout: Maximum time to wait (None = forever)

        Returns:
            RestartRequest if restart was requested, None if timeout
        """
        try:
            if timeout:
                await asyncio.wait_for(
                    self._restart_event.wait(),
                    timeout=timeout
                )
            else:
                await self._restart_event.wait()

            # Clear the event for next time
            self._restart_event.clear()

            return self._current_request

        except asyncio.TimeoutError:
            return None

    def is_restart_pending(self) -> bool:
        """Check if a restart is pending."""
        return self._is_restarting and not self._cancelled

    def get_countdown_remaining(self) -> int:
        """Get remaining countdown seconds."""
        return self._countdown_remaining if self._is_restarting else 0

    def get_current_request(self) -> Optional[RestartRequest]:
        """Get the current restart request (if any)."""
        return self._current_request

    def get_state(self) -> dict[str, Any]:
        """Get current coordinator state."""
        return {
            "is_restarting": self._is_restarting,
            "cancelled": self._cancelled,
            "countdown_remaining": self._countdown_remaining,
            "current_request": self._current_request.to_dict() if self._current_request else None,
            "pending_requests": len(self._pending_requests),
        }

    def on_restart_requested(self, callback: Callable[[RestartRequest], None]) -> None:
        """Register a callback for restart requests."""
        self._on_restart_requested.append(callback)

    def on_restart_cancelled(self, callback: Callable[[RestartRequest], None]) -> None:
        """Register a callback for restart cancellations."""
        self._on_restart_cancelled.append(callback)

    def on_countdown_tick(self, callback: Callable[[int, RestartRequest], None]) -> None:
        """Register a callback for countdown ticks."""
        self._on_countdown_tick.append(callback)

    async def cleanup(self) -> None:
        """Clean up coordinator resources."""
        await self._cancel_countdown()
        self._restart_event.clear()
        self._is_restarting = False
        self._current_request = None
        self._pending_requests.clear()
        logger.debug("Restart coordinator cleaned up")


# Module-level singleton
_restart_coordinator: Optional[RestartCoordinator] = None


def get_restart_coordinator() -> RestartCoordinator:
    """Get or create the restart coordinator singleton."""
    global _restart_coordinator
    if _restart_coordinator is None:
        _restart_coordinator = RestartCoordinator()
    return _restart_coordinator


async def request_restart(
    source: RestartSource = RestartSource.INTERNAL,
    reason: str = "System restart requested",
    urgency: RestartUrgency = RestartUrgency.MEDIUM,
    countdown_seconds: int = 5,
    cancellable: bool = True,
    metadata: Optional[dict[str, Any]] = None,
) -> bool:
    """
    Convenience function to request a restart.

    This can be called from anywhere in the supervisor codebase
    without needing to get the coordinator instance first.

    Args:
        source: What component is requesting the restart
        reason: Human-readable reason
        urgency: How urgent this restart is
        countdown_seconds: Delay before restart
        cancellable: Whether user can cancel
        metadata: Additional context

    Returns:
        True if request was accepted
    """
    coordinator = get_restart_coordinator()
    return await coordinator.request_restart(
        source=source,
        reason=reason,
        urgency=urgency,
        countdown_seconds=countdown_seconds,
        cancellable=cancellable,
        metadata=metadata,
    )


def cancel_restart() -> bool:
    """
    Convenience function to cancel a pending restart.

    Returns:
        True if restart was cancelled
    """
    coordinator = get_restart_coordinator()
    return coordinator.cancel_restart()
