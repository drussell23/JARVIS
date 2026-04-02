"""
REM Health Scanner — System health monitoring for the Autonomous Engineering Hive.

Collects system metrics (RAM, CPU, disk) via psutil, assesses them against
configurable severity thresholds, and creates Hive threads for any findings.
Designed to run during REM cognitive cycles with a bounded API-call budget.

Severity thresholds:
    RAM/CPU:  < 70% = clear, 70-90% = warning, > 90% = error
    Disk:     < 85% = clear, 85-95% = warning, > 95% = error
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    ThreadState,
)

logger = logging.getLogger(__name__)

# Try to import psutil; gracefully degrade if unavailable.
try:
    import psutil  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

# ============================================================================
# THRESHOLDS
# ============================================================================

# Each entry: (metric_name, warning_floor, error_floor)
_RAM_CPU_THRESHOLDS: List[Tuple[str, float, float]] = [
    ("ram_percent", 70.0, 90.0),
    ("cpu_percent", 70.0, 90.0),
]

_DISK_THRESHOLDS: List[Tuple[str, float, float]] = [
    ("disk_percent", 85.0, 95.0),
]

_ALL_THRESHOLDS = _RAM_CPU_THRESHOLDS + _DISK_THRESHOLDS


# ============================================================================
# HEALTH SCANNER
# ============================================================================


class HealthScanner:
    """Collect system metrics and create Hive threads for health findings.

    Parameters
    ----------
    persona_engine:
        PersonaEngine instance (typed as ``Any`` to avoid circular imports).
    thread_manager:
        ThreadManager instance for creating and managing threads.
    relay:
        HudRelayAgent instance (typed as ``Any`` to avoid circular imports).
    """

    def __init__(
        self,
        persona_engine: Any,
        thread_manager: Any,
        relay: Any,
    ) -> None:
        self._persona_engine = persona_engine
        self._thread_manager = thread_manager
        self._relay = relay

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self, budget: int
    ) -> Tuple[List[str], int, bool, Optional[str]]:
        """Execute a full health-scan cycle.

        Parameters
        ----------
        budget:
            Maximum number of LLM inference calls allowed.

        Returns
        -------
        tuple
            ``(thread_ids, calls_used, should_escalate, escalation_id)``
        """
        metrics = self._collect_metrics()
        findings = self._assess(metrics)

        thread_ids: List[str] = []
        calls_used: int = 0
        should_escalate: bool = False
        escalation_id: Optional[str] = None

        if not findings:
            # All clear — create a single summary thread.
            thread = self._thread_manager.create_thread(
                title="System Health: All Clear",
                trigger_event="rem_health_scan",
                cognitive_state=CognitiveState.REM,
            )
            log_msg = AgentLogMessage(
                thread_id=thread.thread_id,
                agent_name="health_scanner",
                trinity_parent="jarvis",
                severity="info",
                category="health",
                payload=metrics,
            )
            self._thread_manager.add_message(thread.thread_id, log_msg)
            thread_ids.append(thread.thread_id)
            return thread_ids, 0, False, None

        # Process each finding (up to budget).
        for finding in findings:
            if calls_used >= budget:
                break

            metric_name = finding["metric"]
            value = finding["value"]
            severity = finding["severity"]

            thread = self._thread_manager.create_thread(
                title=f"Health: {metric_name} at {value}%",
                trigger_event="rem_health_scan",
                cognitive_state=CognitiveState.REM,
            )
            tid = thread.thread_id

            log_msg = AgentLogMessage(
                thread_id=tid,
                agent_name="health_scanner",
                trinity_parent="jarvis",
                severity=severity,
                category="health",
                payload=finding,
            )
            self._thread_manager.add_message(tid, log_msg)

            # Transition to DEBATING so persona can reason.
            self._thread_manager.transition(tid, ThreadState.DEBATING)

            # Generate persona reasoning (consumes 1 LLM call).
            reasoning_msg = await self._persona_engine.generate_reasoning(
                "jarvis", PersonaIntent.OBSERVE, thread
            )
            self._thread_manager.add_message(tid, reasoning_msg)
            calls_used += 1

            thread_ids.append(tid)

            if severity == "error":
                should_escalate = True
                escalation_id = tid

        return thread_ids, calls_used, should_escalate, escalation_id

    # ------------------------------------------------------------------
    # Metric collection
    # ------------------------------------------------------------------

    def _collect_metrics(self) -> Dict[str, float]:
        """Collect system metrics via psutil.

        Returns a dictionary with ``ram_percent``, ``cpu_percent``, and
        ``disk_percent``.  Returns zeros if psutil is unavailable.
        """
        if psutil is None:
            return {"ram_percent": 0.0, "cpu_percent": 0.0, "disk_percent": 0.0}

        try:
            ram_percent = psutil.virtual_memory().percent
        except Exception:
            ram_percent = 0.0

        try:
            cpu_percent = psutil.cpu_percent(interval=None)
        except Exception:
            cpu_percent = 0.0

        try:
            disk_percent = psutil.disk_usage("/").percent
        except Exception:
            disk_percent = 0.0

        return {
            "ram_percent": float(ram_percent),
            "cpu_percent": float(cpu_percent),
            "disk_percent": float(disk_percent),
        }

    # ------------------------------------------------------------------
    # Assessment
    # ------------------------------------------------------------------

    def _assess(self, metrics: Dict[str, float]) -> List[Dict[str, Any]]:
        """Assess metrics against thresholds.

        Returns a list of finding dicts, each with keys:
        ``metric``, ``value``, ``severity``, ``threshold_warning``,
        ``threshold_error``.
        """
        findings: List[Dict[str, Any]] = []

        for metric_name, warn_floor, error_floor in _ALL_THRESHOLDS:
            value = metrics.get(metric_name, 0.0)

            if value > error_floor:
                severity = "error"
            elif value >= warn_floor:
                severity = "warning"
            else:
                continue  # Below warning threshold — no finding.

            findings.append(
                {
                    "metric": metric_name,
                    "value": value,
                    "severity": severity,
                    "threshold_warning": warn_floor,
                    "threshold_error": error_floor,
                }
            )

        return findings
