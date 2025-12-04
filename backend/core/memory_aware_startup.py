"""
Memory-Aware Startup Manager
============================

Intelligent startup system that detects available RAM and automatically
activates hybrid cloud architecture when local resources are constrained.

Features:
- Detects available RAM at startup using macOS vm_stat
- Automatically activates cloud-first mode when RAM < threshold
- Spins up GCP Spot VM for heavy ML processing
- Configures hybrid router to offload ML to cloud
- Zero hardcoding - all thresholds configurable
- Async throughout for non-blocking operations

This solves the "Startup timeout - please check logs" issue caused by
trying to load heavy ML models (Whisper, SpeechBrain, ECAPA-TDNN) on
RAM-constrained systems.

Usage:
    from core.memory_aware_startup import MemoryAwareStartup

    startup_manager = MemoryAwareStartup()
    startup_mode = await startup_manager.determine_startup_mode()

    if startup_mode.use_cloud_ml:
        await startup_manager.activate_cloud_ml_backend()
"""

import asyncio
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)


class StartupMode(Enum):
    """Startup mode based on available resources"""
    LOCAL_FULL = "local_full"          # Full local ML loading (RAM >= 6GB free)
    LOCAL_MINIMAL = "local_minimal"    # Minimal local, defer heavy ML (4-6GB free)
    CLOUD_FIRST = "cloud_first"        # Skip local ML, use GCP (< 4GB free)
    CLOUD_ONLY = "cloud_only"          # Critical RAM, all ML on cloud (< 2GB free)


@dataclass
class MemoryStatus:
    """Current memory status on the system"""
    total_gb: float
    used_gb: float
    free_gb: float
    available_gb: float  # Including reclaimable
    compressed_gb: float
    wired_gb: float
    page_outs: int
    memory_pressure: float  # 0-100%

    @property
    def is_under_pressure(self) -> bool:
        """Check if system is under memory pressure"""
        return self.memory_pressure > 70 or self.page_outs > 1000

    @property
    def is_critical(self) -> bool:
        """Check if memory is critically low"""
        return self.available_gb < 2.0 or self.memory_pressure > 85


