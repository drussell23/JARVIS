"""
Container Awareness for JARVIS Loading Server v212.0
=====================================================

Detects container/cgroup limits for resource-aware timeout scaling.

Features:
- cgroup v1 and v2 detection
- Memory limit detection
- CPU quota detection
- Automatic timeout scaling based on resources
- Kubernetes/Docker environment detection
- Resource pressure monitoring

Usage:
    from backend.loading_server.container_awareness import ContainerAwareness

    container = ContainerAwareness()
    if container.is_containerized():
        multiplier = container.get_timeout_multiplier()
        adjusted_timeout = base_timeout * multiplier

Author: JARVIS Trinity System
Version: 212.0.0
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("LoadingServer.Container")


@dataclass
class ContainerAwareness:
    """
    Detects container/cgroup limits for resource-aware timeouts.

    Supports:
    - Docker containers
    - Kubernetes pods
    - LXC/LXD containers
    - Podman containers
    - systemd cgroups

    Features:
    - cgroup v1 and v2 detection
    - Memory limit detection
    - CPU quota detection
    - Automatic timeout scaling based on resources
    """

    _in_container: Optional[bool] = field(init=False, default=None)
    _memory_limit_bytes: Optional[int] = field(init=False, default=None)
    _cpu_quota: Optional[float] = field(init=False, default=None)
    _cgroup_version: Optional[int] = field(init=False, default=None)
    _container_runtime: Optional[str] = field(init=False, default=None)

    @property
    def is_containerized(self) -> bool:
        """Property accessor for container detection (calls the method)."""
        return self._is_containerized()

    @property
    def container_type(self) -> Optional[str]:
        """Property accessor for container runtime type."""
        return self._container_runtime

    @property
    def cgroup_version(self) -> Optional[int]:
        """Property accessor for cgroup version."""
        return self._cgroup_version

    def __post_init__(self):
        """Detect container environment on initialization."""
        self._detect_environment()

    def _detect_environment(self) -> None:
        """Detect container runtime and cgroup version."""
        # Detect cgroup version
        if Path("/sys/fs/cgroup/cgroup.controllers").exists():
            self._cgroup_version = 2
        elif Path("/sys/fs/cgroup/memory").exists():
            self._cgroup_version = 1
        else:
            self._cgroup_version = None

        # Detect container runtime
        if Path("/.dockerenv").exists():
            self._container_runtime = "docker"
            self._in_container = True
        elif os.environ.get("KUBERNETES_SERVICE_HOST"):
            self._container_runtime = "kubernetes"
            self._in_container = True
        elif self._check_cgroup_for_container():
            self._container_runtime = "container"
            self._in_container = True
        else:
            self._container_runtime = None
            self._in_container = False

    def _check_cgroup_for_container(self) -> bool:
        """Check cgroup for container indicators."""
        try:
            cgroup_path = Path("/proc/1/cgroup")
            if cgroup_path.exists():
                content = cgroup_path.read_text()
                container_indicators = [
                    "docker",
                    "kubepods",
                    "lxc",
                    "containerd",
                    "podman",
                    "crio",
                    "garden",
                ]
                return any(indicator in content.lower() for indicator in container_indicators)
        except (PermissionError, IOError):
            pass
        return False

    @lru_cache(maxsize=1)
    def _is_containerized(self) -> bool:
        """
        Check if running in container (Docker/K8s/etc).

        Returns:
            True if running in a container, False otherwise
        """
        if self._in_container is not None:
            return self._in_container

        # Additional checks
        checks = [
            Path("/.dockerenv").exists(),
            os.environ.get("KUBERNETES_SERVICE_HOST") is not None,
            os.environ.get("container") is not None,
            self._check_cgroup_for_container(),
        ]

        self._in_container = any(checks)
        return self._in_container

    def get_container_runtime(self) -> Optional[str]:
        """
        Get the detected container runtime.

        Returns:
            Runtime name (docker, kubernetes, lxc, podman, etc.) or None
        """
        return self._container_runtime

    def get_cgroup_version(self) -> Optional[int]:
        """
        Get the cgroup version (1 or 2).

        Returns:
            1 for cgroup v1, 2 for cgroup v2, None if not detected
        """
        return self._cgroup_version

    def get_memory_limit(self) -> Optional[int]:
        """
        Get container memory limit in bytes.

        Returns:
            Memory limit in bytes, or None if unlimited/not detected
        """
        if self._memory_limit_bytes is not None:
            return self._memory_limit_bytes

        # Try cgroup v2 first
        cgroup_v2_paths = [
            Path("/sys/fs/cgroup/memory.max"),
            Path("/sys/fs/cgroup/user.slice/memory.max"),
        ]

        for cgroup_v2 in cgroup_v2_paths:
            if cgroup_v2.exists():
                try:
                    limit = cgroup_v2.read_text().strip()
                    if limit != "max":
                        self._memory_limit_bytes = int(limit)
                        return self._memory_limit_bytes
                except (ValueError, IOError):
                    pass

        # Try cgroup v1
        cgroup_v1_paths = [
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            Path("/sys/fs/cgroup/memory/docker/memory.limit_in_bytes"),
        ]

        for cgroup_v1 in cgroup_v1_paths:
            if cgroup_v1.exists():
                try:
                    limit = int(cgroup_v1.read_text().strip())
                    # Check if it's effectively unlimited (huge value)
                    if limit < 1e18:  # Less than ~1 exabyte
                        self._memory_limit_bytes = limit
                        return self._memory_limit_bytes
                except (ValueError, IOError):
                    pass

        return None

    def get_memory_usage(self) -> Optional[int]:
        """
        Get current container memory usage in bytes.

        Returns:
            Current memory usage in bytes, or None if not available
        """
        # cgroup v2
        v2_path = Path("/sys/fs/cgroup/memory.current")
        if v2_path.exists():
            try:
                return int(v2_path.read_text().strip())
            except (ValueError, IOError):
                pass

        # cgroup v1
        v1_path = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        if v1_path.exists():
            try:
                return int(v1_path.read_text().strip())
            except (ValueError, IOError):
                pass

        return None

    def get_memory_pressure(self) -> Optional[float]:
        """
        Get memory pressure ratio (0.0 - 1.0).

        Returns:
            Memory usage as fraction of limit, or None if not available
        """
        limit = self.get_memory_limit()
        usage = self.get_memory_usage()

        if limit and usage:
            return min(1.0, usage / limit)
        return None

    def get_cpu_quota(self) -> Optional[float]:
        """
        Get container CPU quota as a fraction of a CPU.

        Returns:
            CPU quota (e.g., 0.5 = half a CPU), or None if unlimited
        """
        if self._cpu_quota is not None:
            return self._cpu_quota

        # cgroup v2
        v2_path = Path("/sys/fs/cgroup/cpu.max")
        if v2_path.exists():
            try:
                content = v2_path.read_text().strip()
                parts = content.split()
                if len(parts) >= 2 and parts[0] != "max":
                    quota = int(parts[0])
                    period = int(parts[1])
                    self._cpu_quota = quota / period
                    return self._cpu_quota
            except (ValueError, IOError, IndexError):
                pass

        # cgroup v1
        v1_quota = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        v1_period = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")

        if v1_quota.exists() and v1_period.exists():
            try:
                quota = int(v1_quota.read_text().strip())
                period = int(v1_period.read_text().strip())
                if quota > 0 and period > 0:
                    self._cpu_quota = quota / period
                    return self._cpu_quota
            except (ValueError, IOError):
                pass

        return None

    def get_cpu_count(self) -> int:
        """
        Get number of CPUs available to the container.

        Returns:
            Number of CPUs (respects cgroup limits if set)
        """
        # Check for Kubernetes CPU request/limit
        cpu_limit = os.environ.get("KUBERNETES_CPU_LIMIT")
        if cpu_limit:
            try:
                return max(1, int(float(cpu_limit)))
            except ValueError:
                pass

        # Check cgroup CPU quota
        quota = self.get_cpu_quota()
        if quota:
            return max(1, int(quota))

        # Fall back to system CPU count
        return os.cpu_count() or 1

    def get_timeout_multiplier(self) -> float:
        """
        Get timeout multiplier based on container resources.

        Returns 1.0 for native, > 1.0 for resource-constrained containers.
        Use this to scale timeouts when running in constrained environments.

        Returns:
            Multiplier to apply to base timeouts
        """
        if not self.is_containerized():
            return 1.0

        multiplier = 1.0

        # Memory-based scaling
        memory_limit = self.get_memory_limit()
        if memory_limit:
            gb = memory_limit / (1024 ** 3)
            if gb < 1.0:
                multiplier = max(multiplier, 3.0)  # Triple timeouts for <1GB
            elif gb < 2.0:
                multiplier = max(multiplier, 2.0)  # Double timeouts for <2GB
            elif gb < 4.0:
                multiplier = max(multiplier, 1.5)  # 1.5x for <4GB

        # CPU-based scaling
        cpu_quota = self.get_cpu_quota()
        if cpu_quota:
            if cpu_quota < 0.5:
                multiplier = max(multiplier, 3.0)  # Very limited CPU
            elif cpu_quota < 1.0:
                multiplier = max(multiplier, 2.0)  # Less than 1 CPU
            elif cpu_quota < 2.0:
                multiplier = max(multiplier, 1.5)  # 1-2 CPUs

        # Memory pressure scaling
        pressure = self.get_memory_pressure()
        if pressure and pressure > 0.8:
            multiplier = max(multiplier, 2.0)  # Under memory pressure

        return multiplier

    def get_resource_summary(self) -> Dict[str, Any]:
        """
        Get a summary of detected container resources.

        Returns:
            Dict with container info, memory, CPU details
        """
        memory_limit = self.get_memory_limit()
        memory_usage = self.get_memory_usage()

        return {
            "is_containerized": self.is_containerized(),
            "container_runtime": self.get_container_runtime(),
            "cgroup_version": self.get_cgroup_version(),
            "memory": {
                "limit_bytes": memory_limit,
                "limit_gb": round(memory_limit / (1024 ** 3), 2) if memory_limit else None,
                "usage_bytes": memory_usage,
                "usage_gb": round(memory_usage / (1024 ** 3), 2) if memory_usage else None,
                "pressure": self.get_memory_pressure(),
            },
            "cpu": {
                "quota": self.get_cpu_quota(),
                "count": self.get_cpu_count(),
            },
            "timeout_multiplier": self.get_timeout_multiplier(),
        }

    def should_reduce_features(self) -> bool:
        """
        Check if we should reduce features due to resource constraints.

        Returns:
            True if the environment is very constrained
        """
        memory_limit = self.get_memory_limit()
        cpu_quota = self.get_cpu_quota()

        # Very constrained if < 512MB or < 0.25 CPU
        if memory_limit and memory_limit < 512 * 1024 * 1024:
            return True
        if cpu_quota and cpu_quota < 0.25:
            return True

        return False

    def get_recommended_worker_count(self) -> int:
        """
        Get recommended number of worker threads/processes.

        Returns:
            Recommended worker count based on available resources
        """
        cpu_count = self.get_cpu_count()

        # In containers, be more conservative with workers
        if self.is_containerized():
            # Use at most half of available CPUs, minimum 1
            return max(1, min(4, cpu_count // 2))

        # Native: use all CPUs up to a reasonable limit
        return max(1, min(8, cpu_count))
