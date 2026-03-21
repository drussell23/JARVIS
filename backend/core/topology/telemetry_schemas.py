"""Telemetry event schemas and payload builders for the Topology package."""
from __future__ import annotations

from typing import Any, Dict, Optional

from backend.core.topology.hardware_env import HardwareEnvironmentState

HARDWARE_SCHEMA = "lifecycle.hardware@1.0.0"
PROACTIVE_DRIVE_SCHEMA = "reasoning.proactive_drive@1.0.0"


def build_hardware_payload(hw: HardwareEnvironmentState) -> Dict[str, Any]:
    """Build TelemetryEnvelope payload from HardwareEnvironmentState."""
    return {
        "os_family": hw.os_family,
        "cpu_logical_cores": hw.cpu_logical_cores,
        "ram_total_mb": hw.ram_total_mb,
        "ram_available_mb": hw.ram_available_mb,
        "compute_tier": hw.compute_tier.value,
        "gpu_name": hw.gpu.name if hw.gpu else None,
        "gpu_vram_total_mb": hw.gpu.vram_total_mb if hw.gpu else None,
        "gpu_vram_free_mb": hw.gpu.vram_free_mb if hw.gpu else None,
        "hostname": hw.hostname,
        "python_version": hw.python_version,
        "max_parallel_inference_tasks": hw.max_parallel_inference_tasks,
        "max_shadow_harness_workers": hw.max_shadow_harness_workers,
    }


def build_drive_tick_payload(
    state: str,
    reason: str,
    target_name: Optional[str] = None,
    target_domain: Optional[str] = None,
) -> Dict[str, Any]:
    """Build TelemetryEnvelope payload from ProactiveDrive.tick() result."""
    return {
        "state": state,
        "reason": reason,
        "target_name": target_name,
        "target_domain": target_domain,
    }
