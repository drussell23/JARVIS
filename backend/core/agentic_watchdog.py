"""
JARVIS Agentic Watchdog - Supervisor Safety System v1.0
========================================================

The critical safety layer that monitors agentic (Computer Use) execution
and provides kill-switch capabilities for runaway AI behavior.

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                    AgenticWatchdog                          │
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
    │  │  Heartbeat  │  │  Activity   │  │   Downgrade        │ │
    │  │  Monitor    │  │  Analyzer   │  │   Protocol         │ │
    │  └──────┬──────┘  └──────┬──────┘  └─────────┬───────────┘ │
    │         │                │                   │              │
    │         └────────────────┴───────────────────┘              │
    │                          │                                  │
    │              ┌───────────▼───────────┐                      │
    │              │   Kill Switch Engine  │                      │
    │              └───────────────────────┘                      │
    └─────────────────────────────────────────────────────────────┘

Safety Features:
- Heartbeat monitoring with configurable timeout
- Activity rate limiting (prevents click storms)
- Automatic downgrade to passive mode on anomalies
- Voice announcement of safety events
- Comprehensive audit logging
- Circuit breaker for repeated failures

Two-Tier Security Model:
- Tier 1 (JARVIS): Safe APIs, read-only, low VBIA threshold
- Tier 2 (JARVIS ACCESS/EXECUTE): Full Computer Use, strict VBIA, watchdog active

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Awaitable
from collections import deque

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class WatchdogConfig:
    """Configuration for the Agentic Watchdog."""

    # Heartbeat settings
    heartbeat_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_WATCHDOG_HEARTBEAT_TIMEOUT", "10.0"))
    )
    heartbeat_grace_period: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_WATCHDOG_GRACE_PERIOD", "30.0"))
    )

    # Activity rate limiting
    max_clicks_per_second: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_WATCHDOG_MAX_CLICKS_PER_SEC", "5.0"))
    )
    max_keystrokes_per_second: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_WATCHDOG_MAX_KEYS_PER_SEC", "20.0"))
    )
    activity_window_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_WATCHDOG_ACTIVITY_WINDOW", "2.0"))
    )

    # Kill switch settings
    max_consecutive_failures: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_WATCHDOG_MAX_FAILURES", "3"))
    )
    cooldown_after_kill: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_WATCHDOG_COOLDOWN", "30.0"))
    )

    # Voice feedback
    voice_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_WATCHDOG_VOICE", "true").lower() == "true"
    )

    # Audit logging
    audit_log_path: Optional[str] = field(
        default_factory=lambda: os.getenv("JARVIS_WATCHDOG_AUDIT_LOG", None)
    )


# =============================================================================
# Enums and Data Classes
# =============================================================================

class AgenticMode(str, Enum):
    """Operating modes for the agentic system."""
    PASSIVE = "passive"           # Standard JARVIS - safe APIs only
    SUPERVISED = "supervised"     # Computer Use with human confirmation
    AUTONOMOUS = "autonomous"     # Full autonomous Computer Use
    KILLED = "killed"             # Emergency shutdown - no agentic allowed


class WatchdogEvent(str, Enum):
    """Events tracked by the watchdog."""
    HEARTBEAT_RECEIVED = "heartbeat_received"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    ACTIVITY_SPIKE = "activity_spike"
    KILL_SWITCH_TRIGGERED = "kill_switch_triggered"
    MODE_CHANGED = "mode_changed"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    DOWNGRADE_INITIATED = "downgrade_initiated"
    RECOVERY_STARTED = "recovery_started"


@dataclass
class Heartbeat:
    """A heartbeat from an agentic task."""
    task_id: str
    goal: str
    current_action: str
    actions_count: int
    timestamp: float
    mode: AgenticMode
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActivityRecord:
    """Record of an activity event (click, keystroke, etc.)."""
    activity_type: str  # "click", "keystroke", "scroll", "screenshot"
    timestamp: float
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WatchdogStatus:
    """Current status of the watchdog."""
    mode: AgenticMode
    active_task_id: Optional[str]
    active_goal: Optional[str]
    last_heartbeat: Optional[float]
    heartbeat_healthy: bool
    activity_rate: Dict[str, float]  # type -> rate per second
    consecutive_failures: int
    kill_switch_armed: bool
    uptime_seconds: float
    events_logged: int


# =============================================================================
# Activity Analyzer
# =============================================================================

class ActivityAnalyzer:
    """
    Analyzes activity patterns to detect anomalies.

    Monitors:
    - Click rate (prevent click storms)
    - Keystroke rate (prevent spam)
    - Screenshot frequency (prevent API abuse)
    - Overall activity patterns
    """

    def __init__(self, config: WatchdogConfig):
        self.config = config
        self._activity_buffer: deque[ActivityRecord] = deque(maxlen=1000)
        self._last_analysis: Dict[str, float] = {}

    def record_activity(self, activity_type: str, details: Dict[str, Any] = None):
        """Record an activity event."""
        record = ActivityRecord(
            activity_type=activity_type,
            timestamp=time.time(),
            details=details or {}
        )
        self._activity_buffer.append(record)

    def get_rate(self, activity_type: str) -> float:
        """Get the current rate for an activity type (per second)."""
        now = time.time()
        window_start = now - self.config.activity_window_seconds

        count = sum(
            1 for r in self._activity_buffer
            if r.activity_type == activity_type and r.timestamp >= window_start
        )

        return count / self.config.activity_window_seconds if self.config.activity_window_seconds > 0 else 0

    def get_all_rates(self) -> Dict[str, float]:
        """Get rates for all activity types."""
        now = time.time()
        window_start = now - self.config.activity_window_seconds

        counts: Dict[str, int] = {}
        for record in self._activity_buffer:
            if record.timestamp >= window_start:
                counts[record.activity_type] = counts.get(record.activity_type, 0) + 1

        window = self.config.activity_window_seconds
        return {
            activity_type: count / window if window > 0 else 0
            for activity_type, count in counts.items()
        }

    def check_anomaly(self) -> Optional[str]:
        """
        Check for activity anomalies.

        Returns:
            Anomaly description if detected, None otherwise
        """
        rates = self.get_all_rates()

        # Check click rate
        click_rate = rates.get("click", 0) + rates.get("mouse_click", 0)
        if click_rate > self.config.max_clicks_per_second:
            return f"Click storm detected: {click_rate:.1f}/sec (max: {self.config.max_clicks_per_second})"

        # Check keystroke rate
        key_rate = rates.get("keystroke", 0) + rates.get("key", 0) + rates.get("type", 0)
        if key_rate > self.config.max_keystrokes_per_second:
            return f"Keystroke flood detected: {key_rate:.1f}/sec (max: {self.config.max_keystrokes_per_second})"

        return None

    def clear(self):
        """Clear activity buffer."""
        self._activity_buffer.clear()


# =============================================================================
# Agentic Watchdog
# =============================================================================

class AgenticWatchdog:
    """
    The main watchdog service that monitors agentic execution.

    Responsibilities:
    - Monitor heartbeats from agentic tasks
    - Detect activity anomalies (click storms, etc.)
    - Trigger kill switch on safety violations
    - Manage mode transitions (autonomous -> passive)
    - Provide audit logging
    """

    def __init__(
        self,
        config: Optional[WatchdogConfig] = None,
        tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.config = config or WatchdogConfig()
        self._tts_callback = tts_callback

        # State
        self._mode = AgenticMode.PASSIVE
        self._active_task_id: Optional[str] = None
        self._active_goal: Optional[str] = None
        self._last_heartbeat: Optional[float] = None
        self._consecutive_failures = 0
        self._kill_switch_armed = False
        self._start_time = time.time()
        self._events_logged = 0
        self._cooldown_until: Optional[float] = None

        # Components
        self._activity_analyzer = ActivityAnalyzer(self.config)
        self._event_log: deque[Dict[str, Any]] = deque(maxlen=1000)

        # Tasks
        self._monitor_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

        # Callbacks
        self._on_kill_callbacks: List[Callable[[], Awaitable[None]]] = []
        self._on_mode_change_callbacks: List[Callable[[AgenticMode, AgenticMode], Awaitable[None]]] = []

        logger.info("[Watchdog] AgenticWatchdog initialized")

    # =========================================================================
    # Properties (v210.0)
    # =========================================================================
    
    @property
    def mode(self) -> AgenticMode:
        """
        v210.0: Current mode of the watchdog.
        
        Provides convenient access to the current mode without needing
        to call get_status().
        
        Returns:
            Current AgenticMode (PASSIVE, AUTONOMOUS, DOWNGRADED, KILLED)
        """
        return self._mode
    
    @property
    def is_active(self) -> bool:
        """v210.0: Whether the watchdog is actively monitoring a task."""
        return self._active_task_id is not None
    
    @property
    def is_autonomous(self) -> bool:
        """v210.0: Whether the watchdog is in autonomous mode."""
        return self._mode == AgenticMode.AUTONOMOUS
    
    @property
    def kill_switch_armed(self) -> bool:
        """v210.0: Whether the kill switch is armed."""
        return self._kill_switch_armed

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self):
        """Start the watchdog monitoring."""
        if self._monitor_task is not None:
            return

        self._shutdown_event.clear()
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("[Watchdog] Started monitoring")

    async def stop(self):
        """Stop the watchdog monitoring."""
        self._shutdown_event.set()

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

        logger.info("[Watchdog] Stopped monitoring")

    # =========================================================================
    # Heartbeat Interface
    # =========================================================================

    def receive_heartbeat(self, heartbeat: Heartbeat):
        """
        Receive a heartbeat from an agentic task.

        This should be called periodically (every 1-2 seconds) by the
        AgenticTaskRunner while executing.
        """
        self._last_heartbeat = time.time()
        self._active_task_id = heartbeat.task_id
        self._active_goal = heartbeat.goal

        self._log_event(WatchdogEvent.HEARTBEAT_RECEIVED, {
            "task_id": heartbeat.task_id,
            "goal": heartbeat.goal,
            "action": heartbeat.current_action,
            "actions_count": heartbeat.actions_count,
        })

        logger.debug(f"[Watchdog] Heartbeat: {heartbeat.current_action} ({heartbeat.actions_count} actions)")

    def record_activity(self, activity_type: str, details: Dict[str, Any] = None):
        """
        Record an activity event (click, keystroke, etc.).

        This should be called by the Computer Use connector for each action.
        """
        self._activity_analyzer.record_activity(activity_type, details)

    # =========================================================================
    # Task Lifecycle
    # =========================================================================

    async def task_started(self, task_id: str, goal: str, mode: AgenticMode):
        """Called when an agentic task starts."""
        # Check cooldown
        if self._cooldown_until and time.time() < self._cooldown_until:
            remaining = self._cooldown_until - time.time()
            raise RuntimeError(f"Watchdog cooldown active ({remaining:.0f}s remaining)")

        # Check if kill switch prevents execution
        if self._mode == AgenticMode.KILLED:
            raise RuntimeError("Agentic execution blocked - kill switch active")

        self._active_task_id = task_id
        self._active_goal = goal
        self._last_heartbeat = time.time()

        # Arm kill switch for autonomous mode
        if mode == AgenticMode.AUTONOMOUS:
            self._kill_switch_armed = True
            logger.info("[Watchdog] Kill switch ARMED for autonomous task")

        self._log_event(WatchdogEvent.TASK_STARTED, {
            "task_id": task_id,
            "goal": goal,
            "mode": mode.value,
        })

        await self._announce(f"Agentic task started: {goal[:50]}")

    async def task_completed(self, task_id: str, success: bool):
        """Called when an agentic task completes."""
        self._log_event(
            WatchdogEvent.TASK_COMPLETED if success else WatchdogEvent.TASK_FAILED,
            {"task_id": task_id, "success": success}
        )

        if not success:
            self._consecutive_failures += 1
            logger.warning(f"[Watchdog] Task failed (consecutive: {self._consecutive_failures})")

            if self._consecutive_failures >= self.config.max_consecutive_failures:
                await self._trigger_downgrade("Too many consecutive failures")
        else:
            self._consecutive_failures = 0

        self._kill_switch_armed = False
        self._active_task_id = None
        self._active_goal = None
        self._activity_analyzer.clear()

    # =========================================================================
    # Mode Management
    # =========================================================================

    async def set_mode(self, new_mode: AgenticMode, reason: str = ""):
        """Change the operating mode."""
        old_mode = self._mode

        if old_mode == new_mode:
            return

        self._mode = new_mode

        self._log_event(WatchdogEvent.MODE_CHANGED, {
            "old_mode": old_mode.value,
            "new_mode": new_mode.value,
            "reason": reason,
        })

        logger.info(f"[Watchdog] Mode changed: {old_mode.value} -> {new_mode.value} ({reason})")

        # Notify callbacks
        for callback in self._on_mode_change_callbacks:
            try:
                await callback(old_mode, new_mode)
            except Exception as e:
                logger.error(f"[Watchdog] Mode change callback failed: {e}")

        # Voice announcement
        mode_names = {
            AgenticMode.PASSIVE: "Passive mode",
            AgenticMode.SUPERVISED: "Supervised mode",
            AgenticMode.AUTONOMOUS: "Autonomous mode",
            AgenticMode.KILLED: "Emergency shutdown",
        }
        await self._announce(f"{mode_names.get(new_mode, new_mode.value)} activated. {reason}")

    def get_mode(self) -> AgenticMode:
        """Get current operating mode."""
        return self._mode

    def is_agentic_allowed(self) -> bool:
        """Check if agentic execution is currently allowed."""
        if self._mode == AgenticMode.KILLED:
            return False
        if self._cooldown_until and time.time() < self._cooldown_until:
            return False
        return True

    # =========================================================================
    # Kill Switch
    # =========================================================================

    async def trigger_kill_switch(self, reason: str):
        """
        Trigger the emergency kill switch.

        This immediately:
        1. Terminates any running agentic task
        2. Sets mode to KILLED
        3. Activates cooldown
        4. Announces the event
        """
        logger.warning(f"[Watchdog] KILL SWITCH TRIGGERED: {reason}")

        self._log_event(WatchdogEvent.KILL_SWITCH_TRIGGERED, {"reason": reason})

        # Notify kill callbacks
        for callback in self._on_kill_callbacks:
            try:
                await callback()
            except Exception as e:
                logger.error(f"[Watchdog] Kill callback failed: {e}")

        # Set mode and cooldown
        await self.set_mode(AgenticMode.KILLED, reason)
        self._cooldown_until = time.time() + self.config.cooldown_after_kill

        # Clear state
        self._kill_switch_armed = False
        self._active_task_id = None
        self._active_goal = None
        self._activity_analyzer.clear()

        await self._announce(
            f"Emergency shutdown activated. {reason}. "
            f"Agentic control disabled for {self.config.cooldown_after_kill:.0f} seconds.",
            priority=True
        )

    async def _trigger_downgrade(self, reason: str):
        """Downgrade from autonomous to passive mode."""
        self._log_event(WatchdogEvent.DOWNGRADE_INITIATED, {"reason": reason})

        logger.warning(f"[Watchdog] Downgrading to passive mode: {reason}")

        # Stop current task if running
        if self._active_task_id:
            for callback in self._on_kill_callbacks:
                try:
                    await callback()
                except Exception as e:
                    logger.error(f"[Watchdog] Kill callback during downgrade failed: {e}")

        await self.set_mode(AgenticMode.PASSIVE, f"Downgraded: {reason}")
        self._cooldown_until = time.time() + self.config.cooldown_after_kill

    async def recover(self):
        """Recover from killed state after cooldown."""
        if self._mode != AgenticMode.KILLED:
            return

        if self._cooldown_until and time.time() < self._cooldown_until:
            remaining = self._cooldown_until - time.time()
            raise RuntimeError(f"Cannot recover yet - {remaining:.0f}s remaining in cooldown")

        self._log_event(WatchdogEvent.RECOVERY_STARTED, {})

        self._consecutive_failures = 0
        self._cooldown_until = None

        await self.set_mode(AgenticMode.PASSIVE, "Recovered from emergency shutdown")

        await self._announce("Agentic control recovered. Ready for new commands.")

    # =========================================================================
    # Callbacks
    # =========================================================================

    def on_kill(self, callback: Callable[[], Awaitable[None]]):
        """Register a callback for when kill switch is triggered."""
        self._on_kill_callbacks.append(callback)

    def on_mode_change(self, callback: Callable[[AgenticMode, AgenticMode], Awaitable[None]]):
        """Register a callback for mode changes."""
        self._on_mode_change_callbacks.append(callback)

    # =========================================================================
    # Status
    # =========================================================================

    def get_status(self) -> WatchdogStatus:
        """Get current watchdog status."""
        heartbeat_healthy = True
        if self._active_task_id and self._kill_switch_armed:
            if self._last_heartbeat:
                elapsed = time.time() - self._last_heartbeat
                heartbeat_healthy = elapsed < self.config.heartbeat_timeout_seconds

        return WatchdogStatus(
            mode=self._mode,
            active_task_id=self._active_task_id,
            active_goal=self._active_goal,
            last_heartbeat=self._last_heartbeat,
            heartbeat_healthy=heartbeat_healthy,
            activity_rate=self._activity_analyzer.get_all_rates(),
            consecutive_failures=self._consecutive_failures,
            kill_switch_armed=self._kill_switch_armed,
            uptime_seconds=time.time() - self._start_time,
            events_logged=self._events_logged,
        )

    # =========================================================================
    # Internal
    # =========================================================================

    async def _monitor_loop(self):
        """Main monitoring loop."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(1.0)  # Check every second

                # Skip if no active task or not armed
                if not self._active_task_id or not self._kill_switch_armed:
                    continue

                # Check heartbeat timeout
                if self._last_heartbeat:
                    elapsed = time.time() - self._last_heartbeat

                    # Grace period for task startup
                    if elapsed > self.config.heartbeat_grace_period:
                        logger.warning(f"[Watchdog] Heartbeat timeout ({elapsed:.1f}s)")
                        self._log_event(WatchdogEvent.HEARTBEAT_TIMEOUT, {
                            "elapsed": elapsed,
                            "task_id": self._active_task_id,
                        })
                        await self.trigger_kill_switch(f"Heartbeat timeout ({elapsed:.0f}s)")
                        continue

                # Check activity anomalies
                anomaly = self._activity_analyzer.check_anomaly()
                if anomaly:
                    logger.warning(f"[Watchdog] Activity anomaly: {anomaly}")
                    self._log_event(WatchdogEvent.ACTIVITY_SPIKE, {"anomaly": anomaly})
                    await self.trigger_kill_switch(f"Activity anomaly: {anomaly}")
                    continue

                # Check cooldown expiry
                if self._mode == AgenticMode.KILLED and self._cooldown_until:
                    if time.time() >= self._cooldown_until:
                        logger.info("[Watchdog] Cooldown expired - ready for recovery")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Watchdog] Monitor loop error: {e}")

    def _log_event(self, event_type: WatchdogEvent, data: Dict[str, Any]):
        """Log a watchdog event."""
        self._events_logged += 1

        event = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type.value,
            "data": data,
        }

        self._event_log.append(event)

        # Write to audit log if configured
        if self.config.audit_log_path:
            try:
                import json
                with open(self.config.audit_log_path, "a") as f:
                    f.write(json.dumps(event) + "\n")
            except Exception as e:
                logger.error(f"[Watchdog] Failed to write audit log: {e}")

    async def _announce(self, text: str, priority: bool = False):
        """Announce via TTS if enabled."""
        if not self.config.voice_enabled:
            return

        if self._tts_callback:
            try:
                await self._tts_callback(text)
            except Exception as e:
                logger.debug(f"[Watchdog] TTS callback failed: {e}")


# =============================================================================
# Singleton Access
# =============================================================================

_watchdog_instance: Optional[AgenticWatchdog] = None


def get_watchdog() -> AgenticWatchdog:
    """Get the global watchdog instance."""
    global _watchdog_instance
    if _watchdog_instance is None:
        _watchdog_instance = AgenticWatchdog()
    return _watchdog_instance


def set_watchdog(watchdog: AgenticWatchdog):
    """Set the global watchdog instance."""
    global _watchdog_instance
    _watchdog_instance = watchdog


async def start_watchdog(
    config: Optional[WatchdogConfig] = None,
    tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> AgenticWatchdog:
    """Create, configure, and start the watchdog."""
    global _watchdog_instance

    _watchdog_instance = AgenticWatchdog(config=config, tts_callback=tts_callback)
    await _watchdog_instance.start()

    return _watchdog_instance


async def stop_watchdog():
    """Stop the global watchdog."""
    global _watchdog_instance

    if _watchdog_instance:
        await _watchdog_instance.stop()
        _watchdog_instance = None
