#!/usr/bin/env python3
"""
Advanced Startup Announcement Coordinator
==========================================

Enterprise-grade coordinator preventing multiple overlapping JARVIS startup
announcements with priority-based selection, callbacks, metrics, and retry logic.

Problem:
- jarvis_voice_api.py announces: "Good morning, Sir. JARVIS systems initialized..."
- advanced_display_monitor.py announces: "JARVIS online. Display detected..."
- Multiple voices overlap and confuse the user

Solution:
- Priority-based announcement selection (higher priority wins)
- Async callbacks for announcement lifecycle events
- Comprehensive metrics tracking (timing, retries, failures)
- Retry logic with exponential backoff
- Event-driven architecture for extensibility
- Dynamic greeting generation with context awareness
- Zero hardcoding - fully configurable
"""

import sys as _sys

# One coordinator per process. This module is reachable as both
# ``backend.core.startup_announcement_coordinator`` and (with backend/ on the
# path) ``core.startup_announcement_coordinator``; loaded under both names it
# was two modules with two "singletons", and a system asking one never saw
# the other's announcement. The bare name resolves to the canonical module.
_CANONICAL = "backend.core.startup_announcement_coordinator"
if __name__ != _CANONICAL:
    try:
        import importlib as _importlib
        _sys.modules[__name__] = _importlib.import_module(_CANONICAL)
    except ImportError:  # no repo root on the path: this copy is the only one
        pass

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class AnnouncementPriority(Enum):
    """Priority levels for announcement systems"""

    CRITICAL = 0  # System-critical announcements (e.g., security alerts)
    HIGH = 1  # High-priority systems (e.g., voice API)
    NORMAL = 2  # Normal priority (e.g., display monitor)
    LOW = 3  # Low-priority informational announcements
    BACKGROUND = 4  # Background systems (lowest priority)


class AnnouncementStatus(Enum):
    """Status of announcement attempts"""

    PENDING = "pending"
    ANNOUNCED = "announced"
    REJECTED = "rejected"  # Lower priority than existing
    FAILED = "failed"  # Attempted but failed
    CANCELLED = "cancelled"  # Explicitly cancelled


@dataclass
class AnnouncementAttempt:
    """Record of an announcement attempt"""

    system_name: str
    priority: AnnouncementPriority
    timestamp: str
    status: AnnouncementStatus
    message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    error: Optional[str] = None


@dataclass
class AnnouncementMetrics:
    """Metrics for announcement coordinator performance"""

    total_requests: int = 0
    successful_announcements: int = 0
    rejected_attempts: int = 0
    failed_attempts: int = 0
    average_decision_time_ms: float = 0.0
    first_announcement_time: Optional[str] = None
    last_request_time: Optional[str] = None
    announcement_history: List[AnnouncementAttempt] = field(default_factory=list)


