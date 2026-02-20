"""
JARVIS Parallel Initializer v2.0.0
==================================

Runs ALL heavy initialization as background tasks AFTER uvicorn starts serving.

Key Features:
- Server starts serving health endpoint IMMEDIATELY
- ML models, databases, neural mesh load in background
- Progress is tracked and reported via /health endpoint
- Graceful degradation if components fail
- v2.0: Circuit breakers for stale component detection
- v2.0: Progressive readiness (interactive_ready before full_mode)
- v2.0: Watchdog for hung component detection
- v2.0: Reduced timeouts for faster startup

Usage in main.py lifespan:
    from core.parallel_initializer import ParallelInitializer

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initializer = ParallelInitializer(app)

        # Minimal setup - server ready in <1s
        await initializer.minimal_setup()

        # Server starts serving NOW (yield)
        yield

        # Heavy init runs in background
        # Shutdown handled here
        await initializer.shutdown()
"""

import asyncio
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Trinity Unified Event Loop Manager - shared infrastructure across repos
try:
    # Add cross_repo to path for shared modules
    _cross_repo_path = Path.home() / ".jarvis" / "cross_repo"
    if str(_cross_repo_path) not in sys.path:
        sys.path.insert(0, str(_cross_repo_path))

    from unified_loop_manager import (
        get_trinity_manager,
        safe_create_task,
        safe_to_thread,
        safe_gather,
        TrinityComponent,
        TrinityConfig,
    )
    TRINITY_AVAILABLE = True
