#!/usr/bin/env python3
"""
Supervisor-Aware GCP Controller
================================

Intelligent GCP Spot VM management integrated with the JARVIS Supervisor.

Key Principles:
- VMs are expensive - only create when ABSOLUTELY necessary
- Supervisor awareness - don't create VMs during updates/maintenance
- Memory intelligence - use IntelligentMemoryController insights
- Idle shutdown - aggressively terminate unused VMs
- Cost tracking - real-time budget awareness
- No hardcoding - all configuration from env/config

This is the unified brain for GCP resource management that prevents
unnecessary spending while ensuring performance when truly needed.

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# ENUMS AND TYPES
# ============================================================================


class VMDecision(Enum):
    """Decision outcomes for VM creation requests."""
    CREATE = "create"                   # Create the VM
    DENY_BUDGET = "deny_budget"         # Over budget
    DENY_MAINTENANCE = "deny_maintenance"  # Supervisor in maintenance
    DENY_INEFFECTIVE = "deny_ineffective"  # Previous VMs didn't help
    DENY_COOLDOWN = "deny_cooldown"     # Too soon after last VM
    DENY_LOCAL_OK = "deny_local_ok"     # Local can handle it
    DENY_CHURN = "deny_churn"           # Too many VMs today
    DEFER = "defer"                     # Wait and re-evaluate


class VMLifecycleState(Enum):
    """VM lifecycle states."""
    NONE = auto()              # No VM
    REQUESTED = auto()         # Creation requested
    CREATING = auto()          # Being created
    RUNNING = auto()           # Running and active
    IDLE = auto()              # Running but idle
    TERMINATING = auto()       # Being terminated
    TERMINATED = auto()        # Successfully terminated
    FAILED = auto()            # Creation/operation failed


class SupervisorState(Enum):
    """Supervisor states relevant to GCP decisions."""
    RUNNING = "running"        # Normal operation
    UPDATING = "updating"      # System update in progress
    RESTARTING = "restarting"  # System restart
    MAINTENANCE = "maintenance"  # Maintenance mode


@dataclass
class VMCreationRequest:
    """Request to create a VM with full context."""
    timestamp: datetime
    memory_percent: float
    trigger_reason: str
    requested_by: str  # "memory_pressure", "ml_offload", "manual", etc.
    urgency: str       # "low", "medium", "high", "critical"
    components_needed: List[str] = field(default_factory=list)
    estimated_duration_minutes: int = 30
    
    # Decision tracking
    decision: Optional[VMDecision] = None
    decision_reason: str = ""
    decision_timestamp: Optional[datetime] = None


@dataclass
class ActiveVM:
    """Tracks an active VM."""
    instance_id: str
    created_at: datetime
    last_activity: datetime
    components_loaded: List[str]
    trigger_request: VMCreationRequest
    
    # Runtime stats
    requests_handled: int = 0
    memory_freed_mb: float = 0.0
    estimated_cost: float = 0.0
    
    # Idle detection
    idle_since: Optional[datetime] = None
    idle_warnings: int = 0


@dataclass
class GCPControllerConfig:
    """
    Configuration for the supervisor-aware GCP controller.
    All values from environment or config - no hardcoding.
    """
    # Budget controls
    daily_budget_limit: float = field(
        default_factory=lambda: float(os.getenv("GCP_DAILY_BUDGET", "1.00"))
    )
    weekly_budget_limit: float = field(
        default_factory=lambda: float(os.getenv("GCP_WEEKLY_BUDGET", "5.00"))
    )
    emergency_reserve_percent: float = field(
        default_factory=lambda: float(os.getenv("GCP_EMERGENCY_RESERVE", "20"))
    ) 
    
    # VM pricing
    spot_hourly_rate: float = field(
        default_factory=lambda: float(os.getenv("GCP_SPOT_HOURLY", "0.029"))
    )
    
    # Creation controls
    max_vms_per_day: int = field(
        default_factory=lambda: int(os.getenv("GCP_MAX_VMS_DAY", "5"))
    )
    min_creation_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("GCP_MIN_INTERVAL_MINS", "30"))
    )
    min_vm_lifetime_minutes: int = field(
        default_factory=lambda: int(os.getenv("GCP_MIN_VM_LIFE_MINS", "5"))
    )
    
    # Idle shutdown
    idle_warning_minutes: int = field(
        default_factory=lambda: int(os.getenv("GCP_IDLE_WARN_MINS", "10"))
    )
    idle_shutdown_minutes: int = field(
        default_factory=lambda: int(os.getenv("GCP_IDLE_SHUTDOWN_MINS", "15"))
    )
    
    # Memory thresholds (override defaults)
    min_memory_for_vm: float = field(
        default_factory=lambda: float(os.getenv("GCP_MIN_MEM_TRIGGER", "88"))
    )
    critical_memory_for_vm: float = field(
        default_factory=lambda: float(os.getenv("GCP_CRIT_MEM_TRIGGER", "95"))
    )
    
    # Effectiveness tracking
    min_effectiveness_rate: float = field(
        default_factory=lambda: float(os.getenv("GCP_MIN_EFFECTIVENESS", "0.5"))
    )
    
    def __post_init__(self):
        """Validate configuration."""
        if self.daily_budget_limit <= 0:
            logger.warning("Invalid daily budget, using default $1.00")
            self.daily_budget_limit = 1.00


# ============================================================================
# SUPERVISOR-AWARE GCP CONTROLLER
# ============================================================================


class SupervisorAwareGCPController:
    """
    Intelligent GCP Spot VM controller with supervisor integration.
    
    This controller makes sophisticated decisions about when to create VMs:
    
    1. Budget Awareness
       - Tracks daily/weekly spend
       - Reserves budget for emergencies
       - Denies non-critical requests when over budget
    
    2. Supervisor Integration
       - Blocks VM creation during updates/maintenance
       - Coordinates with supervisor state machine
       - Uses supervisor for graceful shutdown
    
    3. Memory Intelligence
       - Integrates with IntelligentMemoryController
       - Uses adaptive thresholds from baseline
       - Tracks if VMs actually helped
    
    4. Idle Management
       - Monitors VM activity
       - Issues idle warnings
       - Auto-terminates unused VMs
    
    5. Anti-Churn Protection
       - Limits VMs per day
       - Enforces minimum intervals
       - Tracks VM effectiveness
    
    Example:
        >>> controller = SupervisorAwareGCPController()
        >>> decision = await controller.request_vm(
        ...     memory_percent=92.0,
        ...     trigger="memory_pressure",
        ...     components=["ecapa", "whisper"]
        ... )
        >>> if decision == VMDecision.CREATE:
        ...     vm = await controller.create_vm()
    """
    
    def __init__(
        self,
        config: Optional[GCPControllerConfig] = None,
    ):
        """
        Initialize the supervisor-aware GCP controller.
        
        Args:
            config: Controller configuration (uses env vars if None)
        """
        self.config = config or GCPControllerConfig()
        
        # State
        self._state = VMLifecycleState.NONE
        self._state_lock = asyncio.Lock()
        
        # Active VM tracking
        self._active_vm: Optional[ActiveVM] = None
        
        # Request history
        self._request_history: List[VMCreationRequest] = []
        self._max_history = 100
        
        # Daily counters (reset at midnight)
        self._today = datetime.now().strftime("%Y-%m-%d")
        self._vms_created_today = 0
        self._spend_today = 0.0
        self._last_vm_created: Optional[datetime] = None
        self._last_vm_terminated: Optional[datetime] = None
        
        # Effectiveness tracking
        self._effective_vms = 0
        self._ineffective_vms = 0
        
        # Supervisor state
        self._supervisor_state = SupervisorState.RUNNING
        
        # Idle monitoring task
        self._idle_monitor_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        
        # Callbacks
        self._on_decision: List[Callable[[VMCreationRequest], None]] = []
        self._on_vm_created: List[Callable[[ActiveVM], None]] = []
        self._on_vm_terminated: List[Callable[[ActiveVM], None]] = []

        # v3.1: Stall recovery tracking
        self._stall_count: int = 0
        self._max_stall_retries: int = int(
            os.environ.get("JARVIS_GCP_MAX_STALL_RETRIES", "2")
        )
        self._last_stall_time: Optional[datetime] = None
        self._gcp_marked_unavailable: bool = False
        
        logger.info("🎮 Supervisor-Aware GCP Controller initialized")
        logger.info(f"   Budget: ${self.config.daily_budget_limit:.2f}/day")
        logger.info(f"   Max VMs: {self.config.max_vms_per_day}/day")
        logger.info(f"   Memory trigger: {self.config.min_memory_for_vm}%")
        logger.info(f"   Idle shutdown: {self.config.idle_shutdown_minutes} min")
    
    # =========================================================================
    # SUPERVISOR INTEGRATION
    # =========================================================================
    
    def set_supervisor_state(self, state: SupervisorState) -> None:
        """
        Update supervisor state (called by supervisor).
        
        Args:
            state: Current supervisor state
        """
        old_state = self._supervisor_state
        self._supervisor_state = state
        
        if old_state != state:
            logger.info(f"🎮 Supervisor state: {old_state.value} → {state.value}")
            
            # If entering maintenance, don't create new VMs
            if state in (SupervisorState.UPDATING, SupervisorState.MAINTENANCE):
                logger.info("🎮 VM creation blocked during maintenance")
    
    def is_maintenance_mode(self) -> bool:
        """Check if supervisor is in maintenance mode."""
        return self._supervisor_state in (
            SupervisorState.UPDATING,
            SupervisorState.RESTARTING,
            SupervisorState.MAINTENANCE,
        )
    
    # =========================================================================
    # BUDGET MANAGEMENT
    # =========================================================================
    
    def _reset_daily_counters_if_needed(self) -> None:
        """Reset daily counters at midnight."""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._today:
            logger.info(f"📅 New day - resetting counters (yesterday: {self._vms_created_today} VMs, ${self._spend_today:.2f})")
            self._today = today
            self._vms_created_today = 0
            self._spend_today = 0.0
    
    def get_remaining_budget(self) -> float:
        """Get remaining daily budget."""
        self._reset_daily_counters_if_needed()
        return max(0, self.config.daily_budget_limit - self._spend_today)
    
    def get_emergency_reserve(self) -> float:
        """Get emergency budget reserve."""
        return self.config.daily_budget_limit * (self.config.emergency_reserve_percent / 100)
    
    def can_afford_vm(self, estimated_hours: float = 1.0, is_emergency: bool = False) -> Tuple[bool, str]:
        """
        Check if we can afford a VM.
        
        Args:
            estimated_hours: Estimated VM runtime
            is_emergency: If True, can use emergency reserve
            
        Returns:
            Tuple of (can_afford, reason)
        """
        self._reset_daily_counters_if_needed()
        
        estimated_cost = estimated_hours * self.config.spot_hourly_rate
        remaining = self.get_remaining_budget()
        reserve = self.get_emergency_reserve()
        
        # Emergency requests can use reserve
        if is_emergency:
            available = remaining
        else:
            available = remaining - reserve
        
        if estimated_cost > available:
            return False, f"Budget exceeded (need ${estimated_cost:.2f}, have ${available:.2f})"
        
        return True, f"Within budget (${available:.2f} available)"
    
    def record_vm_cost(self, runtime_hours: float) -> None:
        """Record VM cost for the day."""
        self._reset_daily_counters_if_needed()
        cost = runtime_hours * self.config.spot_hourly_rate
        self._spend_today += cost
        logger.info(f"💰 Recorded cost: ${cost:.3f} (today: ${self._spend_today:.2f})")
    
    # =========================================================================
    # MEMORY INTEGRATION
    # =========================================================================
    
    def _get_memory_controller(self):
        """Get the intelligent memory controller (lazy import)."""
        try:
            from process_cleanup_manager import get_memory_controller
            return get_memory_controller()
        except ImportError:
            return None
    
    def _should_create_based_on_memory(
        self,
        memory_percent: float,
    ) -> Tuple[bool, str]:
        """
        Decide if VM should be created based on memory intelligence.
        
        Uses IntelligentMemoryController insights if available.
        
        Returns:
            Tuple of (should_create, reason)
        """
        controller = self._get_memory_controller()
        
        if controller:
            # Use controller's adaptive thresholds
            stats = controller.get_stats()
            
            # If memory controller is in backoff, local cleanup isn't helping
            # This is actually a good signal FOR VM creation
            if stats.get("state") == "BACKOFF":
                if memory_percent >= self.config.min_memory_for_vm:
                    return True, "Memory cleanup ineffective (backoff mode) + high pressure"
            
            # Check effectiveness of recent relief attempts
            success_rate = stats.get("recent_success_rate", 1.0)
            if success_rate < 0.3 and memory_percent >= self.config.min_memory_for_vm:
                return True, f"Local cleanup failing ({success_rate:.0%} success) + high pressure"
            
            # If cleanup is working, don't need VM
            if success_rate > 0.7:
                return False, f"Local cleanup effective ({success_rate:.0%} success)"
        
        # Fallback to simple thresholds
        if memory_percent >= self.config.critical_memory_for_vm:
            return True, f"Critical memory pressure ({memory_percent:.1f}%)"
        elif memory_percent >= self.config.min_memory_for_vm:
            return True, f"High memory pressure ({memory_percent:.1f}%)"
        else:
            return False, f"Memory acceptable ({memory_percent:.1f}%)"
    
    # =========================================================================
    # VM DECISION LOGIC
    # =========================================================================
    
    async def request_vm(
        self,
        memory_percent: float,
        trigger: str,
        urgency: str = "medium",
        components: Optional[List[str]] = None,
        estimated_duration_minutes: int = 30,
    ) -> Tuple[VMDecision, str]:
        """
        Request a VM with full decision logic.
        
        This is the main entry point for VM creation requests.
        It evaluates multiple factors before deciding.
        
        Args:
            memory_percent: Current memory usage
            trigger: What triggered this request
            urgency: "low", "medium", "high", "critical"
            components: Components to load on VM
            estimated_duration_minutes: How long VM is needed
            
        Returns:
            Tuple of (decision, reason)
        """
        async with self._state_lock:
            self._reset_daily_counters_if_needed()
            
            # Create request record
            request = VMCreationRequest(
                timestamp=datetime.now(),
                memory_percent=memory_percent,
                trigger_reason=trigger,
                requested_by=trigger,
                urgency=urgency,
                components_needed=components or [],
                estimated_duration_minutes=estimated_duration_minutes,
            )
            
            # Check 1: Already have a VM?
            if self._active_vm is not None:
                decision = VMDecision.DENY_LOCAL_OK
                reason = "VM already running"
                request.decision = decision
                request.decision_reason = reason
                request.decision_timestamp = datetime.now()
                self._record_request(request)
                return decision, reason
            
            # Check 2: Supervisor in maintenance?
            if self.is_maintenance_mode():
                decision = VMDecision.DENY_MAINTENANCE
                reason = f"Supervisor in {self._supervisor_state.value} mode"
                request.decision = decision
                request.decision_reason = reason
                request.decision_timestamp = datetime.now()
                self._record_request(request)
                return decision, reason
            
            # Check 3: Over VM limit for the day?
            if self._vms_created_today >= self.config.max_vms_per_day:
                # Allow critical requests to exceed limit
                if urgency != "critical":
                    decision = VMDecision.DENY_CHURN
                    reason = f"VM limit reached ({self._vms_created_today}/{self.config.max_vms_per_day} today)"
                    request.decision = decision
                    request.decision_reason = reason
                    request.decision_timestamp = datetime.now()
                    self._record_request(request)
                    return decision, reason
            
            # Check 4: Too soon after last VM?
            if self._last_vm_created:
                elapsed = (datetime.now() - self._last_vm_created).total_seconds() / 60
                if elapsed < self.config.min_creation_interval_minutes:
                    if urgency not in ("high", "critical"):
                        decision = VMDecision.DENY_COOLDOWN
                        remaining = self.config.min_creation_interval_minutes - elapsed
                        reason = f"Cooldown ({remaining:.0f} min remaining)"
                        request.decision = decision
                        request.decision_reason = reason
                        request.decision_timestamp = datetime.now()
                        self._record_request(request)
                        return decision, reason
            
            # Check 5: Budget check
            estimated_hours = estimated_duration_minutes / 60
            is_emergency = urgency == "critical"
            can_afford, budget_reason = self.can_afford_vm(estimated_hours, is_emergency)
            if not can_afford:
                decision = VMDecision.DENY_BUDGET
                request.decision = decision
                request.decision_reason = budget_reason
                request.decision_timestamp = datetime.now()
                self._record_request(request)
                return decision, budget_reason
            
            # Check 6: Effectiveness check
            total_vms = self._effective_vms + self._ineffective_vms
            if total_vms >= 3:  # Have enough data
                effectiveness = self._effective_vms / total_vms
                if effectiveness < self.config.min_effectiveness_rate:
                    if urgency not in ("high", "critical"):
                        decision = VMDecision.DENY_INEFFECTIVE
                        reason = f"Recent VMs not effective ({effectiveness:.0%} success)"
                        request.decision = decision
                        request.decision_reason = reason
                        request.decision_timestamp = datetime.now()
                        self._record_request(request)
                        return decision, reason
            
            # Check 7: Memory-based decision
            should_create, memory_reason = self._should_create_based_on_memory(memory_percent)
            if not should_create and urgency not in ("high", "critical"):
                decision = VMDecision.DENY_LOCAL_OK
                request.decision = decision
                request.decision_reason = memory_reason
                request.decision_timestamp = datetime.now()
                self._record_request(request)
                return decision, memory_reason
            
            # All checks passed - approve creation
            decision = VMDecision.CREATE
            reason = f"Approved: {memory_reason}"
            request.decision = decision
            request.decision_reason = reason
            request.decision_timestamp = datetime.now()
            self._record_request(request)
            
            # Notify callbacks
            for callback in self._on_decision:
                try:
                    callback(request)
                except Exception as e:
                    logger.error(f"Decision callback error: {e}")
            
            return decision, reason
    
    def _record_request(self, request: VMCreationRequest) -> None:
        """Record request in history."""
        self._request_history.append(request)
        if len(self._request_history) > self._max_history:
            self._request_history.pop(0)
        
        # Log decision
        emoji = "✅" if request.decision == VMDecision.CREATE else "❌"
        decision_value = request.decision.value if request.decision else "unknown"
        logger.info(
            f"{emoji} VM request: {decision_value} - {request.decision_reason} "
            f"(trigger={request.trigger_reason}, urgency={request.urgency})"
        )
    
    # =========================================================================
    # VM LIFECYCLE
    # =========================================================================
    
    async def create_vm(
        self,
        request: Optional[VMCreationRequest] = None,
    ) -> Optional[ActiveVM]:
        """
        Create a GCP Spot VM.
        
        This should only be called after request_vm returns CREATE.
        
        Args:
            request: The approved request
            
        Returns:
            ActiveVM if successful, None if failed
        """
        async with self._state_lock:
            if self._active_vm is not None:
                # v137.2: Reuse existing VM instead of failing
                # This allows emergency offload to work when VM is already running
                logger.info(f"[v137.2] ✅ Reusing existing active VM: {self._active_vm.instance_id}")
                return self._active_vm
            
            self._state = VMLifecycleState.CREATING
        
        try:
            # Import the actual VM manager
            try:
                from backend.core.gcp_vm_manager import get_gcp_vm_manager_safe
            except ImportError:
                from core.gcp_vm_manager import get_gcp_vm_manager_safe

            vm_manager = await get_gcp_vm_manager_safe()
            if not vm_manager:
                logger.error("GCP VM Manager not available")
                return None
            
            # Create the VM
            components = request.components_needed if request else ["ecapa_tdnn"]
            vm = await vm_manager.create_vm(
                components=components,
                trigger_reason=request.trigger_reason if request else "manual",
            )
            
            if not vm:
                async with self._state_lock:
                    self._state = VMLifecycleState.FAILED
                return None
            
            # Track the VM
            now = datetime.now()
            active_vm = ActiveVM(
                instance_id=vm.instance_id,  # v132.1: VMInstance is a dataclass, not dict
                created_at=now,
                last_activity=now,
                components_loaded=components,
                trigger_request=request or VMCreationRequest(
                    timestamp=now,
                    memory_percent=0,
                    trigger_reason="manual",
                    requested_by="manual",
                    urgency="medium",
                ),
            )
            
            async with self._state_lock:
                self._active_vm = active_vm
                self._state = VMLifecycleState.RUNNING
                self._vms_created_today += 1
                self._last_vm_created = now
            
            logger.info(f"🚀 VM created: {active_vm.instance_id}")
            
            # Start idle monitoring
            if self._idle_monitor_task is None or self._idle_monitor_task.done():
                self._idle_monitor_task = asyncio.create_task(self._idle_monitor_loop())
            
            # Notify callbacks
            for callback in self._on_vm_created:
                try:
                    callback(active_vm)
                except Exception as e:
                    logger.error(f"VM created callback error: {e}")
            
            return active_vm
            
        except Exception as e:
            logger.error(f"VM creation failed: {e}")
            async with self._state_lock:
                self._state = VMLifecycleState.FAILED
            return None
    
    async def terminate_vm(
        self,
        reason: str = "manual",
        was_effective: bool = True,
    ) -> bool:
        """
        Terminate the active VM.
        
        Args:
            reason: Why terminating
            was_effective: Did the VM help?
            
        Returns:
            True if terminated successfully
        """
        async with self._state_lock:
            if self._active_vm is None:
                return True
            
            vm = self._active_vm
            self._state = VMLifecycleState.TERMINATING
        
        try:
            # Import the actual VM manager
            try:
                from backend.core.gcp_vm_manager import get_gcp_vm_manager_safe
            except ImportError:
                from core.gcp_vm_manager import get_gcp_vm_manager_safe

            vm_manager = await get_gcp_vm_manager_safe()
            if vm_manager:
                await vm_manager.terminate_vm(vm.instance_id)
            
            # Calculate and record cost
            runtime_hours = (datetime.now() - vm.created_at).total_seconds() / 3600
            self.record_vm_cost(runtime_hours)
            
            async with self._state_lock:
                # Track effectiveness
                if was_effective:
                    self._effective_vms += 1
                else:
                    self._ineffective_vms += 1
                
                self._active_vm = None
                self._state = VMLifecycleState.TERMINATED
                self._last_vm_terminated = datetime.now()
            
            logger.info(
                f"🛑 VM terminated: {vm.instance_id} "
                f"(runtime: {runtime_hours:.2f}h, effective: {was_effective}, reason: {reason})"
            )
            
            # Notify callbacks
            for callback in self._on_vm_terminated:
                try:
                    callback(vm)
                except Exception as e:
                    logger.error(f"VM terminated callback error: {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"VM termination failed: {e}")
            async with self._state_lock:
                self._state = VMLifecycleState.FAILED
            return False
    
    # =========================================================================
    # IDLE MONITORING
    # =========================================================================
    
    def record_vm_activity(self) -> None:
        """Record that VM was just used."""
        if self._active_vm:
            self._active_vm.last_activity = datetime.now()
            self._active_vm.requests_handled += 1
            self._active_vm.idle_since = None
            self._active_vm.idle_warnings = 0
    
    async def _idle_monitor_loop(self) -> None:
        """Background loop to monitor VM idle state."""
        logger.info("🔍 Idle monitor started")
        
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(60)  # Check every minute
                
                if self._active_vm is None:
                    break
                
                vm = self._active_vm
                idle_seconds = (datetime.now() - vm.last_activity).total_seconds()
                idle_minutes = idle_seconds / 60
                
                # Check for idle warning threshold
                if idle_minutes >= self.config.idle_warning_minutes:
                    if vm.idle_since is None:
                        vm.idle_since = datetime.now()
                    
                    vm.idle_warnings += 1
                    
                    # Check for shutdown threshold
                    if idle_minutes >= self.config.idle_shutdown_minutes:
                        logger.warning(
                            f"⚠️ VM {vm.instance_id} idle for {idle_minutes:.0f} min - terminating"
                        )
                        await self.terminate_vm(
                            reason=f"idle_{idle_minutes:.0f}min",
                            was_effective=vm.requests_handled > 0,
                        )
                        break
                    else:
                        remaining = self.config.idle_shutdown_minutes - idle_minutes
                        logger.info(
                            f"⏰ VM {vm.instance_id} idle for {idle_minutes:.0f} min "
                            f"({remaining:.0f} min until shutdown)"
                        )
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Idle monitor error: {e}")
        
        logger.info("🔍 Idle monitor stopped")
    
    # =========================================================================
    # STATS AND MONITORING
    # =========================================================================
    
    # =========================================================================
    # STALL RECOVERY TRACKING (v3.1)
    # =========================================================================

    def record_stall(self) -> bool:
        """
        Record a VM stall event. Returns True if retries remain.

        Called by the supervisor when stall detection fires. Tracks how many
        times a VM has stalled so the system knows when to give up.
        """
        self._stall_count += 1
        self._last_stall_time = datetime.now()
        remaining = self._max_stall_retries - self._stall_count
        logger.warning(
            f"[GCP Controller] Stall #{self._stall_count} recorded. "
            f"{remaining} retries remaining."
        )
        return remaining > 0

    def mark_gcp_unavailable(self, reason: str) -> None:
        """Mark GCP as unavailable after exhausting recovery retries."""
        self._gcp_marked_unavailable = True
        logger.error(f"[GCP Controller] GCP marked UNAVAILABLE: {reason}")

    def is_gcp_available(self) -> bool:
        """Check if GCP is currently available for VM operations."""
        return not self._gcp_marked_unavailable

    def reset_stall_tracking(self) -> None:
        """Reset stall tracking after successful VM recovery."""
        if self._stall_count > 0:
            logger.info(
                f"[GCP Controller] Stall tracking reset "
                f"(was at {self._stall_count} stalls)"
            )
        self._stall_count = 0
        self._last_stall_time = None
        self._gcp_marked_unavailable = False

    def get_stats(self) -> Dict[str, Any]:
        """Get controller statistics."""
        self._reset_daily_counters_if_needed()
        
        total_vms = self._effective_vms + self._ineffective_vms
        effectiveness = self._effective_vms / total_vms if total_vms > 0 else 1.0
        
        return {
            "state": self._state.name,
            "supervisor_state": self._supervisor_state.value,
            "has_active_vm": self._active_vm is not None,
            "active_vm_id": self._active_vm.instance_id if self._active_vm else None,
            "vms_created_today": self._vms_created_today,
            "max_vms_per_day": self.config.max_vms_per_day,
            "spend_today": round(self._spend_today, 3),
            "budget_today": self.config.daily_budget_limit,
            "budget_remaining": round(self.get_remaining_budget(), 3),
            "effectiveness": {
                "effective_vms": self._effective_vms,
                "ineffective_vms": self._ineffective_vms,
                "rate": round(effectiveness, 2),
            },
            "last_vm_created": self._last_vm_created.isoformat() if self._last_vm_created else None,
            "last_vm_terminated": self._last_vm_terminated.isoformat() if self._last_vm_terminated else None,
            "request_history_count": len(self._request_history),
            # v3.1: Stall recovery stats
            "stall_count": self._stall_count,
            "gcp_available": not self._gcp_marked_unavailable,
            "last_stall_time": self._last_stall_time.isoformat() if self._last_stall_time else None,
        }
    
    def get_recent_decisions(self, count: int = 10) -> List[Dict[str, Any]]:
        """Get recent VM decisions."""
        recent = self._request_history[-count:]
        return [
            {
                "timestamp": r.timestamp.isoformat(),
                "memory_percent": r.memory_percent,
                "trigger": r.trigger_reason,
                "urgency": r.urgency,
                "decision": r.decision.value if r.decision else None,
                "reason": r.decision_reason,
            }
            for r in recent
        ]
    
    # =========================================================================
    # LIFECYCLE
    # =========================================================================
    
    async def start(self) -> None:
        """Start the controller."""
        logger.info("🎮 Supervisor-Aware GCP Controller started")
    
    async def stop(self) -> None:
        """Stop the controller and cleanup."""
        self._shutdown_event.set()
        
        if self._idle_monitor_task and not self._idle_monitor_task.done():
            self._idle_monitor_task.cancel()
            try:
                await self._idle_monitor_task
            except asyncio.CancelledError:
                pass
        
        # Terminate any active VM
        if self._active_vm:
            await self.terminate_vm(reason="controller_shutdown")
        
        logger.info("🎮 Supervisor-Aware GCP Controller stopped")


# ============================================================================
# MODULE-LEVEL SINGLETON
# ============================================================================


_controller: Optional[SupervisorAwareGCPController] = None


def get_supervisor_gcp_controller() -> SupervisorAwareGCPController:
    """Get or create the supervisor-aware GCP controller singleton."""
    global _controller
    if _controller is None:
        _controller = SupervisorAwareGCPController()
    return _controller


def reset_supervisor_gcp_controller() -> None:
    """
    v270.2: Reset singleton for clean in-process restart.

    Without this, stale budget counters, effectiveness rates, stall
    tracking, and VM lifecycle state from a previous run bleed into
    the next startup — identical root cause to the startup_state_machine
    persistence bug fixed in v266.1.

    Must be called during shutdown BEFORE the next startup cycle begins.
    """
    global _controller
    _controller = None


async def request_vm(
    memory_percent: float,
    trigger: str,
    urgency: str = "medium",
    components: Optional[List[str]] = None,
) -> Tuple[VMDecision, str]:
    """
    Convenience function to request a VM.
    
    Example:
        >>> decision, reason = await request_vm(
        ...     memory_percent=92.0,
        ...     trigger="memory_pressure",
        ...     components=["ecapa", "whisper"]
        ... )
    """
    controller = get_supervisor_gcp_controller()
    return await controller.request_vm(
        memory_percent=memory_percent,
        trigger=trigger,
        urgency=urgency,
        components=components,
    )

