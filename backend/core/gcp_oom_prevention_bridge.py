"""
GCP OOM Prevention Bridge v1.0.0
=================================

Intelligent bridge that prevents Out-Of-Memory crashes by:
1. Pre-flight memory checks BEFORE heavy component initialization
2. Automatic GCP Spot VM (32GB RAM) spin-up when local memory is insufficient
3. Seamless offloading of memory-intensive operations to cloud
4. Cross-repo coordination for JARVIS Prime and Reactor Core

This solves the SIGKILL (exit code -9) crash during INITIALIZING_AGI_HUB by
proactively detecting memory pressure and offloading to GCP before OOM occurs.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │  GCP OOM Prevention Bridge (this file)                          │
    │  ├── ProactiveResourceGuard Integration (memory monitoring)     │
    │  ├── MemoryAwareStartup Integration (startup decisions)         │
    │  ├── GCPVMManager Integration (Spot VM lifecycle)               │
    │  └── Cross-Repo Coordination (signals for Prime/Reactor)        │
    └─────────────────────────────────────────────────────────────────┘

Usage:
    from core.gcp_oom_prevention_bridge import (
        check_memory_before_heavy_init,
        ensure_sufficient_memory_or_offload,
        get_oom_prevention_bridge,
    )

    # Before initializing heavy components:
    can_proceed, offload_target = await check_memory_before_heavy_init(
        component="agi_hub",
        estimated_mb=4000,
    )

    if can_proceed:
        # Safe to initialize locally
        await initialize_agi_hub()
    else:
        # Heavy components should run on GCP VM
        await offload_initialization_to_cloud(offload_target)

Author: JARVIS Trinity v131.0 - OOM Prevention
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class MemoryDecision(Enum):
    """Decision about where to run heavy operations."""
    LOCAL = "local"           # Sufficient local RAM
    CLOUD = "cloud"           # Offload to GCP Spot VM
    CLOUD_REQUIRED = "cloud_required"  # Critical - must use cloud
    ABORT = "abort"           # Cannot proceed (no cloud available, critical RAM)


@dataclass
class MemoryCheckResult:
    """Result of pre-initialization memory check."""
    decision: MemoryDecision
    can_proceed_locally: bool
    gcp_vm_required: bool
    gcp_vm_ready: bool
    gcp_vm_ip: Optional[str]
    available_ram_gb: float
    required_ram_gb: float
    memory_pressure_percent: float
    reason: str
    recommendations: List[str] = field(default_factory=list)
    component_name: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/IPC."""
        return {
            "decision": self.decision.value,
            "can_proceed_locally": self.can_proceed_locally,
            "gcp_vm_required": self.gcp_vm_required,
            "gcp_vm_ready": self.gcp_vm_ready,
            "gcp_vm_ip": self.gcp_vm_ip,
            "available_ram_gb": round(self.available_ram_gb, 2),
            "required_ram_gb": round(self.required_ram_gb, 2),
            "memory_pressure_percent": round(self.memory_pressure_percent, 1),
            "reason": self.reason,
            "recommendations": self.recommendations,
            "component_name": self.component_name,
            "timestamp": self.timestamp,
        }


# Memory estimates for heavy components (in MB)
HEAVY_COMPONENT_MEMORY_ESTIMATES = {
    "agi_hub": 4000,           # AGI Hub with ML models
    "neural_mesh": 2000,       # Neural mesh system
    "whisper_large": 3000,     # Whisper large model
    "whisper_medium": 2000,    # Whisper medium model
    "whisper_small": 1000,     # Whisper small model
    "speechbrain": 800,        # SpeechBrain + ECAPA
    "ecapa_tdnn": 500,         # ECAPA-TDNN embeddings
    "pytorch_runtime": 1500,   # PyTorch base runtime
    "transformers": 500,       # Transformers library
    "jarvis_prime": 6000,      # Full JARVIS Prime (GGUF model)
    "reactor_core": 1000,      # Reactor Core ML models
    "vision_system": 1500,     # Computer vision components
    "default": 500,            # Default estimate for unknown components
}

# Thresholds (configurable via environment)
OOM_PREVENTION_THRESHOLDS = {
    "min_free_ram_gb": float(os.getenv("JARVIS_MIN_FREE_RAM_GB", "2.0")),
    "cloud_trigger_ram_gb": float(os.getenv("JARVIS_CLOUD_TRIGGER_RAM_GB", "4.0")),
    "critical_ram_gb": float(os.getenv("JARVIS_CRITICAL_RAM_GB", "1.5")),
    "memory_pressure_cloud_trigger": float(os.getenv("JARVIS_PRESSURE_CLOUD_TRIGGER", "75.0")),
    "memory_pressure_critical": float(os.getenv("JARVIS_PRESSURE_CRITICAL", "90.0")),
}