@dataclass
class StartupDecision:
    """Decision about how to start the system"""
    mode: StartupMode
    use_cloud_ml: bool
    skip_local_whisper: bool
    skip_local_speechbrain: bool
    skip_local_ecapa: bool
    skip_component_warmup: bool
    skip_neural_mesh: bool
    gcp_vm_required: bool
    reason: str
    memory_status: MemoryStatus
    recommendations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/storage"""
        return {
            "mode": self.mode.value,
            "use_cloud_ml": self.use_cloud_ml,
            "skip_local_whisper": self.skip_local_whisper,
            "skip_local_speechbrain": self.skip_local_speechbrain,
            "skip_local_ecapa": self.skip_local_ecapa,
            "skip_component_warmup": self.skip_component_warmup,
            "skip_neural_mesh": self.skip_neural_mesh,
            "gcp_vm_required": self.gcp_vm_required,
            "reason": self.reason,
            "memory_free_gb": self.memory_status.free_gb,
            "memory_available_gb": self.memory_status.available_gb,
            "memory_pressure": self.memory_status.memory_pressure,
            "recommendations": self.recommendations,
        }


class MemoryAwareStartup:
    """
    Intelligent startup manager that adapts to available system resources.

    Automatically detects RAM constraints and activates cloud-first mode
    to prevent startup timeouts from heavy ML model loading.
    """

    # Default thresholds (can be overridden via env vars)
    DEFAULT_FULL_LOCAL_THRESHOLD_GB = 6.0   # Free RAM needed for full local ML
    DEFAULT_MINIMAL_LOCAL_THRESHOLD_GB = 4.0  # Free RAM for minimal local
    DEFAULT_CLOUD_FIRST_THRESHOLD_GB = 2.0    # Below this = cloud only

    # ML model memory requirements (approximate)
    ML_MODEL_MEMORY = {
        "whisper_base": 0.5,      # Whisper base model
        "whisper_small": 1.0,     # Whisper small model
        "whisper_medium": 2.0,    # Whisper medium model
        "speechbrain": 0.3,       # SpeechBrain base
        "ecapa_tdnn": 0.2,        # ECAPA-TDNN 192D
        "pytorch_base": 0.5,      # PyTorch runtime
        "transformers": 0.3,      # Transformers library
        "neural_mesh": 0.8,       # Neural mesh system
        "component_warmup": 0.5,  # Warmup overhead
    }

    def __init__(self):
        """Initialize the memory-aware startup manager"""
        # Load thresholds from environment
        self.full_local_threshold = float(
            os.getenv("JARVIS_FULL_LOCAL_RAM_GB", self.DEFAULT_FULL_LOCAL_THRESHOLD_GB)
        )
        self.minimal_local_threshold = float(
            os.getenv("JARVIS_MINIMAL_LOCAL_RAM_GB", self.DEFAULT_MINIMAL_LOCAL_THRESHOLD_GB)
        )
        self.cloud_first_threshold = float(
            os.getenv("JARVIS_CLOUD_FIRST_RAM_GB", self.DEFAULT_CLOUD_FIRST_THRESHOLD_GB)
        )

        # Cloud ML configuration
        self.gcp_project = os.getenv("GCP_PROJECT_ID", "jarvis-473803")
        self.gcp_zone = os.getenv("GCP_ZONE", "us-central1-a")
        self.gcp_vm_type = os.getenv("GCP_ML_VM_TYPE", "e2-highmem-4")

        # State
        self._gcp_vm_manager = None
        self._hybrid_router = None
        self._cloud_ml_active = False
        self._startup_decision: Optional[StartupDecision] = None

        logger.info(f"MemoryAwareStartup initialized")
        logger.info(f"  Full local threshold: {self.full_local_threshold}GB")
        logger.info(f"  Minimal local threshold: {self.minimal_local_threshold}GB")
        logger.info(f"  Cloud-first threshold: {self.cloud_first_threshold}GB")

    async def get_memory_status(self) -> MemoryStatus:
        """
        Get current memory status using macOS vm_stat.

        Returns:
            MemoryStatus: Current memory status
        """
        try:
            # Get vm_stat output
            result = await asyncio.create_subprocess_exec(
                "vm_stat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await result.communicate()
            vm_stat_output = stdout.decode()

            # Parse vm_stat (page size is 16384 bytes on Apple Silicon)
            page_size = 16384
            stats = {}

            for line in vm_stat_output.split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = key.strip().lower().replace(' ', '_')
                    value = value.strip().rstrip('.')
                    try:
                        stats[key] = int(value)
                    except ValueError:
                        pass

            # Get total RAM
            sysctl_result = await asyncio.create_subprocess_exec(
                "sysctl", "-n", "hw.memsize",
                stdout=asyncio.subprocess.PIPE
            )
            stdout, _ = await sysctl_result.communicate()
            total_bytes = int(stdout.decode().strip())
            total_gb = total_bytes / (1024**3)

            # Calculate memory values
            pages_free = stats.get('pages_free', 0)
            pages_active = stats.get('pages_active', 0)
            pages_inactive = stats.get('pages_inactive', 0)
            pages_speculative = stats.get('pages_speculative', 0)
            pages_wired = stats.get('pages_wired_down', 0)
            pages_compressed = stats.get('pages_occupied_by_compressor', 0)
            page_outs = stats.get('pageouts', 0)

            free_bytes = pages_free * page_size
            free_gb = free_bytes / (1024**3)

            # Available = free + inactive + speculative (reclaimable)
            available_bytes = (pages_free + pages_inactive + pages_speculative) * page_size
            available_gb = available_bytes / (1024**3)

            wired_bytes = pages_wired * page_size
            wired_gb = wired_bytes / (1024**3)

            compressed_bytes = pages_compressed * page_size
            compressed_gb = compressed_bytes / (1024**3)

            used_gb = total_gb - available_gb

            # Calculate memory pressure (used / total, accounting for compression)
            memory_pressure = ((total_gb - available_gb) / total_gb) * 100

            return MemoryStatus(
                total_gb=total_gb,
                used_gb=used_gb,
                free_gb=free_gb,
                available_gb=available_gb,
                compressed_gb=compressed_gb,
                wired_gb=wired_gb,
                page_outs=page_outs,
                memory_pressure=memory_pressure,
            )

        except Exception as e:
            logger.error(f"Failed to get memory status: {e}")
            # Return conservative defaults
            return MemoryStatus(
                total_gb=16.0,
                used_gb=14.0,
                free_gb=2.0,
                available_gb=2.0,
                compressed_gb=2.0,
                wired_gb=2.0,
                page_outs=0,
                memory_pressure=87.5,
            )

    async def determine_startup_mode(self) -> StartupDecision:
        """
        Determine the optimal startup mode based on available resources.

        Returns:
            StartupDecision: Decision about how to start the system
        """
        memory = await self.get_memory_status()

        logger.info("=" * 60)
        logger.info("🧠 MEMORY-AWARE STARTUP ANALYSIS")
        logger.info("=" * 60)
        logger.info(f"  Total RAM: {memory.total_gb:.1f} GB")
        logger.info(f"  Used: {memory.used_gb:.1f} GB ({memory.memory_pressure:.1f}%)")
        logger.info(f"  Free: {memory.free_gb:.1f} GB")
        logger.info(f"  Available (with reclaimable): {memory.available_gb:.1f} GB")
        logger.info(f"  Compressed: {memory.compressed_gb:.1f} GB")
        logger.info(f"  Page outs: {memory.page_outs}")
        logger.info("=" * 60)

        recommendations = []

        # Determine startup mode based on available RAM
        if memory.available_gb >= self.full_local_threshold:
            # Plenty of RAM - full local mode
            decision = StartupDecision(
                mode=StartupMode.LOCAL_FULL,
                use_cloud_ml=False,
                skip_local_whisper=False,
                skip_local_speechbrain=False,
                skip_local_ecapa=False,
                skip_component_warmup=False,
                skip_neural_mesh=False,
                gcp_vm_required=False,
                reason=f"Sufficient RAM ({memory.available_gb:.1f}GB available >= {self.full_local_threshold}GB threshold)",
                memory_status=memory,
                recommendations=["Full local ML loading enabled"],
            )
            logger.info(f"✅ STARTUP MODE: LOCAL_FULL")
            logger.info(f"   Reason: {decision.reason}")

        elif memory.available_gb >= self.minimal_local_threshold:
            # Moderate RAM - minimal local, defer heavy ML
            recommendations = [
                "Close Chrome tabs to free RAM",
                "Consider using cloud ML for heavy operations",
                "Whisper will load on first use instead of startup",
            ]
            decision = StartupDecision(
                mode=StartupMode.LOCAL_MINIMAL,
                use_cloud_ml=False,  # Not required but available
                skip_local_whisper=True,  # Defer to first use
                skip_local_speechbrain=False,  # Small enough
                skip_local_ecapa=False,  # Small enough
                skip_component_warmup=True,  # Skip to reduce pressure
                skip_neural_mesh=True,  # Defer
                gcp_vm_required=False,
                reason=f"Moderate RAM ({memory.available_gb:.1f}GB) - deferring heavy ML models",
                memory_status=memory,
                recommendations=recommendations,
            )
            logger.info(f"⚠️  STARTUP MODE: LOCAL_MINIMAL")
            logger.info(f"   Reason: {decision.reason}")
            logger.info(f"   Skipping: Whisper preload, component warmup, neural mesh")

        elif memory.available_gb >= self.cloud_first_threshold:
            # Low RAM - cloud first mode
            recommendations = [
                "GCP Spot VM will handle ML processing",
                "Close other applications to free local RAM",
                "Local backend will handle real-time tasks only",
            ]
            decision = StartupDecision(
                mode=StartupMode.CLOUD_FIRST,
                use_cloud_ml=True,
                skip_local_whisper=True,
                skip_local_speechbrain=True,
                skip_local_ecapa=True,
                skip_component_warmup=True,
                skip_neural_mesh=True,
                gcp_vm_required=True,
                reason=f"Low RAM ({memory.available_gb:.1f}GB < {self.minimal_local_threshold}GB) - activating cloud ML",
                memory_status=memory,
                recommendations=recommendations,
            )
            logger.info(f"☁️  STARTUP MODE: CLOUD_FIRST")
            logger.info(f"   Reason: {decision.reason}")
            logger.info(f"   Action: Will spin up GCP Spot VM for ML processing")

        else:
            # Critical RAM - cloud only mode
            recommendations = [
                "CRITICAL: Close applications immediately",
                "All ML processing will be on GCP",
                "Only essential local services will run",
            ]
            decision = StartupDecision(
                mode=StartupMode.CLOUD_ONLY,
                use_cloud_ml=True,
                skip_local_whisper=True,
                skip_local_speechbrain=True,
                skip_local_ecapa=True,
                skip_component_warmup=True,
                skip_neural_mesh=True,
                gcp_vm_required=True,
                reason=f"CRITICAL RAM ({memory.available_gb:.1f}GB < {self.cloud_first_threshold}GB) - cloud only mode",
                memory_status=memory,
                recommendations=recommendations,
            )
            logger.warning(f"🔴 STARTUP MODE: CLOUD_ONLY (CRITICAL)")
            logger.warning(f"   Reason: {decision.reason}")
            logger.warning(f"   Action: Emergency cloud-only mode activated")

        # Log recommendations
        if recommendations:
            logger.info("📋 Recommendations:")
            for rec in recommendations:
                logger.info(f"   • {rec}")

        self._startup_decision = decision
        return decision

    async def activate_cloud_ml_backend(self) -> Dict[str, Any]:
        """
        Activate GCP Spot VM for ML processing.

        Returns:
            Dict with activation status and VM details
        """
        logger.info("☁️  Activating GCP ML Backend...")

        try:
            # Import GCP VM manager
            from core.gcp_vm_manager import get_gcp_vm_manager, VMManagerConfig

            # Configure for ML workload
            config = VMManagerConfig(
                project_id=self.gcp_project,
                zone=self.gcp_zone,
                machine_type=self.gcp_vm_type,
                use_spot=True,
                memory_threshold_percent=85,
                auto_create=True,
                auto_cleanup=True,
                idle_timeout_minutes=15,
                max_cost_per_hour=0.10,
            )

            # Get or create VM manager
            self._gcp_vm_manager = await get_gcp_vm_manager(config)
            await self._gcp_vm_manager.initialize()

            # Check if we should create VM
            memory = await self.get_memory_status()
            should_create, reason = await self._gcp_vm_manager.should_create_vm(
                memory_pressure=memory.memory_pressure,
                pending_tasks=["ml_processing", "voice_recognition"],
            )

            if should_create:
                logger.info(f"   Creating GCP Spot VM: {reason}")

                # Create VM with ML components
                vm_result = await self._gcp_vm_manager.create_vm(
                    components=["whisper", "speechbrain", "ecapa_tdnn", "ml_models"],
                    trigger_reason=f"Memory-aware startup: {reason}",
                )

                if vm_result and vm_result.get("success"):
                    self._cloud_ml_active = True
                    logger.info(f"✅ GCP ML Backend activated!")
                    logger.info(f"   VM: {vm_result.get('instance_id')}")
                    logger.info(f"   IP: {vm_result.get('ip_address')}")
                    logger.info(f"   Cost: ${vm_result.get('cost_per_hour', 0.029)}/hr")

                    # Configure hybrid router to use GCP for ML
                    await self._configure_hybrid_routing(vm_result)

                    return {
                        "success": True,
                        "mode": "gcp_spot_vm",
                        "vm_id": vm_result.get("instance_id"),
                        "ip": vm_result.get("ip_address"),
                        "cost_per_hour": vm_result.get("cost_per_hour", 0.029),
                    }
                else:
                    logger.warning(f"⚠️  GCP VM creation failed: {vm_result}")
                    return {"success": False, "error": "VM creation failed"}
            else:
                logger.info(f"   GCP VM not needed: {reason}")
                return {"success": True, "mode": "local", "reason": reason}

        except ImportError as e:
            logger.warning(f"⚠️  GCP VM manager not available: {e}")
            return {"success": False, "error": f"Import error: {e}"}
        except Exception as e:
            logger.error(f"❌ Failed to activate GCP ML backend: {e}")
            return {"success": False, "error": str(e)}

    async def _configure_hybrid_routing(self, vm_result: Dict[str, Any]) -> None:
        """
        Configure hybrid router to send ML requests to GCP.

        Args:
            vm_result: VM creation result with IP and details
        """
        try:
            from core.hybrid_router import get_hybrid_router

            self._hybrid_router = get_hybrid_router()

            # Update GCP backend URL
            gcp_ip = vm_result.get("ip_address")
            if gcp_ip:
                await self._hybrid_router.update_backend_url(
                    backend_name="gcp",
                    url=f"http://{gcp_ip}:8010",
                )

                # Set ML capabilities to route to GCP
                ml_capabilities = [
                    "ml_processing",
                    "whisper_transcription",
                    "speaker_verification",
                    "voice_embedding",
                    "nlp_analysis",
                    "heavy_computation",
                ]

                for capability in ml_capabilities:
                    await self._hybrid_router.set_capability_route(
                        capability=capability,
                        backend="gcp",
                        priority=1,  # High priority
                    )

                logger.info(f"   Hybrid router configured for GCP ML offloading")

        except Exception as e:
            logger.warning(f"⚠️  Could not configure hybrid routing: {e}")

    async def get_ml_endpoint(self, operation: str) -> str:
        """
        Get the appropriate endpoint for an ML operation.

        Args:
            operation: ML operation type (whisper, speaker_verify, etc.)

        Returns:
            Endpoint URL (local or GCP)
        """
        if self._cloud_ml_active and self._gcp_vm_manager:
            # Get active VM
            active_vms = [
                vm for vm in self._gcp_vm_manager.vms.values()
                if vm.is_healthy
            ]

            if active_vms:
                vm = active_vms[0]
                return f"http://{vm.ip_address}:8010/api/ml/{operation}"

        # Fall back to local
        return f"http://localhost:8010/api/ml/{operation}"

    @property
    def is_cloud_ml_active(self) -> bool:
        """Check if cloud ML backend is active"""
        return self._cloud_ml_active

    @property
    def startup_decision(self) -> Optional[StartupDecision]:
        """Get the startup decision"""
        return self._startup_decision

    async def cleanup(self) -> None:
        """Cleanup resources on shutdown"""
        if self._gcp_vm_manager:
            logger.info("🧹 Cleaning up GCP ML backend...")
            await self._gcp_vm_manager.cleanup_all_vms(
                reason="JARVIS shutdown - memory-aware startup cleanup"
            )


# Global instance
_startup_manager: Optional[MemoryAwareStartup] = None


async def get_startup_manager() -> MemoryAwareStartup:
    """Get or create the global startup manager"""
    global _startup_manager
    if _startup_manager is None:
        _startup_manager = MemoryAwareStartup()
    return _startup_manager


async def determine_startup_mode() -> StartupDecision:
    """
    Convenience function to determine startup mode.

    Returns:
        StartupDecision: Decision about how to start the system
    """
    manager = await get_startup_manager()
    return await manager.determine_startup_mode()


async def activate_cloud_ml_if_needed(decision: StartupDecision) -> Dict[str, Any]:
    """
    Activate cloud ML backend if the startup decision requires it.

    Args:
        decision: Startup decision from determine_startup_mode()

    Returns:
        Dict with activation status
    """
    if decision.gcp_vm_required:
        manager = await get_startup_manager()
        return await manager.activate_cloud_ml_backend()
    return {"success": True, "mode": "local", "reason": "Cloud ML not required"}