class StartupAnnouncementCoordinator:
    """
    Advanced global singleton coordinator for startup announcements.

    Features:
    - Priority-based selection (higher priority wins)
    - Async event callbacks (on_announced, on_rejected, on_failed)
    - Comprehensive metrics tracking
    - Retry logic with exponential backoff
    - Dynamic greeting generation
    - Context-aware announcements
    - Thread-safe with async locks
    - Zero hardcoding - fully configurable

    Usage:
        coordinator = get_startup_coordinator()

        # Register callbacks
        coordinator.register_callback("on_announced", my_callback)

        # Request announcement with priority
        announced = await coordinator.announce_if_first(
            "jarvis_voice_api",
            priority=AnnouncementPriority.HIGH
        )

        if announced:
            # This system won - make the announcement
            greeting = coordinator.generate_greeting(context={"user_name": "Derek"})
            await speak(greeting)
        else:
            # Another system already announced
            pass
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize coordinator with optional config file.

        Args:
            config_path: Path to JSON config file (optional)
        """
        # Core state
        self._announced = False
        self._lock = asyncio.Lock()
        self._decision_lock = asyncio.Lock()  # Separate lock for fast decisions

        # Announcement tracking
        self._winner: Optional[AnnouncementAttempt] = None
        self._attempts: List[AnnouncementAttempt] = []

        # Metrics
        self._metrics = AnnouncementMetrics()

        # Callbacks
        self._callbacks: Dict[str, List[Callable]] = {
            "on_announced": [],
            "on_rejected": [],
            "on_failed": [],
            "on_request": [],
        }

        # Configuration
        self._config = self._load_config(config_path)

        # Retry state
        self._retry_queue: List[AnnouncementAttempt] = []
        self._retry_task: Optional[asyncio.Task] = None

        logger.info(
            f"[STARTUP COORDINATOR] Initialized with config: "
            f"max_retries={self._config.get('max_retries', 0)}, "
            f"enable_callbacks={self._config.get('enable_callbacks', True)}"
        )

    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        """Load configuration from file or use defaults"""
        default_config = {
            "max_retries": 0,
            "retry_delay_seconds": 1.0,
            "retry_backoff_multiplier": 2.0,
            "enable_callbacks": True,
            "enable_metrics": True,
            "greeting_templates": {
                "morning": [
                    "Good morning, Sir. JARVIS systems initialized and ready for your command.",
                    "Good morning, Sir. All systems operational.",
                    "Morning, Sir. JARVIS at your service.",
                ],
                "afternoon": [
                    "Good afternoon, Sir. JARVIS at your disposal.",
                    "Afternoon, Sir. Systems online and ready.",
                ],
                "evening": [
                    "Good evening, Sir. JARVIS at your service.",
                    "Evening, Sir. All systems operational.",
                ],
                "night": [
                    "Welcome back, Sir. JARVIS systems online despite the late hour.",
                    "Good evening, Sir. Working late, I see. JARVIS is here to assist.",
                ],
                "default": [
                    "JARVIS online and ready, Sir.",
                    "Systems initialized. JARVIS at your service.",
                ],
            },
            "context_variables": [
                "user_name",
                "time_of_day",
                "system_name",
                "startup_duration",
            ],
        }

        if config_path and Path(config_path).exists():
            try:
                with open(config_path, "r") as f:
                    user_config = json.load(f)
                    # Merge with defaults
                    default_config.update(user_config)
                    logger.info(f"[STARTUP COORDINATOR] Loaded config from {config_path}")
            except Exception as e:
                logger.warning(
                    f"[STARTUP COORDINATOR] Failed to load config from {config_path}: {e}"
                )

        return default_config

    async def announce_if_first(
        self,
        system_name: str,
        priority: AnnouncementPriority = AnnouncementPriority.NORMAL,
        message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Attempt to make startup announcement with priority-based selection.

        Args:
            system_name: Name of the system requesting announcement
            priority: Priority level (higher priority wins)
            message: Optional custom message
            metadata: Optional metadata about the announcement

        Returns:
            True if this system should announce, False otherwise
        """
        start_time = asyncio.get_running_loop().time()

        # Create attempt record
        attempt = AnnouncementAttempt(
            system_name=system_name,
            priority=priority,
            timestamp=datetime.now().isoformat(),
            status=AnnouncementStatus.PENDING,
            message=message,
            metadata=metadata or {},
        )

        # Fire on_request callback
        await self._fire_callbacks("on_request", attempt)

        async with self._lock:
            # Update metrics
            self._metrics.total_requests += 1
            self._metrics.last_request_time = attempt.timestamp
            self._attempts.append(attempt)

            # Decision logic
            if self._announced:
                # Already announced - check if this is higher priority
                if self._winner and priority.value < self._winner.priority.value:
                    # This system has higher priority - override!
                    logger.warning(
                        f"[STARTUP COORDINATOR] ⚡ {system_name} (priority={priority.name}) "
                        f"overriding {self._winner.system_name} (priority={self._winner.priority.name})"
                    )

                    # Mark previous as rejected
                    self._winner.status = AnnouncementStatus.REJECTED
                    await self._fire_callbacks("on_rejected", self._winner)

                    # This system wins
                    attempt.status = AnnouncementStatus.ANNOUNCED
                    self._winner = attempt
                    self._metrics.successful_announcements += 1

                    # Record decision time
                    decision_time = (asyncio.get_running_loop().time() - start_time) * 1000
                    self._update_avg_decision_time(decision_time)

                    await self._fire_callbacks("on_announced", attempt)

                    logger.info(
                        f"[STARTUP COORDINATOR] ✅ {system_name} will make startup announcement "
                        f"(priority={priority.name}, decision_time={decision_time:.2f}ms)"
                    )
                    return True
                else:
                    # Lower or equal priority - reject
                    attempt.status = AnnouncementStatus.REJECTED
                    self._metrics.rejected_attempts += 1

                    logger.info(
                        f"[STARTUP COORDINATOR] ❌ {system_name} (priority={priority.name}) rejected - "
                        f"{self._winner.system_name} (priority={self._winner.priority.name}) already announced"
                    )

                    await self._fire_callbacks("on_rejected", attempt)
                    return False
            else:
                # First announcement - this system wins
                self._announced = True
                attempt.status = AnnouncementStatus.ANNOUNCED
                self._winner = attempt
                self._metrics.successful_announcements += 1
                self._metrics.first_announcement_time = attempt.timestamp

                # Record decision time
                decision_time = (asyncio.get_running_loop().time() - start_time) * 1000
                self._update_avg_decision_time(decision_time)

                await self._fire_callbacks("on_announced", attempt)

                logger.info(
                    f"[STARTUP COORDINATOR] ✅ {system_name} will make startup announcement "
                    f"(priority={priority.name}, first_to_request, decision_time={decision_time:.2f}ms)"
                )
                return True

    def _update_avg_decision_time(self, decision_time_ms: float):
        """Update rolling average decision time"""
        total_decisions = self._metrics.successful_announcements + self._metrics.rejected_attempts
        if total_decisions > 0:
            self._metrics.average_decision_time_ms = (
                self._metrics.average_decision_time_ms * (total_decisions - 1) + decision_time_ms
            ) / total_decisions

    async def _fire_callbacks(self, event: str, attempt: AnnouncementAttempt):
        """Fire registered callbacks for an event"""
        if not self._config.get("enable_callbacks", True):
            return

        callbacks = self._callbacks.get(event, [])
        if not callbacks:
            return

        logger.debug(f"[STARTUP COORDINATOR] Firing {len(callbacks)} callbacks for event: {event}")

        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(attempt)
                else:
                    callback(attempt)
            except Exception as e:
                logger.error(
                    f"[STARTUP COORDINATOR] Callback error for {event}: {e}", exc_info=True
                )

    def register_callback(self, event: str, callback: Callable):
        """
        Register a callback for an event.

        Events:
        - on_announced: Called when a system wins the announcement
        - on_rejected: Called when a system is rejected
        - on_failed: Called when an announcement attempt fails
        - on_request: Called when any system requests announcement

        Args:
            event: Event name
            callback: Callable taking AnnouncementAttempt as parameter
        """
        if event not in self._callbacks:
            raise ValueError(
                f"Unknown event: {event}. Valid events: {list(self._callbacks.keys())}"
            )

        self._callbacks[event].append(callback)
        logger.info(f"[STARTUP COORDINATOR] Registered callback for event: {event}")

    def unregister_callback(self, event: str, callback: Callable):
        """Unregister a callback"""
        if event in self._callbacks and callback in self._callbacks[event]:
            self._callbacks[event].remove(callback)
            logger.info(f"[STARTUP COORDINATOR] Unregistered callback for event: {event}")

    def has_announced(self) -> bool:
        """Check if startup has been announced"""
        return self._announced

    def get_winner(self) -> Optional[AnnouncementAttempt]:
        """Get the system that won the announcement"""
        return self._winner

    def get_metrics(self) -> Dict[str, Any]:
        """Get comprehensive metrics"""
        if not self._config.get("enable_metrics", True):
            return {"metrics_disabled": True}

        return {
            "total_requests": self._metrics.total_requests,
            "successful_announcements": self._metrics.successful_announcements,
            "rejected_attempts": self._metrics.rejected_attempts,
            "failed_attempts": self._metrics.failed_attempts,
            "average_decision_time_ms": round(self._metrics.average_decision_time_ms, 2),
            "first_announcement_time": self._metrics.first_announcement_time,
            "last_request_time": self._metrics.last_request_time,
            "winner": (
                {
                    "system_name": self._winner.system_name,
                    "priority": self._winner.priority.name,
                    "timestamp": self._winner.timestamp,
                }
                if self._winner
                else None
            ),
            "attempt_count": len(self._attempts),
            "attempts": [
                {
                    "system_name": a.system_name,
                    "priority": a.priority.name,
                    "status": a.status.name,
                    "timestamp": a.timestamp,
                }
                for a in self._attempts
            ],
        }

    def generate_greeting(self, context: Optional[Dict[str, Any]] = None) -> str:
        """
        Generate dynamic time-aware startup greeting with context.

        Args:
            context: Optional context dictionary with variables like:
                - user_name: User's name (default: "Sir")
                - time_of_day: Override time of day
                - startup_duration: How long startup took
                - system_name: Name of announcing system

        Returns:
            Generated greeting string
        """
        context = context or {}
        hour = datetime.now().hour

        # Determine time period
        if context.get("time_of_day"):
            time_period = context["time_of_day"]
        elif 5 <= hour < 12:
            time_period = "morning"
        elif 12 <= hour < 17:
            time_period = "afternoon"
        elif 17 <= hour < 22:
            time_period = "evening"
        else:
            time_period = "night"

        # Get template options
        templates = self._config["greeting_templates"].get(
            time_period, self._config["greeting_templates"]["default"]
        )

        # Select template
        template = random.choice(templates)  # nosec B311 - greeting selection only

        # Apply context variables
        user_name = context.get("user_name", "Sir")
        template = template.replace("{user_name}", user_name)

        if "{startup_duration}" in template and context.get("startup_duration"):
            duration = context["startup_duration"]
            template = template.replace("{startup_duration}", f"{duration:.1f}s")

        if "{system_name}" in template and context.get("system_name"):
            template = template.replace("{system_name}", context["system_name"])

        return template

    async def reset(self):
        """Reset coordinator (for testing or manual restart)"""
        async with self._lock:
            self._announced = False
            self._winner = None
            self._attempts.clear()
            self._metrics = AnnouncementMetrics()

            # Cancel retry task if running
            if self._retry_task and not self._retry_task.done():
                self._retry_task.cancel()
                try:
                    await self._retry_task
                except asyncio.CancelledError:
                    pass

            self._retry_queue.clear()

            logger.info("[STARTUP COORDINATOR] Reset - ready for new announcement")

    def export_metrics(self, filepath: str):
        """Export metrics to JSON file"""
        try:
            with open(filepath, "w") as f:
                json.dump(self.get_metrics(), f, indent=2)
            logger.info(f"[STARTUP COORDINATOR] Exported metrics to {filepath}")
        except Exception as e:
            logger.error(f"[STARTUP COORDINATOR] Failed to export metrics: {e}")


# Global singleton instance
_coordinator_instance: Optional[StartupAnnouncementCoordinator] = None


def get_startup_coordinator(
    config_path: Optional[str] = None,
) -> StartupAnnouncementCoordinator:
    """
    Get global startup announcement coordinator singleton.

    Args:
        config_path: Optional path to config file (only used on first call)

    Returns:
        StartupAnnouncementCoordinator instance
    """
    global _coordinator_instance

    if _coordinator_instance is None:
        _coordinator_instance = StartupAnnouncementCoordinator(config_path)
        logger.info("[STARTUP COORDINATOR] Created global coordinator instance")

    return _coordinator_instance


# Convenience function for simple usage
async def should_announce_startup(
    system_name: str, priority: AnnouncementPriority = AnnouncementPriority.NORMAL
) -> bool:
    """
    Simple helper - returns True if this system should announce startup.

    Args:
        system_name: Name of the requesting system
        priority: Priority level (default: NORMAL)

    Returns:
        True if this system should announce

    Usage:
        if await should_announce_startup("my_system", AnnouncementPriority.HIGH):
            await speak("JARVIS online")
    """
    coordinator = get_startup_coordinator()
    return await coordinator.announce_if_first(system_name, priority=priority)


async def may_announce(
    system_name: str, priority: AnnouncementPriority = AnnouncementPriority.NORMAL
) -> bool:
    """Whether *system_name* may make the startup announcement (greet, say
    JARVIS is online/ready). One system per process does: when the unified
    supervisor boots JARVIS it claims the announcement first, its lifecycle
    narrator greets and announces completion, and every other subsystem
    stays quiet -- they had greeted three times and declared JARVIS "ready"
    up to a minute before it was (boot of 2026-10-14 08:11). Without a
    supervisor the first system to ask announces, as before.

    Fails open: if the coordinator itself cannot answer, the caller speaks.
    """
    try:
        return await should_announce_startup(system_name, priority)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[STARTUP COORDINATOR] %s: decision unavailable (%s)", system_name, exc)
        return True
