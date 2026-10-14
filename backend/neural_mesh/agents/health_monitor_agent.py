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
        self._low_disk_free_gb = float(os.getenv("HEALTH_MONITOR_LOW_DISK_FREE_GB", "10.0"))
        self._cpu_high_sustained_checks = int(os.getenv("HEALTH_MONITOR_CPU_SUSTAINED_CHECKS", "3"))
        self._cpu_relief_emitted: bool = False
        self._throttle_cache_time: float = 0.0
        self._throttle_factor: float = 1.0
        # v258.3: Startup phase detection for system metrics grace period.
        # High CPU/memory during startup (ML model loading, parallel init)
        # is expected and should not flag the system as "degraded".
        self._startup_time: float = time.monotonic()
        self._metrics_startup_grace = float(
            os.getenv("HEALTH_MONITOR_METRICS_STARTUP_GRACE", "300.0")
        )
        # v258.3: Alert deduplication — prevents identical alerts accumulating.
        # Same issue message within window is not re-alerted; resolution tracked.
        self._alert_dedup_window = float(
            os.getenv("HEALTH_MONITOR_ALERT_DEDUP_WINDOW", "120.0")
        )
        self._active_alerts: Dict[str, float] = {}
        # v258.3: CPU spike detection — track recent readings to detect
        # short-duration spikes that would be masked by averaged data.
        self._cpu_history_max_samples = int(
            os.getenv("HEALTH_MONITOR_CPU_HISTORY_SAMPLES", "10")
        )
        self._cpu_recent_readings: List[float] = []
        self._cpu_spike_threshold = float(
            os.getenv("HEALTH_MONITOR_CPU_SPIKE_THRESHOLD", "95.0")
        )

    def _is_startup_phase(self) -> bool:
        """Check if the system is currently in startup phase.

        v258.3: Uses the unified system phase signal (sys._jarvis_system_phase)
        with fallback to JARVIS_STARTUP_TIMESTAMP env var.  During startup,
        high CPU and memory usage are expected (ML model loading, parallel
        init, torch imports) and should not trigger health degradation.
        """
        import sys as _sys

        # Primary: unified system phase signal (set by supervisor)
        _phase = getattr(_sys, '_jarvis_system_phase', None)
        if _phase:
            return _phase.get("phase") == "startup"

        # Fallback: env var timestamp set early in supervisor startup
        _startup_ts_str = os.environ.get("JARVIS_STARTUP_TIMESTAMP", "")
        if _startup_ts_str:
            try:
                return time.time() - float(_startup_ts_str) < self._metrics_startup_grace
            except (ValueError, TypeError):
                pass

        # Final fallback: time since health monitor creation
        return time.monotonic() - self._startup_time < self._metrics_startup_grace

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

            # v258.3: Track CPU readings for spike detection.
            # The async metrics service samples every 2s with interval=0.1,
            # so each reading is a fresh 100ms measurement, not a long average.
            self._cpu_recent_readings.append(cpu)
            if len(self._cpu_recent_readings) > self._cpu_history_max_samples:
                self._cpu_recent_readings = self._cpu_recent_readings[-self._cpu_history_max_samples:]

            # v258.3: Startup phase awareness — high CPU/memory is expected
            # during ML model loading, parallel init, torch imports.
            _in_startup = self._is_startup_phase()

            # v258.3: Track sustained CPU pressure + emit relief.
            # Only flag as issue when SUSTAINED (N consecutive checks),
            # not on a single spike. During startup, sustained CPU is
            # informational only — it does not degrade system health.
            if cpu > self._cpu_high_threshold:
                self._cpu_high_consecutive += 1
                if self._cpu_high_consecutive >= self._cpu_high_sustained_checks:
                    if not _in_startup:
                        health["issues"].append("High CPU usage (sustained)")
                    else:
                        # Informational during startup — log but don't degrade
                        health.setdefault("info", []).append(
                            f"CPU {cpu:.0f}% (expected during startup)"
                        )
                    if not self._cpu_relief_emitted:
                        await self._emit_cpu_relief_request(cpu)
                        self._cpu_relief_emitted = True
            else:
                if self._cpu_high_consecutive > 0:
                    self._cpu_high_consecutive = 0
                    if self._cpu_relief_emitted:
                        await self._emit_cpu_recovery(cpu)
                        self._cpu_relief_emitted = False

            # v258.3: Spike detection — catch short bursts above spike threshold
            # even if not sustained (e.g., one reading at 99% among normal ones).
            if (len(self._cpu_recent_readings) >= 3
                    and not _in_startup
                    and cpu <= self._cpu_high_threshold):
                # Only check for spikes when current reading is normal
                # (sustained check handles the "currently high" case)
                _recent_max = max(self._cpu_recent_readings[-3:])
                if _recent_max >= self._cpu_spike_threshold:
                    health.setdefault("info", []).append(
                        f"Recent CPU spike: {_recent_max:.0f}% (now {cpu:.0f}%)"
                    )

            # v258.3: Memory threshold is higher during startup (95%)
            # because ML model loading transiently spikes memory.
            _memory_threshold = 95.0 if _in_startup else 90.0
            if memory.percent > _memory_threshold:
                health["issues"].append("High memory usage")
            # Free space is what runs out, not a percentage: 91% used on a
            # 3.7 TB drive is 345 GB free and was reported as critical.
            _free_gb = disk.free / (1024 ** 3)
            if _free_gb < self._low_disk_free_gb:
                health["issues"].append(f"Low disk space ({_free_gb:.1f} GB free)")
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

        # v258.3: Severity-weighted status derivation.
        # 4-level model: healthy → elevated → degraded → critical.
        # "elevated" is a new level for CPU-only sustained pressure —
        # it recovers naturally and should not trigger escalation.
        if health["issues"]:
            _has_error = any("error" in i.lower() for i in health["issues"])
            _has_disk = any("disk" in i.lower() for i in health["issues"])
            _has_agent = any("agent " in i.lower() for i in health["issues"])
            _has_memory = any("memory" in i.lower() for i in health["issues"])
            _has_cpu = any("cpu" in i.lower() for i in health["issues"])

            if _has_error or _has_disk:
                health["overall_status"] = "critical"
            elif _has_agent or _has_memory:
                health["overall_status"] = "degraded"
            elif _has_cpu:
                # CPU-only sustained pressure — recovers naturally
                health["overall_status"] = "elevated"
            else:
                health["overall_status"] = "degraded"

        # v258.3: Publish to unified shared health state — enables all 5 health
        # monitoring systems (HealthMonitorAgent, supervisor HealthChecker,
        # AdaptiveResourceGovernor, TrinityHealthMonitor, DistributedHealthMonitor)
        # to read a single authoritative health assessment.
        import sys as _sys
        _sys._jarvis_health_state = {  # type: ignore[attr-defined]
            "overall_status": health["overall_status"],
            "issues": health.get("issues", []),
            "info": health.get("info", []),
            "system": health.get("system", {}),
            "agents": health.get("agents", {}),
            "timestamp": time.time(),
            "source": "health_monitor_agent",
            "is_startup": self._is_startup_phase(),
        }

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

                # v258.3: Alert dedup — only create new alert if not seen
                # within dedup window. Emit RESOLVED when issues clear.
                _now_ts = time.time()
                _current_issue_set = set(health.get("issues", []))

                for issue in _current_issue_set:
                    _last_seen = self._active_alerts.get(issue, 0.0)
                    if _now_ts - _last_seen > self._alert_dedup_window:
                        # New or expired — create alert
                        self._alerts.append({
                            "timestamp": datetime.now().isoformat(),
                            "severity": "warning" if "error" not in issue.lower() else "critical",
                            "message": issue,
                        })
                    self._active_alerts[issue] = _now_ts

                # Emit RESOLVED for issues that cleared
                _resolved = [
                    k for k in self._active_alerts
                    if k not in _current_issue_set
                ]
                for issue in _resolved:
                    self._alerts.append({
                        "timestamp": datetime.now().isoformat(),
                        "severity": "info",
                        "message": f"RESOLVED: {issue}",
                    })
                    del self._active_alerts[issue]

                # Limit alerts history
                if len(self._alerts) > 1000:
                    self._alerts = self._alerts[-1000:]

                # v258.3: Log with severity-appropriate level + deduplication.
                # "elevated" (CPU-only) → logger.info (not a warning)
                # "degraded" / "critical" → logger.warning
                issues = health.get("issues", [])
                _status = health["overall_status"]
                if _status not in ("healthy",) and issues:
                    current_issues = frozenset(issues)
                    if current_issues != self._last_logged_issues:
                        _msg = (
                            "System health: %s - %d issue(s): %s"
                        )
                        _args = (_status, len(issues), "; ".join(issues))
                        if _status == "elevated":
                            logger.info(_msg, *_args)
                        else:
                            logger.warning(_msg, *_args)
                        self._last_logged_issues = current_issues
                elif _status == "healthy":
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
        _throttle_data = {
            "level": level,
            "throttle_factor": throttle_factor,
            "expires_at": time.time() + duration,
            "timestamp": time.time(),
            "cpu_percent": cpu,
        }
        setattr(_sys, '_jarvis_cpu_throttle', _throttle_data)

        # v258.3: Cross-process signal — write to ~/.jarvis/signals/cpu_pressure.json
        # so JARVIS Prime and Reactor Core can read it (sys attr is process-local).
        try:
            import json
            _signal_dir = os.path.join(os.path.expanduser("~"), ".jarvis", "signals")
            os.makedirs(_signal_dir, exist_ok=True)
            _signal_path = os.path.join(_signal_dir, "cpu_pressure.json")
            _signal_data = json.dumps(_throttle_data)
            # Use atomic write pattern (write tmp + rename) to avoid partial reads
            _tmp_path = _signal_path + ".tmp"
            with open(_tmp_path, "w") as f:
                f.write(_signal_data)
            os.replace(_tmp_path, _signal_path)
        except Exception as _sig_err:
            logger.debug("Could not write cross-process CPU signal: %s", _sig_err)

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

        # v258.3: Clear cross-process signal file
        try:
            _signal_path = os.path.join(
                os.path.expanduser("~"), ".jarvis", "signals", "cpu_pressure.json"
            )
            if os.path.exists(_signal_path):
                os.remove(_signal_path)
        except Exception:
            pass  # Best-effort cleanup

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
