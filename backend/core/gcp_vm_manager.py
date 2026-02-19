"""
GCP Spot VM Auto-Creation Manager
==================================

Advanced, robust, async system for auto-creating and managing GCP Spot VMs
when local memory pressure is too high.

Features:
- Auto-creates e2-highmem-4 Spot VMs (32GB RAM) when RAM >85%
- Integrates with intelligent_gcp_optimizer for smart decisions
- Tracks all costs via cost_tracker
- Async/await throughout for non-blocking operations
- No hardcoding - all configuration from environment/config
- Comprehensive error handling and retry logic
- VM lifecycle management (create, monitor, cleanup)
- Orphaned VM detection and cleanup
- Health monitoring and auto-recovery
- Parallel operations where safe
- Circuit breaker pattern for fault tolerance
- Dynamic configuration loading
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import yaml
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, TypeVar

# Google Cloud Compute Engine API
# Use TYPE_CHECKING to allow type hints without requiring the library at runtime
COMPUTE_AVAILABLE = False
compute_v1: Any = None  # Module placeholder for runtime

try:
    from google.cloud import compute_v1 as _compute_v1
    compute_v1 = _compute_v1
    COMPUTE_AVAILABLE = True
except ImportError:
    logging.warning("google-cloud-compute not installed. VM creation disabled.")

# Type aliases for when compute_v1 is not available
if TYPE_CHECKING:
    from google.cloud import compute_v1 as compute_v1_types
    InstancesClientType = compute_v1_types.InstancesClient
    ZonesClientType = compute_v1_types.ZonesClient
    InstanceType = compute_v1_types.Instance
else:
    InstancesClientType = Any
    ZonesClientType = Any
    InstanceType = Any

# Import with fallback for different import contexts
try:
    from core.cost_tracker import get_cost_tracker, CostTracker
    from core.intelligent_gcp_optimizer import get_gcp_optimizer
except ImportError:
    # Fallback for when running from within core directory
    try:
        from .cost_tracker import get_cost_tracker, CostTracker
        from .intelligent_gcp_optimizer import get_gcp_optimizer
    except ImportError:
        # Final fallback - provide stubs
        def get_cost_tracker():
            return None
        def get_gcp_optimizer(config=None):
            return None
        CostTracker = None

# v9.0: Rate limit manager integration
RATE_LIMIT_MANAGER_AVAILABLE = False
try:
    from core.gcp_rate_limit_manager import (
        get_rate_limit_manager,
        GCPService,
        OperationType,
        RateLimitExceededError,
    )
    RATE_LIMIT_MANAGER_AVAILABLE = True
except ImportError:
    try:
        from .gcp_rate_limit_manager import (
            get_rate_limit_manager,
            GCPService,
            OperationType,
            RateLimitExceededError,
        )
        RATE_LIMIT_MANAGER_AVAILABLE = True
    except ImportError:
        pass

# v224.0: aiohttp availability for golden image health polling
AIOHTTP_AVAILABLE = False
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    pass

from backend.core.async_safety import LazyAsyncLock, create_safe_task

# Log severity bridge for criticality-aware logging
try:
    from backend.core.log_severity_bridge import log_component_failure, is_component_required
except ImportError:
    def log_component_failure(component, message, error=None, **ctx):
        logging.getLogger(__name__).error(f"{component}: {message}")
    def is_component_required(component):
        return True

logger = logging.getLogger(__name__)

# Type variable for generic retry decorator
T = TypeVar('T')

# v235.0: Startup script version tag — bumped on every significant script change.
# Embedded in VM metadata at creation. Running VMs with a stale version are
# automatically recycled (deleted + recreated) to pick up the latest script.
_STARTUP_SCRIPT_VERSION = "235.3"


# ============================================================================
# v109.4: SHUTDOWN DETECTION TO PREVENT INITIALIZATION DURING ATEXIT
# ============================================================================

# When True, prevents GCP client initialization which uses guarded FDs
# This is set by shutdown_hook.py during cleanup to prevent EXC_GUARD crashes
_gcp_shutdown_requested = False


def mark_gcp_shutdown() -> None:
    """
    v109.4: Mark that GCP resources should not be initialized.

    This is called by shutdown_hook.py during cleanup to prevent
    GCP client libraries from being initialized during interpreter
    shutdown, which would cause EXC_GUARD crashes due to guarded
    file descriptors used by libdispatch/GCD on macOS.

    Once called, initialize() and get_gcp_vm_manager() will return
    early without initializing GCP API clients.
    """
    global _gcp_shutdown_requested
    _gcp_shutdown_requested = True
    logger.debug("[GCPVMManager] v109.4: Shutdown requested - GCP init disabled")


def is_gcp_shutdown_requested() -> bool:
    """Check if GCP shutdown has been requested."""
    return _gcp_shutdown_requested


# ============================================================================
# CIRCUIT BREAKER FOR FAULT TOLERANCE
# ============================================================================


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, rejecting requests
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreaker:
    """
    Circuit breaker pattern for fault-tolerant GCP operations.
    Prevents cascading failures when GCP API is having issues.
    """
    name: str
    failure_threshold: int = 3  # Open circuit after N failures
    recovery_timeout: float = 60.0  # Seconds before trying again
    half_open_max_calls: int = 1  # Calls to test when half-open
    
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: Optional[float] = None
    half_open_calls: int = 0
    
    def can_execute(self) -> Tuple[bool, str]:
        """Check if we can execute an operation"""
        if self.state == CircuitState.CLOSED:
            return True, "Circuit closed - normal operation"
        
        if self.state == CircuitState.OPEN:
            # Check if we should transition to half-open
            if self.last_failure_time and (time.time() - self.last_failure_time) >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                logger.info(f"🔌 Circuit '{self.name}' transitioning to HALF_OPEN")
                return True, "Circuit half-open - testing recovery"
            return False, f"Circuit OPEN - {self.recovery_timeout - (time.time() - (self.last_failure_time or 0)):.0f}s until retry"
        
        # Half-open state
        if self.half_open_calls < self.half_open_max_calls:
            return True, "Circuit half-open - testing"
        return False, "Circuit half-open - max test calls reached"
    
    def record_success(self):
        """Record successful operation"""
        if self.state == CircuitState.HALF_OPEN:
            logger.info(f"✅ Circuit '{self.name}' recovered - transitioning to CLOSED")
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.half_open_calls = 0
    
    def record_failure(self, error: Exception):
        """Record failed operation"""
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.state == CircuitState.HALF_OPEN:
            logger.warning(f"⚠️ Circuit '{self.name}' recovery failed - re-opening")
            self.state = CircuitState.OPEN
        elif self.failure_count >= self.failure_threshold:
            logger.error(f"🔴 Circuit '{self.name}' OPEN after {self.failure_count} failures: {error}")
            self.state = CircuitState.OPEN


class VMState(Enum):
    """VM lifecycle states"""

    CREATING = "creating"
    PROVISIONING = "provisioning"
    STAGING = "staging"
    RUNNING = "running"
    STOPPING = "stopping"
    TERMINATED = "terminated"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass
class VMInstance:
    """Represents a managed VM instance"""

    instance_id: str
    name: str
    zone: str
    state: VMState
    created_at: float
    ip_address: Optional[str] = None
    internal_ip: Optional[str] = None
    last_health_check: Optional[float] = None
    health_status: str = "unknown"
    components: List[str] = field(default_factory=list)
    trigger_reason: str = ""
    cost_per_hour: float = 0.029  # e2-highmem-4 Spot VM
    total_cost: float = 0.0
    metadata: Dict = field(default_factory=dict)

    # Cost tracking and efficiency
    last_activity_time: float = field(default_factory=time.time)
    component_usage_count: int = 0  # How many times components were accessed
    cost_efficiency_score: float = 0.0  # 0-100, higher is better ROI
    score_confidence: float = 0.0  # 0-1, how much real data backs the score

    # Configurable thresholds (set from VMManagerConfig at creation time)
    _idle_threshold_minutes: float = float(os.getenv("GCP_IDLE_TIMEOUT_MINUTES", os.getenv("JARVIS_SPOT_VM_IDLE_TIMEOUT", "30")))
    _wasting_money_threshold: float = float(os.getenv("GCP_EFFICIENCY_TERMINATION_THRESHOLD", "30.0"))
    _min_confidence_for_termination: float = float(os.getenv("GCP_EFFICIENCY_MIN_CONFIDENCE", "0.5"))

    # Detailed metrics (like GCP Console)
    cpu_percent: float = 0.0
    memory_used_gb: float = 0.0
    memory_total_gb: float = 32.0
    network_sent_mb: float = 0.0
    network_received_mb: float = 0.0
    disk_read_mb: float = 0.0
    disk_write_mb: float = 0.0

    @property
    def uptime_hours(self) -> float:
        """Calculate VM uptime in hours"""
        return (time.time() - self.created_at) / 3600

    @property
    def idle_time_minutes(self) -> float:
        """Calculate how long VM has been idle"""
        return (time.time() - self.last_activity_time) / 60

    @property
    def is_healthy(self) -> bool:
        """Check if VM is healthy"""
        return self.health_status == "healthy" and self.state == VMState.RUNNING

    @property
    def is_idle(self) -> bool:
        """Check if VM has been idle too long (uses configurable threshold)"""
        return self.idle_time_minutes > self._idle_threshold_minutes

    @property
    def is_wasting_money(self) -> bool:
        """Determine if VM is wasting money (idle + low efficiency + confident score).
        Requires score_confidence > threshold to prevent killing new VMs
        that haven't had time to accumulate real data."""
        return (
            self.is_idle
            and self.cost_efficiency_score < self._wasting_money_threshold
            and self.score_confidence > self._min_confidence_for_termination
        )

    @property
    def memory_percent(self) -> float:
        """Memory usage percentage"""
        return (self.memory_used_gb / self.memory_total_gb * 100) if self.memory_total_gb > 0 else 0.0

    def update_cost(self):
        """Update total cost based on uptime"""
        self.total_cost = self.uptime_hours * self.cost_per_hour

    def update_efficiency_score(
        self,
        ramp_minutes: float = 30.0,
        startup_bonus_max: float = 50.0,
    ):
        """Calculate cost efficiency score (0-100, higher is better ROI).

        Also computes score_confidence (0-1) indicating how much real data
        backs the score. Low-confidence scores won't trigger VM termination,
        preventing false kills on newly created VMs.

        Args:
            ramp_minutes: Minutes over which startup bonus declines to 0.
            startup_bonus_max: Maximum startup bonus for brand-new VMs.
        """
        # Guard: VM just created (<4 seconds), give benefit of the doubt
        if self.uptime_hours < 0.001:
            self.cost_efficiency_score = startup_bonus_max
            self.score_confidence = 0.0  # No data at all
            return

        # Factors:
        # 1. Usage frequency (component_usage_count / uptime_minutes)
        # 2. Recency (how recently was it used?)
        # 3. Resource utilization (is it actually being used?)

        uptime_minutes = self.uptime_hours * 60
        usage_rate = (self.component_usage_count / uptime_minutes) if uptime_minutes > 0 else 0
        recency_score = max(0, 100 - (self.idle_time_minutes * 2))  # Decreases as idle time increases
        utilization_score = min(100, (self.cpu_percent + self.memory_percent) / 2)

        # Startup ramp: VMs under ramp_minutes get a declining bonus that
        # compensates for insufficient usage data.
        startup_bonus = max(0.0, startup_bonus_max * (1.0 - uptime_minutes / ramp_minutes))

        # Weighted average
        self.cost_efficiency_score = (
            usage_rate * 100 * 0.4 +  # 40% weight on usage frequency
            recency_score * 0.3 +       # 30% weight on recency
            utilization_score * 0.3 +   # 30% weight on resource utilization
            startup_bonus               # Ramp bonus for young VMs
        )

        self.cost_efficiency_score = min(100.0, self.cost_efficiency_score)

        # Confidence = how much real data backs this score
        # 70% based on data signals, 30% based on time running
        data_signals = 0
        if self.component_usage_count > 0:
            data_signals += 1
        if self.cpu_percent > 0:
            data_signals += 1
        if self.memory_used_gb > 0:
            data_signals += 1
        time_factor = min(1.0, uptime_minutes / ramp_minutes)  # 0→1 over ramp period
        self.score_confidence = min(1.0, (data_signals / 3.0) * 0.7 + time_factor * 0.3)

    def record_activity(self):
        """Record that VM components were used"""
        self.last_activity_time = time.time()
        self.component_usage_count += 1


@dataclass
class VMManagerConfig:
    """
    Dynamic configuration for GCP VM Manager.
    All values loaded from environment variables with sensible defaults.
    No hardcoding - fully configurable at runtime.
    """

    # v147.0: GCP Enabled Flag - checks multiple env vars for compatibility
    # Accepts: GCP_ENABLED, GCP_VM_ENABLED, JARVIS_SPOT_VM_ENABLED (any "true" enables)
    # This fixes the inconsistency where .env.gcp sets JARVIS_SPOT_VM_ENABLED
    # but the manager only checked GCP_ENABLED
    enabled: bool = field(
        default_factory=lambda: any([
            os.getenv("GCP_ENABLED", "false").lower() == "true",
            os.getenv("GCP_VM_ENABLED", "false").lower() == "true",
            os.getenv("JARVIS_SPOT_VM_ENABLED", "false").lower() == "true",
        ])
    )

    # GCP Configuration (all from environment)
    project_id: str = field(
        default_factory=lambda: os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    )
    region: str = field(default_factory=lambda: os.getenv("GCP_REGION", "us-central1"))
    zone: str = field(default_factory=lambda: os.getenv("GCP_ZONE", "us-central1-a"))

    # VM Configuration (dynamic)
    machine_type: str = field(
        default_factory=lambda: os.getenv("GCP_VM_MACHINE_TYPE", "e2-highmem-4")
    )
    use_spot: bool = field(
        default_factory=lambda: os.getenv("GCP_USE_SPOT_VMS", "true").lower() == "true"
    )
    spot_max_price: float = field(
        default_factory=lambda: float(os.getenv("GCP_SPOT_MAX_PRICE", "0.10"))
    )

    # VM Naming
    vm_name_prefix: str = field(
        default_factory=lambda: os.getenv("GCP_VM_NAME_PREFIX", "jarvis-backend")
    )

    # Image Configuration (dynamic)
    image_project: str = field(
        default_factory=lambda: os.getenv("GCP_IMAGE_PROJECT", "ubuntu-os-cloud")
    )
    image_family: str = field(
        default_factory=lambda: os.getenv("GCP_IMAGE_FAMILY", "ubuntu-2204-lts")
    )

    # Disk Configuration
    boot_disk_size_gb: int = field(
        default_factory=lambda: int(os.getenv("GCP_BOOT_DISK_SIZE_GB", "80"))  # v241.0: 80 GB for multi-model golden image (~27 GB models + OS + deps)
    )
    boot_disk_type: str = field(
        default_factory=lambda: os.getenv("GCP_BOOT_DISK_TYPE", "pd-standard")
    )

    # Network Configuration
    network: str = field(default_factory=lambda: os.getenv("GCP_NETWORK", "default"))
    subnetwork: str = field(default_factory=lambda: os.getenv("GCP_SUBNETWORK", "default"))

    # Startup Script Path
    startup_script_path: Optional[str] = field(
        default_factory=lambda: os.getenv(
            "GCP_STARTUP_SCRIPT_PATH",
            os.path.join(os.path.dirname(__file__), "gcp_vm_startup.sh")
        )
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # v1.0.0: DOCKER CONTAINER-BASED VM DEPLOYMENT (Pre-baked ML Dependencies)
    # ═══════════════════════════════════════════════════════════════════════════
    # When enabled, VMs are created using Container-Optimized OS with a Docker
    # image that has all ML dependencies pre-installed. This eliminates the
    # 5-8 minute ml_deps installation phase during startup.
    #
    # Benefits:
    #   - Startup time: ~2-3 min instead of ~8-10 min
    #   - Consistent environment across all VMs
    #   - Easier debugging and reproducibility
    #   - Reduced network dependencies during startup
    #
    # Usage:
    #   1. Build and push image: python scripts/build_gcp_inference_image.py
    #   2. Set JARVIS_GCP_CONTAINER_IMAGE to the image URL
    #   3. Set JARVIS_GCP_USE_CONTAINER=true
    # ═══════════════════════════════════════════════════════════════════════════
    
    # Enable container-based deployment (uses Container-Optimized OS)
    use_container: bool = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_USE_CONTAINER", "false").lower() == "true"
    )
    
    # Docker image for container-based deployment
    # Format: gcr.io/PROJECT/IMAGE:TAG or REGION-docker.pkg.dev/PROJECT/REPO/IMAGE:TAG
    container_image: Optional[str] = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_CONTAINER_IMAGE", None)
    )
    
    # Container-Optimized OS image for container-based VMs
    container_os_image_project: str = field(
        default_factory=lambda: os.getenv("GCP_CONTAINER_OS_PROJECT", "cos-cloud")
    )
    container_os_image_family: str = field(
        default_factory=lambda: os.getenv("GCP_CONTAINER_OS_FAMILY", "cos-stable")
    )
    
    # Container environment variables (comma-separated key=value pairs)
    container_env_vars: str = field(
        default_factory=lambda: os.getenv(
            "JARVIS_GCP_CONTAINER_ENV",
            "JARVIS_DEPS_PREBAKED=true,JARVIS_SKIP_ML_DEPS_INSTALL=true,JARVIS_GCP_INFERENCE=true"
        )
    )
    
    # Fallback to startup script if container deployment fails
    container_fallback_to_script: bool = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_CONTAINER_FALLBACK", "true").lower() == "true"
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # v224.0: GOLDEN IMAGE (Pre-baked Custom VM Image) Configuration
    # ═══════════════════════════════════════════════════════════════════════════
    # ENTERPRISE-GRADE SOLUTION: Custom machine image with everything pre-installed.
    # This reduces VM startup from 10-15 minutes to ~30-60 seconds.
    #
    # What's Pre-baked in the Golden Image:
    #   - Python 3.11 with virtual environment
    #   - All ML dependencies (torch, transformers, llama-cpp-python, etc.)
    #   - JARVIS-Prime codebase with all dependencies
    #   - Model files (7B+ parameters) already downloaded
    #   - System configured and ready to serve
    #
    # Benefits:
    #   - Startup time: ~30-60 seconds instead of 10-15 minutes
    #   - No network downloads during startup
    #   - Consistent, tested environment
    #   - Instant model availability
    #
    # Hierarchy (highest priority first):
    #   1. Custom Golden Image (if enabled and available)
    #   2. Container-based deployment (if enabled)
    #   3. Standard startup script (fallback)
    #
    # Usage:
    #   1. Create golden image: python3 unified_supervisor.py --create-golden-image
    #   2. Set JARVIS_GCP_USE_GOLDEN_IMAGE=true
    #   3. Optionally set JARVIS_GCP_GOLDEN_IMAGE_NAME to override auto-detection
    # ═══════════════════════════════════════════════════════════════════════════
    
    # Enable golden image deployment (highest priority deployment mode)
    use_golden_image: bool = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_USE_GOLDEN_IMAGE", "false").lower() == "true"
    )
    
    # Custom image name (if not set, auto-detects latest jarvis-prime-golden-* image)
    golden_image_name: Optional[str] = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_GOLDEN_IMAGE_NAME", None)
    )
    
    # Project containing the golden image (defaults to current project)
    golden_image_project: Optional[str] = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_GOLDEN_IMAGE_PROJECT", None)
    )
    
    # Golden image family for auto-detection of latest version
    golden_image_family: str = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_GOLDEN_IMAGE_FAMILY", "jarvis-prime-golden")
    )
    
    # Maximum age of golden image before rebuild is recommended (days)
    golden_image_max_age_days: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_GCP_GOLDEN_IMAGE_MAX_AGE_DAYS", "30"))
    )
    
    # Auto-rebuild golden image if stale (requires elevated permissions)
    golden_image_auto_rebuild: bool = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_GOLDEN_IMAGE_AUTO_REBUILD", "false").lower() == "true"
    )
    
    # Fallback to container/script if golden image is unavailable
    golden_image_fallback: bool = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_GOLDEN_IMAGE_FALLBACK", "true").lower() == "true"
    )
    
    # Model to pre-load in golden image (for image building)
    golden_image_model: str = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_GOLDEN_IMAGE_MODEL", "mistral-7b-instruct-v0.2")
    )
    
    # Machine type for building golden image (needs enough RAM for model)
    golden_image_builder_machine_type: str = field(
        default_factory=lambda: os.getenv("JARVIS_GCP_GOLDEN_BUILDER_MACHINE_TYPE", "e2-highmem-8")
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # INVINCIBLE NODE (Static IP + STOP termination) Configuration
    # ═══════════════════════════════════════════════════════════════════════════
    # When these are set, the manager uses persistent VM strategy instead of
    # ephemeral VMs. The VM survives preemption in STOPPED state and can be
    # quickly restarted (~30s) instead of recreated (~3-5 min).
    # v210.0: Added default name for auto-creation if not specified
    static_ip_name: Optional[str] = field(
        default_factory=lambda: os.getenv("GCP_VM_STATIC_IP_NAME", "jarvis-prime-ip")
    )
    static_instance_name: Optional[str] = field(
        default_factory=lambda: os.getenv("GCP_VM_INSTANCE_NAME", "jarvis-prime-node")
    )
    # When True, uses STOP instead of DELETE on termination (Invincible Node mode)
    use_stop_termination: bool = field(
        default_factory=lambda: os.getenv("GCP_VM_TERMINATION_ACTION", "DELETE").upper() == "STOP"
    )
    # Health check endpoint settings for static VM
    static_vm_health_poll_interval: float = field(
        default_factory=lambda: float(os.getenv("GCP_STATIC_VM_HEALTH_POLL_INTERVAL", "5.0"))
    )
    static_vm_health_timeout: float = field(
        default_factory=lambda: float(os.getenv("GCP_STATIC_VM_HEALTH_TIMEOUT", "300.0"))
    )

    # Health Check Configuration
    health_check_interval: int = field(
        default_factory=lambda: int(os.getenv("GCP_HEALTH_CHECK_INTERVAL", "30"))
    )
    health_check_timeout: int = field(
        default_factory=lambda: int(os.getenv("GCP_HEALTH_CHECK_TIMEOUT", "10"))
    )
    max_health_check_failures: int = field(
        default_factory=lambda: int(os.getenv("GCP_MAX_HEALTH_CHECK_FAILURES", "3"))
    )

    # VM Lifecycle
    max_vm_lifetime_hours: float = field(
        default_factory=lambda: float(os.getenv("GCP_MAX_VM_LIFETIME_HOURS", "3.0"))
    )
    idle_timeout_minutes: int = field(
        # Prefer JARVIS_SPOT_VM_IDLE_TIMEOUT (used by hybrid cloud stack), fall back to legacy GCP_*
        default_factory=lambda: int(
            os.getenv("GCP_IDLE_TIMEOUT_MINUTES", os.getenv("JARVIS_SPOT_VM_IDLE_TIMEOUT", "30"))
        )
    )

    # Retry Configuration
    max_create_attempts: int = field(
        default_factory=lambda: int(os.getenv("GCP_MAX_CREATE_ATTEMPTS", "3"))
    )
    retry_delay_seconds: int = field(
        default_factory=lambda: int(os.getenv("GCP_RETRY_DELAY_SECONDS", "10"))
    )

    # Resource Limits
    max_concurrent_vms: int = field(
        default_factory=lambda: int(os.getenv("GCP_MAX_CONCURRENT_VMS", "2"))
    )
    daily_budget_usd: float = field(
        # Prefer JARVIS_SPOT_VM_DAILY_BUDGET (used by hybrid cloud stack), fall back to legacy GCP_*
        default_factory=lambda: float(
            os.getenv("GCP_DAILY_BUDGET_USD", os.getenv("JARVIS_SPOT_VM_DAILY_BUDGET", "5.0"))
        )
    )

    # Monitoring
    enable_monitoring: bool = field(
        default_factory=lambda: os.getenv("GCP_ENABLE_MONITORING", "true").lower() == "true"
    )
    enable_logging: bool = field(
        default_factory=lambda: os.getenv("GCP_ENABLE_LOGGING", "true").lower() == "true"
    )

    # Circuit breaker settings
    circuit_failure_threshold: int = field(
        default_factory=lambda: int(os.getenv("GCP_CIRCUIT_FAILURE_THRESHOLD", "3"))
    )
    circuit_recovery_timeout: float = field(
        default_factory=lambda: float(os.getenv("GCP_CIRCUIT_RECOVERY_TIMEOUT", "60.0"))
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # EFFICIENCY SCORING CONFIGURATION — All thresholds env-var configurable
    # ═══════════════════════════════════════════════════════════════════════════
    efficiency_warning_threshold: float = field(
        default_factory=lambda: float(os.getenv("GCP_EFFICIENCY_WARNING_THRESHOLD", "50.0"))
    )
    efficiency_termination_threshold: float = field(
        default_factory=lambda: float(os.getenv("GCP_EFFICIENCY_TERMINATION_THRESHOLD", "30.0"))
    )
    efficiency_startup_bonus: float = field(
        default_factory=lambda: float(os.getenv("GCP_EFFICIENCY_STARTUP_BONUS", "50.0"))
    )
    efficiency_ramp_minutes: float = field(
        default_factory=lambda: float(os.getenv("GCP_EFFICIENCY_RAMP_MINUTES", "30.0"))
    )
    efficiency_grace_minutes: float = field(
        default_factory=lambda: float(os.getenv("GCP_EFFICIENCY_GRACE_MINUTES", "30.0"))
    )
    efficiency_warning_interval_seconds: float = field(
        default_factory=lambda: float(os.getenv("GCP_EFFICIENCY_WARNING_INTERVAL", "300"))
    )
    efficiency_min_confidence_for_termination: float = field(
        default_factory=lambda: float(os.getenv("GCP_EFFICIENCY_MIN_CONFIDENCE", "0.5"))
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # METRICS COLLECTION CONFIGURATION
    # ═══════════════════════════════════════════════════════════════════════════
    metrics_collection_enabled: bool = field(
        default_factory=lambda: os.getenv("GCP_METRICS_COLLECTION_ENABLED", "true").lower() == "true"
    )
    metrics_window_seconds: int = field(
        default_factory=lambda: int(os.getenv("GCP_METRICS_WINDOW_SECONDS", "300"))
    )

    def __post_init__(self):
        """Validate configuration after initialization"""
        # Track validation status for pre-flight checks
        self._validation_errors: List[str] = []

        if not self.enabled:
            logger.debug("GCP VM Manager disabled (GCP_ENABLED=false or not set)")
            return  # Skip rest of validation if disabled

        # ═══════════════════════════════════════════════════════════════════════════
        # CRITICAL: Validate required fields for API calls
        # ═══════════════════════════════════════════════════════════════════════════
        if not self.project_id or self.project_id.strip() == "":
            self._validation_errors.append(
                "GCP_PROJECT_ID not set - required for VM operations. "
                "Set via environment variable or GOOGLE_CLOUD_PROJECT."
            )
            logger.error(
                "❌ GCP_ENABLED=true but GCP_PROJECT_ID not set. "
                "VM creation will be blocked until this is configured."
            )

        if not self.zone or self.zone.strip() == "":
            self._validation_errors.append(
                "GCP_ZONE not set - required for VM operations. "
                "Set via environment variable (default: us-central1-a)."
            )
            logger.error(
                "❌ GCP_ENABLED=true but GCP_ZONE not set. "
                "VM creation will be blocked until this is configured."
            )

        # Log configuration summary (only if enabled and valid)
        if self.enabled and not self._validation_errors:
            logger.info(f"GCP VM Manager enabled:")
            logger.info(f"  Project: {self.project_id}")
            logger.info(f"  Zone: {self.zone}")
            logger.info(f"  Machine Type: {self.machine_type}")
            logger.info(f"  Use Spot: {self.use_spot}")
            logger.info(f"  Daily Budget: ${self.daily_budget_usd}")
        elif self._validation_errors:
            logger.warning(f"GCP VM Manager has {len(self._validation_errors)} configuration error(s)")

    def is_valid_for_vm_operations(self) -> Tuple[bool, str]:
        """
        Check if configuration is valid for VM operations.

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not self.enabled:
            return False, "GCP VM Manager is disabled (GCP_ENABLED=false)"

        if hasattr(self, '_validation_errors') and self._validation_errors:
            return False, "; ".join(self._validation_errors)

        # Double-check critical fields (defensive)
        if not self.project_id or self.project_id.strip() == "":
            return False, "GCP_PROJECT_ID is empty or not set"

        if not self.zone or self.zone.strip() == "":
            return False, "GCP_ZONE is empty or not set"

        return True, "Configuration valid"

    def to_dict(self) -> Dict[str, Any]:
        """Export configuration as dictionary"""
        return {
            "enabled": self.enabled,
            "project_id": self.project_id,
            "region": self.region,
            "zone": self.zone,
            "machine_type": self.machine_type,
            "use_spot": self.use_spot,
            "daily_budget_usd": self.daily_budget_usd,
            "max_concurrent_vms": self.max_concurrent_vms,
            "max_vm_lifetime_hours": self.max_vm_lifetime_hours,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VMManagerConfig":
        """Create configuration from dictionary"""
        return cls(**{k: v for k, v in data.items() if hasattr(cls, k)})


class GCPMetricsCollector:
    """
    Real VM metrics from GCP Monitoring API with circuit breaker.

    Uses hypervisor-level compute.googleapis.com/instance/cpu/utilization
    which is always available without installing the Ops Agent.
    Memory metrics require the Ops Agent so are NOT collected here —
    memory is estimated from activity patterns instead.

    Features:
    - Circuit breaker: opens after consecutive failures, auto-recovers
    - Async-safe: all GCP API calls wrapped in asyncio.to_thread
    - Graceful degradation: returns None on failure (never crashes caller)
    """

    def __init__(
        self,
        project_id: str,
        circuit_threshold: int = 3,
        circuit_timeout: float = 60.0,
        metrics_window_seconds: int = 300,
    ):
        self._project_id = project_id
        self._client: Any = None
        self._available = False
        self._metrics_window_seconds = metrics_window_seconds

        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_threshold = circuit_threshold
        self._circuit_open_until = 0.0
        self._circuit_timeout = circuit_timeout

        self._initialize()

    def _initialize(self):
        """Try to import and create the Monitoring API client."""
        try:
            from google.cloud import monitoring_v3
            self._client = monitoring_v3.MetricServiceClient()
            self._available = True
            logger.info("GCPMetricsCollector: Monitoring API client initialized")
        except ImportError:
            logger.info(
                "GCPMetricsCollector: google-cloud-monitoring not installed — "
                "falling back to estimated metrics"
            )
            self._available = False
        except Exception as e:
            logger.warning(f"GCPMetricsCollector: Failed to initialize Monitoring client: {e}")
            self._available = False

    @property
    def is_available(self) -> bool:
        """Whether the collector can attempt to fetch metrics."""
        return self._available and time.time() >= self._circuit_open_until

    async def fetch_vm_metrics(
        self, instance_name: str, zone: str
    ) -> Optional[Dict[str, float]]:
        """
        Fetch CPU utilization from GCP Monitoring API.

        Uses hypervisor-level cpu/utilization (no Ops Agent needed).
        Returns dict with 'cpu_percent' key, or None on failure.
        Does NOT fetch memory (requires Ops Agent).

        Args:
            instance_name: GCP VM instance name
            zone: GCP zone (e.g., 'us-central1-a')

        Returns:
            Dict with 'cpu_percent' (0-100 scale), or None on failure
        """
        if not self._available:
            return None

        if time.time() < self._circuit_open_until:
            return None

        try:
            result = await asyncio.to_thread(
                self._query_cpu_utilization, instance_name, zone
            )
            self._consecutive_failures = 0
            return result
        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._circuit_threshold:
                self._circuit_open_until = time.time() + self._circuit_timeout
                logger.warning(
                    f"GCPMetricsCollector: circuit breaker OPEN after "
                    f"{self._consecutive_failures} failures "
                    f"(will retry in {self._circuit_timeout}s): {e}"
                )
            else:
                logger.debug(
                    f"GCPMetricsCollector: fetch failed ({self._consecutive_failures}/"
                    f"{self._circuit_threshold}): {e}"
                )
            return None

    def _query_cpu_utilization(
        self, instance_name: str, zone: str
    ) -> Optional[Dict[str, float]]:
        """
        Synchronous GCP Monitoring API query for CPU utilization.

        Queries compute.googleapis.com/instance/cpu/utilization which
        returns a value 0.0-1.0 (fraction). We convert to 0-100%.
        """
        from google.cloud import monitoring_v3
        from google.protobuf import timestamp_pb2

        now = time.time()
        window_start = now - self._metrics_window_seconds

        interval = monitoring_v3.TimeInterval()
        start_time = timestamp_pb2.Timestamp()
        start_time.FromSeconds(int(window_start))
        end_time = timestamp_pb2.Timestamp()
        end_time.FromSeconds(int(now))
        interval.start_time = start_time
        interval.end_time = end_time

        project_name = f"projects/{self._project_id}"

        # Hypervisor-level CPU — always available, no agent needed
        request = monitoring_v3.ListTimeSeriesRequest(
            name=project_name,
            filter=(
                f'metric.type = "compute.googleapis.com/instance/cpu/utilization" '
                f'AND resource.labels.instance_id = "{instance_name}" '
                f'AND resource.labels.zone = "{zone}"'
            ),
            interval=interval,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        )

        results = self._client.list_time_series(request=request)

        # Extract the most recent data point
        for ts in results:
            for point in ts.points:
                # cpu/utilization is 0.0-1.0 fraction, convert to percentage
                cpu_fraction = point.value.double_value
                return {"cpu_percent": round(cpu_fraction * 100.0, 1)}

        return None

    def reset_circuit_breaker(self):
        """Manually reset the circuit breaker (e.g., after config change)."""
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0


@dataclass
class QuotaInfo:
    """
    v9.0: Intelligent GCP Quota tracking with caching.
    Prevents unnecessary VM creation attempts when quotas are exceeded.
    """
    metric: str  # e.g., "CPUS_ALL_REGIONS", "IN_USE_ADDRESSES"
    limit: float
    usage: float
    available: float = field(init=False)
    is_exceeded: bool = field(init=False)
    region: Optional[str] = None
    last_checked: float = field(default_factory=time.time)
    cache_ttl_seconds: int = 60  # Cache for 60 seconds
    
    def __post_init__(self):
        self.available = max(0, self.limit - self.usage)
        self.is_exceeded = self.usage >= self.limit
    
    @property
    def is_stale(self) -> bool:
        """Check if quota info is stale and needs refresh."""
        return time.time() - self.last_checked > self.cache_ttl_seconds
    
    @property
    def utilization_percent(self) -> float:
        """Return quota utilization as a percentage."""
        return (self.usage / self.limit * 100) if self.limit > 0 else 100.0


@dataclass
class QuotaCheckResult:
    """
    Result of pre-flight quota check before VM creation.
    """
    can_create: bool
    blocking_quotas: List[QuotaInfo] = field(default_factory=list)
    warning_quotas: List[QuotaInfo] = field(default_factory=list)  # >80% utilized
    checked_at: float = field(default_factory=time.time)
    message: str = ""
    
    @property
    def has_warnings(self) -> bool:
        return len(self.warning_quotas) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# v224.0: GOLDEN IMAGE BUILDER - Enterprise-Grade Pre-baked VM Images
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class GoldenImageInfo:
    """
    v224.0: Information about a golden image.
    
    Tracks metadata for golden images including version, age, and model info.
    """
    name: str
    project: str
    family: str
    creation_time: datetime
    model_name: str
    model_version: str
    jarvis_version: str
    disk_size_gb: int
    status: str  # READY, PENDING, FAILED
    source_vm_name: Optional[str] = None
    
    @property
    def age_days(self) -> float:
        """Return age of the image in days."""
        return (datetime.now() - self.creation_time).total_seconds() / 86400
    
    def is_stale(self, max_age_days: int) -> bool:
        """Check if image is stale (older than max_age_days)."""
        return self.age_days > max_age_days

    def is_model_mismatched(self, expected_model: Optional[str] = None) -> bool:
        """v233.0: Check if golden image model differs from expected.

        Uses aggressive normalization to handle format variants like
        meta-llama/Llama-2-7b vs llama-2-7b-chat.

        Args:
            expected_model: Expected model name. If None, reads from
                           JARVIS_GCP_GOLDEN_IMAGE_MODEL env var.

        Returns:
            True if mismatch detected, False if match or cannot determine.
        """
        if expected_model is None:
            expected_model = os.getenv("JARVIS_GCP_GOLDEN_IMAGE_MODEL", "").strip()
        if not expected_model or self.model_name == "unknown":
            return False  # Can't validate — safe default
        is_mismatch = (
            _normalize_model_name(expected_model) != _normalize_model_name(self.model_name)
        )
        if is_mismatch:
            logger.warning(
                f"[GoldenImageInfo] v233.0: Model mismatch — "
                f"image '{self.name}' has '{self.model_name}', "
                f"expected '{expected_model}'"
            )
        return is_mismatch

    @classmethod
    def from_gcp_image(cls, image: Any, project: str) -> "GoldenImageInfo":
        """
        Create GoldenImageInfo from GCP Image object.
        
        Args:
            image: GCP Image object from compute_v1
            project: GCP project ID
            
        Returns:
            GoldenImageInfo instance
        """
        # Parse labels for metadata
        labels = dict(image.labels) if image.labels else {}
        
        # Parse creation timestamp
        creation_time = datetime.now()
        if image.creation_timestamp:
            try:
                # GCP timestamps are in RFC3339 format
                creation_time = datetime.fromisoformat(
                    image.creation_timestamp.replace('Z', '+00:00')
                ).replace(tzinfo=None)
            except (ValueError, AttributeError):
                pass
        
        return cls(
            name=image.name,
            project=project,
            family=labels.get("family", image.family or "jarvis-prime-golden"),
            creation_time=creation_time,
            model_name=labels.get("model-name", "unknown"),
            model_version=labels.get("model-version", "unknown"),
            jarvis_version=labels.get("jarvis-version", "unknown"),
            disk_size_gb=int(image.disk_size_gb) if image.disk_size_gb else 50,
            status=image.status or "READY",
            source_vm_name=labels.get("source-vm", None),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# v233.0: Golden Image Diagnostic Types
# ═══════════════════════════════════════════════════════════════════════════════

def _normalize_model_name(name: str) -> str:
    """v233.0: Aggressive normalization for model name comparison.

    Handles variants like:
    - meta-llama/Llama-2-7b → llama27b
    - llama-2-7b-chat → llama27bchat
    - Mistral_7B_Instruct_v0.2 → mistral7binstructv02
    """
    import re
    name = name.lower()
    name = name.split("/")[-1]  # Strip HuggingFace repo prefix
    name = re.sub(r"[^a-z0-9]", "", name)  # Only alphanumeric
    return name


class GoldenImageFailureType(str, Enum):
    """v233.0: Classification of golden image failure modes.

    Used by _diagnose_golden_image_stall() to categorize serial console
    output patterns into actionable failure types for corruption detection.
    """
    CORRUPTED_MODEL_CACHE = "corrupted_model_cache"
    DEPENDENCY_DRIFT = "dependency_drift"
    SERVICE_NEVER_STARTED = "service_never_started"
    PERMISSION_ERROR = "permission_error"
    DISK_FULL = "disk_full"
    HEALTH_ENDPOINT_CRASH = "health_endpoint_crash"
    OUT_OF_MEMORY = "out_of_memory"
    MODEL_LOAD_HANG = "model_load_hang"
    NETWORK_TIMEOUT = "network_timeout"
    UNKNOWN = "unknown"


# Types that indicate IMAGE-LEVEL corruption (not transient GCP issues).
# These trigger immediate suspect flagging on first stall.
_CRITICAL_FAILURE_TYPES = frozenset({
    GoldenImageFailureType.CORRUPTED_MODEL_CACHE,
    GoldenImageFailureType.SERVICE_NEVER_STARTED,
    GoldenImageFailureType.DEPENDENCY_DRIFT,
})

# Types that can be caused by transient GCP issues.
# These require 2+ occurrences before flagging suspect.
_TRANSIENT_FAILURE_TYPES = frozenset({
    GoldenImageFailureType.NETWORK_TIMEOUT,
    GoldenImageFailureType.OUT_OF_MEMORY,
})


@dataclass
class GoldenImageDiagnostic:
    """v233.0: Diagnostic result from golden image stall analysis.

    Captures serial console output and pattern-matches against known
    failure modes to enable intelligent corruption detection and recovery.
    """
    failure_type: GoldenImageFailureType
    serial_output_tail: str
    confidence: float  # 0.0-1.0
    detected_patterns: List[str]
    vm_name: str
    stall_progress: float
    timestamp: float = field(default_factory=time.time)

    @property
    def is_critical_failure(self) -> bool:
        """Whether this failure indicates image-level corruption (not transient).

        Critical failures trigger immediate suspect flagging (threshold=1).
        Transient types (NETWORK_TIMEOUT, OUT_OF_MEMORY) require 2+ occurrences.
        """
        return (
            self.failure_type in _CRITICAL_FAILURE_TYPES
            and self.confidence >= 0.8
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for logging and inclusion in recovery result dict."""
        return {
            "failure_type": self.failure_type.value,
            "confidence": round(self.confidence, 2),
            "detected_patterns": self.detected_patterns,
            "vm_name": self.vm_name,
            "stall_progress": round(self.stall_progress, 1),
            "is_critical": self.is_critical_failure,
            "serial_tail_lines": len(self.serial_output_tail.split('\n')),
        }


class GoldenImageBuilder:
    """
    v224.0: Enterprise-Grade Golden Image Builder.
    
    Creates pre-baked VM images with everything installed:
    - Python environment with all dependencies
    - JARVIS-Prime codebase
    - Pre-downloaded model files
    - Configured startup scripts
    
    This reduces VM startup from 10-15 minutes to ~30-60 seconds.
    
    Workflow:
    1. Create a "builder" VM with startup script that installs everything
    2. Wait for installation to complete (monitored via /health/startup)
    3. Stop the VM
    4. Create a machine image from the stopped VM's disk
    5. Delete the builder VM
    
    The resulting image can be used for instant-on VMs.
    """
    
    def __init__(
        self,
        config: "VMManagerConfig",
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        
        # GCP API clients (initialized lazily)
        self._instances_client: Optional[InstancesClientType] = None
        self._images_client: Any = None
        self._machine_images_client: Any = None
        
        # Build state
        self._current_build_vm: Optional[str] = None
        self._build_in_progress: bool = False
        
    async def _get_instances_client(self) -> Optional[InstancesClientType]:
        """Lazy initialization of GCP Instances client."""
        if not COMPUTE_AVAILABLE:
            return None
        if self._instances_client is None:
            self._instances_client = compute_v1.InstancesClient()
        return self._instances_client
    
    async def _get_images_client(self) -> Optional[Any]:
        """Lazy initialization of GCP Images client."""
        if not COMPUTE_AVAILABLE:
            return None
        if self._images_client is None:
            self._images_client = compute_v1.ImagesClient()
        return self._images_client
    
    async def _get_machine_images_client(self) -> Optional[Any]:
        """Lazy initialization of GCP Machine Images client."""
        if not COMPUTE_AVAILABLE:
            return None
        if self._machine_images_client is None:
            self._machine_images_client = compute_v1.MachineImagesClient()
        return self._machine_images_client
    
    def _discover_prime_repo_url(self) -> str:
        """
        v228.0: Dynamically discover JARVIS-Prime repository URL.
        
        Discovery order (first match wins, zero hardcoded usernames):
          1. JARVIS_PRIME_REPO_URL environment variable (explicit override)
          2. JARVIS_REPO_URL environment variable (legacy/metadata compat)
          3. Auto-detect from local jarvis-prime sibling repo's git remote
          4. Fail with clear error — never silently use a wrong URL
        
        Returns:
            Repository URL string, or empty string if discovery fails
        """
        # 1. Explicit environment variables (highest priority)
        for env_key in ("JARVIS_PRIME_REPO_URL", "JARVIS_REPO_URL"):
            url = os.getenv(env_key)
            if url:
                self.logger.info(f"[GoldenImageBuilder] Repo URL from {env_key}: {url}")
                return url
        
        # 2. Auto-detect from local sibling repository git remote
        try:
            import subprocess
            from pathlib import Path
            
            # Search multiple potential locations for the jarvis-prime repo
            search_paths = []
            
            # Relative to this file's repo root (../../jarvis-prime)
            this_file = Path(__file__).resolve()
            repo_root = this_file.parent.parent.parent  # backend/core/ → project root
            workspace_parent = repo_root.parent  # parent of JARVIS-AI-Agent
            
            search_paths.extend([
                workspace_parent / "jarvis-prime",
                workspace_parent / "JARVIS-Prime",
            ])
            
            # Home-based paths
            home = Path.home()
            search_paths.extend([
                home / "Documents" / "repos" / "jarvis-prime",
                home / "Documents" / "repos" / "JARVIS-Prime",
            ])
            
            # Deduplicate (resolve to absolute)
            seen = set()
            unique_paths = []
            for p in search_paths:
                resolved = str(p.resolve())
                if resolved not in seen:
                    seen.add(resolved)
                    unique_paths.append(p)
            
            for path in unique_paths:
                if path.exists() and (path / ".git").exists():
                    result = subprocess.run(
                        ["git", "remote", "get-url", "origin"],
                        cwd=str(path),
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        detected_url = result.stdout.strip()
                        self.logger.info(
                            f"[GoldenImageBuilder] Auto-detected repo URL from "
                            f"{path}: {detected_url}"
                        )
                        return detected_url
        except Exception as e:
            self.logger.debug(f"[GoldenImageBuilder] Auto-detect git remote failed: {e}")
        
        # 3. No URL found — log clear error
        self.logger.warning(
            "[GoldenImageBuilder] Could not discover JARVIS-Prime repo URL. "
            "Set JARVIS_PRIME_REPO_URL environment variable for explicit configuration. "
            "The golden image builder needs a valid repo URL to clone JARVIS-Prime code."
        )
        return ""
    
    async def list_golden_images(
        self,
        family: Optional[str] = None,
        include_deprecated: bool = False,
    ) -> List[GoldenImageInfo]:
        """
        List all golden images in the project.
        
        Args:
            family: Filter by image family (default: from config)
            include_deprecated: Include deprecated images
            
        Returns:
            List of GoldenImageInfo sorted by creation time (newest first)
        """
        images_client = await self._get_images_client()
        if not images_client:
            self.logger.warning("[GoldenImageBuilder] GCP not available")
            return []
        
        family = family or self.config.golden_image_family
        project = self.config.golden_image_project or self.config.project_id
        
        try:
            # List all images in the project
            request = compute_v1.ListImagesRequest(
                project=project,
                filter=f"family={family}" if family else None,
            )
            
            images = []
            for image in images_client.list(request=request):
                # Skip deprecated images unless requested
                if not include_deprecated and image.deprecated:
                    continue
                
                # Only include images that match our naming convention
                if image.name.startswith("jarvis-prime-golden"):
                    images.append(GoldenImageInfo.from_gcp_image(image, project))
            
            # Sort by creation time (newest first)
            images.sort(key=lambda x: x.creation_time, reverse=True)
            
            self.logger.info(f"[GoldenImageBuilder] Found {len(images)} golden images")
            return images
            
        except Exception as e:
            self.logger.error(f"[GoldenImageBuilder] Error listing images: {e}")
            return []
    
    async def get_latest_golden_image(
        self,
        family: Optional[str] = None,
    ) -> Optional[GoldenImageInfo]:
        """
        Get the latest golden image from a family.
        
        Args:
            family: Image family (default: from config)
            
        Returns:
            Latest GoldenImageInfo or None if no images found
        """
        images_client = await self._get_images_client()
        if not images_client:
            return None
        
        family = family or self.config.golden_image_family
        project = self.config.golden_image_project or self.config.project_id
        
        try:
            # Use getFromFamily for efficient lookup
            request = compute_v1.GetFromFamilyImageRequest(
                project=project,
                family=family,
            )
            
            image = images_client.get_from_family(request=request)
            return GoldenImageInfo.from_gcp_image(image, project)
            
        except Exception as e:
            self.logger.debug(f"[GoldenImageBuilder] No image in family '{family}': {e}")
            return None
    
    async def check_golden_image_status(self) -> Dict[str, Any]:
        """
        Check status of golden images for operational readiness.
        
        Returns:
            Dict with status information:
            - available: bool - Whether a usable golden image exists
            - latest_image: Optional[GoldenImageInfo]
            - is_stale: bool - Whether the image is older than max age
            - recommendation: str - What action to take
        """
        latest = await self.get_latest_golden_image()
        
        if not latest:
            return {
                "available": False,
                "latest_image": None,
                "is_stale": False,
                "recommendation": "CREATE_NEW",
                "message": "No golden image found. Create one with --create-golden-image",
            }
        
        is_stale = latest.is_stale(self.config.golden_image_max_age_days)
        
        return {
            "available": True,
            "latest_image": latest,
            "is_stale": is_stale,
            "age_days": latest.age_days,
            "recommendation": "REBUILD" if is_stale else "READY",
            "message": (
                f"Golden image '{latest.name}' is {latest.age_days:.1f} days old. "
                f"{'Consider rebuilding.' if is_stale else 'Ready for use.'}"
            ),
        }
    
    def _generate_builder_startup_script(self) -> str:
        """
        v228.0: Generate the startup script for the golden image builder VM.
        
        This script runs on a temporary builder VM and pre-bakes everything:
          Phase 1: System packages (Python, build tools, git)
          Phase 2: Python virtual environment
          Phase 3: Clone JARVIS-Prime repository (CRITICAL — provides the code)
          Phase 4: Install ML dependencies (torch, transformers, llama-cpp-python)
          Phase 5: Download and cache LLM model with verification
          Phase 6: Configure for fast startup (systemd, .env, APARS)
          Phase 7: Validate build integrity and signal completion
        
        All URLs are dynamically discovered — zero hardcoded usernames.
        """
        model_name = self.config.golden_image_model
        
        # v228.0: Dynamically discover repo URL (never hardcoded)
        jarvis_prime_repo = self._discover_prime_repo_url()
        if not jarvis_prime_repo:
            self.logger.error(
                "[GoldenImageBuilder] Cannot generate builder script: "
                "no JARVIS-Prime repo URL. Set JARVIS_PRIME_REPO_URL."
            )
            raise ValueError(
                "JARVIS-Prime repository URL not found. "
                "Set JARVIS_PRIME_REPO_URL environment variable or ensure "
                "jarvis-prime repo exists locally with a git remote configured."
            )
        
        self.logger.info(f"[GoldenImageBuilder] Using repo URL: {jarvis_prime_repo}")
        
        return f'''#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# v228.0: Golden Image Builder - Enterprise-Grade Pre-bake
# ═══════════════════════════════════════════════════════════════════════════════
# Creates a golden image with code, dependencies, and models pre-installed.
# Runs on a temporary builder VM. Signals completion via health endpoint.
#
# Key improvements over v224.0:
#   - Actually clones JARVIS-Prime repository (was missing!)
#   - Model download verification (fail-fast on missing models)
#   - systemd EnvironmentFile integration
#   - Build integrity validation before signaling ready
#   - Dynamic repo URL (zero hardcoded usernames)
# ═══════════════════════════════════════════════════════════════════════════════

set -e  # Exit on error (but handle gracefully)

LOG_FILE="/var/log/golden-image-build.log"
HEALTH_FILE="/tmp/golden_image_status.json"
READY_FILE="/tmp/golden_image_ready"
MODEL_NAME="{model_name}"
REPO_URL="{jarvis_prime_repo}"
JARVIS_DIR="/opt/jarvis-prime"
MODEL_CACHE="$JARVIS_DIR/models"
BUILD_ERRORS=0

# ─────────────────────────────────────────────────────────────────────────────
# Logging and Status Functions
# ─────────────────────────────────────────────────────────────────────────────

log() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}}

update_status() {{
    local status="$1"
    local progress="$2"
    local step="$3"
    
    cat > "$HEALTH_FILE" << EOFSTATUS
{{
    "status": "$status",
    "progress_pct": $progress,
    "current_step": "$step",
    "model_name": "$MODEL_NAME",
    "repo_url": "$REPO_URL",
    "build_errors": $BUILD_ERRORS,
    "timestamp": "$(date -Iseconds)"
}}
EOFSTATUS
}}

# Start health endpoint immediately (for monitoring)
start_health_endpoint() {{
    python3 << 'EOFPY' &
import http.server
import json
import os

class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health/startup":
            try:
                with open("/tmp/golden_image_status.json", "r") as f:
                    status = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                status = {{"status": "booting", "progress_pct": 0, "current_step": "Starting..."}}
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(status).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 8000), HealthHandler)
    server.serve_forever()
EOFPY
}}

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: System Setup
# ─────────────────────────────────────────────────────────────────────────────

log "═══════════════════════════════════════════════════════════════════════════════"
log "GOLDEN IMAGE BUILD v228.0 STARTED"
log "   Repo URL:  $REPO_URL"
log "   Model:     $MODEL_NAME"
log "═══════════════════════════════════════════════════════════════════════════════"

update_status "booting" 0 "Starting golden image build..."

start_health_endpoint
sleep 2

update_status "installing" 3 "Installing system packages..."
log "Phase 1: Installing system packages"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \\
    python3.11 python3.11-venv python3.11-dev python3-pip \\
    build-essential cmake ninja-build \\
    git curl wget \\
    libssl-dev libffi-dev \\
    2>&1 | tee -a "$LOG_FILE"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Python Environment
# ─────────────────────────────────────────────────────────────────────────────

update_status "installing" 10 "Setting up Python environment..."
log "Phase 2: Setting up Python environment"

mkdir -p "$JARVIS_DIR"
cd "$JARVIS_DIR"

python3.11 -m venv venv
source venv/bin/activate

pip install --upgrade pip setuptools wheel 2>&1 | tee -a "$LOG_FILE"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: Clone JARVIS-Prime Repository (CRITICAL)
# ─────────────────────────────────────────────────────────────────────────────

update_status "cloning" 15 "Cloning JARVIS-Prime repository..."
log "Phase 3: Cloning JARVIS-Prime from $REPO_URL"

# Clone into a temporary directory, then move contents to JARVIS_DIR
CLONE_DIR="/tmp/jarvis-prime-clone"
rm -rf "$CLONE_DIR"

CLONE_SUCCESS=false
for attempt in 1 2 3; do
    log "   Clone attempt $attempt/3..."
    # v233.3: Timeout to prevent indefinite hang
    if timeout 120 git clone --depth 1 "$REPO_URL" "$CLONE_DIR" 2>&1 | tee -a "$LOG_FILE"; then
        CLONE_SUCCESS=true
        break
    fi
    log "   Clone attempt $attempt failed. Retrying in 5s..."
    sleep 5
done

if [ "$CLONE_SUCCESS" = true ]; then
    # Move cloned code into JARVIS_DIR (preserve venv which is already there)
    # Use rsync-style approach: copy everything except .git to save space
    cp -a "$CLONE_DIR"/.git "$JARVIS_DIR/.git" 2>/dev/null || true
    # Copy all files except .git directory
    find "$CLONE_DIR" -maxdepth 1 -not -name '.git' -not -path "$CLONE_DIR" -exec cp -a {{}} "$JARVIS_DIR/" \\;
    rm -rf "$CLONE_DIR"
    
    # Verify critical files exist
    if [ -d "$JARVIS_DIR/jarvis_prime" ]; then
        log "   ✅ JARVIS-Prime code cloned successfully"
        log "   ✅ jarvis_prime module found at $JARVIS_DIR/jarvis_prime"
    else
        log "   ⚠️  Clone succeeded but jarvis_prime module not found!"
        log "   ⚠️  Contents of $JARVIS_DIR:"
        ls -la "$JARVIS_DIR" 2>&1 | tee -a "$LOG_FILE"
        BUILD_ERRORS=$((BUILD_ERRORS + 1))
    fi
else
    log "   ❌ CRITICAL: Failed to clone JARVIS-Prime after 3 attempts!"
    log "   ❌ Repo URL: $REPO_URL"
    log "   ❌ The golden image will NOT have inference code!"
    BUILD_ERRORS=$((BUILD_ERRORS + 1))
    update_status "error" 15 "Git clone failed: $REPO_URL"
fi

# Install jarvis-prime requirements if they exist (from the cloned repo)
if [ -f "$JARVIS_DIR/requirements.txt" ]; then
    log "   Installing JARVIS-Prime requirements..."
    update_status "installing" 20 "Installing JARVIS-Prime requirements..."
    pip install -r "$JARVIS_DIR/requirements.txt" 2>&1 | tee -a "$LOG_FILE" || true
elif [ -f "$JARVIS_DIR/pyproject.toml" ]; then
    log "   Installing JARVIS-Prime from pyproject.toml..."
    update_status "installing" 20 "Installing JARVIS-Prime package..."
    pip install -e "$JARVIS_DIR" 2>&1 | tee -a "$LOG_FILE" || true
fi

# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: Install ML Dependencies
# ─────────────────────────────────────────────────────────────────────────────

update_status "installing" 25 "Installing ML dependencies..."
log "Phase 4: Installing ML dependencies"

pip install \\
    torch>=2.1.0 \\
    transformers>=4.35.0 \\
    accelerate>=0.24.0 \\
    bitsandbytes>=0.41.0 \\
    sentencepiece>=0.1.99 \\
    safetensors>=0.4.0 \\
    huggingface_hub>=0.19.0 \\
    llama-cpp-python>=0.2.20 \\
    2>&1 | tee -a "$LOG_FILE"

update_status "installing" 40 "Installing server dependencies..."
log "Phase 4b: Installing server dependencies"

pip install \\
    fastapi>=0.104.0 \\
    uvicorn[standard]>=0.24.0 \\
    httpx>=0.25.0 \\
    pydantic>=2.5.0 \\
    aiohttp>=3.9.0 \\
    python-multipart>=0.0.6 \\
    2>&1 | tee -a "$LOG_FILE"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 5: v241.0 — Download Multi-Model Inventory (with Verification & Manifest)
# ─────────────────────────────────────────────────────────────────────────────

update_status "downloading" 50 "Downloading multi-model inventory..."
log "Phase 5: v241.0 — Downloading 8 models for task-type routing"

mkdir -p "$MODEL_CACHE"

python3 << EOFMODEL
import os
import sys
import json
from datetime import datetime

# v241.0: Multi-model inventory for GCP task-type routing
# Each model is downloaded individually as a specific GGUF file.
MODELS = {{
    "mistral-7b": {{
        "repo": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
        "filename": "mistral-7b-instruct-v0.2.Q4_K_M.gguf",
        "size_gb": 4.4,
        "routable": True,
        "config_overrides": {{"n_ctx": 8192, "chat_template": "mistral", "n_gpu_layers": 0, "flash_attn": False}},
    }},
    "qwen-2.5-7b": {{
        "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
        "size_gb": 4.4,
        "routable": True,
        "config_overrides": {{"n_ctx": 32768, "chat_template": "chatml", "n_gpu_layers": 0, "flash_attn": False}},
    }},
    "qwen-2.5-coder-7b": {{
        "repo": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
        "filename": "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
        "size_gb": 4.4,
        "routable": True,
        "config_overrides": {{"n_ctx": 32768, "chat_template": "chatml", "n_gpu_layers": 0, "flash_attn": False}},
    }},
    "llava-v1.6-mistral-7b": {{
        "repo": "cjpais/llava-v1.6-mistral-7b-gguf",
        "filename": "llava-v1.6-mistral-7b.Q4_K_M.gguf",
        "size_gb": 4.9,
        "routable": False,
    }},
    "phi-3.5-mini": {{
        "repo": "bartowski/Phi-3.5-mini-instruct-GGUF",
        "filename": "Phi-3.5-mini-instruct-Q4_K_M.gguf",
        "size_gb": 2.2,
        "routable": True,
        "config_overrides": {{"n_ctx": 4096, "chat_template": "phi3", "n_gpu_layers": 0, "flash_attn": False}},
    }},
    "llama-3.1-8b": {{
        "repo": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        "filename": "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        "size_gb": 4.9,
        "routable": True,
        "config_overrides": {{"n_ctx": 8192, "chat_template": "llama3", "n_gpu_layers": 0, "flash_attn": False}},
    }},
    "tinyllama-1.1b": {{
        "repo": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
        "filename": "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
        "size_gb": 0.67,
        "routable": False,
    }},
    "bge-large-en": {{
        "repo": "TaylorAI/bge-large-en-v1.5-gguf",
        "filename": "bge-large-en-v1.5-q4_k_m.gguf",
        "size_gb": 0.17,
        "routable": False,
    }},
    # v241.1: Advanced reasoning & specialist models
    "deepseek-r1-qwen-7b": {{
        "repo": "bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF",
        "filename": "DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf",
        "size_gb": 4.4,
        "routable": True,
        "config_overrides": {{"n_ctx": 32768, "chat_template": "chatml", "n_gpu_layers": 0, "flash_attn": False}},
    }},
    "gemma-2-9b": {{
        "repo": "bartowski/gemma-2-9b-it-GGUF",
        "filename": "gemma-2-9b-it-Q4_K_M.gguf",
        "size_gb": 5.5,
        "routable": True,
        "config_overrides": {{"n_ctx": 8192, "chat_template": "gemma", "n_gpu_layers": 0, "flash_attn": False}},
    }},
    "qwen-2.5-math-7b": {{
        "repo": "Qwen/Qwen2.5-Math-7B-Instruct-GGUF",
        "filename": "qwen2.5-math-7b-instruct-q4_k_m.gguf",
        "size_gb": 4.4,
        "routable": True,
        "config_overrides": {{"n_ctx": 4096, "chat_template": "chatml", "n_gpu_layers": 0, "flash_attn": False}},
    }},
}}

models_dir = "/opt/jarvis-prime/models"
os.makedirs(models_dir, exist_ok=True)

from huggingface_hub import hf_hub_download

manifest = {{"models": {{}}, "build_timestamp": datetime.utcnow().isoformat(), "version": "v241.1"}}
total = len(MODELS)
success_count = 0
fail_count = 0

for i, (model_id, spec) in enumerate(MODELS.items()):
    target_path = os.path.join(models_dir, spec["filename"])
    pct = int(((i + 1) / total) * 100)
    print(f"[{{i+1}}/{{total}}] Downloading {{model_id}} ({{spec['size_gb']}} GB)...")

    if os.path.exists(target_path) and os.path.getsize(target_path) > 1_000_000:
        print(f"  Already exists: {{target_path}} ({{os.path.getsize(target_path) / (1024**3):.2f}} GB)")
        success_count += 1
    else:
        try:
            hf_hub_download(
                repo_id=spec["repo"],
                filename=spec["filename"],
                local_dir=models_dir,
                local_dir_use_symlinks=False,
            )
            if os.path.exists(target_path):
                print(f"  Downloaded: {{os.path.getsize(target_path) / (1024**3):.2f}} GB")
                success_count += 1
            else:
                print(f"  WARNING: File not found after download: {{target_path}}")
                fail_count += 1
                continue
        except Exception as e:
            print(f"  FAILED: {{e}}")
            fail_count += 1
            continue

    manifest["models"][model_id] = {{
        "file": spec["filename"],
        "size_gb": spec["size_gb"],
        "path": target_path,
        "routable": spec.get("routable", True),
        "config_overrides": spec.get("config_overrides", {{}}),
    }}

# Write manifest (primary inventory source at runtime)
manifest_path = os.path.join(models_dir, "manifest.json")
with open(manifest_path, "w") as f:
    json.dump(manifest, f, indent=2)
print(f"Manifest written: {{manifest_path}}")

print(f"Results: {{success_count}}/{{total}} models downloaded, {{fail_count}} failed")
if fail_count > 0:
    print("WARNING: Some models failed to download but image creation continues")
if success_count == 0:
    print("CRITICAL: No models downloaded!")
    sys.exit(1)
EOFMODEL

if [ $? -ne 0 ]; then
    log "   ⚠️  Multi-model download had failures"
    BUILD_ERRORS=$((BUILD_ERRORS + 1))
else
    log "   ✅ Multi-model inventory downloaded and manifest written"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Phase 6: Configure for Fast Startup
# ─────────────────────────────────────────────────────────────────────────────

update_status "configuring" 80 "Configuring for fast startup..."
log "Phase 6: Configuring for fast startup"

# Create environment file (sourced by systemd and startup script)
cat > "$JARVIS_DIR/.env" << EOFENV
# ═══════════════════════════════════════════════════════════════════════════════
# Pre-baked Golden Image Configuration (v228.0)
# Generated at: $(date -Iseconds)
# ═══════════════════════════════════════════════════════════════════════════════
JARVIS_DEPS_PREBAKED=true
JARVIS_SKIP_ML_DEPS_INSTALL=true
JARVIS_GCP_INFERENCE=true
JARVIS_MODEL_CACHE=$MODEL_CACHE
GCP_MODELS_DIR=$MODEL_CACHE
GCP_MULTI_MODEL_ENABLED=true
JARVIS_PRIME_DIR=$JARVIS_DIR
JARVIS_PRIME_VENV=$JARVIS_DIR/venv
JARVIS_PRIME_REPO_URL=$REPO_URL
PYTHONPATH=$JARVIS_DIR
EOFENV

log "   ✅ Environment file created at $JARVIS_DIR/.env"

# Create systemd service with EnvironmentFile integration
cat > /etc/systemd/system/jarvis-prime.service << EOFSVC
[Unit]
Description=JARVIS-Prime Inference Server (Golden Image v228.0)
After=network.target
Documentation=https://github.com/drussell23/jarvis-prime

[Service]
Type=simple
User=root
WorkingDirectory=$JARVIS_DIR
EnvironmentFile=$JARVIS_DIR/.env
Environment=PATH=$JARVIS_DIR/venv/bin:/usr/bin:/bin
ExecStart=$JARVIS_DIR/venv/bin/python -m jarvis_prime.server
Restart=on-failure
RestartSec=5
TimeoutStartSec=120
StandardOutput=append:/var/log/jarvis-prime.log
StandardError=append:/var/log/jarvis-prime.log

[Install]
WantedBy=multi-user.target
EOFSVC

systemctl daemon-reload
log "   ✅ systemd service configured with EnvironmentFile"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 7: Build Integrity Validation and Cleanup
# ─────────────────────────────────────────────────────────────────────────────

update_status "finalizing" 90 "Validating build integrity..."
log "Phase 7: Validating build integrity"

# Validate critical components
VALIDATION_PASSED=true

# Check 1: Python and venv
if [ ! -f "$JARVIS_DIR/venv/bin/python" ]; then
    log "   ❌ FAIL: Python venv not found"
    VALIDATION_PASSED=false
    BUILD_ERRORS=$((BUILD_ERRORS + 1))
else
    log "   ✅ Python venv: OK"
fi

# Check 2: Core ML packages importable
$JARVIS_DIR/venv/bin/python -c "import torch; print(f'   ✅ PyTorch {{torch.__version__}}')" 2>&1 | tee -a "$LOG_FILE" || {{
    log "   ❌ FAIL: PyTorch not importable"
    VALIDATION_PASSED=false
    BUILD_ERRORS=$((BUILD_ERRORS + 1))
}}

$JARVIS_DIR/venv/bin/python -c "import transformers; print(f'   ✅ Transformers {{transformers.__version__}}')" 2>&1 | tee -a "$LOG_FILE" || {{
    log "   ❌ FAIL: Transformers not importable"
    VALIDATION_PASSED=false
    BUILD_ERRORS=$((BUILD_ERRORS + 1))
}}

$JARVIS_DIR/venv/bin/python -c "import llama_cpp; print('   ✅ llama-cpp-python: OK')" 2>&1 | tee -a "$LOG_FILE" || {{
    log "   ⚠️  llama-cpp-python not importable (optional)"
}}

# Check 3: JARVIS-Prime code present
if [ -d "$JARVIS_DIR/jarvis_prime" ]; then
    log "   ✅ jarvis_prime module: OK"
else
    log "   ❌ FAIL: jarvis_prime module not found"
    VALIDATION_PASSED=false
    BUILD_ERRORS=$((BUILD_ERRORS + 1))
fi

# Check 4: Model cache has content
MODEL_SIZE=$(du -sm "$MODEL_CACHE" 2>/dev/null | cut -f1 || echo "0")
if [ "$MODEL_SIZE" -gt 0 ]; then
    log "   ✅ Model cache: ${{MODEL_SIZE}}MB"
else
    log "   ⚠️  Model cache empty (models will download at first use)"
fi

# Check 5: Environment file
if [ -f "$JARVIS_DIR/.env" ]; then
    log "   ✅ Environment file: OK"
else
    log "   ❌ FAIL: Environment file missing"
    VALIDATION_PASSED=false
    BUILD_ERRORS=$((BUILD_ERRORS + 1))
fi

# Check 6: systemd service
if systemctl list-unit-files | grep -q jarvis-prime; then
    log "   ✅ systemd service: registered"
else
    log "   ⚠️  systemd service not registered"
fi

# Cleanup
update_status "finalizing" 95 "Cleaning up..."
apt-get clean
rm -rf /var/lib/apt/lists/*
rm -rf ~/.cache/pip

if [ "$VALIDATION_PASSED" = true ]; then
    update_status "ready" 100 "Golden image ready! ($BUILD_ERRORS warnings)"
    log "═══════════════════════════════════════════════════════════════════════════════"
    log "✅ GOLDEN IMAGE BUILD v228.0 COMPLETED SUCCESSFULLY"
    log "   Build errors/warnings: $BUILD_ERRORS"
    log "═══════════════════════════════════════════════════════════════════════════════"
else
    update_status "error" 99 "Build completed with $BUILD_ERRORS critical errors"
    log "═══════════════════════════════════════════════════════════════════════════════"
    log "⚠️  GOLDEN IMAGE BUILD COMPLETED WITH ERRORS ($BUILD_ERRORS)"
    log "   The image may not function correctly. Review errors above."
    log "═══════════════════════════════════════════════════════════════════════════════"
fi

# Signal completion (even with errors — let the caller decide)
touch "$READY_FILE"

log "Build complete. Health endpoint running on :8000"
wait
'''

    async def create_golden_image(
        self,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Tuple[bool, str, Optional[GoldenImageInfo]]:
        """
        Create a new golden image with everything pre-installed.
        
        This is a long-running operation (10-20 minutes) that:
        1. Creates a builder VM
        2. Waits for installation to complete
        3. Stops the VM
        4. Creates a machine image
        5. Cleans up the builder VM
        
        Args:
            progress_callback: Optional callback(progress_pct, message)
            
        Returns:
            Tuple of (success, message, GoldenImageInfo or None)
        """
        if self._build_in_progress:
            return False, "Build already in progress", None
        
        self._build_in_progress = True
        instances_client = await self._get_instances_client()
        images_client = await self._get_images_client()
        
        if not instances_client or not images_client:
            self._build_in_progress = False
            return False, "GCP API not available", None
        
        project = self.config.golden_image_project or self.config.project_id
        zone = self.config.zone
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        builder_vm_name = f"jarvis-golden-builder-{timestamp}"
        image_name = f"jarvis-prime-golden-{timestamp}"
        
        self._current_build_vm = builder_vm_name
        
        def report_progress(pct: int, msg: str):
            self.logger.info(f"[GoldenImageBuilder] {pct}% - {msg}")
            if progress_callback:
                progress_callback(pct, msg)
        
        try:
            # ─────────────────────────────────────────────────────────────────
            # Phase 1: Create Builder VM
            # ─────────────────────────────────────────────────────────────────
            report_progress(5, f"Creating builder VM: {builder_vm_name}")
            
            # Generate startup script
            startup_script = self._generate_builder_startup_script()
            
            # Machine type for builder (needs enough RAM for model)
            machine_type = f"zones/{zone}/machineTypes/{self.config.golden_image_builder_machine_type}"
            
            # Disk configuration (large enough for model + deps)
            # IMPORTANT: auto_delete=False to preserve disk for image creation
            boot_disk = compute_v1.AttachedDisk(
                auto_delete=False,  # Keep disk for image creation!
                boot=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    disk_size_gb=100,  # 100GB for model + deps
                    disk_type=f"zones/{zone}/diskTypes/pd-ssd",  # SSD for faster builds
                    source_image=f"projects/{self.config.image_project}/global/images/family/{self.config.image_family}",
                ),
            )
            
            # Network interface
            network_interface = compute_v1.NetworkInterface(
                network=f"global/networks/{self.config.network}",
                access_configs=[compute_v1.AccessConfig(name="External NAT", type="ONE_TO_ONE_NAT")],
            )
            
            # Service account
            service_account = compute_v1.ServiceAccount(
                email="default",
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            
            # Create instance
            instance = compute_v1.Instance(
                name=builder_vm_name,
                machine_type=machine_type,
                disks=[boot_disk],
                network_interfaces=[network_interface],
                service_accounts=[service_account],
                metadata=compute_v1.Metadata(items=[
                    compute_v1.Items(key="startup-script", value=startup_script),
                    compute_v1.Items(key="jarvis-build-type", value="golden-image"),
                    compute_v1.Items(key="jarvis-model", value=self.config.golden_image_model),
                ]),
                tags=compute_v1.Tags(items=["jarvis", "golden-image-builder"]),
                labels={
                    "created-by": "jarvis",
                    "type": "golden-image-builder",
                    "model-name": self.config.golden_image_model.replace("/", "-").replace(".", "-").lower(),
                },
            )
            
            # Insert instance
            operation = instances_client.insert(
                project=project,
                zone=zone,
                instance_resource=instance,
            )
            
            # Wait for VM creation
            report_progress(10, "Waiting for VM to start...")
            await asyncio.sleep(30)  # Wait for VM to boot
            
            # ─────────────────────────────────────────────────────────────────
            # Phase 2: Monitor Build Progress
            # ─────────────────────────────────────────────────────────────────
            report_progress(15, "Monitoring build progress...")
            
            # Get VM IP for health checks
            vm_ip = None
            for _ in range(30):  # Try for 5 minutes
                try:
                    instance = instances_client.get(
                        project=project,
                        zone=zone,
                        instance=builder_vm_name,
                    )
                    if instance.network_interfaces:
                        access_configs = instance.network_interfaces[0].access_configs
                        if access_configs:
                            vm_ip = access_configs[0].nat_i_p
                            if vm_ip:
                                break
                except Exception:
                    pass
                await asyncio.sleep(10)
            
            if not vm_ip:
                raise RuntimeError("Failed to get VM IP address")
            
            report_progress(20, f"Builder VM IP: {vm_ip}")
            
            # Poll health endpoint until build completes
            # The health endpoint may not be accessible from outside GCP (firewall),
            # so we use a timeout-based approach with graceful fallback.
            health_url = f"http://{vm_ip}:8000/health/startup"
            max_build_time = 20 * 60  # 20 minutes max (typical build is 10-15 min)
            min_build_time = 10 * 60  # Wait at least 10 minutes before giving up
            start_time = time.time()
            last_progress = 0
            consecutive_failures = 0
            max_consecutive_failures = 20  # Give up on health checks after 20 failures (~10 min)
            
            if AIOHTTP_AVAILABLE:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    while time.time() - start_time < max_build_time:
                        try:
                            async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                if resp.status == 200:
                                    consecutive_failures = 0  # Reset on success
                                    data = await resp.json()
                                    status = data.get("status", "unknown")
                                    build_progress = data.get("progress_pct", 0)
                                    current_step = data.get("current_step", "Building...")
                                    
                                    # Map build progress (0-100) to our progress (20-80)
                                    mapped_progress = 20 + int(build_progress * 0.6)
                                    if mapped_progress > last_progress:
                                        last_progress = mapped_progress
                                        report_progress(mapped_progress, current_step)
                                    
                                    if status == "ready":
                                        report_progress(80, "Build completed successfully!")
                                        break
                                    elif status == "error":
                                        raise RuntimeError(f"Build failed: {data.get('error_details', 'Unknown error')}")
                        except Exception as e:
                            consecutive_failures += 1
                            self.logger.debug(f"Health check failed ({consecutive_failures}/{max_consecutive_failures}): {e}")
                            
                            # If health endpoint is consistently unreachable (likely firewall),
                            # and we've waited the minimum time, proceed anyway
                            elapsed = time.time() - start_time
                            if consecutive_failures >= max_consecutive_failures and elapsed >= min_build_time:
                                self.logger.info(
                                    f"[GoldenImageBuilder] Health endpoint unreachable after {int(elapsed/60)}min. "
                                    f"Proceeding with image creation (build likely complete)."
                                )
                                report_progress(75, "Build time elapsed - proceeding...")
                                break
                        
                        await asyncio.sleep(30)  # Poll every 30 seconds
            else:
                # Fallback: wait a fixed time
                report_progress(40, "Waiting for build (no aiohttp)...")
                await asyncio.sleep(15 * 60)  # Wait 15 minutes
            
            # ─────────────────────────────────────────────────────────────────
            # Phase 3: Stop VM and Create Image
            # ─────────────────────────────────────────────────────────────────
            report_progress(85, "Stopping builder VM...")
            
            # Stop the VM
            stop_operation = instances_client.stop(
                project=project,
                zone=zone,
                instance=builder_vm_name,
            )
            
            # Wait for VM to stop
            for _ in range(30):  # Wait up to 5 minutes
                try:
                    instance = instances_client.get(
                        project=project,
                        zone=zone,
                        instance=builder_vm_name,
                    )
                    if instance.status == "TERMINATED":
                        break
                except Exception:
                    pass
                await asyncio.sleep(10)
            
            report_progress(90, f"Creating image: {image_name}...")
            
            # Create image from the VM's boot disk
            # Get the source disk
            instance = instances_client.get(
                project=project,
                zone=zone,
                instance=builder_vm_name,
            )
            
            source_disk = instance.disks[0].source
            
            # Create image
            image_resource = compute_v1.Image(
                name=image_name,
                family=self.config.golden_image_family,
                source_disk=source_disk,
                labels={
                    "created-by": "jarvis",
                    "type": "golden-image",
                    "model-name": self.config.golden_image_model.replace("/", "-").replace(".", "-").lower(),
                    "model-version": "latest",
                    "jarvis-version": os.getenv("JARVIS_VERSION", "unknown"),
                    "source-vm": builder_vm_name,
                },
                description=f"JARVIS-Prime golden image with {self.config.golden_image_model} pre-loaded",
            )
            
            image_operation = images_client.insert(
                project=project,
                image_resource=image_resource,
            )
            
            # Wait for image creation
            report_progress(95, "Waiting for image creation...")
            await asyncio.sleep(60)  # Images typically take ~1 minute to create
            
            # ─────────────────────────────────────────────────────────────────
            # Phase 4: Cleanup
            # ─────────────────────────────────────────────────────────────────
            report_progress(98, "Cleaning up builder VM...")
            
            # Delete the builder VM
            delete_operation = instances_client.delete(
                project=project,
                zone=zone,
                instance=builder_vm_name,
            )
            
            # Get the created image info
            image_info = await self.get_latest_golden_image()
            
            report_progress(100, f"Golden image created: {image_name}")
            
            self._build_in_progress = False
            self._current_build_vm = None
            
            return True, f"Successfully created golden image: {image_name}", image_info
            
        except Exception as e:
            self.logger.error(f"[GoldenImageBuilder] Error creating golden image: {e}")
            
            # Attempt cleanup
            try:
                if self._current_build_vm:
                    instances_client.delete(
                        project=project,
                        zone=zone,
                        instance=self._current_build_vm,
                    )
            except Exception:
                pass
            
            self._build_in_progress = False
            self._current_build_vm = None
            
            return False, f"Failed to create golden image: {e}", None
    
    async def delete_golden_image(
        self,
        image_name: str,
    ) -> Tuple[bool, str]:
        """
        Delete a golden image.
        
        Args:
            image_name: Name of the image to delete
            
        Returns:
            Tuple of (success, message)
        """
        images_client = await self._get_images_client()
        if not images_client:
            return False, "GCP API not available"
        
        project = self.config.golden_image_project or self.config.project_id
        
        try:
            images_client.delete(
                project=project,
                image=image_name,
            )
            return True, f"Deleted golden image: {image_name}"
        except Exception as e:
            return False, f"Failed to delete image: {e}"
    
    async def cleanup_old_images(
        self,
        keep_count: int = 3,
    ) -> Tuple[int, List[str]]:
        """
        Clean up old golden images, keeping only the most recent ones.
        
        Args:
            keep_count: Number of recent images to keep (default: 3)
            
        Returns:
            Tuple of (deleted_count, list of deleted image names)
        """
        images = await self.list_golden_images()
        
        if len(images) <= keep_count:
            return 0, []
        
        # Images are sorted newest first, so delete from keep_count onwards
        images_to_delete = images[keep_count:]
        deleted = []
        
        for image in images_to_delete:
            success, msg = await self.delete_golden_image(image.name)
            if success:
                deleted.append(image.name)
                self.logger.info(f"[GoldenImageBuilder] Deleted old image: {image.name}")
        
        return len(deleted), deleted


class GCPVMManager:
    """
    Advanced GCP Spot VM auto-creation and lifecycle manager.

    Features:
    - Circuit breaker pattern for fault tolerance
    - Parallel async operations where safe
    - Dynamic configuration from environment
    - Robust error handling with retries
    - Cost tracking integration
    - Health monitoring with auto-recovery
    - v9.0: Intelligent quota checking before VM creation

    Integrates with:
    - intelligent_gcp_optimizer: For VM creation decisions
    - cost_tracker: For billing tracking
    - platform_memory_monitor: For memory pressure detection
    """

    def __init__(self, config: Optional[VMManagerConfig] = None):
        self.config = config or VMManagerConfig()

        # API clients (initialized lazily)
        # Use type aliases that work even when compute_v1 is not available
        self.instances_client: Optional[InstancesClientType] = None
        self.zones_client: Optional[ZonesClientType] = None
        self.zone_operations_client = None  # For polling operation status
        self.regions_client = None  # For quota checking

        # Integrations (initialized safely)
        self.cost_tracker: Optional[Any] = None  # CostTracker or None
        self.gcp_optimizer: Optional[Any] = None

        # State tracking with thread-safe locks
        self.managed_vms: Dict[str, VMInstance] = {}
        self.creating_vms: Dict[str, asyncio.Task] = {}
        self._vm_lock = asyncio.Lock()  # Protect VM state modifications
        self._init_lock = asyncio.Lock()  # Protect initialization

        # v193.2: Smart VM creation waiting - prevents "blocked: another creation in progress" errors
        # When multiple callers try to create a VM concurrently (e.g., main startup + background retry),
        # the second caller should WAIT for the first creation to complete instead of failing immediately.
        # _creation_event: Set when a creation completes (success or failure)
        # _creation_result: Stores the result of the in-progress creation for waiting callers
        self._creation_event: Optional[asyncio.Event] = None
        self._creation_result: Optional[VMInstance] = None
        self._creation_error: Optional[str] = None

        # v9.0: Quota cache to avoid repeated API calls
        self._quota_cache: Dict[str, QuotaInfo] = {}
        self._quota_cache_lock = asyncio.Lock()
        self._quota_exceeded_until: float = 0  # Cooldown after quota exceeded
        self._quota_cooldown_seconds: int = 300  # 5 minute cooldown

        # Circuit breakers for fault tolerance
        self._circuit_breakers = {
            "vm_create": CircuitBreaker(
                name="vm_create",
                failure_threshold=self.config.circuit_failure_threshold,
                recovery_timeout=self.config.circuit_recovery_timeout,
            ),
            "vm_delete": CircuitBreaker(
                name="vm_delete",
                failure_threshold=self.config.circuit_failure_threshold,
                recovery_timeout=self.config.circuit_recovery_timeout,
            ),
            "cost_tracker": CircuitBreaker(
                name="cost_tracker",
                failure_threshold=5,  # More lenient for non-critical operations
                recovery_timeout=30.0,
            ),
            "quota_check": CircuitBreaker(
                name="quota_check",
                failure_threshold=3,
                recovery_timeout=60.0,
            ),
        }

        # v232.0: Golden image corruption detection
        self._golden_image_stall_history: Dict[str, List[float]] = {}
        self._golden_image_suspect_set: set = set()
        self._golden_image_corruption_threshold = int(
            os.environ.get("JARVIS_GOLDEN_IMAGE_CORRUPTION_THRESHOLD", "3")
        )
        self._golden_image_stall_tolerance_pct = float(
            os.environ.get("JARVIS_GOLDEN_IMAGE_STALL_TOLERANCE_PCT", "5.0")
        )

        # v233.0: Diagnostic signature tracking for enhanced corruption detection
        self._golden_image_diagnostic_history: Dict[str, List[Dict[str, Any]]] = {}
        # v233.0: Prevent concurrent golden image rebuilds
        self._golden_rebuild_in_progress: bool = False

        # Monitoring
        self.monitoring_task: Optional[asyncio.Task] = None
        self.is_monitoring = False

        # Enhanced stats
        self.stats = {
            "total_created": 0,
            "total_failed": 0,
            "quota_blocks": 0,
            "quota_checks": 0,
            "total_terminated": 0,
            "current_active": 0,
            "total_cost": 0.0,
            "circuit_breaks": 0,
            "retries": 0,
            "last_error": None,
            "last_error_time": None,
        }

        self.initialized = False
        self._initialization_error: Optional[Exception] = None

        # v86.0: State for cross-repo cloud offloading coordination
        self._cloud_offload_active: bool = False
        self._cloud_offload_reason: str = ""
        self._cloud_offload_triggered_at: Optional[float] = None

        # GCP Monitoring API metrics collector (circuit-breaker protected)
        self._metrics_collector: Optional[GCPMetricsCollector] = None
        if self.config.metrics_collection_enabled:
            try:
                self._metrics_collector = GCPMetricsCollector(
                    project_id=self.config.project_id,
                    circuit_threshold=self.config.circuit_failure_threshold,
                    circuit_timeout=self.config.circuit_recovery_timeout,
                    metrics_window_seconds=self.config.metrics_window_seconds,
                )
            except Exception as e:
                logger.warning(f"Failed to create GCPMetricsCollector: {e}")

        # IP-to-VM index for O(1) activity lookup by IP address
        self._ip_to_vm: Dict[str, str] = {}

        # v224.0: Golden Image Builder for pre-baked VM images
        self._golden_image_builder: Optional[GoldenImageBuilder] = None
        self._golden_image_cache: Optional[GoldenImageInfo] = None
        self._golden_image_cache_time: float = 0
        self._golden_image_cache_ttl: float = 300  # 5 minutes

    # =========================================================================
    # v86.0: Property accessors for clean interface
    # =========================================================================

    @property
    def enabled(self) -> bool:
        """
        Check if GCP VM Manager is enabled.

        This property provides a clean interface for external code to check
        if the manager is available for cloud offloading operations.

        Returns:
            True if GCP is enabled in config, False otherwise
        """
        return self.config.enabled

    @property
    def is_ready(self) -> bool:
        """
        Check if manager is fully initialized and ready for operations.

        Returns:
            True if enabled, initialized, and no initialization errors
        """
        return (
            self.enabled and
            self.initialized and
            self._initialization_error is None
        )

    @property
    def cloud_offload_active(self) -> bool:
        """Check if cloud offloading is currently active."""
        return self._cloud_offload_active

    @property
    def cloud_offload_reason(self) -> str:
        """Get the reason cloud offloading was activated."""
        return self._cloud_offload_reason

    def mark_cloud_offload_active(self, reason: str) -> None:
        """
        Mark cloud offloading as active with the given reason.

        Args:
            reason: Why cloud offloading was activated (e.g., "High CPU", "Low memory")
        """
        import time
        self._cloud_offload_active = True
        self._cloud_offload_reason = reason
        self._cloud_offload_triggered_at = time.time()
        logger.info(f"☁️ [GCPVMManager] Cloud offloading marked ACTIVE: {reason}")

    def mark_cloud_offload_inactive(self) -> None:
        """Mark cloud offloading as inactive."""
        self._cloud_offload_active = False
        self._cloud_offload_reason = ""
        self._cloud_offload_triggered_at = None
        logger.info("☁️ [GCPVMManager] Cloud offloading marked INACTIVE")

    # =========================================================================
    # Activity recording for efficiency tracking
    # =========================================================================

    def record_vm_activity_by_ip(self, ip_address: str) -> bool:
        """Record activity on a VM identified by IP address. O(1) lookup."""
        vm_name = self._ip_to_vm.get(ip_address)
        if vm_name and vm_name in self.managed_vms:
            self.managed_vms[vm_name].record_activity()
            return True
        return False

    def record_vm_activity_by_name(self, vm_name: str) -> bool:
        """Record activity on a VM identified by name."""
        if vm_name in self.managed_vms:
            self.managed_vms[vm_name].record_activity()
            return True
        return False

    def _update_ip_index(self, vm: VMInstance) -> None:
        """Update the IP-to-VM index when a VM's IP changes."""
        if vm.ip_address:
            self._ip_to_vm[vm.ip_address] = vm.name
        if vm.internal_ip:
            self._ip_to_vm[vm.internal_ip] = vm.name

    def get_status(self) -> Dict[str, Any]:
        """
        Get comprehensive status of the GCP VM Manager.

        Returns:
            Dict with enabled, ready, cloud_offload status, and stats
        """
        return {
            "enabled": self.enabled,
            "initialized": self.initialized,
            "is_ready": self.is_ready,
            "cloud_offload_active": self._cloud_offload_active,
            "cloud_offload_reason": self._cloud_offload_reason,
            "cloud_offload_triggered_at": self._cloud_offload_triggered_at,
            "initialization_error": str(self._initialization_error) if self._initialization_error else None,
            "config": self.config.to_dict() if hasattr(self.config, 'to_dict') else {"enabled": self.enabled},
            "stats": self.stats,
            "circuit_breakers": {
                name: {"state": str(cb.state), "failures": cb.failure_count}
                for name, cb in self._circuit_breakers.items()
            },
        }

    async def initialize(self):
        """
        Initialize GCP API clients and integrations with robust error handling.

        Uses async lock to prevent race conditions during initialization.
        Gracefully handles missing dependencies and API failures.

        v109.4: Checks for shutdown state to prevent initialization during
        interpreter shutdown, which would cause EXC_GUARD crashes.
        """
        # v109.4: CRITICAL - Don't initialize during shutdown
        # GCP client libraries use guarded FDs that cause EXC_GUARD crashes
        if _gcp_shutdown_requested:
            logger.debug("[GCPVMManager] Skipping init - shutdown in progress")
            return

        # Check for interpreter shutdown (late stage)
        try:
            if sys.modules is None:
                logger.debug("[GCPVMManager] Skipping init - interpreter shutdown detected")
                return
        except Exception:
            return

        # Quick check without lock
        if self.initialized:
            return

        async with self._init_lock:
            # Double-check after acquiring lock
            if self.initialized:
                return

            # v109.4: Check again after acquiring lock
            if _gcp_shutdown_requested:
                logger.debug("[GCPVMManager] Skipping init - shutdown detected after lock")
                return

            if not COMPUTE_AVAILABLE:
                self._initialization_error = RuntimeError(
                    "google-cloud-compute package not installed. "
                    "Install with: pip install google-cloud-compute"
                )
                logger.warning(
                    "ℹ️  GCP Compute Engine API not available. "
                    "Spot VM creation disabled."
                )
                raise self._initialization_error

            logger.info("🚀 Initializing GCP VM Manager...")

            try:
                # Initialize GCP Compute Engine clients (parallel-safe)
                await self._initialize_gcp_clients()

                # Initialize integrations (with error isolation)
                await self._initialize_integrations()

                # v134.0: Sync local VM tracking with GCP reality
                # This cleans up stale entries from previous runs that may cause 404 errors
                await self._sync_managed_vms_with_gcp()

                # Start monitoring if enabled
                if self.config.enable_monitoring:
                    self.monitoring_task = asyncio.create_task(
                        self._monitoring_loop(),
                        name="gcp_vm_monitoring"
                    )
                    logger.info("✅ VM monitoring started")

                # v235.3: Start periodic orphan cleanup (runs every 30min)
                self._orphan_cleanup_task = asyncio.create_task(
                    self._periodic_orphan_cleanup(),
                    name="gcp_orphan_cleanup"
                )

                self.initialized = True
                logger.info("✅ GCP VM Manager ready")
                logger.info(f"   Project: {self.config.project_id}")
                logger.info(f"   Zone: {self.config.zone}")
                logger.info(f"   Machine Type: {self.config.machine_type}")
                logger.info(f"   Daily Budget: ${self.config.daily_budget_usd}")

            except Exception as e:
                self._initialization_error = e
                log_component_failure(
                    "gcp-vm",
                    "Failed to initialize GCP VM Manager",
                    error=e,
                    project_id=self.config.project_id,
                    zone=self.config.zone,
                )
                raise

    async def _initialize_gcp_clients(self):
        """
        Initialize GCP API clients with robust event loop handling.

        Uses modern asyncio patterns (get_running_loop) and handles edge cases:
        - Event loop closing during shutdown
        - No running event loop (sync context)
        - Thread pool executor failures

        Falls back to synchronous initialization if async fails.
        """
        try:
            # First check if we have a running event loop and it's not closing
            try:
                loop = asyncio.get_running_loop()
                if loop.is_closed() or loop.is_running() is False:
                    raise RuntimeError("Event loop is closed or not running")
            except RuntimeError as loop_error:
                # No running loop or loop is closing - initialize synchronously
                logger.warning(
                    f"[GCPVMManager] No running event loop ({loop_error}) - "
                    "initializing GCP clients synchronously"
                )
                self._initialize_gcp_clients_sync()
                return

            # Run client initialization in thread pool to avoid blocking
            # Use asyncio.to_thread for cleaner syntax (Python 3.9+)
            try:
                self.instances_client = await asyncio.to_thread(
                    compute_v1.InstancesClient
                )
                self.zones_client = await asyncio.to_thread(
                    compute_v1.ZonesClient
                )
                self.zone_operations_client = await asyncio.to_thread(
                    compute_v1.ZoneOperationsClient
                )
            except RuntimeError as executor_error:
                # Thread pool executor may fail during shutdown
                if "cannot schedule" in str(executor_error).lower():
                    logger.warning(
                        f"[GCPVMManager] Thread pool unavailable ({executor_error}) - "
                        "falling back to sync initialization"
                    )
                    self._initialize_gcp_clients_sync()
                    return
                raise

            logger.info(f"✅ GCP API clients initialized (Project: {self.config.project_id})")

        except Exception as e:
            log_component_failure(
                "gcp-vm",
                "Failed to initialize GCP API clients",
                error=e,
            )
            raise RuntimeError(f"GCP API client initialization failed: {e}") from e

    def _initialize_gcp_clients_sync(self):
        """
        Synchronous fallback for GCP client initialization.

        Used when:
        - No running event loop (during shutdown)
        - Thread pool executor is unavailable
        - Called from synchronous context
        """
        try:
            self.instances_client = compute_v1.InstancesClient()
            self.zones_client = compute_v1.ZonesClient()
            self.zone_operations_client = compute_v1.ZoneOperationsClient()
            logger.info(
                f"✅ GCP API clients initialized synchronously "
                f"(Project: {self.config.project_id})"
            )
        except Exception as e:
            log_component_failure(
                "gcp-vm",
                "Synchronous GCP client initialization failed",
                error=e,
            )
            raise

    async def _initialize_integrations(self):
        """
        Initialize integrations with graceful fallbacks and modern asyncio patterns.

        Non-critical integrations fail gracefully without blocking core functionality.
        Uses asyncio.to_thread for clean async I/O.
        """
        # Cost tracker - non-critical, continue without it
        try:
            self.cost_tracker = get_cost_tracker()
            if self.cost_tracker:
                # Ensure cost tracker is initialized
                if hasattr(self.cost_tracker, 'initialize'):
                    await self.cost_tracker.initialize()
                logger.info("✅ Cost tracker integrated")
            else:
                logger.warning("⚠️  Cost tracker not available - cost tracking disabled")
        except Exception as e:
            logger.warning(f"⚠️  Cost tracker initialization failed (non-critical): {e}")
            self.cost_tracker = None

        # GCP optimizer - non-critical, continue without it
        try:
            self.gcp_optimizer = get_gcp_optimizer(config={"project_id": self.config.project_id})
            if self.gcp_optimizer:
                logger.info("✅ GCP optimizer integrated")
            else:
                logger.warning("⚠️  GCP optimizer not available - using fallback decisions")
        except Exception as e:
            logger.warning(f"⚠️  GCP optimizer initialization failed (non-critical): {e}")
            self.gcp_optimizer = None

            self.gcp_optimizer = None
        
        # v155.0: CRITICAL - Ensure firewall rule exists BEFORE any VM creation
        # Previous versions marked this "non-critical" but it's actually MANDATORY
        self._firewall_rule_verified = False
        await self._ensure_firewall_rule()

    async def _ensure_firewall_rule(self) -> bool:
        """
        v155.0: CRITICAL - Ensure GCP firewall rule exists for health checks.

        ROOT CAUSE FIX: Previous versions marked firewall creation as "non-critical"
        but without this rule, health checks CANNOT reach VMs, causing 100% of
        provisioning attempts to timeout with "GCP VM not ready after Xs".

        v155.0 Changes:
        - Returns bool indicating success (True) or failure (False)
        - Sets self._firewall_rule_verified flag
        - VM creation will be BLOCKED if firewall rule doesn't exist
        - Added retry logic with exponential backoff
        - Better error classification and recovery guidance

        Creates a firewall rule allowing TCP ports to VMs tagged with 'jarvis-node'.
        """
        firewall_name = "jarvis-allow-health-checks"
        max_retries = 3

        for attempt in range(max_retries):
            try:
                # Import the firewalls client
                firewalls_client = await asyncio.to_thread(compute_v1.FirewallsClient)

                # Check if firewall rule already exists
                try:
                    existing_rule = await asyncio.to_thread(
                        firewalls_client.get,
                        project=self.config.project_id,
                        firewall=firewall_name,
                    )
                    logger.info(f"[v155.0] ✅ Firewall rule '{firewall_name}' verified - health checks will work")
                    self._firewall_rule_verified = True
                    return True
                except Exception as e:
                    error_str = str(e).lower()
                    if "404" not in error_str and "not found" not in error_str:
                        # Unexpected error - might be permissions
                        if "403" in error_str or "permission" in error_str:
                            logger.error(
                                f"[v155.0] ❌ PERMISSION DENIED checking firewall rule. "
                                f"Service account needs 'compute.firewalls.get' permission.\n"
                                f"    Fix: gcloud projects add-iam-policy-binding {self.config.project_id} \\\n"
                                f"        --member='serviceAccount:YOUR_SA@...' \\\n"
                                f"        --role='roles/compute.securityAdmin'"
                            )
                        else:
                            logger.warning(f"[v155.0] Firewall check error: {e}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)  # Exponential backoff
                            continue
                        return False
                    # 404 = rule doesn't exist, we'll create it

                # Create the firewall rule
                logger.info(f"[v155.0] 🔥 Creating firewall rule '{firewall_name}' (attempt {attempt + 1}/{max_retries})...")

                firewall_rule = compute_v1.Firewall(
                    name=firewall_name,
                    description="CRITICAL: Allow health checks for JARVIS GCP VMs (v155.0)",
                    network=f"global/networks/{self.config.network}",
                    priority=1000,
                    direction="INGRESS",
                    target_tags=["jarvis-node"],
                    source_ranges=["0.0.0.0/0"],  # Required for external health checks
                    allowed=[
                        compute_v1.Allowed(
                            I_p_protocol="tcp",
                            ports=["8000", "8010", "8080", "8090", "22"],  # All JARVIS ports + SSH for debugging
                        ),
                    ],
                )

                # Insert the firewall rule
                operation = await asyncio.to_thread(
                    firewalls_client.insert,
                    project=self.config.project_id,
                    firewall_resource=firewall_rule,
                )

                # Wait for operation to complete (with timeout)
                global_ops_client = await asyncio.to_thread(compute_v1.GlobalOperationsClient)

                # Poll for completion (max 60 seconds - increased from 30)
                for poll in range(60):
                    op_result = await asyncio.to_thread(
                        global_ops_client.get,
                        project=self.config.project_id,
                        operation=operation.name,
                    )
                    if op_result.status == compute_v1.Operation.Status.DONE:
                        if op_result.error:
                            logger.error(f"[v155.0] ❌ Firewall creation failed: {op_result.error}")
                            break
                        logger.info(f"[v155.0] ✅ Firewall rule '{firewall_name}' created successfully!")
                        logger.info(f"[v155.0]    Allows TCP ports 8000,8010,8080,8090,22 to VMs with 'jarvis-node' tag")
                        self._firewall_rule_verified = True
                        return True
                    await asyncio.sleep(1)
                else:
                    logger.warning(f"[v155.0] ⚠️ Firewall creation timed out after 60s - may still complete")
                    # Optimistically mark as verified since operation started
                    self._firewall_rule_verified = True
                    return True

            except Exception as e:
                error_str = str(e).lower()

                # Classify the error for better guidance
                if "already exists" in error_str:
                    logger.info(f"[v155.0] ✅ Firewall rule already exists (race condition, OK)")
                    self._firewall_rule_verified = True
                    return True
                elif "403" in error_str or "permission" in error_str:
                    logger.error(
                        f"[v155.0] ❌ PERMISSION DENIED creating firewall rule.\n"
                        f"    The service account needs 'compute.firewalls.create' permission.\n"
                        f"    Quick fix - create manually:\n"
                        f"    gcloud compute firewall-rules create {firewall_name} \\\n"
                        f"        --allow tcp:8000,tcp:8010,tcp:8080,tcp:8090,tcp:22 \\\n"
                        f"        --target-tags jarvis-node \\\n"
                        f"        --description 'JARVIS health checks' \\\n"
                        f"        --project {self.config.project_id}"
                    )
                    return False
                elif "quota" in error_str:
                    logger.error(f"[v155.0] ❌ QUOTA EXCEEDED for firewall rules: {e}")
                    return False
                else:
                    logger.warning(f"[v155.0] ⚠️ Firewall rule creation error (attempt {attempt + 1}): {e}")

                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

        # All retries exhausted
        logger.error(
            f"[v155.0] ❌ CRITICAL: Could not ensure firewall rule exists after {max_retries} attempts.\n"
            f"    VM creation will be BLOCKED until this is resolved.\n"
            f"    Manual fix:\n"
            f"    gcloud compute firewall-rules create {firewall_name} \\\n"
            f"        --allow tcp:8000,tcp:8010,tcp:8080,tcp:8090,tcp:22 \\\n"
            f"        --target-tags jarvis-node \\\n"
            f"        --description 'JARVIS health checks' \\\n"
            f"        --project {self.config.project_id}"
        )
        return False

    async def _get_vm_serial_console_output(self, vm_name: str, lines: int = 100) -> Optional[str]:
        """
        v155.0: Get serial console output from a VM for debugging startup failures.

        This provides visibility into what's happening inside the VM when
        health checks fail. Essential for diagnosing startup script issues.

        Args:
            vm_name: Name of the VM
            lines: Number of lines to retrieve (default 100)

        Returns:
            Serial console output string, or None if unavailable
        """
        try:
            output = await asyncio.to_thread(
                self.instances_client.get_serial_port_output,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=vm_name,
                port=1,  # Primary serial port
            )

            if output and output.contents:
                # Get last N lines
                all_lines = output.contents.split('\n')
                recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                return '\n'.join(recent_lines)
            return None

        except Exception as e:
            logger.debug(f"[v155.0] Could not get serial console output for {vm_name}: {e}")
            return None

    async def diagnose_vm_startup_failure(self, vm_name: str, vm_ip: Optional[str] = None) -> Dict[str, Any]:
        """
        v155.0: Comprehensive diagnosis of VM startup failure.

        When health checks fail, this method gathers diagnostic information
        to help identify the root cause.

        Returns:
            Dict with diagnostic information including:
            - vm_state: Current VM state in GCP
            - serial_output: Last 100 lines of serial console
            - firewall_rule_exists: Whether the firewall rule exists
            - network_reachable: Whether the IP is pingable
            - health_check_results: Results of health check attempts
        """
        diagnosis: Dict[str, Any] = {
            "vm_name": vm_name,
            "vm_ip": vm_ip,
            "timestamp": datetime.now().isoformat(),
            "diagnosis_version": "v155.0",
        }

        # 1. Check VM state in GCP
        try:
            instance = await asyncio.to_thread(
                self.instances_client.get,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=vm_name,
            )
            diagnosis["vm_state"] = instance.status
            diagnosis["vm_creation_time"] = instance.creation_timestamp if hasattr(instance, 'creation_timestamp') else None

            # Get network interface info
            if instance.network_interfaces:
                ni = instance.network_interfaces[0]
                diagnosis["internal_ip"] = ni.network_i_p if hasattr(ni, 'network_i_p') else None
                if ni.access_configs:
                    diagnosis["external_ip"] = ni.access_configs[0].nat_i_p if hasattr(ni.access_configs[0], 'nat_i_p') else None
        except Exception as e:
            diagnosis["vm_state"] = f"ERROR: {e}"

        # 2. Get serial console output
        serial_output = await self._get_vm_serial_console_output(vm_name)
        if serial_output:
            diagnosis["serial_output"] = serial_output

            # Look for common failure patterns
            failure_patterns = [
                ("apt-get", "Package manager issue"),
                ("pip3 install", "Python package installation issue"),
                ("Permission denied", "Permission issue"),
                ("No space left", "Disk space issue"),
                ("Connection refused", "Network connectivity issue"),
                ("timeout", "Timeout issue"),
                ("error", "General error"),
                ("failed", "General failure"),
            ]

            detected_issues = []
            serial_lower = serial_output.lower()
            for pattern, description in failure_patterns:
                if pattern.lower() in serial_lower:
                    detected_issues.append(description)

            diagnosis["detected_issues"] = list(set(detected_issues))
        else:
            diagnosis["serial_output"] = "Unavailable"
            diagnosis["detected_issues"] = ["Could not retrieve serial console output"]

        # 3. Check firewall rule
        diagnosis["firewall_rule_verified"] = getattr(self, '_firewall_rule_verified', False)

        # 4. Log comprehensive diagnosis
        logger.error(
            f"[v155.0] 🔍 VM STARTUP FAILURE DIAGNOSIS\n"
            f"    VM: {vm_name}\n"
            f"    IP: {vm_ip}\n"
            f"    State: {diagnosis.get('vm_state', 'unknown')}\n"
            f"    Firewall Rule: {'✅ Verified' if diagnosis['firewall_rule_verified'] else '❌ NOT VERIFIED'}\n"
            f"    Detected Issues: {diagnosis.get('detected_issues', [])}\n"
            f"    Serial Output (last 20 lines):\n"
            f"    {'='*60}\n"
            f"    {chr(10).join((diagnosis.get('serial_output', 'N/A') or 'N/A').split(chr(10))[-20:])}\n"
            f"    {'='*60}"
        )

        return diagnosis

    async def _diagnose_golden_image_stall(
        self,
        vm_name: str,
        stall_progress: float,
        max_serial_lines: int = 200,
    ) -> GoldenImageDiagnostic:
        """v233.0: Diagnose why a golden image VM stalled during startup.

        Analyzes serial console output to identify specific failure patterns
        that indicate golden image corruption vs. transient failures.

        This enables:
        1. Immediate corruption flagging for known-bad patterns
        2. Better logging for troubleshooting
        3. Intelligent recovery strategies

        Args:
            vm_name: Name of the stalled VM
            stall_progress: Progress % at which the stall occurred
            max_serial_lines: Number of serial console lines to retrieve

        Returns:
            GoldenImageDiagnostic with failure type, confidence, and evidence
        """
        import re as _re

        # Fetch serial console output using existing method (line 2455)
        serial_output = await self._get_vm_serial_console_output(
            vm_name=vm_name, lines=max_serial_lines
        )

        if not serial_output:
            logger.warning(
                f"[GoldenDiagnostic] No serial output for {vm_name} — "
                f"cannot diagnose stall at {stall_progress:.0f}%"
            )
            return GoldenImageDiagnostic(
                failure_type=GoldenImageFailureType.UNKNOWN,
                serial_output_tail="[Serial console unavailable]",
                confidence=0.0,
                detected_patterns=[],
                vm_name=vm_name,
                stall_progress=stall_progress,
            )

        serial_lower = serial_output.lower()
        detected_patterns: List[str] = []
        failure_scores: Dict[GoldenImageFailureType, float] = {}

        # ── Pattern Group 1: Corrupted Model Cache ────────────────────────
        _model_patterns = [
            ("model validation failed", 0.95),
            ("model integrity check failed", 0.95),
            ("corrupted model file", 0.90),
            ("model checkpoint corrupted", 0.90),
            ("invalid model format", 0.85),
            ("gguf.*corrupt", 0.90),
            ("safetensors.*corrupt", 0.85),
            ("model file.*truncated", 0.85),
        ]
        for pattern, conf in _model_patterns:
            if ".*" in pattern:
                if _re.search(pattern, serial_lower):
                    detected_patterns.append(pattern)
                    failure_scores[GoldenImageFailureType.CORRUPTED_MODEL_CACHE] = max(
                        failure_scores.get(GoldenImageFailureType.CORRUPTED_MODEL_CACHE, 0), conf
                    )
            elif pattern in serial_lower:
                detected_patterns.append(pattern)
                failure_scores[GoldenImageFailureType.CORRUPTED_MODEL_CACHE] = max(
                    failure_scores.get(GoldenImageFailureType.CORRUPTED_MODEL_CACHE, 0), conf
                )

        # ── Pattern Group 2: Dependency Drift ─────────────────────────────
        _dep_patterns = [
            ("modulenotfounderror", 0.90),
            ("importerror", 0.85),
            ("no module named", 0.85),
            ("package.*not found", 0.75),
        ]
        for pattern, conf in _dep_patterns:
            if pattern in serial_lower:
                detected_patterns.append(pattern)
                failure_scores[GoldenImageFailureType.DEPENDENCY_DRIFT] = max(
                    failure_scores.get(GoldenImageFailureType.DEPENDENCY_DRIFT, 0), conf
                )

        # ── Pattern Group 3: Service Never Started ────────────────────────
        _startup_markers = [
            "starting jarvis", "jarvis service started",
            "apars health endpoint", "uvicorn running",
        ]
        has_startup_marker = any(m in serial_lower for m in _startup_markers)
        if not has_startup_marker and stall_progress > 10:
            detected_patterns.append("no_service_startup_marker")
            failure_scores[GoldenImageFailureType.SERVICE_NEVER_STARTED] = 0.90

        # ── Pattern Group 4: Permission Error ─────────────────────────────
        _perm_patterns = [
            ("permission denied", 0.95),
            ("operation not permitted", 0.90),
            ("access denied", 0.90),
        ]
        for pattern, conf in _perm_patterns:
            if pattern in serial_lower:
                detected_patterns.append(pattern)
                failure_scores[GoldenImageFailureType.PERMISSION_ERROR] = max(
                    failure_scores.get(GoldenImageFailureType.PERMISSION_ERROR, 0), conf
                )

        # ── Pattern Group 5: Disk Full ────────────────────────────────────
        _disk_patterns = [
            ("no space left on device", 0.95),
            ("enospc", 0.95),
        ]
        for pattern, conf in _disk_patterns:
            if pattern in serial_lower:
                detected_patterns.append(pattern)
                failure_scores[GoldenImageFailureType.DISK_FULL] = max(
                    failure_scores.get(GoldenImageFailureType.DISK_FULL, 0), conf
                )

        # ── Pattern Group 6: Out of Memory ────────────────────────────────
        _oom_patterns = [
            ("out of memory", 0.95),
            ("oom killer", 0.95),
            ("killed process", 0.80),
            ("cannot allocate memory", 0.90),
        ]
        for pattern, conf in _oom_patterns:
            if pattern in serial_lower:
                detected_patterns.append(pattern)
                failure_scores[GoldenImageFailureType.OUT_OF_MEMORY] = max(
                    failure_scores.get(GoldenImageFailureType.OUT_OF_MEMORY, 0), conf
                )

        # ── Pattern Group 7: Health Endpoint Crash ────────────────────────
        _health_patterns = [
            ("health endpoint.*failed", 0.85),
            ("apars.*crash", 0.90),
        ]
        for pattern, conf in _health_patterns:
            if _re.search(pattern, serial_lower):
                detected_patterns.append(pattern)
                failure_scores[GoldenImageFailureType.HEALTH_ENDPOINT_CRASH] = max(
                    failure_scores.get(GoldenImageFailureType.HEALTH_ENDPOINT_CRASH, 0), conf
                )

        # ── Pattern Group 8: Model Load Hang ──────────────────────────────
        if "loading model" in serial_lower and stall_progress > 40:
            last_idx = serial_lower.rfind("loading model")
            if last_idx > 0:
                after_loading = serial_output[last_idx:]
                if len(after_loading.strip().split('\n')) < 5:
                    detected_patterns.append("model_load_no_progress_after_start")
                    failure_scores[GoldenImageFailureType.MODEL_LOAD_HANG] = 0.85

        # ── Pattern Group 9: Network Timeout ──────────────────────────────
        _net_patterns = [
            ("connection timeout", 0.80),
            ("network unreachable", 0.85),
            ("timeout.*download", 0.75),
        ]
        for pattern, conf in _net_patterns:
            if pattern in serial_lower:
                detected_patterns.append(pattern)
                failure_scores[GoldenImageFailureType.NETWORK_TIMEOUT] = max(
                    failure_scores.get(GoldenImageFailureType.NETWORK_TIMEOUT, 0), conf
                )

        # ── Determine primary failure type ────────────────────────────────
        if failure_scores:
            primary_type, confidence = max(failure_scores.items(), key=lambda x: x[1])
        else:
            primary_type = GoldenImageFailureType.UNKNOWN
            confidence = 0.0
            detected_patterns.append("no_known_patterns_matched")

        diagnostic = GoldenImageDiagnostic(
            failure_type=primary_type,
            serial_output_tail=serial_output,
            confidence=confidence,
            detected_patterns=detected_patterns,
            vm_name=vm_name,
            stall_progress=stall_progress,
        )

        # Log prominently
        logger.error(
            f"[GoldenDiagnostic] Stall analysis for {vm_name}:\n"
            f"    Progress: {stall_progress:.0f}%\n"
            f"    Failure Type: {primary_type.value}\n"
            f"    Confidence: {confidence:.0%}\n"
            f"    Patterns: {detected_patterns[:5]}\n"
            f"    Critical: {'YES — likely image corruption' if diagnostic.is_critical_failure else 'No — may be transient'}\n"
            f"    Serial tail (last 10 lines):\n"
            f"    {'='*60}\n"
            f"    {chr(10).join(serial_output.split(chr(10))[-10:])}\n"
            f"    {'='*60}"
        )

        return diagnostic

    async def _sync_managed_vms_with_gcp(self):
        """
        v134.0 / v235.3: Synchronize local VM tracking with actual GCP state.

        ROOT CAUSE FIX for stale managed_vms entries AND orphaned untracked VMs:
        On startup (or after crash/re-exec), the managed_vms dict may:
        - Contain entries for VMs that no longer exist (causes 404 errors)
        - Be MISSING entries for VMs that ARE running in GCP (causes orphans)

        This method:
        1. Checks each tracked VM against GCP to verify existence
        2. Removes entries for VMs that no longer exist
        3. Updates state for VMs that exist but have changed status
        4. v235.3: Discovers and ADOPTS running GCP VMs not in managed_vms
        5. Logs any discrepancies for debugging

        Called during initialization to ensure clean state before operations begin.
        """
        if not self.instances_client:
            logger.warning("[VMSync] Instances client not available - skipping sync")
            return

        stale_vms = []
        updated_vms = []
        adopted_vms = []

        async with self._vm_lock:
            # Phase 1: Validate tracked VMs against GCP
            if self.managed_vms:
                logger.info(f"[VMSync] Syncing {len(self.managed_vms)} tracked VMs with GCP...")

                for vm_name, vm in list(self.managed_vms.items()):
                    try:
                        exists, gcp_status = await self._check_vm_exists_in_gcp(vm_name)

                        if not exists:
                            logger.info(
                                f"[VMSync] VM '{vm_name}' no longer exists in GCP "
                                f"(tracked state: {vm.state.value}) - marking for removal"
                            )
                            stale_vms.append(vm_name)
                        elif gcp_status:
                            # VM exists - update our tracking to match GCP state
                            status_map = {
                                "PROVISIONING": VMState.PROVISIONING,
                                "STAGING": VMState.STAGING,
                                "RUNNING": VMState.RUNNING,
                                "STOPPING": VMState.STOPPING,
                                "TERMINATED": VMState.TERMINATED,
                            }
                            new_state = status_map.get(gcp_status, VMState.UNKNOWN)

                            if vm.state != new_state:
                                logger.info(
                                    f"[VMSync] VM '{vm_name}' state updated: "
                                    f"{vm.state.value} → {new_state.value}"
                                )
                                vm.state = new_state
                                updated_vms.append(vm_name)

                            # If VM is terminated in GCP, mark for removal
                            if gcp_status == "TERMINATED":
                                logger.info(
                                    f"[VMSync] VM '{vm_name}' is TERMINATED in GCP - marking for removal"
                                )
                                stale_vms.append(vm_name)

                    except Exception as e:
                        logger.warning(f"[VMSync] Error checking VM '{vm_name}': {e}")
                        # Don't remove on error - could be transient

                # Remove stale VMs from tracking
                for vm_name in stale_vms:
                    if vm_name in self.managed_vms:
                        vm = self.managed_vms[vm_name]
                        # Update stats
                        self.stats["current_active"] = max(0, self.stats["current_active"] - 1)
                        if vm.state == VMState.RUNNING:
                            self.stats["total_terminated"] += 1
                        # Remove from tracking
                        del self.managed_vms[vm_name]

            # Phase 2 (v235.3): Discover and adopt untracked running VMs
            # This handles the case where managed_vms is empty (fresh restart)
            # or VMs were created by a previous supervisor session
            try:
                request = compute_v1.ListInstancesRequest(
                    project=self.config.project_id,
                    zone=self.config.zone,
                    filter="name:jarvis-* AND status=RUNNING",
                )
                gcp_instances = await asyncio.to_thread(
                    lambda: list(self.instances_client.list(request=request))
                )

                for instance in gcp_instances:
                    name = instance.name
                    # Skip VMs already tracked
                    if name in self.managed_vms:
                        continue

                    # Skip golden builder VMs — they're transient build artifacts
                    if "golden-builder" in name:
                        continue

                    # Adopt this VM into managed_vms
                    ip_address = None
                    internal_ip = None
                    for iface in (instance.network_interfaces or []):
                        if iface.network_i_p:
                            internal_ip = iface.network_i_p
                        for access in (iface.access_configs or []):
                            if access.nat_i_p:
                                ip_address = access.nat_i_p

                    # Parse creation time for age tracking
                    created_at = time.time()
                    if instance.creation_timestamp:
                        try:
                            created_dt = datetime.fromisoformat(
                                instance.creation_timestamp.replace('Z', '+00:00')
                            ).replace(tzinfo=None)
                            created_at = created_dt.timestamp()
                        except Exception:
                            pass

                    # Determine VM class from labels/name
                    labels = dict(instance.labels) if instance.labels else {}
                    is_invincible = (
                        labels.get("vm-class") == "invincible"
                        or name.startswith("jarvis-prime-node")
                    )

                    vm_instance = VMInstance(
                        instance_id=str(instance.id) if instance.id else name,
                        name=name,
                        zone=self.config.zone,
                        state=VMState.RUNNING,
                        created_at=created_at,
                        ip_address=ip_address,
                        internal_ip=internal_ip,
                        health_status="unknown",
                        trigger_reason="adopted-on-startup",
                        metadata={
                            "adopted": True,
                            "adopted_at": time.time(),
                            "vm_class": "invincible" if is_invincible else "spot",
                            "labels": labels,
                        },
                    )
                    self.managed_vms[name] = vm_instance
                    adopted_vms.append(name)
                    logger.info(
                        f"[VMSync] v235.3: Adopted untracked VM '{name}' "
                        f"(ip={ip_address}, class={'invincible' if is_invincible else 'spot'}, "
                        f"age={((time.time() - created_at) / 3600):.1f}h)"
                    )

            except Exception as e:
                logger.warning(f"[VMSync] v235.3: VM discovery scan failed: {e}")

        # Summary logging
        parts = []
        if stale_vms:
            parts.append(f"{len(stale_vms)} stale removed")
        if updated_vms:
            parts.append(f"{len(updated_vms)} updated")
        if adopted_vms:
            parts.append(f"{len(adopted_vms)} adopted")
        if parts:
            logger.info(f"[VMSync] Sync complete: {', '.join(parts)}")
        else:
            logger.info("[VMSync] All VMs in sync — no changes needed")

    async def _count_active_gcp_instances(self) -> int:
        """
        v229.0: Count all JARVIS VM instances actually running in GCP.
        
        This queries the GCP API directly instead of relying on local tracking,
        which can become stale after crashes, re-execs, or orphaned sessions.
        
        Returns:
            Number of running JARVIS VM instances in GCP
        """
        if not self.instances_client:
            return 0
        
        try:
            count = 0
            prefix = self.config.vm_name_prefix  # "jarvis-backend"
            
            # Use GCP aggregated list with filter for efficiency
            request = compute_v1.ListInstancesRequest(
                project=self.config.project_id,
                zone=self.config.zone,
                filter=f"name:{prefix}-* AND status=RUNNING",
            )
            
            for instance in await asyncio.to_thread(
                lambda: list(self.instances_client.list(request=request))
            ):
                count += 1
            
            # Also count golden builder VMs (they use significant resources)
            try:
                builder_request = compute_v1.ListInstancesRequest(
                    project=self.config.project_id,
                    zone=self.config.zone,
                    filter="name:jarvis-golden-builder-* AND status=RUNNING",
                )
                for _ in await asyncio.to_thread(
                    lambda: list(self.instances_client.list(request=builder_request))
                ):
                    count += 1
            except Exception:
                pass  # Non-critical
            
            logger.debug(f"[VMGuard] GCP running JARVIS instances: {count}")
            return count
            
        except Exception as e:
            logger.debug(f"[VMGuard] GCP instance count failed: {e}")
            return 0

    async def cleanup_orphaned_gcp_instances(self, max_age_hours: float = 3.0) -> Dict[str, Any]:
        """
        v229.0: Scan GCP for orphaned JARVIS VMs and delete them.
        
        Orphan detection logic:
        - VMs named 'jarvis-backend-*' older than max_age_hours → DELETE
        - VMs named 'jarvis-golden-builder-*' older than 1 hour → DELETE  
        - VMs named 'jarvis-prime-node' or with label 'vm-class=invincible' → SKIP (persistent)
        
        This runs at startup (Clean Slate phase) to prevent quota exhaustion
        and cost accumulation from crashed/orphaned sessions.
        
        Returns:
            Dict with cleanup results
        """
        results = {
            "scanned": 0,
            "deleted": 0,
            "protected": 0,
            "errors": [],
            "details": [],
        }
        
        if not self.instances_client:
            logger.warning("[OrphanCleanup] GCP client not initialized")
            return results
        
        try:
            # List ALL jarvis-prefixed VMs
            request = compute_v1.ListInstancesRequest(
                project=self.config.project_id,
                zone=self.config.zone,
                filter="name:jarvis-*",
            )
            
            instances = await asyncio.to_thread(
                lambda: list(self.instances_client.list(request=request))
            )
            
            for instance in instances:
                results["scanned"] += 1
                name = instance.name
                status = instance.status
                labels = dict(instance.labels) if instance.labels else {}
                
                # Parse creation time
                age_hours = 0.0
                if instance.creation_timestamp:
                    try:
                        created = datetime.fromisoformat(
                            instance.creation_timestamp.replace('Z', '+00:00')
                        ).replace(tzinfo=None)
                        age_hours = (datetime.now() - created).total_seconds() / 3600
                    except Exception:
                        pass
                
                # PROTECT persistent VMs
                is_persistent = (
                    name.startswith("jarvis-prime-node")
                    or labels.get("vm-class") == "invincible"
                )
                
                if is_persistent:
                    results["protected"] += 1
                    results["details"].append(
                        f"  PROTECTED: {name} ({status}, {age_hours:.1f}h, class=invincible)"
                    )
                    continue
                
                # Determine max age based on VM type
                is_builder = "golden-builder" in name
                effective_max_age = 1.0 if is_builder else max_age_hours  # Builders: 1h max
                
                if age_hours >= effective_max_age:
                    logger.info(
                        f"🗑️ [OrphanCleanup] Deleting orphaned VM: {name} "
                        f"(status={status}, age={age_hours:.1f}h, max={effective_max_age}h)"
                    )
                    try:
                        operation = await asyncio.to_thread(
                            self.instances_client.delete,
                            project=self.config.project_id,
                            zone=self.config.zone,
                            instance=name,
                        )
                        await self._wait_for_operation(operation, timeout=60)
                        results["deleted"] += 1
                        results["details"].append(
                            f"  DELETED: {name} ({status}, {age_hours:.1f}h)"
                        )
                    except Exception as e:
                        error_msg = f"Failed to delete {name}: {e}"
                        results["errors"].append(error_msg)
                        results["details"].append(f"  ERROR: {error_msg}")
                else:
                    results["details"].append(
                        f"  OK: {name} ({status}, {age_hours:.1f}h < {effective_max_age}h)"
                    )
            
            if results["deleted"] > 0:
                logger.info(
                    f"✅ [OrphanCleanup] Cleaned {results['deleted']} orphaned VMs "
                    f"(scanned: {results['scanned']}, protected: {results['protected']})"
                )
            else:
                logger.debug(
                    f"[OrphanCleanup] No orphans found "
                    f"(scanned: {results['scanned']}, protected: {results['protected']})"
                )
            
        except Exception as e:
            logger.error(f"[OrphanCleanup] Scan failed: {e}")
            results["errors"].append(str(e))
        
        return results

    async def _periodic_orphan_cleanup(self) -> None:
        """
        v235.3: Background task that periodically scans for and removes orphaned VMs.

        Root cause: cleanup_orphaned_gcp_instances() only runs at startup (Clean Slate).
        If a VM is orphaned AFTER startup (crash, failed termination, preemption
        creating a replacement without cleaning up the old one), the orphan persists
        until the next full restart — accumulating cost.

        This task runs every 30 minutes to catch orphans between restarts.
        Also re-syncs managed_vms to catch state drift.
        """
        _interval = float(os.environ.get("JARVIS_ORPHAN_CLEANUP_INTERVAL", "1800"))  # 30min
        _max_age = float(os.environ.get("JARVIS_ORPHAN_MAX_AGE_HOURS", "3.0"))

        # Wait for initial startup to complete before first run
        await asyncio.sleep(120)

        while self.is_monitoring:
            try:
                # Re-sync tracked VMs with GCP reality
                await self._sync_managed_vms_with_gcp()

                # Scan for and delete orphaned VMs
                results = await asyncio.wait_for(
                    self.cleanup_orphaned_gcp_instances(max_age_hours=_max_age),
                    timeout=90.0,
                )
                if results.get("deleted", 0) > 0:
                    logger.info(
                        f"[PeriodicCleanup] v235.3: Cleaned {results['deleted']} "
                        f"orphaned VMs (scanned: {results['scanned']})"
                    )

            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                logger.warning("[PeriodicCleanup] Orphan scan timed out (90s)")
            except Exception as e:
                logger.debug(f"[PeriodicCleanup] Error (non-fatal): {e}")

            # Sleep with interruptible check
            _remaining = _interval
            while _remaining > 0 and self.is_monitoring:
                await asyncio.sleep(min(5.0, _remaining))
                _remaining -= 5.0

    async def get_active_vm(self) -> Optional[VMInstance]:
        """
        Get the currently active (RUNNING) VM instance.

        Returns:
            VMInstance if found and running, None otherwise.
        """
        async with self._vm_lock:
            for vm in self.managed_vms.values():
                if vm.state == VMState.RUNNING and vm.is_healthy:
                    return vm
        return None

    async def get_most_recent_vm(self) -> Optional[VMInstance]:
        """
        v214.0: Get the most recently created VM instance, regardless of health status.
        
        This is useful immediately after provisioning when the VM exists but
        hasn't passed health checks yet.
        
        Returns:
            VMInstance if found, None otherwise.
        """
        async with self._vm_lock:
            if not self.managed_vms:
                return None
            # Return the first RUNNING VM, or any VM if none are running
            running_vms = [vm for vm in self.managed_vms.values() if vm.state == VMState.RUNNING]
            if running_vms:
                return running_vms[0]
            # No running VMs - return any VM
            return next(iter(self.managed_vms.values()), None)
    
    async def start_spot_vm(self) -> Tuple[bool, Optional[str]]:
        """
        Start a Spot VM for immediate use.
        
        v147.0: Enhanced with detailed error messages for diagnostics.
        v214.0: Returns (success, ip_or_error, vm_name) - vm_name available via
                get_most_recent_vm() immediately after this call.
        
        Returns:
            (success, result_or_error)
            - On success: (True, ip_address)
            - On failure: (False, error_message)
        """
        # v147.0: Check enabled with detailed error
        if not self.config.enabled:
            enabled_vars = {
                "GCP_ENABLED": os.getenv("GCP_ENABLED", "not set"),
                "GCP_VM_ENABLED": os.getenv("GCP_VM_ENABLED", "not set"),
                "JARVIS_SPOT_VM_ENABLED": os.getenv("JARVIS_SPOT_VM_ENABLED", "not set"),
            }
            logger.warning(
                f"[v147.0] GCP VM Manager disabled. Env vars: {enabled_vars}. "
                f"Set any of these to 'true' to enable."
            )
            return False, "GCP_DISABLED: Set GCP_ENABLED, GCP_VM_ENABLED, or JARVIS_SPOT_VM_ENABLED=true"
        
        # v147.0: Validate configuration before attempting
        is_valid, validation_error = self.config.is_valid_for_vm_operations()
        if not is_valid:
            logger.warning(f"[v147.0] GCP config validation failed: {validation_error}")
            return False, f"CONFIG_INVALID: {validation_error}"

        # v155.0: CRITICAL - Check firewall rule before VM creation
        # Without this, health checks will ALWAYS fail and VM will timeout
        if not getattr(self, '_firewall_rule_verified', False):
            logger.warning("[v155.0] Firewall rule not verified - attempting to verify now...")
            firewall_ok = await self._ensure_firewall_rule()
            if not firewall_ok:
                logger.error(
                    "[v155.0] ❌ BLOCKING VM CREATION: Firewall rule not configured.\n"
                    "    Health checks cannot reach VMs without this rule.\n"
                    "    Fix: Create firewall rule manually or grant service account permissions."
                )
                return False, "FIREWALL_RULE_MISSING: Health checks will fail. Create jarvis-allow-health-checks rule."

        try:
            # Check if we already have one
            existing = await self.get_active_vm()
            if existing:
                logger.info(f"[v147.0] Using existing active VM: {existing.ip_address}")
                return True, existing.ip_address
                
            # v132.2: Fixed create_vm call - uses correct parameters
            # create_vm expects (components: List[str], trigger_reason: str)
            logger.info("[v147.0] No existing VM, creating new Spot VM...")
            vm = await self.create_vm(
                components=["ml_processing", "heavy_computation", "auto_offload"],
                trigger_reason="auto_offload",
            )
            
            # v213.0: Wait for IP assignment with retry loop
            # GCP can take a few seconds to assign the external IP after VM creation
            if vm and vm.state == VMState.RUNNING:
                if vm.ip_address:
                    logger.info(f"[v213.0] Spot VM created successfully: {vm.ip_address}")
                    return True, vm.ip_address
                
                # v213.0: IP not immediately available - poll GCP for up to 60s
                logger.info(f"[v213.0] VM created but IP not yet assigned. Waiting for IP assignment...")
                ip_wait_start = time.time()
                ip_wait_timeout = 60.0  # 60 second timeout for IP assignment
                
                while (time.time() - ip_wait_start) < ip_wait_timeout:
                    try:
                        # Query GCP for current VM state
                        instance = await asyncio.to_thread(
                            self.instances_client.get,
                            project=self.config.project_id,
                            zone=self.config.zone,
                            instance=vm.name,
                        )
                        
                        # Check for external IP
                        if instance.network_interfaces:
                            ni = instance.network_interfaces[0]
                            if ni.access_configs:
                                ip_address = ni.access_configs[0].nat_i_p
                                if ip_address:
                                    # Update our tracked VM with the IP
                                    async with self._vm_lock:
                                        if vm.name in self.managed_vms:
                                            self.managed_vms[vm.name].ip_address = ip_address
                                            self._update_ip_index(self.managed_vms[vm.name])
                                    logger.info(f"[v213.0] IP assigned after {time.time() - ip_wait_start:.1f}s: {ip_address}")
                                    return True, ip_address
                        
                        await asyncio.sleep(2.0)  # Poll every 2 seconds
                    except Exception as ip_poll_err:
                        logger.debug(f"[v213.0] IP poll error (retrying): {ip_poll_err}")
                        await asyncio.sleep(3.0)
                
                # Timed out waiting for IP - return what we have (might be None)
                logger.warning(f"[v213.0] Timed out waiting for IP assignment after {ip_wait_timeout}s")
                return True, vm.ip_address  # Still return success - VM is running
            
            # VM creation returned but not running
            if vm:
                return False, f"VM_NOT_RUNNING: VM created but state is {vm.state.name}"
            else:
                return False, "VM_CREATE_FAILED: create_vm returned None"
                
        except Exception as e:
            log_component_failure(
                "gcp-vm",
                "Failed to start Spot VM",
                error=e,
            )
            return False, f"EXCEPTION: {str(e)}"

        # Initialize regions client for quota checking
        # Use modern asyncio patterns with fallback
        try:
            try:
                # Try async initialization first
                self.regions_client = await asyncio.to_thread(
                    compute_v1.RegionsClient
                )
            except RuntimeError:
                # Fall back to sync if thread pool unavailable
                self.regions_client = compute_v1.RegionsClient()
            logger.info("✅ Regions client initialized for quota checking")
        except Exception as e:
            logger.warning(f"⚠️  Regions client initialization failed (quota checking limited): {e}")
            self.regions_client = None

    # ═══════════════════════════════════════════════════════════════════════════
    # v9.0: INTELLIGENT QUOTA MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def check_quotas_before_creation(self) -> QuotaCheckResult:
        """
        v9.0: Pre-flight quota check before attempting VM creation.
        
        Checks critical quotas like CPUS_ALL_REGIONS and IN_USE_ADDRESSES
        to avoid wasted API calls when quotas are exceeded.
        
        Returns:
            QuotaCheckResult with can_create=True if quotas allow VM creation
        """
        self.stats["quota_checks"] += 1
        
        # Check if we're in quota cooldown period
        if time.time() < self._quota_exceeded_until:
            remaining = self._quota_exceeded_until - time.time()
            logger.info(f"⏳ Quota cooldown active ({remaining:.0f}s remaining) - skipping VM creation")
            return QuotaCheckResult(
                can_create=False,
                message=f"Quota cooldown active ({remaining:.0f}s remaining)",
                blocking_quotas=list(self._quota_cache.values())
            )
        
        # Circuit breaker check
        circuit = self._circuit_breakers["quota_check"]
        can_execute, circuit_reason = circuit.can_execute()
        if not can_execute:
            logger.warning(f"⚠️  Quota check circuit breaker open: {circuit_reason}")
            # Assume quotas are OK if we can't check (optimistic)
            return QuotaCheckResult(can_create=True, message="Quota check unavailable - proceeding optimistically")
        
        blocking_quotas: List[QuotaInfo] = []
        warning_quotas: List[QuotaInfo] = []
        
        try:
            # Check critical quotas in parallel
            quota_checks = await asyncio.gather(
                self._check_cpu_quota(),
                self._check_address_quota(),
                self._check_instance_quota(),
                return_exceptions=True
            )
            
            for result in quota_checks:
                if isinstance(result, Exception):
                    logger.warning(f"⚠️  Quota check error (non-fatal): {result}")
                    continue
                if result is None:
                    continue
                    
                quota_info: QuotaInfo = result
                
                # Cache the quota info
                async with self._quota_cache_lock:
                    self._quota_cache[quota_info.metric] = quota_info
                
                if quota_info.is_exceeded:
                    blocking_quotas.append(quota_info)
                    logger.warning(
                        f"🚫 QUOTA EXCEEDED: {quota_info.metric} "
                        f"({quota_info.usage:.0f}/{quota_info.limit:.0f})"
                    )
                elif quota_info.utilization_percent > 80:
                    warning_quotas.append(quota_info)
                    logger.info(
                        f"⚠️  High quota utilization: {quota_info.metric} "
                        f"({quota_info.utilization_percent:.0f}%)"
                    )
            
            circuit.record_success()
            
            if blocking_quotas:
                # Set cooldown to avoid repeated failures
                self._quota_exceeded_until = time.time() + self._quota_cooldown_seconds
                self.stats["quota_blocks"] += 1
                
                blocked_names = ", ".join(q.metric for q in blocking_quotas)
                return QuotaCheckResult(
                    can_create=False,
                    blocking_quotas=blocking_quotas,
                    warning_quotas=warning_quotas,
                    message=f"Quota exceeded: {blocked_names}. Cooldown: {self._quota_cooldown_seconds}s"
                )
            
            return QuotaCheckResult(
                can_create=True,
                warning_quotas=warning_quotas,
                message="All quotas OK" if not warning_quotas else f"Quotas OK with {len(warning_quotas)} warnings"
            )
            
        except Exception as e:
            circuit.record_failure(e)
            log_component_failure(
                "gcp-vm",
                "Quota check failed",
                error=e,
            )
            # Proceed optimistically if quota check fails
            return QuotaCheckResult(can_create=True, message=f"Quota check error: {e} - proceeding optimistically")
    
    async def _check_cpu_quota(self) -> Optional[QuotaInfo]:
        """Check CPUS_ALL_REGIONS quota."""
        return await self._get_quota_metric("CPUS_ALL_REGIONS")
    
    async def _check_address_quota(self) -> Optional[QuotaInfo]:
        """Check IN_USE_ADDRESSES quota for the region."""
        return await self._get_quota_metric("IN_USE_ADDRESSES", region=self.config.region)
    
    async def _check_instance_quota(self) -> Optional[QuotaInfo]:
        """Check INSTANCES quota for the region."""
        return await self._get_quota_metric("INSTANCES", region=self.config.region)
    
    async def _get_quota_metric(
        self, metric: str, region: Optional[str] = None
    ) -> Optional[QuotaInfo]:
        """
        Get a specific quota metric from GCP.
        Uses cache if available and not stale.
        """
        cache_key = f"{metric}:{region or 'global'}"
        
        # Check cache first
        async with self._quota_cache_lock:
            if cache_key in self._quota_cache:
                cached = self._quota_cache[cache_key]
                if not cached.is_stale:
                    return cached
        
        if not self.regions_client or not self.config.project_id:
            return None
        
        try:
            # Query quota from GCP
            target_region = region or self.config.region
            
            region_info = await asyncio.to_thread(
                self.regions_client.get,
                project=self.config.project_id,
                region=target_region
            )
            
            # Find the quota in the region's quotas list
            for quota in region_info.quotas:
                if quota.metric == metric:
                    quota_info = QuotaInfo(
                        metric=metric,
                        limit=quota.limit,
                        usage=quota.usage,
                        region=target_region
                    )
                    return quota_info
            
            # Quota not found - might be a global quota
            # For global quotas like CPUS_ALL_REGIONS, we need to sum across regions
            # or check project-level quotas (simplified here)
            return None
            
        except Exception as e:
            logger.debug(f"Could not fetch quota {metric}: {e}")
            return None
    
    def _is_quota_exceeded_error(self, error: Exception) -> Tuple[bool, Optional[str]]:
        """
        v9.0: Detect if an error is a quota exceeded error.
        
        Returns:
            (is_quota_error, quota_name or None)
        """
        error_str = str(error).lower()
        
        quota_patterns = [
            ("quota", "CPUS_ALL_REGIONS"),
            ("quota 'cpus", "CPUS_ALL_REGIONS"),
            ("quota 'in_use_addresses", "IN_USE_ADDRESSES"),
            ("quota 'instances", "INSTANCES"),
            ("quota exceeded", None),  # Generic quota exceeded
            ("resource_exhausted", None),
        ]
        
        for pattern, quota_name in quota_patterns:
            if pattern in error_str:
                # Try to extract the specific quota from the error message
                if quota_name is None:
                    # Parse from error: "Quota 'CPUS_ALL_REGIONS' exceeded"
                    import re
                    match = re.search(r"quota '([A-Z_]+)'", str(error), re.IGNORECASE)
                    if match:
                        quota_name = match.group(1).upper()
                    else:
                        quota_name = "UNKNOWN"
                return True, quota_name
        
        return False, None

    async def should_create_vm(
        self, memory_snapshot, trigger_reason: str = ""
    ) -> Tuple[bool, str, float]:
        """
        Determine if we should create a VM based on current conditions.
        
        v2.0: Enhanced with intelligent budget enforcement from cost_tracker.

        Returns: (should_create, reason, confidence_score)
        """
        if not self.initialized:
            await self.initialize()

        # ═══════════════════════════════════════════════════════════════════════════
        # v2.0: INTELLIGENT BUDGET ENFORCEMENT
        # ═══════════════════════════════════════════════════════════════════════════
        # Uses cost_tracker.can_create_vm() which provides:
        # - Hard budget enforcement (blocks when exceeded)
        # - Budget warning alerts (at 50% threshold)
        # - Cost forecasting (warns if likely to exceed)
        # - Solo developer mode protection
        # ═══════════════════════════════════════════════════════════════════════════
        if self.cost_tracker:
            try:
                # Use intelligent budget check
                if hasattr(self.cost_tracker, 'can_create_vm'):
                    allowed, reason, details = await self.cost_tracker.can_create_vm()
                    if not allowed:
                        logger.warning(f"🚫 VM creation blocked by budget: {reason}")
                        return (False, reason, 0.0)
                    
                    # Log budget status if close to limit
                    if details.get("budget_percent_used", 0) >= 50:
                        logger.info(
                            f"💰 Budget status: {details['budget_percent_used']:.0f}% used "
                            f"(${details['daily_spent']:.2f}/${details['daily_budget']:.2f})"
                        )
                else:
                    # Fallback to simple daily cost check
                    daily_cost = await self.cost_tracker.get_daily_cost()
                    if daily_cost >= self.config.daily_budget_usd:
                        return (
                            False,
                            f"Daily budget exceeded: ${daily_cost:.2f} / ${self.config.daily_budget_usd:.2f}",
                            0.0,
                        )
            except Exception as e:
                logger.warning(f"⚠️ Budget check failed (allowing VM): {e}")

        # v229.0: Check concurrent VM limits using REAL GCP instance count
        # Previous bug: only checked tracked VMs (self.managed_vms), but untracked
        # VMs (from crashed sessions, pre-warm tasks, orphans) weren't counted.
        # This led to 7 VMs running simultaneously, exhausting CPU quota.
        # Fix: Query GCP API for actual running JARVIS instances.
        active_vms = len([vm for vm in self.managed_vms.values() if vm.state == VMState.RUNNING])
        
        # Also count untracked VMs via GCP API (async-safe)
        try:
            gcp_vm_count = await self._count_active_gcp_instances()
            if gcp_vm_count > active_vms:
                logger.warning(
                    f"⚠️ [VMGuard] GCP has {gcp_vm_count} running JARVIS VMs "
                    f"but only {active_vms} are tracked locally. "
                    f"Orphan cleanup recommended."
                )
                active_vms = gcp_vm_count  # Use the higher (real) count
        except Exception as e:
            logger.debug(f"[VMGuard] GCP instance count check failed: {e}")
        
        if active_vms >= self.config.max_concurrent_vms:
            return (
                False,
                f"Max concurrent VMs reached: {active_vms} / {self.config.max_concurrent_vms} "
                f"(run orphan cleanup to free slots)",
                0.0,
            )

        # Check if already creating a VM
        if self.creating_vms:
            return False, "VM creation already in progress", 0.0

        # Use intelligent optimizer for decision
        if self.gcp_optimizer:
            should_create, reason, score = await self.gcp_optimizer.should_create_vm(
                memory_snapshot, current_processes=None
            )
            # PressureScore uses composite_score, not overall_score
            confidence = score.composite_score if hasattr(score, 'composite_score') else getattr(score, 'overall_score', 0.0)
            return should_create, reason, confidence

        # Fallback: Simple memory pressure check
        if memory_snapshot.gcp_shift_recommended:
            return True, trigger_reason or memory_snapshot.reasoning, 0.8

        return False, "Memory pressure within acceptable limits", 0.0

    async def _adopt_existing_vm(
        self,
        vm_name: str,
        components: List[str],
        trigger_reason: str,
        metadata: Optional[Dict] = None,
    ) -> Optional[VMInstance]:
        """v233.0: Adopt an already-existing GCP VM into our tracking.

        Used when a VM is discovered to already exist (pre-creation check or
        409 recovery). Fetches instance details, extracts IPs, creates a
        VMInstance tracking object, and registers it in managed_vms.

        Returns VMInstance if successful, None if the instance can't be fetched.
        """
        try:
            instance = await asyncio.to_thread(
                self.instances_client.get,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=vm_name,
            )
            ip_address = None
            internal_ip = None
            if instance.network_interfaces:
                internal_ip = instance.network_interfaces[0].network_i_p
                if instance.network_interfaces[0].access_configs:
                    ip_address = instance.network_interfaces[0].access_configs[0].nat_i_p

            vm_instance = VMInstance(
                instance_id=str(instance.id),
                name=vm_name,
                zone=self.config.zone,
                state=VMState.RUNNING,
                created_at=time.time(),
                ip_address=ip_address,
                internal_ip=internal_ip,
                components=components,
                trigger_reason=trigger_reason,
                metadata=metadata or {},
            )
            async with self._vm_lock:
                self.managed_vms[vm_name] = vm_instance
                self._update_ip_index(vm_instance)
                self.stats["total_created"] += 1
                self.stats["current_active"] += 1

            # Set startup grace period for adopted VMs — they need time to be evaluated
            if not hasattr(self, '_startup_grace_until'):
                self._startup_grace_until: Dict[str, float] = {}
            grace_seconds = self.config.efficiency_grace_minutes * 60
            self._startup_grace_until[vm_name] = time.time() + grace_seconds
            logger.info(
                f"[AdoptVM] Set {self.config.efficiency_grace_minutes}m grace period "
                f"for adopted VM '{vm_name}'"
            )

            return vm_instance
        except Exception as e:
            logger.warning(f"[CreateVM] Failed to adopt VM '{vm_name}': {e}")
            return None

    async def create_vm(
        self, components: List[str], trigger_reason: str, metadata: Optional[Dict] = None
    ) -> Optional[VMInstance]:
        """
        Create a new GCP Spot VM instance with circuit breaker protection.

        v9.0: Now includes pre-flight quota checking to avoid wasted API calls.

        Args:
            components: List of components that will run on this VM
            trigger_reason: Why this VM is being created
            metadata: Additional metadata

        Returns:
            VMInstance if successful, None otherwise
        """
        if not self.initialized:
            await self.initialize()

        # ═══════════════════════════════════════════════════════════════════════════
        # PRE-FLIGHT CONFIGURATION VALIDATION
        # ═══════════════════════════════════════════════════════════════════════════
        # Ensure project_id and zone are properly configured before making API calls
        # This prevents cryptic API errors about "missing project/zone fields"
        # ═══════════════════════════════════════════════════════════════════════════
        is_valid, validation_error = self.config.is_valid_for_vm_operations()
        if not is_valid:
            # v109.1: Changed from ERROR to INFO - GCP being disabled is expected
            # configuration when running without cloud resources. This is not an error.
            logger.info(f"ℹ️  VM creation skipped - {validation_error}")
            logger.debug("   To enable: set GCP_PROJECT_ID, GCP_ZONE, and GCP_ENABLED=true")
            self.stats["total_failed"] += 1
            self.stats["last_error"] = f"Configuration invalid: {validation_error}"
            self.stats["last_error_time"] = time.time()
            return None

        # ═══════════════════════════════════════════════════════════════════════════
        # v1.0: CROSS-PROCESS RESOURCE LOCK (via ProcessCoordinationHub)
        # ═══════════════════════════════════════════════════════════════════════════
        # Prevents multiple processes (run_supervisor.py, start_system.py) from
        # creating GCP VMs simultaneously, which could cause:
        # 1. Duplicate VMs = double billing
        # 2. Resource quota exhaustion
        # 3. Race conditions in VM tracking
        # ═══════════════════════════════════════════════════════════════════════════
        coord_hub = None
        try:
            from backend.core.trinity_process_coordination import (
                get_coordination_hub,
                LockType,
            )
            coord_hub = await get_coordination_hub()
        except ImportError:
            logger.debug("[VMManager] ProcessCoordinationHub not available (optional)")
        except Exception as e:
            logger.debug(f"[VMManager] CoordHub init warning: {e}")

        # Check circuit breaker before attempting
        circuit = self._circuit_breakers["vm_create"]
        can_execute, circuit_reason = circuit.can_execute()

        if not can_execute:
            logger.warning(f"🔌 VM creation blocked by circuit breaker: {circuit_reason}")
            self.stats["circuit_breaks"] += 1
            return None

        # ═══════════════════════════════════════════════════════════════════════════
        # v9.0: PRE-FLIGHT QUOTA CHECK
        # ═══════════════════════════════════════════════════════════════════════════
        # Check quotas BEFORE attempting VM creation to avoid:
        # 1. Wasted API calls that will fail anyway
        # 2. Multiple retry attempts that all fail with the same error
        # 3. Unnecessary delays and error logs
        # ═══════════════════════════════════════════════════════════════════════════
        quota_check = await self.check_quotas_before_creation()
        
        if not quota_check.can_create:
            logger.warning(f"🚫 VM creation blocked by quota check: {quota_check.message}")
            for quota in quota_check.blocking_quotas:
                logger.warning(
                    f"   ├─ {quota.metric}: {quota.usage:.0f}/{quota.limit:.0f} "
                    f"({quota.utilization_percent:.0f}% used)"
                )
            return None
        
        if quota_check.has_warnings:
            logger.info(f"⚠️  Quota warnings (proceeding anyway):")
            for quota in quota_check.warning_quotas:
                logger.info(
                    f"   ├─ {quota.metric}: {quota.usage:.0f}/{quota.limit:.0f} "
                    f"({quota.utilization_percent:.0f}% used)"
                )

        # ═══════════════════════════════════════════════════════════════════════════
        # v9.0: RATE LIMIT CHECK
        # ═══════════════════════════════════════════════════════════════════════════
        if RATE_LIMIT_MANAGER_AVAILABLE:
            try:
                rate_manager = await get_rate_limit_manager()
                acquired, reason = await rate_manager.acquire(
                    GCPService.COMPUTE_ENGINE, OperationType.WRITE, timeout=10.0
                )
                if not acquired:
                    logger.warning(f"⏳ Rate limit prevents VM creation: {reason}")
                    return None
            except Exception as e:
                logger.debug(f"Rate limit check failed (proceeding): {e}")

        logger.info(f"🚀 Creating GCP Spot VM...")
        logger.info(f"   Components: {', '.join(components)}")
        logger.info(f"   Trigger: {trigger_reason}")
        logger.info(f"   Quota check: {quota_check.message}")

        # ═══════════════════════════════════════════════════════════════════════════
        # v193.2: SMART VM CREATION WITH CONCURRENT REQUEST HANDLING
        # ═══════════════════════════════════════════════════════════════════════════
        # When multiple callers try to create a VM concurrently (e.g., main startup
        # path + background GCP retry task), instead of blocking the second caller
        # with an error, we make it WAIT for the first creation to complete and
        # return that result. This prevents "VM_CREATE_FAILED: create_vm returned None"
        # errors during Trinity protocol initialization.
        #
        # Smart waiting behavior:
        # - First caller: Creates the VM, other callers wait for result
        # - Waiting callers: Get the same VM instance (or error) as the first caller
        # - Timeout: Waiting callers give up after 5 minutes (configurable)
        # ═══════════════════════════════════════════════════════════════════════════
        creation_id = f"create_{int(time.time() * 1000)}"
        wait_timeout = float(os.environ.get("GCP_VM_CREATION_WAIT_TIMEOUT", "300.0"))
        wait_event: Optional[asyncio.Event] = None
        should_wait = False

        async with self._vm_lock:
            if self.creating_vms:
                existing_creation_ids = list(self.creating_vms.keys())
                logger.info(
                    f"⏳ VM creation already in progress ({existing_creation_ids}). "
                    f"Waiting up to {wait_timeout}s for it to complete..."
                )

                # Get or create the event to wait on
                if self._creation_event is None:
                    self._creation_event = asyncio.Event()

                # Release lock and wait for creation to complete
                wait_event = self._creation_event
                should_wait = True

        # If we found an in-progress creation, wait for it
        if should_wait and wait_event is not None:
            try:
                await asyncio.wait_for(asyncio.shield(wait_event.wait()), timeout=wait_timeout)

                # Check the result of the creation we waited for
                async with self._vm_lock:
                    if self._creation_result is not None:
                        logger.info(
                            f"✅ Waited for in-progress creation: got VM {self._creation_result.name}"
                        )
                        return self._creation_result
                    elif self._creation_error:
                        logger.warning(
                            f"⚠️ In-progress creation failed: {self._creation_error}"
                        )
                        return None
                    else:
                        # Creation completed but no result - shouldn't happen
                        logger.warning("⚠️ In-progress creation completed with no result")
                        return None

            except asyncio.TimeoutError:
                logger.error(
                    f"⏰ Timed out waiting {wait_timeout}s for in-progress VM creation. "
                    f"The other creation may still be running."
                )
                return None

        # Re-acquire lock to mark ourselves as creating
        async with self._vm_lock:
            # Double-check no one snuck in
            if self.creating_vms:
                logger.warning(
                    f"🚫 Race condition: another creation started while we waited. "
                    f"Deferring to: {list(self.creating_vms.keys())}"
                )
                return None

            # Mark that we're creating and reset the event
            self.creating_vms[creation_id] = None
            self._creation_event = asyncio.Event()
            self._creation_result = None
            self._creation_error = None
            logger.debug(f"[v193.2] Starting VM creation: {creation_id}")

        # ═══════════════════════════════════════════════════════════════════════════
        # v1.0: Try to acquire GCP VM creation lock (best-effort, non-blocking)
        # This prevents duplicate VMs when multiple processes try to create simultaneously
        # ═══════════════════════════════════════════════════════════════════════════
        if coord_hub is not None:
            try:
                from backend.core.trinity_process_coordination import LockType
                # Try to log lock acquisition (non-blocking check)
                logger.debug("🔒 Attempting GCP VM creation lock (coordination hub available)")
            except Exception as lock_err:
                logger.debug(f"Lock coordination warning (continuing): {lock_err}")

        attempt = 0
        last_error = None
        vm_instance = None
        _cleanup_retries = 0  # v233.0: Bounded counter for pre-creation zombie cleanup
        _max_cleanup_retries = 3  # Max zombie cleanups before counting as a real attempt

        try:
            while attempt < self.config.max_create_attempts:
                attempt += 1
                try:
                    # v233.0: Generate collision-proof VM name using timestamp + random suffix.
                    # Previous: 1-second granularity timestamp alone caused 409 "already exists"
                    # when retries fired within the same second. Now includes 4-char hex suffix
                    # from os.urandom() for uniqueness even within sub-second retries.
                    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    _name_suffix = os.urandom(2).hex()  # 4 hex chars, 65536 possibilities
                    vm_name = f"{self.config.vm_name_prefix}-{timestamp}-{_name_suffix}"

                    logger.info(
                        f"🔨 Attempt {attempt}/{self.config.max_create_attempts}: Creating VM '{vm_name}'"
                    )

                    # v233.0: Pre-creation existence check — if a VM with this name somehow
                    # exists (e.g., from a previous partial creation), handle it before insert.
                    _pre_status, _pre_meta, _pre_err = await self._describe_instance_full(vm_name)
                    if _pre_status not in ("NOT_FOUND", "ERROR"):
                        logger.warning(
                            f"[CreateVM] v233.0: VM '{vm_name}' already exists "
                            f"(status={_pre_status}) — reusing if RUNNING"
                        )
                        if _pre_status == "RUNNING":
                            # VM is already running — adopt it instead of failing
                            _adopted = await self._adopt_existing_vm(
                                vm_name, components, trigger_reason, metadata,
                            )
                            if _adopted:
                                circuit.record_success()
                                logger.info(
                                    f"✅ Reused existing RUNNING VM: {vm_name} "
                                    f"({_adopted.ip_address})"
                                )
                                return _adopted
                        else:
                            # VM exists in non-RUNNING state — terminate zombie, regenerate name.
                            logger.info(
                                f"[CreateVM] Cleaning up stale VM '{vm_name}' "
                                f"(status={_pre_status})"
                            )
                            try:
                                await self.terminate_vm(
                                    vm_name=vm_name,
                                    reason=f"Pre-creation cleanup: stale instance in {_pre_status} state",
                                )
                            except Exception as _cleanup_err:
                                logger.debug(f"[CreateVM] Cleanup of '{vm_name}' non-fatal: {_cleanup_err}")
                            # Next iteration generates a new random suffix — won't hit same zombie.
                            # Don't count cleanup as a real attempt, but cap to prevent infinite loop.
                            _cleanup_retries += 1
                            if _cleanup_retries <= _max_cleanup_retries:
                                attempt -= 1
                            continue

                    # Build VM configuration
                    instance_config = self._build_instance_config(
                        vm_name=vm_name,
                        components=components,
                        trigger_reason=trigger_reason,
                        metadata=metadata or {},
                    )

                    # Create the VM (async operation)
                    operation = await asyncio.to_thread(
                        self.instances_client.insert,
                        project=self.config.project_id,
                        zone=self.config.zone,
                        instance_resource=instance_config,
                    )

                    logger.info(f"⏳ VM creation operation started: {operation.name}")

                    # Wait for operation to complete
                    await self._wait_for_operation(operation)

                    # Get the created instance
                    instance = await asyncio.to_thread(
                        self.instances_client.get,
                        project=self.config.project_id,
                        zone=self.config.zone,
                        instance=vm_name,
                    )

                    # Extract IP addresses
                    ip_address = None
                    internal_ip = None
                    if instance.network_interfaces:
                        internal_ip = instance.network_interfaces[0].network_i_p
                        if instance.network_interfaces[0].access_configs:
                            ip_address = instance.network_interfaces[0].access_configs[0].nat_i_p

                    # Create VMInstance tracking object
                    vm_instance = VMInstance(
                        instance_id=str(instance.id),
                        name=vm_name,
                        zone=self.config.zone,
                        state=VMState.RUNNING,
                        created_at=time.time(),
                        ip_address=ip_address,
                        internal_ip=internal_ip,
                        components=components,
                        trigger_reason=trigger_reason,
                        metadata=metadata or {},
                    )

                    # Track the VM with lock protection
                    async with self._vm_lock:
                        self.managed_vms[vm_name] = vm_instance
                        self._update_ip_index(vm_instance)
                        self.stats["total_created"] += 1
                        self.stats["current_active"] += 1

                    # Record in cost tracker (isolated error handling)
                    await self._record_vm_creation_safe(
                        vm_instance=vm_instance,
                        components=components,
                        trigger_reason=trigger_reason,
                        metadata=metadata,
                    )

                    # Circuit breaker success
                    circuit.record_success()

                    logger.info(f"✅ VM created successfully: {vm_name}")
                    logger.info(f"   External IP: {ip_address or 'N/A'}")
                    logger.info(f"   Internal IP: {internal_ip or 'N/A'}")
                    logger.info(f"   Cost: ${vm_instance.cost_per_hour:.3f}/hour")

                    # Set startup grace period for new VMs
                    if not hasattr(self, '_startup_grace_until'):
                        self._startup_grace_until: Dict[str, float] = {}
                    grace_seconds = self.config.efficiency_grace_minutes * 60
                    self._startup_grace_until[vm_name] = time.time() + grace_seconds

                    return vm_instance

                except Exception as e:
                    last_error = e
                    _err_str = str(e).lower()
                    self.stats["retries"] += 1
                    self.stats["last_error"] = str(e)
                    self.stats["last_error_time"] = datetime.now().isoformat()
                    logger.error(f"❌ Attempt {attempt} failed: {e}")

                    # v233.0: Handle 409 Conflict ("already exists") — the insert
                    # succeeded server-side but we got a stale error, OR a race condition
                    # created the VM between our existence check and insert call.
                    _is_conflict = "409" in str(e) or "already exists" in _err_str
                    if _is_conflict:
                        logger.warning(
                            f"[CreateVM] v233.0: 409 Conflict for '{vm_name}' — "
                            f"checking if VM is usable"
                        )
                        try:
                            _conflict_status, _, _ = await self._describe_instance_full(vm_name)
                            if _conflict_status == "RUNNING":
                                # VM actually exists and is running — adopt it
                                _adopted_409 = await self._adopt_existing_vm(
                                    vm_name, components, trigger_reason, metadata,
                                )
                                if _adopted_409:
                                    circuit.record_success()
                                    logger.info(
                                        f"✅ Recovered from 409: reusing VM '{vm_name}' "
                                        f"({_adopted_409.ip_address})"
                                    )
                                    return _adopted_409
                            elif _conflict_status in ("STAGING", "PROVISIONING"):
                                # VM is still being created — wait for it
                                logger.info(
                                    f"[CreateVM] VM '{vm_name}' is {_conflict_status} — "
                                    f"waiting for it to become RUNNING"
                                )
                                _wait_start = time.time()
                                _wait_max = 120.0  # 2 minutes max wait
                                while time.time() - _wait_start < _wait_max:
                                    await asyncio.sleep(5)
                                    _ws, _, _ = await self._describe_instance_full(vm_name)
                                    if _ws == "RUNNING":
                                        _adopted_staging = await self._adopt_existing_vm(
                                            vm_name, components, trigger_reason, metadata,
                                        )
                                        if _adopted_staging:
                                            circuit.record_success()
                                            logger.info(
                                                f"✅ Recovered from 409: VM '{vm_name}' "
                                                f"now RUNNING ({_adopted_staging.ip_address})"
                                            )
                                            return _adopted_staging
                                    elif _ws in ("TERMINATED", "NOT_FOUND", "ERROR"):
                                        break  # VM failed — fall through to retry
                                # VM didn't become RUNNING — clean up and retry
                                logger.warning(
                                    f"[CreateVM] VM '{vm_name}' didn't reach RUNNING after 409"
                                )
                            else:
                                # VM in terminal state — clean up zombie
                                logger.info(
                                    f"[CreateVM] Cleaning up zombie VM '{vm_name}' "
                                    f"(status={_conflict_status})"
                                )
                            # Clean up the conflicting instance for next retry
                            try:
                                await self.terminate_vm(
                                    vm_name=vm_name,
                                    reason="409 conflict cleanup",
                                )
                            except Exception:
                                pass
                        except Exception as _409_err:
                            logger.debug(f"[CreateVM] 409 recovery failed: {_409_err}")
                        # Next retry will generate a new name (new suffix)
                        if attempt < self.config.max_create_attempts:
                            delay = self.config.retry_delay_seconds * attempt
                            logger.info(f"⏳ Retrying with new name in {delay}s...")
                            await asyncio.sleep(delay)
                        continue

                    # v9.0: Detect quota exceeded errors and stop retrying immediately
                    is_quota_error, quota_name = self._is_quota_exceeded_error(e)
                    if is_quota_error:
                        logger.error(f"🚫 QUOTA EXCEEDED ({quota_name}) - stopping retries immediately")

                        # Set cooldown to prevent future attempts
                        self._quota_exceeded_until = time.time() + self._quota_cooldown_seconds
                        self.stats["quota_blocks"] += 1

                        # Update quota cache with exceeded status
                        if quota_name:
                            async with self._quota_cache_lock:
                                self._quota_cache[quota_name] = QuotaInfo(
                                    metric=quota_name,
                                    limit=0,  # Unknown
                                    usage=0,  # Unknown
                                    region=self.config.region,
                                )
                                self._quota_cache[quota_name].is_exceeded = True  # Force exceeded

                        # Report to rate limit manager
                        if RATE_LIMIT_MANAGER_AVAILABLE:
                            try:
                                rate_manager = await get_rate_limit_manager()
                                rate_manager.handle_quota_exceeded(GCPService.COMPUTE_ENGINE, quota_name)
                            except Exception:
                                pass

                        # Don't retry - quotas won't change in seconds
                        break

                    # v9.0: Detect rate limit (429) errors
                    is_429 = "429" in str(e) or "too many requests" in _err_str
                    if is_429:
                        logger.error(f"🚫 RATE LIMITED (429) - entering cooldown")
                        if RATE_LIMIT_MANAGER_AVAILABLE:
                            try:
                                rate_manager = await get_rate_limit_manager()
                                rate_manager.handle_429_response(GCPService.COMPUTE_ENGINE)
                            except Exception:
                                pass
                        # Still retry after cooldown
                        await asyncio.sleep(60)  # 60 second cooldown for 429

                    if attempt < self.config.max_create_attempts:
                        delay = self.config.retry_delay_seconds * attempt
                        logger.info(f"⏳ Retrying in {delay}s...")
                        await asyncio.sleep(delay)

            # All attempts failed - record in circuit breaker
            circuit.record_failure(last_error)
            
            self.stats["total_failed"] += 1
            
            # Check if this was a quota failure
            is_quota_error, quota_name = self._is_quota_exceeded_error(last_error) if last_error else (False, None)
            if is_quota_error:
                logger.error(f"❌ VM creation failed due to quota: {quota_name}")
                logger.error(f"   Cooldown active for {self._quota_cooldown_seconds}s")
            else:
                logger.error(f"❌ Failed to create VM after {self.config.max_create_attempts} attempts")
                logger.error(f"   Last error: {last_error}")

            return None
        finally:
            # v193.2: Clean up creation tracking AND signal waiting callers
            async with self._vm_lock:
                if creation_id in self.creating_vms:
                    del self.creating_vms[creation_id]
                    logger.debug(f"[v193.2] Cleared VM creation guard: {creation_id}")

                # Store result for waiting callers
                # Note: vm_instance is set on SUCCESS, last_error is set on FAILURE
                # But last_error might also be set from earlier retry attempts even on success
                self._creation_result = vm_instance
                if vm_instance is not None:
                    # Success - clear any error from previous attempts
                    self._creation_error = None
                elif last_error:
                    self._creation_error = str(last_error)
                else:
                    self._creation_error = "VM creation failed (unknown reason)"

                # Signal waiting callers that creation is complete
                if self._creation_event:
                    self._creation_event.set()
                    logger.debug(
                        f"[v193.2] Signaled creation complete: "
                        f"success={vm_instance is not None}, error={self._creation_error}"
                    )

    async def _record_vm_creation_safe(
        self,
        vm_instance: VMInstance,
        components: List[str],
        trigger_reason: str,
        metadata: Optional[Dict] = None,
    ):
        """
        Safely record VM creation in cost tracker with circuit breaker.
        
        This is isolated so cost tracker failures don't affect VM creation.
        """
        if not self.cost_tracker:
            logger.debug("Cost tracker not available - skipping creation record")
            return

        circuit = self._circuit_breakers["cost_tracker"]
        can_execute, _ = circuit.can_execute()
        
        if not can_execute:
            logger.debug("Cost tracker circuit open - skipping creation record")
            return

        try:
            # Try the new method name first, fall back to old name
            if hasattr(self.cost_tracker, 'record_vm_creation'):
                await self.cost_tracker.record_vm_creation(
                    instance_id=vm_instance.instance_id,
                    vm_type=self.config.machine_type,
                    region=self.config.region,
                    zone=self.config.zone,
                    components=components,
                    trigger_reason=trigger_reason,
                    metadata=metadata or {},
                )
            elif hasattr(self.cost_tracker, 'record_vm_created'):
                await self.cost_tracker.record_vm_created(
                    instance_id=vm_instance.instance_id,
                    components=components,
                    trigger_reason=trigger_reason,
                    metadata=metadata or {},
                )
            else:
                logger.warning("Cost tracker has no VM creation recording method")
                return
            
            circuit.record_success()
            logger.debug(f"💰 VM creation recorded in cost tracker: {vm_instance.instance_id}")
            
        except Exception as e:
            circuit.record_failure(e)
            logger.warning(f"⚠️  Failed to record VM creation in cost tracker (non-critical): {e}")

    def _build_instance_config(
        self, vm_name: str, components: List[str], trigger_reason: str, metadata: Dict
    ) -> InstanceType:
        """
        Build GCP Instance configuration.
        
        v1.0.0: Added support for container-based deployment with pre-baked ML dependencies.
        
        When self.config.use_container is True and a valid container_image is set,
        uses Container-Optimized OS with Docker to eliminate the 5-8 minute ml_deps
        installation phase. Falls back to startup script if container deployment fails.
        """

        # Machine type URL
        machine_type_url = f"zones/{self.config.zone}/machineTypes/{self.config.machine_type}"
        
        # ═══════════════════════════════════════════════════════════════════════════
        # v224.0: DEPLOYMENT MODE HIERARCHY (highest priority first)
        # ═══════════════════════════════════════════════════════════════════════════
        # 1. Golden Image - Pre-baked with everything installed (~30-60s startup)
        # 2. Container Mode - Docker with pre-built image (~2-3 min startup)
        # 3. Startup Script - Traditional installation (~10-15 min startup)
        # ═══════════════════════════════════════════════════════════════════════════
        
        # v1.0.0: Determine deployment mode (container vs startup script)
        use_container_mode = (
            self.config.use_container and 
            self.config.container_image and 
            len(self.config.container_image) > 0
        )
        
        # v224.0: Check for golden image (highest priority)
        use_golden_image_mode = False
        golden_image_source = None
        golden_image_disk_size_gb: Optional[int] = None  # Track golden image disk size for VM creation
        
        if self.config.use_golden_image:
            # Check if we have a cached golden image
            now = time.time()
            if (
                self._golden_image_cache is not None 
                and now - self._golden_image_cache_time < self._golden_image_cache_ttl
            ):
                # Use cached golden image info
                golden_image = self._golden_image_cache
                golden_project = self.config.golden_image_project or self.config.project_id
                
                # v233.0: Multi-criteria golden image validation
                _skip_reasons: List[str] = []
                if golden_image.is_stale(self.config.golden_image_max_age_days):
                    _skip_reasons.append(
                        f"stale ({golden_image.age_days:.0f}d > {self.config.golden_image_max_age_days}d)"
                    )
                if self.is_golden_image_suspect(golden_image.name):
                    _skip_reasons.append("flagged SUSPECT by corruption detector")
                if golden_image.is_model_mismatched(self.config.golden_image_model):
                    _skip_reasons.append(
                        f"model mismatch (has '{golden_image.model_name}')"
                    )

                if _skip_reasons:
                    logger.warning(
                        f"⚠️ [GCP] Skipping golden image '{golden_image.name}': "
                        f"{', '.join(_skip_reasons)}"
                    )
                    if self.config.golden_image_fallback:
                        logger.info("   Falling back to container/script deployment")
                else:
                    use_golden_image_mode = True
                    golden_image_source = f"projects/{golden_project}/global/images/{golden_image.name}"
                    golden_image_disk_size_gb = golden_image.disk_size_gb
                    logger.info(
                        f"🌟 [GCP] Using cached golden image: {golden_image.name} "
                        f"(age: {golden_image.age_days:.1f}d, model: {golden_image.model_name}, "
                        f"disk: {golden_image_disk_size_gb}GB)"
                    )
            else:
                # No cached image, but golden image is enabled
                # We'll try to use the image family (GCP auto-selects latest)
                if self.config.golden_image_name:
                    # Explicit image name configured
                    golden_project = self.config.golden_image_project or self.config.project_id
                    golden_image_source = f"projects/{golden_project}/global/images/{self.config.golden_image_name}"
                    use_golden_image_mode = True
                    # Query the actual image to get its disk size
                    try:
                        if hasattr(self, '_images_client') and self._images_client:
                            img_info = self._images_client.get(project=golden_project, image=self.config.golden_image_name)
                            golden_image_disk_size_gb = int(img_info.disk_size_gb) if img_info.disk_size_gb else 100
                            logger.info(f"🌟 [GCP] Using configured golden image: {self.config.golden_image_name} ({golden_image_disk_size_gb}GB)")
                        else:
                            # Default to 100GB for golden images (safe minimum)
                            golden_image_disk_size_gb = 100
                            logger.info(f"🌟 [GCP] Using configured golden image: {self.config.golden_image_name} (defaulting to {golden_image_disk_size_gb}GB)")
                    except Exception as e:
                        # Default to 100GB for golden images (safe minimum)
                        golden_image_disk_size_gb = 100
                        logger.warning(f"⚠️ [GCP] Could not query image size: {e}. Defaulting to {golden_image_disk_size_gb}GB")
                elif self.config.golden_image_family:
                    # Use image family (GCP auto-selects latest)
                    golden_project = self.config.golden_image_project or self.config.project_id
                    golden_image_source = f"projects/{golden_project}/global/images/family/{self.config.golden_image_family}"
                    use_golden_image_mode = True
                    # Query the latest image from family to get its disk size
                    try:
                        if hasattr(self, '_images_client') and self._images_client:
                            img_info = self._images_client.get_from_family(project=golden_project, family=self.config.golden_image_family)
                            golden_image_disk_size_gb = int(img_info.disk_size_gb) if img_info.disk_size_gb else 100
                            logger.info(f"🌟 [GCP] Using golden image family: {self.config.golden_image_family} (latest: {img_info.name}, {golden_image_disk_size_gb}GB)")
                        else:
                            # Default to 100GB for golden images (safe minimum)
                            golden_image_disk_size_gb = 100
                            logger.info(f"🌟 [GCP] Using golden image family: {self.config.golden_image_family} (defaulting to {golden_image_disk_size_gb}GB)")
                    except Exception as e:
                        # Default to 100GB for golden images (safe minimum)
                        golden_image_disk_size_gb = 100
                        logger.warning(f"⚠️ [GCP] Could not query family image size: {e}. Defaulting to {golden_image_disk_size_gb}GB")
        
        # Log deployment mode decision
        if use_golden_image_mode:
            logger.info("🌟 [GCP] Deployment mode: GOLDEN IMAGE (fastest)")
        elif use_container_mode:
            logger.info(f"🐳 [GCP] Deployment mode: CONTAINER ({self.config.container_image})")
        else:
            logger.info("📜 [GCP] Deployment mode: STARTUP SCRIPT (traditional)")
        
        # v224.0: Choose OS image based on deployment mode hierarchy
        if use_golden_image_mode and golden_image_source:
            # Golden image - everything pre-installed
            source_image = golden_image_source
            logger.debug(f"   Source image: {source_image}")
        elif use_container_mode:
            # Container-Optimized OS for Docker-based deployment
            source_image = f"projects/{self.config.container_os_image_project}/global/images/family/{self.config.container_os_image_family}"
            logger.debug(f"   Container OS: {self.config.container_os_image_family}")
        else:
            # Standard Ubuntu for startup script-based deployment
            source_image = f"projects/{self.config.image_project}/global/images/family/{self.config.image_family}"

        # Boot disk configuration
        # v224.1: Dynamic disk sizing - use larger of configured size or golden image size
        # This prevents "disk size smaller than image size" errors when using golden images
        effective_disk_size_gb = self.config.boot_disk_size_gb
        if use_golden_image_mode and golden_image_disk_size_gb:
            effective_disk_size_gb = max(self.config.boot_disk_size_gb, golden_image_disk_size_gb)
            if effective_disk_size_gb > self.config.boot_disk_size_gb:
                logger.info(
                    f"📀 [GCP] Adjusting disk size: {self.config.boot_disk_size_gb}GB → {effective_disk_size_gb}GB "
                    f"(golden image requires {golden_image_disk_size_gb}GB minimum)"
                )
        
        boot_disk = compute_v1.AttachedDisk(
            auto_delete=True,
            boot=True,
            initialize_params=compute_v1.AttachedDiskInitializeParams(
                disk_size_gb=effective_disk_size_gb,
                disk_type=f"zones/{self.config.zone}/diskTypes/{self.config.boot_disk_type}",
                source_image=source_image,
            ),
        )

        # Network interface
        network_interface = compute_v1.NetworkInterface(
            network=f"global/networks/{self.config.network}",
            subnetwork=f"regions/{self.config.region}/subnetworks/{self.config.subnetwork}" if self.config.subnetwork != "default" else None,
            access_configs=[compute_v1.AccessConfig(name="External NAT", type="ONE_TO_ONE_NAT")],
        )

        # Service Account (cloud-platform scope for Secret Manager, Monitoring, Logging)
        service_account = compute_v1.ServiceAccount(
            email="default",
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )

        # Metadata
        # v147.0: Added port and repo URL for startup script configuration
        # v228.0: Dynamic repo URL discovery — always pass to VM metadata
        jarvis_port = os.environ.get("JARVIS_PRIME_PORT", "8000")
        
        # v228.0: Dynamically discover repo URL (never rely on hardcoded fallbacks)
        jarvis_repo_url = os.environ.get("JARVIS_REPO_URL") or os.environ.get("JARVIS_PRIME_REPO_URL", "")
        if not jarvis_repo_url:
            # Auto-detect from golden image builder's discovery logic
            builder = self.get_golden_image_builder()
            if builder:
                jarvis_repo_url = builder._discover_prime_repo_url()
        
        metadata_items = [
            compute_v1.Items(key="jarvis-components", value=",".join(components)),
            compute_v1.Items(key="jarvis-trigger", value=trigger_reason),
            compute_v1.Items(key="jarvis-created-at", value=datetime.now().isoformat()),
            compute_v1.Items(key="jarvis-port", value=jarvis_port),  # v147.0: Port for health checks
            compute_v1.Items(key="jarvis-startup-script-version", value=_STARTUP_SCRIPT_VERSION),  # v235.0
        ]
        
        # v228.0: Always pass repo URL to VM metadata (critical for code availability)
        if jarvis_repo_url:
            metadata_items.append(compute_v1.Items(key="jarvis-repo-url", value=jarvis_repo_url))
            logger.debug(f"   📦 Repo URL passed to VM: {jarvis_repo_url}")
        else:
            logger.warning(
                "⚠️ [GCP] No JARVIS-Prime repo URL discovered. "
                "VM startup script may fail to clone code. "
                "Set JARVIS_PRIME_REPO_URL or JARVIS_REPO_URL."
            )

        # v224.0: Deployment mode handling (golden image, container, or startup script)
        if use_golden_image_mode and golden_image_source:
            # ═══════════════════════════════════════════════════════════════════
            # GOLDEN IMAGE DEPLOYMENT (Fastest - Everything Pre-installed)
            # ═══════════════════════════════════════════════════════════════════
            # Uses a custom VM image with everything pre-baked:
            #   - Python environment with all ML dependencies
            #   - JARVIS-Prime codebase
            #   - Model files already downloaded
            #   - Systemd service configured for auto-start
            #
            # Benefits:
            #   - Startup time: ~30-60 seconds instead of 10-15 minutes!
            #   - No network downloads during startup
            #   - Consistent, tested environment
            #   - Instant model availability
            # ═══════════════════════════════════════════════════════════════════
            
            # Mark as golden image deployment
            metadata_items.append(
                compute_v1.Items(key="jarvis-deployment-mode", value="golden-image")
            )
            metadata_items.append(
                compute_v1.Items(key="jarvis-golden-image-source", value=golden_image_source)
            )
            
            # v229.0: Use shared golden startup script (single source of truth)
            golden_startup_script = self._generate_golden_startup_script()
            metadata_items.append(
                compute_v1.Items(key="startup-script", value=golden_startup_script)
            )
            
            logger.info("   🌟 Golden image deployment configured")
            logger.info(f"   📀 Image: {golden_image_source}")
            logger.info("   ⚡ Expected startup: ~30-60 seconds")
            
        elif use_container_mode:
            # ═══════════════════════════════════════════════════════════════════
            # CONTAINER-BASED DEPLOYMENT (Pre-baked ML Dependencies)
            # ═══════════════════════════════════════════════════════════════════
            # Uses Container-Optimized OS with a Docker image that has all ML
            # dependencies pre-installed. This eliminates Phase 3 (ml_deps)
            # which normally takes 5-8 minutes.
            #
            # Benefits:
            #   - Startup time: ~2-3 min instead of ~8-10 min
            #   - Consistent environment across all VMs
            #   - Reduced network dependencies during startup
            # ═══════════════════════════════════════════════════════════════════
            
            # Parse container environment variables
            container_env = {}
            for pair in self.config.container_env_vars.split(","):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    container_env[key.strip()] = value.strip()
            
            # Add JARVIS-specific environment variables
            container_env["JARVIS_PORT"] = jarvis_port
            container_env["JARVIS_COMPONENTS"] = ",".join(components)
            
            # Build container manifest for Container-Optimized OS
            # This uses the konlet (Container-Optimized OS Container Agent)
            container_manifest = self._build_container_manifest(
                image=self.config.container_image,
                port=int(jarvis_port),
                env_vars=container_env,
            )
            
            # Add container declaration to metadata
            metadata_items.append(
                compute_v1.Items(key="gce-container-declaration", value=container_manifest)
            )
            
            # Mark as container-based deployment
            metadata_items.append(
                compute_v1.Items(key="jarvis-deployment-mode", value="container")
            )
            metadata_items.append(
                compute_v1.Items(key="jarvis-container-image", value=self.config.container_image)
            )
            
            logger.info(f"   🐳 Container deployment configured")
            logger.info(f"   📦 Image: {self.config.container_image}")
            logger.info(f"   🔧 Env vars: {len(container_env)} configured")
            
        else:
            # ═══════════════════════════════════════════════════════════════════
            # STARTUP SCRIPT DEPLOYMENT (Traditional)
            # ═══════════════════════════════════════════════════════════════════
            # Uses Ubuntu with startup script for ML dependency installation.
            # This is the fallback when container mode is not enabled.
            #
            # Note: The startup script now includes smart skip logic that
            # detects pre-installed packages, so it will still be faster if
            # the VM image has packages cached.
            # ═══════════════════════════════════════════════════════════════════
            
            # Add startup script
            # v149.2: CRITICAL FIX - Do NOT inject auto-shutdown!
            # The startup script (gcp_vm_startup.sh) is designed for LONG-RUNNING services:
            # - Phase 1: Starts health endpoint quickly
            # - Phase 2: Runs in BACKGROUND (nohup ... &)
            # - Main script exits, but Phase 2 keeps running
            #
            # If we inject "shutdown -h now", it runs IMMEDIATELY after Phase 1,
            # killing the VM before Phase 2 (the real inference server) starts.
            # This was the ROOT CAUSE of "GCP VM unreachable" after 90s timeout.
            #
            # The VM's lifecycle is controlled by:
            # 1. max_run_duration in Spot VM config (hard GCP limit)
            # 2. Explicit terminate_vm() calls from JARVIS
            # 3. GCP preemption (for Spot VMs)
            if self.config.startup_script_path and os.path.exists(self.config.startup_script_path):
                with open(self.config.startup_script_path, "r") as f:
                    startup_script = f.read()
                
                # v149.2: REMOVED auto-shutdown injection - this killed VMs prematurely
                # The VM will be managed by max_run_duration and explicit termination calls
                    
                metadata_items.append(compute_v1.Items(key="startup-script", value=startup_script))
            
            # Mark as startup script deployment
            metadata_items.append(
                compute_v1.Items(key="jarvis-deployment-mode", value="startup-script")
            )

        # Build instance
        instance = compute_v1.Instance(
            name=vm_name,
            machine_type=machine_type_url,
            disks=[boot_disk],
            network_interfaces=[network_interface],
            service_accounts=[service_account],
            metadata=compute_v1.Metadata(items=metadata_items),
            tags=compute_v1.Tags(items=["jarvis", "backend", "spot-vm", "jarvis-node"]),
            labels={"created-by": "jarvis", "type": "backend", "vm-class": "spot"},
        )

        # Configure as Spot VM with Hard Duration Limit
        # This is the "Dead Man's Switch": GCP kills the VM after max_run_duration seconds
        # even if the local script crashes or loses connectivity.
        if self.config.use_spot:
            # Calculate duration in seconds (default 3 hours = 10800s)
            max_duration_seconds = int(self.config.max_vm_lifetime_hours * 3600)
            
            # Ensure duration is valid (must be between 60s and 604800s for Spot)
            max_duration_seconds = max(60, min(max_duration_seconds, 604800))
            
            instance.scheduling = compute_v1.Scheduling(
                preemptible=True,
                on_host_maintenance="TERMINATE",
                automatic_restart=False,
                provisioning_model="SPOT",
                instance_termination_action="DELETE",
                max_run_duration=compute_v1.Duration(seconds=max_duration_seconds)
            )

        return instance

    def _build_container_manifest(
        self, 
        image: str, 
        port: int = 8000, 
        env_vars: Optional[Dict[str, str]] = None
    ) -> str:
        """
        Build container declaration manifest for Container-Optimized OS.
        
        v1.0.0: Creates YAML manifest for konlet (Container-Optimized OS Container Agent)
        that specifies how to run the Docker container on the VM.
        
        Args:
            image: Docker image URL (e.g., gcr.io/project/image:tag)
            port: Port to expose (default 8000)
            env_vars: Environment variables for the container
            
        Returns:
            YAML string for gce-container-declaration metadata
        """
        env_vars = env_vars or {}
        
        # Build environment variable list
        env_list = [{"name": k, "value": str(v)} for k, v in env_vars.items()]
        
        # Container manifest spec (konlet format)
        manifest = {
            "spec": {
                "containers": [{
                    "name": "jarvis-inference",
                    "image": image,
                    "env": env_list,
                    "stdin": False,
                    "tty": False,
                    "ports": [{
                        "containerPort": port,
                        "hostPort": port,
                        "protocol": "TCP",
                    }],
                    "securityContext": {
                        "privileged": False,
                    },
                    "volumeMounts": [{
                        "name": "jarvis-data",
                        "mountPath": "/opt/jarvis-prime/data",
                        "readOnly": False,
                    }],
                }],
                "volumes": [{
                    "name": "jarvis-data",
                    "emptyDir": {},
                }],
                "restartPolicy": "Always",
            },
        }
        
        # Convert to YAML
        return yaml.dump(manifest, default_flow_style=False)

    # ═══════════════════════════════════════════════════════════════════════════════
    # v224.0: GOLDEN IMAGE MANAGEMENT METHODS
    # ═══════════════════════════════════════════════════════════════════════════════
    
    def get_golden_image_builder(self) -> GoldenImageBuilder:
        """
        Get or create the golden image builder instance.
        
        Returns:
            GoldenImageBuilder instance for this manager
        """
        if self._golden_image_builder is None:
            self._golden_image_builder = GoldenImageBuilder(
                config=self.config,
                logger=logger,
            )
        return self._golden_image_builder
    
    async def check_golden_image_availability(self) -> Dict[str, Any]:
        """
        Check if a golden image is available and suitable for use.
        
        This method checks:
        1. If golden image deployment is enabled
        2. If a golden image exists in the configured family
        3. If the image is not stale
        
        Returns:
            Dict with availability status:
            {
                "available": bool,
                "enabled": bool,
                "image_info": Optional[GoldenImageInfo],
                "is_stale": bool,
                "recommendation": str,
                "message": str,
            }
        """
        result = {
            "available": False,
            "enabled": self.config.use_golden_image,
            "image_info": None,
            "is_stale": False,
            "recommendation": "DISABLED",
            "message": "",
        }
        
        if not self.config.use_golden_image:
            result["message"] = "Golden image deployment is disabled. Enable with JARVIS_GCP_USE_GOLDEN_IMAGE=true"
            return result
        
        builder = self.get_golden_image_builder()
        status = await builder.check_golden_image_status()
        
        result["available"] = status["available"]
        result["image_info"] = status.get("latest_image")
        result["is_stale"] = status.get("is_stale", False)
        result["recommendation"] = status["recommendation"]
        result["message"] = status["message"]
        
        # Update cache if we got a valid image
        if status["available"] and status.get("latest_image"):
            self._golden_image_cache = status["latest_image"]
            self._golden_image_cache_time = time.time()
        
        return result
    
    async def create_golden_image(
        self,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Tuple[bool, str, Optional[GoldenImageInfo]]:
        """
        Create a new golden image with everything pre-installed.
        
        This is a long-running operation (10-20 minutes) that creates a
        VM, installs everything, and creates an image from it.
        
        Args:
            progress_callback: Optional callback(progress_pct, message)
            
        Returns:
            Tuple of (success, message, GoldenImageInfo or None)
        """
        builder = self.get_golden_image_builder()
        success, message, image_info = await builder.create_golden_image(
            progress_callback=progress_callback
        )
        
        # Update cache on success
        if success and image_info:
            self._golden_image_cache = image_info
            self._golden_image_cache_time = time.time()
        
        return success, message, image_info
    
    async def list_golden_images(self) -> List[GoldenImageInfo]:
        """
        List all available golden images.
        
        Returns:
            List of GoldenImageInfo sorted by creation time (newest first)
        """
        builder = self.get_golden_image_builder()
        return await builder.list_golden_images()
    
    async def cleanup_old_golden_images(self, keep_count: int = 3) -> Tuple[int, List[str]]:
        """
        Clean up old golden images, keeping only the most recent ones.
        
        Args:
            keep_count: Number of recent images to keep (default: 3)
            
        Returns:
            Tuple of (deleted_count, list of deleted image names)
        """
        builder = self.get_golden_image_builder()
        return await builder.cleanup_old_images(keep_count=keep_count)
    
    def get_golden_image_status_summary(self) -> Dict[str, Any]:
        """
        Get a quick summary of golden image status from cache.
        
        This is a synchronous method that returns cached data.
        For fresh data, use check_golden_image_availability().
        
        Returns:
            Dict with status summary
        """
        return {
            "enabled": self.config.use_golden_image,
            "cached_image": self._golden_image_cache.name if self._golden_image_cache else None,
            "cache_age_seconds": time.time() - self._golden_image_cache_time if self._golden_image_cache else None,
            "cache_valid": (
                self._golden_image_cache is not None 
                and time.time() - self._golden_image_cache_time < self._golden_image_cache_ttl
            ),
            "image_family": self.config.golden_image_family,
            "max_age_days": self.config.golden_image_max_age_days,
            "auto_rebuild": self.config.golden_image_auto_rebuild,
            "fallback_enabled": self.config.golden_image_fallback,
        }

    def record_golden_image_stall(
        self,
        image_name: str,
        stall_progress: float,
        diagnostic: Optional[GoldenImageDiagnostic] = None,
    ) -> bool:
        """v233.0: Record a stall event and detect corruption with three-tier logic.

        Detection tiers (checked in order):
        1. IMMEDIATE — critical diagnostic failures flag suspect on first stall
           (CORRUPTED_MODEL_CACHE, SERVICE_NEVER_STARTED, DEPENDENCY_DRIFT)
           NOT for transient types (NETWORK_TIMEOUT, OUT_OF_MEMORY) which need 2+
        2. DIAGNOSTIC CLUSTERING — 2+ diagnostics with same failure_type → suspect
        3. PROGRESS CLUSTERING — N stalls at same % (existing v232.0 median logic)

        Args:
            image_name: Name of the golden image that was used
            stall_progress: Progress percentage at which the VM stalled
            diagnostic: Optional diagnostic from serial console analysis

        Returns:
            True if this stall caused the image to be flagged as suspect
        """
        from collections import Counter

        # ── Track progress history ────────────────────────────────────────
        history = self._golden_image_stall_history.setdefault(image_name, [])
        history.append(stall_progress)

        # ── Track diagnostic history ─────────────────────────────────────
        diag_history = self._golden_image_diagnostic_history.setdefault(image_name, [])
        if diagnostic:
            diag_history.append({
                "failure_type": diagnostic.failure_type.value,
                "confidence": diagnostic.confidence,
                "stall_progress": stall_progress,
                "timestamp": diagnostic.timestamp,
                "patterns": diagnostic.detected_patterns[:3],
            })

        threshold = self._golden_image_corruption_threshold

        # ══════════════════════════════════════════════════════════════════
        # Tier 1: IMMEDIATE flagging for critical image-level failures
        # ══════════════════════════════════════════════════════════════════
        if diagnostic and diagnostic.is_critical_failure:
            was_new = image_name not in self._golden_image_suspect_set
            self._golden_image_suspect_set.add(image_name)
            if was_new:
                logger.error(
                    f"[CorruptionDetect] Golden image '{image_name}' flagged SUSPECT "
                    f"on FIRST stall — critical failure:\n"
                    f"    Type: {diagnostic.failure_type.value}\n"
                    f"    Confidence: {diagnostic.confidence:.0%}\n"
                    f"    Patterns: {diagnostic.detected_patterns[:5]}\n"
                    f"    Progress: {stall_progress:.0f}%"
                )
                self._maybe_trigger_rebuild(image_name, diagnostic)
            return was_new

        # ══════════════════════════════════════════════════════════════════
        # Tier 2: DIAGNOSTIC CLUSTERING — same failure type recurring
        # ══════════════════════════════════════════════════════════════════
        if len(diag_history) >= 2:
            recent_diags = diag_history[-threshold:] if len(diag_history) >= threshold else diag_history
            failure_types = [d["failure_type"] for d in recent_diags]
            type_counts = Counter(failure_types)
            if type_counts:
                most_common_type, most_common_count = type_counts.most_common(1)[0]
                # Unknown doesn't count for clustering
                if (
                    most_common_type != GoldenImageFailureType.UNKNOWN.value
                    and most_common_count >= 2
                ):
                    was_new = image_name not in self._golden_image_suspect_set
                    self._golden_image_suspect_set.add(image_name)
                    if was_new:
                        logger.error(
                            f"[CorruptionDetect] Golden image '{image_name}' flagged SUSPECT — "
                            f"diagnostic clustering: {most_common_count}x '{most_common_type}'"
                        )
                        self._maybe_trigger_rebuild(image_name, diagnostic)
                    return was_new

        # ══════════════════════════════════════════════════════════════════
        # Tier 3: PROGRESS CLUSTERING — N stalls at same % (v232.0 logic)
        # ══════════════════════════════════════════════════════════════════
        if len(history) < threshold:
            return False

        recent = history[-threshold:]
        sorted_recent = sorted(recent)
        median = sorted_recent[len(sorted_recent) // 2]

        if median == 0:
            return False

        tolerance = self._golden_image_stall_tolerance_pct
        all_similar = all(
            abs(p - median) <= (tolerance / 100.0) * median
            for p in recent
        )

        if all_similar:
            was_new = image_name not in self._golden_image_suspect_set
            self._golden_image_suspect_set.add(image_name)
            if was_new:
                logger.error(
                    f"[CorruptionDetect] Golden image '{image_name}' flagged SUSPECT — "
                    f"last {threshold} VMs all stalled near {median:.0f}% "
                    f"(stalls: {[f'{p:.0f}%' for p in recent]})"
                )
                self._maybe_trigger_rebuild(image_name, diagnostic)
            return was_new
        return False

    def _maybe_trigger_rebuild(
        self,
        image_name: str,
        diagnostic: Optional[GoldenImageDiagnostic] = None,
    ) -> None:
        """v233.0: Conditionally trigger background golden image rebuild."""
        if not self.config.golden_image_auto_rebuild:
            return
        reason = (
            f"corruption detected: {diagnostic.failure_type.value}"
            if diagnostic else "repeated stalls"
        )
        try:
            create_safe_task(
                self._trigger_golden_image_rebuild(image_name, reason),
                name=f"golden_image_rebuild_{image_name}",
            )
            logger.info(
                f"[CorruptionDetect] Auto-rebuild triggered for '{image_name}' "
                f"(JARVIS_GCP_GOLDEN_IMAGE_AUTO_REBUILD=true)"
            )
        except RuntimeError:
            # No event loop running (unlikely in async context)
            logger.warning("[CorruptionDetect] Cannot trigger rebuild — no event loop")

    def is_golden_image_suspect(self, image_name: str) -> bool:
        """v232.0: Check if a golden image has been flagged as suspect."""
        return image_name in self._golden_image_suspect_set

    def get_golden_image_corruption_status(self) -> Dict[str, Any]:
        """v233.0: Get diagnostic info about golden image corruption detection."""
        return {
            "suspect_images": list(self._golden_image_suspect_set),
            "stall_history": {
                name: [round(p, 1) for p in hist]
                for name, hist in self._golden_image_stall_history.items()
            },
            "diagnostic_history": {
                name: diag_list[-5:]
                for name, diag_list in self._golden_image_diagnostic_history.items()
            },
            "corruption_threshold": self._golden_image_corruption_threshold,
            "stall_tolerance_pct": self._golden_image_stall_tolerance_pct,
            "rebuild_in_progress": self._golden_rebuild_in_progress,
        }

    async def _trigger_golden_image_rebuild(
        self, image_name: str, reason: str,
    ) -> None:
        """v233.0: Fire-and-forget background rebuild of golden image.

        Protected by _golden_rebuild_in_progress mutex to prevent concurrent
        rebuilds when multiple stalls trigger in quick succession.
        """
        if self._golden_rebuild_in_progress:
            logger.info(
                f"[AutoRebuild] Already in progress, skipping duplicate for '{image_name}'"
            )
            return
        self._golden_rebuild_in_progress = True
        try:
            logger.info(
                f"[AutoRebuild] Starting background rebuild "
                f"(replacing '{image_name}', reason: {reason})..."
            )
            builder = self.get_golden_image_builder()
            result = await builder.build_golden_image()
            if result:
                logger.info(
                    f"[AutoRebuild] New golden image built: {result.name}\n"
                    f"    Model: {result.model_name}\n"
                    f"    Old suspect '{image_name}' cleared from suspect set"
                )
                self._golden_image_suspect_set.discard(image_name)
            else:
                logger.error(
                    f"[AutoRebuild] Build failed for replacement of '{image_name}'"
                )
        except Exception as e:
            logger.error(f"[AutoRebuild] Background rebuild error: {e}")
        finally:
            self._golden_rebuild_in_progress = False

    async def _wait_for_operation(self, operation, timeout: int = 300):
        """
        Wait for a GCP zone operation to complete.
        
        Uses the ZoneOperationsClient to poll operation status correctly.
        This fixes the 'Unknown field for Instance: target_link' error.
        """
        start_time = time.time()
        operation_name = operation.name
        
        logger.debug(f"Waiting for operation: {operation_name}")

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(f"Operation '{operation_name}' timed out after {timeout}s")

            # Check operation status
            if operation.status == compute_v1.Operation.Status.DONE:
                if operation.error:
                    errors = [error.message for error in operation.error.errors]
                    raise Exception(f"Operation failed: {', '.join(errors)}")
                logger.debug(f"Operation '{operation_name}' completed successfully")
                return

            await asyncio.sleep(2)

            # Refresh operation status using ZoneOperationsClient (correct approach)
            try:
                operation = await asyncio.to_thread(
                    self.zone_operations_client.get,
                    project=self.config.project_id,
                    zone=self.config.zone,
                    operation=operation_name,
                )
            except Exception as e:
                logger.warning(f"Error polling operation status: {e}")
                # Continue waiting - operation might still complete
                await asyncio.sleep(2)

    async def terminate_vm(self, vm_name: str, reason: str = "Manual termination") -> bool:
        """
        v134.0: Terminate a VM instance with existence verification and circuit breaker protection.
        v153.0: Added invincible VM protection — persistent VMs cannot be terminated by
                automated systems (cost-cutting, idle detection, memory pressure, max lifetime).

        ROOT CAUSE FIX for 404 NotFound errors:
        This method now verifies VM existence in GCP before attempting delete operations.
        This prevents spurious 404 errors that occur when:
        1. Spot VMs are preempted by GCP (can happen at any time)
        2. VMs are deleted manually via GCP Console or gcloud CLI
        3. VMs are deleted by another process or previous session
        4. Stale entries exist in managed_vms from previous runs

        Args:
            vm_name: Name of the VM to terminate
            reason: Reason for termination (for logging/tracking)

        Returns:
            True if terminated successfully (or VM doesn't exist), False otherwise
        """
        # v153.0: INVINCIBLE VM PROTECTION — central guard for ALL termination paths.
        # This prevents the monitoring loop's cost-cutting, idle detection, memory
        # pressure, and max lifetime checks from deleting persistent VMs.
        is_invincible = vm_name.startswith("jarvis-prime-node")

        # Also check metadata if VM is tracked
        if not is_invincible and vm_name in self.managed_vms:
            vm_meta = self.managed_vms[vm_name].metadata or {}
            is_invincible = (
                vm_meta.get("vm_class") == "invincible"
                or vm_meta.get("labels", {}).get("vm-class") == "invincible"
            )

        if is_invincible:
            logger.warning(
                f"🛡️ [InvincibleGuard] BLOCKED termination of persistent VM '{vm_name}' "
                f"(reason: {reason}). Invincible VMs cannot be terminated by automated systems. "
                f"Use GCP Console or gcloud CLI for manual override."
            )
            return False

        async with self._vm_lock:
            if vm_name not in self.managed_vms:
                logger.warning(f"⚠️  VM not found in managed VMs: {vm_name}")
                # Try to delete anyway in case it exists in GCP but not tracked
                return await self._force_delete_vm(vm_name, reason)

            vm = self.managed_vms[vm_name]

        # Check circuit breaker
        circuit = self._circuit_breakers["vm_delete"]
        can_execute, circuit_reason = circuit.can_execute()

        if not can_execute:
            logger.warning(f"🔌 VM termination blocked by circuit breaker: {circuit_reason}")
            self.stats["circuit_breaks"] += 1
            return False

        # v134.0: ROOT CAUSE FIX - Check if VM actually exists in GCP first
        # This prevents 404 errors that would trip the circuit breaker
        exists, gcp_status = await self._check_vm_exists_in_gcp(vm_name)

        if not exists:
            logger.info(
                f"✅ VM '{vm_name}' does not exist in GCP - cleaning up local tracking "
                f"(may have been preempted, manually deleted, or never created)"
            )
            # Update local tracking to reflect reality
            async with self._vm_lock:
                vm.state = VMState.TERMINATED
                self.stats["total_terminated"] += 1
                self.stats["current_active"] = max(0, self.stats["current_active"] - 1)
                self.stats["total_cost"] += vm.total_cost
                if vm_name in self.managed_vms:
                    del self.managed_vms[vm_name]
            # Record success - VM is in desired state (terminated/non-existent)
            circuit.record_success()
            return True

        # v134.0: Check if VM is already in a terminal state
        if gcp_status in ("TERMINATED", "STOPPING"):
            logger.info(
                f"ℹ️  VM '{vm_name}' is already {gcp_status} - cleaning up tracking"
            )
            async with self._vm_lock:
                vm.state = VMState.TERMINATED
                self.stats["total_terminated"] += 1
                self.stats["current_active"] = max(0, self.stats["current_active"] - 1)
                self.stats["total_cost"] += vm.total_cost
                if vm_name in self.managed_vms:
                    del self.managed_vms[vm_name]
            circuit.record_success()
            return True

        logger.info(f"🛑 Terminating VM: {vm_name} (Reason: {reason}, GCP Status: {gcp_status})")

        try:
            # Update cost before termination
            vm.update_cost()

            # Record termination in cost tracker (isolated)
            await self._record_vm_termination_safe(vm, reason)

            # Delete the VM
            operation = await asyncio.to_thread(
                self.instances_client.delete,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=vm_name,
            )

            await self._wait_for_operation(operation)

            # Update tracking with lock protection
            async with self._vm_lock:
                vm.state = VMState.TERMINATED
                self.stats["total_terminated"] += 1
                self.stats["current_active"] = max(0, self.stats["current_active"] - 1)
                self.stats["total_cost"] += vm.total_cost

                # Remove from managed VMs
                if vm_name in self.managed_vms:
                    del self.managed_vms[vm_name]

            # Circuit breaker success
            circuit.record_success()

            logger.info(f"✅ VM terminated: {vm_name}")
            logger.info(f"   Uptime: {vm.uptime_hours:.2f}h")
            logger.info(f"   Cost: ${vm.total_cost:.4f}")

            return True

        except Exception as e:
            error_str = str(e).lower()
            # v134.0: Handle 404 gracefully if VM was deleted between our check and delete
            is_not_found = (
                "404" in error_str or
                "not found" in error_str or
                "notfound" in error_str
            )

            if is_not_found:
                logger.info(
                    f"✅ VM '{vm_name}' was deleted between existence check and delete call "
                    f"(likely preempted or deleted by another process)"
                )
                # Clean up tracking - VM is in desired state
                async with self._vm_lock:
                    vm.state = VMState.TERMINATED
                    self.stats["total_terminated"] += 1
                    self.stats["current_active"] = max(0, self.stats["current_active"] - 1)
                    self.stats["total_cost"] += vm.total_cost
                    if vm_name in self.managed_vms:
                        del self.managed_vms[vm_name]
                circuit.record_success()  # Desired state achieved
                return True

            # Actual error - record failure
            circuit.record_failure(e)
            log_component_failure(
                "gcp-vm",
                f"Failed to terminate VM {vm_name}",
                error=e,
                vm_name=vm_name,
            )
            return False

    async def _check_vm_exists_in_gcp(self, vm_name: str) -> Tuple[bool, Optional[str]]:
        """
        v134.0: Check if a VM actually exists in GCP before attempting operations.

        ROOT CAUSE FIX for 404 errors during VM termination:
        The 404 NotFound error occurs when we try to delete a VM that:
        1. Was preempted by GCP (Spot VMs can be reclaimed at any time)
        2. Was deleted manually via GCP Console or gcloud CLI
        3. Was deleted by another process or previous session
        4. Never existed (stale entry in managed_vms from previous run)

        This method queries GCP to verify VM existence before delete operations.

        Args:
            vm_name: Name of the VM to check

        Returns:
            Tuple of (exists: bool, status: Optional[str])
            - If VM exists: (True, current_status)
            - If VM doesn't exist: (False, None)
            - On error: (False, None) with logged warning
        """
        try:
            instance = await asyncio.to_thread(
                self.instances_client.get,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=vm_name,
            )
            return (True, instance.status)

        except Exception as e:
            error_str = str(e).lower()
            # Check for 404 NotFound errors (various formats from GCP client)
            is_not_found = (
                "404" in error_str or
                "not found" in error_str or
                "notfound" in error_str or
                "does not exist" in error_str or
                "was not found" in error_str
            )

            if is_not_found:
                logger.debug(f"[VMExists] VM '{vm_name}' does not exist in GCP")
                return (False, None)

            # Other errors - log but treat as not existing to be safe
            logger.warning(
                f"[VMExists] Error checking VM '{vm_name}' existence: {e}. "
                f"Treating as non-existent to prevent 404 on delete."
            )
            return (False, None)

    async def _force_delete_vm(self, vm_name: str, reason: str) -> bool:
        """
        v134.0: Force delete a VM that may exist in GCP but not in our tracking.
        v153.0: Added invincible VM protection.

        ROOT CAUSE FIX: Now checks if VM exists before attempting delete to
        prevent 404 NotFound errors from cluttering logs and circuit breaker.
        """
        # v153.0: Invincible VM protection — same guard as terminate_vm()
        if vm_name.startswith("jarvis-prime-node"):
            logger.warning(
                f"🛡️ [InvincibleGuard] BLOCKED force-deletion of persistent VM '{vm_name}' "
                f"(reason: {reason}). Use GCP Console or gcloud CLI for manual override."
            )
            return False

        # v134.0: Check if VM actually exists in GCP first
        exists, status = await self._check_vm_exists_in_gcp(vm_name)

        if not exists:
            logger.info(
                f"✅ VM '{vm_name}' does not exist in GCP - nothing to delete "
                f"(may have been preempted, manually deleted, or never created)"
            )
            return True  # Return True because the desired state (VM deleted) is achieved

        try:
            logger.info(f"🗑️  Force-deleting untracked VM: {vm_name} (status: {status})")

            operation = await asyncio.to_thread(
                self.instances_client.delete,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=vm_name,
            )

            await self._wait_for_operation(operation)
            logger.info(f"✅ Force-deleted VM: {vm_name}")
            return True

        except Exception as e:
            error_str = str(e).lower()
            # Handle race condition: VM was deleted between our check and delete call
            is_not_found = (
                "404" in error_str or
                "not found" in error_str or
                "notfound" in error_str
            )

            if is_not_found:
                logger.info(
                    f"✅ VM '{vm_name}' was deleted between existence check and delete call "
                    f"(likely preempted or deleted by another process)"
                )
                return True  # Desired state achieved

            logger.warning(f"⚠️  Force delete failed for {vm_name}: {e}")
            return False

    async def recover_stalled_vm(
        self,
        vm_name: str,
        stall_progress: float,
        max_retries: int = 0,
        recovery_timeout: Optional[float] = None,
        progress_callback: Optional[Callable[[int, str, str], None]] = None,
    ) -> Dict[str, Any]:
        """
        v3.1: Terminate a stalled VM and optionally retry with a fresh golden image.

        Called when stall detection determines a VM has stopped making progress
        (e.g., stuck at 58% for >60s during golden image startup).

        Args:
            vm_name: Name of the stalled VM instance
            stall_progress: Last known progress percentage when stall was detected
            max_retries: Number of remaining retry attempts (0 = terminate only)
            recovery_timeout: Timeout for VM recreation (default from env)
            progress_callback: Optional callback for progress reporting

        Returns:
            Dict with keys:
                terminated (bool): Whether the stuck VM was terminated
                retried (bool): Whether recreation was attempted
                new_vm_ready (bool): Whether the new VM is ready
                new_vm_ip (Optional[str]): IP address of the new VM
                error (Optional[str]): Error description if something failed
                status_message (str): Human-readable summary
        """
        # v232.1: Default 450s (was 300s) — GCP golden image VM creation + boot + health
        # can legitimately take 4-5 minutes. Configurable via env var.
        recovery_timeout = recovery_timeout or float(
            os.environ.get("JARVIS_GCP_RECOVERY_TIMEOUT", "450")
        )

        result: Dict[str, Any] = {
            "terminated": False,
            "retried": False,
            "new_vm_ready": False,
            "new_vm_ip": None,
            "error": None,
            "status_message": "",
        }

        # ── Step 0 (v232.0): Extract golden image metadata BEFORE termination ──
        _stall_golden_image_name: Optional[str] = None
        try:
            _status, _meta, _err = await self._describe_instance_full(vm_name)
            if _meta:
                _golden_src = _meta.get("jarvis-golden-image-source", "")
                if _golden_src:
                    # Extract image name from "projects/.../images/IMAGE_NAME"
                    _stall_golden_image_name = _golden_src.rstrip("/").split("/")[-1]
        except Exception as _e:
            logger.debug(f"[Recovery] Could not extract golden image metadata: {_e}")

        # ── Step 0b (v233.0): Run diagnostic BEFORE terminating VM ─────
        _stall_diagnostic: Optional["GoldenImageDiagnostic"] = None
        try:
            _stall_diagnostic = await self._diagnose_golden_image_stall(
                vm_name, stall_progress,
            )
            result["diagnostic"] = _stall_diagnostic.to_dict()
        except Exception as _diag_err:
            logger.warning(
                f"[Recovery] Diagnostic failed (non-fatal): {_diag_err}"
            )

        # ── Step 1: Terminate the stuck VM ──────────────────────────────
        logger.warning(
            f"[Recovery] Terminating stalled VM '{vm_name}' "
            f"(stuck at {stall_progress:.0f}%)"
        )
        try:
            terminated = await self.terminate_vm(
                vm_name=vm_name,
                reason=f"Stalled at {stall_progress:.0f}% during golden image startup",
            )
            result["terminated"] = terminated
            if not terminated:
                result["error"] = f"Failed to terminate stalled VM '{vm_name}'"
                result["status_message"] = result["error"]
                logger.error(f"[Recovery] {result['error']}")
                return result
            logger.info(f"[Recovery] Stalled VM '{vm_name}' terminated successfully")

            # v232.0 + v233.0: Record stall for corruption detection (with diagnostic)
            if _stall_golden_image_name:
                is_suspect = self.record_golden_image_stall(
                    _stall_golden_image_name, stall_progress,
                    diagnostic=_stall_diagnostic,
                )
                if is_suspect:
                    logger.warning(
                        f"[Recovery] Golden image '{_stall_golden_image_name}' now flagged "
                        f"as SUSPECT — will be skipped in future VM creation"
                    )
        except Exception as e:
            result["error"] = f"Exception terminating stalled VM: {e}"
            result["status_message"] = result["error"]
            logger.error(f"[Recovery] {result['error']}")
            return result

        # ── Step 2: Check if retry is viable ────────────────────────────
        if max_retries <= 0:
            result["status_message"] = (
                f"Stalled VM terminated. No retries remaining."
            )
            logger.info(f"[Recovery] {result['status_message']}")
            return result

        # Check vm_create circuit breaker
        circuit = self._circuit_breakers.get("vm_create")
        if circuit:
            can_execute, cb_reason = circuit.can_execute()
            if not can_execute:
                result["error"] = (
                    f"Circuit breaker OPEN for VM creation: {cb_reason}"
                )
                result["status_message"] = result["error"]
                logger.warning(f"[Recovery] {result['error']}")
                return result

        # ── Step 3: Retry with fresh golden image VM ────────────────────
        result["retried"] = True
        logger.info(
            f"[Recovery] Retrying VM creation with fresh golden image "
            f"(timeout={recovery_timeout}s)..."
        )

        try:
            success, ip_address, status_msg = await asyncio.wait_for(
                asyncio.shield(self.ensure_static_vm_ready(
                    progress_callback=progress_callback,
                )),
                timeout=recovery_timeout,
            )

            if success and ip_address:
                result["new_vm_ready"] = True
                result["new_vm_ip"] = ip_address
                result["status_message"] = (
                    f"Recovery succeeded: new VM ready at {ip_address}"
                )
                logger.info(f"[Recovery] {result['status_message']}")
            else:
                result["error"] = f"VM recreation completed but not ready: {status_msg}"
                result["status_message"] = result["error"]
                logger.warning(f"[Recovery] {result['error']}")

        except asyncio.TimeoutError:
            result["error"] = (
                f"VM recreation timed out after {recovery_timeout}s"
            )
            result["status_message"] = result["error"]
            logger.error(f"[Recovery] {result['error']}")
        except Exception as e:
            result["error"] = f"VM recreation failed: {e}"
            result["status_message"] = result["error"]
            logger.error(f"[Recovery] {result['error']}")

        return result

    async def _record_vm_termination_safe(self, vm: VMInstance, reason: str):
        """
        Safely record VM termination in cost tracker with circuit breaker.

        This is isolated so cost tracker failures don't affect VM termination.
        """
        if not self.cost_tracker:
            logger.debug("Cost tracker not available - skipping termination record")
            return

        circuit = self._circuit_breakers["cost_tracker"]
        can_execute, _ = circuit.can_execute()
        
        if not can_execute:
            logger.debug("Cost tracker circuit open - skipping termination record")
            return

        try:
            # Try the new method name first, fall back to old name
            if hasattr(self.cost_tracker, 'record_vm_termination'):
                await self.cost_tracker.record_vm_termination(
                    instance_id=vm.instance_id,
                    reason=reason,
                    total_cost=vm.total_cost,
                )
            elif hasattr(self.cost_tracker, 'record_vm_deleted'):
                await self.cost_tracker.record_vm_deleted(
                    instance_id=vm.instance_id,
                    was_orphaned=False,
                    actual_cost=vm.total_cost,
                )
            else:
                logger.warning("Cost tracker has no VM termination recording method")
                return
            
            circuit.record_success()
            logger.debug(f"💰 VM termination recorded in cost tracker: {vm.instance_id}")
            
        except Exception as e:
            circuit.record_failure(e)
            logger.warning(f"⚠️  Failed to record VM termination in cost tracker (non-critical): {e}")

    def _publish_vm_state(self) -> None:
        """
        Publish VM state to shared filesystem for cross-repo coordination.

        Writes to ~/.jarvis/cross_repo/gcp/:
        - discovered_endpoints.json: Format compatible with J-Prime's gcp_cross_repo_bridge.py
        - jarvis_vms.json: Full debug state for troubleshooting

        Called once per monitoring loop cycle (not per-VM).
        Synchronous and non-blocking — just writes JSON to disk.
        """
        cross_repo_dir = os.path.expanduser("~/.jarvis/cross_repo/gcp")
        os.makedirs(cross_repo_dir, exist_ok=True)

        # Build endpoint list (format J-Prime's gcp_cross_repo_bridge.py expects)
        endpoints = []
        vm_states = {}
        for vm_name, vm in self.managed_vms.items():
            vm_info = {
                "name": vm_name,
                "instance_id": vm.instance_id,
                "zone": vm.zone,
                "state": vm.state.value if vm.state else "unknown",
                "ip_address": vm.ip_address,
                "internal_ip": vm.internal_ip,
                "uptime_hours": round(vm.uptime_hours, 2),
                "cost_efficiency_score": round(vm.cost_efficiency_score, 1),
                "score_confidence": round(vm.score_confidence, 2),
                "component_usage_count": vm.component_usage_count,
                "idle_time_minutes": round(vm.idle_time_minutes, 1),
                "cpu_percent": round(vm.cpu_percent, 1),
                "memory_used_gb": round(vm.memory_used_gb, 1),
                "total_cost": round(vm.total_cost, 4),
                "health_status": vm.health_status,
                "components": vm.components,
                "last_updated": time.time(),
            }
            vm_states[vm_name] = vm_info

            # Only publish running VMs with IPs as endpoints
            if vm.state == VMState.RUNNING and vm.ip_address:
                endpoints.append({
                    "ip": vm.ip_address,
                    "internal_ip": vm.internal_ip,
                    "port": 8010,  # Default JARVIS backend port
                    "name": vm_name,
                    "zone": vm.zone,
                    "health": vm.health_status,
                    "source": "jarvis-body",
                })

        # Write discovered endpoints (J-Prime reads this)
        endpoints_path = os.path.join(cross_repo_dir, "discovered_endpoints.json")
        try:
            with open(endpoints_path, "w") as f:
                json.dump({
                    "endpoints": endpoints,
                    "updated_at": time.time(),
                    "source": "gcp_vm_manager",
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"Failed to write {endpoints_path}: {e}")

        # Write full VM state (for debugging / other consumers)
        vms_path = os.path.join(cross_repo_dir, "jarvis_vms.json")
        try:
            with open(vms_path, "w") as f:
                json.dump({
                    "vms": vm_states,
                    "updated_at": time.time(),
                    "manager_stats": self.stats,
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"Failed to write {vms_path}: {e}")

    async def _monitoring_loop(self):
        """
        Background monitoring loop for VM health, lifecycle, and intelligent cost-cutting.

        Features:
        - Health monitoring
        - Cost tracking and efficiency scoring
        - Idle VM detection and auto-termination
        - Memory pressure monitoring (terminate when local RAM normalizes)
        - Detailed metrics collection (CPU, memory, network, disk)

        v93.6: Enhanced with graceful shutdown handling - uses short sleep intervals
        to check shutdown state frequently, preventing "Task was destroyed" errors.
        """
        logger.info("🔍 VM monitoring loop started (intelligent cost-cutting enabled)")
        self.is_monitoring = True

        # v93.6: Import shutdown event for graceful termination
        try:
            from backend.core.async_safety import get_shutdown_event
            shutdown_event = get_shutdown_event()
        except ImportError:
            shutdown_event = None

        while self.is_monitoring:
            try:
                # v93.6: Use short sleep intervals (1s) to check shutdown state frequently
                # This prevents the "Task was destroyed but it is pending" error
                sleep_remaining = self.config.health_check_interval
                while sleep_remaining > 0 and self.is_monitoring:
                    # Check shutdown event if available
                    if shutdown_event and shutdown_event.is_set():
                        logger.info("🔍 VM monitoring loop: shutdown event detected")
                        self.is_monitoring = False
                        return

                    await asyncio.sleep(min(1.0, sleep_remaining))
                    sleep_remaining -= 1.0

                # Check if we should exit after sleep
                if not self.is_monitoring:
                    break

                for vm_name, vm in list(self.managed_vms.items()):
                    # === STEP 1: Always update cost (no GCP API needed) ===
                    vm.update_cost()

                    # === STEP 2: Collect GCP state + metrics (best-effort) ===
                    # Failures keep last-known values — never reset to 0
                    try:
                        instance = await asyncio.to_thread(
                            self.instances_client.get,
                            project=self.config.project_id,
                            zone=self.config.zone,
                            instance=vm_name,
                        )

                        status_map = {
                            "PROVISIONING": VMState.PROVISIONING,
                            "STAGING": VMState.STAGING,
                            "RUNNING": VMState.RUNNING,
                            "STOPPING": VMState.STOPPING,
                            "TERMINATED": VMState.TERMINATED,
                        }
                        vm.state = status_map.get(instance.status, VMState.UNKNOWN)

                        # Collect real metrics via GCP Monitoring API (if available)
                        metrics_collected = False
                        if self.config.metrics_collection_enabled and hasattr(self, '_metrics_collector') and self._metrics_collector:
                            try:
                                metrics = await self._metrics_collector.fetch_vm_metrics(
                                    vm_name, self.config.zone
                                )
                                if metrics:
                                    vm.cpu_percent = metrics.get("cpu_percent", vm.cpu_percent)
                                    vm.memory_used_gb = metrics.get("memory_used_gb", vm.memory_used_gb)
                                    metrics_collected = True
                            except Exception as mc_err:
                                logger.debug(f"Metrics collector error for {vm_name}: {mc_err}")

                        # Fallback estimation only when no real metrics AND VM has recent activity
                        if not metrics_collected and vm.cpu_percent == 0.0:
                            if vm.component_usage_count > 0 and vm.idle_time_minutes < 2.0:
                                # Active VM with recent usage — conservative estimate
                                vm.cpu_percent = max(vm.cpu_percent, 25.0)
                                vm.memory_used_gb = max(vm.memory_used_gb, 8.0)
                            # NOTE: idle VMs with no metrics keep 0.0 — that's honest data,
                            # and the confidence system handles it correctly

                        # Health status
                        is_healthy = vm.state == VMState.RUNNING
                        vm.last_health_check = time.time()
                        vm.health_status = "healthy" if is_healthy else "unhealthy"

                        # State-based logging (only for unexpected states)
                        if not is_healthy:
                            expected_shutdown_states = {VMState.STOPPING, VMState.TERMINATED}
                            expected_startup_states = {VMState.CREATING, VMState.PROVISIONING, VMState.STAGING}
                            if vm.state in expected_shutdown_states:
                                logger.debug(f"VM {vm_name} in expected shutdown state: {vm.state.value}")
                            elif vm.state in expected_startup_states:
                                logger.info(f"VM {vm_name} starting up: {vm.state.value}")
                            elif vm.state == VMState.FAILED:
                                logger.warning(f"⚠️  VM {vm_name} health check failed (state: {vm.state.value})")
                            else:
                                logger.warning(f"⚠️  VM {vm_name} in unexpected state: {vm.state.value}")

                    except Exception as metrics_error:
                        # GCP API failed — keep last-known state and metrics, don't reset to 0
                        logger.debug(f"Could not collect state/metrics for {vm_name}: {metrics_error}")

                    # === STEP 3: ALWAYS calculate efficiency score (outside try/except) ===
                    # This runs whether or not GCP API succeeded — uses whatever data is available
                    vm.update_efficiency_score(
                        ramp_minutes=self.config.efficiency_ramp_minutes,
                        startup_bonus_max=self.config.efficiency_startup_bonus,
                    )

                    # === STEP 4: INTELLIGENT COST-CUTTING CHECKS ===

                    # Skip ALL cost-cutting checks for invincible VMs.
                    # InvincibleGuard in terminate_vm() is the safety net, but we
                    # avoid even reaching it to prevent repeated WARNING spam.
                    vm_meta = vm.metadata or {}
                    _is_invincible = (
                        vm_name.startswith("jarvis-prime-node")
                        or vm_meta.get("vm_class") == "invincible"
                        or vm_meta.get("labels", {}).get("vm-class") == "invincible"
                    )
                    if _is_invincible:
                        logger.debug(
                            f"[InvincibleVM] Skipping cost-cutting checks for "
                            f"persistent VM '{vm_name}' (uptime: {vm.uptime_hours:.1f}h)"
                        )
                        continue

                    # 4a. Check VM lifetime (hard limit)
                    if vm.uptime_hours >= self.config.max_vm_lifetime_hours:
                        logger.info(
                            f"⏰ VM {vm_name} exceeded max lifetime ({self.config.max_vm_lifetime_hours}h)"
                        )
                        await self.terminate_vm(vm_name, reason="Max lifetime exceeded")
                        continue

                    # 4b. Skip cost check during startup grace period
                    grace_until = getattr(self, '_startup_grace_until', {}).get(vm_name, 0)
                    if time.time() < grace_until:
                        logger.debug(
                            f"☁️ VM {vm_name} in startup grace period "
                            f"({grace_until - time.time():.0f}s remaining), "
                            f"skipping cost check"
                        )
                        continue

                    # 4c. Check if VM is wasting money (idle + low efficiency + high confidence)
                    # Confidence gate prevents killing VMs with insufficient data
                    idle_limit = float(self.config.idle_timeout_minutes)
                    is_idle = vm.idle_time_minutes > idle_limit
                    is_wasting_money = (
                        is_idle
                        and vm.cost_efficiency_score < self.config.efficiency_termination_threshold
                        and vm.score_confidence > self.config.efficiency_min_confidence_for_termination
                    )

                    if is_wasting_money:
                        logger.warning(
                            f"💰 VM {vm_name} is wasting money: "
                            f"idle for {vm.idle_time_minutes:.1f}m, "
                            f"efficiency score: {vm.cost_efficiency_score:.1f}% "
                            f"(confidence: {vm.score_confidence:.2f})"
                        )
                        await self.terminate_vm(
                            vm_name,
                            reason=(
                                f"Cost waste: idle {vm.idle_time_minutes:.1f}m "
                                f"(limit {idle_limit:.0f}m), efficiency {vm.cost_efficiency_score:.1f}% "
                                f"(confidence: {vm.score_confidence:.2f})"
                            ),
                        )
                        continue

                    # 4d. Check if local memory pressure normalized (VM no longer needed)
                    try:
                        import psutil
                        local_mem_percent = psutil.virtual_memory().percent

                        if local_mem_percent < 70:
                            logger.info(
                                f"📉 Local RAM normalized ({local_mem_percent:.1f}%) - "
                                f"VM {vm_name} no longer needed"
                            )
                            await self.terminate_vm(
                                vm_name,
                                reason=f"Memory pressure resolved (local RAM: {local_mem_percent:.1f}%)"
                            )
                            continue
                    except Exception as mem_check_error:
                        logger.debug(f"Could not check local memory: {mem_check_error}")

                    # === STEP 5: Efficiency warnings (configurable thresholds) ===
                    if (vm.state == VMState.RUNNING
                            and vm.cost_efficiency_score < self.config.efficiency_warning_threshold):
                        vm_startup_minutes = vm.uptime_hours * 60

                        # Initialize rate limiting tracker if needed
                        if not hasattr(self, '_efficiency_warning_times'):
                            self._efficiency_warning_times: Dict[str, float] = {}

                        last_warning = self._efficiency_warning_times.get(vm_name, 0)

                        should_warn = (
                            vm_startup_minutes > self.config.efficiency_grace_minutes
                            and time.time() - last_warning > self.config.efficiency_warning_interval_seconds
                        )

                        if should_warn:
                            logger.warning(
                                f"⚠️  VM {vm_name} low efficiency: {vm.cost_efficiency_score:.1f}% "
                                f"(idle: {vm.idle_time_minutes:.1f}m, uptime: {vm.uptime_hours:.1f}h, "
                                f"confidence: {vm.score_confidence:.2f}, "
                                f"usage: {vm.component_usage_count}, "
                                f"cpu: {vm.cpu_percent:.1f}%)"
                            )
                            self._efficiency_warning_times[vm_name] = time.time()

                # === STEP 6: Publish VM state for cross-repo coordination ===
                # Called once per monitoring cycle (not per-VM) — writes to ~/.jarvis/cross_repo/gcp/
                try:
                    self._publish_vm_state()
                except Exception as pub_err:
                    logger.debug(f"Cross-repo state publishing failed (non-critical): {pub_err}")

            except asyncio.CancelledError:
                # v93.6: Graceful shutdown - don't log as error
                logger.info("🔍 VM monitoring loop cancelled (graceful shutdown)")
                self.is_monitoring = False
                return
            except Exception as e:
                if not self.is_monitoring:
                    # Shutdown in progress, don't log error
                    break
                log_component_failure(
                    "gcp-vm",
                    "Error in monitoring loop",
                    error=e,
                )

        # v93.6: Clean exit logging
        logger.info("🔍 VM monitoring loop stopped")

    async def _health_check_vm(self, vm_name: str) -> bool:
        """Perform health check on a VM"""
        if vm_name not in self.managed_vms:
            return False

        vm = self.managed_vms[vm_name]

        try:
            # Get instance status from GCP
            instance = await asyncio.to_thread(
                self.instances_client.get,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=vm_name,
            )

            # Update state
            status_map = {
                "PROVISIONING": VMState.PROVISIONING,
                "STAGING": VMState.STAGING,
                "RUNNING": VMState.RUNNING,
                "STOPPING": VMState.STOPPING,
                "TERMINATED": VMState.TERMINATED,
            }
            vm.state = status_map.get(instance.status, VMState.UNKNOWN)

            return vm.state == VMState.RUNNING

        except Exception as e:
            log_component_failure(
                "gcp-vm",
                f"Health check failed for {vm_name}",
                error=e,
                vm_name=vm_name,
            )
            return False

    async def cleanup_all_vms(self, reason: str = "System shutdown"):
        """Terminate all managed VMs with cost summary"""
        if not self.managed_vms:
            logger.info("ℹ️  No VMs to clean up")
            return

        logger.info(f"🧹 Cleaning up all VMs: {reason}")

        # Calculate total costs before terminating
        total_session_cost = 0.0
        total_uptime_hours = 0.0
        vm_count = len(self.managed_vms)

        for vm in self.managed_vms.values():
            vm.update_cost()
            total_session_cost += vm.total_cost
            total_uptime_hours += vm.uptime_hours

        # Terminate all VMs
        tasks = []
        for vm_name in list(self.managed_vms.keys()):
            tasks.append(self.terminate_vm(vm_name, reason=reason))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Display cost summary
        logger.info("✅ All VMs cleaned up")
        logger.info("=" * 60)
        logger.info("💰 GCP VM COST SUMMARY")
        logger.info("=" * 60)
        logger.info(f"   VMs Terminated:  {vm_count}")
        logger.info(f"   Total Uptime:    {total_uptime_hours:.2f} hours")
        logger.info(f"   Session Cost:    ${total_session_cost:.4f}")
        logger.info(f"   Total Lifetime:  ${self.stats['total_cost']:.4f}")
        logger.info("=" * 60)

    async def cleanup(self):
        """
        Cleanup and shutdown with graceful task termination.

        v93.6: Enhanced with proper timeout handling to prevent hanging during shutdown.
        """
        logger.info("🧹 GCP VM Manager cleanup starting...")

        # Signal monitoring loop to stop
        self.is_monitoring = False

        # v93.6: Set global shutdown event if available
        try:
            from backend.core.async_safety import set_shutdown_event
            set_shutdown_event()
        except ImportError:
            pass

        # v93.6: Cancel monitoring task with timeout
        if self.monitoring_task and not self.monitoring_task.done():
            self.monitoring_task.cancel()
            try:
                # Give task 5 seconds to shutdown gracefully
                await asyncio.wait_for(
                    asyncio.shield(self.monitoring_task),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning("⚠️ Monitoring task didn't stop in time, forcing...")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"Monitoring task cleanup error (non-critical): {e}")

        # v235.3: Cancel periodic orphan cleanup task
        if hasattr(self, '_orphan_cleanup_task') and self._orphan_cleanup_task and not self._orphan_cleanup_task.done():
            self._orphan_cleanup_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._orphan_cleanup_task),
                    timeout=5.0
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                pass

        # Cleanup VMs with timeout protection
        try:
            await asyncio.wait_for(
                asyncio.shield(self.cleanup_all_vms(reason="Manager shutdown")),
                timeout=30.0
            )
        except asyncio.TimeoutError:
            logger.warning("⚠️ VM cleanup timed out after 30s")
        except Exception as e:
            logger.warning(f"⚠️ VM cleanup error: {e}")

        self.initialized = False
        logger.info("🧹 GCP VM Manager cleaned up")

    def get_stats(self) -> Dict:
        """Get comprehensive manager statistics including circuit breaker status"""
        circuit_status = {}
        for name, circuit in self._circuit_breakers.items():
            circuit_status[name] = {
                "state": circuit.state.value,
                "failure_count": circuit.failure_count,
                "can_execute": circuit.can_execute()[0],
            }
        
        # Calculate VM costs summary
        total_vm_cost = sum(vm.total_cost for vm in self.managed_vms.values())
        
        return {
            **self.stats,
            "managed_vms": len(self.managed_vms),
            "creating_vms": len(self.creating_vms),
            "current_vm_cost": total_vm_cost,
            "circuit_breakers": circuit_status,
            "config": self.config.to_dict(),
            "initialized": self.initialized,
        }

    def get_circuit_breaker_status(self) -> Dict[str, Dict]:
        """Get detailed circuit breaker status"""
        return {
            name: {
                "state": circuit.state.value,
                "failure_count": circuit.failure_count,
                "failure_threshold": circuit.failure_threshold,
                "recovery_timeout": circuit.recovery_timeout,
                "last_failure_time": circuit.last_failure_time,
                "can_execute": circuit.can_execute()[0],
                "reason": circuit.can_execute()[1],
            }
            for name, circuit in self._circuit_breakers.items()
        }

    def reset_circuit_breaker(self, name: str) -> bool:
        """Manually reset a circuit breaker"""
        if name in self._circuit_breakers:
            circuit = self._circuit_breakers[name]
            circuit.state = CircuitState.CLOSED
            circuit.failure_count = 0
            circuit.half_open_calls = 0
            logger.info(f"🔌 Circuit breaker '{name}' manually reset")
            return True
        return False

    # =========================================================================
    # INVINCIBLE NODE: Static IP + STOP Termination Support
    # =========================================================================
    # These methods implement the "Invincible Node" strategy where:
    # - A persistent Spot VM with static IP survives preemption in STOPPED state
    # - Fast restart (~30s) instead of full recreation (~3-5 min)
    # - State machine: Ping -> Describe -> Start/Create -> Poll
    # =========================================================================

    async def ensure_static_vm_ready(
        self,
        port: Optional[int] = None,
        timeout: Optional[float] = None,
        progress_callback: Optional[Callable[[int, str, str], None]] = None,
    ) -> Tuple[bool, Optional[str], str]:
        """
        Ensure the static/persistent VM (Invincible Node) is ready for requests.

        v229.0: Golden Image-Aware Invincible Node State Machine
        ═══════════════════════════════════════════════════════════
        
        This implements intelligent VM lifecycle management that ensures VMs
        always use the latest golden image when available:
        
        1. PING: Try health endpoint at static IP
        2. If unreachable, DESCRIBE instance via GCP API (with full metadata)
        3. VALIDATE: Check VM lineage against golden image requirements
        4. Branch:
           - If NOT_FOUND -> CREATE new VM (from golden image if available)
           - If STOPPED/TERMINATED + golden mismatch -> DELETE + CREATE from golden
           - If STOPPED/TERMINATED + golden matches -> START (fast path ~30-60s)
           - If RUNNING but unhealthy -> POLL until ready
        5. POLL /health until ready

        Args:
            port: Port for health endpoint (default: JARVIS_PRIME_PORT or 8000)
            timeout: Max time to wait for VM to be ready (default: 300s)
            progress_callback: v220.1 - Optional callback(progress_pct, phase, detail) for real-time dashboard updates

        Returns:
            Tuple of (success: bool, ip_address: Optional[str], status_message: str)
        """
        # Get configuration
        static_ip_name = self.config.static_ip_name
        instance_name = self.config.static_instance_name or "jarvis-prime-node"
        target_port = port or int(os.environ.get("JARVIS_PRIME_PORT", "8000"))
        max_timeout = timeout or self.config.static_vm_health_timeout
        poll_interval = self.config.static_vm_health_poll_interval

        # Check if static IP mode is configured
        if not static_ip_name:
            return False, None, "STATIC_IP_NOT_CONFIGURED: Set GCP_VM_STATIC_IP_NAME"

        # Ensure manager is initialized
        if not self.initialized:
            try:
                await self.initialize()
            except Exception as e:
                return False, None, f"INIT_FAILED: {e}"

        # Validate configuration
        is_valid, validation_error = self.config.is_valid_for_vm_operations()
        if not is_valid:
            return False, None, f"CONFIG_INVALID: {validation_error}"

        # Use lock to prevent concurrent start/create operations
        async with self._vm_lock:
            # Step 1: Get static IP address (v210.0: auto-create if missing)
            static_ip = await self._get_static_ip_address(static_ip_name, auto_create=True)
            if not static_ip:
                return False, None, f"STATIC_IP_FAILED: Could not get or create '{static_ip_name}'"

            logger.info(f"☁️ [InvincibleNode] Ensuring VM ready at {static_ip}:{target_port}")

            # Step 2: Ping health endpoint
            is_healthy, health_status = await self._ping_health_endpoint(
                static_ip, target_port, timeout=10.0
            )

            # v235.4: _ping_health_endpoint already checks all readiness signals
            # (ready_for_inference, model_loaded+healthy, APARS, native phase/stage).
            # No need to re-check ready_for_inference here.
            if is_healthy:
                logger.info(f"✅ [InvincibleNode] VM already ready: {static_ip}")
                return True, static_ip, "ALREADY_READY"

            # Step 3: Describe instance with FULL metadata (v229.0)
            instance_status, vm_metadata, gcp_error = await self._describe_instance_full(instance_name)
            logger.info(f"☁️ [InvincibleNode] Instance status: {instance_status}")

            # Step 4: Branch based on state with golden image intelligence
            if instance_status == "NOT_FOUND":
                # Create new VM (uses golden image automatically via _create_static_vm)
                logger.info(f"☁️ [InvincibleNode] Instance not found - creating new VM")
                create_success, create_error = await self._create_static_vm(
                    instance_name, static_ip_name, target_port
                )
                if not create_success:
                    return False, static_ip, f"CREATE_FAILED: {create_error}"

            elif instance_status in ("TERMINATED", "STOPPED", "SUSPENDED"):
                # v229.0: CHECK GOLDEN IMAGE LINEAGE before deciding start vs recreate
                should_recreate, lineage_reason = await self._check_vm_golden_image_lineage(
                    instance_name, vm_metadata
                )
                
                if should_recreate:
                    # VM doesn't match golden image — delete and recreate
                    logger.info(
                        f"🔄 [InvincibleNode] Recreating VM from golden image "
                        f"(reason: {lineage_reason})"
                    )
                    if progress_callback:
                        # v235.0: Use low pct (0-5) so dashboard maps to 40-43%.
                        # The callback maps pct → 40+pct*0.6. Values >5 map above
                        # where APARS data starts (~43%), causing visible progress
                        # regression when real APARS data arrives and overrides.
                        progress_callback(0, "gcp", f"Recreating VM from golden image ({lineage_reason})")
                    
                    # Delete the old VM
                    del_success, del_error = await self._delete_instance(instance_name)
                    if not del_success:
                        # If delete fails, fall back to starting the old VM
                        logger.warning(
                            f"⚠️ [InvincibleNode] Delete failed ({del_error}), "
                            f"falling back to starting existing VM"
                        )
                        start_success, start_error = await self._start_instance(instance_name)
                        if not start_success:
                            return False, static_ip, f"START_FAILED: {start_error}"
                    else:
                        # Create new VM from golden image
                        create_success, create_error = await self._create_static_vm(
                            instance_name, static_ip_name, target_port
                        )
                        if not create_success:
                            return False, static_ip, f"RECREATE_FAILED: {create_error}"
                        
                        if progress_callback:
                            # v235.0: Low pct avoids regression when APARS data arrives
                            progress_callback(5, "gcp", "Golden image VM created, booting")
                else:
                    # VM matches golden image — fast restart path
                    logger.info(
                        f"☁️ [InvincibleNode] Starting VM: {instance_name} "
                        f"(lineage: {lineage_reason})"
                    )
                    start_success, start_error = await self._start_instance(instance_name)
                    if not start_success:
                        return False, static_ip, f"START_FAILED: {start_error}"

            elif instance_status in ("STAGING", "PROVISIONING"):
                # VM is starting up, proceed to poll
                logger.info(f"☁️ [InvincibleNode] VM is starting: {instance_status}")

            elif instance_status == "RUNNING":
                # VM is running but health check failed
                # v235.0: Check startup script version — a running VM with a stale
                # startup script will never reach ready_for_inference. Detect and
                # recycle early instead of polling until timeout.
                vm_script_version = (
                    vm_metadata.get("jarvis-startup-script-version", "")
                    if vm_metadata else ""
                )
                if vm_script_version != _STARTUP_SCRIPT_VERSION:
                    logger.info(
                        f"🔄 [InvincibleNode] Running VM has stale startup script "
                        f"(vm={vm_script_version or 'pre-v235'}, "
                        f"current={_STARTUP_SCRIPT_VERSION}). "
                        f"Recycling with updated script."
                    )
                    if progress_callback:
                        # v235.0: Low pct avoids regression when APARS data arrives
                        progress_callback(
                            0, "gcp",
                            f"Recycling VM: startup script "
                            f"({vm_script_version or 'pre-v235'} → {_STARTUP_SCRIPT_VERSION})"
                        )

                    # Delete the running VM (GCP API handles running → deleted)
                    del_success, del_error = await self._delete_instance(instance_name)
                    if del_success:
                        create_success, create_error = await self._create_static_vm(
                            instance_name, static_ip_name, target_port
                        )
                        if not create_success:
                            return False, static_ip, f"SCRIPT_UPGRADE_FAILED: {create_error}"
                        if progress_callback:
                            # v235.0: Low pct avoids regression when APARS data arrives
                            progress_callback(
                                5, "gcp",
                                f"VM recreated with v{_STARTUP_SCRIPT_VERSION} script, booting"
                            )
                    else:
                        logger.warning(
                            f"⚠️ [InvincibleNode] Delete failed ({del_error}), "
                            f"proceeding with stale VM (will likely timeout)"
                        )
                else:
                    logger.info(
                        f"☁️ [InvincibleNode] VM running "
                        f"(script v{vm_script_version}) but not healthy yet"
                    )

            elif instance_status == "ERROR":
                return False, static_ip, f"GCP_API_ERROR: {gcp_error}"

            else:
                logger.warning(f"☁️ [InvincibleNode] Unknown status: {instance_status}")

        # Step 5: Poll health endpoint until ready (outside lock to avoid blocking)
        logger.info(f"☁️ [InvincibleNode] Polling health endpoint (timeout: {max_timeout}s)...")

        # v235.0: Protect VM from cost tracker termination during startup polling.
        # Cost tracker runs in a separate async task and can kill "idle" VMs.
        # Grace period = polling timeout + 60s buffer.
        if not hasattr(self, '_startup_grace_until'):
            self._startup_grace_until: Dict[str, float] = {}
        self._startup_grace_until[instance_name] = time.time() + max_timeout + 60

        try:
            poll_success, final_status = await self._poll_health_until_ready(
                static_ip, target_port, max_timeout, poll_interval,
                progress_callback=progress_callback  # v220.1: Pass through for real-time updates
            )
        finally:
            # v235.0: Clear startup grace (both success and failure paths)
            self._startup_grace_until.pop(instance_name, None)

        if poll_success:
            logger.info(f"✅ [InvincibleNode] VM ready: {static_ip}")
            return True, static_ip, "READY"
        else:
            return False, static_ip, f"HEALTH_TIMEOUT: {final_status}"

    async def _get_static_ip_address(self, ip_name: str, auto_create: bool = True) -> Optional[str]:
        """
        Get the IP address for a reserved static IP by name.
        
        v210.0: Added auto_create option to automatically create the static IP
        if it doesn't exist. This enables fully automatic Invincible Node setup.
        
        Args:
            ip_name: Name of the static IP address resource
            auto_create: If True, create the static IP if it doesn't exist
            
        Returns:
            The IP address string, or None if not found and auto_create=False
        """
        try:
            # Use gcloud via subprocess for simplicity (addresses API is separate)
            import subprocess
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "gcloud", "compute", "addresses", "describe", ip_name,
                    "--project", self.config.project_id,
                    "--region", self.config.region,
                    "--format", "value(address)"
                ],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            
            # v210.0: Auto-create static IP if it doesn't exist
            if auto_create:
                logger.info(f"☁️ [InvincibleNode] Static IP '{ip_name}' not found - creating...")
                created_ip = await self._create_static_ip(ip_name)
                if created_ip:
                    logger.info(f"✅ [InvincibleNode] Created static IP: {created_ip}")
                    return created_ip
                else:
                    logger.warning(f"[InvincibleNode] Failed to create static IP '{ip_name}'")
            
            return None
        except Exception as e:
            logger.warning(f"[InvincibleNode] Failed to get static IP: {e}")
            return None
    
    async def _create_static_ip(self, ip_name: str) -> Optional[str]:
        """
        v210.0: Create a new static IP address in GCP.
        
        This enables fully automatic Invincible Node setup without manual
        gcloud commands.
        
        Args:
            ip_name: Name for the new static IP address resource
            
        Returns:
            The allocated IP address string, or None on failure
        """
        try:
            import subprocess
            
            # Create the static IP address
            logger.info(f"☁️ [InvincibleNode] Creating static IP '{ip_name}' in {self.config.region}...")
            
            create_result = await asyncio.to_thread(
                subprocess.run,
                [
                    "gcloud", "compute", "addresses", "create", ip_name,
                    "--project", self.config.project_id,
                    "--region", self.config.region,
                    "--description", "JARVIS Invincible Node static IP (auto-created)"
                ],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if create_result.returncode != 0:
                error_msg = create_result.stderr.strip() if create_result.stderr else "Unknown error"
                # Check if it already exists (race condition protection)
                if "already exists" in error_msg.lower():
                    logger.info(f"[InvincibleNode] Static IP already exists (concurrent creation)")
                else:
                    logger.warning(f"[InvincibleNode] Failed to create static IP: {error_msg}")
                    return None
            
            # Wait a moment for GCP to provision the IP
            await asyncio.sleep(2)
            
            # Retrieve the allocated IP address
            describe_result = await asyncio.to_thread(
                subprocess.run,
                [
                    "gcloud", "compute", "addresses", "describe", ip_name,
                    "--project", self.config.project_id,
                    "--region", self.config.region,
                    "--format", "value(address)"
                ],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if describe_result.returncode == 0 and describe_result.stdout.strip():
                return describe_result.stdout.strip()
            
            return None
            
        except Exception as e:
            logger.warning(f"[InvincibleNode] Error creating static IP: {e}")
            return None

    async def _ping_health_endpoint(
        self, ip: str, port: int, timeout: float = 10.0
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Ping the health endpoint to check if VM is ready.

        Returns:
            Tuple of (is_healthy: bool, health_response: dict)
        """
        import aiohttp
        url = f"http://{ip}:{port}/health"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        is_ready = data.get("ready_for_inference", False)
                        # v235.2: Accept model_loaded + healthy as readiness signal.
                        # Some J-Prime versions set model_loaded=true and status="healthy"
                        # but don't set ready_for_inference explicitly.
                        if not is_ready and data.get("model_loaded") and data.get("status") == "healthy":
                            is_ready = True
                        # v235.2: Also check APARS payload injected by enrichment middleware
                        if not is_ready:
                            apars = data.get("apars", {})
                            if isinstance(apars, dict) and apars.get("total_progress", 0) >= 100:
                                is_ready = True
                        # v235.4: Accept J-Prime NATIVE health format (without APARS).
                        # J-Prime's /health returns {status:"healthy", phase:"ready",
                        # model_load_progress_pct:100, model_loading_in_progress:false}
                        # when model is loaded and serving. This is equivalent to
                        # ready_for_inference=true but uses J-Prime's own schema.
                        if not is_ready and data.get("status") == "healthy":
                            if data.get("phase") == "ready" or data.get("stage") == "ready":
                                is_ready = True
                            elif (data.get("model_load_progress_pct", 0) >= 100
                                  and not data.get("model_loading_in_progress", True)):
                                is_ready = True
                        return is_ready, data
                    return False, {"status": resp.status}
        except asyncio.TimeoutError:
            return False, {"error": "timeout"}
        except Exception as e:
            return False, {"error": str(e)}

    async def _check_vm_ssh_reachable(self, ip: str) -> bool:
        """v235.0: Quick SSH port check for diagnostic purposes.

        Tests TCP connectivity to port 22. If SSH is reachable but the health
        endpoint is not, the startup script likely failed or the service port
        isn't open yet. If SSH is also unreachable, the VM may not have booted,
        or there's a network/firewall issue.
        """
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, 22), timeout=5.0
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def _describe_instance(self, instance_name: str) -> Tuple[str, Optional[str]]:
        """
        Get the current status of an instance from GCP.

        Returns:
            Tuple of (status: str, error: Optional[str])
            Status can be: RUNNING, STOPPED, TERMINATED, STAGING, PROVISIONING,
                          SUSPENDED, STOPPING, NOT_FOUND, ERROR
        """
        try:
            instance = await asyncio.to_thread(
                self.instances_client.get,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=instance_name,
            )
            return instance.status, None
        except Exception as e:
            error_str = str(e).lower()
            if "404" in error_str or "not found" in error_str or "notfound" in error_str:
                return "NOT_FOUND", None
            return "ERROR", str(e)

    async def _describe_instance_full(self, instance_name: str) -> Tuple[str, Optional[Dict[str, str]], Optional[str]]:
        """
        v229.0: Get the current status AND metadata of an instance from GCP.
        
        Returns the full instance metadata so callers can inspect deployment mode,
        golden image source, creation time, and other lineage information needed
        for intelligent VM reuse vs recreate decisions.

        Returns:
            Tuple of (status: str, metadata: Optional[Dict], error: Optional[str])
            - status: RUNNING, STOPPED, TERMINATED, STAGING, etc. or NOT_FOUND, ERROR
            - metadata: Dict of instance metadata key-value pairs (None if NOT_FOUND)
            - error: Error message if any
        """
        try:
            instance = await asyncio.to_thread(
                self.instances_client.get,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=instance_name,
            )
            # Extract metadata items into a flat dict for easy lookup
            metadata_dict: Dict[str, str] = {}
            if instance.metadata and instance.metadata.items:
                for item in instance.metadata.items:
                    metadata_dict[item.key] = item.value
            # Also include labels as metadata with 'label:' prefix
            if instance.labels:
                for key, value in instance.labels.items():
                    metadata_dict[f"label:{key}"] = value
            # Include the source disk image if available (from boot disk)
            if instance.disks:
                for disk in instance.disks:
                    if disk.boot and disk.source:
                        metadata_dict["_boot_disk_source"] = disk.source
            return instance.status, metadata_dict, None
        except Exception as e:
            error_str = str(e).lower()
            if "404" in error_str or "not found" in error_str or "notfound" in error_str:
                return "NOT_FOUND", None, None
            return "ERROR", None, str(e)

    async def _check_vm_golden_image_lineage(
        self, instance_name: str, vm_metadata: Optional[Dict[str, str]]
    ) -> Tuple[bool, str]:
        """
        v229.0: Check if an existing VM was created from the current golden image.
        
        This is the KEY intelligence that prevents restarting stale VMs that were
        built from standard OS images (or outdated golden images) when a fresh
        golden image is now available.
        
        Decision matrix:
        ┌─────────────────────────────┬──────────────────────┬──────────────────────┐
        │ Scenario                    │ VM Metadata          │ Action               │
        ├─────────────────────────────┼──────────────────────┼──────────────────────┤
        │ Golden enabled, VM is std   │ No deployment-mode   │ RECREATE from golden │
        │ Golden enabled, VM is old   │ deployment=golden,   │ RECREATE from golden │
        │   golden image              │   but source!=latest │                      │
        │ Golden enabled, VM matches  │ deployment=golden,   │ RESTART (fast path)  │
        │                             │   source==latest     │                      │
        │ Golden disabled             │ (any)                │ RESTART (no check)   │
        └─────────────────────────────┴──────────────────────┴──────────────────────┘
        
        Args:
            instance_name: Name of the existing VM
            vm_metadata: Metadata dict from _describe_instance_full()
            
        Returns:
            Tuple of (should_recreate: bool, reason: str)
        """
        # If golden images are not enabled, always allow restart
        if not self.config.use_golden_image:
            return False, "golden_image_disabled"
        
        # If we couldn't get metadata, be conservative and check golden image availability
        if vm_metadata is None:
            logger.warning(
                f"⚠️ [InvincibleNode] Cannot read VM metadata for {instance_name}. "
                "Checking golden image availability to decide."
            )
            # If golden images are available, recreate to be safe
            try:
                builder = self.get_golden_image_builder()
                latest = await builder.get_latest_golden_image()
                if latest and not latest.is_stale(self.config.golden_image_max_age_days):
                    return True, "metadata_unavailable_golden_exists"
            except Exception as e:
                logger.debug(f"[InvincibleNode] Golden image check failed: {e}")
            return False, "metadata_unavailable_no_golden"
        
        # Check if VM was created from a golden image
        vm_deployment_mode = vm_metadata.get("jarvis-deployment-mode", "")
        vm_golden_source = vm_metadata.get("jarvis-golden-image-source", "")
        
        # Get the latest golden image to compare
        latest_golden: Optional[GoldenImageInfo] = None
        try:
            builder = self.get_golden_image_builder()
            latest_golden = await builder.get_latest_golden_image()
        except Exception as e:
            logger.debug(f"[InvincibleNode] Golden image lookup error: {e}")
        
        # Case 1: No golden image available — just restart whatever we have
        if latest_golden is None:
            if self.config.golden_image_fallback:
                logger.info(
                    "☁️ [InvincibleNode] No golden image available. "
                    "Restarting existing VM (fallback mode)."
                )
                return False, "no_golden_image_available"
            else:
                return False, "no_golden_image_no_fallback"
        
        # Case 2: Golden image exists but is stale
        if latest_golden.is_stale(self.config.golden_image_max_age_days):
            logger.warning(
                f"⚠️ [InvincibleNode] Latest golden image is stale "
                f"({latest_golden.age_days:.1f} days old, max: {self.config.golden_image_max_age_days}). "
                f"Restarting existing VM."
            )
            return False, "golden_image_stale"
        
        # Case 3: VM was NOT created from a golden image at all
        if vm_deployment_mode != "golden-image":
            logger.info(
                f"🔄 [InvincibleNode] VM '{instance_name}' was created from standard image "
                f"(deployment_mode='{vm_deployment_mode or 'none'}'). "
                f"A fresh golden image '{latest_golden.name}' is available. "
                f"RECREATING for ~30-60s startup instead of ~10-15min."
            )
            return True, "vm_not_from_golden_image"
        
        # Case 4: VM was from golden image — check if it matches the latest
        golden_project = self.config.golden_image_project or self.config.project_id
        expected_source = f"projects/{golden_project}/global/images/{latest_golden.name}"
        
        if vm_golden_source and vm_golden_source != expected_source:
            # VM is from an older golden image
            logger.info(
                f"🔄 [InvincibleNode] VM '{instance_name}' golden image is outdated.\n"
                f"   Current: {vm_golden_source}\n"
                f"   Latest:  {expected_source}\n"
                f"   RECREATING to use latest golden image."
            )
            return True, "golden_image_outdated"
        
        # Case 5: VM matches current golden image — fast restart!
        logger.info(
            f"✅ [InvincibleNode] VM '{instance_name}' matches current golden image. "
            f"Fast restart path (~30-60s)."
        )
        return False, "golden_image_matches"

    async def _delete_instance(self, instance_name: str) -> Tuple[bool, Optional[str]]:
        """
        v229.0: Delete an instance to allow recreation from golden image.
        
        Returns:
            Tuple of (success: bool, error: Optional[str])
        """
        try:
            logger.info(f"🗑️ [InvincibleNode] Deleting VM for golden image recreation: {instance_name}")
            
            operation = await asyncio.to_thread(
                self.instances_client.delete,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=instance_name,
            )
            
            # Wait for deletion to complete
            await self._wait_for_operation(operation, timeout=120)
            
            logger.info(f"✅ [InvincibleNode] VM deleted: {instance_name}")
            return True, None
            
        except Exception as e:
            logger.error(f"❌ [InvincibleNode] Failed to delete VM: {e}")
            return False, str(e)

    def _generate_golden_startup_script(self) -> str:
        """
        v229.0: Generate the golden image startup script.
        
        This is a SINGLE SOURCE OF TRUTH for the golden image startup script,
        used by both _build_instance_config() (ephemeral VMs) and
        _create_static_vm() (Invincible Node VMs).
        
        The script:
        1. Starts APARS health endpoint immediately (<2s)
        2. Reads port from GCP metadata
        3. Sources pre-baked environment
        4. Validates pre-baked code + model integrity
        5. Starts the service (systemd or direct)
        6. Verifies service health with retries
        7. Signals APARS ready
        
        Returns:
            str: Complete bash startup script for golden image VMs
        """
        script = '''#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# v229.0: Golden Image Startup - Enterprise-Grade Fast Boot
# ═══════════════════════════════════════════════════════════════════════════════
# Runs on VMs created from golden images. Everything is pre-installed:
#   - Python + venv + ML dependencies (Phase 3 SKIPPED)
#   - JARVIS-Prime code cloned
#   - LLM model pre-cached
#   - systemd service configured
#
# This script:
#   1. Starts APARS health endpoint immediately (<2s)
#   2. Reads port from GCP metadata
#   3. Sources pre-baked environment
#   4. Validates pre-baked code + model integrity
#   5. Starts the service (systemd or direct)
#   6. Verifies service health with retries
#   7. Signals APARS ready
# ═══════════════════════════════════════════════════════════════════════════════

LOG_FILE="/var/log/jarvis-golden-startup.log"
PROGRESS_FILE="/tmp/jarvis_progress.json"
JARVIS_DIR="/opt/jarvis-prime"
START_TIME=$(date +%s)
STARTUP_SCRIPT_VERSION="__STARTUP_SCRIPT_VERSION__"
STARTUP_SCRIPT_METADATA_VERSION=""

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

update_apars() {
    local phase=$1
    local phase_progress=$2
    local total_progress=$3
    local checkpoint=$4
    local model_loaded=${5:-false}
    local ready=${6:-false}
    local error=${7:-null}
    local now=$(date +%s)
    local elapsed=$((now - START_TIME))

    cat > "$PROGRESS_FILE" << EOFPROGRESS
{
    "phase": ${phase},
    "phase_number": ${phase},
    "phase_progress": ${phase_progress},
    "total_progress": ${total_progress},
    "checkpoint": "${checkpoint}",
    "phase_name": "${checkpoint}",
    "model_loaded": ${model_loaded},
    "ready_for_inference": ${ready},
    "error": ${error},
    "updated_at": ${now},
    "elapsed_seconds": ${elapsed},
    "deployment_mode": "golden_image",
    "deps_prebaked": true,
    "skipped_phases": [2, 3],
    "version": "${STARTUP_SCRIPT_VERSION}",
    "startup_script_version": "${STARTUP_SCRIPT_VERSION}",
    "startup_script_metadata_version": "${STARTUP_SCRIPT_METADATA_VERSION}"
}
EOFPROGRESS
}

# SAFE_ENV_LOAD: Load .env in a sandboxed, time-bounded subshell
safe_load_env() {
    local env_file="$1"
    local timeout_secs="$2"
    local env_dump="/tmp/jarvis_env_dump.$$"

    if [ ! -f "$env_file" ]; then
        return 2
    fi

    if timeout "$timeout_secs" bash -c "set -a; source \"$env_file\" 2>/dev/null; env -0" > "$env_dump" 2>/dev/null; then
        while IFS= read -r -d '' entry; do
            case "$entry" in
                ""|"PWD="*|"OLDPWD="*|"SHLVL="*|"_="*)
                    continue
                    ;;
            esac
            export "$entry"
        done < "$env_dump"
        rm -f "$env_dump"
        return 0
    fi

    rm -f "$env_dump"
    return 1
}

# ─── Phase 0: Read port from GCP metadata FIRST (before health endpoint) ───
# v233.2: CRITICAL FIX — Previously the APARS health endpoint started before
# the port was read from metadata, causing it to listen on the wrong port.
# The supervisor would then poll the metadata port and get no response.
# v233.2: Try with 5s timeout first, retry with 15s on failure, then default
JARVIS_PORT=$(timeout 5 curl -s -H 'Metadata-Flavor: Google' \\
    http://metadata.google.internal/computeMetadata/v1/instance/attributes/jarvis-port \\
    2>/dev/null)
if [ -z "$JARVIS_PORT" ] || ! echo "$JARVIS_PORT" | grep -qE '^[0-9]+$'; then
    log "WARN: First metadata port read failed — retrying with 15s timeout..."
    JARVIS_PORT=$(timeout 15 curl -s -H 'Metadata-Flavor: Google' \\
        http://metadata.google.internal/computeMetadata/v1/instance/attributes/jarvis-port \\
        2>/dev/null)
fi
if [ -z "$JARVIS_PORT" ] || ! echo "$JARVIS_PORT" | grep -qE '^[0-9]+$'; then
    log "WARN: Metadata port read failed — defaulting to 8000"
    JARVIS_PORT="8000"
fi
# Validate port is in sane range
if [ "$JARVIS_PORT" -lt 1 ] 2>/dev/null || [ "$JARVIS_PORT" -gt 65535 ] 2>/dev/null; then
    log "WARN: Invalid port $JARVIS_PORT — defaulting to 8000"
    JARVIS_PORT="8000"
fi
export JARVIS_PORT
log "Port resolved from metadata: $JARVIS_PORT"

# Record startup script metadata version (if present)
STARTUP_SCRIPT_METADATA_VERSION=$(timeout 5 curl -s -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/instance/attributes/jarvis-startup-script-version \
    2>/dev/null)
if [ -z "$STARTUP_SCRIPT_METADATA_VERSION" ]; then
    STARTUP_SCRIPT_METADATA_VERSION="$STARTUP_SCRIPT_VERSION"
fi

export JARVIS_APARS_FILE="$PROGRESS_FILE"

update_apars 0 50 2 "golden_image_booting"

python3 << 'EOFHEALTH' &
import http.server, json, os, time

PROGRESS_FILE = "/tmp/jarvis_progress.json"
start_time = time.time()

# v235.0: Phase-aware ETA — replaces hardcoded 60s countdown
# Golden image phases: 0(5s), 1(20s), 4(30s), 5(120s), 6(30s)
_PHASE_ETA = {0: 5, 1: 20, 4: 30, 5: 120, 6: 30}
_TOTAL_ETA = sum(_PHASE_ETA.values())

def _calc_eta(state, elapsed):
    phase = state.get("phase", 0)
    pp = state.get("phase_progress", 0)
    remaining = 0
    found = False
    for p in sorted(_PHASE_ETA):
        if p == phase:
            found = True
            remaining += _PHASE_ETA[p] * (1.0 - pp / 100.0)
        elif found:
            remaining += _PHASE_ETA[p]
    if not found:
        remaining = max(0, _TOTAL_ETA - elapsed)
    return max(10, int(remaining))

class APARSHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/health", "/health/"):
            try:
                with open(PROGRESS_FILE, "r") as f:
                    state = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                state = {"phase": 0, "total_progress": 5, "checkpoint": "golden_booting",
                         "model_loaded": False, "ready_for_inference": False}

            elapsed = int(time.time() - start_time)
            ready = state.get("ready_for_inference", False)
            response = {
                "status": "healthy" if ready else "starting",
                "phase": "ready" if ready else "starting",
                "mode": "inference" if ready else "golden-startup",
                "model_loaded": state.get("model_loaded", False),
                "ready_for_inference": ready,
                "apars": {
                    "version": state.get("startup_script_version", "unknown"),
                    "phase_number": state.get("phase", 0),
                    "phase_name": state.get("checkpoint", "starting"),
                    "phase_progress": state.get("phase_progress", 0),
                    "total_progress": state.get("total_progress", 5),
                    "checkpoint": state.get("checkpoint", "starting"),
                    "eta_seconds": _calc_eta(state, elapsed) if not ready else 0,
                    "elapsed_seconds": elapsed,
                    "error": state.get("error"),
                    "startup_script_version": state.get("startup_script_version", "unknown"),
                    "startup_script_metadata_version": state.get("startup_script_metadata_version", "unknown"),
                    "deployment_mode": "golden_image",
                    "deps_prebaked": True,
                    "skipped_phases": [2, 3]
                },
                "uptime_seconds": elapsed,
                "version": "v235.0-golden"
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        elif self.path == "/health/ready":
            try:
                with open(PROGRESS_FILE) as f:
                    ready = json.load(f).get("ready_for_inference", False)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                ready = False
            code = 200 if ready else 503
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ready": ready}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

port = int(os.environ.get("JARVIS_PORT", "8000"))
server = http.server.HTTPServer(("0.0.0.0", port), APARSHandler)
server.serve_forever()
EOFHEALTH

HEALTH_PID=$!
sleep 1

# v233.2: Verify health endpoint started successfully
if ! kill -0 $HEALTH_PID 2>/dev/null; then
    log "ERROR: Health endpoint failed to start on port $JARVIS_PORT — retrying..."
    python3 << 'EOFHEALTH2' &
import http.server, json, os, time
PROGRESS_FILE = "/tmp/jarvis_progress.json"
start_time = time.time()
# v235.0: Phase-aware ETA (same as primary handler)
_PHASE_ETA = {0: 5, 1: 20, 4: 30, 5: 120, 6: 30}
_TOTAL_ETA = sum(_PHASE_ETA.values())
def _calc_eta(state, elapsed):
    phase = state.get("phase", 0)
    pp = state.get("phase_progress", 0)
    remaining = 0
    found = False
    for p in sorted(_PHASE_ETA):
        if p == phase:
            found = True
            remaining += _PHASE_ETA[p] * (1.0 - pp / 100.0)
        elif found:
            remaining += _PHASE_ETA[p]
    if not found:
        remaining = max(0, _TOTAL_ETA - elapsed)
    return max(10, int(remaining))
class APARSHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/health", "/health/"):
            try:
                with open(PROGRESS_FILE, "r") as f:
                    state = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                state = {"phase": 0, "total_progress": 5, "checkpoint": "golden_booting"}
            elapsed = int(time.time() - start_time)
            ready = state.get("ready_for_inference", False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "starting",
                "apars": {
                    "total_progress": state.get("total_progress", 5),
                    "phase_number": state.get("phase", 0),
                    "phase_name": state.get("checkpoint", "starting"),
                    "phase_progress": state.get("phase_progress", 0),
                    "eta_seconds": _calc_eta(state, elapsed),
                    "elapsed_seconds": elapsed,
                    "deployment_mode": "golden_image",
                    "startup_script_version": state.get("startup_script_version", "unknown"),
                    "startup_script_metadata_version": state.get("startup_script_metadata_version", "unknown"),
                },
                "ready_for_inference": ready
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, format, *args):
        pass
port = int(os.environ.get("JARVIS_PORT", "8000"))
server = http.server.HTTPServer(("0.0.0.0", port), APARSHandler)
server.serve_forever()
EOFHEALTH2
    HEALTH_PID=$!
    sleep 1
fi

log "═══════════════════════════════════════════════════════════════════════════════"
log "GOLDEN IMAGE STARTUP v${STARTUP_SCRIPT_VERSION} - Pre-baked environment"
log "═══════════════════════════════════════════════════════════════════════════════"
log "APARS health endpoint on port $JARVIS_PORT (PID: $HEALTH_PID)"

# ─── Source pre-baked environment ───
update_apars 1 0 10 "loading_environment"

if [ -f "$JARVIS_DIR/.env" ]; then
    if safe_load_env "$JARVIS_DIR/.env" 15; then
        log "✅ Environment loaded from $JARVIS_DIR/.env"
    else
        log "⚠️  .env sourcing failed or timed out after 15s, continuing with defaults"
    fi
    update_apars 1 50 15 "environment_loaded"
else
    log "⚠️  No .env file found at $JARVIS_DIR/.env"
    update_apars 1 50 15 "no_env_file"
fi

# Ensure PYTHONPATH includes JARVIS_DIR
export PYTHONPATH="${JARVIS_DIR}:${PYTHONPATH:-}"

# v235.0: Smooth progress through skipped phases (stay in Phase 1
# to avoid contradicting skipped_phases: [2, 3] in health response)
update_apars 1 80 18 "env_ready_deps_prebaked"
update_apars 1 100 35 "golden_ready_for_validation"

# ─── Validate pre-baked integrity ───
update_apars 4 0 40 "validating_prebaked"

VALIDATION_OK=true

# Check code exists and is compatible with current startup script
if [ -d "$JARVIS_DIR/jarvis_prime" ]; then
    log "✅ jarvis_prime module found"
    # v235.3: Verify code freshness — ASGI middleware requires module-level app export.
    # Golden images may have old J-Prime code without this. If missing, pull latest.
    if cd "$JARVIS_DIR" && "$JARVIS_DIR/venv/bin/python" -c "from jarvis_prime.server import app" 2>/dev/null; then
        log "✅ jarvis_prime.server.app importable (v235.3+ code)"
        update_apars 4 25 45 "code_validated"
    else
        log "⚠️  jarvis_prime.server.app not importable — code is stale (pre-v235.3)"
        update_apars 4 15 43 "code_stale_pulling"
        REPO_URL=$(timeout 5 curl -s -H 'Metadata-Flavor: Google' \\
            http://metadata.google.internal/computeMetadata/v1/instance/attributes/jarvis-repo-url \\
            2>/dev/null || echo "${JARVIS_PRIME_REPO_URL:-}")
        if [ -n "$REPO_URL" ] && [ -d "$JARVIS_DIR/.git" ]; then
            log "   Pulling latest from: $REPO_URL (timeout 60s)"
            if timeout 60 git -C "$JARVIS_DIR" pull --ff-only 2>&1 | tee -a "$LOG_FILE"; then
                log "✅ Code updated via git pull"
                update_apars 4 25 45 "code_updated"
            else
                log "⚠️  git pull failed — trying fresh clone"
                if timeout 120 git clone --depth 1 "$REPO_URL" /tmp/jprime-update 2>&1 | tee -a "$LOG_FILE"; then
                    cp -a /tmp/jprime-update/jarvis_prime/* "$JARVIS_DIR/jarvis_prime/" 2>/dev/null
                    cp -a /tmp/jprime-update/run_server.py "$JARVIS_DIR/" 2>/dev/null
                    rm -rf /tmp/jprime-update
                    log "✅ Code rescued via fresh clone"
                    update_apars 4 25 45 "code_rescued"
                else
                    log "❌ Code update failed — proceeding with stale code (reverse proxy fallback)"
                    rm -rf /tmp/jprime-update 2>/dev/null
                    update_apars 4 25 44 "code_stale_proceeding"
                fi
            fi
        elif [ -n "$REPO_URL" ]; then
            log "   No .git dir — cloning fresh (timeout 120s)"
            if timeout 120 git clone --depth 1 "$REPO_URL" /tmp/jprime-update 2>&1 | tee -a "$LOG_FILE"; then
                cp -a /tmp/jprime-update/jarvis_prime/* "$JARVIS_DIR/jarvis_prime/" 2>/dev/null
                cp -a /tmp/jprime-update/run_server.py "$JARVIS_DIR/" 2>/dev/null
                rm -rf /tmp/jprime-update
                log "✅ Code updated via clone"
                update_apars 4 25 45 "code_cloned"
            else
                log "❌ Clone failed — proceeding with stale code"
                rm -rf /tmp/jprime-update 2>/dev/null
                update_apars 4 25 44 "code_stale_proceeding"
            fi
        else
            log "⚠️  No repo URL — proceeding with stale code (reverse proxy fallback)"
            update_apars 4 25 44 "code_stale_no_repo"
        fi
    fi
else
    log "⚠️  jarvis_prime module NOT found — pulling latest code"
    update_apars 4 15 43 "code_rescue_starting"
    REPO_URL=$(timeout 5 curl -s -H 'Metadata-Flavor: Google' \\
        http://metadata.google.internal/computeMetadata/v1/instance/attributes/jarvis-repo-url \\
        2>/dev/null || echo "${JARVIS_PRIME_REPO_URL:-}")
    if [ -n "$REPO_URL" ]; then
        log "   Cloning from: $REPO_URL (timeout 120s)"
        # v233.3: Add timeout to prevent indefinite hang on DNS/network issues
        if timeout 120 git clone --depth 1 "$REPO_URL" /tmp/jprime-rescue 2>&1 | tee -a "$LOG_FILE"; then
            cp -a /tmp/jprime-rescue/* "$JARVIS_DIR/" 2>/dev/null && \\
                rm -rf /tmp/jprime-rescue && \\
                log "✅ Code rescued via git clone"
            update_apars 4 40 50 "code_rescue_complete"
        else
            log "❌ Git clone failed or timed out after 120s"
            update_apars 4 40 48 "code_rescue_failed"
            rm -rf /tmp/jprime-rescue 2>/dev/null
        fi
    else
        VALIDATION_OK=false
        log "❌ No repo URL available — cannot rescue code"
    fi
fi

update_apars 4 60 55 "checking_model_cache"

# Check model cache
MODEL_CACHE="${JARVIS_MODEL_CACHE:-$JARVIS_DIR/models}"
if [ -d "$MODEL_CACHE" ] && [ "$(ls -A "$MODEL_CACHE" 2>/dev/null)" ]; then
    # v233.3: Add timeout to prevent hang on network mounts
    MODEL_SIZE=$(timeout 10 du -sm "$MODEL_CACHE" 2>/dev/null | cut -f1 || echo "0")
    log "✅ Model cache: ${MODEL_SIZE}MB"
    update_apars 4 80 65 "model_size_calculated"
    update_apars 5 50 70 "model_cache_verified" true false
else
    log "⚠️  Model cache empty — models will download on first use"
    update_apars 5 50 70 "model_cache_empty" false false
fi

# ─── Start the service ───
update_apars 5 80 85 "starting_service" true false

# v235.2: Generate APARS enrichment launcher
# After the APARS stub is killed, J-Prime takes over the port.
# J-Prime's /health has NO native APARS support — the progress file
# becomes invisible to the supervisor. This ASGI middleware wraps
# J-Prime's app and injects APARS data from the progress file into
# /health responses, maintaining end-to-end progress visibility.
APARS_LAUNCHER="/tmp/jarvis_apars_launcher.py"
cat > "$APARS_LAUNCHER" << 'EOFLAUNCHER'
#!/usr/bin/env python3
"""APARS Enrichment Launcher — auto-generated by startup script.

Wraps J-Prime's ASGI app with middleware that injects APARS progress
data from the progress file into /health endpoint responses.

This solves the APARS blackout that occurs when the APARS stub is
killed and J-Prime takes over — J-Prime has no native awareness of
the APARS progress file, so without this middleware, the supervisor
loses all progress visibility during the critical model-loading phase.
"""
import os, sys, json, time, argparse

_JARVIS_DIR = os.environ.get("JARVIS_DIR", "/opt/jarvis-prime")
sys.path.insert(0, _JARVIS_DIR)
os.chdir(_JARVIS_DIR)

APARS_FILE = os.environ.get("JARVIS_APARS_FILE", "/tmp/jarvis_progress.json")

# ─── APARS file reader with stat-based cache ───
_apars_cache = None
_apars_cache_mtime_ns = 0

def _read_apars():
    global _apars_cache, _apars_cache_mtime_ns
    try:
        st = os.stat(APARS_FILE)
        mtime_ns = st.st_mtime_ns
        if _apars_cache is not None and mtime_ns == _apars_cache_mtime_ns:
            return _apars_cache
        with open(APARS_FILE, "r") as f:
            data = json.load(f)
        _apars_cache = data
        _apars_cache_mtime_ns = mtime_ns
        return data
    except Exception:
        return None

def _build_apars_payload(state):
    """Map progress file fields to supervisor-expected APARS format."""
    if not state:
        return None
    now = time.time()
    updated_at = state.get("updated_at", now)
    elapsed = int(now - updated_at) + state.get("elapsed_seconds", 0)
    return {
        "phase_number": state.get("phase_number", state.get("phase", 0)),
        "phase_name": state.get("phase_name", state.get("checkpoint", "starting")),
        "phase_progress": state.get("phase_progress", 0),
        "total_progress": state.get("total_progress", 5),
        "checkpoint": state.get("checkpoint", "starting"),
        "eta_seconds": max(0, int(state.get("eta_seconds", 10))),
        "elapsed_seconds": elapsed,
        "error": state.get("error"),
        "startup_script_version": state.get("startup_script_version", "unknown"),
        "startup_script_metadata_version": state.get("startup_script_metadata_version", "unknown"),
        "deployment_mode": state.get("deployment_mode", "golden_image"),
        "deps_prebaked": state.get("deps_prebaked", True),
        "skipped_phases": state.get("skipped_phases", [2, 3]),
    }

# ─── ASGI middleware: enrich /health responses with APARS data ───
class APARSEnrichmentMiddleware:
    """ASGI middleware that intercepts /health responses and injects APARS data.

    Only activates for GET /health requests. All other requests pass through
    unmodified with zero overhead (no response buffering).
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/health":
            await self.app(scope, receive, send)
            return

        # Buffer the /health response so we can enrich it
        captured_status = 200
        captured_headers = []
        body_parts = []

        async def capture_send(message):
            nonlocal captured_status, captured_headers
            if message["type"] == "http.response.start":
                captured_status = message.get("status", 200)
                captured_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))

        await self.app(scope, receive, capture_send)

        body = b"".join(body_parts)
        try:
            data = json.loads(body)
            if "apars" not in data:
                state = _read_apars()
                payload = _build_apars_payload(state)
                if payload:
                    data["apars"] = payload
                    # Propagate readiness flags from progress file
                    if state:
                        data.setdefault("model_loaded", state.get("model_loaded", False))
                        if state.get("ready_for_inference", False):
                            data["ready_for_inference"] = True
                    body = json.dumps(data, separators=(",", ":")).encode()
                    # Rebuild headers with updated content-length
                    captured_headers = [
                        (k, v) for k, v in captured_headers
                        if k not in (b"content-length", "content-length")
                        and (not isinstance(k, bytes) or k.lower() != b"content-length")
                    ]
                    captured_headers.append(
                        (b"content-length", str(len(body)).encode())
                    )
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        await send({
            "type": "http.response.start",
            "status": captured_status,
            "headers": captured_headers,
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })

# ─── Import J-Prime app and wrap it ───
_app = None
_import_errors = []

for _mod_path in ("jarvis_prime.server", "jarvis_prime.app", "server", "app"):
    try:
        _mod = __import__(_mod_path, fromlist=["app"])
        _candidate = getattr(_mod, "app", None) or getattr(_mod, "application", None)
        if _candidate is not None:
            _app = _candidate
            break
    except ImportError as e:
        _import_errors.append(f"{_mod_path}: {e}")
    except Exception as e:
        _import_errors.append(f"{_mod_path}: {e}")

if _app is None:
    # Fatal: can't find J-Prime app. Fall back to running module directly.
    import subprocess
    print(f"[APARS Launcher] Could not import J-Prime app: {_import_errors}", flush=True)
    print("[APARS Launcher] Falling back to direct module execution", flush=True)
    _port = os.environ.get("JARVIS_PORT", "8000")
    sys.exit(subprocess.call([
        sys.executable, "-m", "jarvis_prime.server",
        "--host", "0.0.0.0", "--port", _port,
    ]))

wrapped_app = APARSEnrichmentMiddleware(_app)

if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser(description="APARS-enriched J-Prime launcher")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JARVIS_PORT", "8000")),
    )
    args = parser.parse_args()
    print(
        f"[APARS Launcher] Starting J-Prime with APARS enrichment "
        f"on {args.host}:{args.port} (file: {APARS_FILE})",
        flush=True,
    )
    uvicorn.run(wrapped_app, host=args.host, port=args.port)
EOFLAUNCHER
chmod +x "$APARS_LAUNCHER"
log "Generated APARS enrichment launcher: $APARS_LAUNCHER"

# v235.0: Write transitioning state BEFORE killing stub
# Supervisor polls every 5s — if it polls during the 1-2s gap between
# stub kill and real service start, last known state shows "transitioning"
# (not a crash). Without this, Connection refused looks like a failure.
update_apars 5 90 87 "transitioning_to_service" true false

# Kill APARS health stub (the real server takes over the port)
if [ -n "$HEALTH_PID" ]; then
    kill "$HEALTH_PID" 2>/dev/null || true
    sleep 1
fi

log "Starting JARVIS-Prime service on port $JARVIS_PORT..."

if systemctl is-enabled jarvis-prime.service 2>/dev/null; then
    # v235.2: Override ExecStart to use APARS launcher + pass all env vars
    # systemd doesn't inherit shell exports, so JARVIS_APARS_FILE must
    # be explicitly listed. ExecStart= (empty) clears the base definition
    # before setting the new command (systemd convention).
    mkdir -p /etc/systemd/system/jarvis-prime.service.d
    cat > /etc/systemd/system/jarvis-prime.service.d/port.conf << EOFOVERRIDE
[Service]
Environment=JARVIS_PORT=${JARVIS_PORT}
Environment=PYTHONPATH=${JARVIS_DIR}
Environment=JARVIS_APARS_FILE=${JARVIS_APARS_FILE}
Environment=JARVIS_DIR=${JARVIS_DIR}
ExecStart=
ExecStart=${JARVIS_DIR}/venv/bin/python ${APARS_LAUNCHER} --host 0.0.0.0 --port ${JARVIS_PORT}
EOFOVERRIDE
    systemctl daemon-reload
    systemctl start jarvis-prime.service
    log "Started via systemd with APARS enrichment (port $JARVIS_PORT)"
else
    # Fallback: start directly with APARS launcher
    cd "$JARVIS_DIR"
    source venv/bin/activate
    set -a
    # v235.0: Timeout protection — env already loaded in Phase 1, this is defensive
    [ -f .env ] && safe_load_env ".env" 10
    set +a
    export JARVIS_PORT
    export JARVIS_APARS_FILE
    export JARVIS_DIR
    export PYTHONPATH="${JARVIS_DIR}:${PYTHONPATH:-}"
    nohup python "$APARS_LAUNCHER" \\
        --host 0.0.0.0 --port "$JARVIS_PORT" \\
        > /var/log/jarvis-prime.log 2>&1 &
    log "Started directly with APARS enrichment (port $JARVIS_PORT, PID: $!)"
fi

# ─── Verify service health with retries ───
# v235.2: Verify response CONTENT, not just HTTP 200. J-Prime returns 200
# with status="starting" immediately — but model may not be loaded yet.
# Check ready_for_inference OR model_loaded in the JSON response.
update_apars 6 0 90 "verifying_service" true false

READY=false
for attempt in $(seq 1 15); do
    sleep 2
    HEALTH_RESPONSE=$(curl -sf "http://localhost:${JARVIS_PORT}/health" 2>/dev/null || echo "")
    if [ -n "$HEALTH_RESPONSE" ]; then
        # Check for readiness indicators in the JSON response
        if echo "$HEALTH_RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    # v235.3: Only accept definitive readiness signals
    # model_loaded alone is NOT sufficient — J-Prime sets it during loading_tensors
    # before inference is actually possible
    if d.get('ready_for_inference') or d.get('status') == 'healthy':
        sys.exit(0)
    # Also check APARS payload (enrichment middleware may have injected it)
    apars = d.get('apars', {})
    if apars.get('total_progress', 0) >= 95:
        sys.exit(0)
    sys.exit(1)
except: sys.exit(1)
" 2>/dev/null; then
            READY=true
            log "✅ Service healthy after $((attempt * 2)) seconds"
            break
        else
            log "  Attempt $attempt: HTTP 200 but not ready yet (response: ${HEALTH_RESPONSE:0:120})"
        fi
    fi
    update_apars 6 $((attempt * 6)) $((90 + attempt / 2)) "verifying_attempt_${attempt}" true false
done

if [ "$READY" = true ]; then
    update_apars 6 100 100 "inference_ready" true true
    log "═══════════════════════════════════════════════════════════════════════════════"
    ELAPSED=$(($(date +%s) - START_TIME))
    log "✅ GOLDEN IMAGE STARTUP COMPLETE in ${ELAPSED}s"
    log "   Port: $JARVIS_PORT | Mode: golden_image | Ready: true"
    log "═══════════════════════════════════════════════════════════════════════════════"
else
    update_apars 6 100 95 "service_start_timeout" true false '"service_health_check_failed"'
    log "⚠️  Service not responding after 30s — may need more time"
    log "   Check: curl http://localhost:${JARVIS_PORT}/health"
    log "   Logs:  journalctl -u jarvis-prime.service"
fi
'''
        return script.replace("__STARTUP_SCRIPT_VERSION__", _STARTUP_SCRIPT_VERSION)

    async def _start_instance(self, instance_name: str) -> Tuple[bool, Optional[str]]:
        """
        Start a stopped/terminated instance.

        Returns:
            Tuple of (success: bool, error: Optional[str])
        """
        try:
            logger.info(f"☁️ [InvincibleNode] Sending start command: {instance_name}")

            operation = await asyncio.to_thread(
                self.instances_client.start,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=instance_name,
            )

            # Wait for operation to complete
            await self._wait_for_operation(operation, timeout=120)

            logger.info(f"✅ [InvincibleNode] VM start command completed: {instance_name}")
            return True, None

        except Exception as e:
            logger.error(f"❌ [InvincibleNode] Failed to start VM: {e}")
            return False, str(e)

    async def _create_static_vm(
        self, instance_name: str, static_ip_name: str, port: int
    ) -> Tuple[bool, Optional[str]]:
        """
        v229.0: Create a new VM with static IP, intelligently selecting the
        best deployment mode (golden image > container > startup script).
        
        This method now uses the same deployment mode hierarchy as
        _build_instance_config(), ensuring Invincible Node VMs get the
        golden image treatment when available.

        Returns:
            Tuple of (success: bool, error: Optional[str])
        """
        try:
            logger.info(f"☁️ [InvincibleNode] Creating VM with static IP: {instance_name}")

            # Get static IP address for binding
            static_ip = await self._get_static_ip_address(static_ip_name)
            if not static_ip:
                return False, f"Static IP '{static_ip_name}' not found"

            machine_type_url = f"zones/{self.config.zone}/machineTypes/{self.config.machine_type}"

            # ═══════════════════════════════════════════════════════════════════
            # v229.0: DEPLOYMENT MODE HIERARCHY (same as _build_instance_config)
            # 1. Golden Image → pre-baked everything (~30-60s startup)
            # 2. Container Mode → Docker pre-built (~2-3 min startup)
            # 3. Startup Script → traditional install (~10-15 min startup)
            # ═══════════════════════════════════════════════════════════════════
            source_image = None
            deployment_mode = "startup-script"
            golden_image_source = None
            _code_stale_warning = False  # v235.3: Golden image code freshness flag
            effective_disk_size_gb = self.config.boot_disk_size_gb
            
            # Try golden image first (highest priority)
            if self.config.use_golden_image:
                try:
                    builder = self.get_golden_image_builder()
                    latest_golden = await builder.get_latest_golden_image()
                    
                    if latest_golden:
                        # v233.0: Multi-criteria golden image validation
                        _skip_reasons: List[str] = []
                        if latest_golden.is_stale(self.config.golden_image_max_age_days):
                            _skip_reasons.append(
                                f"stale ({latest_golden.age_days:.0f}d > {self.config.golden_image_max_age_days}d)"
                            )
                        if self.is_golden_image_suspect(latest_golden.name):
                            _skip_reasons.append("flagged SUSPECT by corruption detector")
                        if latest_golden.is_model_mismatched(self.config.golden_image_model):
                            _skip_reasons.append(
                                f"model mismatch (has '{latest_golden.model_name}')"
                            )
                        # v235.3: Check if golden image predates current startup script.
                        # The golden image's jarvis_version tracks when it was created.
                        # If it's older than v235.3, the disk code lacks module-level
                        # app export. The startup script's code freshness check (Phase 4)
                        # will handle this at runtime, but log a heads-up here.
                        if latest_golden.jarvis_version and latest_golden.jarvis_version < "235.3":
                            _code_stale_warning = True
                            logger.info(
                                f"⚠️ [InvincibleNode] v235.3: Golden image '{latest_golden.name}' "
                                f"has jarvis_version={latest_golden.jarvis_version} (pre-v235.3). "
                                f"Startup script will auto-update J-Prime code on boot."
                            )

                        if _skip_reasons:
                            logger.warning(
                                f"⚠️ [InvincibleNode] Skipping golden image '{latest_golden.name}': "
                                f"{', '.join(_skip_reasons)} — falling back to standard image"
                            )
                        else:
                            golden_project = self.config.golden_image_project or self.config.project_id
                            golden_image_source = f"projects/{golden_project}/global/images/{latest_golden.name}"
                            source_image = golden_image_source
                            deployment_mode = "golden-image"
                            # Ensure disk is large enough for the golden image
                            if latest_golden.disk_size_gb:
                                effective_disk_size_gb = max(effective_disk_size_gb, latest_golden.disk_size_gb)
                            logger.info(
                                f"🌟 [InvincibleNode] Using golden image: {latest_golden.name} "
                                f"(age: {latest_golden.age_days:.1f}d, model: {latest_golden.model_name}, "
                                f"disk: {effective_disk_size_gb}GB)"
                            )
                    elif latest_golden is None:
                        logger.warning(
                            f"⚠️ [InvincibleNode] Golden image '{latest_golden.name}' is stale "
                            f"({latest_golden.age_days:.1f}d old). Falling back."
                        )
                    else:
                        logger.info("☁️ [InvincibleNode] No golden image found. Falling back.")
                except Exception as e:
                    logger.warning(f"⚠️ [InvincibleNode] Golden image lookup failed: {e}. Falling back.")
            
            # Fall back to standard image if no golden image
            if source_image is None:
                source_image = f"projects/{self.config.image_project}/global/images/family/{self.config.image_family}"
                logger.info(f"📜 [InvincibleNode] Using standard image: {self.config.image_family}")

            # Boot disk with intelligently selected source image
            boot_disk = compute_v1.AttachedDisk(
                auto_delete=True,
                boot=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    disk_size_gb=effective_disk_size_gb,
                    disk_type=f"zones/{self.config.zone}/diskTypes/{self.config.boot_disk_type}",
                    source_image=source_image,
                ),
            )

            # Network interface with STATIC IP
            network_interface = compute_v1.NetworkInterface(
                network=f"global/networks/{self.config.network}",
                access_configs=[
                    compute_v1.AccessConfig(
                        name="External NAT",
                        type="ONE_TO_ONE_NAT",
                        nat_i_p=static_ip,  # Bind to reserved static IP
                    )
                ],
            )

            # Service account
            service_account = compute_v1.ServiceAccount(
                email="default",
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )

            # v229.0: Dynamic repo URL discovery (consistent with _build_instance_config)
            jarvis_repo_url = os.environ.get("JARVIS_REPO_URL") or os.environ.get("JARVIS_PRIME_REPO_URL", "")
            if not jarvis_repo_url:
                try:
                    builder = self.get_golden_image_builder()
                    jarvis_repo_url = builder._discover_prime_repo_url()
                except Exception:
                    pass

            # Metadata
            metadata_items = [
                compute_v1.Items(key="jarvis-port", value=str(port)),
                compute_v1.Items(key="jarvis-components", value="inference"),
                compute_v1.Items(key="jarvis-trigger", value="invincible_node_v229"),
                compute_v1.Items(key="jarvis-created-at", value=datetime.now().isoformat()),
                compute_v1.Items(key="jarvis-deployment-mode", value=deployment_mode),
                compute_v1.Items(key="jarvis-startup-script-version", value=_STARTUP_SCRIPT_VERSION),  # v235.0
            ]
            
            # Pass repo URL to VM metadata
            if jarvis_repo_url:
                metadata_items.append(compute_v1.Items(key="jarvis-repo-url", value=jarvis_repo_url))
            
            # Track golden image source for lineage validation
            if golden_image_source:
                metadata_items.append(
                    compute_v1.Items(key="jarvis-golden-image-source", value=golden_image_source)
                )
                # v235.3: Signal to startup script that code freshness check is mandatory
                # because the golden image predates the current startup script version
                if _code_stale_warning:
                    metadata_items.append(
                        compute_v1.Items(key="jarvis-force-code-update", value="true")
                    )

            # Add startup script based on deployment mode
            if deployment_mode == "golden-image":
                # Use the golden image startup script (fast boot, APARS-compliant)
                # This is the same script used in _build_instance_config for golden images
                golden_startup_script = self._generate_golden_startup_script()
                metadata_items.append(compute_v1.Items(key="startup-script", value=golden_startup_script))
                logger.info("   ⚡ Golden startup script attached (expected: ~30-60s)")
            elif self.config.startup_script_path and os.path.exists(self.config.startup_script_path):
                with open(self.config.startup_script_path, "r") as f:
                    startup_script = f.read()
                metadata_items.append(compute_v1.Items(key="startup-script", value=startup_script))
                logger.info("   📜 Standard startup script attached (expected: ~10-15min)")

            # Build instance with STOP termination (Invincible Node)
            instance = compute_v1.Instance(
                name=instance_name,
                machine_type=machine_type_url,
                disks=[boot_disk],
                network_interfaces=[network_interface],
                service_accounts=[service_account],
                metadata=compute_v1.Metadata(items=metadata_items),
                tags=compute_v1.Tags(items=["jarvis-node", "http-server", "https-server"]),
                labels={
                    "created-by": "jarvis",
                    "type": "prime-node",
                    "vm-class": "invincible",
                    "deployment-mode": deployment_mode.replace("-", "_"),
                },
                scheduling=compute_v1.Scheduling(
                    preemptible=True,
                    on_host_maintenance="TERMINATE",
                    automatic_restart=False,
                    provisioning_model="SPOT",
                    instance_termination_action="STOP",  # INVINCIBLE: Survive preemption
                ),
            )

            # Create the VM
            operation = await asyncio.to_thread(
                self.instances_client.insert,
                project=self.config.project_id,
                zone=self.config.zone,
                instance_resource=instance,
            )

            # Wait for creation
            await self._wait_for_operation(operation, timeout=300)

            logger.info(
                f"✅ [InvincibleNode] VM created: {instance_name} "
                f"(mode: {deployment_mode}, image: {source_image.split('/')[-1]})"
            )
            return True, None

        except Exception as e:
            logger.error(f"❌ [InvincibleNode] Failed to create VM: {e}")
            return False, str(e)

    async def _poll_health_until_ready(
        self, ip: str, port: int, timeout: float, poll_interval: float,
        progress_callback: Optional[Callable[[int, str, str], None]] = None,
    ) -> Tuple[bool, str]:
        """
        Poll the health endpoint until the VM reports ready_for_inference=true.
        
        v220.1: Added progress_callback for real-time dashboard updates.

        Args:
            ip: IP address to poll
            port: Port number
            timeout: Max timeout in seconds
            poll_interval: Seconds between polls
            progress_callback: Optional callback(progress_pct, phase, detail) for real-time updates

        Returns:
            Tuple of (success: bool, final_status: str)
        """
        start_time = time.time()
        last_status = "starting"
        consecutive_errors = 0  # v235.0: Local variable, reset each polling session
        has_seen_version = False  # v235.3: Track if valid startup_script_version ever received
        has_seen_any_response = False  # v235.3: Track if VM has responded at all

        while (time.time() - start_time) < timeout:
            elapsed = time.time() - start_time
            is_ready, health_data = await self._ping_health_endpoint(ip, port, timeout=10.0)

            if is_ready:
                # v220.1: Report 100% on success
                if progress_callback:
                    try:
                        progress_callback(100, "ready", f"VM ready at {ip}")
                    except Exception:
                        pass
                return True, "ready_for_inference"

            # Extract progress info for logging and callback
            progress_pct = 0
            phase_name = "starting"
            detail = "Waiting for VM..."
            has_real_data = False  # v235.0: Only call callback for real data

            if "apars" in health_data:
                has_real_data = True  # v235.0: Real APARS data from VM
                has_seen_any_response = True  # v235.3
                consecutive_errors = 0  # v235.0: Reset on successful response
                apars = health_data["apars"]
                # v235.2: Detect startup script version mismatches at runtime
                # Grace period: ignore version mismatch for first 60s of polling.
                # The progress file (/tmp/jarvis_progress.json) may persist across
                # quick VM stop/start cycles. If a stale file from v235.1 is read
                # before the new startup script overwrites it, an immediate mismatch
                # check would trigger VM recycling → new VM boots → reads stale file
                # again → infinite recycle loop. The 60s grace allows the new startup
                # script to overwrite the file before we act on version mismatch.
                script_version = apars.get("startup_script_version")
                metadata_version = apars.get("startup_script_metadata_version")
                if (
                    script_version
                    and script_version not in ("unknown", "none", "null")
                ):
                    has_seen_version = True  # v235.3: Valid version received
                    if elapsed > 60:  # v235.3: Grace period for stale files
                        if script_version != _STARTUP_SCRIPT_VERSION:
                            logger.warning(
                                f"☁️ [InvincibleNode] Startup script version mismatch "
                                f"(vm={script_version}, expected={_STARTUP_SCRIPT_VERSION}, "
                                f"elapsed={int(elapsed)}s — past 60s grace period)."
                            )
                            return False, f"SCRIPT_VERSION_MISMATCH: {script_version}"
                        if (
                            metadata_version
                            and metadata_version not in ("unknown", "none", "null")
                            and metadata_version != script_version
                        ):
                            logger.warning(
                                f"☁️ [InvincibleNode] Startup script metadata mismatch "
                                f"(metadata={metadata_version}, vm={script_version})."
                            )
                            return False, f"SCRIPT_METADATA_MISMATCH: {metadata_version}"
                progress_pct = apars.get("total_progress", 0)
                phase_name = apars.get("phase_name", "unknown")
                eta = apars.get("eta_seconds", 0)
                deploy_mode = apars.get("deployment_mode", "")
                mode_tag = f" [{deploy_mode}]" if deploy_mode else ""
                detail = f"{phase_name}{mode_tag} ({progress_pct}%, ETA {eta}s)"
                last_status = f"phase={phase_name}, progress={progress_pct}%, eta={eta}s, mode={deploy_mode}"
                logger.debug(f"☁️ [InvincibleNode] Health poll: {last_status}")
            elif health_data.get("model_loaded") or health_data.get("status") == "healthy":
                # v235.2: J-Prime is running, model loaded or healthy — this is
                # meaningful progress data even without APARS. Set has_real_data
                # so the supervisor gets the update. This is safe because we're
                # past the stub phase (model_loaded/healthy means J-Prime is running),
                # so deferring the synthetic generator is correct behavior.
                has_seen_any_response = True  # v235.3
                has_real_data = True
                consecutive_errors = 0
                model_loaded = health_data.get("model_loaded", False)
                if model_loaded and health_data.get("status") == "healthy":
                    progress_pct = 95
                    phase_name = "model_ready"
                    detail = f"Model loaded, service healthy ({int(elapsed)}s)"
                elif model_loaded:
                    progress_pct = 90
                    phase_name = "model_loaded"
                    detail = f"Model loaded, initializing ({int(elapsed)}s)"
                else:
                    progress_pct = 85
                    phase_name = "service_healthy"
                    detail = f"Service healthy, loading model ({int(elapsed)}s)"
                last_status = f"phase={phase_name}, model_loaded={model_loaded}"
                logger.debug(
                    f"☁️ [InvincibleNode] J-Prime responding (no APARS): "
                    f"{phase_name}, model_loaded={model_loaded}, elapsed={int(elapsed)}s"
                )
            elif health_data.get("status") == "starting":
                # v235.0: VM is responding but WITHOUT APARS data (legacy format).
                # Do NOT set has_real_data — this is a pre-APARS response that
                # carries no real progress info (just "I'm alive, not ready").
                # If we mark it as real data, the supervisor callback sets
                # source="apars" → refreshes _last_real_gcp_progress_update
                # every 5s → permanently defers synthetic generator → dashboard
                # stuck at whatever synthetic % was reached before first response.
                # Let synthetic handle progress for non-APARS VMs.
                has_seen_any_response = True  # v235.3
                consecutive_errors = 0  # VM IS responding (reset error counter)
                mode = health_data.get("mode", "unknown")
                phase = health_data.get("phase", "starting")
                detail = f"VM booting: {phase} (mode={mode}, {int(elapsed)}s)"
                progress_pct = min(80, int((elapsed / timeout) * 70) + 10)
                phase_name = phase
                last_status = f"booting: phase={phase}, mode={mode}"
                logger.debug(
                    f"☁️ [InvincibleNode] Legacy health response (no APARS): "
                    f"phase={phase}, mode={mode}, elapsed={int(elapsed)}s"
                )
            elif "error" in health_data:
                last_status = f"error={health_data['error']}"
                detail = health_data.get("error", "Unknown error")[:40]
                consecutive_errors += 1

                # v235.0: Diagnostic escalation after sustained errors
                if consecutive_errors == 6:  # ~30s of failures at 5s intervals
                    logger.warning(
                        f"☁️ [InvincibleNode] Health endpoint unreachable "
                        f"for ~{consecutive_errors * poll_interval:.0f}s. "
                        f"Error: {health_data.get('error')}. "
                        f"Attempting SSH diagnostic..."
                    )
                    try:
                        ssh_ok = await self._check_vm_ssh_reachable(ip)
                        if ssh_ok:
                            logger.info(
                                f"☁️ [InvincibleNode] VM SSH reachable at {ip} "
                                f"but port {port} not responding. "
                                f"Likely: startup script failed or port not open."
                            )
                        else:
                            logger.warning(
                                f"☁️ [InvincibleNode] VM SSH also unreachable at {ip}. "
                                f"Likely: VM not booted, network issue, or firewall."
                            )
                    except Exception:
                        pass
            elif not health_data:
                consecutive_errors += 1
                # v229.1: No response at all — provide diagnostic transparency
                # This typically means VM is still booting (startup script not yet running)
                progress_pct = min(50, int((elapsed / timeout) * 40) + 5)
                if elapsed < 30:
                    detail = f"VM booting, no response yet ({int(elapsed)}s)"
                    phase_name = "booting"
                elif elapsed < 90:
                    detail = f"Startup script running ({int(elapsed)}s)"
                    phase_name = "startup_script"
                else:
                    detail = f"Waiting for health endpoint ({int(elapsed)}s)"
                    phase_name = "waiting_health"
                last_status = f"no_response, elapsed={int(elapsed)}s"

                # v235.0: Diagnostic escalation for no-response too
                if consecutive_errors == 6:
                    logger.warning(
                        f"☁️ [InvincibleNode] No health response "
                        f"for ~{consecutive_errors * poll_interval:.0f}s. "
                        f"Attempting SSH diagnostic..."
                    )
                    try:
                        ssh_ok = await self._check_vm_ssh_reachable(ip)
                        if ssh_ok:
                            logger.info(
                                f"☁️ [InvincibleNode] VM SSH reachable at {ip} "
                                f"but health endpoint not responding on port {port}. "
                                f"Likely: startup script still running or crashed."
                            )
                        else:
                            logger.warning(
                                f"☁️ [InvincibleNode] VM SSH also unreachable at {ip}. "
                                f"Likely: VM not booted, network issue, or firewall."
                            )
                    except Exception:
                        pass
            else:
                # Estimate progress based on elapsed time
                progress_pct = min(90, int((elapsed / timeout) * 90) + 20)
                detail = f"Polling health ({int(elapsed)}s elapsed)"

            # v235.3: Enforce version contract — if VM is responding but has
            # never reported a valid startup_script_version after the grace
            # period, the startup script is too old (pre-v235) or the APARS
            # enrichment pipeline is broken. Fail early so the outer retry
            # logic can recycle the VM with a current startup script.
            if (
                elapsed > 90
                and has_seen_any_response
                and not has_seen_version
            ):
                logger.warning(
                    f"☁️ [InvincibleNode] VM responding for {int(elapsed)}s "
                    f"but has never reported startup_script_version. "
                    f"Likely running pre-v235 startup script or APARS "
                    f"enrichment is not active. Triggering recycle."
                )
                return False, "VERSION_NEVER_REPORTED"

            # v235.0: Only call progress callback for real APARS data.
            # Legacy "status=starting" (no APARS key), errors, and no-response
            # cases do NOT trigger the callback. This prevents the supervisor
            # from setting source="apars" for non-APARS responses, which would
            # permanently defer the synthetic progress generator (the callback
            # refreshes _last_real_gcp_progress_update every 5s, creating an
            # infinite deferral loop → dashboard stuck at whatever synthetic
            # % was reached before the first legacy response arrived).
            if progress_callback and has_real_data:
                try:
                    progress_callback(progress_pct, phase_name, detail)
                except Exception:
                    pass  # Don't let callback errors break polling

            await asyncio.sleep(poll_interval)

        return False, f"timeout after {timeout}s (last: {last_status})"

    @property
    def is_static_vm_mode(self) -> bool:
        """Check if static VM (Invincible Node) mode is configured."""
        return bool(self.config.static_ip_name)

    # =========================================================================
    # CLOUD MONITOR: CLI Monitoring Suite
    # =========================================================================
    # These methods provide real-time visibility into the Invincible Node
    # for the unified_supervisor.py --monitor CLI mode.
    # =========================================================================

    async def get_invincible_node_status(self) -> Dict[str, Any]:
        """
        Get comprehensive status of the Invincible Node for monitoring dashboard.

        Returns:
            Dict with:
            - instance_name: str
            - zone: str
            - project_id: str
            - static_ip: str or None
            - gcp_status: RUNNING/STOPPED/TERMINATED/NOT_FOUND/ERROR
            - machine_type: str or None
            - uptime_seconds: float or None
            - termination_action: str or None
            - health: dict with HTTP health check results
            - error: str or None
        """
        result = {
            "instance_name": self.config.static_instance_name,
            "zone": self.config.zone,
            "project_id": self.config.project_id,
            "static_ip": None,
            "gcp_status": "UNKNOWN",
            "machine_type": None,
            "uptime_seconds": None,
            "termination_action": None,
            "created_at": None,
            "health": None,
            "error": None,
        }

        if not self.is_static_vm_mode:
            result["error"] = "Invincible Node not configured (GCP_VM_STATIC_IP_NAME not set)"
            return result

        # Get static IP address (read-only check, don't auto-create during status)
        try:
            # v210.0: Use auto_create=False for status checks to avoid side effects
            static_ip = await self._get_static_ip_address(self.config.static_ip_name, auto_create=False)
            if static_ip:
                result["static_ip"] = static_ip
            else:
                # IP doesn't exist yet - will be auto-created during ensure_static_vm_ready
                result["gcp_status"] = "NOT_FOUND"
                result["error"] = None  # Not an error, just needs to be created
        except Exception as e:
            result["error"] = f"Failed to get static IP: {e}"

        # Get instance details from GCP
        try:
            instance = await asyncio.to_thread(
                self.instances_client.get,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=self.config.static_instance_name,
            )

            result["gcp_status"] = instance.status
            result["machine_type"] = instance.machine_type.split("/")[-1] if instance.machine_type else None

            # Extract scheduling info
            if instance.scheduling:
                result["termination_action"] = instance.scheduling.instance_termination_action

            # Calculate uptime if running
            if instance.status == "RUNNING" and instance.last_start_timestamp:
                try:
                    from datetime import datetime as dt
                    # Parse GCP timestamp (RFC3339)
                    start_time = dt.fromisoformat(instance.last_start_timestamp.replace("Z", "+00:00"))
                    result["uptime_seconds"] = (dt.now(start_time.tzinfo) - start_time).total_seconds()
                except Exception:
                    pass

            # Get creation time
            if instance.creation_timestamp:
                result["created_at"] = instance.creation_timestamp

        except Exception as e:
            error_str = str(e).lower()
            if "404" in error_str or "not found" in error_str:
                result["gcp_status"] = "NOT_FOUND"
            else:
                result["gcp_status"] = "ERROR"
                result["error"] = str(e)

        # Perform health check if we have a static IP
        if result["static_ip"]:
            port = int(os.environ.get("JARVIS_PRIME_PORT", "8000"))
            is_healthy, health_data = await self._ping_health_endpoint(
                result["static_ip"], port, timeout=10.0
            )
            result["health"] = {
                "reachable": is_healthy or "error" not in health_data or health_data.get("error") != "timeout",
                "ready_for_inference": health_data.get("ready_for_inference", False),
                "status": health_data.get("status", "unknown"),
                "model_loaded": health_data.get("model_loaded", False),
                "active_model": health_data.get("active_model"),
                "apars": health_data.get("apars"),
                "raw": health_data,
            }

        return result

    def get_ssh_logs_command(
        self,
        log_path: str = "/var/log/jarvis-startup.log",
        lines: int = 50,
        follow: bool = True,
    ) -> List[str]:
        """
        Construct the gcloud SSH command to tail logs from the Invincible Node.

        Args:
            log_path: Path to the log file on the VM
            lines: Number of lines to show initially
            follow: Whether to follow (tail -f) the log

        Returns:
            List of command arguments for subprocess
        """
        instance_name = self.config.static_instance_name
        zone = self.config.zone
        project = self.config.project_id

        tail_cmd = f"tail -n {lines}"
        if follow:
            tail_cmd += " -f"
        tail_cmd += f" {log_path}"

        return [
            "gcloud", "compute", "ssh",
            instance_name,
            f"--zone={zone}",
            f"--project={project}",
            "--command", tail_cmd,
        ]

    async def stream_invincible_node_logs(
        self,
        log_path: str = "/var/log/jarvis-startup.log",
        lines: int = 50,
        callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Stream logs from the Invincible Node via SSH.

        Args:
            log_path: Path to the log file on the VM
            lines: Number of initial lines to show
            callback: Optional callback for each line (if None, prints to stdout)
        """
        import subprocess

        cmd = self.get_ssh_logs_command(log_path, lines, follow=True)

        logger.info(f"[CloudMonitor] Streaming logs: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if callback:
                    callback(decoded)
                else:
                    print(decoded)
        except asyncio.CancelledError:
            process.terminate()
            raise
        finally:
            if process.returncode is None:
                process.terminate()

    async def wake_invincible_node(self) -> Tuple[bool, str]:
        """
        Wake up the Invincible Node if it's stopped.

        Returns:
            Tuple of (success: bool, message: str)
        """
        if not self.is_static_vm_mode:
            return False, "Invincible Node not configured"

        # Get current status
        status, error = await self._describe_instance(self.config.static_instance_name)

        if status == "RUNNING":
            return True, "Node is already running"

        if status in ("STOPPED", "TERMINATED", "SUSPENDED"):
            success, err = await self._start_instance(self.config.static_instance_name)
            if success:
                return True, f"Node started successfully (was {status})"
            return False, f"Failed to start node: {err}"

        if status == "NOT_FOUND":
            return False, "Node does not exist. Run ./deploy_spot_node.sh first"

        return False, f"Node in unexpected state: {status}"

    async def stop_invincible_node(self) -> Tuple[bool, str]:
        """
        Stop the Invincible Node (to save costs).

        Returns:
            Tuple of (success: bool, message: str)
        """
        if not self.is_static_vm_mode:
            return False, "Invincible Node not configured"

        instance_name = self.config.static_instance_name

        # Get current status
        status, error = await self._describe_instance(instance_name)

        if status in ("STOPPED", "TERMINATED", "SUSPENDED"):
            return True, f"Node is already {status}"

        if status == "NOT_FOUND":
            return False, "Node does not exist"

        if status != "RUNNING":
            return False, f"Node in unexpected state: {status}"

        try:
            logger.info(f"[CloudMonitor] Stopping node: {instance_name}")

            operation = await asyncio.to_thread(
                self.instances_client.stop,
                project=self.config.project_id,
                zone=self.config.zone,
                instance=instance_name,
            )

            await self._wait_for_operation(operation, timeout=120)
            return True, "Node stopped successfully"

        except Exception as e:
            return False, f"Failed to stop node: {e}"


# ============================================================================
# SINGLETON MANAGEMENT
# ============================================================================

_gcp_vm_manager: Optional[GCPVMManager] = None
_manager_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def reset_gcp_vm_manager_singleton() -> None:
    """
    v132.0: Force reset the GCP VM Manager singleton.

    This is used when we need to re-create the manager with new configuration,
    such as when auto-enabling GCP during OOM prevention.

    IMPORTANT: This should only be called when you KNOW you need to re-initialize
    with different settings. Normal usage should use get_gcp_vm_manager().
    """
    global _gcp_vm_manager

    async with _manager_lock:
        if _gcp_vm_manager is not None:
            # Cleanup existing manager
            try:
                if hasattr(_gcp_vm_manager, 'cleanup_all_vms'):
                    # Don't cleanup VMs on reset - just release the reference
                    pass
            except Exception as e:
                logger.debug(f"[GCPVMManager] Cleanup during reset: {e}")

            _gcp_vm_manager = None
            logger.info("[GCPVMManager] v132.0: Singleton reset - will reinitialize with fresh config")


async def get_gcp_vm_manager_with_force_enable() -> GCPVMManager:
    """
    v132.0: Get GCP VM Manager with forced enable.

    This resets the singleton and creates a new manager with GCP_ENABLED=true.
    Used for OOM prevention auto-enable functionality.

    Returns:
        GCPVMManager with GCP enabled (if credentials available)
    """
    # Set environment variable BEFORE resetting singleton
    os.environ["GCP_ENABLED"] = "true"

    # Reset singleton to force re-creation with new config
    await reset_gcp_vm_manager_singleton()

    # Create new manager with fresh config (will read GCP_ENABLED=true)
    return await get_gcp_vm_manager()


async def get_gcp_vm_manager(config: Optional[VMManagerConfig] = None) -> GCPVMManager:
    """
    Get or create singleton GCP VM Manager with thread-safe initialization.
    
    Args:
        config: Optional configuration override
        
    Returns:
        Initialized GCPVMManager instance
        
    Raises:
        RuntimeError: If GCP Compute API is not available
    """
    global _gcp_vm_manager

    # Quick check without lock
    if _gcp_vm_manager is not None and _gcp_vm_manager.initialized:
        return _gcp_vm_manager

    async with _manager_lock:
        # Double-check after acquiring lock
        if _gcp_vm_manager is None:
            _gcp_vm_manager = GCPVMManager(config=config)
        
        if not _gcp_vm_manager.initialized:
            await _gcp_vm_manager.initialize()

        return _gcp_vm_manager


async def get_gcp_vm_manager_safe(config: Optional[VMManagerConfig] = None) -> Optional[GCPVMManager]:
    """
    Safely get GCP VM Manager, returning None if not available.
    
    This is useful when you want to use the manager if available
    but don't want to fail if GCP is not configured.
    
    Args:
        config: Optional configuration override
        
    Returns:
        GCPVMManager instance or None if not available
    """
    try:
        return await get_gcp_vm_manager(config)
    except Exception as e:
        logger.debug(f"GCP VM Manager not available: {e}")
        return None


async def create_vm_if_needed(
    memory_snapshot, 
    components: List[str], 
    trigger_reason: str, 
    metadata: Optional[Dict] = None
) -> Optional[VMInstance]:
    """
    Convenience function: Check if VM needed and create if so.

    Args:
        memory_snapshot: Memory pressure snapshot
        components: Components to run on the VM
        trigger_reason: Why this VM is being created
        metadata: Additional metadata
        
    Returns:
        VMInstance if created, None otherwise
    """
    try:
        # Explicit opt-in guardrail (prevents surprise spend).
        # Accept legacy GCP_VM_ENABLED, otherwise use JARVIS_SPOT_VM_ENABLED.
        enabled_flag = os.getenv("GCP_VM_ENABLED")
        if enabled_flag is None:
            enabled_flag = os.getenv("JARVIS_SPOT_VM_ENABLED", "false")
        if str(enabled_flag).lower() != "true":
            logger.info("ℹ️  Spot VM creation disabled by configuration")
            return None

        manager = await get_gcp_vm_manager_safe()
        
        if manager is None:
            logger.debug("GCP VM Manager not available - cannot create VM")
            return None

        should_create, reason, confidence = await manager.should_create_vm(
            memory_snapshot, trigger_reason
        )

        if should_create:
            logger.info(f"✅ VM creation recommended: {reason} (confidence: {confidence:.2%})")
            return await manager.create_vm(components, trigger_reason, metadata)
        else:
            logger.info(f"ℹ️  VM creation not needed: {reason}")
            return None
            
    except Exception as e:
        log_component_failure(
            "gcp-vm",
            "Error in create_vm_if_needed",
            error=e,
        )
        return None


async def cleanup_vm_manager():
    """Cleanup the global VM manager instance"""
    global _gcp_vm_manager

    async with _manager_lock:
        if _gcp_vm_manager is not None:
            await _gcp_vm_manager.cleanup()
            _gcp_vm_manager = None


# ============================================================================
# v95.0: PROACTIVE MEMORY-AWARE INITIALIZATION
# ============================================================================

async def proactive_vm_manager_init(
    memory_threshold: float = 70.0,
    force: bool = False,
) -> Optional[GCPVMManager]:
    """
    v95.0: Proactively initialize VM manager based on memory pressure.

    This function checks memory usage and initializes the VM manager
    if memory is above the threshold, preparing for potential offloading.

    Args:
        memory_threshold: Memory percentage threshold to trigger init (default 70%)
        force: If True, initialize regardless of memory pressure

    Returns:
        GCPVMManager instance if initialized, None if not needed or unavailable
    """
    try:
        import psutil
        mem_percent = psutil.virtual_memory().percent

        if not force and mem_percent < memory_threshold:
            logger.debug(
                f"[v95.0] Memory ({mem_percent:.1f}%) below threshold ({memory_threshold}%) - "
                f"VM manager init deferred"
            )
            return None

        logger.info(
            f"[v95.0] Memory pressure detected ({mem_percent:.1f}%) - "
            f"proactively initializing VM manager"
        )

        manager = await get_gcp_vm_manager_safe()

        if manager:
            logger.info("[v95.0] ✅ VM manager proactively initialized for memory offloading")
            return manager
        else:
            logger.warning(
                "[v95.0] ⚠️ VM manager not available - local memory management only"
            )
            return None

    except Exception as e:
        logger.debug(f"[v95.0] Proactive VM manager init failed: {e}")
        return None


class LocalMemoryFallback:
    """
    v95.0: Fallback handler when GCP VM is not available.

    Provides local memory management strategies as a fallback:
    - GC triggering
    - Cache clearing
    - Process priority adjustment
    - Memory mapping suggestions
    """

    _instance: Optional["LocalMemoryFallback"] = None

    def __new__(cls) -> "LocalMemoryFallback":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self.logger = logging.getLogger("LocalMemoryFallback")
        self._gc_cooldown = 30.0  # seconds between GC attempts
        self._last_gc_time = 0.0
        self._cache_clear_callbacks: List[Callable] = []

    def register_cache_clear_callback(self, callback: Callable) -> None:
        """Register a callback to be invoked when clearing caches."""
        if callback not in self._cache_clear_callbacks:
            self._cache_clear_callbacks.append(callback)

    async def attempt_local_relief(self, target_free_mb: float = 1024.0) -> Dict[str, Any]:
        """
        Attempt to free memory locally without GCP VM.

        Returns:
            Dict with 'freed_mb' and 'strategies_used'
        """
        import gc
        import psutil

        result = {
            "freed_mb": 0.0,
            "strategies_used": [],
            "initial_memory_percent": psutil.virtual_memory().percent,
            "final_memory_percent": 0.0,
        }

        initial_available = psutil.virtual_memory().available / (1024 * 1024)

        # Strategy 1: Trigger garbage collection
        current_time = time.time()
        if current_time - self._last_gc_time >= self._gc_cooldown:
            gc.collect()
            self._last_gc_time = current_time
            result["strategies_used"].append("garbage_collection")
            self.logger.info("[LocalMemoryFallback] Triggered garbage collection")

        # Strategy 2: Clear registered caches
        for callback in self._cache_clear_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback()
                else:
                    callback()
                result["strategies_used"].append("cache_clear")
            except Exception as e:
                self.logger.debug(f"Cache clear callback failed: {e}")

        # Strategy 3: Suggest memory-intensive process reduction
        # This is informational - actual process management is handled elsewhere
        try:
            high_mem_procs = []
            for proc in psutil.process_iter(['pid', 'name', 'memory_percent']):
                try:
                    if proc.info['memory_percent'] > 5.0:
                        high_mem_procs.append({
                            'pid': proc.info['pid'],
                            'name': proc.info['name'],
                            'memory_percent': proc.info['memory_percent']
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if high_mem_procs:
                high_mem_procs.sort(key=lambda x: x['memory_percent'], reverse=True)
                result["high_memory_processes"] = high_mem_procs[:5]
                self.logger.info(
                    f"[LocalMemoryFallback] Top memory consumers: "
                    f"{[p['name'] for p in high_mem_procs[:3]]}"
                )
        except Exception as e:
            self.logger.debug(f"Process inspection failed: {e}")

        # Calculate freed memory
        final_available = psutil.virtual_memory().available / (1024 * 1024)
        result["freed_mb"] = final_available - initial_available
        result["final_memory_percent"] = psutil.virtual_memory().percent

        self.logger.info(
            f"[LocalMemoryFallback] Relief attempt: {result['freed_mb']:.1f}MB freed, "
            f"strategies: {result['strategies_used']}"
        )

        return result


def get_local_memory_fallback() -> LocalMemoryFallback:
    """Get the singleton LocalMemoryFallback instance."""
    return LocalMemoryFallback()


def is_vm_manager_available() -> bool:
    """Check if VM manager is available without initializing"""
    return COMPUTE_AVAILABLE


def get_vm_manager_sync() -> Optional[GCPVMManager]:
    """Get VM manager synchronously (only if already initialized)"""
    return _gcp_vm_manager if _gcp_vm_manager and _gcp_vm_manager.initialized else None


def record_vm_activity(ip_address: str = "", vm_name: str = "") -> bool:
    """
    Module-level helper for recording VM activity from any consumer.

    Safe to call from anywhere — returns False if VM manager isn't running.
    Never raises exceptions. Used by transport-layer hooks (HybridBackendClient)
    and direct HTTP fallback paths (cloud_ml_router, prime_router).

    Args:
        ip_address: VM's external or internal IP address
        vm_name: VM instance name (alternative to IP)

    Returns:
        True if activity was recorded, False otherwise
    """
    manager = get_vm_manager_sync()
    if not manager:
        return False
    try:
        if ip_address:
            return manager.record_vm_activity_by_ip(ip_address)
        if vm_name:
            return manager.record_vm_activity_by_name(vm_name)
    except Exception:
        pass
    return False

