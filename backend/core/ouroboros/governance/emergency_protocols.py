"""
Emergency Protocol Engine — JARVIS-Level Tier 2.

"House Party Protocol: activate all suits."

5-level escalation system with alert accumulation, severity decay,
named protocols, cooldown recovery, and voice integration.

Levels:
  GREEN  — Normal operations
  YELLOW — Elevated monitoring (2-4 alerts/hour)
  ORANGE — Halt autonomous modifications (5+ alerts OR 3x CI fail)
  RED    — Rollback to checkpoint, lock writes, notify human
  HOUSE PARTY — Catastrophic: halt everything, preserve state, page human

Named Protocols:
  HOUSE PARTY — Catastrophic response
  CLEAN SLATE — Rollback ALL repos to last tagged release
  IRON LEGION — Spawn max parallel workers for large-scale fix
  VERONICA   — Route to Doubleword 397B for deep analysis

Boundary Principle:
  Deterministic: Alert accumulation, severity scoring, decay computation,
  level thresholds. No model inference in the escalation logic itself.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_GREEN_THRESHOLD = float(os.environ.get("JARVIS_EMERGENCY_GREEN", "3"))
_YELLOW_THRESHOLD = float(os.environ.get("JARVIS_EMERGENCY_YELLOW", "8"))
_ORANGE_THRESHOLD = float(os.environ.get("JARVIS_EMERGENCY_ORANGE", "15"))
_RED_THRESHOLD = float(os.environ.get("JARVIS_EMERGENCY_RED", "25"))
_ENABLED = os.environ.get(
    "JARVIS_EMERGENCY_ENABLED", "true"
).lower() in ("true", "1", "yes")


class AlertLevel(IntEnum):
    GREEN = 0
    YELLOW = 1
    ORANGE = 2
    RED = 3
    HOUSE_PARTY = 4


class AlertType(str, Enum):
    TEST_FAILURE = "test_failure"
    SECURITY_CVE = "security_cve"
    CI_FAILURE = "ci_failure"
    GITHUB_ISSUE_CRITICAL = "github_issue_critical"
    PERFORMANCE_REGRESSION = "performance_regression"
    CROSS_REPO_DRIFT = "cross_repo_drift"
    ENTROPY_TRIGGER = "entropy_trigger"
    IMPORT_ERROR = "import_error"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    GENERATION_FAILURE = "generation_failure"


# Severity points and decay half-life per alert type
_ALERT_CONFIG: Dict[AlertType, tuple] = {
    AlertType.TEST_FAILURE:          (1.0, 1800),    # 1 pt, 30 min half-life
    AlertType.SECURITY_CVE:          (3.0, 14400),   # 3 pt, 4 hr half-life
    AlertType.CI_FAILURE:            (2.0, 3600),    # 2 pt, 1 hr half-life
    AlertType.GITHUB_ISSUE_CRITICAL: (3.0, 14400),   # 3 pt, 4 hr
    AlertType.PERFORMANCE_REGRESSION:(1.0, 3600),    # 1 pt, 1 hr
    AlertType.CROSS_REPO_DRIFT:      (2.0, 7200),    # 2 pt, 2 hr
    AlertType.ENTROPY_TRIGGER:       (2.0, 3600),    # 2 pt, 1 hr
    AlertType.IMPORT_ERROR:          (1.0, 1800),    # 1 pt, 30 min
    AlertType.INFRASTRUCTURE_FAILURE:(3.0, 7200),    # 3 pt, 2 hr
    AlertType.GENERATION_FAILURE:    (1.0, 1800),    # 1 pt, 30 min
}


@dataclass
class Alert:
    """One recorded alert event."""
    alert_type: AlertType
    severity_points: float
    half_life_s: float
    message: str
    timestamp: float = field(default_factory=time.time)
    source_op_id: str = ""

    def decayed_points(self, now: float) -> float:
        """Compute current severity after exponential decay."""
        elapsed = now - self.timestamp
        if elapsed <= 0:
            return self.severity_points
        return self.severity_points * math.exp(-0.693 * elapsed / self.half_life_s)


@dataclass
class EmergencyState:
    """Current emergency state."""
    level: AlertLevel
    total_severity: float
    active_alerts: int
    message: str
    protocol: str = ""         # Named protocol if activated
    activated_at: float = 0.0
    autonomous_ops_halted: bool = False


class EmergencyProtocolEngine:
    """5-level emergency escalation with alert accumulation and decay.

    Monitors all sensor alerts via the TelemetryBus. Accumulates severity
    points with exponential decay. Escalates through GREEN → YELLOW →
    ORANGE → RED → HOUSE PARTY based on total severity score.

    Voice integration: announces level changes via safe_say().
    Pipeline integration: can halt autonomous operations at ORANGE+.
    """

    def __init__(
        self,
        say_fn: Optional[Callable[..., Coroutine]] = None,
        checkpoint_fn: Optional[Callable[..., Coroutine]] = None,
    ) -> None:
        self._say_fn = say_fn
        self._checkpoint_fn = checkpoint_fn
        self._alerts: List[Alert] = []
        self._current_level = AlertLevel.GREEN
        self._level_changed_at = time.time()
        self._ops_halted = False
        self._human_confirmed_resume = False

    @property
    def current_level(self) -> AlertLevel:
        return self._current_level

    @property
    def is_ops_halted(self) -> bool:
        return self._ops_halted

    def record_alert(
        self, alert_type: AlertType, message: str = "", op_id: str = "",
    ) -> EmergencyState:
        """Record an alert and re-evaluate emergency level."""
        if not _ENABLED:
            return self._build_state()

        points, half_life = _ALERT_CONFIG.get(alert_type, (1.0, 3600))
        alert = Alert(
            alert_type=alert_type,
            severity_points=points,
            half_life_s=half_life,
            message=message,
            source_op_id=op_id,
        )
        self._alerts.append(alert)

        # Prune alerts older than 24 hours
        cutoff = time.time() - 86400
        self._alerts = [a for a in self._alerts if a.timestamp > cutoff]

        # Re-evaluate level
        new_level = self._compute_level()
        if new_level != self._current_level:
            self._on_level_change(self._current_level, new_level)
            self._current_level = new_level
            self._level_changed_at = time.time()

        return self._build_state()

    def can_proceed(self) -> bool:
        """Check if autonomous operations are allowed at current level."""
        if self._current_level >= AlertLevel.ORANGE and not self._human_confirmed_resume:
            return False
        return True

    def confirm_resume(self) -> None:
        """Human confirms it's safe to resume operations after emergency."""
        self._human_confirmed_resume = True
        self._ops_halted = False
        logger.info("[Emergency] Human confirmed resume — operations unlocked")

    def get_state(self) -> EmergencyState:
        """Get current emergency state."""
        return self._build_state()

    def get_status(self) -> Dict[str, Any]:
        """Status for observability / remote API."""
        now = time.time()
        return {
            "level": self._current_level.name,
            "level_value": self._current_level.value,
            "total_severity": round(self._compute_severity(now), 2),
            "active_alerts": len([a for a in self._alerts if a.decayed_points(now) > 0.1]),
            "ops_halted": self._ops_halted,
            "level_duration_s": round(now - self._level_changed_at),
        }

    # ------------------------------------------------------------------
    # Level computation (deterministic — exponential decay + thresholds)
    # ------------------------------------------------------------------

    def _compute_severity(self, now: Optional[float] = None) -> float:
        """Compute total severity score with decay. Deterministic."""
        now = now or time.time()
        return sum(a.decayed_points(now) for a in self._alerts)

    def _compute_level(self) -> AlertLevel:
        """Compute emergency level from total severity. Deterministic."""
        severity = self._compute_severity()

        if severity >= _RED_THRESHOLD:
            return AlertLevel.HOUSE_PARTY
        if severity >= _ORANGE_THRESHOLD:
            return AlertLevel.RED
        if severity >= _YELLOW_THRESHOLD:
            return AlertLevel.ORANGE
        if severity >= _GREEN_THRESHOLD:
            return AlertLevel.YELLOW
        return AlertLevel.GREEN

    def _build_state(self) -> EmergencyState:
        now = time.time()
        severity = self._compute_severity(now)
        active = len([a for a in self._alerts if a.decayed_points(now) > 0.1])

        protocol = ""
        if self._current_level == AlertLevel.HOUSE_PARTY:
            protocol = "HOUSE_PARTY"
        elif self._current_level == AlertLevel.RED:
            protocol = "VERONICA"  # Deep analysis mode

        return EmergencyState(
            level=self._current_level,
            total_severity=round(severity, 2),
            active_alerts=active,
            message=self._level_message(),
            protocol=protocol,
            activated_at=self._level_changed_at,
            autonomous_ops_halted=self._ops_halted,
        )

    def _level_message(self) -> str:
        messages = {
            AlertLevel.GREEN: "All systems nominal.",
            AlertLevel.YELLOW: "Elevated activity detected. Monitoring closely.",
            AlertLevel.ORANGE: "Multiple alerts. Autonomous operations paused.",
            AlertLevel.RED: "Critical. Rolling back to safe state. Notifying you.",
            AlertLevel.HOUSE_PARTY: "HOUSE PARTY PROTOCOL. All operations halted.",
        }
        return messages.get(self._current_level, "Unknown state.")

    # ------------------------------------------------------------------
    # Level change handlers
    # ------------------------------------------------------------------

    def _on_level_change(self, old: AlertLevel, new: AlertLevel) -> None:
        """Handle a level transition. Deterministic actions per level."""
        logger.warning(
            "[Emergency] Level change: %s -> %s (severity=%.1f)",
            old.name, new.name, self._compute_severity(),
        )

        if new >= AlertLevel.ORANGE:
            self._ops_halted = True
            self._human_confirmed_resume = False
            logger.warning("[Emergency] Autonomous operations HALTED")

        if new == AlertLevel.GREEN and old > AlertLevel.GREEN:
            self._ops_halted = False
            logger.info("[Emergency] Returned to GREEN — operations resumed")

        # Voice notification (fire-and-forget)
        if self._say_fn is not None:
            try:
                msg = self._level_message()
                asyncio.get_event_loop().create_task(
                    self._say_fn(msg),
                )
            except Exception:
                pass

        # Create checkpoint on escalation
        if new >= AlertLevel.YELLOW and self._checkpoint_fn is not None:
            try:
                asyncio.get_event_loop().create_task(
                    self._checkpoint_fn(),
                )
            except Exception:
                pass
