#!/usr/bin/env python3
"""
JARVIS Infrastructure Orchestrator - On-Demand Cloud Resource Management
=========================================================================
v1.0.0 - Unified Infrastructure Lifecycle Edition

The root problem this solves:
- GCP resources (Cloud Run, Redis, etc.) stay deployed even when JARVIS isn't running
- This causes idle costs and resource waste
- No unified lifecycle management across JARVIS, JARVIS-Prime, and Reactor-Core

Solution: On-demand infrastructure provisioning and automatic cleanup.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │                    JARVIS Supervisor Starts                          │
    │  ┌─────────────────────────────────────────────────────────────┐    │
    │  │           Infrastructure Orchestrator                        │    │
    │  │  ┌──────────────────┬──────────────────┬─────────────────┐  │    │
    │  │  │ JARVIS Backend   │  JARVIS Prime    │  Reactor Core   │  │    │
    │  │  │ (Cloud Run)      │  (Cloud Run)     │  (GCS/Redis)    │  │    │
    │  │  └──────────────────┴──────────────────┴─────────────────┘  │    │
    │  │                                                              │    │
    │  │  Decision Engine:                                            │    │
    │  │  • Need GCP? → Memory pressure, workload type, config       │    │
    │  │  • Provision? → terraform apply (targeted modules)          │    │
    │  │  • Destroy? → On shutdown, terraform destroy                │    │
    │  └─────────────────────────────────────────────────────────────┘    │
    │                                                                      │
    │  On Shutdown: terraform destroy -target=<resources_we_created>      │
    └─────────────────────────────────────────────────────────────────────┘

Key Principles:
1. Only destroy what WE created (don't destroy pre-existing resources)
2. Intelligent decision-making (don't provision if not needed)
3. Async/parallel operations for fast startup/shutdown
4. Environment-driven configuration (no hardcoding)
5. Circuit breaker pattern for fault tolerance
6. Multi-repo awareness (JARVIS, Prime, Reactor)

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Enums and Constants
# =============================================================================

class ResourceState(Enum):
    """State of a managed resource."""
    UNKNOWN = auto()
    NOT_PROVISIONED = auto()
    PROVISIONING = auto()
    PROVISIONED = auto()
    FAILED = auto()
    DESTROYING = auto()
    DESTROYED = auto()


class ProvisioningReason(Enum):
    """Why a resource should be provisioned."""
    MEMORY_PRESSURE = "memory_pressure"
    EXPLICIT_REQUEST = "explicit_request"
    WORKLOAD_TYPE = "workload_type"
    CLOUD_FALLBACK = "cloud_fallback"
    CONFIG_ENABLED = "config_enabled"
    CROSS_REPO_DEPENDENCY = "cross_repo_dependency"


class DestroyReason(Enum):
    """Why a resource should be destroyed."""
    JARVIS_SHUTDOWN = "jarvis_shutdown"
    RESOURCE_IDLE = "resource_idle"
    COST_LIMIT_REACHED = "cost_limit_reached"
    EXPLICIT_REQUEST = "explicit_request"
    ERROR_RECOVERY = "error_recovery"


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class InfrastructureConfig:
    """Configuration for infrastructure orchestration."""

    # Feature toggles
    on_demand_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_INFRA_ON_DEMAND", "true").lower() == "true"
    )
    auto_destroy_on_shutdown: bool = field(
        default_factory=lambda: os.getenv("JARVIS_INFRA_AUTO_DESTROY", "true").lower() == "true"
    )

    # Terraform settings
    terraform_dir: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_TERRAFORM_DIR",
            str(Path(__file__).parent.parent.parent / "terraform")
        ))
    )
    terraform_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_TERRAFORM_TIMEOUT", "300"))
    )
    terraform_auto_approve: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TERRAFORM_AUTO_APPROVE", "true").lower() == "true"
    )

    # Resource thresholds for intelligent provisioning
    memory_pressure_threshold_gb: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_MEMORY_THRESHOLD_GB", "4.0"))
    )

    # Cloud Run settings
    jarvis_prime_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_USE_CLOUD_RUN", "false").lower() == "true"
    )
    jarvis_backend_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_BACKEND_USE_CLOUD_RUN", "false").lower() == "true"
    )
    redis_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_REDIS_ENABLED", "false").lower() == "true"
    )

    # Multi-repo paths
    jarvis_prime_path: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_PRIME_PATH",
            str(Path.home() / "Documents/repos/jarvis-prime")
        ))
    )
    reactor_core_path: Path = field(
        default_factory=lambda: Path(os.getenv(
            "REACTOR_CORE_PATH",
            str(Path.home() / "Documents/repos/reactor-core")
        ))
    )

    # Cost protection
    daily_cost_limit_usd: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_DAILY_COST_LIMIT", "1.0"))
    )

    # Circuit breaker
    max_consecutive_failures: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_INFRA_MAX_FAILURES", "3"))
    )
    circuit_breaker_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_INFRA_CB_TIMEOUT", "300"))
    )

    # State persistence
    state_file: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_INFRA_STATE_FILE",
            str(Path.home() / ".jarvis/infrastructure_state.json")
        ))
    )


# =============================================================================
# Resource Tracking
# =============================================================================

@dataclass
class ManagedResource:
    """Tracks a single managed infrastructure resource."""
    name: str
    terraform_module: str
    state: ResourceState = ResourceState.UNKNOWN
    provisioned_at: Optional[float] = None
    destroyed_at: Optional[float] = None
    provisioning_reason: Optional[ProvisioningReason] = None
    we_created_it: bool = False  # Critical: only destroy if WE created it
    estimated_hourly_cost_usd: float = 0.0
    last_health_check: Optional[float] = None
    health_check_failures: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InfrastructureState:
    """Complete state of managed infrastructure."""
    resources: Dict[str, ManagedResource] = field(default_factory=dict)
    session_started_at: float = field(default_factory=time.time)
    total_cost_this_session_usd: float = 0.0
    terraform_apply_count: int = 0
    terraform_destroy_count: int = 0
    last_terraform_run: Optional[float] = None
    errors: List[Dict[str, Any]] = field(default_factory=list)


# =============================================================================
# Circuit Breaker
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = auto()     # Normal operation
    OPEN = auto()       # Failing, reject calls
    HALF_OPEN = auto()  # Testing if recovered


@dataclass
class CircuitBreaker:
    """Circuit breaker for infrastructure operations."""
    failure_count: int = 0
    last_failure_time: Optional[float] = None
    state: CircuitState = CircuitState.CLOSED
    max_failures: int = 3
    timeout_seconds: int = 300

    def record_success(self):
        """Record a successful operation."""
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self):
        """Record a failed operation."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.max_failures:
            self.state = CircuitState.OPEN

    def can_proceed(self) -> bool:
        """Check if operation can proceed."""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if timeout has passed
            if self.last_failure_time:
                elapsed = time.time() - self.last_failure_time
                if elapsed >= self.timeout_seconds:
                    self.state = CircuitState.HALF_OPEN
                    return True
            return False

        # HALF_OPEN: Allow one request to test
        return True