class GCPOOMPreventionBridge:
    """
    Bridge that coordinates OOM prevention across JARVIS components.

    Features:
    - Pre-flight memory checks before heavy initialization
    - Automatic GCP Spot VM spin-up when needed
    - Cross-repo signal coordination
    - Intelligent decision-making for cloud vs local
    """

    def __init__(self):
        self._memory_aware_startup = None
        self._gcp_vm_manager = None
        self._proactive_guard = None
        self._initialized = False
        self._active_gcp_vm: Optional[Dict[str, Any]] = None
        self._offload_mode_active = False
        self._lock = asyncio.Lock()

        # Cross-repo signal file for coordination
        self._signal_dir = Path(os.getenv(
            "JARVIS_SIGNAL_DIR",
            str(Path.home() / ".jarvis" / "signals")
        ))
        self._signal_dir.mkdir(parents=True, exist_ok=True)

        # Track memory checks
        self._check_history: List[MemoryCheckResult] = []
        self._last_check_time: float = 0
        self._check_cooldown_seconds = 5.0  # Don't check too frequently

        logger.info("[OOMBridge] Initialized GCP OOM Prevention Bridge v1.0.0")

    async def initialize(self) -> bool:
        """
        Initialize connections to memory monitoring and GCP services.

        Returns:
            True if initialized successfully
        """
        if self._initialized:
            return True

        async with self._lock:
            if self._initialized:
                return True

            try:
                # Initialize ProactiveResourceGuard for memory monitoring
                try:
                    from core.proactive_resource_guard import get_proactive_resource_guard
                    self._proactive_guard = get_proactive_resource_guard()
                    logger.info("[OOMBridge] ProactiveResourceGuard connected")
                except ImportError:
                    logger.warning("[OOMBridge] ProactiveResourceGuard not available")

                # Initialize MemoryAwareStartup for startup decisions
                try:
                    from core.memory_aware_startup import get_startup_manager
                    self._memory_aware_startup = await get_startup_manager()
                    logger.info("[OOMBridge] MemoryAwareStartup connected")
                except ImportError:
                    logger.warning("[OOMBridge] MemoryAwareStartup not available")

                # Initialize GCPVMManager for cloud offloading
                try:
                    from core.gcp_vm_manager import get_gcp_vm_manager
                    self._gcp_vm_manager = await get_gcp_vm_manager()
                    if self._gcp_vm_manager.enabled:
                        logger.info("[OOMBridge] GCPVMManager connected (enabled)")
                    else:
                        logger.info("[OOMBridge] GCPVMManager connected (disabled)")
                except ImportError:
                    logger.warning("[OOMBridge] GCPVMManager not available")

                self._initialized = True
                return True

            except Exception as e:
                logger.error(f"[OOMBridge] Initialization failed: {e}")
                return False

    async def _get_memory_status(self) -> Tuple[float, float]:
        """
        Get current memory status.

        Returns:
            Tuple of (available_ram_gb, memory_pressure_percent)
        """
        # Try MemoryAwareStartup first (most accurate on macOS)
        if self._memory_aware_startup:
            try:
                status = await self._memory_aware_startup.get_memory_status()
                return status.available_gb, status.memory_pressure
            except Exception as e:
                logger.debug(f"[OOMBridge] MemoryAwareStartup check failed: {e}")

        # Fallback to ProactiveResourceGuard
        if self._proactive_guard:
            try:
                total_gb, available_gb, used_gb = self._proactive_guard.get_memory_info()
                pressure = (used_gb / total_gb * 100) if total_gb > 0 else 0
                return available_gb, pressure
            except Exception as e:
                logger.debug(f"[OOMBridge] ProactiveResourceGuard check failed: {e}")

        # Final fallback - use psutil
        try:
            import psutil
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024**3)
            pressure = mem.percent
            return available_gb, pressure
        except ImportError:
            pass

        # Assume constrained if we can't check
        return 2.0, 85.0

    def _get_component_memory_estimate(self, component: str) -> int:
        """Get memory estimate for a component in MB."""
        return HEAVY_COMPONENT_MEMORY_ESTIMATES.get(
            component.lower(),
            HEAVY_COMPONENT_MEMORY_ESTIMATES["default"]
        )

    async def check_memory_before_init(
        self,
        component: str,
        estimated_mb: Optional[int] = None,
        auto_offload: bool = True,
    ) -> MemoryCheckResult:
        """
        Check if there's sufficient memory before initializing a heavy component.

        This is the main entry point for OOM prevention. Call this BEFORE
        initializing any heavy component (ML models, neural mesh, etc.).

        Args:
            component: Name of the component to initialize
            estimated_mb: Estimated memory requirement (uses lookup if not provided)
            auto_offload: If True, automatically spin up GCP VM when needed

        Returns:
            MemoryCheckResult with decision and recommendations
        """
        await self.initialize()

        # Get memory estimate
        if estimated_mb is None:
            estimated_mb = self._get_component_memory_estimate(component)
        required_gb = estimated_mb / 1024

        # Get current memory status
        available_gb, pressure = await self._get_memory_status()

        thresholds = OOM_PREVENTION_THRESHOLDS
        recommendations = []

        logger.info(f"[OOMBridge] Memory check for '{component}':")
        logger.info(f"  Available RAM: {available_gb:.1f} GB")
        logger.info(f"  Required RAM: {required_gb:.1f} GB")
        logger.info(f"  Memory pressure: {pressure:.1f}%")

        # Determine remaining RAM after component loads
        remaining_after_load = available_gb - required_gb

        # Decision logic
        if pressure >= thresholds["memory_pressure_critical"]:
            # CRITICAL: Memory pressure too high
            decision = MemoryDecision.CLOUD_REQUIRED
            can_proceed_locally = False
            gcp_required = True
            reason = f"CRITICAL memory pressure ({pressure:.1f}% >= {thresholds['memory_pressure_critical']}%)"
            recommendations.append("⚠️ CRITICAL: System near OOM - GCP VM required")
            recommendations.append("Close applications immediately to prevent crash")

        elif remaining_after_load < thresholds["critical_ram_gb"]:
            # Would leave system in critical state
            decision = MemoryDecision.CLOUD_REQUIRED
            can_proceed_locally = False
            gcp_required = True
            reason = f"Loading {component} would leave only {remaining_after_load:.1f}GB free (< {thresholds['critical_ram_gb']}GB critical threshold)"
            recommendations.append(f"🚀 Offloading {component} to GCP Spot VM (32GB RAM)")
            recommendations.append("Local RAM will remain stable")

        elif remaining_after_load < thresholds["cloud_trigger_ram_gb"] or pressure >= thresholds["memory_pressure_cloud_trigger"]:
            # Below cloud trigger threshold - recommend cloud
            decision = MemoryDecision.CLOUD
            can_proceed_locally = False  # Strongly recommend cloud
            gcp_required = True
            reason = f"Memory constrained ({remaining_after_load:.1f}GB after load < {thresholds['cloud_trigger_ram_gb']}GB threshold)"
            recommendations.append(f"☁️ Recommended: Run {component} on GCP Spot VM")
            recommendations.append("Cost: ~$0.029/hour for e2-highmem-4 (32GB RAM)")
            recommendations.append("Auto-terminates when idle")

        elif remaining_after_load < thresholds["min_free_ram_gb"]:
            # Borderline - can proceed but with warning
            decision = MemoryDecision.LOCAL
            can_proceed_locally = True
            gcp_required = False
            reason = f"Borderline RAM ({remaining_after_load:.1f}GB after load)"
            recommendations.append(f"⚠️ Low RAM warning: {remaining_after_load:.1f}GB will remain after loading {component}")
            recommendations.append("Consider closing other applications")

        else:
            # Sufficient memory
            decision = MemoryDecision.LOCAL
            can_proceed_locally = True
            gcp_required = False
            reason = f"Sufficient RAM ({remaining_after_load:.1f}GB will remain after loading {component})"
            recommendations.append(f"✅ Safe to load {component} locally")

        # If GCP required and auto_offload enabled, try to spin up VM
        gcp_ready = False
        gcp_ip = None

        if gcp_required and auto_offload:
            gcp_ready, gcp_ip = await self._ensure_gcp_vm_ready()
            if not gcp_ready:
                if decision == MemoryDecision.CLOUD_REQUIRED:
                    decision = MemoryDecision.ABORT
                    recommendations.append("❌ Cannot proceed: GCP VM unavailable and local RAM insufficient")
                else:
                    # Downgrade to local with warning
                    decision = MemoryDecision.LOCAL
                    can_proceed_locally = True
                    recommendations.append("⚠️ GCP unavailable - proceeding locally with risk")

        result = MemoryCheckResult(
            decision=decision,
            can_proceed_locally=can_proceed_locally,
            gcp_vm_required=gcp_required,
            gcp_vm_ready=gcp_ready,
            gcp_vm_ip=gcp_ip,
            available_ram_gb=available_gb,
            required_ram_gb=required_gb,
            memory_pressure_percent=pressure,
            reason=reason,
            recommendations=recommendations,
            component_name=component,
        )

        # Log decision
        logger.info(f"[OOMBridge] Decision: {decision.value}")
        logger.info(f"  Reason: {reason}")
        for rec in recommendations:
            logger.info(f"  → {rec}")

        # Track history
        self._check_history.append(result)
        if len(self._check_history) > 100:
            self._check_history = self._check_history[-50:]

        # Write cross-repo signal
        await self._write_oom_signal(result)

        return result

    async def _ensure_gcp_vm_ready(self) -> Tuple[bool, Optional[str]]:
        """
        Ensure a GCP Spot VM is ready for offloading.

        Returns:
            Tuple of (is_ready, vm_ip)
        """
        if not self._gcp_vm_manager or not self._gcp_vm_manager.enabled:
            logger.warning("[OOMBridge] GCP VM Manager not available or disabled")
            return False, None

        try:
            # Check if we already have an active VM
            if self._active_gcp_vm and self._active_gcp_vm.get("ip"):
                # Verify it's still healthy
                if self._gcp_vm_manager.is_ready:
                    logger.info(f"[OOMBridge] Reusing active GCP VM: {self._active_gcp_vm.get('ip')}")
                    return True, self._active_gcp_vm.get("ip")

            # Initialize manager if needed
            if not self._gcp_vm_manager.initialized:
                await self._gcp_vm_manager.initialize()

            # Check if manager is ready
            if not self._gcp_vm_manager.is_ready:
                logger.warning("[OOMBridge] GCP VM Manager not ready")
                return False, None

            # Get memory status for VM creation decision
            available_gb, pressure = await self._get_memory_status()

            # Create memory snapshot for the manager
            import platform as platform_module

            class MemorySnapshot:
                def __init__(self, available_gb: float, pressure: float):
                    self.gcp_shift_recommended = True  # We already decided we need cloud
                    self.reasoning = f"OOM prevention: {pressure:.1f}% pressure, {available_gb:.1f}GB available"
                    self.memory_pressure = pressure
                    self.available_gb = available_gb
                    self.total_gb = 16.0  # Estimate
                    self.used_gb = 16.0 - available_gb
                    self.usage_percent = pressure
                    self.platform = platform_module.system().lower()
                    self.macos_pressure_level = "critical" if pressure > 85 else "warning"
                    self.macos_is_swapping = pressure > 80
                    self.macos_page_outs = 0
                    self.linux_psi_some_avg10 = None
                    self.linux_psi_full_avg10 = None

            memory_snapshot = MemorySnapshot(available_gb, pressure)

            # Check if we should create VM
            should_create, reason, _confidence = await self._gcp_vm_manager.should_create_vm(
                memory_snapshot,
                trigger_reason="OOM prevention - automatic offloading"
            )

            if should_create:
                logger.info(f"[OOMBridge] Creating GCP Spot VM: {reason}")

                # Create VM with ML components
                # create_vm returns Optional[VMInstance], not a dict
                vm_instance = await self._gcp_vm_manager.create_vm(
                    components=["ml_backend", "heavy_processing"],
                    trigger_reason=f"OOM Prevention: {reason}",
                )

                if vm_instance and vm_instance.state.value == "running":
                    # Store VM info as dict for compatibility
                    self._active_gcp_vm = {
                        "instance_id": vm_instance.instance_id,
                        "name": vm_instance.name,
                        "ip_address": vm_instance.ip_address,
                        "internal_ip": vm_instance.internal_ip,
                        "zone": vm_instance.zone,
                        "cost_per_hour": vm_instance.cost_per_hour,
                    }
                    self._offload_mode_active = True
                    logger.info(f"[OOMBridge] ✅ GCP Spot VM ready:")
                    logger.info(f"  Instance: {vm_instance.instance_id}")
                    logger.info(f"  IP: {vm_instance.ip_address}")
                    logger.info(f"  Cost: ${vm_instance.cost_per_hour}/hour")
                    return True, vm_instance.ip_address
                else:
                    logger.error(f"[OOMBridge] GCP VM creation failed: {vm_instance}")
                    return False, None
            else:
                logger.info(f"[OOMBridge] GCP VM creation declined: {reason}")
                return False, None

        except Exception as e:
            logger.error(f"[OOMBridge] Failed to ensure GCP VM: {e}")
            return False, None

    async def _write_oom_signal(self, result: MemoryCheckResult) -> None:
        """
        Write OOM signal file for cross-repo coordination.

        This allows JARVIS Prime and Reactor Core to know about memory
        decisions and adjust their behavior accordingly.
        """
        try:
            signal_file = self._signal_dir / "oom_prevention.json"
            signal_data = {
                "timestamp": time.time(),
                "decision": result.decision.value,
                "gcp_vm_required": result.gcp_vm_required,
                "gcp_vm_ip": result.gcp_vm_ip,
                "available_ram_gb": result.available_ram_gb,
                "memory_pressure_percent": result.memory_pressure_percent,
                "offload_mode_active": self._offload_mode_active,
                "component": result.component_name,
            }
            with open(signal_file, "w") as f:
                json.dump(signal_data, f, indent=2)
        except Exception as e:
            logger.debug(f"[OOMBridge] Could not write OOM signal: {e}")

    async def get_offload_endpoint(self, operation: str) -> Optional[str]:
        """
        Get the endpoint for offloaded operations.

        Args:
            operation: Operation type (ml_inference, heavy_compute, etc.)

        Returns:
            Endpoint URL or None if offloading not active
        """
        if not self._offload_mode_active or not self._active_gcp_vm:
            return None

        gcp_ip = self._active_gcp_vm.get("ip_address")
        if not gcp_ip:
            return None

        # Return the GCP endpoint
        return f"http://{gcp_ip}:8010/api/{operation}"

    def is_offload_mode_active(self) -> bool:
        """Check if cloud offloading is active."""
        return self._offload_mode_active

    def get_status(self) -> Dict[str, Any]:
        """Get current bridge status."""
        return {
            "initialized": self._initialized,
            "offload_mode_active": self._offload_mode_active,
            "active_gcp_vm": self._active_gcp_vm,
            "gcp_enabled": bool(self._gcp_vm_manager and self._gcp_vm_manager.enabled),
            "gcp_ready": bool(self._gcp_vm_manager and self._gcp_vm_manager.is_ready),
            "recent_checks": len(self._check_history),
            "thresholds": OOM_PREVENTION_THRESHOLDS,
        }

    async def cleanup(self) -> None:
        """Cleanup resources on shutdown."""
        if self._offload_mode_active and self._gcp_vm_manager:
            logger.info("[OOMBridge] Cleaning up GCP VMs...")
            try:
                await self._gcp_vm_manager.cleanup_all_vms(
                    reason="JARVIS shutdown - OOM prevention bridge cleanup"
                )
            except Exception as e:
                logger.warning(f"[OOMBridge] Cleanup warning: {e}")

        self._offload_mode_active = False
        self._active_gcp_vm = None


