"""
Atomic system monitoring and health tools.

These tools provide the Observer context with system health visibility,
resource metrics, and self-healing capabilities.

Delegates to the existing HealthMonitorAgent and psutil infrastructure.

The 397B Architect selects these tools by reading docstrings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_HEALTH_TIMEOUT_S = float(os.environ.get("TOOL_HEALTH_TIMEOUT_S", "10.0"))

_health_agent = None


@dataclass(frozen=True)
class SystemHealth:
    """Overall system health snapshot.

    Attributes:
        status: "healthy", "degraded", or "critical".
        cpu_percent: Current CPU usage (0-100).
        memory_percent: Current RAM usage (0-100).
        memory_available_gb: Available RAM in gigabytes.
        disk_percent: Disk usage percentage.
        agent_count: Number of active agents in the registry.
        issues: List of detected health issues (strings).
    """
    status: str
    cpu_percent: float
    memory_percent: float
    memory_available_gb: float
    disk_percent: float
    agent_count: int = 0
    issues: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProcessInfo:
    """Information about a running process.

    Attributes:
        pid: Process ID.
        name: Process name.
        cpu_percent: CPU usage by this process.
        memory_mb: Memory usage in megabytes.
        status: Process status ("running", "sleeping", etc.).
    """
    pid: int
    name: str
    cpu_percent: float
    memory_mb: float
    status: str


async def check_system_health() -> SystemHealth:
    """Check overall system health including CPU, memory, disk, and agents.

    Aggregates system resource metrics and agent registry status into
    a single health snapshot.  Identifies issues like high CPU, low
    memory, or unresponsive agents.

    Returns:
        SystemHealth with status, resource metrics, and issue list.

    Use when:
        The Observer needs a quick system health check, or the Architect
        needs to decide whether the system has capacity for a heavy task.
    """
    agent = await _get_health_agent()
    if agent is not None:
        try:
            result = await asyncio.wait_for(
                agent._check_health(),
                timeout=_HEALTH_TIMEOUT_S,
            )
            return SystemHealth(
                status=result.get("overall_status", "unknown"),
                cpu_percent=result.get("cpu_percent", 0),
                memory_percent=result.get("memory_percent", 0),
                memory_available_gb=result.get("memory_available_gb", 0),
                disk_percent=result.get("disk_percent", 0),
                agent_count=result.get("active_agents", 0),
                issues=result.get("issues", []),
            )
        except Exception as exc:
            logger.debug("[tool:system] health agent failed: %s", exc)

    return await _fallback_health()


async def get_system_metrics() -> Dict[str, float]:
    """Get current system resource metrics.

    Returns a flat dictionary of metric_name -> value suitable for
    feeding into detect_anomalies() from the intelligence tools.

    Returns:
        Dict with cpu_percent, memory_percent, memory_available_gb,
        disk_percent, and process counts.

    Use when:
        The Observer needs raw metrics for anomaly detection, trend
        analysis, or dashboard display.
    """
    health = await check_system_health()
    return {
        "cpu_percent": health.cpu_percent,
        "memory_percent": health.memory_percent,
        "memory_available_gb": health.memory_available_gb,
        "disk_percent": health.disk_percent,
        "agent_count": float(health.agent_count),
    }


async def get_top_processes(limit: int = 10) -> List[ProcessInfo]:
    """Get the top processes by CPU usage.

    Useful for identifying what is consuming system resources.

    Args:
        limit: Number of top processes to return (default 10).

    Returns:
        List of ProcessInfo sorted by CPU usage (highest first).

    Use when:
        The Observer detects high CPU and needs to identify which
        process is responsible, or the Architect needs to decide
        whether to throttle background tasks.
    """
    try:
        import psutil
        processes = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "status"]):
            try:
                info = proc.info
                mem_mb = (info.get("memory_info") or proc.memory_info()).rss / (1024 * 1024)
                processes.append(ProcessInfo(
                    pid=info["pid"],
                    name=info.get("name", ""),
                    cpu_percent=info.get("cpu_percent", 0) or 0,
                    memory_mb=round(mem_mb, 1),
                    status=info.get("status", "unknown"),
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        processes.sort(key=lambda p: p.cpu_percent, reverse=True)
        return processes[:limit]

    except ImportError:
        logger.warning("[tool:system] psutil not installed")
        return []
    except Exception as exc:
        logger.error("[tool:system] get_top_processes error: %s", exc)
        return []


async def check_port_available(port: int) -> bool:
    """Check if a TCP port is available (not in use).

    Args:
        port: Port number to check.

    Returns:
        True if the port is available (not listening).

    Use when:
        The Developer or Architect needs to verify a port is free
        before starting a service, or needs to check if a service
        is running on a specific port.
    """
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex(("localhost", port))
            return result != 0  # 0 = connected (port in use)
    except Exception:
        return True  # Assume available on error


async def get_disk_usage(path: str = "/") -> Dict[str, Any]:
    """Get disk usage for a path.

    Args:
        path: Filesystem path to check (default "/" for root).

    Returns:
        Dict with total_gb, used_gb, free_gb, percent_used.

    Use when:
        The Observer needs to check disk space before a large operation
        (model download, log rotation, etc.).
    """
    try:
        import shutil
        usage = shutil.disk_usage(path)
        return {
            "path": path,
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "used_gb": round(usage.used / (1024 ** 3), 1),
            "free_gb": round(usage.free / (1024 ** 3), 1),
            "percent_used": round(usage.used / usage.total * 100, 1),
        }
    except Exception as exc:
        logger.error("[tool:system] disk_usage error: %s", exc)
        return {"path": path, "error": str(exc)}


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

async def _get_health_agent():
    global _health_agent
    if _health_agent is not None:
        return _health_agent

    for path in ("backend.neural_mesh.agents.health_monitor_agent",
                 "neural_mesh.agents.health_monitor_agent"):
        try:
            import importlib
            mod = importlib.import_module(path)
            _health_agent = mod.HealthMonitorAgent()
            return _health_agent
        except (ImportError, Exception):
            continue
    return None


async def _fallback_health() -> SystemHealth:
    """Basic health check without the full agent (uses psutil directly)."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        issues = []
        status = "healthy"
        if cpu > 90:
            issues.append(f"CPU critical: {cpu}%")
            status = "critical"
        elif cpu > 70:
            issues.append(f"CPU high: {cpu}%")
            status = "degraded"
        if mem.percent > 90:
            issues.append(f"Memory critical: {mem.percent}%")
            status = "critical"
        elif mem.percent > 80:
            issues.append(f"Memory high: {mem.percent}%")
            if status == "healthy":
                status = "degraded"

        return SystemHealth(
            status=status,
            cpu_percent=cpu,
            memory_percent=mem.percent,
            memory_available_gb=round(mem.available / (1024 ** 3), 1),
            disk_percent=disk.percent,
            issues=issues,
        )
    except ImportError:
        return SystemHealth(
            status="unknown", cpu_percent=0, memory_percent=0,
            memory_available_gb=0, disk_percent=0,
            issues=["psutil not installed"],
        )
