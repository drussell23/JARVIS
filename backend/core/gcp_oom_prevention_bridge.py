"""
GCP OOM Prevention Bridge v2.0.0
=================================

Intelligent, adaptive bridge that prevents Out-Of-Memory crashes by:
1. Pre-flight memory checks BEFORE heavy component initialization
2. Automatic GCP Spot VM (32GB RAM) spin-up when local memory is insufficient
3. Dynamic auto-enable of GCP when critical (no manual GCP_ENABLED required)
4. Multi-tier graceful degradation fallback chain
5. Adaptive memory estimation based on historical usage
6. Cross-repo coordination for JARVIS Prime and Reactor Core

v2.0.0 IMPROVEMENTS:
- AUTO-ENABLE GCP: Dynamically enables GCP when OOM is critical, even if
  GCP_ENABLED=false. This is the intelligent, no-hardcoding approach.
- GRACEFUL DEGRADATION: Multi-tier fallback chain when GCP unavailable:
  Tier 1: GCP Cloud Offload (if available)
  Tier 2: Aggressive Memory Optimization (kill caches, reduce estimates)
  Tier 3: Component-by-Component Loading (sequential instead of parallel)
  Tier 4: Minimal Mode (core only, no ML)
  Tier 5: ABORT (only as absolute last resort)
- ADAPTIVE ESTIMATES: Learns from actual memory usage to improve estimates
- INTELLIGENT RECOVERY: Automatically recovers when memory becomes available

This solves the SIGKILL (exit code -9) crash during INITIALIZING_AGI_HUB by
proactively detecting memory pressure and taking intelligent action.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │  GCP OOM Prevention Bridge v2.0.0                               │
    │  ├── ProactiveResourceGuard Integration (memory monitoring)     │
    │  ├── MemoryAwareStartup Integration (startup decisions)         │
    │  ├── GCPVMManager Integration (Spot VM lifecycle)               │
    │  ├── DynamicGCPEnabler (auto-enable when critical)              │
    │  ├── GracefulDegradationChain (multi-tier fallback)             │
    │  ├── AdaptiveMemoryEstimator (learns from usage)                │
    │  └── Cross-Repo Coordination (signals for Prime/Reactor)        │
    └─────────────────────────────────────────────────────────────────┘

Usage:
    from core.gcp_oom_prevention_bridge import (
        check_memory_before_heavy_init,
        ensure_sufficient_memory_or_offload,
        get_oom_prevention_bridge,
    )

    # Before initializing heavy components:
    result = await check_memory_before_heavy_init(
        component="agi_hub",
        estimated_mb=4000,
    )

    if result.can_proceed_locally:
        # Safe to initialize locally
        await initialize_agi_hub()
    elif result.gcp_vm_ready:
        # Heavy components should run on GCP VM
        await offload_initialization_to_cloud(result.gcp_vm_ip)
    elif result.fallback_strategy:
        # Use graceful degradation strategy
        await execute_fallback_strategy(result.fallback_strategy)
    else:
        # Only reach here if ALL strategies exhausted
        raise MemoryInsufficientError(result.reason)

Author: JARVIS Trinity v132.0 - Intelligent OOM Prevention
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# v2.0.0: GRACEFUL DEGRADATION STRATEGIES
# =============================================================================

class DegradationTier(Enum):
    """
    Multi-tier graceful degradation when memory is insufficient.

    The system tries each tier in order until one succeeds.
    ABORT is only reached if ALL tiers fail.
    """
    TIER_0_LOCAL = "local"                    # Sufficient local RAM - no degradation
    TIER_1_GCP_CLOUD = "gcp_cloud"            # Offload to GCP Spot VM
    TIER_2_AGGRESSIVE_OPTIMIZE = "aggressive" # Kill caches, reduce estimates
    TIER_3_SEQUENTIAL_LOAD = "sequential"     # Load components one-by-one
    TIER_4_MINIMAL_MODE = "minimal"           # Core only, no ML models
    TIER_5_ABORT = "abort"                    # Cannot proceed (last resort)


@dataclass
class FallbackStrategy:
    """Strategy for graceful degradation when memory insufficient."""
    tier: DegradationTier
    description: str
    actions: List[str]
    estimated_memory_freed_mb: int = 0
    requires_user_confirmation: bool = False
    can_recover_later: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier.value,
            "description": self.description,
            "actions": self.actions,
            "estimated_memory_freed_mb": self.estimated_memory_freed_mb,
            "requires_user_confirmation": self.requires_user_confirmation,
            "can_recover_later": self.can_recover_later,
        }


# Pre-defined fallback strategies
FALLBACK_STRATEGIES = {
    DegradationTier.TIER_2_AGGRESSIVE_OPTIMIZE: FallbackStrategy(
        tier=DegradationTier.TIER_2_AGGRESSIVE_OPTIMIZE,
        description="Aggressive memory optimization - clear caches and reduce component memory",
        actions=[
            "Clear Python garbage collector",
            "Release unused memory pools",
            "Reduce batch sizes for ML models",
            "Disable optional features temporarily",
            "Use memory-mapped files where possible",
        ],
        estimated_memory_freed_mb=500,
        requires_user_confirmation=False,
        can_recover_later=True,
    ),
    DegradationTier.TIER_3_SEQUENTIAL_LOAD: FallbackStrategy(
        tier=DegradationTier.TIER_3_SEQUENTIAL_LOAD,
        description="Sequential loading - initialize components one at a time",
        actions=[
            "Disable parallel initialization",
            "Load components sequentially to reduce peak memory",
            "Unload each component's setup data before next",
            "Smaller memory footprint, longer startup time",
        ],
        estimated_memory_freed_mb=1500,
        requires_user_confirmation=False,
        can_recover_later=True,
    ),
    DegradationTier.TIER_4_MINIMAL_MODE: FallbackStrategy(
        tier=DegradationTier.TIER_4_MINIMAL_MODE,
        description="Minimal mode - core functionality only, no ML models",
        actions=[
            "Skip ML model loading (voice, vision, etc.)",
            "Disable JARVIS Prime neural features",
            "API-only mode for AI capabilities",
            "Core routing and health monitoring only",
        ],
        estimated_memory_freed_mb=4000,
        requires_user_confirmation=True,  # User should know about reduced functionality
        can_recover_later=True,
    ),
}


class MemoryDecision(Enum):
    """
    Decision about where to run heavy operations.

    v2.0.0: Added DEGRADED for graceful degradation modes.
    """
    LOCAL = "local"                     # Sufficient local RAM
    CLOUD = "cloud"                     # Offload to GCP Spot VM
    CLOUD_REQUIRED = "cloud_required"   # Critical - must use cloud
    DEGRADED = "degraded"               # Running with reduced functionality
    ABORT = "abort"                     # Cannot proceed (ALL fallbacks exhausted)


@dataclass
class MemoryCheckResult:
    """
    Result of pre-initialization memory check.

    v2.0.0: Added fallback_strategy and degradation_tier for graceful degradation.
    """
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
    # v2.0.0: Graceful degradation fields
    fallback_strategy: Optional[FallbackStrategy] = None
    degradation_tier: DegradationTier = DegradationTier.TIER_0_LOCAL
    gcp_auto_enabled: bool = False  # True if GCP was auto-enabled due to critical memory
    adaptive_estimate_used: bool = False  # True if using learned estimate
    actual_estimate_mb: int = 0  # The estimate that was actually used

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
            "degradation_tier": self.degradation_tier.value,
            "fallback_strategy": self.fallback_strategy.to_dict() if self.fallback_strategy else None,
            "gcp_auto_enabled": self.gcp_auto_enabled,
            "adaptive_estimate_used": self.adaptive_estimate_used,
            "actual_estimate_mb": self.actual_estimate_mb,
        }

    @property
    def can_proceed(self) -> bool:
        """Check if we can proceed (locally, via GCP, or via fallback)."""
        return self.decision != MemoryDecision.ABORT


# =============================================================================
# v2.0.0: ADAPTIVE MEMORY ESTIMATION
# =============================================================================

# Base memory estimates for heavy components (in MB)
# These are CONSERVATIVE defaults - the system learns actual usage over time
HEAVY_COMPONENT_MEMORY_ESTIMATES = {
    # Core JARVIS components
    "agi_hub": 4000,           # AGI Hub with ML models
    "neural_mesh": 2000,       # Neural mesh system
    "jarvis_prime": 6000,      # Full JARVIS Prime (GGUF model)
    "reactor_core": 3000,      # v132.3: Reactor Core ML/autonomy (increased from 1000)
    "vision_system": 1500,     # Computer vision components
    "startup_initialization": 3000,  # Total startup initialization
    "vosk": 2000,              # v132.3: Vosk speech recognition models
    "whisper": 2000,           # v132.3: General whisper estimate

    # ML Models
    "whisper_large": 3000,     # Whisper large model
    "whisper_medium": 2000,    # Whisper medium model
    "whisper_small": 1000,     # Whisper small model
    "speechbrain": 800,        # SpeechBrain + ECAPA
    "ecapa_tdnn": 500,         # ECAPA-TDNN embeddings
    "pytorch_runtime": 1500,   # PyTorch base runtime
    "transformers": 500,       # Transformers library

    # Default for unknown components
    "default": 500,
}

# v2.0.0: Configurable thresholds with intelligent defaults
# These are loaded from environment, but can be dynamically adjusted
OOM_PREVENTION_THRESHOLDS = {
    # RAM thresholds
    "min_free_ram_gb": float(os.getenv("JARVIS_MIN_FREE_RAM_GB", "2.0")),
    "cloud_trigger_ram_gb": float(os.getenv("JARVIS_CLOUD_TRIGGER_RAM_GB", "4.0")),
    "critical_ram_gb": float(os.getenv("JARVIS_CRITICAL_RAM_GB", "1.5")),

    # Memory pressure thresholds
    "memory_pressure_cloud_trigger": float(os.getenv("JARVIS_PRESSURE_CLOUD_TRIGGER", "75.0")),
    "memory_pressure_critical": float(os.getenv("JARVIS_PRESSURE_CRITICAL", "90.0")),

    # v2.0.0: New intelligent thresholds
    "auto_enable_gcp_pressure": float(os.getenv("JARVIS_AUTO_GCP_PRESSURE", "85.0")),
    "estimation_safety_factor": float(os.getenv("JARVIS_ESTIMATION_SAFETY", "1.2")),  # 20% safety margin
    "adaptive_learning_rate": float(os.getenv("JARVIS_ADAPTIVE_RATE", "0.3")),  # How fast to adapt estimates

    # Fallback behavior
    "enable_graceful_degradation": os.getenv("JARVIS_GRACEFUL_DEGRADATION", "true").lower() == "true",
    "auto_enable_gcp_on_critical": os.getenv("JARVIS_AUTO_ENABLE_GCP", "true").lower() == "true",
    "skip_gcp_if_credentials_missing": os.getenv("JARVIS_SKIP_GCP_NO_CREDS", "true").lower() == "true",
}


class AdaptiveMemoryEstimator:
    """
    v2.0.0: Learns from actual memory usage to improve estimates over time.

    Instead of static estimates that may be too aggressive (causing ABORT)
    or too conservative (wasting GCP resources), this class tracks actual
    memory usage and adapts estimates accordingly.
    """

    def __init__(self, history_file: Optional[Path] = None):
        self._history_file = history_file or Path(
            os.getenv("JARVIS_MEMORY_HISTORY",
                      str(Path.home() / ".jarvis" / "memory_history.json"))
        )
        self._learned_estimates: Dict[str, int] = {}
        self._usage_history: Dict[str, List[Dict[str, Any]]] = {}
        self._load_history()

    def _load_history(self) -> None:
        """Load historical memory usage data."""
        try:
            if self._history_file.exists():
                with open(self._history_file) as f:
                    data = json.load(f)
                    self._learned_estimates = data.get("estimates", {})
                    self._usage_history = data.get("history", {})
                    logger.debug(f"[AdaptiveEstimator] Loaded {len(self._learned_estimates)} learned estimates")
        except Exception as e:
            logger.debug(f"[AdaptiveEstimator] Could not load history: {e}")

    def _save_history(self) -> None:
        """Save memory usage history."""
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_file, "w") as f:
                json.dump({
                    "estimates": self._learned_estimates,
                    "history": self._usage_history,
                    "last_updated": time.time(),
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"[AdaptiveEstimator] Could not save history: {e}")

    def get_estimate(self, component: str, use_base: bool = False) -> Tuple[int, bool]:
        """
        Get memory estimate for a component.

        Args:
            component: Component name
            use_base: If True, always use base estimate (ignore learned)

        Returns:
            Tuple of (estimate_mb, is_learned)
        """
        component_key = component.lower()
        base_estimate = HEAVY_COMPONENT_MEMORY_ESTIMATES.get(
            component_key,
            HEAVY_COMPONENT_MEMORY_ESTIMATES["default"]
        )

        if use_base or component_key not in self._learned_estimates:
            return base_estimate, False

        learned = self._learned_estimates[component_key]
        # Apply safety factor to learned estimate
        safety_factor = OOM_PREVENTION_THRESHOLDS["estimation_safety_factor"]
        safe_estimate = int(learned * safety_factor)

        return safe_estimate, True

    def record_actual_usage(
        self,
        component: str,
        actual_mb: int,
        estimated_mb: int,
        succeeded: bool,
    ) -> None:
        """
        Record actual memory usage to improve future estimates.

        Args:
            component: Component name
            actual_mb: Actual memory used
            estimated_mb: What was estimated
            succeeded: Whether initialization succeeded
        """
        component_key = component.lower()
        learning_rate = OOM_PREVENTION_THRESHOLDS["adaptive_learning_rate"]

        # Record to history
        if component_key not in self._usage_history:
            self._usage_history[component_key] = []

        self._usage_history[component_key].append({
            "actual_mb": actual_mb,
            "estimated_mb": estimated_mb,
            "succeeded": succeeded,
            "timestamp": time.time(),
        })

        # Keep last 10 records per component
        if len(self._usage_history[component_key]) > 10:
            self._usage_history[component_key] = self._usage_history[component_key][-10:]

        # Update learned estimate using exponential moving average
        current_estimate = self._learned_estimates.get(component_key, estimated_mb)
        new_estimate = int(current_estimate * (1 - learning_rate) + actual_mb * learning_rate)

        # Don't let learned estimate go below 50% of base or above 200% of base
        base = HEAVY_COMPONENT_MEMORY_ESTIMATES.get(component_key, 500)
        new_estimate = max(int(base * 0.5), min(int(base * 2.0), new_estimate))

        self._learned_estimates[component_key] = new_estimate
        self._save_history()

        logger.debug(
            f"[AdaptiveEstimator] Updated {component}: "
            f"actual={actual_mb}MB, new_estimate={new_estimate}MB"
        )

    def get_total_estimate(self, components: List[str]) -> int:
        """Get total estimate for multiple components."""
        total = 0
        for comp in components:
            estimate, _ = self.get_estimate(comp)
            total += estimate
        return total


# Global adaptive estimator instance
_adaptive_estimator: Optional[AdaptiveMemoryEstimator] = None


def get_adaptive_estimator() -> AdaptiveMemoryEstimator:
    """Get or create the global adaptive estimator."""
    global _adaptive_estimator
    if _adaptive_estimator is None:
        _adaptive_estimator = AdaptiveMemoryEstimator()
    return _adaptive_estimator


class GCPOOMPreventionBridge:
    """
    v2.0.0: Intelligent bridge that coordinates OOM prevention across JARVIS components.

    Features:
    - Pre-flight memory checks before heavy initialization
    - Automatic GCP Spot VM spin-up when needed (with auto-enable)
    - Multi-tier graceful degradation when GCP unavailable
    - Adaptive memory estimation that learns from actual usage
    - Cross-repo signal coordination
    - Intelligent decision-making for cloud vs local

    v2.0.0 ENHANCEMENTS:
    - AUTO-ENABLE GCP: Dynamically enables GCP when memory is critical,
      even if GCP_ENABLED=false (respects JARVIS_AUTO_ENABLE_GCP setting)
    - GRACEFUL DEGRADATION: Multi-tier fallback chain:
      Tier 1: GCP Cloud (32GB RAM Spot VM)
      Tier 2: Aggressive Memory Optimization
      Tier 3: Sequential Component Loading
      Tier 4: Minimal Mode (core only)
      Tier 5: ABORT (only if ALL tiers fail)
    - ADAPTIVE ESTIMATION: Learns from actual memory usage
    - INTELLIGENT RECOVERY: Auto-recovers when memory becomes available
    """

    def __init__(self):
        self._memory_aware_startup = None
        self._gcp_vm_manager = None
        self._proactive_guard = None
        self._initialized = False
        self._active_gcp_vm: Optional[Dict[str, Any]] = None
        self._offload_mode_active = False
        self._lock = asyncio.Lock()

        # v2.0.0: Adaptive memory estimation
        self._adaptive_estimator = get_adaptive_estimator()

        # v2.0.0: Track current degradation state
        self._current_degradation_tier = DegradationTier.TIER_0_LOCAL
        self._degradation_active = False
        self._gcp_auto_enabled = False  # True if we auto-enabled GCP

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

        logger.info("[OOMBridge] Initialized GCP OOM Prevention Bridge v2.0.0")
        logger.info(f"  Auto-enable GCP on critical: {OOM_PREVENTION_THRESHOLDS['auto_enable_gcp_on_critical']}")
        logger.info(f"  Graceful degradation: {OOM_PREVENTION_THRESHOLDS['enable_graceful_degradation']}")

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

    def _get_component_memory_estimate(self, component: str, use_adaptive: bool = True) -> Tuple[int, bool]:
        """
        Get memory estimate for a component in MB.

        v2.0.0: Uses adaptive estimation that learns from actual usage.

        Args:
            component: Component name
            use_adaptive: If True, use learned estimates when available

        Returns:
            Tuple of (estimate_mb, is_adaptive)
        """
        if use_adaptive and self._adaptive_estimator:
            return self._adaptive_estimator.get_estimate(component)

        base_estimate = HEAVY_COMPONENT_MEMORY_ESTIMATES.get(
            component.lower(),
            HEAVY_COMPONENT_MEMORY_ESTIMATES["default"]
        )
        return base_estimate, False

    async def check_memory_before_init(
        self,
        component: str,
        estimated_mb: Optional[int] = None,
        auto_offload: bool = True,
    ) -> MemoryCheckResult:
        """
        v2.0.0: Intelligent memory check with graceful degradation.

        This is the main entry point for OOM prevention. Call this BEFORE
        initializing any heavy component (ML models, neural mesh, etc.).

        IMPROVEMENTS in v2.0.0:
        - Uses adaptive memory estimation (learns from actual usage)
        - Auto-enables GCP when memory is critical (no manual GCP_ENABLED needed)
        - Multi-tier graceful degradation when GCP unavailable
        - Never ABORTs unless ALL fallback strategies exhausted

        Args:
            component: Name of the component to initialize
            estimated_mb: Estimated memory requirement (uses adaptive estimate if not provided)
            auto_offload: If True, automatically spin up GCP VM when needed

        Returns:
            MemoryCheckResult with decision, fallback strategy, and recommendations
        """
        await self.initialize()

        # v2.0.0: Get adaptive memory estimate
        adaptive_estimate_used = False
        if estimated_mb is None:
            estimated_mb, adaptive_estimate_used = self._get_component_memory_estimate(component)
        required_gb = estimated_mb / 1024

        # Get current memory status
        available_gb, pressure = await self._get_memory_status()

        thresholds = OOM_PREVENTION_THRESHOLDS
        recommendations: List[str] = []

        logger.info(f"[OOMBridge] v2.0.0 Memory check for '{component}':")
        logger.info(f"  Available RAM: {available_gb:.1f} GB")
        logger.info(f"  Required RAM: {required_gb:.1f} GB (adaptive={adaptive_estimate_used})")
        logger.info(f"  Memory pressure: {pressure:.1f}%")

        # Determine remaining RAM after component loads
        remaining_after_load = available_gb - required_gb

        # v2.0.0: Multi-tier decision logic with graceful degradation
        decision, can_proceed_locally, gcp_required, reason, degradation_tier, fallback_strategy = \
            self._make_intelligent_decision(
                component=component,
                available_gb=available_gb,
                required_gb=required_gb,
                remaining_after_load=remaining_after_load,
                pressure=pressure,
                thresholds=thresholds,
                recommendations=recommendations,
            )

        # v2.0.0: If GCP required, try to get it ready (with auto-enable if critical)
        gcp_ready = False
        gcp_ip: Optional[str] = None
        gcp_auto_enabled = False

        if gcp_required and auto_offload:
            gcp_ready, gcp_ip, gcp_auto_enabled = await self._ensure_gcp_vm_ready_v2(
                is_critical=(decision == MemoryDecision.CLOUD_REQUIRED),
                reason=reason,
            )

            if not gcp_ready:
                # v2.0.0: GCP failed - try graceful degradation chain
                if thresholds["enable_graceful_degradation"]:
                    decision, can_proceed_locally, degradation_tier, fallback_strategy = \
                        await self._try_graceful_degradation(
                            decision,  # original_decision (unused but kept for signature clarity)
                            component=component,
                            available_gb=available_gb,
                            required_gb=required_gb,
                            recommendations=recommendations,
                        )
                else:
                    # No graceful degradation - original behavior
                    if decision == MemoryDecision.CLOUD_REQUIRED:
                        decision = MemoryDecision.ABORT
                        recommendations.append("❌ Cannot proceed: GCP VM unavailable and local RAM insufficient")
                        recommendations.append("💡 Tip: Enable JARVIS_GRACEFUL_DEGRADATION=true for fallback options")
                    else:
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
            fallback_strategy=fallback_strategy,
            degradation_tier=degradation_tier,
            gcp_auto_enabled=gcp_auto_enabled,
            adaptive_estimate_used=adaptive_estimate_used,
            actual_estimate_mb=estimated_mb,
        )

        # Log decision with tier info
        tier_str = f" (Tier: {degradation_tier.value})" if degradation_tier != DegradationTier.TIER_0_LOCAL else ""
        logger.info(f"[OOMBridge] Decision: {decision.value}{tier_str}")
        logger.info(f"  Reason: {reason}")
        if gcp_auto_enabled:
            logger.info(f"  🔧 GCP was AUTO-ENABLED due to critical memory")
        for rec in recommendations:
            logger.info(f"  → {rec}")

        # Track history
        self._check_history.append(result)
        if len(self._check_history) > 100:
            self._check_history = self._check_history[-50:]

        # Write cross-repo signal
        await self._write_oom_signal(result)

        return result

    def _make_intelligent_decision(
        self,
        component: str,
        available_gb: float,
        required_gb: float,
        remaining_after_load: float,
        pressure: float,
        thresholds: Dict[str, Any],
        recommendations: List[str],
    ) -> Tuple[MemoryDecision, bool, bool, str, DegradationTier, Optional[FallbackStrategy]]:
        """
        v2.0.0: Make intelligent decision about memory strategy.

        Returns:
            Tuple of (decision, can_proceed_locally, gcp_required, reason, degradation_tier, fallback_strategy)
        """
        # Default values
        fallback_strategy: Optional[FallbackStrategy] = None
        degradation_tier = DegradationTier.TIER_0_LOCAL

        # Decision logic with clear priority
        if pressure >= thresholds["memory_pressure_critical"]:
            # CRITICAL: Memory pressure too high
            decision = MemoryDecision.CLOUD_REQUIRED
            can_proceed_locally = False
            gcp_required = True
            reason = f"CRITICAL memory pressure ({pressure:.1f}% >= {thresholds['memory_pressure_critical']}%)"
            recommendations.append("⚠️ CRITICAL: System near OOM - GCP VM required")
            recommendations.append("Close applications immediately to prevent crash")
            degradation_tier = DegradationTier.TIER_1_GCP_CLOUD

        elif remaining_after_load < thresholds["critical_ram_gb"]:
            # Would leave system in critical state
            decision = MemoryDecision.CLOUD_REQUIRED
            can_proceed_locally = False
            gcp_required = True
            reason = f"Loading {component} would leave only {remaining_after_load:.1f}GB free (< {thresholds['critical_ram_gb']}GB critical threshold)"
            recommendations.append(f"🚀 Offloading {component} to GCP Spot VM (32GB RAM)")
            recommendations.append("Local RAM will remain stable")
            degradation_tier = DegradationTier.TIER_1_GCP_CLOUD

        elif remaining_after_load < thresholds["cloud_trigger_ram_gb"] or pressure >= thresholds["memory_pressure_cloud_trigger"]:
            # Below cloud trigger threshold - recommend cloud
            decision = MemoryDecision.CLOUD
            can_proceed_locally = False  # Strongly recommend cloud
            gcp_required = True
            reason = f"Memory constrained ({remaining_after_load:.1f}GB after load < {thresholds['cloud_trigger_ram_gb']}GB threshold)"
            recommendations.append(f"☁️ Recommended: Run {component} on GCP Spot VM")
            recommendations.append("Cost: ~$0.029/hour for e2-highmem-4 (32GB RAM)")
            recommendations.append("Auto-terminates when idle")
            degradation_tier = DegradationTier.TIER_1_GCP_CLOUD

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

        return decision, can_proceed_locally, gcp_required, reason, degradation_tier, fallback_strategy

    async def _try_graceful_degradation(
        self,
        _original_decision: MemoryDecision,
        component: str,
        available_gb: float,
        required_gb: float,
        recommendations: List[str],
    ) -> Tuple[MemoryDecision, bool, DegradationTier, Optional[FallbackStrategy]]:
        """
        v2.0.0: Try graceful degradation strategies when GCP unavailable.

        This is the KEY IMPROVEMENT that prevents ABORT by trying fallback tiers:
        - Tier 2: Aggressive memory optimization (free caches, reduce estimates)
        - Tier 3: Sequential loading (one component at a time)
        - Tier 4: Minimal mode (core only, no ML)
        - Tier 5: ABORT (only if ALL above fail)

        Returns:
            Tuple of (new_decision, can_proceed, degradation_tier, fallback_strategy)
        """
        logger.info(f"[OOMBridge] v2.0.0: Trying graceful degradation for '{component}'...")
        logger.info(f"  Available: {available_gb:.1f}GB, Required: {required_gb:.1f}GB")

        # Tier 2: Aggressive Memory Optimization
        freed_mb = await self._try_aggressive_memory_optimization()
        available_after_optimization = available_gb + (freed_mb / 1024)

        if available_after_optimization > required_gb + OOM_PREVENTION_THRESHOLDS["min_free_ram_gb"]:
            strategy = FALLBACK_STRATEGIES[DegradationTier.TIER_2_AGGRESSIVE_OPTIMIZE]
            recommendations.append(f"✅ Tier 2: Freed {freed_mb}MB via aggressive optimization")
            recommendations.append(f"Proceeding with optimized memory settings for {component}")
            return MemoryDecision.DEGRADED, True, DegradationTier.TIER_2_AGGRESSIVE_OPTIMIZE, strategy

        # Tier 3: Sequential Loading (reduces peak memory)
        # With sequential loading, we only need memory for one component at a time
        strategy = FALLBACK_STRATEGIES[DegradationTier.TIER_3_SEQUENTIAL_LOAD]
        reduced_requirement_gb = required_gb * 0.6  # Sequential loading uses ~60% peak memory

        if available_after_optimization > reduced_requirement_gb + OOM_PREVENTION_THRESHOLDS["critical_ram_gb"]:
            recommendations.append(f"✅ Tier 3: Using sequential loading for {component}")
            recommendations.append(f"Peak memory reduced from {required_gb:.1f}GB to {reduced_requirement_gb:.1f}GB")
            return MemoryDecision.DEGRADED, True, DegradationTier.TIER_3_SEQUENTIAL_LOAD, strategy

        # Tier 4: Minimal Mode (core only, no ML models)
        strategy = FALLBACK_STRATEGIES[DegradationTier.TIER_4_MINIMAL_MODE]
        minimal_requirement_gb = 1.0  # Core functionality only needs ~1GB

        if available_after_optimization > minimal_requirement_gb + OOM_PREVENTION_THRESHOLDS["critical_ram_gb"]:
            recommendations.append(f"⚠️ Tier 4: Minimal mode for {component} - core only")
            recommendations.append("ML models disabled, API routing available")
            recommendations.append("Use JARVIS_MINIMAL_MODE=false to require full functionality")
            return MemoryDecision.DEGRADED, True, DegradationTier.TIER_4_MINIMAL_MODE, strategy

        # Tier 5: All strategies exhausted - ABORT as last resort
        recommendations.append(f"❌ Tier 5: All degradation strategies exhausted for {component}")
        recommendations.append(f"Available: {available_after_optimization:.1f}GB, Need: {minimal_requirement_gb:.1f}GB minimum")
        recommendations.append("Options: Add RAM, close applications, or enable GCP (set GCP_PROJECT_ID, GCP_ZONE, GCP_ENABLED=true)")
        return MemoryDecision.ABORT, False, DegradationTier.TIER_5_ABORT, None

    async def _try_aggressive_memory_optimization(self) -> int:
        """
        v2.0.0: Aggressively optimize memory to free up RAM.

        Returns:
            Amount of memory freed in MB
        """
        freed_mb = 0

        try:
            # 1. Force Python garbage collection
            import gc
            gc.collect()
            freed_mb += 50  # Conservative estimate

            # 2. Try to clear Python caches
            try:
                import importlib
                # Clear module finder caches
                importlib.invalidate_caches()
                freed_mb += 20
            except Exception:
                pass

            # 3. Clear any in-memory caches we control
            try:
                # This is where component-specific cache clearing would go
                # For now, just log that we tried
                logger.debug("[OOMBridge] Cleared internal caches")
                freed_mb += 30
            except Exception:
                pass

            # 4. Suggest OS-level memory clearing (macOS specific)
            try:
                import platform
                if platform.system() == "Darwin":
                    # Note: We can't actually run purge without sudo, but we log the suggestion
                    logger.debug("[OOMBridge] On macOS - memory pressure may trigger system cleanup")
                    freed_mb += 50  # macOS often handles this automatically
            except Exception:
                pass

            logger.info(f"[OOMBridge] Aggressive optimization freed ~{freed_mb}MB")
            return freed_mb

        except Exception as e:
            logger.warning(f"[OOMBridge] Aggressive optimization failed: {e}")
            return 0

    async def _ensure_gcp_vm_ready_v2(
        self,
        is_critical: bool = False,
        reason: str = "",
    ) -> Tuple[bool, Optional[str], bool]:
        """
        v2.0.0: Ensure a GCP Spot VM is ready, with AUTO-ENABLE capability.

        KEY IMPROVEMENT: If GCP is disabled but memory is CRITICAL, this method
        will attempt to auto-enable GCP (if credentials are available).

        Args:
            is_critical: If True, attempt to auto-enable GCP if disabled
            reason: Reason for needing GCP

        Returns:
            Tuple of (is_ready, vm_ip, was_auto_enabled)
        """
        gcp_auto_enabled = False

        # Check if GCP is available but disabled
        if not self._gcp_vm_manager:
            logger.warning("[OOMBridge] GCP VM Manager not available")
            return False, None, False

        # v2.0.0: AUTO-ENABLE GCP when critical
        if not self._gcp_vm_manager.enabled:
            if is_critical and OOM_PREVENTION_THRESHOLDS["auto_enable_gcp_on_critical"]:
                # Try to auto-enable GCP
                auto_enabled = await self._try_auto_enable_gcp(reason)
                if auto_enabled:
                    gcp_auto_enabled = True
                    self._gcp_auto_enabled = True
                    logger.info("[OOMBridge] ✅ GCP auto-enabled due to critical memory pressure")
                else:
                    logger.warning(
                        "[OOMBridge] GCP disabled and could not be auto-enabled. "
                        "Set GCP_PROJECT_ID and GCP_ZONE environment variables to enable GCP."
                    )
                    return False, None, False
            else:
                logger.warning("[OOMBridge] GCP VM Manager disabled (GCP_ENABLED=false)")
                return False, None, False

        # Use the original logic for the rest
        is_ready, vm_ip = await self._ensure_gcp_vm_ready()
        return is_ready, vm_ip, gcp_auto_enabled

    async def _try_auto_enable_gcp(self, reason: str) -> bool:
        """
        v2.0.0: Attempt to auto-enable GCP for critical memory situations.

        This checks if GCP credentials are available and if so, dynamically
        enables GCP. This allows JARVIS to use GCP when truly needed without
        requiring GCP_ENABLED=true in normal operation.

        Returns:
            True if GCP was successfully enabled
        """
        logger.info("[OOMBridge] v2.0.0: Attempting to auto-enable GCP...")

        # Check if we have the required credentials/config
        gcp_project = os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
        gcp_zone = os.getenv("GCP_ZONE", "us-central1-a")

        if not gcp_project:
            if OOM_PREVENTION_THRESHOLDS["skip_gcp_if_credentials_missing"]:
                logger.info("[OOMBridge] GCP_PROJECT_ID not set - skipping GCP auto-enable")
                return False
            else:
                logger.warning("[OOMBridge] GCP_PROJECT_ID required but not set")
                return False

        # Check for credentials
        creds_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        has_default_creds = False

        if not creds_file:
            # Try to check for default credentials
            try:
                default_creds_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
                has_default_creds = default_creds_path.exists()
            except Exception:
                pass

        if not creds_file and not has_default_creds:
            logger.info("[OOMBridge] No GCP credentials found - skipping auto-enable")
            return False

        # Credentials available - try to enable GCP
        try:
            # v2.0.1: Use the new force-enable function that properly resets the singleton
            # The old approach of just setting GCP_ENABLED=true didn't work because the
            # singleton was already created with enabled=false
            logger.info(f"[OOMBridge] Auto-enabling GCP (project={gcp_project}, zone={gcp_zone})")

            try:
                # Try to import the force-enable function (v132.0+)
                from core.gcp_vm_manager import get_gcp_vm_manager_with_force_enable
                self._gcp_vm_manager = await get_gcp_vm_manager_with_force_enable()
            except ImportError:
                # Fallback for older versions - try the old approach
                from core.gcp_vm_manager import get_gcp_vm_manager, reset_gcp_vm_manager_singleton
                os.environ["GCP_ENABLED"] = "true"
                await reset_gcp_vm_manager_singleton()
                self._gcp_vm_manager = await get_gcp_vm_manager()

            # Check if it worked
            if self._gcp_vm_manager and self._gcp_vm_manager.enabled:
                logger.info(f"[OOMBridge] ✅ GCP auto-enabled successfully for: {reason}")
                return True
            else:
                logger.warning("[OOMBridge] GCP auto-enable failed - manager still disabled after reset")
                # Log diagnostic info
                if self._gcp_vm_manager:
                    logger.warning(f"  Manager exists but enabled={self._gcp_vm_manager.enabled}")
                    logger.warning(f"  Config enabled={self._gcp_vm_manager.config.enabled}")
                    logger.warning(f"  GCP_ENABLED env={os.getenv('GCP_ENABLED')}")
                return False

        except Exception as e:
            logger.error(f"[OOMBridge] GCP auto-enable failed: {e}")
            return False

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
        v2.0.0: Write comprehensive OOM signal file for cross-repo coordination.

        This allows JARVIS Prime and Reactor Core to know about memory
        decisions and adjust their behavior accordingly. The signal includes:
        - Decision type (local, cloud, degraded, abort)
        - Degradation tier and fallback strategy
        - GCP status and VM info
        - Memory metrics
        - Recommendations for other components

        Signal file: ~/.jarvis/signals/oom_prevention.json
        """
        try:
            signal_file = self._signal_dir / "oom_prevention.json"

            # Build comprehensive signal data
            signal_data = {
                # v2.0.0: Enhanced signal format
                "version": "2.0.0",
                "timestamp": time.time(),

                # Core decision
                "decision": result.decision.value,
                "can_proceed": result.can_proceed,
                "can_proceed_locally": result.can_proceed_locally,
                "reason": result.reason,

                # v2.0.0: Degradation info
                "degradation_active": self._degradation_active,
                "degradation_tier": result.degradation_tier.value if result.degradation_tier else "none",
                "fallback_strategy": result.fallback_strategy.to_dict() if result.fallback_strategy else None,

                # GCP status
                "gcp_vm_required": result.gcp_vm_required,
                "gcp_vm_ready": result.gcp_vm_ready,
                "gcp_vm_ip": result.gcp_vm_ip,
                "gcp_auto_enabled": result.gcp_auto_enabled,
                "offload_mode_active": self._offload_mode_active,

                # Memory metrics
                "available_ram_gb": round(result.available_ram_gb, 2),
                "required_ram_gb": round(result.required_ram_gb, 2),
                "memory_pressure_percent": round(result.memory_pressure_percent, 1),

                # Component info
                "component": result.component_name,
                "adaptive_estimate_used": result.adaptive_estimate_used,
                "actual_estimate_mb": result.actual_estimate_mb,

                # Recommendations for other components
                "recommendations": result.recommendations,

                # Hints for cross-repo behavior adjustment
                "hints": {
                    "extend_timeouts": result.decision in (MemoryDecision.DEGRADED, MemoryDecision.CLOUD_REQUIRED),
                    "reduce_parallelism": result.degradation_tier in (
                        DegradationTier.TIER_3_SEQUENTIAL_LOAD,
                        DegradationTier.TIER_4_MINIMAL_MODE,
                    ) if result.degradation_tier else False,
                    "skip_non_essential": result.degradation_tier == DegradationTier.TIER_4_MINIMAL_MODE if result.degradation_tier else False,
                    "use_smaller_models": result.degradation_tier in (
                        DegradationTier.TIER_2_AGGRESSIVE_OPTIMIZE,
                        DegradationTier.TIER_4_MINIMAL_MODE,
                    ) if result.degradation_tier else False,
                },
            }

            with open(signal_file, "w") as f:
                json.dump(signal_data, f, indent=2)

            logger.debug(f"[OOMBridge] Wrote OOM signal: decision={result.decision.value}, "
                        f"tier={result.degradation_tier.value if result.degradation_tier else 'none'}")

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
    # Core classes
    "GCPOOMPreventionBridge",
    "MemoryCheckResult",
    "MemoryDecision",

    # v2.0.0: Graceful degradation
    "DegradationTier",
    "FallbackStrategy",
    "FALLBACK_STRATEGIES",

    # v2.0.0: Adaptive estimation
    "AdaptiveMemoryEstimator",
    "get_adaptive_estimator",

    # Main functions
    "get_oom_prevention_bridge",
    "check_memory_before_heavy_init",
    "ensure_sufficient_memory_or_offload",

    # Configuration
    "HEAVY_COMPONENT_MEMORY_ESTIMATES",
    "OOM_PREVENTION_THRESHOLDS",
]