except ImportError as e:
    TRINITY_AVAILABLE = False
    # Fallbacks if Trinity not available
    def safe_create_task(coro, *, name=None):
        return asyncio.create_task(coro, name=name)
    async def safe_to_thread(func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    async def safe_gather(*coros, return_exceptions=False):
        return await asyncio.gather(*coros, return_exceptions=return_exceptions)
    logging.getLogger(__name__).debug(f"Trinity not available: {e}")

# Python 3.9 compatible lock - lazily initializes asyncio.Lock
try:
    from backend.utils.python39_compat import AsyncLock
except ImportError:
    try:
        from utils.python39_compat import AsyncLock
    except ImportError:
        # Fallback: Define inline if import fails
        class AsyncLock:
            """Python 3.9-safe lock that lazily creates asyncio.Lock."""
            def __init__(self):
                self._thread_lock = threading.RLock()
                self._async_lock: Optional[asyncio.Lock] = None

            def _get_async_lock(self) -> asyncio.Lock:
                if self._async_lock is None:
                    try:
                        self._async_lock = asyncio.Lock()
                    except RuntimeError:
                        pass
                return self._async_lock

            async def __aenter__(self):
                async_lock = self._get_async_lock()
                if async_lock:
                    await async_lock.acquire()
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                async_lock = self._get_async_lock()
                if async_lock and async_lock.locked():
                    async_lock.release()
                return False

# Import the startup progress broadcaster for real-time WebSocket updates
from core.startup_progress_broadcaster import get_startup_broadcaster

# Import Hyper-Speed AI Loader for Ghost Proxy model loading
try:
    from core.ai_loader import (
        get_ai_manager,
        get_optimization_router,
        ModelPriority,
        ModelStatus,
        OptimizationEngine,
        ModelCategory,
    )
    AI_LOADER_AVAILABLE = True
except ImportError as e:
    AI_LOADER_AVAILABLE = False
    logging.getLogger(__name__).warning(f"AI Loader not available: {e}")

# Import TaskLifecycleManager for proper task tracking
try:
    from core.task_lifecycle_manager import (
        get_task_manager,
        TaskPriority,
        is_shutting_down,
    )
    TASK_MANAGER_AVAILABLE = True
except ImportError:
    TASK_MANAGER_AVAILABLE = False

# v131.0: Import GCP OOM Prevention Bridge for pre-flight memory checks
# v132.0: Added DegradationTier for graceful degradation support
OOM_PREVENTION_AVAILABLE = False
_DegradationTierType = None  # Placeholder for type (will be set by import)
try:
    from core.gcp_oom_prevention_bridge import (
        check_memory_before_heavy_init,
        MemoryDecision,
        DegradationTier,  # v132.0
        HEAVY_COMPONENT_MEMORY_ESTIMATES,
    )
    OOM_PREVENTION_AVAILABLE = True
    _DegradationTierType = DegradationTier  # Store for type checking
except ImportError:
    DegradationTier = None  # Fallback placeholder
    logging.getLogger(__name__).debug("OOM Prevention Bridge not available")

logger = logging.getLogger(__name__)


class InitPhase(Enum):
    """Initialization phases"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ComponentInit:
    """Tracks a single component initialization with circuit breaker support"""
    name: str
    phase: InitPhase = InitPhase.PENDING
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    error: Optional[str] = None
    priority: int = 50  # 0-100, lower = earlier
    dependencies: List[str] = field(default_factory=list)
    is_critical: bool = False
    # v2.0: Circuit breaker fields
    is_interactive: bool = False  # True for components needed for user interaction
    stale_threshold_seconds: float = 30.0  # Mark as stale after this duration in RUNNING
    _stale_warned: bool = field(default=False, repr=False)  # v258.1: watchdog early warning flag

    @property
    def duration_ms(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time) * 1000
        return None

    @property
    def is_stale(self) -> bool:
        """Check if component is stuck in RUNNING state too long"""
        if self.phase != InitPhase.RUNNING or self.start_time is None:
            return False
        elapsed = time.time() - self.start_time
        return elapsed > self.stale_threshold_seconds

    @property
    def running_seconds(self) -> Optional[float]:
        """Get how long component has been running"""
        if self.phase == InitPhase.RUNNING and self.start_time:
            return time.time() - self.start_time
        return None


class ParallelInitializer:
    """
    Manages parallel background initialization of JARVIS components.

    The key insight is that uvicorn should start serving immediately,
    and all heavy initialization should run in background tasks.

    v2.0 Enhancements:
    - Progressive readiness: interactive_ready fires when user can interact
    - Stale component detection: watchdog marks hung components as failed
    - Circuit breakers: prevent cascade failures from slow components
    - Reduced timeouts: faster startup with graceful degradation
    """

    def __init__(self, app):
        self.app = app
        self.components: Dict[str, ComponentInit] = {}
        self.started_at: Optional[float] = None
        self.background_task: Optional[asyncio.Task] = None
        self._lock = AsyncLock()  # Python 3.9 compatible
        # Note: asyncio.Event() can be created outside event loop in Python 3.9
        # but we'll create them lazily for safety
        self._ready_event: Optional[asyncio.Event] = None
        self._full_mode_event: Optional[asyncio.Event] = None
        self._shutdown_event: Optional[asyncio.Event] = None
        # v2.0: Interactive readiness - fires when user can interact (WebSocket + Voice API)
        self._interactive_ready_event: Optional[asyncio.Event] = None
        # v2.0: Watchdog task for detecting stale components
        self._watchdog_task: Optional[asyncio.Task] = None
        # v2.0: Track if interactive mode was announced
        self._interactive_announced = False

        # Store references for cleanup
        self._tasks: List[asyncio.Task] = []

        # Register standard components
        self._register_components()

    def _get_ready_event(self) -> asyncio.Event:
        """Lazily create the ready event (Python 3.9 safe)."""
        if self._ready_event is None:
            self._ready_event = asyncio.Event()
        return self._ready_event

    def _get_full_mode_event(self) -> asyncio.Event:
        """Lazily create the full mode event (Python 3.9 safe)."""
        if self._full_mode_event is None:
            self._full_mode_event = asyncio.Event()
        return self._full_mode_event

    def _get_shutdown_event(self) -> asyncio.Event:
        """Lazily create the shutdown event (Python 3.9 safe)."""
        if self._shutdown_event is None:
            self._shutdown_event = asyncio.Event()
        return self._shutdown_event

    def _get_interactive_ready_event(self) -> asyncio.Event:
        """Lazily create the interactive ready event (Python 3.9 safe)."""
        if self._interactive_ready_event is None:
            self._interactive_ready_event = asyncio.Event()
        return self._interactive_ready_event

    def _register_components(self):
        """
        Register all JARVIS components with priorities and circuit breaker settings.

        v2.0 Enhancements:
        - is_interactive: Components needed for user interaction (faster stale threshold)
        - stale_threshold: How long before marking as stale (default 30s)
        - Reduced timeouts for non-critical components
        """
        # Cost-safe gating for cloud/spot features (explicit opt-in; no hardcoded cloud usage)
        enable_spot_vm = (
            os.getenv("JARVIS_SPOT_VM_ENABLED", "false").lower() == "true"
            or os.getenv("GCP_VM_ENABLED", "false").lower() == "true"
        )
        enable_cloud_ml = bool(
            (os.getenv("JARVIS_CLOUD_ML_ENDPOINT", "").strip())
            or (os.getenv("JARVIS_CLOUD_ML_ENDPOINTS", "").strip())
        )
        enable_vbi_prewarm = os.getenv("JARVIS_VBI_PREWARM_ENABLED", "false").lower() == "true"

        # Phase 1: Critical infrastructure (truly parallel - no dependencies!)
        # Config is instant, don't need circuit breaker
        self._add_component("config", priority=1, is_critical=True, stale_threshold=5.0)

        # Phase 1.5: Hyper-Speed AI Loader (initializes BEFORE any ML models)
        # This enables Ghost Proxy pattern for instant startup
        if AI_LOADER_AVAILABLE:
            self._add_component("ai_loader", priority=5, is_critical=False, stale_threshold=10.0)
            # Optimized voice models use Ghost Proxies - instant registration
            self._add_component("optimized_voice_models", priority=6, is_interactive=True, stale_threshold=5.0)
            # Optimized vision system uses Ghost Proxies - instant registration
            self._add_component("optimized_vision_system", priority=7, stale_threshold=5.0)
            # Optimized intelligence/neural mesh uses Ghost Proxies
            self._add_component("optimized_intelligence", priority=8, stale_threshold=5.0)

        # Cloud SQL proxy - give it more time but not critical for interactive use
        # v86.0: Now uses ProxyReadinessGate for DB-level verification
        self._add_component("cloud_sql_proxy", priority=10, stale_threshold=45.0)
        # Learning DB depends on cloud_sql_proxy for DB-level readiness
        # v86.0: Explicit dependency ensures learning_db waits for DB-level gate
        self._add_component("learning_database", priority=12, stale_threshold=40.0, dependencies=["cloud_sql_proxy"])

        # Phase 2: ML Infrastructure (parallel, non-blocking)
        # All these can start simultaneously
        self._add_component("memory_aware_startup", priority=20, stale_threshold=15.0)
        if enable_spot_vm:
            self._add_component("gcp_vm_manager", priority=20, stale_threshold=30.0)
        self._add_component("cloud_ml_router", priority=20, stale_threshold=20.0)
        if enable_cloud_ml or enable_spot_vm:
            self._add_component("cloud_ecapa_client", priority=20, stale_threshold=90.0)
        # VBI runs in background - doesn't block anything
        if enable_vbi_prewarm:
            self._add_component("vbi_prewarm", priority=21, stale_threshold=45.0)
        self._add_component("vbi_health_monitor", priority=21, stale_threshold=20.0)
        self._add_component("ml_engine_registry", priority=22, stale_threshold=30.0)

        # Phase 3: Voice System (parallel - INTERACTIVE components!)
        # These are needed for user interaction - mark as interactive with faster thresholds
        self._add_component("speaker_verification", priority=30, stale_threshold=40.0)
        # Voice unlock API is INTERACTIVE - needed for unlock commands
        self._add_component("voice_unlock_api", priority=30, is_interactive=True, stale_threshold=20.0)
        # JARVIS voice API is INTERACTIVE - needed for voice commands
        self._add_component("jarvis_voice_api", priority=30, is_interactive=True, stale_threshold=20.0)
        # WebSocket is CRITICAL for interactive use - fastest threshold
        self._add_component("unified_websocket", priority=30, is_interactive=True, stale_threshold=15.0)

        # Phase 4: Intelligence Systems (parallel, can be slow but non-blocking)
        # These are NOT interactive - can complete in background after startup
        self._add_component("neural_mesh", priority=40, stale_threshold=60.0)
        self._add_component("goal_inference", priority=40, stale_threshold=30.0)
        self._add_component("uae_engine", priority=40, stale_threshold=30.0)
        self._add_component("hybrid_orchestrator", priority=40, stale_threshold=30.0)
        self._add_component("vision_analyzer", priority=40, stale_threshold=30.0)
        self._add_component("display_monitor", priority=40, stale_threshold=20.0)

        # Phase 5: Supporting services (parallel)
        self._add_component("dynamic_components", priority=50, stale_threshold=30.0)

        # Phase 6: Agentic System (soft dependencies - will work even if deps not ready)
        # Agentic system is heavy - give it more time but skip if too slow
        self._add_component("agentic_system", priority=55, stale_threshold=60.0)

    def _add_component(
        self,
        name: str,
        priority: int = 50,
        is_critical: bool = False,
        dependencies: List[str] = None,
        is_interactive: bool = False,
        stale_threshold: float = 30.0
    ):
        """Add a component to track with circuit breaker support"""
        # v258.1: Allow per-component stale threshold override via env var
        # e.g. JARVIS_STALE_THRESHOLD_CLOUD_ECAPA_CLIENT=90
        env_key = f"JARVIS_STALE_THRESHOLD_{name.upper()}"
        env_val = os.environ.get(env_key)
        if env_val:
            try:
                stale_threshold = float(env_val)
            except (ValueError, TypeError):
                pass

        self.components[name] = ComponentInit(
            name=name,
            priority=priority,
            is_critical=is_critical,
            dependencies=dependencies or [],
            is_interactive=is_interactive,
            stale_threshold_seconds=stale_threshold
        )

    async def minimal_setup(self):
        """
        Minimal setup that runs BEFORE yield.
        This should complete in <1 second.

        v2.0: Also starts the stale component watchdog.
        """
        self.started_at = time.time()
        logger.info("=" * 60)
        logger.info("JARVIS Parallel Startup v2.0.0")
        logger.info("=" * 60)

        # ============== TRINITY INTEGRATION (v3.0) ==============
        # Initialize Trinity Unified Loop Manager FIRST
        # This ensures proper event loop management across all components
        if TRINITY_AVAILABLE:
            try:
                trinity_manager = get_trinity_manager()
                await trinity_manager.initialize(TrinityComponent.JARVIS_BODY)
                self.app.state.trinity_manager = trinity_manager
                logger.info("✅ Trinity Unified Loop Manager initialized")
            except Exception as e:
                logger.warning(f"Trinity initialization failed (non-fatal): {e}")
                self.app.state.trinity_manager = None
        else:
            self.app.state.trinity_manager = None
            logger.debug("Trinity not available, using standard asyncio")
        # =========================================================

        # Initialize app state FIRST before marking any components
        self.app.state.parallel_initializer = self
        self.app.state.startup_phase = "STARTING"
        self.app.state.startup_progress = 0.0
        self.app.state.components_ready = set()
        self.app.state.components_failed = set()
        # v2.0: Track interactive readiness
        self.app.state.interactive_ready = False

        # Mark config as complete (it's just loading env vars)
        await self._mark_complete("config")

        # Server is ready for basic health checks
        self._get_ready_event().set()
        logger.info("Server ready for health checks")

        # Launch background initialization (using safe_create_task for Trinity integration)
        self.background_task = safe_create_task(
            self._background_initialization(),
            name="parallel_init"
        )
        self._tasks.append(self.background_task)

        # v2.0: Start watchdog for stale component detection
        self._watchdog_task = safe_create_task(
            self._stale_component_watchdog(),
            name="stale_watchdog"
        )
        self._tasks.append(self._watchdog_task)

    async def _background_initialization(self):
        """
        Background task that initializes all heavy components.
        This runs AFTER the server starts serving requests.

        v131.0: Includes OOM prevention - checks memory BEFORE heavy initialization.
        """
        logger.info("")
        logger.info("=" * 60)
        logger.info("Background Initialization Starting...")
        logger.info("=" * 60)

        self.app.state.startup_phase = "INITIALIZING"

        # =========================================================================
        # v131.0: OOM PREVENTION - Pre-flight memory check before heavy components
        # =========================================================================
        # This prevents SIGKILL (exit code -9) crashes during initialization by
        # detecting low memory BEFORE loading heavy ML models and offloading to GCP.
        # =========================================================================
        # Track GCP offload state (stored in app.state for component access)
        self.app.state.gcp_offload_active = False
        self.app.state.gcp_vm_ip = None
        self.app.state.oom_degradation_active = False  # v132.0

        if OOM_PREVENTION_AVAILABLE:
            try:
                # Estimate total heavy component memory
                heavy_components = ["neural_mesh", "speaker_verification", "jarvis_voice_api"]
                total_estimated_mb = sum(
                    HEAVY_COMPONENT_MEMORY_ESTIMATES.get(c, 500)
                    for c in heavy_components
                )

                logger.info(f"[OOM Prevention] Checking memory for heavy init (~{total_estimated_mb}MB required)")

                # Check memory before proceeding
                memory_result = await check_memory_before_heavy_init(
                    component="startup_initialization",
                    estimated_mb=total_estimated_mb,
                    auto_offload=True,
                )

                if memory_result.decision == MemoryDecision.CLOUD_REQUIRED:
                    logger.warning(f"[OOM Prevention] ⚠️ Local RAM insufficient - GCP offload required")
                    if memory_result.gcp_vm_ready:
                        self.app.state.gcp_offload_active = True
                        self.app.state.gcp_vm_ip = memory_result.gcp_vm_ip
                        logger.info(f"[OOM Prevention] ✅ GCP VM ready at {memory_result.gcp_vm_ip}")
                        logger.info(f"[OOM Prevention] Heavy components will be offloaded to cloud")
                    else:
                        logger.error(f"[OOM Prevention] ❌ GCP VM not available - proceeding with risk")

                elif memory_result.decision == MemoryDecision.CLOUD:
                    logger.info(f"[OOM Prevention] ☁️ Cloud recommended (optional)")
                    if memory_result.gcp_vm_ready:
                        self.app.state.gcp_offload_active = True
                        self.app.state.gcp_vm_ip = memory_result.gcp_vm_ip
                        logger.info(f"[OOM Prevention] Using GCP VM at {memory_result.gcp_vm_ip}")

                elif memory_result.decision == MemoryDecision.DEGRADED:
                    # v132.0: Graceful degradation - proceed with reduced functionality
                    tier_name = memory_result.degradation_tier.value if memory_result.degradation_tier else "unknown"
                    logger.info(f"[OOM Prevention] ⚡ Using graceful degradation (Tier: {tier_name})")
                    if memory_result.fallback_strategy:
                        logger.info(f"[OOM Prevention] Strategy: {memory_result.fallback_strategy.description}")
                    # Store degradation state for components to query
                    self.app.state.oom_degradation_active = True
                    self.app.state.oom_degradation_tier = memory_result.degradation_tier
                    self.app.state.oom_fallback_strategy = memory_result.fallback_strategy

                elif memory_result.decision == MemoryDecision.ABORT:
                    # v132.0: ABORT only when ALL degradation strategies exhausted
                    logger.error(f"[OOM Prevention] ❌ ABORT - All strategies exhausted")
                    logger.error(f"[OOM Prevention] Reason: {memory_result.reason}")
                    for rec in memory_result.recommendations[-3:]:
                        logger.error(f"[OOM Prevention]   → {rec}")
                    # Continue anyway in degraded mode - better than failing completely
                    self.app.state.oom_abort_attempted = True

                else:
                    logger.info(f"[OOM Prevention] ✅ Sufficient local RAM ({memory_result.available_ram_gb:.1f}GB)")
                    if memory_result.gcp_auto_enabled:
                        logger.info("[OOM Prevention] 🔧 Note: GCP was auto-enabled for future use")

            except Exception as e:
                logger.warning(f"[OOM Prevention] Check failed (non-fatal): {e}")
        # =========================================================================

        try:
            # Group components by priority
            priority_groups = self._group_by_priority()

            # Initialize each priority group
            for priority, group in sorted(priority_groups.items()):
                if self._get_shutdown_event().is_set():
                    break

                logger.info(f"Initializing priority {priority} components: {[c.name for c in group]}")

                # Run group in parallel
                tasks = []
                for comp in group:
                    # v125.0: Enhanced dependency checking with failure propagation
                    dep_status = self._check_dependency_status(comp)
                    if dep_status == "ready":
                        tasks.append(self._init_component(comp.name))
                    elif dep_status == "failed":
                        # v125.0: Dependencies failed - skip this component immediately
                        failed_deps = [
                            d for d in comp.dependencies
                            if self.components.get(d, ComponentInit(name=d)).phase
                            in (InitPhase.FAILED, InitPhase.SKIPPED)
                        ]
                        logger.warning(
                            f"⚠️ Skipping {comp.name} - dependency failure cascade: {failed_deps}"
                        )
                        await self._mark_skipped(
                            comp.name,
                            f"Dependency failure cascade ({', '.join(failed_deps)})"
                        )
                    else:
                        # Dependencies not ready yet (still running or pending)
                        logger.warning(f"Skipping {comp.name} - dependencies not ready: {comp.dependencies}")
                        await self._mark_skipped(comp.name, "Dependencies not ready")

                if tasks:
                    await safe_gather(*tasks, return_exceptions=True)

                # Update progress
                self._update_progress()

                # v125.0: Check for infrastructure failure fast-forward
                # If critical infrastructure components (cloud_sql_proxy, config) fail early,
                # cascade-skip all remaining dependent components instead of waiting
                if self._should_fast_forward_startup():
                    logger.warning(
                        "⚡ v125.0: Infrastructure failure detected - fast-forwarding startup"
                    )
                    await self._fast_forward_remaining_components()
                    break

            # Check final state
            failed_critical = [
                c.name for c in self.components.values()
                if c.phase == InitPhase.FAILED and c.is_critical
            ]

            if failed_critical:
                self.app.state.startup_phase = "DEGRADED"
                logger.warning(f"DEGRADED MODE: Critical components failed: {failed_critical}")
            else:
                self.app.state.startup_phase = "FULL_MODE"
                self._get_full_mode_event().set()
                logger.info("")
                logger.info("=" * 60)
                logger.info("FULL MODE - All Systems Operational")
                logger.info("=" * 60)

            # Log final stats
            elapsed = time.time() - self.started_at
            ready = sum(1 for c in self.components.values() if c.phase == InitPhase.COMPLETE)
            total = len(self.components)
            logger.info(f"Initialization complete: {ready}/{total} components in {elapsed:.1f}s")

            # Broadcast final completion to frontend
            broadcaster = get_startup_broadcaster()
            success = not failed_critical
            await broadcaster.broadcast_complete(
                success=success,
                message="JARVIS Online" if success else f"JARVIS Online (degraded: {', '.join(failed_critical)})"
            )

        except Exception as e:
            logger.error(f"Background initialization error: {e}", exc_info=True)
            self.app.state.startup_phase = "ERROR"

            # Broadcast error state
            broadcaster = get_startup_broadcaster()
            await broadcaster.broadcast_complete(
                success=False,
                message=f"Startup error: {str(e)}"
            )

    def _group_by_priority(self) -> Dict[int, List[ComponentInit]]:
        """Group components by priority for parallel execution"""
        groups: Dict[int, List[ComponentInit]] = {}
        for comp in self.components.values():
            if comp.priority not in groups:
                groups[comp.priority] = []
            groups[comp.priority].append(comp)
        return groups

    def _check_dependency_status(self, comp: ComponentInit) -> str:
        """
        v125.0: Check the status of component dependencies.

        This enables intelligent dependency failure propagation:
        - If ANY dependency has FAILED or SKIPPED, return "failed" to cascade skip
        - If ALL dependencies are COMPLETE, return "ready" to proceed
        - Otherwise return "not_ready" to wait

        Args:
            comp: The component to check dependencies for

        Returns:
            "ready": All dependencies complete, component can initialize
            "failed": At least one dependency failed/skipped, component should skip
            "not_ready": Dependencies still in progress, component should wait
        """
        if not comp.dependencies:
            return "ready"

        all_complete = True
        any_failed = False

        for dep_name in comp.dependencies:
            dep_comp = self.components.get(dep_name)
            if dep_comp is None:
                # Unknown dependency - treat as complete (defensive)
                logger.warning(f"[DependencyCheck] Unknown dependency '{dep_name}' for {comp.name}")
                continue

            if dep_comp.phase == InitPhase.COMPLETE:
                continue
            elif dep_comp.phase in (InitPhase.FAILED, InitPhase.SKIPPED):
                any_failed = True
                all_complete = False
            else:
                all_complete = False

        if any_failed:
            return "failed"
        elif all_complete:
            return "ready"
        else:
            return "not_ready"

    def _should_fast_forward_startup(self) -> bool:
        """
        v125.0: Check if we should fast-forward startup due to infrastructure failure.

        Infrastructure components are those that many other components depend on.
        If they fail, waiting for dependent components is pointless - they will all
        fail or skip anyway. Fast-forwarding saves time and prevents the 600s global
        startup timeout from triggering.

        Infrastructure components:
        - cloud_sql_proxy: Database connectivity
        - config: Configuration loading (actually handled separately)

        Returns:
            True if infrastructure failure detected and fast-forward is recommended
        """
        infrastructure_components = ["cloud_sql_proxy"]

        for infra_name in infrastructure_components:
            infra_comp = self.components.get(infra_name)
            if infra_comp and infra_comp.phase in (InitPhase.FAILED, InitPhase.SKIPPED):
                # Check if this component has dependents that are still pending
                has_pending_dependents = False
                for comp in self.components.values():
                    if infra_name in comp.dependencies:
                        if comp.phase == InitPhase.PENDING:
                            has_pending_dependents = True
                            break

                if has_pending_dependents:
                    logger.info(
                        f"[v125.0] Infrastructure component '{infra_name}' failed with pending dependents"
                    )
                    return True

        return False

    async def _fast_forward_remaining_components(self):
        """
        v125.0: Fast-forward all remaining PENDING components to SKIPPED.

        This is called when infrastructure failure is detected. Instead of waiting
        for each component to timeout individually (which could take many minutes),
        we immediately skip all PENDING components with a clear reason.

        This prevents the 600s global startup timeout from triggering.
        """
        skipped_count = 0

        for comp in self.components.values():
            if comp.phase == InitPhase.PENDING:
                await self._mark_skipped(
                    comp.name,
                    "Fast-forward skip (infrastructure failure)"
                )
                skipped_count += 1
            elif comp.phase == InitPhase.RUNNING:
                # Don't interrupt running components - let them timeout naturally
                # or complete. The watchdog will handle stuck components.
                logger.debug(f"[v125.0] Not skipping running component: {comp.name}")

        if skipped_count > 0:
            logger.warning(
                f"⚡ v125.0: Fast-forward skipped {skipped_count} pending components"
            )
            self._update_progress()

    async def _stale_component_watchdog(self):
        """
        v2.0: Watchdog task that detects stale (hung) components.

        Runs in background and:
        1. Checks for components stuck in RUNNING state too long
        2. Marks stale components as SKIPPED (not FAILED) for graceful degradation
        3. Checks for interactive readiness (WebSocket + Voice API)
        4. Broadcasts interactive ready when core components are available
        """
        logger.info("🐕 Stale component watchdog started")

        check_interval = 3.0  # Check every 3 seconds
        max_runtime = 180.0  # Stop watchdog after 3 minutes

        start_time = time.time()

        while not self._get_shutdown_event().is_set():
            try:
                elapsed = time.time() - start_time

                # Stop watchdog after max runtime
                if elapsed > max_runtime:
                    logger.info("🐕 Watchdog completed (max runtime reached)")
                    break

                # v258.1 R2-#4: Early warning scan (BEFORE stale detection)
                # Must be separate from stale_components loop since is_stale = already exceeded
                for name, comp in self.components.items():
                    if comp.phase == InitPhase.RUNNING and comp.start_time:
                        _elapsed = comp.running_seconds
                        _warn_at = comp.stale_threshold_seconds * 0.8
                        if _elapsed >= _warn_at and _elapsed < comp.stale_threshold_seconds:
                            if not comp._stale_warned:
                                logger.warning(
                                    f"Watchdog: {name} approaching stale threshold "
                                    f"({_elapsed:.0f}s / {comp.stale_threshold_seconds:.0f}s)"
                                )
                                comp._stale_warned = True

                # Check for stale components
                stale_components = []
                for name, comp in self.components.items():
                    if comp.is_stale:
                        stale_components.append((name, comp.running_seconds))

                # Mark stale components as skipped
                for name, running_secs in stale_components:
                    comp = self.components.get(name)
                    if comp and comp.phase == InitPhase.RUNNING:
                        logger.warning(
                            f"⚠️ Watchdog: {name} stale after {running_secs:.0f}s "
                            f"(threshold: {comp.stale_threshold_seconds}s) - skipping"
                        )
                        await self._mark_skipped(
                            name,
                            f"Stale after {running_secs:.0f}s (watchdog)"
                        )

                # Check for interactive readiness
                if not self._get_interactive_ready_event().is_set():
                    await self._check_interactive_ready()

                # If full mode is set, we're done
                if self._get_full_mode_event().is_set():
                    logger.info("🐕 Watchdog: Full mode reached, stopping")
                    break

                await asyncio.sleep(check_interval)

            except asyncio.CancelledError:
                logger.info("🐕 Watchdog cancelled")
                break
            except Exception as e:
                logger.warning(f"🐕 Watchdog error: {e}")
                await asyncio.sleep(check_interval)

    async def _check_interactive_ready(self):
        """
        v2.0: Check if interactive components are ready.

        Interactive readiness means the user can start interacting with JARVIS,
        even if background components (ML models, neural mesh) are still loading.

        Interactive components: unified_websocket, jarvis_voice_api, voice_unlock_api
        """
        if self._get_interactive_ready_event().is_set():
            return

        interactive_components = [
            name for name, comp in self.components.items()
            if comp.is_interactive
        ]

        ready_interactive = [
            name for name in interactive_components
            if self.components[name].phase == InitPhase.COMPLETE
        ]

        # Need at least WebSocket to be interactive
        websocket_ready = self.components.get("unified_websocket")
        if websocket_ready and websocket_ready.phase == InitPhase.COMPLETE:
            # WebSocket is ready - we can accept connections
            if len(ready_interactive) >= 1:  # At least WebSocket
                self._get_interactive_ready_event().set()
                self.app.state.interactive_ready = True

                if not self._interactive_announced:
                    self._interactive_announced = True
                    logger.info("")
                    logger.info("=" * 60)
                    logger.info("🟢 INTERACTIVE MODE - User can interact!")
                    logger.info(f"   Ready: {', '.join(ready_interactive)}")
                    pending = [n for n in interactive_components if n not in ready_interactive]
                    if pending:
                        logger.info(f"   Still loading: {', '.join(pending)}")
                    logger.info("=" * 60)

                    # Broadcast interactive ready to frontend
                    broadcaster = get_startup_broadcaster()
                    await broadcaster.broadcast_component_complete(
                        component="interactive_ready",
                        message="JARVIS is ready for interaction!",
                        duration_ms=None
                    )

    def is_interactive_ready(self) -> bool:
        """Check if interactive components are ready"""
        return self._get_interactive_ready_event().is_set()

    async def _init_component(self, name: str):
        """
        Initialize a single component with timeout protection and graceful degradation.

        v2.0 Enhancements:
        - Per-component timeout protection (60s default, 120s for heavy components)
        - Graceful degradation for non-critical components
        - Better error context and logging
        - Skips already-completed components (e.g., config marked complete in minimal_setup)
        """
        comp = self.components.get(name)
        if not comp:
            return

        # Skip if already complete (e.g., config is marked complete in minimal_setup)
        if comp.phase == InitPhase.COMPLETE:
            logger.debug(f"[SKIP] {name} already complete")
            return

        comp.phase = InitPhase.RUNNING
        comp.start_time = time.time()

        # Broadcast component start via WebSocket
        broadcaster = get_startup_broadcaster()
        await broadcaster.broadcast_component_start(
            component=name,
            message=f"Initializing {name.replace('_', ' ').title()}..."
        )

        # Determine timeout based on component type
        # Heavy components (ML, DB) get more time
        heavy_components = {
            'vbi_prewarm', 'cloud_ecapa_client', 'neural_mesh',
            'ml_engine_registry', 'cloud_sql_proxy', 'learning_database',
            'agentic_system'
        }
        timeout = 120.0 if name in heavy_components else 60.0

        try:
            # Dispatch to component-specific initializer with timeout
            initializer = getattr(self, f"_init_{name}", None)
            if initializer:
                try:
                    await asyncio.wait_for(initializer(), timeout=timeout)
                except asyncio.TimeoutError:
                    error_msg = f"Initialization timeout after {timeout}s"
                    if comp.is_critical:
                        # Critical component timeout is a failure
                        raise RuntimeError(error_msg)
                    else:
                        # Non-critical component timeout - log and continue
                        logger.warning(f"⚠️ {name} initialization timeout ({timeout}s) - continuing with degraded functionality")
                        await self._mark_skipped(name, error_msg)
                        return
            else:
                logger.debug(f"No initializer for {name}, marking complete")

            await self._mark_complete(name)

        except Exception as e:
            error_context = str(e)
            if comp.is_critical:
                logger.error(f"❌ Critical component {name} failed: {error_context}")
            else:
                logger.warning(f"⚠️ Non-critical component {name} failed: {error_context}")
            await self._mark_failed(name, error_context)

    async def _mark_complete(self, name: str):
        """Mark a component as complete"""
        comp = self.components.get(name)
        if comp:
            comp.phase = InitPhase.COMPLETE
            comp.end_time = time.time()
            self.app.state.components_ready.add(name)

            duration_ms = comp.duration_ms
            if duration_ms:
                logger.info(f"[READY] {name} ({duration_ms:.0f}ms)")
            else:
                logger.info(f"[READY] {name}")

            # Broadcast completion via WebSocket (with timeout to prevent blocking)
            try:
                broadcaster = get_startup_broadcaster()
                await asyncio.wait_for(
                    broadcaster.broadcast_component_complete(
                        component=name,
                        message=f"{name.replace('_', ' ').title()} ready",
                        duration_ms=duration_ms
                    ),
                    timeout=2.0  # 2 second timeout to prevent startup blocking
                )
            except asyncio.TimeoutError:
                logger.warning(f"[STARTUP] Broadcast timeout for {name} - continuing startup")
            except Exception as e:
                logger.warning(f"[STARTUP] Broadcast error for {name}: {e} - continuing startup")

    async def _mark_failed(self, name: str, error: str):
        """Mark a component as failed"""
        comp = self.components.get(name)
        if comp:
            comp.phase = InitPhase.FAILED
            comp.end_time = time.time()
            comp.error = error
            self.app.state.components_failed.add(name)
            logger.warning(f"[FAILED] {name}: {error}")

            # Broadcast failure via WebSocket (with timeout to prevent blocking)
            try:
                broadcaster = get_startup_broadcaster()
                await asyncio.wait_for(
                    broadcaster.broadcast_component_failed(
                        component=name,
                        error=error,
                        is_critical=comp.is_critical
                    ),
                    timeout=2.0  # 2 second timeout to prevent startup blocking
                )
            except asyncio.TimeoutError:
                logger.warning(f"[STARTUP] Broadcast timeout for {name} failure - continuing")
            except Exception as e:
                logger.warning(f"[STARTUP] Broadcast error for {name} failure: {e} - continuing")

    async def _mark_skipped(self, name: str, reason: str):
        """Mark a component as skipped"""
        comp = self.components.get(name)
        if comp:
            comp.phase = InitPhase.SKIPPED
            comp.error = reason
            logger.info(f"[SKIPPED] {name}: {reason}")

    def _update_progress(self):
        """Update startup progress percentage"""
        total = len(self.components)
        done = sum(
            1 for c in self.components.values()
            if c.phase in (InitPhase.COMPLETE, InitPhase.FAILED, InitPhase.SKIPPED)
        )
        self.app.state.startup_progress = done / total if total > 0 else 1.0

    # =========================================================================
    # Component-specific initializers
    # =========================================================================

    async def _init_ai_loader(self):
        """
        Initialize the Hyper-Speed AI Loader with Unified Optimization Router.

        This is the core of the non-blocking startup architecture:
        - Creates the AsyncModelManager singleton
        - Discovers available optimization engines (Rust, JIT, ONNX, etc.)
        - Prepares the router for intelligent model loading

        After this completes, all subsequent model registrations will use
        Ghost Proxies for instant startup with background loading.
        """
        if not AI_LOADER_AVAILABLE:
            logger.warning("AI Loader not available - skipping initialization")
            return

        try:
            logger.info("=" * 60)
            logger.info("HYPER-SPEED AI LOADER INITIALIZATION")
            logger.info("=" * 60)

            # Get the global AI Manager (singleton)
            ai_manager = get_ai_manager()

            # Discover available optimization engines
            router = get_optimization_router()
            engines = router.discover_engines()

            # Count available engines
            available_engines = [
                (e.name, c.speedup_factor)
                for e, c in engines.items()
                if c.available
            ]

            logger.info(f"   Optimization Router: {len(available_engines)} engines available")
            for name, speedup in available_engines[:4]:  # Show top 4
                logger.info(f"      {name}: {speedup}x speedup")

            # Store in app state for global access
            self.app.state.ai_manager = ai_manager
            self.app.state.optimization_router = router
            self.app.state.ai_loader_ready = True

            logger.info("   ✅ AI Loader ready - Ghost Proxy pattern enabled")
            logger.info("   All ML models will now use non-blocking background loading")
            logger.info("=" * 60)

        except Exception as e:
            logger.error(f"AI Loader initialization failed: {e}", exc_info=True)
            self.app.state.ai_loader_ready = False
            # Don't raise - system can still function with direct loading
            raise

    async def _init_optimized_voice_models(self):
        """
        Register Voice Models via Hyper-Speed AI Loader.

        This method demonstrates the Ghost Proxy pattern:
        1. Define heavy loader functions (DON'T run them!)
        2. Register with AI Manager (returns instant proxy)
        3. Attach proxies to app.state (server thinks models are ready)
        4. Background loading happens automatically

        Result: This method completes in <0.01s instead of 4+ seconds,
        because we're just registering proxies, not loading models.
        """
        if not AI_LOADER_AVAILABLE:
            logger.info("AI Loader not available - voice models will load directly")
            return

        try:
            logger.info("=" * 60)
            logger.info("OPTIMIZED VOICE MODEL REGISTRATION (Ghost Proxies)")
            logger.info("=" * 60)

            ai_manager = get_ai_manager()
            start_time = time.time()

            # =========================================================
            # 1. ECAPA-TDNN Speaker Embedding Model (Heavy - 4+ seconds normally)
            # =========================================================
            def load_ecapa_heavy():
                """Heavy loader for ECAPA-TDNN model - runs in background thread."""
                try:
                    from cloud_services.ecapa_cloud_service import ECAPAModelManager
                    manager = ECAPAModelManager()
                    logger.info("   [BACKGROUND] ECAPA model loaded")
                    return manager
                except ImportError:
                    # Try alternative import path (v93.0: Updated for SpeechBrain 1.0+ compatibility)
                    try:
                        try:
                            from speechbrain.inference import EncoderClassifier
                        except ImportError:
                            from speechbrain.pretrained import EncoderClassifier
                        model = EncoderClassifier.from_hparams(
                            source="speechbrain/spkrec-ecapa-voxceleb",
                            savedir="/tmp/ecapa_model",
                        )
                        logger.info("   [BACKGROUND] ECAPA (SpeechBrain) loaded")
                        return model
                    except Exception as e:
                        logger.warning(f"   [BACKGROUND] ECAPA load failed: {e}")
                        return None

            # Register with AI Manager - returns INSTANTLY!
            ecapa_proxy = ai_manager.register_model(
                name="ecapa_speaker",
                loader_func=load_ecapa_heavy,
                priority=ModelPriority.HIGH,  # Load first
                hints={
                    "category": "voice",
                    "quantization": "int8",
                    "prefer_speed": True,
                },
                quantize=True,
            )

            # Attach to app.state - server can use this immediately
            self.app.state.ecapa_model = ecapa_proxy
            logger.info(f"   ✅ ECAPA Speaker Model registered (proxy: {ecapa_proxy.status.name})")

            # =========================================================
            # 2. Voice Unlock Classifier (Medium - 2+ seconds normally)
            # =========================================================
            def load_voice_unlock_classifier():
                """Heavy loader for voice unlock classifier."""
                try:
                    from voice_unlock.intelligent_voice_unlock_service import (
                        get_intelligent_unlock_service,
                    )
                    service = get_intelligent_unlock_service()
                    logger.info("   [BACKGROUND] Voice unlock classifier loaded")
                    return service
                except Exception as e:
                    logger.warning(f"   [BACKGROUND] Voice unlock load failed: {e}")
                    return None

            voice_unlock_proxy = ai_manager.register_model(
                name="voice_unlock_classifier",
                loader_func=load_voice_unlock_classifier,
                priority=ModelPriority.HIGH,
                hints={
                    "category": "voice",
                    "engine": "rust_int8",  # Prefer Rust INT8 if available
                },
                quantize=True,
            )

            self.app.state.voice_unlock_classifier = voice_unlock_proxy
            logger.info(f"   ✅ Voice Unlock Classifier registered (proxy: {voice_unlock_proxy.status.name})")

            # =========================================================
            # 3. Speaker Verification Service (if learning_db available)
            # =========================================================
            learning_db = getattr(self.app.state, 'learning_db', None)

            def load_speaker_verification():
                """Heavy loader for speaker verification service."""
                try:
                    from voice.speaker_verification_service import SpeakerVerificationService
                    if learning_db:
                        service = SpeakerVerificationService(learning_db)
                        # Can't call async from sync, but service will init on first use
                        logger.info("   [BACKGROUND] Speaker verification service loaded")
                        return service
                    return None
                except Exception as e:
                    logger.warning(f"   [BACKGROUND] Speaker verification load failed: {e}")
                    return None

            speaker_proxy = ai_manager.register_model(
                name="speaker_verification",
                loader_func=load_speaker_verification,
                priority=ModelPriority.NORMAL,
                hints={"category": "voice"},
                quantize=False,  # Service doesn't need quantization
            )

            self.app.state.speaker_verification_proxy = speaker_proxy
            logger.info(f"   ✅ Speaker Verification registered (proxy: {speaker_proxy.status.name})")

            # =========================================================
            # 4. VAD (Voice Activity Detection) Model
            # =========================================================
            def load_vad_model():
                """Heavy loader for VAD model.

                Uses torch.hub with cache-aware source selection:
                - source='local' when cache exists (no network, no timeout)
                - source='github' as fallback for first download
                """
                try:
                    import torch
                    # Check if Silero VAD is already cached locally
                    hub_dir = torch.hub.get_dir()
                    local_cache = os.path.join(hub_dir, "snakers4_silero-vad_master")
                    if os.path.isdir(local_cache):
                        # Cache exists — load directly, no network validation
                        source = "local"
                        repo = local_cache
                    else:
                        # First download — must fetch from GitHub
                        source = "github"
                        repo = "snakers4/silero-vad"

                    vad_model, utils = torch.hub.load(
                        repo_or_dir=repo,
                        model='silero_vad',
                        force_reload=False,
                        onnx=True,  # Use ONNX for speed
                        source=source,
                    )
                    logger.info(f"   [BACKGROUND] VAD (Silero) loaded (source={source})")
                    return {"model": vad_model, "utils": utils}
                except Exception as e:
                    logger.warning(f"   [BACKGROUND] VAD load failed: {e}")
                    return None

            vad_proxy = ai_manager.register_model(
                name="vad_silero",
                loader_func=load_vad_model,
                priority=ModelPriority.NORMAL,
                hints={
                    "category": "voice",
                    "engine": "onnx",  # Prefer ONNX
                },
                quantize=False,
            )

            self.app.state.vad_model = vad_proxy
            logger.info(f"   ✅ VAD Model registered (proxy: {vad_proxy.status.name})")

            # =========================================================
            # Registration complete - measure time
            # =========================================================
            elapsed_ms = (time.time() - start_time) * 1000

            logger.info("")
            logger.info(f"   🚀 All voice models registered in {elapsed_ms:.1f}ms!")
            logger.info("   Models are loading in BACKGROUND - server is ready NOW")
            logger.info("   First request may wait briefly if model not yet loaded")
            logger.info("=" * 60)

            # Store registration status
            self.app.state.voice_models_registered = True
            self.app.state.voice_model_proxies = {
                "ecapa_speaker": ecapa_proxy,
                "voice_unlock_classifier": voice_unlock_proxy,
                "speaker_verification": speaker_proxy,
                "vad_silero": vad_proxy,
            }

        except Exception as e:
            logger.error(f"Voice model registration failed: {e}", exc_info=True)
            self.app.state.voice_models_registered = False
            # Don't raise - voice models will be loaded directly as fallback

    async def _init_optimized_vision_system(self):
        """
        Register Vision System via Hyper-Speed AI Loader.

        Vision components include:
        - Claude Vision Analyzer (Claude API-based image analysis)
        - Display Monitor (screen capture and analysis)
        - YOLO Object Detection (optional, if available)
        - CLIP Vision Encoder (optional, for embeddings)

        All registered as Ghost Proxies for instant startup.
        """
        if not AI_LOADER_AVAILABLE:
            logger.info("AI Loader not available - vision system will load directly")
            return

        try:
            logger.info("=" * 60)
            logger.info("OPTIMIZED VISION SYSTEM REGISTRATION (Ghost Proxies)")
            logger.info("=" * 60)

            ai_manager = get_ai_manager()
            start_time = time.time()

            # =========================================================
            # 1. Claude Vision Analyzer (Primary vision system)
            # =========================================================
            api_key = os.getenv("ANTHROPIC_API_KEY")

            def load_claude_vision():
                """Heavy loader for Claude Vision Analyzer."""
                try:
                    # v109.2: Ensure event loop exists for thread pool workers
                    # Some components in ClaudeVisionAnalyzer may need asyncio
                    import asyncio
                    try:
                        asyncio.get_running_loop()
                    except RuntimeError:
                        # No running loop - create one for this thread
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)

                    # Use the correct module path - main file is claude_vision_analyzer_main.py
                    from vision.claude_vision_analyzer_main import ClaudeVisionAnalyzer
                    if api_key:
                        analyzer = ClaudeVisionAnalyzer(api_key)
                        logger.info("   [BACKGROUND] Claude Vision Analyzer loaded")
                        return analyzer
                    logger.info("   [BACKGROUND] No API key - Claude Vision unavailable")
                    return None
                except Exception as e:
                    logger.info(f"   [BACKGROUND] Claude Vision not loaded: {e}")
                    return None

            vision_proxy = ai_manager.register_model(
                name="vision_analyzer",
                loader_func=load_claude_vision,
                priority=ModelPriority.HIGH,
                hints={
                    "category": "vision",
                    "prefer_speed": True,
                },
                quantize=False,  # API-based, no quantization needed
            )

            self.app.state.vision_analyzer = vision_proxy
            logger.info(f"   ✅ Claude Vision Analyzer registered (proxy: {vision_proxy.status.name})")

            # =========================================================
            # 2. Display Monitor (Screen capture and analysis)
            # =========================================================
            def load_display_monitor():
                """Heavy loader for Display Monitor."""
                try:
                    # Use the correct module path - display monitor is in display/ not vision/
                    from display.display_monitor_service import DisplayMonitorService
                    monitor = DisplayMonitorService()
                    logger.info("   [BACKGROUND] Display Monitor loaded")
                    return monitor
                except Exception as e:
                    logger.warning(f"   [BACKGROUND] Display Monitor load failed: {e}")
                    return None

            display_proxy = ai_manager.register_model(
                name="display_monitor",
                loader_func=load_display_monitor,
                priority=ModelPriority.NORMAL,
                hints={"category": "vision"},
                quantize=False,
            )

            self.app.state.display_monitor = display_proxy
            logger.info(f"   ✅ Display Monitor registered (proxy: {display_proxy.status.name})")

            # =========================================================
            # 3. YOLO Object Detector (optional)
            # =========================================================
            def load_yolo_detector():
                """Heavy loader for YOLO object detector."""
                try:
                    from ultralytics import YOLO
                    # Use YOLOv8 nano for speed
                    model = YOLO("yolov8n.pt")
                    logger.info("   [BACKGROUND] YOLO detector loaded")
                    return model
                except ImportError:
                    logger.debug("   [BACKGROUND] YOLO not available (ultralytics not installed)")
                    return None
                except Exception as e:
                    logger.warning(f"   [BACKGROUND] YOLO load failed: {e}")
                    return None

            yolo_proxy = ai_manager.register_model(
                name="yolo_detector",
                loader_func=load_yolo_detector,
                priority=ModelPriority.LOW,  # Optional, load last
                hints={
                    "category": "vision",
                    "engine": "onnx",  # Prefer ONNX for YOLO
                },
                quantize=True,
                lazy=True,  # Only load on first use
            )

            self.app.state.yolo_detector = yolo_proxy
            logger.info(f"   ✅ YOLO Detector registered (proxy: {yolo_proxy.status.name}, lazy=True)")

            # =========================================================
            # 4. Screen OCR (Tesseract-based text extraction)
            # =========================================================
            def load_screen_ocr():
                """Heavy loader for screen OCR."""
                try:
                    import pytesseract
                    # Verify tesseract is available
                    pytesseract.get_tesseract_version()
                    logger.info("   [BACKGROUND] Screen OCR (Tesseract) loaded")
                    return {"engine": "tesseract", "available": True}
                except Exception as e:
                    logger.debug(f"   [BACKGROUND] OCR not available: {e}")
                    return None

            ocr_proxy = ai_manager.register_model(
                name="screen_ocr",
                loader_func=load_screen_ocr,
                priority=ModelPriority.LOW,
                hints={"category": "vision"},
                quantize=False,
                lazy=True,  # Only load on first use
            )

            self.app.state.screen_ocr = ocr_proxy
            logger.info(f"   ✅ Screen OCR registered (proxy: {ocr_proxy.status.name}, lazy=True)")

            # =========================================================
            # Registration complete
            # =========================================================
            elapsed_ms = (time.time() - start_time) * 1000

            logger.info("")
            logger.info(f"   🚀 All vision components registered in {elapsed_ms:.1f}ms!")
            logger.info("   Vision system loading in BACKGROUND - server is ready NOW")
            logger.info("=" * 60)

            # Store registration status
            self.app.state.vision_system_registered = True
            self.app.state.vision_model_proxies = {
                "vision_analyzer": vision_proxy,
                "display_monitor": display_proxy,
                "yolo_detector": yolo_proxy,
                "screen_ocr": ocr_proxy,
            }

        except Exception as e:
            logger.error(f"Vision system registration failed: {e}", exc_info=True)
            self.app.state.vision_system_registered = False

    async def _init_optimized_intelligence(self):
        """
        Register Intelligence/Neural Mesh via Hyper-Speed AI Loader.

        Intelligence components include:
        - Neural Mesh (multi-agent coordination)
        - Hybrid Orchestrator (local/cloud decision engine)
        - Goal Inference Engine (user intent prediction)
        - UAE Engine (Unified Awareness Engine)

        All registered as Ghost Proxies for instant startup.
        """
        if not AI_LOADER_AVAILABLE:
            logger.info("AI Loader not available - intelligence system will load directly")
            return

        try:
            logger.info("=" * 60)
            logger.info("OPTIMIZED INTELLIGENCE REGISTRATION (Ghost Proxies)")
            logger.info("=" * 60)

            ai_manager = get_ai_manager()
            start_time = time.time()

            # Check startup decision for cloud-first mode
            startup_decision = getattr(self.app.state, 'startup_decision', None)
            skip_neural_mesh = startup_decision and getattr(startup_decision, 'skip_neural_mesh', False)

            # =========================================================
            # 1. Neural Mesh (Multi-agent coordination)
            # =========================================================
            if not skip_neural_mesh:
                def load_neural_mesh():
                    """Heavy loader for Neural Mesh."""
                    try:
                        from neural_mesh.integration import (
                            initialize_neural_mesh,
                            NeuralMeshConfig,
                        )
                        import asyncio

                        config = NeuralMeshConfig(
                            enable_crew=True,
                            enable_monitoring=True,
                            enable_knowledge_graph=True,
                            lazy_load=True,
                        )

                        # Run async init in sync context
                        loop = asyncio.new_event_loop()
                        try:
                            result = loop.run_until_complete(initialize_neural_mesh(config))
                            logger.info("   [BACKGROUND] Neural Mesh loaded")
                            return result
                        finally:
                            loop.close()
                    except ImportError:
                        logger.debug("   [BACKGROUND] Neural Mesh not available")
                        return None
                    except Exception as e:
                        logger.warning(f"   [BACKGROUND] Neural Mesh load failed: {e}")
                        return None

                mesh_proxy = ai_manager.register_model(
                    name="neural_mesh",
                    loader_func=load_neural_mesh,
                    priority=ModelPriority.NORMAL,
                    hints={
                        "category": "neural_net",
                        "prefer_speed": False,  # Prefer thoroughness
                    },
                    quantize=False,
                )

                self.app.state.neural_mesh = mesh_proxy
                logger.info(f"   ✅ Neural Mesh registered (proxy: {mesh_proxy.status.name})")
            else:
                logger.info("   ⏭️ Neural Mesh skipped (cloud-first mode)")

            # =========================================================
            # 2. Hybrid Orchestrator (Local/Cloud decision engine)
            # =========================================================
            def load_hybrid_orchestrator():
                """Heavy loader for Hybrid Orchestrator."""
                try:
                    from core.hybrid_orchestrator import get_orchestrator
                    import asyncio

                    orchestrator = get_orchestrator()

                    # Run async start in sync context
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(orchestrator.start())
                        logger.info("   [BACKGROUND] Hybrid Orchestrator loaded")
                        return orchestrator
                    finally:
                        loop.close()
                except Exception as e:
                    logger.warning(f"   [BACKGROUND] Hybrid Orchestrator load failed: {e}")
                    return None

            orchestrator_proxy = ai_manager.register_model(
                name="hybrid_orchestrator",
                loader_func=load_hybrid_orchestrator,
                priority=ModelPriority.NORMAL,
                hints={"category": "neural_net"},
                quantize=False,
            )

            self.app.state.hybrid_orchestrator = orchestrator_proxy
            logger.info(f"   ✅ Hybrid Orchestrator registered (proxy: {orchestrator_proxy.status.name})")

            # =========================================================
            # 3. Goal Inference Engine
            # =========================================================
            def load_goal_inference():
                """Heavy loader for Goal Inference Engine."""
                try:
                    from intelligence.goal_inference_engine import GoalInferenceEngine
                    engine = GoalInferenceEngine()
                    logger.info("   [BACKGROUND] Goal Inference Engine loaded")
                    return engine
                except ImportError:
                    logger.debug("   [BACKGROUND] Goal Inference not available")
                    return None
                except Exception as e:
                    logger.warning(f"   [BACKGROUND] Goal Inference load failed: {e}")
                    return None

            goal_proxy = ai_manager.register_model(
                name="goal_inference",
                loader_func=load_goal_inference,
                priority=ModelPriority.LOW,
                hints={"category": "neural_net"},
                quantize=False,
                lazy=True,  # Load on first use
            )

            self.app.state.goal_inference = goal_proxy
            logger.info(f"   ✅ Goal Inference registered (proxy: {goal_proxy.status.name}, lazy=True)")

            # =========================================================
            # 4. UAE Engine (Unified Awareness Engine)
            # =========================================================
            def load_uae_engine():
                """Heavy loader for UAE Engine."""
                try:
                    from intelligence.unified_awareness_engine import get_uae_engine
                    vision_analyzer = getattr(self.app.state, 'vision_analyzer', None)
                    engine = get_uae_engine(vision_analyzer=vision_analyzer)
                    logger.info("   [BACKGROUND] UAE Engine loaded")
                    return engine
                except ImportError:
                    logger.debug("   [BACKGROUND] UAE Engine not available")
                    return None
                except Exception as e:
                    logger.warning(f"   [BACKGROUND] UAE Engine load failed: {e}")
                    return None

            uae_proxy = ai_manager.register_model(
                name="uae_engine",
                loader_func=load_uae_engine,
                priority=ModelPriority.NORMAL,
                hints={"category": "neural_net"},
                quantize=False,
            )

            self.app.state.uae_engine = uae_proxy
            logger.info(f"   ✅ UAE Engine registered (proxy: {uae_proxy.status.name})")

            # =========================================================
            # Registration complete
            # =========================================================
            elapsed_ms = (time.time() - start_time) * 1000

            logger.info("")
            logger.info(f"   🚀 All intelligence components registered in {elapsed_ms:.1f}ms!")
            logger.info("   Intelligence system loading in BACKGROUND - server is ready NOW")
            logger.info("=" * 60)

            # Store registration status
            self.app.state.intelligence_registered = True
            self.app.state.intelligence_proxies = {
                "neural_mesh": getattr(self.app.state, 'neural_mesh', None),
                "hybrid_orchestrator": orchestrator_proxy,
                "goal_inference": goal_proxy,
                "uae_engine": uae_proxy,
            }

        except Exception as e:
            logger.error(f"Intelligence registration failed: {e}", exc_info=True)
            self.app.state.intelligence_registered = False

    async def _init_cloud_sql_proxy(self):
        """
        Initialize Cloud SQL proxy with DB-level readiness verification.

        v86.0 Enhancements:
        - Uses ProxyReadinessGate for DB-level verification (not just TCP port check)
        - Performs actual `SELECT 1` query to verify database connectivity
        - Graceful degradation to SQLite if proxy/DB unavailable
        - Distinguishes between credential errors vs proxy/network issues
        - No hardcoded delays - uses proper async readiness gate
        """
        try:
            # Add backend dir to path
            backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if backend_dir not in sys.path:
                sys.path.insert(0, backend_dir)

            from intelligence.cloud_sql_proxy_manager import get_proxy_manager
            from intelligence.cloud_sql_connection_manager import (
                get_connection_manager,
                get_readiness_gate,
                ReadinessState
            )

            # Get connection manager and readiness gate
            conn_mgr = get_connection_manager()
            readiness_gate = get_readiness_gate()

            # Signal we're in startup mode (suppress connection errors during startup)
            conn_mgr.set_proxy_ready(False)

            proxy_manager = get_proxy_manager()
            if not proxy_manager.is_running():
                # Start proxy asynchronously with timeout protection
                try:
                    await asyncio.wait_for(
                        proxy_manager.start(force_restart=False),
                        timeout=30.0  # 30s max for proxy startup
                    )
                except asyncio.TimeoutError:
                    logger.warning("⚠️ Cloud SQL proxy startup timeout (30s) - will verify DB-level readiness")
                    # Continue anyway - proxy might still be starting, gate will verify

            # Start health monitor in background (non-blocking, tracked)
            if TASK_MANAGER_AVAILABLE:
                task_mgr = get_task_manager()
                await task_mgr.spawn_monitor(
                    "cloud_sql_proxy_monitor",
                    proxy_manager.monitor(check_interval=60)
                )
            else:
                monitor_task = asyncio.create_task(
                    proxy_manager.monitor(check_interval=60),
                    name="cloud_sql_proxy_monitor"
                )
                self._tasks.append(monitor_task)

            # Store in app state
            self.app.state.cloud_sql_proxy_manager = proxy_manager

            # v86.0: Use ProxyReadinessGate for DB-level verification
            # This performs actual SELECT 1 query, not just TCP port check
            async def verify_db_level_readiness():
                """
                Verify DB-level readiness using ProxyReadinessGate.

                This replaces the old TCP-only check with actual database verification.
                """
                try:
                    # Wait for DB-level readiness (SELECT 1 verification)
                    # Timeout of 30s matches proxy startup timeout
                    result = await readiness_gate.wait_for_ready(timeout=30.0)

                    if result.state == ReadinessState.READY:
                        # DB-level verified! Signal readiness
                        conn_mgr.set_proxy_ready(True)
                        port = proxy_manager.config.get('cloud_sql', {}).get('port', 5432)
                        logger.info(f"   ✅ Cloud SQL DB-level ready on 127.0.0.1:{port} (verified via SELECT 1)")
                        if result.message:
                            logger.info(f"      {result.message}")

                        # v134.0: Start ProxyWatchdog for aggressive auto-recovery
                        try:
                            from intelligence.cloud_sql_connection_manager import start_proxy_watchdog
                            await start_proxy_watchdog()
                            logger.info("   🐕 ProxyWatchdog started (10s interval, aggressive auto-recovery)")
                        except ImportError:
                            logger.debug("   ProxyWatchdog not available (using legacy 60s monitor)")
                        except Exception as wd_err:
                            logger.warning(f"   ProxyWatchdog start failed: {wd_err}")

                    elif result.state == ReadinessState.DEGRADED_SQLITE:
                        # Credentials issue - fall back to SQLite
                        logger.warning(f"   ⚠️ Cloud SQL credentials invalid - using SQLite fallback")
                        logger.warning(f"      Reason: {result.failure_reason or result.message}")
                        conn_mgr.set_proxy_ready(False)

                    elif result.state == ReadinessState.UNAVAILABLE:
                        # Proxy/network issue - may recover later
                        logger.warning(f"   ⚠️ Cloud SQL proxy/network unavailable - using SQLite fallback")
                        logger.warning(f"      Reason: {result.failure_reason or result.message}")
                        conn_mgr.set_proxy_ready(False)

                    else:
                        # Unknown state
                        logger.warning(f"   ⚠️ Cloud SQL readiness unknown ({result.state}) - using SQLite fallback")
                        conn_mgr.set_proxy_ready(False)

                except asyncio.TimeoutError:
                    logger.warning("   ⚠️ Cloud SQL DB-level verification timeout (30s) - using SQLite fallback")
                    conn_mgr.set_proxy_ready(False)

                except Exception as e:
                    logger.warning(f"   ⚠️ Cloud SQL readiness check failed: {e}")
                    conn_mgr.set_proxy_ready(False)

            # Run DB-level readiness verification in background (don't block startup, tracked)
            if TASK_MANAGER_AVAILABLE:
                task_mgr = get_task_manager()
                await task_mgr.spawn(
                    "cloud_sql_db_level_ready_signal",
                    verify_db_level_readiness(),
                    priority=TaskPriority.HIGH
                )
            else:
                ready_task = asyncio.create_task(
                    verify_db_level_readiness(),
                    name="cloud_sql_db_level_ready_signal"
                )
                self._tasks.append(ready_task)

            port = proxy_manager.config.get('cloud_sql', {}).get('port', 5432)
            logger.info(f"   Cloud SQL proxy starting on 127.0.0.1:{port} (DB-level verification pending)")

        except ImportError as e:
            # Handle case where cloud_sql_connection_manager doesn't have new APIs yet
            logger.warning(f"⚠️ ProxyReadinessGate not available ({e}) - using legacy TCP check")
            # Fall back to legacy behavior
            try:
                from intelligence.cloud_sql_proxy_manager import get_proxy_manager
                from intelligence.cloud_sql_connection_manager import get_connection_manager

                conn_mgr = get_connection_manager()
                proxy_manager = get_proxy_manager()

                if proxy_manager.is_running():
                    conn_mgr.set_proxy_ready(True)
                    logger.info("   ✅ Cloud SQL proxy running (TCP-level only, legacy mode)")
                else:
                    conn_mgr.set_proxy_ready(False)
                    logger.warning("   ⚠️ Cloud SQL proxy not running - using SQLite fallback")
            except Exception as inner_e:
                logger.warning(f"⚠️ Legacy fallback also failed: {inner_e}")

        except Exception as e:
            logger.warning(f"⚠️ Cloud SQL proxy initialization failed: {e}")
            logger.info("   System will use SQLite fallback for local storage")
            # Don't raise - proxy is non-critical, we have SQLite fallback
            # Mark as skipped rather than failed
            pass

    async def _init_learning_database(self):
        """
        Initialize learning database with ProxyReadinessGate coordination.

        v86.0 Enhancements:
        - Checks ProxyReadinessGate state before attempting CloudSQL connections
        - No redundant retries if gate already knows CloudSQL is unavailable
        - Gracefully falls back to SQLite based on gate's determination
        - Respects credential vs proxy failure distinction from gate
        """
        try:
            from intelligence.learning_database import JARVISLearningDatabase

            learning_db = JARVISLearningDatabase()

            # v86.0: Check ProxyReadinessGate state for smarter initialization
            try:
                from intelligence.cloud_sql_connection_manager import (
                    get_readiness_gate,
                    ReadinessState
                )

                gate = get_readiness_gate()
                gate_state = gate.state

                # If gate already determined CloudSQL is unavailable, skip retries
                if gate_state == ReadinessState.DEGRADED_SQLITE:
                    logger.info("   ProxyReadinessGate indicates credentials invalid - using SQLite only")
                    await asyncio.wait_for(learning_db.initialize(), timeout=20.0)
                    self.app.state.learning_db = learning_db
                    logger.info("   ✅ Learning database ready (SQLite fallback mode)")
                    return

                elif gate_state == ReadinessState.UNAVAILABLE:
                    logger.info("   ProxyReadinessGate indicates proxy unavailable - using SQLite only")
                    await asyncio.wait_for(learning_db.initialize(), timeout=20.0)
                    self.app.state.learning_db = learning_db
                    logger.info("   ✅ Learning database ready (SQLite fallback, proxy may recover)")
                    return

                elif gate_state == ReadinessState.READY:
                    # CloudSQL verified ready - initialize with confidence
                    logger.info("   ProxyReadinessGate confirmed DB-level ready - initializing with CloudSQL")
                    await asyncio.wait_for(learning_db.initialize(), timeout=20.0)
                    self.app.state.learning_db = learning_db
                    logger.info("   ✅ Learning database ready (hybrid CloudSQL + SQLite)")
                    return

                # Gate state is UNKNOWN or CHECKING - wait briefly then proceed
                elif gate_state in (ReadinessState.UNKNOWN, ReadinessState.CHECKING):
                    logger.info(f"   ProxyReadinessGate state is {gate_state.value} - waiting for DB-level verification...")

                    # Wait for gate to reach a final state (max 15s)
                    try:
                        result = await gate.wait_for_ready(timeout=15.0)

                        if result.state == ReadinessState.READY:
                            logger.info(f"   Gate confirmed ready")
                            # Fall through to retry loop with CloudSQL available
                        elif result.state in (ReadinessState.UNAVAILABLE, ReadinessState.DEGRADED_SQLITE):
                            # v238.0: Gate gave definitive non-READY answer — skip retries,
                            # initialize with SQLite fallback immediately. This eliminates
                            # the 3×20s retry cascade when Cloud SQL is confirmed unavailable.
                            logger.info(
                                f"   Gate determined {result.state.value} after wait — "
                                f"fast-falling to SQLite (skipping {3}×{20}s retries)"
                            )
                            await asyncio.wait_for(learning_db.initialize(), timeout=20.0)
                            self.app.state.learning_db = learning_db
                            logger.info("   ✅ Learning database ready (SQLite fallback, gate-directed)")
                            return
                        else:
                            logger.info(f"   Gate determined state: {result.state.value} ({result.failure_reason or 'no details'})")

                    except asyncio.TimeoutError:
                        logger.warning("   Gate verification timeout - proceeding with initialization anyway")

            except ImportError:
                # ProxyReadinessGate not available - use legacy behavior
                logger.debug("   ProxyReadinessGate not available - using legacy initialization")

            # Try initialization with retries (only if gate didn't give definitive answer)
            max_retries = 3
            retry_delay = 2.0

            for attempt in range(max_retries):
                try:
                    # v238.0: Before each retry, re-check gate state — if it transitioned
                    # to UNAVAILABLE since we started, bail immediately instead of wasting
                    # another 20s timeout.
                    if attempt > 0:
                        try:
                            from intelligence.cloud_sql_connection_manager import (
                                get_readiness_gate as _get_gate,
                                ReadinessState as _RS,
                            )
                            _gate = _get_gate()
                            _gs = _gate.state
                            if _gs in (_RS.UNAVAILABLE, _RS.DEGRADED_SQLITE):
                                logger.info(
                                    f"   Gate now {_gs.value} — aborting retry {attempt+1}, "
                                    f"falling to SQLite"
                                )
                                await asyncio.wait_for(learning_db.initialize(), timeout=20.0)
                                self.app.state.learning_db = learning_db
                                logger.info("   ✅ Learning database ready (SQLite fallback, mid-retry gate check)")
                                return
                        except (ImportError, Exception):
                            pass  # Gate unavailable — continue with retry

                    # v226.1: Create fresh instance per attempt to avoid partial-state
                    # reuse. A timed-out initialize() may leave SQLite connections open,
                    # partial schema, or half-initialized ChromaDB. Retrying on the same
                    # instance leaks resources and risks inconsistent state.
                    if attempt > 0:
                        try:
                            await learning_db.close()
                        except BaseException:
                            pass
                        learning_db = JARVISLearningDatabase()

                    # Initialize with timeout per attempt
                    await asyncio.wait_for(learning_db.initialize(), timeout=20.0)
                    self.app.state.learning_db = learning_db
                    logger.info("   ✅ Learning database ready (hybrid CloudSQL + SQLite)")
                    return

                except asyncio.TimeoutError:
                    if attempt < max_retries - 1:
                        logger.info(f"   Learning DB init timeout (attempt {attempt+1}/{max_retries}) - retrying in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                    else:
                        raise RuntimeError(f"Learning DB initialization timeout after {max_retries} attempts")

                except Exception as e:
                    error_str = str(e).lower()
                    # Check if it's a connection error (proxy not ready)
                    if any(err in error_str for err in ['connection', 'proxy', 'refused', 'timeout']):
                        if attempt < max_retries - 1:
                            logger.info(f"   CloudSQL not ready (attempt {attempt+1}/{max_retries}) - retrying in {retry_delay}s...")
                            await asyncio.sleep(retry_delay)
                            continue
                        else:
                            logger.warning(f"   ⚠️ CloudSQL unavailable after {max_retries} attempts - using SQLite only")
                            # Still store the DB instance - it will fall back to SQLite
                            self.app.state.learning_db = learning_db
                            return
                    else:
                        # Non-connection error - raise immediately
                        raise

        except Exception as e:
            logger.warning(f"⚠️ Learning database initialization failed: {e}")
            logger.info("   System will operate without persistent learning (memory only)")
            # Don't raise - system can operate without learning DB
            pass

    async def _init_gcp_vm_manager(self):
        """
        ALWAYS initialize GCP VM Manager at startup.
        
        This ensures the VM manager is available for runtime memory pressure
        handling, even if the startup decision was LOCAL_FULL.
        
        Without this, if memory pressure rises after startup, the system
        cannot spin up a Spot VM because the manager was never initialized.
        """
        try:
            from core.gcp_vm_manager import get_gcp_vm_manager, VMManagerConfig
            import os
            
            # Configure for ML workload with sensible defaults
            config = VMManagerConfig(
                project_id=os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-ai-436818"),
                zone=os.getenv("GCP_ZONE", "us-central1-a"),
                machine_type=os.getenv("GCP_ML_VM_TYPE", "e2-highmem-4"),
                use_spot=True,
                spot_max_price=0.10,
                idle_timeout_minutes=15,
            )
            
            # Get and initialize the VM manager (singleton)
            vm_manager = await get_gcp_vm_manager(config)
            await vm_manager.initialize()
            
            # Store in app state for runtime access
            self.app.state.gcp_vm_manager = vm_manager
            
            logger.info(f"   GCP VM Manager: Ready (Project: {config.project_id})")
            logger.info(f"   Spot VM Type: {config.machine_type} (standby for memory pressure)")
            
        except ImportError as e:
            logger.debug(f"GCP VM Manager not available: {e}")
            self.app.state.gcp_vm_manager = None
        except Exception as e:
            logger.warning(f"GCP VM Manager init failed (non-critical): {e}")
            self.app.state.gcp_vm_manager = None

    async def _init_memory_aware_startup(self):
        """Determine memory-aware startup mode"""
        try:
            from core.memory_aware_startup import (
                determine_startup_mode,
                activate_cloud_ml_if_needed,
            )

            # Stage 1: Memory analysis (fast — just reads psutil stats)
            _mode_timeout = float(os.environ.get("JARVIS_MEMORY_MODE_TIMEOUT", "10.0"))
            startup_decision = await asyncio.wait_for(
                determine_startup_mode(), timeout=_mode_timeout,
            )
            self.app.state.startup_decision = startup_decision

            logger.info(f"   Memory mode: {startup_decision.mode.value}")
            logger.info(f"   Reason: {startup_decision.reason}")

            # Stage 2: Cloud activation (potentially slow — GCP VM creation)
            # Gets its own sub-timeout so it doesn't consume the entire component budget.
            if startup_decision.gcp_vm_required:
                # Check if Cloud SQL is already known-unavailable — if so, skip cloud
                # activation since it depends on infrastructure that's degraded.
                _skip_cloud = False
                try:
                    from intelligence.cloud_sql_connection_manager import (
                        get_readiness_gate as _get_gate,
                        ReadinessState as _RS,
                    )
                    _gate = _get_gate()
                    if _gate.state == _RS.UNAVAILABLE:
                        logger.info(
                            "   Cloud SQL UNAVAILABLE — skipping GCP ML activation "
                            "(would cascade-fail)"
                        )
                        _skip_cloud = True
                        self.app.state.cloud_ml_result = {
                            "success": False,
                            "mode": "skipped",
                            "reason": "Cloud SQL unavailable — GCP activation deferred",
                        }
                except (ImportError, Exception):
                    pass

                if not _skip_cloud:
                    _cloud_timeout = float(
                        os.environ.get("JARVIS_CLOUD_ML_ACTIVATION_TIMEOUT", "45.0")
                    )
                    try:
                        cloud_result = await asyncio.wait_for(
                            activate_cloud_ml_if_needed(startup_decision),
                            timeout=_cloud_timeout,
                        )
                        self.app.state.cloud_ml_result = cloud_result
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"   Cloud ML activation timed out ({_cloud_timeout:.0f}s) "
                            "— continuing without cloud backend"
                        )
                        self.app.state.cloud_ml_result = {
                            "success": False,
                            "mode": "timeout",
                            "reason": f"Activation timed out after {_cloud_timeout:.0f}s",
                        }

        except asyncio.TimeoutError:
            logger.warning(
                f"   determine_startup_mode() timed out ({_mode_timeout:.0f}s) "
                "— defaulting to local mode"
            )
        except ImportError:
            logger.debug("Memory-aware startup not available")
        except Exception as e:
            logger.warning(f"Memory-aware startup failed: {e}")

    async def _init_cloud_ml_router(self):
        """Initialize cloud ML router"""
        try:
            from core.cloud_ml_router import get_cloud_ml_router

            router = await get_cloud_ml_router()
            self.app.state.cloud_ml_router = router
            logger.info(f"   CloudMLRouter ready (backend: {router._current_backend.value})")

        except Exception as e:
            logger.warning(f"CloudMLRouter failed: {e}")

    async def _init_cloud_ecapa_client(self):
        """Initialize Cloud ECAPA client"""
        try:
            from voice_unlock.cloud_ecapa_client import get_cloud_ecapa_client

            client = await get_cloud_ecapa_client()
            if client:
                init_success = await client.initialize()
                if init_success:
                    self.app.state.cloud_ecapa_client = client
                    # Get backend info from client status if available
                    backend_info = "cloud_run"
                    if hasattr(client, 'get_status'):
                        status = client.get_status()
                        if isinstance(status, dict):
                            backend_info = status.get('healthy_endpoint', 'cloud_run')
                    logger.info(f"   CloudECAPAClient ready (backend: {backend_info})")
                else:
                    logger.warning("   CloudECAPAClient initialization returned False")

        except Exception as e:
            logger.warning(f"CloudECAPAClient failed: {e}")

    async def _init_vbi_prewarm(self):
        """
        Initialize VBI (Voice Biometric Intelligence) Pre-Warming in a NON-BLOCKING manner.

        v2.0 Enhancements:
        - Runs pre-warming in background (doesn't block startup)
        - Reduced timeout from 45s to 30s (faster startup)
        - Early success detection (returns as soon as first warmup succeeds)
        - Graceful degradation (voice still works if pre-warm incomplete)

        This ensures ECAPA embedding extraction is ready BEFORE any unlock requests,
        but doesn't delay server startup.
        """
        try:
            from core.vbi_debug_tracer import (
                get_prewarmer,
                get_orchestrator,
                get_tracer
            )

            logger.info("🔥 Starting VBI pre-warm (background, non-blocking)")

            # Initialize the VBI components (lightweight, no model loading yet)
            prewarmer = get_prewarmer()
            orchestrator = get_orchestrator()
            tracer = get_tracer()

            # Store references in app state IMMEDIATELY
            self.app.state.vbi_prewarmer = prewarmer
            self.app.state.vbi_orchestrator = orchestrator
            self.app.state.vbi_tracer = tracer

            # Initial status (will be updated by background task)
            self.app.state.vbi_prewarm_status = {
                "status": "warming",
                "timestamp": time.time()
            }

            # Reduced timeout for faster startup
            prewarm_timeout = float(os.environ.get("VBI_PREWARM_TIMEOUT", "30"))

            # Run actual pre-warming in BACKGROUND (non-blocking)
            async def background_prewarm():
                """Background task for VBI pre-warming - doesn't block startup"""
                try:
                    logger.info(f"   Background VBI pre-warm starting (timeout: {prewarm_timeout}s)...")

                    prewarm_result = await asyncio.wait_for(
                        prewarmer.warmup(force=True),
                        timeout=prewarm_timeout
                    )

                    if prewarm_result.get("status") == "success":
                        warmup_ms = prewarm_result.get("total_duration_ms", 0)
                        logger.info(f"   ✅ VBI pre-warm COMPLETE in {warmup_ms:.0f}ms")
                        logger.info(f"   ✅ ECAPA is now HOT - no cold starts during unlock!")

                        self.app.state.vbi_prewarm_status = {
                            "status": "success",
                            "warmup_ms": warmup_ms,
                            "timestamp": time.time(),
                            "endpoint": prewarm_result.get("stages", [{}])[0].get("endpoint", "unknown")
                        }
                    else:
                        logger.warning(f"   ⚠️ VBI pre-warm incomplete: {prewarm_result.get('status')}")
                        self.app.state.vbi_prewarm_status = {
                            "status": prewarm_result.get("status", "unknown"),
                            "error": prewarm_result.get("error"),
                            "timestamp": time.time()
                        }

                except asyncio.TimeoutError:
                    logger.warning(f"   ⚠️ VBI pre-warm timed out after {prewarm_timeout}s (background)")
                    logger.info("   Voice unlock will still work - first request may be slower")
                    self.app.state.vbi_prewarm_status = {
                        "status": "timeout",
                        "timeout_seconds": prewarm_timeout,
                        "timestamp": time.time()
                    }

                except Exception as e:
                    logger.warning(f"   ⚠️ Background VBI pre-warm error: {e}")
                    self.app.state.vbi_prewarm_status = {
                        "status": "error",
                        "error": str(e),
                        "timestamp": time.time()
                    }

            # Launch background pre-warm task (tracked for cleanup)
            if TASK_MANAGER_AVAILABLE:
                task_mgr = get_task_manager()
                await task_mgr.spawn(
                    "vbi_background_prewarm",
                    background_prewarm(),
                    priority=TaskPriority.LOW
                )
            else:
                prewarm_task = asyncio.create_task(
                    background_prewarm(),
                    name="vbi_background_prewarm"
                )
                self._tasks.append(prewarm_task)

            # Return immediately - don't wait for pre-warming to complete
            logger.info("   VBI pre-warm running in background (non-blocking)")

        except ImportError as e:
            logger.warning(f"⚠️ VBI pre-warmer not available: {e}")
            logger.info("   Voice unlock will work but may have cold start delays")

        except Exception as e:
            logger.warning(f"⚠️ VBI pre-warm setup failed: {e}")
            logger.info("   Voice unlock will still work - this is non-fatal")
            # Don't raise - this is not critical for server operation

    async def _init_vbi_health_monitor(self):
        """
        Initialize VBI Health Monitor for advanced operation tracking.

        Provides:
        - In-flight operation tracking with automatic timeout detection
        - Application-level heartbeat system for all VBI components
        - Circuit breakers with adaptive thresholds
        - Fallback chain orchestration
        - Integration with CloudECAPAClient, CloudSQL, WebSocket
        """
        try:
            from core.vbi_health_monitor import (
                get_vbi_health_monitor,
                ComponentType,
                HealthLevel,
            )

            logger.info("=" * 60)
            logger.info("VBI HEALTH MONITOR INITIALIZATION")
            logger.info("=" * 60)

            # Get or create the singleton monitor
            monitor = await get_vbi_health_monitor()

            # Store reference in app state for global access
            self.app.state.vbi_health_monitor = monitor

            # Register initial heartbeats for existing components
            components_to_register = []

            # Check for CloudECAPAClient
            if hasattr(self.app.state, 'cloud_ecapa_client') and self.app.state.cloud_ecapa_client:
                components_to_register.append((ComponentType.ECAPA_CLIENT, "CloudECAPAClient"))

            # Check for learning database (CloudSQL)
            if hasattr(self.app.state, 'learning_db') and self.app.state.learning_db:
                components_to_register.append((ComponentType.CLOUDSQL, "LearningDatabase"))

            # Register heartbeats for existing components
            for comp_type, comp_name in components_to_register:
                await monitor.record_heartbeat(
                    component=comp_type,
                    health_level=HealthLevel.HEALTHY,
                    latency_ms=0.0,
                    metadata={"source": "startup", "component_name": comp_name}
                )
                logger.info(f"   Registered heartbeat for {comp_name}")

            # Track state to avoid log spam
            # v93.1: Added grace period tracking to prevent false stale warnings at startup
            _event_state = {
                "stale_components": set(),
                "open_circuits": set(),
                "registration_times": {},  # Track when components were first registered
            }
            
            # Grace period before reporting staleness (prevents 0.0s warnings at startup)
            INITIAL_GRACE_PERIOD = 15.0  # seconds
            
            # Subscribe to health events for logging/alerting
            async def handle_health_event(event_type: str, data: dict):
                """Handle health events from VBI monitor.
                
                Only logs state CHANGES to avoid spam. Events that repeat
                (like stale heartbeats) are only logged once per component.
                
                v93.1: Added grace period to prevent false stale warnings at startup.
                """
                nonlocal _event_state
                
                if event_type == "operation_timeout":
                    # Timeouts are always important - log them
                    op_id = data.get("operation_id", "unknown")
                    op_type = data.get("operation_type", "unknown")
                    component = data.get("component", "unknown")
                    elapsed = data.get("elapsed_seconds", 0)
                    logger.warning(
                        f"⚠️ VBI Operation Timeout: {op_type} ({op_id}) on {component} "
                        f"after {elapsed:.1f}s"
                    )
                elif event_type == "heartbeat_stale":
                    # Only log NEW stale components, and only after grace period
                    component = data.get("component", "unknown")
                    last_beat = data.get("last_heartbeat_seconds_ago", 0)
                    
                    # Track registration time for grace period
                    if component not in _event_state["registration_times"]:
                        _event_state["registration_times"][component] = time.time()
                    
                    # Check if we're past the initial grace period
                    registration_time = _event_state["registration_times"].get(component, 0)
                    time_since_registration = time.time() - registration_time
                    
                    # Only report staleness if:
                    # 1. Past initial grace period AND
                    # 2. Actually stale (not 0.0s which indicates just-registered) AND
                    # 3. Not already reported
                    if (time_since_registration > INITIAL_GRACE_PERIOD and 
                        last_beat > INITIAL_GRACE_PERIOD and
                        component not in _event_state["stale_components"]):
                        _event_state["stale_components"].add(component)
                        logger.warning(
                            f"⚠️ VBI Heartbeat Stale: {component} - no heartbeat for {last_beat:.1f}s"
                        )
                elif event_type == "heartbeat_received":
                    # Clear stale state when heartbeat is received
                    component = data.get("component", "unknown")
                    _event_state["stale_components"].discard(component)
                    # Update registration time if this is a new component
                    if component not in _event_state["registration_times"]:
                        _event_state["registration_times"][component] = time.time()
                elif event_type == "circuit_opened":
                    # Only log NEW circuit opens
                    component = data.get("component", "unknown")
                    if component not in _event_state["open_circuits"]:
                        _event_state["open_circuits"].add(component)
                        failure_rate = data.get("failure_rate", 0)
                        logger.warning(
                            f"🔴 Circuit Breaker OPENED: {component} (failure rate: {failure_rate:.1%})"
                        )
                elif event_type == "circuit_closed":
                    component = data.get("component", "unknown")
                    if component in _event_state["open_circuits"]:
                        _event_state["open_circuits"].discard(component)
                        logger.info(
                            f"🟢 Circuit Breaker CLOSED: {component} - recovered"
                        )
                elif event_type == "health_degraded":
                    component = data.get("component", "unknown")
                    level = data.get("health_level", "unknown")
                    logger.warning(f"⚠️ Health Degraded: {component} -> {level}")

            monitor.on_event(handle_health_event)

            # Get initial system health
            system_health = await monitor.get_system_health()
            overall_health = system_health.get("overall_health", "unknown")
            active_ops = system_health.get("active_operations", 0)

            logger.info(f"   Overall health: {overall_health}")
            logger.info(f"   Active operations: {active_ops}")
            logger.info(f"   ✅ VBI Health Monitor ready (operation tracking active)")
            logger.info("=" * 60)

            # Store status for health checks
            self.app.state.vbi_health_monitor_status = {
                "status": "initialized",
                "overall_health": overall_health,
                "timestamp": time.time()
            }

        except ImportError as e:
            logger.warning(f"VBI Health Monitor not available: {e}")
            logger.warning("   Operation tracking will not be available")
            self.app.state.vbi_health_monitor = None
            self.app.state.vbi_health_monitor_status = {
                "status": "not_available",
                "error": str(e),
                "timestamp": time.time()
            }

        except Exception as e:
            logger.error(f"VBI Health Monitor failed: {e}")
            logger.error("   This is non-fatal - system will operate without advanced health tracking")
            self.app.state.vbi_health_monitor = None
            self.app.state.vbi_health_monitor_status = {
                "status": "failed",
                "error": str(e),
                "timestamp": time.time()
            }
            # Don't raise - this is not critical for server operation

    async def _init_ml_engine_registry(self):
        """Initialize ML engine registry"""
        try:
            try:
                from voice_unlock.ml_engine_registry import get_ml_registry
            except ImportError:
                from backend.voice_unlock.ml_engine_registry import get_ml_registry

            registry = await get_ml_registry()
            if registry:
                self.app.state.ml_registry = registry

                # Launch background prewarm
                startup_decision = getattr(self.app.state, 'startup_decision', None)
                registry.prewarm_background(
                    parallel=True,
                    startup_decision=startup_decision,
                )
                logger.info(f"   ML Engine Registry ready (engines: {list(registry._engines.keys())})")

        except Exception as e:
            logger.warning(f"ML Engine Registry failed: {e}")
            raise  # This is important for voice unlock

    async def _init_speaker_verification(self):
        """Initialize speaker verification service and pre-warm ECAPA classifier"""
        # Check if we already have Ghost Proxies from AI Loader
        voice_proxies = getattr(self.app.state, 'voice_model_proxies', {})

        if voice_proxies.get('speaker_verification'):
            # Already registered via AI Loader - just wait for it to be ready
            proxy = voice_proxies['speaker_verification']
            logger.info(f"   Speaker verification: using Ghost Proxy ({proxy.status.name})")

            # Store reference globally for compatibility
            import voice.speaker_verification_service as sv
            sv._global_speaker_service = proxy  # Will auto-wait when accessed

            # Don't block - the proxy handles waiting
            return

        # Fallback: Load directly if AI Loader not available
        try:
            from voice.speaker_verification_service import SpeakerVerificationService

            learning_db = getattr(self.app.state, 'learning_db', None)
            if learning_db:
                service = SpeakerVerificationService(learning_db)
                await service.initialize_fast()

                # Store globally
                import voice.speaker_verification_service as sv
                sv._global_speaker_service = service
                self.app.state.speaker_verification_service = service

                profiles = len(service.speaker_profiles)
                logger.info(f"   Speaker verification ready ({profiles} profiles)")

        except Exception as e:
            logger.warning(f"Speaker verification failed: {e}")

        # Pre-warm local ECAPA classifier to avoid 12s cold start on first unlock
        # Skip if AI Loader is handling this via Ghost Proxy
        if voice_proxies.get('ecapa_speaker'):
            logger.info("   ECAPA pre-warm: handled by AI Loader Ghost Proxy")
            return

        try:
            from voice_unlock.intelligent_voice_unlock_service import prewarm_ecapa_classifier
            logger.info("   Pre-warming ECAPA classifier for voice unlock...")
            await prewarm_ecapa_classifier()
            logger.info("   ECAPA classifier pre-warmed successfully")
        except Exception as e:
            logger.warning(f"   ECAPA pre-warm failed (non-fatal): {e}")

    async def _init_voice_unlock_api(self):
        """Ensure voice unlock API is mounted and components dict is populated"""
        try:
            from api.voice_unlock_api import router as voice_unlock_router

            # Check if already mounted
            existing = any(
                hasattr(r, 'path') and '/api/voice-unlock' in str(r.path)
                for r in self.app.routes
            )
            if not existing:
                self.app.include_router(voice_unlock_router, tags=["voice_unlock"])

            # Populate global components dict for voice_unlock
            import main
            main.components["voice_unlock"] = {
                "router": voice_unlock_router,
                "available": True,
                "initialized": True
            }

            # Also set app.state for health check
            self.app.state.voice_unlock = {
                "initialized": True,
                "available": True
            }

            logger.info("   Voice unlock API mounted")

        except Exception as e:
            logger.warning(f"Voice unlock API failed: {e}")

    async def _init_jarvis_voice_api(self):
        """Initialize JARVIS voice API and populate global components dict"""
        try:
            # Import the global components dict from main
            import main

            # Import voice system components
            voice = {}

            try:
                from api.voice_api import VoiceAPI
                voice["api"] = VoiceAPI
                voice["available"] = True
            except ImportError:
                voice["available"] = False

            try:
                from api.enhanced_voice_routes import router as enhanced_voice_router
                voice["enhanced_router"] = enhanced_voice_router
                voice["enhanced_available"] = True
            except ImportError:
                voice["enhanced_available"] = False

            try:
                from api.jarvis_voice_api import jarvis_api, router as jarvis_voice_router
                voice["jarvis_router"] = jarvis_voice_router
                voice["jarvis_api"] = jarvis_api
                voice["jarvis_available"] = True

                # Mount the JARVIS voice router if not already mounted
                existing = any(
                    hasattr(r, 'path') and '/voice/jarvis' in str(r.path)
                    for r in self.app.routes
                )
                if not existing:
                    self.app.include_router(jarvis_voice_router, prefix="/voice/jarvis", tags=["jarvis"])
                    logger.info("   JARVIS voice router mounted at /voice/jarvis")

            except ImportError as e:
                voice["jarvis_available"] = False
                logger.debug(f"JARVIS voice API import failed: {e}")

            # Populate the global components dict
            main.components["voice"] = voice

            if voice.get("jarvis_available"):
                logger.info("   JARVIS voice API ready (jarvis_available=True)")
            else:
                logger.warning("   JARVIS voice API loaded but jarvis_available=False")

        except Exception as e:
            logger.warning(f"JARVIS voice API failed: {e}")

    async def _init_unified_websocket(self):
        """Initialize unified WebSocket for frontend communication"""
        try:
            from api.unified_websocket import router as unified_ws_router

            # Check if already mounted - look for exact /ws path in WebSocket routes
            existing = any(
                hasattr(r, 'path') and r.path == '/ws'
                for r in self.app.routes
            )
            if not existing:
                self.app.include_router(unified_ws_router, tags=["websocket"])
                logger.info("   ✅ Unified WebSocket mounted at /ws")
            else:
                logger.info("   ✅ Unified WebSocket /ws already mounted")

        except ImportError as e:
            logger.warning(f"   Could not import unified WebSocket router: {e}")
            # Try fallback
            try:
                from api.vision_websocket import router as vision_ws_router
                self.app.include_router(vision_ws_router, prefix="/vision", tags=["vision"])
                logger.info("   Vision WebSocket mounted (fallback)")
            except ImportError:
                logger.warning("   No WebSocket router available")
        except Exception as e:
            logger.warning(f"Unified WebSocket failed: {e}")

    async def _init_neural_mesh(self):
        """Initialize neural mesh multi-agent system"""
        # Check if we already have Ghost Proxy from AI Loader
        intel_proxies = getattr(self.app.state, 'intelligence_proxies', {})

        if intel_proxies.get('neural_mesh'):
            # Already registered via AI Loader - just log status
            proxy = intel_proxies['neural_mesh']
            if proxy:
                logger.info(f"   Neural mesh: using Ghost Proxy ({proxy.status.name})")
            return

        # Fallback: Load directly if AI Loader not available
        try:
            startup_decision = getattr(self.app.state, 'startup_decision', None)
            if startup_decision and startup_decision.skip_neural_mesh:
                logger.info("   Neural mesh skipped (cloud-first mode)")
                return

            from neural_mesh.integration import (
                initialize_neural_mesh,
                NeuralMeshConfig,
            )

            config = NeuralMeshConfig(
                enable_crew=True,
                enable_monitoring=True,
                enable_knowledge_graph=True,
                lazy_load=True,
            )
            result = await initialize_neural_mesh(config)

            if result.get("status") == "initialized":
                self.app.state.neural_mesh = result
                logger.info(f"   Neural mesh ready ({result.get('elapsed_seconds', 0):.1f}s)")

        except ImportError:
            logger.debug("Neural mesh not available")
        except Exception as e:
            logger.warning(f"Neural mesh failed: {e}")

    async def _init_hybrid_orchestrator(self):
        """Initialize hybrid orchestrator with Trinity integration"""
        # Check if we already have Ghost Proxy from AI Loader
        intel_proxies = getattr(self.app.state, 'intelligence_proxies', {})

        if intel_proxies.get('hybrid_orchestrator'):
            proxy = intel_proxies['hybrid_orchestrator']
            logger.info(f"   Hybrid orchestrator: using Ghost Proxy ({proxy.status.name})")
            return

        # Fallback: Load directly if AI Loader not available
        try:
            from core.hybrid_orchestrator import get_orchestrator

            orchestrator = get_orchestrator()

            # Ensure Trinity manager is available for proper async context
            trinity_manager = getattr(self.app.state, 'trinity_manager', None)
            if trinity_manager:
                # Set Trinity reference in orchestrator for consensus protocol
                orchestrator._trinity_manager = trinity_manager

            await orchestrator.start()
            self.app.state.hybrid_orchestrator = orchestrator
            logger.info("   ✅ Hybrid orchestrator ready (Trinity-integrated)")

        except KeyError as e:
            # Specific handling for missing config keys (like 'routing')
            logger.warning(f"   [BACKGROUND] Hybrid Orchestrator load failed: {e}")
            logger.debug("   This usually means hybrid_config.yaml is missing or malformed")
        except Exception as e:
            logger.warning(f"   [BACKGROUND] Hybrid Orchestrator load failed: {e}")

    async def _init_vision_analyzer(self):
        """Initialize vision analyzer"""
        # Check if we already have Ghost Proxy from AI Loader
        vision_proxies = getattr(self.app.state, 'vision_model_proxies', {})

        if vision_proxies.get('vision_analyzer'):
            # Already registered via AI Loader - just log status
            proxy = vision_proxies['vision_analyzer']
            logger.info(f"   Vision analyzer: using Ghost Proxy ({proxy.status.name})")
            return

        # Fallback: Load directly if AI Loader not available
        try:
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                logger.info("   Vision analyzer skipped (no API key)")
                return

            # Use the correct module path - main file is claude_vision_analyzer_main.py
            from vision.claude_vision_analyzer_main import ClaudeVisionAnalyzer

            analyzer = ClaudeVisionAnalyzer(api_key)
            self.app.state.vision_analyzer = analyzer
            logger.info("   Vision analyzer ready")

        except Exception as e:
            logger.warning(f"Vision analyzer failed: {e}")

    async def _init_goal_inference(self):
        """Initialize goal inference system"""
        try:
            # This is typically loaded via dynamic components
            logger.info("   Goal inference ready (lazy)")

        except Exception as e:
            logger.warning(f"Goal inference failed: {e}")

    async def _init_uae_engine(self):
        """Initialize UAE (lazy loading enabled by default)"""
        try:
            # UAE uses lazy loading - just prepare config
            vision_analyzer = getattr(self.app.state, 'vision_analyzer', None)
            self.app.state.uae_lazy_config = {
                "vision_analyzer": vision_analyzer,
                "sai_monitoring_interval": 5.0,
                "enable_auto_start": True,
                "enable_learning_db": True,
                "enable_yabai": True,
                "enable_proactive_intelligence": True,
            }
            self.app.state.uae_engine = None  # Lazy load on first use
            logger.info("   UAE ready (lazy loading)")

        except Exception as e:
            logger.warning(f"UAE prep failed: {e}")

    async def _init_display_monitor(self):
        """Initialize display monitor"""
        try:
            # Typically loaded on demand
            logger.info("   Display monitor ready (lazy)")

        except Exception as e:
            logger.warning(f"Display monitor failed: {e}")

    async def _init_dynamic_components(self):
        """Initialize dynamic component manager"""
        try:
            from core.dynamic_component_manager import get_component_manager

            manager = get_component_manager()
            if manager:
                self.app.state.component_manager = manager
                # Track the monitoring task for proper cleanup
                if TASK_MANAGER_AVAILABLE:
                    task_mgr = get_task_manager()
                    await task_mgr.spawn_monitor(
                        "dynamic_component_monitor",
                        manager.start_monitoring()
                    )
                else:
                    monitor_task = asyncio.create_task(
                        manager.start_monitoring(),
                        name="dynamic_component_monitor"
                    )
                    self._tasks.append(monitor_task)
                logger.info("   Dynamic component manager ready")

        except ImportError:
            logger.debug("Dynamic component manager not available")
        except Exception as e:
            logger.warning(f"Dynamic components failed: {e}")

    async def _init_agentic_system(self):
        """
        Initialize the JARVIS Agentic System.

        This enables autonomous Computer Use capabilities by integrating:
        - Computer Use Tool (vision-based UI automation)
        - UAE action routing (intelligent element positioning)
        - Neural Mesh agent registration (multi-agent coordination)
        - Agentic configuration (dynamic, zero-hardcoding)

        The agentic system allows JARVIS to autonomously execute
        multi-step tasks using vision-based computer control.
        """
        try:
            logger.info("=" * 60)
            logger.info("AGENTIC SYSTEM INITIALIZATION")
            logger.info("=" * 60)

            # Step 1: Load agentic configuration
            try:
                from core.agentic_config import get_agentic_config
                agentic_config = get_agentic_config()
                self.app.state.agentic_config = agentic_config
                logger.info(f"   Agentic config loaded (debug={agentic_config.debug_mode})")
            except ImportError:
                logger.warning("   Agentic config not available - using defaults")
                agentic_config = None

            # Step 2: Initialize Computer Use Tool (LAZY - don't block startup)
            # Computer Use is heavy and can block. Initialize lazily on first use.
            self.app.state.computer_use_tool = None
            self.app.state.computer_use_lazy_config = {"config": agentic_config}
            logger.info("   Computer Use Tool ready (lazy load on first use)")

            # Step 3: Connect UAE to Computer Use (if both available)
            uae_engine = getattr(self.app.state, 'uae_engine', None)
            if uae_engine is None:
                # Try lazy initialization of UAE
                uae_lazy_config = getattr(self.app.state, 'uae_lazy_config', None)
                if uae_lazy_config:
                    try:
                        from intelligence.unified_awareness_engine import get_uae_engine
                        uae_engine = get_uae_engine(
                            vision_analyzer=uae_lazy_config.get('vision_analyzer')
                        )
                        self.app.state.uae_engine = uae_engine
                        logger.info("   UAE initialized for agentic routing")
                    except Exception as e:
                        logger.warning(f"   Could not initialize UAE: {e}")

            if uae_engine and self.app.state.computer_use_tool:
                logger.info("   UAE ↔ Computer Use routing enabled")
                self.app.state.agentic_routing_enabled = True
            else:
                self.app.state.agentic_routing_enabled = False

            # Step 4 & 5: Neural Mesh integration and workflow executor
            # These are heavy operations - defer to background or lazy load
            self.app.state.computer_use_agent = None
            self.app.state.agentic_workflow_executor = None
            self.app.state.neural_mesh_lazy_init_pending = True
            logger.info("   Neural Mesh agent & workflow executor ready (lazy)")

            # Step 6: Store status
            self.app.state.agentic_system = {
                "initialized": True,
                "config_available": agentic_config is not None,
                "computer_use_available": self.app.state.computer_use_tool is not None,
                "uae_routing_enabled": self.app.state.agentic_routing_enabled,
                "neural_mesh_agent": hasattr(self.app.state, 'computer_use_agent'),
                "workflow_executor": hasattr(self.app.state, 'agentic_workflow_executor'),
                "timestamp": time.time(),
            }

            logger.info("   ✅ Agentic System ready")
            logger.info("=" * 60)

        except Exception as e:
            logger.error(f"Agentic system initialization failed: {e}", exc_info=True)
            self.app.state.agentic_system = {
                "initialized": False,
                "error": str(e),
                "timestamp": time.time(),
            }
            # Don't raise - agentic system is not critical for basic operation

    # =========================================================================
    # Status and health
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """
        Get current initialization status.

        Returns format compatible with supervisor's check_system_status():
        - components.ready: LIST of component names (not counts!)
        - components.failed: LIST of failed component names
        - is_complete: Whether startup is fully complete
        - message: Current status message

        v2.0 Enhancements:
        - interactive_ready: True when user can interact (WebSocket + Voice API)
        - stale_components: List of components stuck in RUNNING too long
        - running_components: Components currently initializing with duration

        Also includes detailed component info for debugging.
        """
        elapsed = time.time() - self.started_at if self.started_at else 0

        # Get component name LISTS (not counts!) - this is what supervisor expects
        ready_components = [
            name for name, c in self.components.items()
            if c.phase == InitPhase.COMPLETE
        ]
        failed_components = [
            name for name, c in self.components.items()
            if c.phase == InitPhase.FAILED
        ]
        skipped_components = [
            name for name, c in self.components.items()
            if c.phase == InitPhase.SKIPPED
        ]
        pending_components = [
            name for name, c in self.components.items()
            if c.phase in (InitPhase.PENDING, InitPhase.RUNNING)
        ]
        # v2.0: Track stale and running components
        stale_components = [
            name for name, c in self.components.items()
            if c.is_stale
        ]
        running_components = {
            name: c.running_seconds
            for name, c in self.components.items()
            if c.phase == InitPhase.RUNNING and c.running_seconds is not None
        }

        # Map internal component names to supervisor-expected names
        # Supervisor expects: database, voice, vision, models, websocket, backend, agentic
        component_mapping = {
            # Database components
            "cloud_sql_proxy": "database",
            "learning_database": "database",
            # Voice components
            "speaker_verification": "voice",
            "voice_unlock_api": "voice",
            "jarvis_voice_api": "voice",
            # Vision components
            "vision_analyzer": "vision",
            "display_monitor": "vision",
            # ML/Models components
            "ml_engine_registry": "models",
            "cloud_ml_router": "models",
            "cloud_ecapa_client": "models",
            "vbi_prewarm": "models",
            "neural_mesh": "models",
            # WebSocket
            "unified_websocket": "websocket",
            # Agentic System (Computer Use + UAE routing)
            "agentic_system": "agentic",
        }

        # Build simplified component lists for supervisor
        simplified_ready = set()
        for comp_name in ready_components:
            mapped = component_mapping.get(comp_name)
            if mapped:
                simplified_ready.add(mapped)

        simplified_failed = set()
        for comp_name in failed_components:
            mapped = component_mapping.get(comp_name)
            if mapped:
                simplified_failed.add(mapped)

        # Determine if startup is complete
        is_complete = self._get_full_mode_event().is_set()
        # v2.0: Check for interactive readiness
        is_interactive = self._get_interactive_ready_event().is_set()

        # Generate status message
        if is_complete:
            message = "JARVIS startup complete!"
        elif is_interactive:
            message = f"JARVIS ready for interaction! ({len(pending_components)} components still loading)"
        elif failed_components:
            message = f"Startup in progress ({len(failed_components)} failures)"
        elif stale_components:
            message = f"Startup in progress ({len(stale_components)} components slow)"
        elif pending_components:
            message = f"Initializing {len(pending_components)} components..."
        else:
            message = "Starting up..."

        return {
            "phase": self.app.state.startup_phase,
            "progress": self.app.state.startup_progress,
            "message": message,
            "elapsed_seconds": elapsed,
            # Supervisor-compatible component format (LISTS of names!)
            "components": {
                "ready": list(simplified_ready),
                "failed": list(simplified_failed),
                # Also include counts for backwards compatibility
                "ready_count": len(ready_components),
                "failed_count": len(failed_components),
                "skipped_count": len(skipped_components),
                "pending_count": len(pending_components),
                "total": len(self.components),
            },
            "ready_for_requests": self._get_ready_event().is_set(),
            # v2.0: Interactive readiness (user can interact even if not full_mode)
            "interactive_ready": is_interactive,
            "full_mode": is_complete,
            "is_complete": is_complete,
            # v2.0: Stale component tracking
            "stale_components": stale_components,
            "running_components": running_components,
            # Detailed component info for debugging
            "components_detail": {
                name: {
                    "status": comp.phase.value,
                    "duration_ms": comp.duration_ms,
                    "error": comp.error,
                    "is_stale": comp.is_stale,
                    "running_seconds": comp.running_seconds,
                }
                for name, comp in self.components.items()
            },
            # Raw component lists for advanced debugging
            "internal_components": {
                "ready": ready_components,
                "failed": failed_components,
                "skipped": skipped_components,
                "pending": pending_components,
            },
            # AI Loader stats (if available)
            "ai_loader": self._get_ai_loader_status(),
        }

    def _get_ai_loader_status(self) -> Dict[str, Any]:
        """Get AI Loader status for health endpoint."""
        if not AI_LOADER_AVAILABLE:
            return {"available": False, "reason": "Not imported"}

        if not getattr(self.app.state, 'ai_loader_ready', False):
            return {"available": True, "initialized": False}

        try:
            ai_manager = getattr(self.app.state, 'ai_manager', None)
            if ai_manager is None:
                return {"available": True, "initialized": False}

            stats = ai_manager.get_stats()
            return {
                "available": True,
                "initialized": True,
                "models": {
                    "total": stats["summary"]["total"],
                    "ready": stats["summary"]["ready"],
                    "loading": stats["summary"]["loading"],
                    "failed": stats["summary"]["failed"],
                },
                "router": {
                    "engines_available": stats["router"]["available_count"],
                    "engines_total": stats["router"]["total_count"],
                },
                "memory_mb": stats["summary"]["total_memory_mb"],
            }
        except Exception as e:
            return {"available": True, "initialized": True, "error": str(e)}

    async def wait_for_full_mode(self, timeout: float = 300.0) -> bool:
        """Wait for FULL_MODE to be reached"""
        try:
            await asyncio.wait_for(self._get_full_mode_event().wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # =========================================================================
    # Shutdown
    # =========================================================================

    async def shutdown(self):
        """Clean shutdown of all components with proper task lifecycle management"""
        logger.info("=" * 60)
        logger.info("PARALLEL INITIALIZER SHUTDOWN")
        logger.info("=" * 60)

        self._get_shutdown_event().set()

        # Use TaskLifecycleManager if available for coordinated shutdown
        if TASK_MANAGER_AVAILABLE:
            try:
                task_mgr = get_task_manager()
                task_mgr.request_shutdown()  # Signal all managed tasks
                logger.info("TaskLifecycleManager shutdown signaled")
            except Exception as e:
                logger.warning(f"TaskLifecycleManager shutdown signal failed: {e}")

        # Cancel background initialization task
        if self.background_task and not self.background_task.done():
            logger.info("Cancelling background initialization task...")
            self.background_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self.background_task),
                    timeout=5.0
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        # Cancel all tracked tasks with timeout protection
        active_tasks = [t for t in self._tasks if not t.done()]
        if active_tasks:
            logger.info(f"Cancelling {len(active_tasks)} tracked tasks...")

            for task in active_tasks:
                task.cancel()

            # Wait for all with timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active_tasks, return_exceptions=True),
                    timeout=10.0
                )
                logger.info("All tracked tasks cancelled successfully")
            except asyncio.TimeoutError:
                logger.warning("Some tasks did not cancel within timeout")

        # If TaskLifecycleManager is available, do full shutdown
        if TASK_MANAGER_AVAILABLE:
            try:
                task_mgr = get_task_manager()
                result = await task_mgr.shutdown_all(timeout=15.0)
                logger.info(f"TaskLifecycleManager shutdown: {result.get('status', 'unknown')}")
            except Exception as e:
                logger.warning(f"TaskLifecycleManager shutdown error: {e}")

        # Shutdown AI Loader (unloads all models)
        if AI_LOADER_AVAILABLE:
            try:
                ai_manager = getattr(self.app.state, 'ai_manager', None)
                if ai_manager:
                    await ai_manager.shutdown()
                    logger.info("AI Loader shutdown complete")
            except Exception as e:
                logger.warning(f"AI Loader shutdown error: {e}")

        logger.info("Parallel initializer shutdown complete")
        logger.info("=" * 60)
