"""HardwareEnvironmentState — dynamic hardware discovery at boot."""
from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ComputeTier(str, Enum):
    CLOUD_GPU = "cloud_gpu"
    CLOUD_CPU = "cloud_cpu"
    LOCAL_GPU = "local_gpu"
    LOCAL_CPU = "local_cpu"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GPUState:
    name: str
    vram_total_mb: int
    vram_free_mb: int
    driver_version: str


@dataclass(frozen=True)
class HardwareEnvironmentState:
    """Immutable snapshot of physical constraints discovered at boot.

    Written once at supervisor Zone 1.0, never mutated.
    Distributed to Prime and Reactor via TelemetryBus
    lifecycle.hardware@1.0.0 envelope.
    """
    os_family: str
    cpu_logical_cores: int
    ram_total_mb: int
    ram_available_mb: int
    compute_tier: ComputeTier
    gpu: Optional[GPUState]
    hostname: str
    python_version: str
    max_parallel_inference_tasks: int
    max_shadow_harness_workers: int

    @classmethod
    def discover(cls) -> HardwareEnvironmentState:
        """Discover actual hardware at runtime. No hardcoding."""
        import psutil

        os_family = platform.system().lower()
        cpu_cores = psutil.cpu_count(logical=True) or 1
        mem = psutil.virtual_memory()
        ram_total_mb = mem.total // (1024 * 1024)
        ram_available_mb = mem.available // (1024 * 1024)
        hostname = platform.node()
        python_version = platform.python_version()

        gpu = cls._probe_gpu()
        tier = cls._classify_tier(gpu, cpu_cores, ram_total_mb)

        MIN_INFERENCE_MB = 2048
        max_parallel = max(1, ram_available_mb // MIN_INFERENCE_MB)
        max_shadow = max(1, cpu_cores // 2)

        return cls(
            os_family=os_family,
            cpu_logical_cores=cpu_cores,
            ram_total_mb=ram_total_mb,
            ram_available_mb=ram_available_mb,
            compute_tier=tier,
            gpu=gpu,
            hostname=hostname,
            python_version=python_version,
            max_parallel_inference_tasks=max_parallel,
            max_shadow_harness_workers=max_shadow,
        )

    @staticmethod
    def _probe_gpu() -> Optional[GPUState]:
        """Probe NVIDIA GPU via nvidia-smi. Returns None on CPU-only or error."""
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version",
                 "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL, timeout=5,
            ).decode().strip().splitlines()[0]
            parts = [p.strip() for p in out.split(",")]
            return GPUState(
                name=parts[0],
                vram_total_mb=int(parts[1]),
                vram_free_mb=int(parts[2]),
                driver_version=parts[3],
            )
        except Exception:
            return None

    @staticmethod
    def _classify_tier(gpu: Optional[GPUState], cores: int, ram_mb: int) -> ComputeTier:
        in_cloud = any(v in os.environ for v in ("GOOGLE_CLOUD_PROJECT", "AWS_REGION", "GCE_METADATA_HOST"))
        if gpu and in_cloud:
            return ComputeTier.CLOUD_GPU
        if gpu:
            return ComputeTier.LOCAL_GPU
        if in_cloud:
            return ComputeTier.CLOUD_CPU
        return ComputeTier.LOCAL_CPU
