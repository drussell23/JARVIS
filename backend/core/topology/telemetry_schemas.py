"""Telemetry event schemas and payload builders for the Topology package."""
from __future__ import annotations

from typing import Any, Dict, Optional

from backend.core.topology.hardware_env import HardwareEnvironmentState

HARDWARE_SCHEMA = "lifecycle.hardware@1.0.0"
PROACTIVE_DRIVE_SCHEMA = "reasoning.proactive_drive@1.0.0"
HOST_CHANGE_SCHEMA = "host.environment_change@1.0.0"
TENDRIL_SCHEMA = "exploration.tendril@1.0.0"


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


def build_host_change_payload(
    change_type: str,
    path: str,
    domain_hint: str,
    details: Optional[dict] = None,
) -> Dict[str, Any]:
    """Build TelemetryEnvelope payload from a MacOSHostObserver event."""
    return {
        "change_type": change_type,
        "path": path,
        "domain_hint": domain_hint,
        "details": details or {},
    }


def build_tendril_payload(
    capability: str,
    domain: str,
    state: str,
    elapsed_seconds: float,
    dead_end_class: str = "",
    context_isolated: bool = True,
) -> Dict[str, Any]:
    """Build TelemetryEnvelope payload from a TendrilManager outcome."""
    return {
        "capability": capability,
        "domain": domain,
        "state": state,
        "elapsed_seconds": elapsed_seconds,
        "dead_end_class": dead_end_class,
        "context_isolated": context_isolated,
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
