"""
Intelligent Error Reporter - Root Cause Diagnosis for JARVIS Surveillance
==========================================================================

Fixes the "I hit a snag" problem by:
1. Diagnosing actual error types (not masking them)
2. Probing component health (Yabai, BetterDisplay, Vision, OCR)
3. Providing actionable error messages with technical details
4. Maintaining user-friendly tone while exposing root causes
5. Suggesting specific remediation steps

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  IntelligentErrorReporter                                           │
    │  ├── ErrorClassifier (categorize exception types)                   │
    │  ├── ComponentHealthProbe (async health checks)                     │
    │  ├── DiagnosticAnalyzer (root cause detection)                      │
    │  └── ActionableMessageBuilder (user-friendly + technical)           │
    └─────────────────────────────────────────────────────────────────────┘

Author: JARVIS v11.1 - Intelligent Error Reporting
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Error Classification
# =============================================================================

class ErrorCategory(enum.Enum):
    """High-level error categories for surveillance failures."""
    YABAI_CONNECTION = "yabai_connection"
    YABAI_TIMEOUT = "yabai_timeout"
    YABAI_PERMISSION = "yabai_permission"
    GHOST_DISPLAY_MISSING = "ghost_display_missing"
    GHOST_DISPLAY_ERROR = "ghost_display_error"
    SCREEN_RECORDING_DENIED = "screen_recording_denied"
    WINDOW_NOT_FOUND = "window_not_found"
    WINDOW_TELEPORTATION_FAILED = "window_teleportation_failed"
    OCR_INITIALIZATION = "ocr_initialization"
    OCR_PROCESSING = "ocr_processing"
    VIDEO_CAPTURE_FAILED = "video_capture_failed"
    WATCHER_TIMEOUT = "watcher_timeout"
    WATCHER_CRASH = "watcher_crash"
    AGENT_INITIALIZATION = "agent_initialization"
    NETWORK_ERROR = "network_error"
    MEMORY_ERROR = "memory_error"
    ASYNC_TIMEOUT = "async_timeout"
    CIRCULAR_SPACE_LOOP = "circular_space_loop"
    UNKNOWN = "unknown"


class ComponentStatus(enum.Enum):
    """Health status for individual components."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class ComponentHealth:
    """Health information for a surveillance component."""
    name: str
    status: ComponentStatus
    response_time_ms: float = 0.0
    error_message: Optional[str] = None
    remediation_hint: Optional[str] = None
    last_checked: datetime = field(default_factory=datetime.now)

    @property
    def is_healthy(self) -> bool:
        return self.status == ComponentStatus.HEALTHY


