"""
Observer Context -- monitors, detects anomalies, watches the screen passively.

The Observer is JARVIS's passive awareness layer.  It continuously
monitors system health, detects anomalies, recognizes patterns, and
watches the screen for changes -- all without being explicitly asked.

The Observer does NOT take actions (that's the Executor's job).  It
REPORTS what it sees and lets the Architect decide how to respond.

Tool access:
    system.*         -- health checks, metrics, processes, disk
    intelligence.*   -- anomaly detection, pattern recognition, context
    screen.*         -- passive screen capture, motion detection
    memory.*         -- store observations, recall past patterns
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core_contexts.tools import system, intelligence, screen, memory

logger = logging.getLogger(__name__)


@dataclass
class Observation:
    """An observation made by the Observer.

    Attributes:
        type: Category ("health", "anomaly", "screen_change", "pattern").
        severity: "info", "warning", "critical".
        summary: One-line human-readable summary.
        details: Full observation details.
        requires_action: Whether this observation should trigger the Architect.
    """
    type: str
    severity: str
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)
    requires_action: bool = False


class Observer:
    """Passive monitoring and awareness context.

    The Observer watches the system and screen without being asked.
    It reports observations to the Architect, which decides whether
    to act on them.

    Key behaviors:
    - Watch system health (CPU, RAM, disk)
    - Detect anomalies in metrics
    - Watch screen for changes (build complete, error appeared)
    - Recognize patterns in user behavior
    - Store observations in memory for long-term learning

    Usage::

        observer = Observer()
        health = await system.check_system_health()
        if health.status != "healthy":
            # Report to Architect
            ...
        frame_a = await screen.capture_screen()
        # ... time passes ...
        frame_b = await screen.capture_screen()
        if screen.detect_motion(frame_a, frame_b):
            # Something changed on screen
            ...
    """

    TOOLS = {
        "system.check_system_health": system.check_system_health,
        "system.get_system_metrics": system.get_system_metrics,
        "system.get_top_processes": system.get_top_processes,
        "system.check_port_available": system.check_port_available,
        "system.get_disk_usage": system.get_disk_usage,
        "intelligence.detect_anomalies": intelligence.detect_anomalies,
        "intelligence.detect_patterns": intelligence.detect_patterns,
        "intelligence.get_environment_context": intelligence.get_environment_context,
        "screen.capture_screen": screen.capture_screen,
        "screen.detect_motion": screen.detect_motion,
        "screen.compute_dhash": screen.compute_dhash,
        "memory.store_memory": memory.store_memory,
        "memory.recall_memory": memory.recall_memory,
        "memory.find_patterns": memory.find_patterns,
    }

    async def health_check(self) -> Observation:
        """Perform a system health check and report observations.

        Returns:
            Observation with health status and any detected issues.
        """
        health = await system.check_system_health()

        severity = "info"
        requires_action = False
        if health.status == "critical":
            severity = "critical"
            requires_action = True
        elif health.status == "degraded":
            severity = "warning"

        return Observation(
            type="health",
            severity=severity,
            summary=f"System {health.status}: CPU {health.cpu_percent}%, "
                    f"RAM {health.memory_percent}%, "
                    f"available {health.memory_available_gb}GB",
            details={
                "cpu_percent": health.cpu_percent,
                "memory_percent": health.memory_percent,
                "memory_available_gb": health.memory_available_gb,
                "disk_percent": health.disk_percent,
                "issues": health.issues,
                "agent_count": health.agent_count,
            },
            requires_action=requires_action,
        )

    async def check_for_anomalies(self) -> List[Observation]:
        """Check system metrics for anomalies.

        Returns:
            List of Observations for any detected anomalies.
        """
        metrics = await system.get_system_metrics()
        anomalies = await intelligence.detect_anomalies(metrics)

        observations = []
        for anomaly in anomalies:
            observations.append(Observation(
                type="anomaly",
                severity=anomaly.severity,
                summary=f"Anomaly: {anomaly.metric_name} = {anomaly.current_value} "
                        f"(expected ~{anomaly.expected_value}, z={anomaly.z_score:.1f})",
                details={
                    "metric": anomaly.metric_name,
                    "current": anomaly.current_value,
                    "expected": anomaly.expected_value,
                    "z_score": anomaly.z_score,
                },
                requires_action=anomaly.severity == "high",
            ))

        return observations

    async def watch_screen_change(
        self,
        reference_frame: Optional[screen.ScreenFrame] = None,
        timeout_s: float = 30.0,
        poll_interval_s: float = 1.0,
    ) -> Optional[Observation]:
        """Watch the screen until something changes.

        Captures frames periodically and compares to the reference
        frame (or the first captured frame if no reference).  Returns
        an observation when significant motion is detected.

        Args:
            reference_frame: Frame to compare against.  If None, captures one.
            timeout_s: Maximum time to watch (seconds).
            poll_interval_s: Time between captures (seconds).

        Returns:
            Observation if screen changed, None if timeout with no change.
        """
        if reference_frame is None:
            reference_frame = await screen.capture_screen()
            if reference_frame is None:
                return None

        import time
        start = time.monotonic()

        while (time.monotonic() - start) < timeout_s:
            await asyncio.sleep(poll_interval_s)
            current = await screen.capture_screen()
            if current is None:
                continue

            if screen.detect_motion(reference_frame, current, threshold_bits=6):
                return Observation(
                    type="screen_change",
                    severity="info",
                    summary="Screen content changed",
                    details={
                        "dhash_before": reference_frame.dhash,
                        "dhash_after": current.dhash,
                        "hamming_distance": screen.hamming_distance(
                            reference_frame.dhash, current.dhash,
                        ),
                        "elapsed_s": round(time.monotonic() - start, 1),
                    },
                    requires_action=False,
                )

        return None

    @classmethod
    def tool_manifest(cls) -> List[Dict[str, str]]:
        """Return the Observer's tool manifest."""
        manifest = []
        for name, fn in cls.TOOLS.items():
            manifest.append({
                "name": name,
                "description": (fn.__doc__ or "").strip().split("\n")[0],
                "module": name.split(".")[0],
            })
        return manifest

    async def execute_tool(self, tool_name: str, **kwargs) -> Any:
        """Execute an Observer tool by name."""
        fn = self.TOOLS.get(tool_name)
        if fn is None:
            raise KeyError(f"Unknown Observer tool: {tool_name}")
        if asyncio.iscoroutinefunction(fn):
            return await fn(**kwargs)
        return fn(**kwargs)
