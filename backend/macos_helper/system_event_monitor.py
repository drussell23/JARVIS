"""
JARVIS macOS Helper - System Event Monitor

Real-time monitoring of macOS system events using NSWorkspace notifications.
Provides async event emission for app launches, focus changes, space transitions,
and system state changes.

Features:
- App lifecycle monitoring (launch, terminate, activate, hide)
- Window focus tracking via Accessibility API
- Space/desktop change detection via Yabai or direct monitoring
- System state monitoring (sleep, wake, screen lock/unlock)
- User activity detection (idle, active)
- Async event emission with batching
- Integration with existing Yabai Spatial Intelligence

Apple Compliance:
- Uses NSWorkspace notifications (public API)
- Accessibility API requires user permission
- No private API usage
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# =============================================================================
# Monitor Configuration
# =============================================================================

def _env_float(key: str, default: float) -> float:
    """Read float from env with safe fallback."""
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _clamp(value: float, minimum: float, maximum: float) -> float:
    """Clamp float to an inclusive range."""
    return max(minimum, min(maximum, value))


@dataclass
class MonitorConfig:
    """Configuration for the system event monitor."""
    # Polling intervals
    window_poll_interval_ms: int = 100  # Window focus polling
    space_poll_interval_ms: int = 250  # Space change polling
    idle_poll_interval_ms: int = 5000  # User idle polling

    # Feature flags
    enable_app_monitoring: bool = True
    enable_window_monitoring: bool = True
    enable_space_monitoring: bool = True
    enable_system_state_monitoring: bool = True
    enable_idle_monitoring: bool = True

    # Yabai integration
    use_yabai_if_available: bool = True
    yabai_path: str = "/opt/homebrew/bin/yabai"

    # Idle thresholds
    idle_threshold_seconds: float = 300.0  # 5 minutes
    short_idle_threshold_seconds: float = 30.0  # 30 seconds

    # Batching
    enable_event_batching: bool = True
    batch_window_ms: int = 50  # Batch events within 50ms

    # Startup and subprocess hardening
    startup_step_timeout_seconds: float = field(
        default_factory=lambda: max(
            0.25,
            _env_float("JARVIS_SYSTEM_EVENT_STARTUP_STEP_TIMEOUT", 4.0),
        )
    )
    # Compatibility note: this now represents warmup target duration (SLO),
    # not a hard timeout that can fail startup.
    startup_warmup_timeout_seconds: float = field(
        default_factory=lambda: max(
            1.0,
            _env_float("JARVIS_SYSTEM_EVENT_STARTUP_WARMUP_TIMEOUT", 12.0),
        )
    )
    startup_forecast_alpha: float = field(
        default_factory=lambda: _clamp(
            _env_float("JARVIS_SYSTEM_EVENT_STARTUP_FORECAST_ALPHA", 0.35),
            0.05,
            0.95,
        )
    )
    subprocess_timeout_seconds: float = field(
        default_factory=lambda: max(
            0.25,
            _env_float("JARVIS_SYSTEM_EVENT_SUBPROCESS_TIMEOUT", 3.0),
        )
    )


# =============================================================================
# System Event Monitor
# =============================================================================

class SystemEventMonitor:
    """
    Monitors macOS system events and emits them to the event bus.

    Uses multiple sources:
    - NSWorkspace notifications for app lifecycle
    - Accessibility API for window focus
    - Yabai for space management (if available)
    - IOKit for idle time
    """

    def __init__(self, config: Optional[MonitorConfig] = None):
        """
        Initialize the system event monitor.

        Args:
            config: Monitor configuration (uses defaults if None)
        """
        self.config = config or MonitorConfig()

        # State tracking
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._start_lock = asyncio.Lock()
        self._startup_state: str = "idle"
        self._startup_error: Optional[str] = None
        self._startup_phase: str = "idle"
        self._startup_progress: float = 0.0
        self._startup_started_at: Optional[datetime] = None
        self._startup_completed_at: Optional[datetime] = None
        self._startup_forecast_ready_at: Optional[datetime] = None
        self._startup_phase_estimates_seconds: Dict[str, float] = {}
        self._startup_phase_results: Dict[str, str] = {}
        self._startup_task: Optional[asyncio.Task] = None

        # App state
        self._running_apps: Dict[str, Dict[str, Any]] = {}  # bundle_id -> app info
        self._frontmost_app: Optional[str] = None  # bundle_id

        # Window state
        self._focused_window: Optional[Dict[str, Any]] = None
        self._windows: Dict[int, Dict[str, Any]] = {}  # window_id -> window info

        # Space state
        self._current_space: int = 1
        self._spaces: Dict[int, Dict[str, Any]] = {}

        # System state
        self._is_screen_locked: bool = False
        # Set by LockStateListener from its own thread via
        # loop.call_soon_threadsafe -- so `set` runs ON the loop and the
        # Event needs no lock of its own. Created here rather than in the
        # loop so `_start_lock_listener` can hand out the bound method
        # before the first await.
        self._lock_event: asyncio.Event = asyncio.Event()
        self._lock_listener: Optional[Any] = None
        self._is_system_sleeping: bool = False
        self._last_user_activity: datetime = datetime.now()
        self._is_idle: bool = False

        # Yabai integration
        self._yabai_available: bool = False
        self._yabai_si = None  # YabaiSpatialIntelligence instance

        # Event bus (lazy loaded)
        self._event_bus = None

        # Event batching
        self._pending_events: List[Any] = []
        self._batch_lock = asyncio.Lock()

        logger.info("SystemEventMonitor initialized")

    async def _run_subprocess(
        self,
        *command: str,
        timeout_seconds: Optional[float] = None,
    ) -> tuple[int, bytes, bytes]:
        """
        Run a subprocess with deterministic timeout and cancellation cleanup.
        """
        timeout = max(
            0.1,
            float(
                self.config.subprocess_timeout_seconds
                if timeout_seconds is None
                else timeout_seconds
            ),
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _terminate_process() -> None:
            if process.returncode is not None:
                return
            try:
                process.terminate()
            except ProcessLookupError:
                return
            except Exception:
                pass
            try:
                await asyncio.wait_for(process.communicate(), timeout=0.5)
            except Exception:
                try:
                    process.kill()
                except ProcessLookupError:
                    return
                except Exception:
                    pass
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.communicate(), timeout=0.5)

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return int(process.returncode or 0), stdout, stderr
        except asyncio.TimeoutError:
            await _terminate_process()
            raise
        except asyncio.CancelledError:
            await _terminate_process()
            raise
        except Exception:
            await _terminate_process()
            raise

    def _register_task(self, task: asyncio.Task) -> None:
        """Track task lifecycle so stop() can cancel deterministically."""
        self._tasks.append(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            try:
                self._tasks.remove(done_task)
            except ValueError:
                pass

        task.add_done_callback(_cleanup)

    def _create_monitored_task(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
        """Create a tracked task with a stable name."""
        task = asyncio.create_task(coro, name=name)
        self._register_task(task)
        return task

    async def _cancel_tasks_by_name(self, task_name: str) -> None:
        """Cancel tracked tasks by name."""
        targets = [
            task for task in list(self._tasks)
            if not task.done() and task.get_name() == task_name
        ]
        for task in targets:
            task.cancel()
        if targets:
            with contextlib.suppress(Exception):
                await asyncio.wait(
                    targets,
                    timeout=max(0.25, self.config.startup_step_timeout_seconds),
                )

    def _estimate_startup_phase_seconds(self, phase_name: str) -> float:
        """Estimate expected duration for a startup warmup phase."""
        estimate = self._startup_phase_estimates_seconds.get(phase_name)
        if estimate is not None:
            return max(0.05, estimate)

        base = max(0.25, self.config.startup_step_timeout_seconds)
        phase_multipliers = {
            "yabai_probe": 0.75,
            "yabai_integration": 1.0,
            "initial_state_capture": 1.5,
        }
        return base * phase_multipliers.get(phase_name, 1.0)

    def _estimate_startup_remaining_seconds(self, phase_names: List[str]) -> float:
        """Estimate remaining warmup time for the specified phases."""
        return max(
            0.0,
            sum(self._estimate_startup_phase_seconds(name) for name in phase_names),
        )

    def _record_startup_phase_duration(self, phase_name: str, duration_seconds: float) -> None:
        """Update phase duration estimate using EWMA forecasting."""
        duration = max(0.01, duration_seconds)
        previous = self._startup_phase_estimates_seconds.get(phase_name)
        if previous is None:
            self._startup_phase_estimates_seconds[phase_name] = duration
            return
        alpha = self.config.startup_forecast_alpha
        self._startup_phase_estimates_seconds[phase_name] = (
            (alpha * duration) + ((1.0 - alpha) * previous)
        )

    async def _warmup_yabai_integration(self) -> None:
        """
        Initialize Yabai integration during warmup when available.
        """
        if not self.config.use_yabai_if_available:
            return
        if not self._yabai_available:
            return
        await asyncio.wait_for(
            self._init_yabai_integration(),
            timeout=max(0.25, self.config.startup_step_timeout_seconds),
        )
        if self._yabai_si:
            # Yabai integration supersedes direct polling.
            await self._cancel_tasks_by_name("space_monitor")

    def _startup_eta_seconds(self) -> Optional[float]:
        """Current startup ETA from forecast, if available."""
        if self._startup_forecast_ready_at is None:
            return None
        return max(
            0.0,
            (self._startup_forecast_ready_at - datetime.now()).total_seconds(),
        )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Start monitoring with fast bootstrap + background warmup."""
        if self._running:
            logger.warning("SystemEventMonitor already running")
            return

        async with self._start_lock:
            if self._running:
                logger.warning("SystemEventMonitor already running")
                return

            self._startup_state = "starting"
            self._startup_error = None
            self._startup_phase = "bootstrap"
            self._startup_progress = 0.0
            self._startup_started_at = datetime.now()
            self._startup_completed_at = None
            self._startup_forecast_ready_at = None
            self._startup_phase_results.clear()

            # Critical dependency: event bus must be available.
            try:
                from .event_bus import get_macos_event_bus
                self._event_bus = await asyncio.wait_for(
                    get_macos_event_bus(),
                    timeout=self.config.startup_step_timeout_seconds,
                )
            except Exception as e:
                self._startup_state = "failed"
                self._startup_error = f"event bus init failed: {e}"
                logger.error("Failed to initialize event bus: %s", e)
                return

            self._running = True

            # Start monitor loops first; expensive environment probing runs
            # in a background warmup task so startup stays deterministic.
            await self._start_monitoring_tasks()

            self._startup_task = self._create_monitored_task(
                self._complete_startup_warmup(),
                name="system_event_monitor_warmup",
            )
            planned_phases = [
                "yabai_probe",
                "initial_state_capture",
            ]
            if self.config.use_yabai_if_available:
                planned_phases.append("yabai_integration")
            self._startup_forecast_ready_at = datetime.now() + timedelta(
                seconds=self._estimate_startup_remaining_seconds(planned_phases)
            )
            self._startup_phase = "warmup"
            self._startup_state = "running"
            self._startup_completed_at = datetime.now()
            logger.info("SystemEventMonitor started (warmup in background)")

    async def stop(self) -> None:
        """Stop all monitoring tasks."""
        async with self._start_lock:
            if not self._running and not self._tasks:
                return

            self._running = False
            self._startup_state = "stopping"

            # Released BEFORE the tasks are cancelled: the listener owns an
            # OS thread and four descriptors, and a daemon thread blocked in
            # select() outlives a cancelled coroutine. Its stop() wakes that
            # thread through its self-pipe rather than waiting on a timeout.
            listener = self._lock_listener
            self._lock_listener = None
            if listener is not None:
                try:
                    listener.stop()
                except Exception:  # noqa: BLE001 — shutdown must not raise
                    logger.debug("[SystemEventMonitor] listener stop failed",
                                 exc_info=True)

            active_tasks = [task for task in list(self._tasks) if not task.done()]
            for task in active_tasks:
                task.cancel()

            if active_tasks:
                done, pending = await asyncio.wait(
                    active_tasks,
                    timeout=max(0.5, self.config.startup_step_timeout_seconds),
                )
                for pending_task in pending:
                    logger.warning(
                        "SystemEventMonitor task did not stop cleanly: %s",
                        pending_task.get_name(),
                    )

            self._tasks.clear()
            self._startup_task = None
            self._startup_phase = "stopped"
            self._startup_progress = 0.0
            self._startup_forecast_ready_at = None

            # Stop Yabai integration
            if self._yabai_si:
                try:
                    await asyncio.wait_for(
                        self._yabai_si.stop_monitoring(),
                        timeout=self.config.startup_step_timeout_seconds,
                    )
                except Exception as e:
                    logger.warning(f"Error stopping Yabai SI: {e}")
                finally:
                    self._yabai_si = None

            self._startup_state = "stopped"
            logger.info("SystemEventMonitor stopped")

    async def _start_monitoring_tasks(self) -> None:
        """Start individual monitoring tasks."""
        if self.config.enable_app_monitoring:
            self._create_monitored_task(self._app_monitoring_loop(), name="app_monitor")

        if self.config.enable_window_monitoring:
            self._create_monitored_task(self._window_monitoring_loop(), name="window_monitor")

        if self.config.enable_space_monitoring and not self._yabai_si:
            # Only run our own space monitoring if not using Yabai SI
            self._create_monitored_task(self._space_monitoring_loop(), name="space_monitor")

        if self.config.enable_system_state_monitoring:
            self._create_monitored_task(
                self._system_state_monitoring_loop(),
                name="system_state_monitor",
            )

        if self.config.enable_idle_monitoring:
            self._create_monitored_task(self._idle_monitoring_loop(), name="idle_monitor")

        if self.config.enable_event_batching:
            self._create_monitored_task(
                self._event_batch_processor(),
                name="event_batch_processor",
            )

    async def _complete_startup_warmup(self) -> None:
        """
        Run startup warmup phases with ETA forecasting.

        This intentionally avoids hard global timeout semantics. Startup
        readiness is driven by phase completion, while per-operation bounds
        remain enforced inside each phase (e.g. subprocess timeouts).
        """
        target_seconds = max(1.0, self.config.startup_warmup_timeout_seconds)
        phases: List[tuple[str, bool, Callable[[], Coroutine[Any, Any, None]]]] = [
            ("yabai_probe", False, self._check_yabai),
            ("initial_state_capture", True, self._capture_initial_state),
        ]
        if self.config.use_yabai_if_available:
            phases.append(("yabai_integration", False, self._warmup_yabai_integration))

        total_phases = max(1, len(phases))
        critical_failures: List[str] = []
        optional_failures: List[str] = []
        started = time.monotonic()

        self._startup_phase = "warmup"
        self._startup_progress = 0.0
        self._startup_forecast_ready_at = datetime.now() + timedelta(
            seconds=self._estimate_startup_remaining_seconds(
                [phase_name for phase_name, _, _ in phases]
            )
        )

        try:
            for index, (phase_name, is_critical, phase_action) in enumerate(phases):
                self._startup_phase = phase_name
                pending = [name for name, _, _ in phases[index:]]
                self._startup_forecast_ready_at = datetime.now() + timedelta(
                    seconds=self._estimate_startup_remaining_seconds(pending)
                )

                phase_started = time.monotonic()
                try:
                    await phase_action()
                    self._startup_phase_results[phase_name] = "ready"
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    message = f"{phase_name}: {e}"
                    self._startup_phase_results[phase_name] = f"failed: {e}"
                    if is_critical:
                        critical_failures.append(message)
                    else:
                        optional_failures.append(message)
                finally:
                    self._record_startup_phase_duration(
                        phase_name,
                        time.monotonic() - phase_started,
                    )
                    self._startup_progress = min(
                        1.0,
                        float(index + 1) / float(total_phases),
                    )

            elapsed = time.monotonic() - started
            self._startup_phase = "ready"
            self._startup_forecast_ready_at = datetime.now()

            if critical_failures:
                if self._running:
                    self._startup_state = "degraded"
                details = "; ".join(critical_failures)
                self._startup_error = f"critical startup warmup failures: {details}"
                logger.warning(
                    "SystemEventMonitor warmup completed with critical failures after %.2fs: %s",
                    elapsed,
                    details,
                )
                return

            self._startup_state = "ready"
            self._startup_error = None
            if optional_failures:
                logger.info(
                    "SystemEventMonitor warmup ready in %.2fs with optional phase issues: %s",
                    elapsed,
                    "; ".join(optional_failures),
                )
            else:
                logger.info(
                    "SystemEventMonitor warmup ready in %.2fs (target %.2fs)",
                    elapsed,
                    target_seconds,
                )

            if elapsed > target_seconds:
                logger.info(
                    "SystemEventMonitor warmup exceeded target by %.2fs (forecast mode, no timeout)",
                    elapsed - target_seconds,
                )
        except asyncio.CancelledError:
            self._startup_phase = "cancelled"
            self._startup_forecast_ready_at = None
            raise
        except Exception as e:  # pragma: no cover - defensive guardrail
            if self._running:
                self._startup_state = "degraded"
            self._startup_phase = "failed"
            self._startup_error = f"startup warmup orchestrator failed: {e}"
            self._startup_forecast_ready_at = None
            logger.warning(
                "SystemEventMonitor warmup orchestration failed: %s (live monitoring remains active)",
                e,
            )

    async def _capture_initial_state(self) -> None:
        """Capture initial system state on startup."""
        checks: List[tuple[str, Callable[[], Coroutine[Any, Any, Any]]]] = []
        if self.config.enable_app_monitoring:
            checks.append(("running_apps", self._update_running_apps))
        if self.config.enable_space_monitoring:
            checks.append(("current_space", self._update_current_space))
        if self.config.enable_system_state_monitoring:
            checks.append(("screen_lock", self._update_screen_lock_status))

        if not checks:
            return

        step_timeout = max(0.25, self.config.startup_step_timeout_seconds)

        async def _run_check(
            check_name: str,
            check_action: Callable[[], Coroutine[Any, Any, Any]],
        ) -> tuple[str, Optional[str]]:
            try:
                result = await asyncio.wait_for(check_action(), timeout=step_timeout)
                if result is False:
                    return (check_name, "returned unsuccessful result")
                return (check_name, None)
            except asyncio.TimeoutError:
                return (check_name, f"timed out after {step_timeout:.2f}s")
            except Exception as e:
                return (check_name, str(e))

        results = await asyncio.gather(
            *[_run_check(name, action) for name, action in checks],
            return_exceptions=False,
        )
        failures = [f"{name}: {error}" for name, error in results if error]

        if failures:
            if len(failures) == len(results):
                raise RuntimeError("; ".join(failures))
            logger.info(
                "Initial state capture partial success (%d/%d checks passed): %s",
                len(results) - len(failures),
                len(results),
                "; ".join(failures),
            )

        logger.debug(
            "Initial state: apps=%d, space=%s, locked=%s",
            len(self._running_apps),
            self._current_space,
            self._is_screen_locked,
        )

    # =========================================================================
    # Yabai Integration
    # =========================================================================

    async def _check_yabai(self) -> None:
        """Check if Yabai is available."""
        try:
            return_code, stdout, _ = await self._run_subprocess(
                self.config.yabai_path,
                "-v",
                timeout_seconds=self.config.startup_step_timeout_seconds,
            )
            if return_code == 0:
                self._yabai_available = True
                logger.info(f"Yabai available: {stdout.decode().strip()}")
            else:
                self._yabai_available = False
                logger.info("Yabai not available")

        except asyncio.TimeoutError:
            self._yabai_available = False
            logger.info("Yabai availability probe timed out")
        except FileNotFoundError:
            self._yabai_available = False
            logger.info("Yabai not installed")
        except Exception as e:
            self._yabai_available = False
            logger.warning(f"Yabai check failed: {e}")

    async def _init_yabai_integration(self) -> None:
        """Initialize integration with existing Yabai Spatial Intelligence."""
        try:
            from intelligence.yabai_spatial_intelligence import (
                YabaiSpatialIntelligence,
                YabaiEventType,
            )

            self._yabai_si = YabaiSpatialIntelligence()

            # Register event listener to bridge Yabai events
            # v250.0: Use YabaiEventType enums — register_event_listener expects
            # enum instances, not strings (calling .value on a string crashes)
            self._yabai_si.register_event_listener(
                YabaiEventType.SPACE_CHANGED,
                self._on_yabai_space_changed
            )
            self._yabai_si.register_event_listener(
                YabaiEventType.WINDOW_FOCUSED,
                self._on_yabai_window_focused
            )
            self._yabai_si.register_event_listener(
                YabaiEventType.APP_LAUNCHED,
                self._on_yabai_app_launched
            )

            # Start Yabai monitoring
            await self._yabai_si.start_monitoring()
            logger.info("Yabai Spatial Intelligence integration active")

        except ImportError:
            logger.info("Yabai Spatial Intelligence not available, using direct monitoring")
            self._yabai_si = None
        except Exception as e:
            logger.warning(f"Failed to initialize Yabai SI: {e}")
            self._yabai_si = None

    async def _on_yabai_space_changed(self, yabai_event: Any) -> None:
        """Handle space change from Yabai SI."""
        from .event_types import MacOSEventFactory
        event = MacOSEventFactory.create_space_changed(
            new_space_id=yabai_event.space_id,
            new_space_index=yabai_event.space_index,
            previous_space_id=self._current_space,
        )
        self._current_space = yabai_event.space_id
        await self._emit_event(event)

    async def _on_yabai_window_focused(self, yabai_event: Any) -> None:
        """Handle window focus from Yabai SI."""
        from .event_types import MacOSEventFactory
        event = MacOSEventFactory.create_window_focused(
            app_name=yabai_event.app_name,
            window_id=yabai_event.window_id,
            window_title=yabai_event.title,
        )
        await self._emit_event(event)

    async def _on_yabai_app_launched(self, yabai_event: Any) -> None:
        """Handle app launch from Yabai SI."""
        from .event_types import MacOSEventFactory
        event = MacOSEventFactory.create_app_launched(
            app_name=yabai_event.app_name,
            bundle_id=getattr(yabai_event, 'bundle_id', ''),
        )
        await self._emit_event(event)

    # =========================================================================
    # App Monitoring
    # =========================================================================

    async def _app_monitoring_loop(self) -> None:
        """Monitor app launches and terminations via NSWorkspace."""
        while self._running:
            try:
                await asyncio.sleep(0.5)  # Check every 500ms
                await self._update_running_apps()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"App monitoring error: {e}")
                await asyncio.sleep(1)

    async def _update_running_apps(self) -> bool:
        """Update running apps and emit events for changes."""
        try:
            # Get running apps via AppleScript
            script = """
            tell application "System Events"
                set appList to {}
                repeat with p in (processes whose background only is false)
                    set end of appList to {name of p, bundle identifier of p, frontmost of p}
                end repeat
                return appList
            end tell
            """
            return_code, stdout, _ = await self._run_subprocess(
                "osascript",
                "-e",
                script,
            )
            if return_code != 0:
                return False

            # Parse output
            current_apps: Dict[str, Dict[str, Any]] = {}
            output = stdout.decode().strip()

            # Parse AppleScript list format
            # Format: {{name, bundle_id, frontmost}, ...}
            if output.startswith("{") and output.endswith("}"):
                # Simple parsing - could be more robust
                import re
                app_pattern = r'\{([^,]+), ([^,]+), (true|false)\}'
                matches = re.findall(app_pattern, output.replace("missing value", ""))

                for name, bundle_id, frontmost in matches:
                    name = name.strip()
                    bundle_id = bundle_id.strip()
                    is_frontmost = frontmost.strip() == "true"

                    current_apps[bundle_id] = {
                        "name": name,
                        "bundle_id": bundle_id,
                        "is_frontmost": is_frontmost,
                    }

                    # Track frontmost app
                    if is_frontmost and self._frontmost_app != bundle_id:
                        old_frontmost = self._frontmost_app
                        self._frontmost_app = bundle_id
                        await self._emit_app_activated(current_apps[bundle_id])

            # Detect launches and terminations
            from .event_types import MacOSEventFactory

            # New apps
            for bundle_id, info in current_apps.items():
                if bundle_id not in self._running_apps:
                    event = MacOSEventFactory.create_app_launched(
                        app_name=info["name"],
                        bundle_id=bundle_id,
                    )
                    await self._emit_event(event)

            # Terminated apps
            for bundle_id, info in self._running_apps.items():
                if bundle_id not in current_apps:
                    from .event_types import MacOSEventType, AppEvent
                    event = AppEvent(
                        event_type=MacOSEventType.APP_TERMINATED,
                        source="system_event_monitor",
                        app_name=info["name"],
                        bundle_id=bundle_id,
                        requires_agi_processing=True,
                    )
                    await self._emit_event(event)

            self._running_apps = current_apps
            return True

        except Exception as e:
            logger.debug(f"Error updating running apps: {e}")
            return False

    async def _emit_app_activated(self, app_info: Dict[str, Any]) -> None:
        """Emit app activated event."""
        from .event_types import MacOSEventFactory
        event = MacOSEventFactory.create_app_activated(
            app_name=app_info["name"],
            bundle_id=app_info["bundle_id"],
        )
        await self._emit_event(event)

    # =========================================================================
    # Window Monitoring
    # =========================================================================

    async def _window_monitoring_loop(self) -> None:
        """Monitor window focus changes via Accessibility API."""
        while self._running:
            try:
                await asyncio.sleep(self.config.window_poll_interval_ms / 1000)
                await self._update_focused_window()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Window monitoring error: {e}")
                await asyncio.sleep(0.5)

    async def _update_focused_window(self) -> None:
        """Update focused window information."""
        try:
            # Get focused window via AppleScript
            script = """
            tell application "System Events"
                set frontApp to first application process whose frontmost is true
                set appName to name of frontApp
                try
                    set frontWindow to first window of frontApp
                    set winTitle to name of frontWindow
                    return {appName, winTitle}
                on error
                    return {appName, ""}
                end try
            end tell
            """
            return_code, stdout, _ = await self._run_subprocess(
                "osascript",
                "-e",
                script,
            )
            if return_code != 0:
                return

            output = stdout.decode().strip()
            # Parse: {appName, windowTitle}
            if output.startswith("{") and output.endswith("}"):
                parts = output[1:-1].split(", ", 1)
                if len(parts) >= 1:
                    app_name = parts[0].strip()
                    window_title = parts[1].strip() if len(parts) > 1 else ""

                    new_window = {
                        "app_name": app_name,
                        "title": window_title,
                    }

                    # Check if window changed
                    if (not self._focused_window or
                        self._focused_window.get("app_name") != app_name or
                        self._focused_window.get("title") != window_title):

                        old_window = self._focused_window
                        self._focused_window = new_window

                        # Emit window focused event
                        from .event_types import MacOSEventFactory
                        event = MacOSEventFactory.create_window_focused(
                            app_name=app_name,
                            window_id=0,  # Not available via AppleScript
                            window_title=window_title,
                        )
                        await self._emit_event(event)

        except Exception as e:
            logger.debug(f"Error updating focused window: {e}")

    # =========================================================================
    # Space Monitoring
    # =========================================================================

    async def _space_monitoring_loop(self) -> None:
        """Monitor space/desktop changes (fallback when Yabai SI not used)."""
        while self._running:
            try:
                await asyncio.sleep(self.config.space_poll_interval_ms / 1000)
                await self._update_current_space()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Space monitoring error: {e}")
                await asyncio.sleep(1)

    async def _update_current_space(self) -> bool:
        """Update current space information."""
        try:
            if self._yabai_available:
                # Use Yabai for space info
                return_code, stdout, _ = await self._run_subprocess(
                    self.config.yabai_path,
                    "-m",
                    "query",
                    "--spaces",
                    "--space",
                )
                if return_code == 0:
                    import json
                    space_info = json.loads(stdout.decode())
                    new_space = space_info.get("index", 1)

                    if new_space != self._current_space:
                        old_space = self._current_space
                        self._current_space = new_space

                        from .event_types import MacOSEventFactory
                        event = MacOSEventFactory.create_space_changed(
                            new_space_id=new_space,
                            new_space_index=new_space,
                            previous_space_id=old_space,
                        )
                        await self._emit_event(event)
                else:
                    return False
            else:
                # Fallback: Can't reliably detect space changes without Yabai
                pass
            return True

        except Exception as e:
            logger.debug(f"Error updating space: {e}")
            return False

    # =========================================================================
    # System State Monitoring
    # =========================================================================

    async def _system_state_monitoring_loop(self) -> None:
        """React to lock/unlock; fall back to asking only when nothing tells us.

        Was `sleep(1)` forever. Measured on a live supervisor, that asked the
        OS 571 times in 9.8 minutes and got the same answer 571 times, while
        emitting 64% of everything the process logged.

        The loop is now EVENT-PRIMARY: `LockStateListener` wakes it from a
        Darwin `notify(3)` descriptor the instant the console state moves.
        The sleep is DEMOTED, not deleted -- registration cannot prove macOS
        posts those keys on this OS version (see the listener docstring), so
        an unproven producer must degrade to the old cadence, never to
        blindness. Same event-primary-with-a-floor shape the intake sensors
        use for `fs.changed.*`.

        The backstop is ADAPTIVE: it starts at the responsive interval and
        backs off geometrically while nothing changes, so a machine that
        never locks stops paying for the question. Any transition -- from the
        listener or from the backstop itself -- collapses it back to
        responsive, because the moment right after a change is the moment the
        next one is most likely.
        """
        self._start_lock_listener()
        delay = self._lock_backstop_min_s()
        while self._running:
            try:
                # Event-primary: return the instant the OS says something
                # moved. `wait_for` is the timeout, so the backstop is the
                # ceiling on ignorance, not the cadence of the check.
                try:
                    await asyncio.wait_for(self._lock_event.wait(), timeout=delay)
                    self._lock_event.clear()
                    woke_on_event = True
                except asyncio.TimeoutError:
                    woke_on_event = False

                changed = await self._update_screen_lock_status()

                if changed or woke_on_event:
                    delay = self._lock_backstop_min_s()
                else:
                    delay = min(delay * self._lock_backstop_growth(),
                                self._lock_backstop_max_s())

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"System state monitoring error: {e}")
                await asyncio.sleep(self._lock_backstop_min_s() * 2)

    # -- lock backstop tuning (env-driven; nothing here is a literal) -------

    def _lock_backstop_min_s(self) -> float:
        """Responsive interval, used right after any transition."""
        return _clamp(_env_float("JARVIS_LOCK_BACKSTOP_MIN_S", 1.0), 0.1, 60.0)

    def _lock_backstop_max_s(self) -> float:
        """Ceiling while the state is quiet. With the listener live this is
        the ONLY cost of the old poller, paid once per this interval."""
        return _clamp(_env_float("JARVIS_LOCK_BACKSTOP_MAX_S", 60.0),
                      self._lock_backstop_min_s(), 3600.0)

    def _lock_backstop_growth(self) -> float:
        """Geometric back-off factor. >1 or the backstop never widens."""
        return _clamp(_env_float("JARVIS_LOCK_BACKSTOP_GROWTH", 2.0), 1.0, 10.0)

    def _start_lock_listener(self) -> None:
        """Arm the OS listener once. NEVER raises: a failure here means the
        adaptive backstop is the whole mechanism, which is still strictly
        better than the fixed 1Hz poll it replaced."""
        if self._lock_listener is not None:
            return
        try:
            from .lock_state_listener import LockStateListener

            listener = LockStateListener(
                on_change=self._lock_event.set,
                loop=asyncio.get_running_loop(),
            )
            if listener.start():
                self._lock_listener = listener
        except Exception:  # noqa: BLE001
            logger.debug("[SystemEventMonitor] lock listener unavailable",
                         exc_info=True)

    async def _update_screen_lock_status(self) -> bool:
        """Update screen lock status. Returns whether the state CHANGED.

        The return value used to be "did this run without throwing", which
        no caller could act on. It now reports the transition, because the
        adaptive backstop needs to know whether the last look found news.
        """
        try:
            # Try using the existing screen lock detector
            try:
                from voice_unlock.objc.server.screen_lock_detector import is_screen_locked
                # OFF THE LOOP. On the fast path this is a cheap ctypes call,
                # but `is_screen_locked` falls back to `osascript` when
                # CGSession is inconclusive -- a synchronous subprocess on the
                # event loop, and precisely the frame the surviving
                # main-thread StallSampler dump caught.
                from backend.core.async_offload import call_off_loop
                is_locked = await call_off_loop(is_screen_locked)
                if is_locked is None:
                    return False        # probe failed; "unknown" is not "unlocked"
            except ImportError:
                # Fallback: Check via IOKit/CGSession
                script = """
                tell application "System Events"
                    get running of screen saver preferences
                end tell
                """
                _, stdout, _ = await self._run_subprocess(
                    "osascript",
                    "-e",
                    script,
                )
                is_locked = b"true" in stdout.lower()

            # Detect change. This delta gate was already here and already
            # correct -- an event is emitted on TRANSITION, never per probe.
            # It is deliberately left alone: the spam was never this gate, it
            # was the detector logging its own no-news answer at INFO.
            if is_locked != self._is_screen_locked:
                old_status = self._is_screen_locked
                self._is_screen_locked = is_locked

                from .event_types import MacOSEventFactory
                if is_locked:
                    event = MacOSEventFactory.create_screen_locked()
                else:
                    event = MacOSEventFactory.create_screen_unlocked()

                logger.info(
                    "[SystemEventMonitor] screen %s (was %s)",
                    "LOCKED" if is_locked else "UNLOCKED",
                    "locked" if old_status else "unlocked",
                )
                await self._emit_event(event)
                return True
            return False

        except Exception as e:
            logger.debug(f"Error checking screen lock: {e}")
            return False

    # =========================================================================
    # Idle Monitoring
    # =========================================================================

    async def _idle_monitoring_loop(self) -> None:
        """Monitor user idle state."""
        while self._running:
            try:
                await asyncio.sleep(self.config.idle_poll_interval_ms / 1000)
                await self._update_idle_state()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Idle monitoring error: {e}")
                await asyncio.sleep(5)

    async def _update_idle_state(self) -> None:
        """Update user idle state."""
        try:
            # Get idle time via ioreg
            return_code, stdout, _ = await self._run_subprocess(
                "ioreg",
                "-c",
                "IOHIDSystem",
                "-d",
                "4",
            )
            if return_code != 0:
                return

            # Parse HIDIdleTime
            output = stdout.decode()
            import re
            match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', output)

            if match:
                # HIDIdleTime is in nanoseconds
                idle_ns = int(match.group(1))
                idle_seconds = idle_ns / 1_000_000_000

                was_idle = self._is_idle

                # Check if user became idle
                if idle_seconds >= self.config.idle_threshold_seconds and not self._is_idle:
                    self._is_idle = True
                    from .event_types import MacOSEventType, UserActivityEvent
                    event = UserActivityEvent(
                        event_type=MacOSEventType.USER_IDLE_STARTED,
                        source="system_event_monitor",
                        activity_type="idle_started",
                        idle_duration_seconds=idle_seconds,
                    )
                    await self._emit_event(event)

                # Check if user became active
                elif idle_seconds < self.config.short_idle_threshold_seconds and self._is_idle:
                    self._is_idle = False
                    self._last_user_activity = datetime.now()

                    from .event_types import MacOSEventType, UserActivityEvent
                    event = UserActivityEvent(
                        event_type=MacOSEventType.USER_IDLE_ENDED,
                        source="system_event_monitor",
                        activity_type="idle_ended",
                        idle_duration_seconds=0,
                    )
                    await self._emit_event(event)

        except Exception as e:
            logger.debug(f"Error checking idle state: {e}")

    # =========================================================================
    # Event Emission
    # =========================================================================

    async def _emit_event(self, event: Any) -> None:
        """Emit an event, optionally batching."""
        if not self._event_bus:
            return

        if self.config.enable_event_batching:
            async with self._batch_lock:
                self._pending_events.append(event)
        else:
            await self._event_bus.emit(event)

    async def _event_batch_processor(self) -> None:
        """Process batched events."""
        while self._running:
            try:
                await asyncio.sleep(self.config.batch_window_ms / 1000)

                events = []
                async with self._batch_lock:
                    if self._pending_events:
                        events = self._pending_events.copy()
                        self._pending_events.clear()

                # Emit events outside lock
                for event in events:
                    await self._event_bus.emit(event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Event batch processor error: {e}")

    # =========================================================================
    # Status and Stats
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get current monitor status."""
        return {
            "running": self._running,
            "startup_state": self._startup_state,
            "startup_error": self._startup_error,
            "startup_phase": self._startup_phase,
            "startup_progress": round(self._startup_progress, 4),
            "startup_started_at": (
                self._startup_started_at.isoformat()
                if self._startup_started_at else None
            ),
            "startup_completed_at": (
                self._startup_completed_at.isoformat()
                if self._startup_completed_at else None
            ),
            "startup_forecast_ready_at": (
                self._startup_forecast_ready_at.isoformat()
                if self._startup_forecast_ready_at else None
            ),
            "startup_eta_seconds": self._startup_eta_seconds(),
            "startup_phase_results": self._startup_phase_results.copy(),
            "yabai_available": self._yabai_available,
            "yabai_si_active": self._yabai_si is not None,
            "running_apps": len(self._running_apps),
            "frontmost_app": self._frontmost_app,
            "current_space": self._current_space,
            "is_screen_locked": self._is_screen_locked,
            "is_idle": self._is_idle,
            "last_user_activity": self._last_user_activity.isoformat(),
            "active_tasks": len(self._tasks),
        }

    def get_running_apps(self) -> Dict[str, Dict[str, Any]]:
        """Get currently running apps."""
        return self._running_apps.copy()

    def get_focused_window(self) -> Optional[Dict[str, Any]]:
        """Get currently focused window."""
        return self._focused_window.copy() if self._focused_window else None


# =============================================================================
# Singleton Pattern
# =============================================================================

_system_event_monitor: Optional[SystemEventMonitor] = None


async def get_system_event_monitor(
    config: Optional[MonitorConfig] = None,
    auto_start: bool = True,
) -> SystemEventMonitor:
    """
    Get the global system event monitor instance.

    Args:
        config: Monitor configuration
        auto_start: Automatically start monitoring

    Returns:
        The SystemEventMonitor singleton
    """
    global _system_event_monitor

    if _system_event_monitor is None:
        _system_event_monitor = SystemEventMonitor(config)

    if auto_start and not _system_event_monitor._running:
        await _system_event_monitor.start()
        if not _system_event_monitor._running:
            status = _system_event_monitor.get_status()
            raise RuntimeError(
                f"SystemEventMonitor failed to start: {status.get('startup_error') or 'unknown error'}"
            )

    return _system_event_monitor


async def stop_system_event_monitor() -> None:
    """Stop the global system event monitor."""
    global _system_event_monitor

    if _system_event_monitor is not None:
        await _system_event_monitor.stop()
        _system_event_monitor = None