# =============================================================================
# Infrastructure Orchestrator
# =============================================================================

class InfrastructureOrchestrator:
    """
    Central orchestrator for on-demand infrastructure management.

    This class manages:
    - Intelligent resource provisioning based on workload
    - Automatic cleanup on JARVIS shutdown
    - Multi-repo awareness (JARVIS, Prime, Reactor)
    - Cost tracking and protection
    - Circuit breaker for fault tolerance

    Usage:
        orchestrator = InfrastructureOrchestrator()

        # On JARVIS startup
        await orchestrator.initialize()
        await orchestrator.ensure_infrastructure()

        # During runtime
        orchestrator.track_cost(0.01)  # Track resource usage

        # On JARVIS shutdown
        await orchestrator.cleanup_infrastructure()
    """

    def __init__(self, config: Optional[InfrastructureConfig] = None):
        self.config = config or InfrastructureConfig()
        self.state = InfrastructureState()
        self.circuit_breaker = CircuitBreaker(
            max_failures=self.config.max_consecutive_failures,
            timeout_seconds=self.config.circuit_breaker_timeout_seconds,
        )

        self._initialized = False
        self._shutdown_requested = False
        self._lock = asyncio.Lock()

        # Callbacks
        self._on_resource_provisioned: List[Callable] = []
        self._on_resource_destroyed: List[Callable] = []
        self._on_cost_threshold: List[Callable] = []

        logger.info("[InfraOrchestrator] Created with on_demand=%s, auto_destroy=%s",
                    self.config.on_demand_enabled, self.config.auto_destroy_on_shutdown)

    # =========================================================================
    # Initialization
    # =========================================================================

    async def initialize(self) -> bool:
        """Initialize the orchestrator and load state."""
        if self._initialized:
            return True

        logger.info("[InfraOrchestrator] Initializing...")

        try:
            # Verify terraform is available
            if not await self._verify_terraform():
                logger.warning("[InfraOrchestrator] Terraform not available - infrastructure management disabled")
                return False

            # Load persisted state
            await self._load_state()

            # Initialize resource tracking
            self._init_resource_tracking()

            # Check current Terraform state
            await self._sync_terraform_state()

            self._initialized = True
            logger.info("[InfraOrchestrator] Initialized successfully")
            return True

        except Exception as e:
            logger.error(f"[InfraOrchestrator] Initialization failed: {e}")
            return False

    def _init_resource_tracking(self):
        """Initialize tracking for all manageable resources."""
        # JARVIS Prime Cloud Run
        self.state.resources["jarvis_prime"] = ManagedResource(
            name="JARVIS-Prime Cloud Run",
            terraform_module="module.jarvis_prime",
            estimated_hourly_cost_usd=0.03,  # ~$0.02-0.05/hr
        )

        # JARVIS Backend Cloud Run
        self.state.resources["jarvis_backend"] = ManagedResource(
            name="JARVIS Backend Cloud Run",
            terraform_module="module.jarvis_backend",
            estimated_hourly_cost_usd=0.10,  # ~$0.05-0.15/hr
        )

        # Redis/Memorystore
        self.state.resources["redis"] = ManagedResource(
            name="Cloud Memorystore (Redis)",
            terraform_module="module.storage",
            estimated_hourly_cost_usd=0.02,  # ~$15/month = ~$0.02/hr
        )

        # Spot VM Template (FREE - just tracking)
        self.state.resources["spot_vm_template"] = ManagedResource(
            name="Spot VM Template",
            terraform_module="module.compute",
            estimated_hourly_cost_usd=0.0,  # Template is free
        )

    # =========================================================================
    # Terraform Operations
    # =========================================================================

    async def _verify_terraform(self) -> bool:
        """Verify Terraform is installed and configured."""
        try:
            # Check terraform binary
            result = await asyncio.create_subprocess_exec(
                "terraform", "version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(result.communicate(), timeout=10)

            if result.returncode != 0:
                return False

            # Check terraform directory exists
            if not self.config.terraform_dir.exists():
                logger.warning(f"[InfraOrchestrator] Terraform dir not found: {self.config.terraform_dir}")
                return False

            logger.debug(f"[InfraOrchestrator] Terraform available: {stdout.decode().splitlines()[0]}")
            return True

        except (FileNotFoundError, asyncio.TimeoutError):
            return False

    async def _sync_terraform_state(self):
        """Sync local state with actual Terraform state."""
        try:
            # Run terraform state list to see what exists
            result = await asyncio.create_subprocess_exec(
                "terraform", "state", "list",
                cwd=str(self.config.terraform_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                result.communicate(),
                timeout=30
            )

            if result.returncode != 0:
                logger.debug(f"[InfraOrchestrator] No Terraform state or error: {stderr.decode()}")
                return

            existing_resources = stdout.decode().strip().split("\n")
            existing_resources = [r for r in existing_resources if r]  # Remove empty

            # Update resource states based on what exists
            for resource_key, resource in self.state.resources.items():
                # Check if module exists in state
                module_exists = any(
                    r.startswith(resource.terraform_module.replace("module.", "module."))
                    for r in existing_resources
                )

                if module_exists:
                    # Resource exists - but did WE create it this session?
                    if resource.state == ResourceState.UNKNOWN:
                        resource.state = ResourceState.PROVISIONED
                        resource.we_created_it = False  # Pre-existing, not ours
                        logger.info(f"[InfraOrchestrator] Found pre-existing: {resource.name}")
                else:
                    resource.state = ResourceState.NOT_PROVISIONED

        except asyncio.TimeoutError:
            logger.warning("[InfraOrchestrator] Terraform state sync timed out")
        except Exception as e:
            logger.debug(f"[InfraOrchestrator] State sync error: {e}")

    async def _terraform_apply(
        self,
        targets: List[str],
        variables: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Run terraform apply for specific targets."""
        if not self.circuit_breaker.can_proceed():
            logger.warning("[InfraOrchestrator] Circuit breaker OPEN - skipping terraform apply")
            return False

        async with self._lock:
            try:
                cmd = ["terraform", "apply"]

                if self.config.terraform_auto_approve:
                    cmd.append("-auto-approve")

                # Add targets
                for target in targets:
                    cmd.extend(["-target", target])

                # Add variables
                if variables:
                    for key, value in variables.items():
                        cmd.extend(["-var", f"{key}={value}"])

                logger.info(f"[InfraOrchestrator] Running: {' '.join(cmd)}")

                result = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(self.config.terraform_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                stdout, stderr = await asyncio.wait_for(
                    result.communicate(),
                    timeout=self.config.terraform_timeout_seconds
                )

                if result.returncode == 0:
                    self.circuit_breaker.record_success()
                    self.state.terraform_apply_count += 1
                    self.state.last_terraform_run = time.time()
                    logger.info("[InfraOrchestrator] Terraform apply successful")
                    return True
                else:
                    self.circuit_breaker.record_failure()
                    error_msg = stderr.decode()[:500]
                    logger.error(f"[InfraOrchestrator] Terraform apply failed: {error_msg}")
                    self.state.errors.append({
                        "time": time.time(),
                        "operation": "apply",
                        "targets": targets,
                        "error": error_msg,
                    })
                    return False

            except asyncio.TimeoutError:
                self.circuit_breaker.record_failure()
                logger.error(f"[InfraOrchestrator] Terraform apply timed out after {self.config.terraform_timeout_seconds}s")
                return False
            except Exception as e:
                self.circuit_breaker.record_failure()
                logger.error(f"[InfraOrchestrator] Terraform apply error: {e}")
                return False

    async def _terraform_destroy(
        self,
        targets: List[str],
    ) -> bool:
        """Run terraform destroy for specific targets."""
        if not targets:
            logger.debug("[InfraOrchestrator] No targets to destroy")
            return True

        async with self._lock:
            try:
                cmd = ["terraform", "destroy"]

                if self.config.terraform_auto_approve:
                    cmd.append("-auto-approve")

                # Add targets
                for target in targets:
                    cmd.extend(["-target", target])

                logger.info(f"[InfraOrchestrator] Running: {' '.join(cmd)}")

                result = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(self.config.terraform_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                stdout, stderr = await asyncio.wait_for(
                    result.communicate(),
                    timeout=self.config.terraform_timeout_seconds
                )

                if result.returncode == 0:
                    self.state.terraform_destroy_count += 1
                    self.state.last_terraform_run = time.time()
                    logger.info("[InfraOrchestrator] Terraform destroy successful")
                    return True
                else:
                    error_msg = stderr.decode()[:500]
                    logger.error(f"[InfraOrchestrator] Terraform destroy failed: {error_msg}")
                    return False

            except asyncio.TimeoutError:
                logger.error(f"[InfraOrchestrator] Terraform destroy timed out")
                return False
            except Exception as e:
                logger.error(f"[InfraOrchestrator] Terraform destroy error: {e}")
                return False

    # =========================================================================
    # Intelligent Decision Making
    # =========================================================================

    def _needs_cloud_infrastructure(self) -> Tuple[bool, List[ProvisioningReason]]:
        """Determine if cloud infrastructure is needed."""
        reasons = []

        # Check explicit configuration
        if self.config.jarvis_prime_enabled:
            reasons.append(ProvisioningReason.CONFIG_ENABLED)
        if self.config.jarvis_backend_enabled:
            reasons.append(ProvisioningReason.CONFIG_ENABLED)
        if self.config.redis_enabled:
            reasons.append(ProvisioningReason.CONFIG_ENABLED)

        # Check memory pressure
        try:
            import psutil
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024 ** 3)

            if available_gb < self.config.memory_pressure_threshold_gb:
                reasons.append(ProvisioningReason.MEMORY_PRESSURE)
                logger.info(f"[InfraOrchestrator] Memory pressure: {available_gb:.1f}GB available < {self.config.memory_pressure_threshold_gb}GB threshold")
        except ImportError:
            pass

        return len(reasons) > 0, reasons

    def _get_resources_to_provision(self, reasons: List[ProvisioningReason]) -> List[str]:
        """Determine which resources to provision based on reasons."""
        targets = []

        # Check each resource
        if self.config.jarvis_prime_enabled or ProvisioningReason.MEMORY_PRESSURE in reasons:
            resource = self.state.resources.get("jarvis_prime")
            if resource and resource.state == ResourceState.NOT_PROVISIONED:
                targets.append(resource.terraform_module)

        if self.config.jarvis_backend_enabled:
            resource = self.state.resources.get("jarvis_backend")
            if resource and resource.state == ResourceState.NOT_PROVISIONED:
                targets.append(resource.terraform_module)

        if self.config.redis_enabled:
            resource = self.state.resources.get("redis")
            if resource and resource.state == ResourceState.NOT_PROVISIONED:
                targets.append(resource.terraform_module)

        return targets

    def _get_resources_to_destroy(self) -> List[str]:
        """Get resources that WE created and should destroy."""
        targets = []

        for key, resource in self.state.resources.items():
            if resource.we_created_it and resource.state == ResourceState.PROVISIONED:
                targets.append(resource.terraform_module)
                logger.debug(f"[InfraOrchestrator] Will destroy: {resource.name}")

        return targets

    # =========================================================================
    # Public API
    # =========================================================================

    async def ensure_infrastructure(self) -> bool:
        """
        Ensure required infrastructure is provisioned.

        This is the main entry point for startup. It:
        1. Checks if cloud infrastructure is needed
        2. Provisions only what's needed
        3. Tracks what WE created for later cleanup
        """
        if not self._initialized:
            await self.initialize()

        if not self.config.on_demand_enabled:
            logger.info("[InfraOrchestrator] On-demand infrastructure disabled")
            return True

        needed, reasons = self._needs_cloud_infrastructure()

        if not needed:
            logger.info("[InfraOrchestrator] No cloud infrastructure needed")
            return True

        logger.info(f"[InfraOrchestrator] Infrastructure needed: {[r.value for r in reasons]}")

        # Get targets to provision
        targets = self._get_resources_to_provision(reasons)

        if not targets:
            logger.info("[InfraOrchestrator] All needed resources already provisioned")
            return True

        # Build variables for terraform
        variables = {}
        if "module.jarvis_prime" in targets:
            variables["enable_jarvis_prime"] = "true"
        if "module.jarvis_backend" in targets:
            variables["enable_jarvis_backend"] = "true"
        if "module.storage" in targets:
            variables["enable_redis"] = "true"

        # Run terraform apply
        logger.info(f"[InfraOrchestrator] Provisioning: {targets}")
        success = await self._terraform_apply(targets, variables)

        if success:
            # Mark resources as provisioned by us
            for target in targets:
                for key, resource in self.state.resources.items():
                    if resource.terraform_module == target:
                        resource.state = ResourceState.PROVISIONED
                        resource.we_created_it = True
                        resource.provisioned_at = time.time()
                        resource.provisioning_reason = reasons[0] if reasons else None
                        logger.info(f"[InfraOrchestrator] Provisioned: {resource.name}")

            # Persist state
            await self._save_state()

            # Notify callbacks
            for callback in self._on_resource_provisioned:
                try:
                    await callback(targets)
                except Exception as e:
                    logger.debug(f"Callback error: {e}")

        return success

    async def cleanup_infrastructure(self, reason: DestroyReason = DestroyReason.JARVIS_SHUTDOWN) -> bool:
        """
        Clean up infrastructure that WE created.

        This is the main entry point for shutdown. It:
        1. Finds resources we created this session
        2. Destroys only those resources
        3. Leaves pre-existing resources alone
        """
        if not self._initialized:
            logger.debug("[InfraOrchestrator] Not initialized - nothing to cleanup")
            return True

        if not self.config.auto_destroy_on_shutdown:
            logger.info("[InfraOrchestrator] Auto-destroy disabled - skipping cleanup")
            return True

        self._shutdown_requested = True

        # Get resources we created
        targets = self._get_resources_to_destroy()

        if not targets:
            logger.info("[InfraOrchestrator] No resources to cleanup (we didn't create any)")
            return True

        logger.info(f"[InfraOrchestrator] Cleaning up {len(targets)} resources: {targets}")
        logger.info(f"[InfraOrchestrator] Reason: {reason.value}")

        # Run terraform destroy
        success = await self._terraform_destroy(targets)

        if success:
            # Update resource states
            for target in targets:
                for key, resource in self.state.resources.items():
                    if resource.terraform_module == target:
                        resource.state = ResourceState.DESTROYED
                        resource.destroyed_at = time.time()
                        logger.info(f"[InfraOrchestrator] Destroyed: {resource.name}")

            # Notify callbacks
            for callback in self._on_resource_destroyed:
                try:
                    await callback(targets)
                except Exception as e:
                    logger.debug(f"Callback error: {e}")
        else:
            logger.warning("[InfraOrchestrator] Some resources may not have been cleaned up!")

        # Persist final state
        await self._save_state()

        return success

    async def force_cleanup_all(self) -> bool:
        """
        Force cleanup ALL cloud resources (emergency use only).

        WARNING: This destroys ALL Cloud Run services, not just ones we created.
        Use only for emergency cost control.
        """
        logger.warning("[InfraOrchestrator] FORCE CLEANUP - destroying ALL cloud resources!")

        all_targets = [
            "module.jarvis_prime",
            "module.jarvis_backend",
            "module.storage",
        ]

        # Set all resources to false
        variables = {
            "enable_jarvis_prime": "false",
            "enable_jarvis_backend": "false",
            "enable_redis": "false",
        }

        return await self._terraform_destroy(all_targets)

    # =========================================================================
    # Cost Tracking
    # =========================================================================

    def track_cost(self, cost_usd: float):
        """Track resource cost."""
        self.state.total_cost_this_session_usd += cost_usd

        # Check cost threshold
        if self.state.total_cost_this_session_usd >= self.config.daily_cost_limit_usd:
            logger.warning(
                f"[InfraOrchestrator] Daily cost limit reached: "
                f"${self.state.total_cost_this_session_usd:.2f} >= ${self.config.daily_cost_limit_usd:.2f}"
            )
            for callback in self._on_cost_threshold:
                try:
                    asyncio.create_task(callback(self.state.total_cost_this_session_usd))
                except Exception:
                    pass

    def get_estimated_hourly_cost(self) -> float:
        """Get estimated hourly cost of running resources."""
        total = 0.0
        for resource in self.state.resources.values():
            if resource.state == ResourceState.PROVISIONED:
                total += resource.estimated_hourly_cost_usd
        return total

    # =========================================================================
    # State Persistence
    # =========================================================================

    async def _save_state(self):
        """Save state to disk."""
        try:
            self.config.state_file.parent.mkdir(parents=True, exist_ok=True)

            state_dict = {
                "session_started_at": self.state.session_started_at,
                "total_cost_this_session_usd": self.state.total_cost_this_session_usd,
                "terraform_apply_count": self.state.terraform_apply_count,
                "terraform_destroy_count": self.state.terraform_destroy_count,
                "last_terraform_run": self.state.last_terraform_run,
                "resources": {
                    k: {
                        "name": v.name,
                        "terraform_module": v.terraform_module,
                        "state": v.state.name,
                        "provisioned_at": v.provisioned_at,
                        "destroyed_at": v.destroyed_at,
                        "we_created_it": v.we_created_it,
                    }
                    for k, v in self.state.resources.items()
                },
            }

            with open(self.config.state_file, "w") as f:
                json.dump(state_dict, f, indent=2)

        except Exception as e:
            logger.debug(f"[InfraOrchestrator] State save failed: {e}")

    async def _load_state(self):
        """Load state from disk."""
        try:
            if self.config.state_file.exists():
                with open(self.config.state_file) as f:
                    state_dict = json.load(f)

                # Only restore relevant state (not resources - they need fresh sync)
                self.state.terraform_apply_count = state_dict.get("terraform_apply_count", 0)
                self.state.terraform_destroy_count = state_dict.get("terraform_destroy_count", 0)

                logger.debug("[InfraOrchestrator] Loaded state from disk")
        except Exception as e:
            logger.debug(f"[InfraOrchestrator] State load failed: {e}")

    # =========================================================================
    # Callbacks
    # =========================================================================

    def on_resource_provisioned(self, callback: Callable):
        """Register callback for when resources are provisioned."""
        self._on_resource_provisioned.append(callback)

    def on_resource_destroyed(self, callback: Callable):
        """Register callback for when resources are destroyed."""
        self._on_resource_destroyed.append(callback)

    def on_cost_threshold(self, callback: Callable):
        """Register callback for when cost threshold is reached."""
        self._on_cost_threshold.append(callback)

    # =========================================================================
    # Status & Stats
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get current orchestrator status."""
        return {
            "initialized": self._initialized,
            "on_demand_enabled": self.config.on_demand_enabled,
            "auto_destroy_enabled": self.config.auto_destroy_on_shutdown,
            "circuit_breaker_state": self.circuit_breaker.state.name,
            "session_duration_seconds": time.time() - self.state.session_started_at,
            "total_cost_usd": self.state.total_cost_this_session_usd,
            "estimated_hourly_cost_usd": self.get_estimated_hourly_cost(),
            "terraform_operations": {
                "apply_count": self.state.terraform_apply_count,
                "destroy_count": self.state.terraform_destroy_count,
                "last_run": self.state.last_terraform_run,
            },
            "resources": {
                k: {
                    "name": v.name,
                    "state": v.state.name,
                    "we_created_it": v.we_created_it,
                    "provisioned_at": v.provisioned_at,
                }
                for k, v in self.state.resources.items()
            },
            "errors_count": len(self.state.errors),
        }


# =============================================================================
# Singleton Access
# =============================================================================

_orchestrator_instance: Optional[InfrastructureOrchestrator] = None


async def get_infrastructure_orchestrator() -> InfrastructureOrchestrator:
    """Get the global infrastructure orchestrator."""
    global _orchestrator_instance

    if _orchestrator_instance is None:
        _orchestrator_instance = InfrastructureOrchestrator()
        await _orchestrator_instance.initialize()

    return _orchestrator_instance


async def cleanup_infrastructure_on_shutdown():
    """Cleanup infrastructure on JARVIS shutdown."""
    global _orchestrator_instance

    if _orchestrator_instance:
        await _orchestrator_instance.cleanup_infrastructure()


def register_shutdown_hook():
    """Register the cleanup function to run on process exit."""
    import atexit

    def _sync_cleanup():
        """Sync wrapper for async cleanup."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(cleanup_infrastructure_on_shutdown())
            else:
                loop.run_until_complete(cleanup_infrastructure_on_shutdown())
        except Exception as e:
            logger.error(f"[InfraOrchestrator] Shutdown cleanup error: {e}")

    atexit.register(_sync_cleanup)

    # Also register for signals
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda s, f: _sync_cleanup())
        except (ValueError, OSError):
            pass  # Can't set signal handler in non-main thread