# =============================================================================
# GLOBAL SINGLETON
# =============================================================================

_bridge_instance: Optional[GCPOOMPreventionBridge] = None
_bridge_lock = asyncio.Lock()


async def get_oom_prevention_bridge() -> GCPOOMPreventionBridge:
    """Get or create the global OOM prevention bridge."""
    global _bridge_instance
    if _bridge_instance is None:
        async with _bridge_lock:
            if _bridge_instance is None:
                _bridge_instance = GCPOOMPreventionBridge()
                await _bridge_instance.initialize()
    return _bridge_instance


async def check_memory_before_heavy_init(
    component: str,
    estimated_mb: Optional[int] = None,
    auto_offload: bool = True,
) -> MemoryCheckResult:
    """
    Convenience function to check memory before initializing heavy components.

    Args:
        component: Component name
        estimated_mb: Memory estimate in MB (optional)
        auto_offload: Auto-spin up GCP VM if needed

    Returns:
        MemoryCheckResult with decision
    """
    bridge = await get_oom_prevention_bridge()
    return await bridge.check_memory_before_init(component, estimated_mb, auto_offload)


async def ensure_sufficient_memory_or_offload(
    component: str,
    estimated_mb: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Ensure sufficient memory or get offload endpoint.

    Returns:
        Tuple of (can_proceed_locally, offload_endpoint_or_none)
    """
    result = await check_memory_before_heavy_init(component, estimated_mb, auto_offload=True)

    if result.can_proceed_locally:
        return True, None
    elif result.gcp_vm_ready:
        return False, result.gcp_vm_ip
    else:
        # Decision is ABORT
        return False, None


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "GCPOOMPreventionBridge",
    "MemoryCheckResult",
    "MemoryDecision",
    "get_oom_prevention_bridge",
    "check_memory_before_heavy_init",
    "ensure_sufficient_memory_or_offload",
    "HEAVY_COMPONENT_MEMORY_ESTIMATES",
    "OOM_PREVENTION_THRESHOLDS",
]
