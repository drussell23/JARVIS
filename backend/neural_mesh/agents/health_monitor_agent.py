"""
JARVIS Neural Mesh - Health Monitor Agent

Monitors the health and performance of all Neural Mesh components.
Provides proactive alerting and self-healing capabilities.
"""

from __future__ import annotations

import asyncio
import logging
import os
import psutil
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import AgentMessage, AgentStatus, KnowledgeType, MessagePriority, MessageType

logger = logging.getLogger(__name__)


class HealthMonitorAgent(BaseNeuralMeshAgent):
    """
    Health Monitor Agent - System health and performance monitoring.

    Capabilities:
    - check_health: Run health checks on all components
    - get_metrics: Get system performance metrics
    - check_agent: Check specific agent health
    - alert: Send health alerts
    - self_heal: Attempt to recover unhealthy components
    """

    # v251.3: Statuses that are acceptable (not counted as issues)
    _HEALTHY_STATUSES = frozenset({AgentStatus.ONLINE, AgentStatus.BUSY})

    # v251.3: Optional agents whose non-ONLINE state never degrades system health.
    # Includes agents that depend on external services (Trinity cross-repo components)
    # or conditionally-available features.
    _OPTIONAL_AGENTS: frozenset = frozenset(
        s.strip() for s in os.getenv(
            "JARVIS_OPTIONAL_AGENTS",
            "computer_use_agent,mas-coordinator,reactor_core",
        ).split(",") if s.strip()
    )

    def __init__(self) -> None:
        super().__init__(
            agent_name="health_monitor_agent",
            agent_type="core",
            capabilities={
                "check_health",
                "get_metrics",
                "check_agent",
                "alert",
                "self_heal",
                "system_status",
            },
            version="1.0.0",
        )

        self._health_history: List[Dict[str, Any]] = []
        self._alerts: List[Dict[str, Any]] = []
        self._monitoring_task: Optional[asyncio.Task] = None
        self._check_interval = 30.0  # seconds
        # v251.3: Startup grace period — agents within this window are not counted as issues
        self._startup_grace_seconds = float(os.getenv("HEALTH_MONITOR_STARTUP_GRACE", "90.0"))
        # v251.3: Log deduplication — only re-log when issue set changes
        self._last_logged_issues: Optional[frozenset] = None
        # v258.0: Sustained CPU tracking + throttle state
        self._cpu_high_consecutive: int = 0
        self._cpu_high_threshold = float(os.getenv("HEALTH_MONITOR_CPU_HIGH_THRESHOLD", "90.0"))
        self._cpu_high_sustained_checks = int(os.getenv("HEALTH_MONITOR_CPU_SUSTAINED_CHECKS", "3"))
        self._cpu_relief_emitted: bool = False
        self._throttle_cache_time: float = 0.0
        self._throttle_factor: float = 1.0

    async def on_initialize(self) -> None:
        logger.info("Initializing HealthMonitorAgent")

        # Start background monitoring
        self._monitoring_task = asyncio.create_task(
            self._monitoring_loop(),
            name="health_monitoring"
        )

        logger.info("HealthMonitorAgent initialized")

    async def on_start(self) -> None:
        logger.info("HealthMonitorAgent started - monitoring system health")

    async def on_stop(self) -> None:
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
        logger.info(f"HealthMonitorAgent stopping - recorded {len(self._alerts)} alerts")

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        action = payload.get("action", "")

        if action == "check_health":
            return await self._check_health()
        elif action == "get_metrics":
            return await self._get_metrics()
        elif action == "check_agent":
            return await self._check_agent(payload.get("agent_name", ""))
        elif action == "system_status":
            return await self._get_system_status()
        elif action == "get_alerts":
            return self._get_alerts(payload.get("limit", 50))
        else:
            raise ValueError(f"Unknown health action: {action}")

    async def _check_health(self) -> Dict[str, Any]:
        """Run comprehensive health check."""
        health = {
            "timestamp": datetime.now().isoformat(),
            "overall_status": "healthy",
            "components": {},
            "issues": [],
        }

        # Check system resources
        try:
            # v258.0: Non-blocking CPU via shared metrics service
            try:
                from backend.core.async_system_metrics import get_cpu_percent
                cpu = await get_cpu_percent()
            except ImportError:
                cpu = psutil.cpu_percent(interval=None)  # instant fallback
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage("/")

            health["system"] = {
                "cpu_percent": cpu,
                "memory_percent": memory.percent,
                "memory_available_gb": memory.available / (1024**3),
                "disk_percent": disk.percent,
                "disk_free_gb": disk.free / (1024**3),
            }

            # v258.0: Track sustained CPU pressure + emit relief
            if cpu > self._cpu_high_threshold:
                health["issues"].append("High CPU usage")
                self._cpu_high_consecutive += 1
                if (self._cpu_high_consecutive >= self._cpu_high_sustained_checks
                        and not self._cpu_relief_emitted):
                    await self._emit_cpu_relief_request(cpu)
                    self._cpu_relief_emitted = True
            else:
                if self._cpu_high_consecutive > 0:
                    self._cpu_high_consecutive = 0
                    if self._cpu_relief_emitted:
                        await self._emit_cpu_recovery(cpu)
                        self._cpu_relief_emitted = False
            if memory.percent > 90:
                health["issues"].append("High memory usage")
            if disk.percent > 90:
                health["issues"].append("Low disk space")
        except Exception as e:
            health["system"] = {"error": str(e)}
            health["issues"].append(f"System metrics error: {e}")

        # Check agents
        if self.registry:
            try:
                agents = await self.registry.get_all_agents()
                now = datetime.now()
                healthy_count = sum(
                    1 for a in agents if a.status in self._HEALTHY_STATUSES
                )
                total = len(agents)

                health["agents"] = {
                    "total": total,
                    "online": healthy_count,
                    "offline": total - healthy_count,
                }

                for agent in agents:
                    health["components"][agent.agent_name] = {
                        "status": agent.status.value,
                        "type": agent.agent_type,
                    }
                    if agent.status in self._HEALTHY_STATUSES:
                        continue

                    # v251.3: Skip agents still within startup grace period
                    if hasattr(agent, "registered_at") and agent.registered_at:
                        age = (now - agent.registered_at).total_seconds()
                        if age < self._startup_grace_seconds:
                            health["components"][agent.agent_name]["grace"] = True
                            continue

                    # v251.3: Optional agents degrade is informational, not an issue
                    if agent.agent_name in self._OPTIONAL_AGENTS:
                        health["components"][agent.agent_name]["optional"] = True
                        continue

                    health["issues"].append(
                        f"Agent {agent.agent_name} is {agent.status.value}"
                    )
                    # v238.0: Broadcast health alert for unhealthy agents
                    try:
                        await self.broadcast(
                            message_type=MessageType.ALERT_RAISED,
                            payload={
                                "type": "agent_health_alert",
                                "agent_name": agent.agent_name,
                                "status": agent.status.value,
                                "agent_type": agent.agent_type,
                            },
                            priority=MessagePriority.HIGH,
                        )
                    except Exception:
                        pass  # Best-effort broadcast
            except Exception as e:
                health["agents"] = {"error": str(e)}

        # Determine overall status
        if health["issues"]:
            if any("error" in issue.lower() for issue in health["issues"]):
                health["overall_status"] = "critical"
            else:
                health["overall_status"] = "degraded"

        # Store in history
        self._health_history.append(health)
        if len(self._health_history) > 1000:
            self._health_history = self._health_history[-1000:]

        return health

    async def _get_metrics(self) -> Dict[str, Any]:
        """Get performance metrics."""
        metrics = {
            "timestamp": datetime.now().isoformat(),
        }

        try:
            # v258.0: Non-blocking CPU via shared metrics service
            try:
                from backend.core.async_system_metrics import get_cpu_percent
                metrics["cpu_percent"] = await get_cpu_percent()
            except ImportError:
                metrics["cpu_percent"] = psutil.cpu_percent(interval=None)
            metrics["memory_percent"] = psutil.virtual_memory().percent
            metrics["disk_percent"] = psutil.disk_usage("/").percent

            # Process metrics (R2-#6: per-process cpu_percent() is non-blocking — NOT migrated)
            process = psutil.Process()
            metrics["process"] = {
                "cpu_percent": process.cpu_percent(),
                "memory_mb": process.memory_info().rss / (1024**2),
                "threads": process.num_threads(),
            }

            # Agent metrics
            if self.registry:
                agents = await self.registry.get_all_agents()
                metrics["agents"] = {
                    "total": len(agents),
                    "by_status": {},
                    "by_type": {},
                }
                for agent in agents:
                    status = agent.status.value
                    metrics["agents"]["by_status"][status] = \
                        metrics["agents"]["by_status"].get(status, 0) + 1
                    agent_type = agent.agent_type
                    metrics["agents"]["by_type"][agent_type] = \
                        metrics["agents"]["by_type"].get(agent_type, 0) + 1

        except Exception as e:
            metrics["error"] = str(e)

        return metrics

    async def _check_agent(self, agent_name: str) -> Dict[str, Any]:
        """Check specific agent health."""
        if not agent_name:
            return {"status": "error", "error": "Agent name required"}

        if self.registry:
            agent = await self.registry.get_agent(agent_name)
            if agent:
                return {
                    "status": "success",
                    "agent": {
                        "name": agent.agent_name,
                        "type": agent.agent_type,
                        "status": agent.status.value,
                        "capabilities": list(agent.capabilities),
                        "last_heartbeat": agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
                    },
                }
        return {"status": "error", "error": f"Agent {agent_name} not found"}

    async def _get_system_status(self) -> Dict[str, Any]:
        """Get overall system status summary."""
        health = await self._check_health()

        return {
            "status": health["overall_status"],
            "timestamp": health["timestamp"],
            "agents": health.get("agents", {}),
            "system": health.get("system", {}),
            "issues_count": len(health.get("issues", [])),
            "recent_alerts": len([a for a in self._alerts if
                                 datetime.fromisoformat(a["timestamp"]) > datetime.now() - timedelta(hours=1)]),
        }

    def _get_alerts(self, limit: int = 50) -> Dict[str, Any]:
        """Get recent alerts."""
        return {
            "status": "success",
            "count": len(self._alerts),
            "alerts": self._alerts[-limit:],
        }

    async def _monitoring_loop(self) -> None:
        """Background health monitoring loop."""
        max_runtime = float(os.getenv("TIMEOUT_HEALTH_MONITORING_SESSION", "86400.0"))  # 24 hours
        health_check_timeout = float(os.getenv("TIMEOUT_HEALTH_CHECK_ITERATION", "30.0"))
        start = time.monotonic()
        cancelled = False

        import sys as _sys  # v258.0: for CPU throttle signal

        while time.monotonic() - start < max_runtime:
            try:
                # v258.0: Self-throttle under CPU pressure — read sys attr (R2-#9: no file I/O)
                effective_interval = self._check_interval
                now = time.time()
                if now - self._throttle_cache_time > 60.0:  # check at most once per minute
                    self._throttle_cache_time = now
                    throttle = getattr(_sys, '_jarvis_cpu_throttle', None)
                    if throttle and throttle.get("expires_at", 0) > now:
                        self._throttle_factor = throttle.get("throttle_factor", 1.0)
                    else:
                        self._throttle_factor = 1.0
                effective_interval = self._check_interval * self._throttle_factor
                await asyncio.sleep(effective_interval)

                health = await asyncio.wait_for(
                    self._check_health(),
                    timeout=health_check_timeout,
                )

                # Create alerts for issues
                for issue in health.get("issues", []):
                    self._alerts.append({
                        "timestamp": datetime.now().isoformat(),
                        "severity": "warning" if "error" not in issue.lower() else "critical",
                        "message": issue,
                    })

                # Limit alerts history
                if len(self._alerts) > 1000:
                    self._alerts = self._alerts[-1000:]

                # v251.3: Log if unhealthy, with deduplication and detail
                issues = health.get("issues", [])
                if health["overall_status"] != "healthy" and issues:
                    current_issues = frozenset(issues)
                    if current_issues != self._last_logged_issues:
                        # Issue set changed — log with full detail
                        logger.warning(
                            "System health: %s - %d issue(s): %s",
                            health["overall_status"],
                            len(issues),
                            "; ".join(issues),
                        )
                        self._last_logged_issues = current_issues
                elif health["overall_status"] == "healthy":
                    if self._last_logged_issues is not None:
                        logger.info("System health recovered to healthy")
                        self._last_logged_issues = None

            except asyncio.TimeoutError:
                logger.warning("Health check iteration timed out")
            except asyncio.CancelledError:
                cancelled = True
                break
            except Exception as e:
                logger.exception(f"Error in health monitoring: {e}")

        if cancelled:
            logger.info("Health monitoring loop cancelled (shutdown)")
        else:
            logger.info("Health monitoring loop reached max runtime, exiting")

    # v258.0: CPU relief signaling methods

    async def _emit_cpu_relief_request(self, cpu: float) -> None:
        """Emit CPU relief request via sys attribute + neural mesh broadcast."""
        import sys as _sys

        logger.warning(
            "Sustained CPU pressure: %.1f%% for %d consecutive checks — emitting relief",
            cpu, self._cpu_high_consecutive,
        )

        # Determine tier
        if cpu >= 99.5:
            level, throttle_factor, duration = "emergency", 16.0, 240.0
        elif cpu >= 98.0:
            level, throttle_factor, duration = "aggressive", 8.0, 120.0
        else:
            level, throttle_factor, duration = "moderate", 4.0, 60.0

        # Primary signal: sys attribute (intra-process, GIL-atomic, zero I/O)
        setattr(_sys, '_jarvis_cpu_throttle', {
            "level": level,
            "throttle_factor": throttle_factor,
            "expires_at": time.time() + duration,
            "timestamp": time.time(),
            "cpu_percent": cpu,
        })

        # Broadcast via neural mesh if available
        try:
            if self.coordinator:
                await self.coordinator.broadcast(AgentMessage(
                    from_agent=self.name,
                    to_agent="*",
                    message_type=MessageType.EVENT,
                    priority=MessagePriority.HIGH,
                    payload={
                        "event": "cpu_relief_request",
                        "cpu_percent": cpu,
                        "level": level,
                        "throttle_factor": throttle_factor,
                        "duration": duration,
                    },
                ))
        except Exception as e:
            logger.debug("Could not broadcast CPU relief: %s", e)

    async def _emit_cpu_recovery(self, cpu: float) -> None:
        """Clear CPU relief signal on recovery."""
        import sys as _sys

        logger.info("CPU pressure recovered: %.1f%% — clearing relief signal", cpu)

        # Clear sys attribute
        if hasattr(_sys, '_jarvis_cpu_throttle'):
            delattr(_sys, '_jarvis_cpu_throttle')

        # Broadcast recovery
        try:
            if self.coordinator:
                await self.coordinator.broadcast(AgentMessage(
                    from_agent=self.name,
                    to_agent="*",
                    message_type=MessageType.EVENT,
                    priority=MessagePriority.NORMAL,
                    payload={
                        "event": "cpu_recovery",
                        "cpu_percent": cpu,
                    },
                ))
        except Exception as e:
            logger.debug("Could not broadcast CPU recovery: %s", e)