@dataclass
class ErrorDiagnosis:
    """Comprehensive error diagnosis result."""
    category: ErrorCategory
    original_exception: Optional[Exception]
    exception_type: str
    exception_message: str
    component_health: Dict[str, ComponentHealth]
    root_cause: str
    user_friendly_message: str
    technical_details: str
    remediation_steps: List[str]
    severity: str  # "low", "medium", "high", "critical"
    timestamp: datetime = field(default_factory=datetime.now)

    def to_voice_message(self, include_technical: bool = True) -> str:
        """Format diagnosis for voice output."""
        msg = self.user_friendly_message

        if include_technical and self.technical_details:
            msg += f" Technical details: {self.technical_details}"

        if self.remediation_steps:
            first_step = self.remediation_steps[0]
            msg += f" To fix this: {first_step}"

        return msg

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/API responses."""
        return {
            "category": self.category.value,
            "exception_type": self.exception_type,
            "exception_message": self.exception_message,
            "root_cause": self.root_cause,
            "user_message": self.user_friendly_message,
            "technical_details": self.technical_details,
            "remediation_steps": self.remediation_steps,
            "severity": self.severity,
            "component_health": {
                name: {
                    "status": health.status.value,
                    "response_time_ms": health.response_time_ms,
                    "error": health.error_message,
                }
                for name, health in self.component_health.items()
            },
            "timestamp": self.timestamp.isoformat(),
        }


# =============================================================================
# Intelligent Error Reporter
# =============================================================================

class IntelligentErrorReporter:
    """
    Intelligent error diagnosis and reporting system.

    Key Features:
    1. Classifies exceptions into actionable categories
    2. Probes component health in parallel
    3. Builds user-friendly messages with technical context
    4. Suggests specific remediation steps
    5. Maintains conversation-appropriate tone
    """

    def __init__(
        self,
        user_name: str = "Sir",
        include_technical_details: bool = True,
        health_probe_timeout: float = 3.0,
    ):
        self.user_name = user_name
        self.include_technical = include_technical_details
        self.health_probe_timeout = health_probe_timeout

        # Error pattern matchers
        self._error_patterns = self._build_error_patterns()

        # Component health cache
        self._health_cache: Dict[str, ComponentHealth] = {}
        self._cache_ttl_seconds = 30.0
        self._last_health_check: Optional[datetime] = None

    # =========================================================================
    # Main Entry Point
    # =========================================================================

    async def diagnose_error(
        self,
        exception: Exception,
        context: Optional[Dict[str, Any]] = None,
        probe_components: bool = True,
    ) -> ErrorDiagnosis:
        """
        Diagnose an error and provide actionable information.

        Args:
            exception: The exception that occurred
            context: Optional context (app_name, trigger_text, etc.)
            probe_components: Whether to probe component health

        Returns:
            ErrorDiagnosis with full analysis
        """
        context = context or {}
        start_time = time.perf_counter()

        # Step 1: Classify the exception
        category = self._classify_exception(exception)

        # Step 2: Probe component health (parallel)
        component_health = {}
        if probe_components:
            component_health = await self._probe_all_components()

        # Step 3: Determine root cause
        root_cause = self._determine_root_cause(
            exception, category, component_health, context
        )

        # Step 4: Build user-friendly message
        user_message = self._build_user_message(
            exception, category, root_cause, context
        )

        # Step 5: Build technical details
        technical_details = self._build_technical_details(
            exception, category, component_health
        )

        # Step 6: Build remediation steps
        remediation = self._build_remediation_steps(
            category, component_health, context
        )

        # Step 7: Determine severity
        severity = self._determine_severity(category, component_health)

        elapsed = (time.perf_counter() - start_time) * 1000
        logger.debug(f"[ErrorReporter] Diagnosis completed in {elapsed:.0f}ms")

        return ErrorDiagnosis(
            category=category,
            original_exception=exception,
            exception_type=type(exception).__name__,
            exception_message=str(exception),
            component_health=component_health,
            root_cause=root_cause,
            user_friendly_message=user_message,
            technical_details=technical_details,
            remediation_steps=remediation,
            severity=severity,
        )

    # =========================================================================
    # Exception Classification
    # =========================================================================

    def _build_error_patterns(self) -> Dict[ErrorCategory, List[str]]:
        """Build regex patterns for error classification."""
        return {
            ErrorCategory.YABAI_CONNECTION: [
                r"yabai.*connection.*refused",
                r"socket\.error.*yabai",
                r"couldn't connect to yabai",
                r"yabai.*socket.*error",
                r"yabai.*not.*running",
            ],
            ErrorCategory.YABAI_TIMEOUT: [
                r"yabai.*timed?\s*out",
                r"yabai.*timeout",
                r"socket\.timeout.*yabai",
                r"yabai.*query.*hung",
            ],
            ErrorCategory.YABAI_PERMISSION: [
                r"yabai.*permission",
                r"accessibility.*denied",
                r"scripting.*addition.*denied",
            ],
            ErrorCategory.GHOST_DISPLAY_MISSING: [
                r"ghost.*display.*not.*found",
                r"no.*virtual.*display",
                r"betterdisplay.*not.*running",
                r"shadow.*monitor.*unavailable",
            ],
            ErrorCategory.GHOST_DISPLAY_ERROR: [
                r"ghost.*display.*error",
                r"failed.*teleport.*ghost",
                r"ghost.*space.*invalid",
            ],
            ErrorCategory.SCREEN_RECORDING_DENIED: [
                r"screen.*recording.*permission",
                r"screencapturekit.*permission",
                r"CGDisplayStream.*permission",
                r"capture.*denied",
            ],
            ErrorCategory.WINDOW_NOT_FOUND: [
                r"no.*windows?.*found",
                r"window.*not.*found",
                r"couldn't.*find.*window",
                r"no.*\w+.*windows?.*open",
            ],
            ErrorCategory.WINDOW_TELEPORTATION_FAILED: [
                r"teleport.*failed",
                r"move.*window.*failed",
                r"rescue.*failed",
                r"couldn't.*move.*window",
            ],
            ErrorCategory.OCR_INITIALIZATION: [
                r"ocr.*init.*failed",
                r"rapidocr.*error",
                r"paddleocr.*error",
                r"tesseract.*not.*found",
            ],
            ErrorCategory.OCR_PROCESSING: [
                r"ocr.*processing.*error",
                r"text.*extraction.*failed",
                r"image.*ocr.*failed",
            ],
            ErrorCategory.VIDEO_CAPTURE_FAILED: [
                r"video.*capture.*failed",
                r"frame.*capture.*error",
                r"screen.*grab.*failed",
                r"CGDisplayStream.*error",
            ],
            ErrorCategory.WATCHER_TIMEOUT: [
                r"watcher.*timeout",
                r"monitoring.*timed?\s*out",
                r"watch.*start.*timeout",
            ],
            ErrorCategory.WATCHER_CRASH: [
                r"watcher.*crashed",
                r"watcher.*died",
                r"monitoring.*failed.*unexpectedly",
                r"consecutive.*frame.*failures",
            ],
            ErrorCategory.AGENT_INITIALIZATION: [
                r"visualmonitoragent.*init.*failed",
                r"agent.*initialization.*error",
                r"failed.*create.*agent",
            ],
            ErrorCategory.ASYNC_TIMEOUT: [
                r"asyncio\.timeout",
                r"TimeoutError",
                r"timed?\s*out",
            ],
            ErrorCategory.CIRCULAR_SPACE_LOOP: [
                r"circular.*space",
                r"infinite.*loop.*detected",
                r"space.*already.*visible",
            ],
        }

    def _classify_exception(self, exception: Exception) -> ErrorCategory:
        """Classify an exception into a category."""
        exc_str = str(exception).lower()
        exc_type = type(exception).__name__.lower()

        # Check type first
        if "timeout" in exc_type:
            return ErrorCategory.ASYNC_TIMEOUT

        if "permission" in exc_type:
            return ErrorCategory.YABAI_PERMISSION

        if "connection" in exc_type:
            return ErrorCategory.YABAI_CONNECTION

        # Check message against patterns
        combined = f"{exc_type} {exc_str}"

        for category, patterns in self._error_patterns.items():
            for pattern in patterns:
                if re.search(pattern, combined, re.IGNORECASE):
                    return category

        return ErrorCategory.UNKNOWN

    # =========================================================================
    # Component Health Probing
    # =========================================================================

    async def _probe_all_components(self) -> Dict[str, ComponentHealth]:
        """Probe all surveillance components in parallel."""
        # Check cache
        if self._last_health_check:
            elapsed = (datetime.now() - self._last_health_check).total_seconds()
            if elapsed < self._cache_ttl_seconds:
                return self._health_cache

        # Probe in parallel
        probes = [
            self._probe_yabai(),
            self._probe_ghost_display(),
            self._probe_screen_recording(),
            self._probe_video_capture(),
        ]

        results = await asyncio.gather(*probes, return_exceptions=True)

        health = {}
        probe_names = ["yabai", "ghost_display", "screen_recording", "video_capture"]

        for name, result in zip(probe_names, results):
            if isinstance(result, Exception):
                health[name] = ComponentHealth(
                    name=name,
                    status=ComponentStatus.ERROR,
                    error_message=str(result),
                )
            else:
                health[name] = result

        # Update cache
        self._health_cache = health
        self._last_health_check = datetime.now()

        return health

    async def _probe_yabai(self) -> ComponentHealth:
        """Probe Yabai window manager health."""
        start = time.perf_counter()

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "yabai", "-m", "query", "--spaces",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=self.health_probe_timeout,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.health_probe_timeout,
            )

            elapsed = (time.perf_counter() - start) * 1000

            if proc.returncode == 0:
                return ComponentHealth(
                    name="yabai",
                    status=ComponentStatus.HEALTHY,
                    response_time_ms=elapsed,
                )
            else:
                error = stderr.decode().strip() if stderr else "Unknown error"
                return ComponentHealth(
                    name="yabai",
                    status=ComponentStatus.ERROR,
                    response_time_ms=elapsed,
                    error_message=error,
                    remediation_hint="Run: brew services restart yabai",
                )

        except asyncio.TimeoutError:
            return ComponentHealth(
                name="yabai",
                status=ComponentStatus.TIMEOUT,
                response_time_ms=self.health_probe_timeout * 1000,
                error_message=f"Yabai query timed out after {self.health_probe_timeout}s",
                remediation_hint="Yabai may be overwhelmed. Run: brew services restart yabai",
            )

        except FileNotFoundError:
            return ComponentHealth(
                name="yabai",
                status=ComponentStatus.UNAVAILABLE,
                error_message="Yabai not installed or not in PATH",
                remediation_hint="Install yabai: brew install koekeishiya/formulae/yabai",
            )

        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            return ComponentHealth(
                name="yabai",
                status=ComponentStatus.ERROR,
                response_time_ms=elapsed,
                error_message=str(e),
            )

    async def _probe_ghost_display(self) -> ComponentHealth:
        """Probe Ghost Display (BetterDisplay) health."""
        start = time.perf_counter()

        try:
            # Check if BetterDisplay virtual display exists
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "system_profiler", "SPDisplaysDataType",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=self.health_probe_timeout,
            )

            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.health_probe_timeout,
            )

            elapsed = (time.perf_counter() - start) * 1000
            output = stdout.decode()

            # Look for virtual display indicators
            has_virtual = any(
                indicator in output.lower()
                for indicator in ["virtual", "ghost", "betterdisplay", "dummy"]
            )

            if has_virtual:
                return ComponentHealth(
                    name="ghost_display",
                    status=ComponentStatus.HEALTHY,
                    response_time_ms=elapsed,
                )
            else:
                return ComponentHealth(
                    name="ghost_display",
                    status=ComponentStatus.UNAVAILABLE,
                    response_time_ms=elapsed,
                    error_message="No virtual display detected",
                    remediation_hint="Create a virtual display in BetterDisplay app",
                )

        except asyncio.TimeoutError:
            return ComponentHealth(
                name="ghost_display",
                status=ComponentStatus.TIMEOUT,
                response_time_ms=self.health_probe_timeout * 1000,
                error_message="Display query timed out",
            )

        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            return ComponentHealth(
                name="ghost_display",
                status=ComponentStatus.ERROR,
                response_time_ms=elapsed,
                error_message=str(e),
            )

    async def _probe_screen_recording(self) -> ComponentHealth:
        """Probe screen recording permission status."""
        start = time.perf_counter()

        try:
            # Check TCC database for screen recording permission
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "sqlite3",
                    os.path.expanduser(
                        "~/Library/Application Support/com.apple.TCC/TCC.db"
                    ),
                    "SELECT client FROM access WHERE service='kTCCServiceScreenCapture' AND auth_value=2;",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=self.health_probe_timeout,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.health_probe_timeout,
            )

            elapsed = (time.perf_counter() - start) * 1000

            if proc.returncode == 0 and stdout:
                # Permission granted to some apps
                return ComponentHealth(
                    name="screen_recording",
                    status=ComponentStatus.HEALTHY,
                    response_time_ms=elapsed,
                )
            else:
                return ComponentHealth(
                    name="screen_recording",
                    status=ComponentStatus.PERMISSION_DENIED,
                    response_time_ms=elapsed,
                    error_message="Screen recording permission may not be granted",
                    remediation_hint=(
                        "Go to System Settings > Privacy & Security > Screen Recording "
                        "and enable access for Terminal/Python"
                    ),
                )

        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            # Can't check TCC directly - assume healthy if no error
            return ComponentHealth(
                name="screen_recording",
                status=ComponentStatus.UNKNOWN,
                response_time_ms=elapsed,
                error_message=f"Could not verify permission: {e}",
            )

    async def _probe_video_capture(self) -> ComponentHealth:
        """Probe video capture capability."""
        start = time.perf_counter()

        try:
            # Quick import check for ScreenCaptureKit availability
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "python3", "-c",
                    "import Quartz; print('ok')",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=self.health_probe_timeout,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.health_probe_timeout,
            )

            elapsed = (time.perf_counter() - start) * 1000

            if proc.returncode == 0:
                return ComponentHealth(
                    name="video_capture",
                    status=ComponentStatus.HEALTHY,
                    response_time_ms=elapsed,
                )
            else:
                return ComponentHealth(
                    name="video_capture",
                    status=ComponentStatus.ERROR,
                    response_time_ms=elapsed,
                    error_message="Quartz framework not available",
                    remediation_hint="Reinstall pyobjc: pip install pyobjc-framework-Quartz",
                )

        except asyncio.TimeoutError:
            return ComponentHealth(
                name="video_capture",
                status=ComponentStatus.TIMEOUT,
                response_time_ms=self.health_probe_timeout * 1000,
                error_message="Video capture probe timed out",
            )

        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            return ComponentHealth(
                name="video_capture",
                status=ComponentStatus.ERROR,
                response_time_ms=elapsed,
                error_message=str(e),
            )

    # =========================================================================
    # Root Cause Analysis
    # =========================================================================

    def _determine_root_cause(
        self,
        exception: Exception,
        category: ErrorCategory,
        health: Dict[str, ComponentHealth],
        context: Dict[str, Any],
    ) -> str:
        """Determine the root cause of the error."""

        # Check component health first
        for name, component in health.items():
            if component.status in (
                ComponentStatus.UNAVAILABLE,
                ComponentStatus.ERROR,
                ComponentStatus.TIMEOUT,
                ComponentStatus.PERMISSION_DENIED,
            ):
                if name == "yabai":
                    return f"Yabai window manager is {component.status.value}: {component.error_message}"
                elif name == "ghost_display":
                    return f"Ghost Display is {component.status.value}: {component.error_message}"
                elif name == "screen_recording":
                    return f"Screen recording {component.status.value}: {component.error_message}"
                elif name == "video_capture":
                    return f"Video capture {component.status.value}: {component.error_message}"

        # Category-based root cause
        root_causes = {
            ErrorCategory.YABAI_CONNECTION: "Yabai is not running or its socket is not accessible",
            ErrorCategory.YABAI_TIMEOUT: "Yabai is overwhelmed by rapid-fire requests",
            ErrorCategory.YABAI_PERMISSION: "Yabai lacks accessibility permissions",
            ErrorCategory.GHOST_DISPLAY_MISSING: "No virtual display exists for background monitoring",
            ErrorCategory.GHOST_DISPLAY_ERROR: "Ghost Display operation failed",
            ErrorCategory.SCREEN_RECORDING_DENIED: "Screen recording permission not granted",
            ErrorCategory.WINDOW_NOT_FOUND: f"No {context.get('app_name', 'target')} windows are open",
            ErrorCategory.WINDOW_TELEPORTATION_FAILED: "Failed to move window to monitoring space",
            ErrorCategory.OCR_INITIALIZATION: "OCR engine failed to initialize",
            ErrorCategory.OCR_PROCESSING: "OCR text extraction failed on captured frame",
            ErrorCategory.VIDEO_CAPTURE_FAILED: "Screen capture stream failed",
            ErrorCategory.WATCHER_TIMEOUT: "Monitoring startup exceeded timeout",
            ErrorCategory.WATCHER_CRASH: "Video monitoring stream crashed unexpectedly",
            ErrorCategory.AGENT_INITIALIZATION: "VisualMonitorAgent failed to initialize",
            ErrorCategory.ASYNC_TIMEOUT: "Operation exceeded time limit",
            ErrorCategory.CIRCULAR_SPACE_LOOP: "Detected infinite loop in space switching",
        }

        return root_causes.get(
            category,
            f"Unknown error: {type(exception).__name__}: {str(exception)[:100]}"
        )

    # =========================================================================
    # Message Building
    # =========================================================================

    def _build_user_message(
        self,
        exception: Exception,
        category: ErrorCategory,
        root_cause: str,
        context: Dict[str, Any],
    ) -> str:
        """Build user-friendly error message."""
        app_name = context.get("app_name", "the app")

        messages = {
            ErrorCategory.YABAI_CONNECTION: (
                f"I can't communicate with the window manager right now, {self.user_name}. "
                f"Yabai appears to be stopped or unresponsive."
            ),
            ErrorCategory.YABAI_TIMEOUT: (
                f"The window manager is taking too long to respond, {self.user_name}. "
                f"It might be overloaded from too many requests."
            ),
            ErrorCategory.YABAI_PERMISSION: (
                f"The window manager doesn't have the permissions it needs, {self.user_name}. "
                f"Accessibility access may need to be re-granted."
            ),
            ErrorCategory.GHOST_DISPLAY_MISSING: (
                f"I need a virtual display to monitor windows in the background, {self.user_name}, "
                f"but one isn't set up. BetterDisplay can create this."
            ),
            ErrorCategory.GHOST_DISPLAY_ERROR: (
                f"I encountered an issue with the virtual display, {self.user_name}. "
                f"The Ghost Display operation failed."
            ),
            ErrorCategory.SCREEN_RECORDING_DENIED: (
                f"I don't have permission to capture the screen, {self.user_name}. "
                f"Screen recording access needs to be enabled in System Settings."
            ),
            ErrorCategory.WINDOW_NOT_FOUND: (
                f"I don't see any {app_name} windows open right now, {self.user_name}. "
                f"Could you open {app_name} first?"
            ),
            ErrorCategory.WINDOW_TELEPORTATION_FAILED: (
                f"I couldn't move the {app_name} window to my monitoring space, {self.user_name}. "
                f"The Search & Rescue protocol encountered an obstacle."
            ),
            ErrorCategory.OCR_INITIALIZATION: (
                f"My text recognition system failed to start, {self.user_name}. "
                f"The OCR engine may need to be reinstalled."
            ),
            ErrorCategory.OCR_PROCESSING: (
                f"I had trouble reading text from {app_name}, {self.user_name}. "
                f"The screen capture was unclear or the OCR failed."
            ),
            ErrorCategory.VIDEO_CAPTURE_FAILED: (
                f"I couldn't capture video from {app_name}, {self.user_name}. "
                f"The screen recording stream failed to start."
            ),
            ErrorCategory.WATCHER_TIMEOUT: (
                f"Starting the monitor for {app_name} took too long, {self.user_name}. "
                f"The system may be under heavy load."
            ),
            ErrorCategory.WATCHER_CRASH: (
                f"My monitoring of {app_name} crashed unexpectedly, {self.user_name}. "
                f"The video stream stopped responding."
            ),
            ErrorCategory.AGENT_INITIALIZATION: (
                f"My visual surveillance system failed to initialize, {self.user_name}. "
                f"There may be a deeper issue with the monitoring infrastructure."
            ),
            ErrorCategory.ASYNC_TIMEOUT: (
                f"The operation timed out, {self.user_name}. "
                f"Something is running slower than expected."
            ),
            ErrorCategory.CIRCULAR_SPACE_LOOP: (
                f"I detected a potential infinite loop while switching spaces, {self.user_name}. "
                f"This can happen with unusual multi-monitor setups."
            ),
        }

        return messages.get(
            category,
            f"I encountered an error while monitoring {app_name}, {self.user_name}. "
            f"Error: {str(exception)[:100]}"
        )

    def _build_technical_details(
        self,
        exception: Exception,
        category: ErrorCategory,
        health: Dict[str, ComponentHealth],
    ) -> str:
        """Build technical details for debugging."""
        parts = []

        # Exception info
        parts.append(f"[{type(exception).__name__}] {str(exception)[:150]}")

        # Category
        parts.append(f"Category: {category.value}")

        # Unhealthy components
        unhealthy = [
            f"{name}={h.status.value}"
            for name, h in health.items()
            if not h.is_healthy
        ]
        if unhealthy:
            parts.append(f"Unhealthy: {', '.join(unhealthy)}")

        return " | ".join(parts)

    def _build_remediation_steps(
        self,
        category: ErrorCategory,
        health: Dict[str, ComponentHealth],
        context: Dict[str, Any],
    ) -> List[str]:
        """Build actionable remediation steps."""
        steps = []

        # Component-specific remediations from health probes
        for name, component in health.items():
            if component.remediation_hint and not component.is_healthy:
                steps.append(component.remediation_hint)

        # Category-specific remediations
        category_steps = {
            ErrorCategory.YABAI_CONNECTION: [
                "Run: brew services restart yabai",
                "Check if yabai is installed: which yabai",
            ],
            ErrorCategory.YABAI_TIMEOUT: [
                "Run: brew services restart yabai",
                "Reduce parallel window operations",
            ],
            ErrorCategory.YABAI_PERMISSION: [
                "Re-grant accessibility permission in System Settings > Privacy & Security > Accessibility",
                "Run: sudo yabai --load-sa (for scripting additions)",
            ],
            ErrorCategory.GHOST_DISPLAY_MISSING: [
                "Open BetterDisplay and create a virtual display",
                "Set the virtual display resolution to 1920x1080 or higher",
            ],
            ErrorCategory.SCREEN_RECORDING_DENIED: [
                "Go to System Settings > Privacy & Security > Screen Recording",
                "Enable access for Terminal, Python, or the JARVIS application",
            ],
            ErrorCategory.WINDOW_NOT_FOUND: [
                f"Open {context.get('app_name', 'the target application')} first",
                "Verify the app name is spelled correctly",
            ],
            ErrorCategory.OCR_INITIALIZATION: [
                "Run: pip install rapidocr-onnxruntime",
                "Check OCR dependencies are installed",
            ],
            ErrorCategory.VIDEO_CAPTURE_FAILED: [
                "Check screen recording permissions",
                "Run: pip install pyobjc-framework-ScreenCaptureKit",
            ],
        }

        if category in category_steps:
            for step in category_steps[category]:
                if step not in steps:
                    steps.append(step)

        return steps[:5]  # Limit to 5 steps

    def _determine_severity(
        self,
        category: ErrorCategory,
        health: Dict[str, ComponentHealth],
    ) -> str:
        """Determine error severity level."""
        critical_categories = {
            ErrorCategory.SCREEN_RECORDING_DENIED,
            ErrorCategory.YABAI_CONNECTION,
            ErrorCategory.AGENT_INITIALIZATION,
        }

        high_categories = {
            ErrorCategory.YABAI_TIMEOUT,
            ErrorCategory.GHOST_DISPLAY_MISSING,
            ErrorCategory.VIDEO_CAPTURE_FAILED,
            ErrorCategory.OCR_INITIALIZATION,
        }

        medium_categories = {
            ErrorCategory.WINDOW_TELEPORTATION_FAILED,
            ErrorCategory.WATCHER_CRASH,
            ErrorCategory.WATCHER_TIMEOUT,
        }

        if category in critical_categories:
            return "critical"
        elif category in high_categories:
            return "high"
        elif category in medium_categories:
            return "medium"
        else:
            return "low"


# =============================================================================
# Module-level convenience functions
# =============================================================================

_reporter_instance: Optional[IntelligentErrorReporter] = None


def get_error_reporter(
    user_name: str = "Sir",
    **kwargs
) -> IntelligentErrorReporter:
    """Get or create the global error reporter instance."""
    global _reporter_instance

    if _reporter_instance is None:
        _reporter_instance = IntelligentErrorReporter(user_name=user_name, **kwargs)

    return _reporter_instance


async def diagnose_surveillance_error(
    exception: Exception,
    app_name: Optional[str] = None,
    trigger_text: Optional[str] = None,
    user_name: str = "Sir",
) -> ErrorDiagnosis:
    """Convenience function to diagnose a surveillance error."""
    reporter = get_error_reporter(user_name=user_name)

    context = {}
    if app_name:
        context["app_name"] = app_name
    if trigger_text:
        context["trigger_text"] = trigger_text

    return await reporter.diagnose_error(exception, context)
