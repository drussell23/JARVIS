"""
JARVIS Parallel Initializer v1.0.0
==================================

Runs ALL heavy initialization as background tasks AFTER uvicorn starts serving.

Key Features:
- Server starts serving health endpoint IMMEDIATELY
- ML models, databases, neural mesh load in background
- Progress is tracked and reported via /health endpoint
- Graceful degradation if components fail

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
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

# Import the startup progress broadcaster for real-time WebSocket updates
from core.startup_progress_broadcaster import get_startup_broadcaster

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
    """Tracks a single component initialization"""
    name: str
    phase: InitPhase = InitPhase.PENDING
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    error: Optional[str] = None
    priority: int = 50  # 0-100, lower = earlier
    dependencies: List[str] = field(default_factory=list)
    is_critical: bool = False

    @property
    def duration_ms(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time) * 1000
        return None


class ParallelInitializer:
    """
    Manages parallel background initialization of JARVIS components.

    The key insight is that uvicorn should start serving immediately,
    and all heavy initialization should run in background tasks.
    """

    def __init__(self, app):
        self.app = app
        self.components: Dict[str, ComponentInit] = {}
        self.started_at: Optional[float] = None
        self.background_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._ready_event = asyncio.Event()
        self._full_mode_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()

        # Store references for cleanup
        self._tasks: List[asyncio.Task] = []

        # Register standard components
        self._register_components()

    def _register_components(self):
        """Register all JARVIS components with priorities"""
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
        self._add_component("config", priority=1, is_critical=True)
        # Cloud SQL proxy is independent - doesn't need config
        self._add_component("cloud_sql_proxy", priority=10)
        # Learning DB can retry if proxy not ready - doesn't block on proxy dependency
        self._add_component("learning_database", priority=12)

        # Phase 2: ML Infrastructure (parallel, non-blocking)
        # All these can start simultaneously
        self._add_component("memory_aware_startup", priority=20)
        if enable_spot_vm:
            self._add_component("gcp_vm_manager", priority=20)  # Same priority = parallel
        self._add_component("cloud_ml_router", priority=20)  # Removed memory_aware dependency
        if enable_cloud_ml or enable_spot_vm:
            self._add_component("cloud_ecapa_client", priority=20)  # Same priority = parallel
        # VBI runs in background - doesn't block anything
        if enable_vbi_prewarm:
            self._add_component("vbi_prewarm", priority=21)  # Slightly after ML infra
        self._add_component("vbi_health_monitor", priority=21)
        self._add_component("ml_engine_registry", priority=22)

        # Phase 3: Voice System (parallel - speaker_verification has internal retry logic)
        # Removed hard dependency on learning_database - it will retry if needed
        self._add_component("speaker_verification", priority=30)
        # Voice unlock API doesn't need speaker_verification to be fully ready
        self._add_component("voice_unlock_api", priority=30)
        self._add_component("jarvis_voice_api", priority=30)  # All voice at same priority
        self._add_component("unified_websocket", priority=30)  # WebSocket can start anytime

        # Phase 4: Intelligence Systems (parallel, can be slow but non-blocking)
        # All at same priority so they run truly in parallel
        self._add_component("neural_mesh", priority=40)
        self._add_component("goal_inference", priority=40)
        self._add_component("uae_engine", priority=40)  # Moved to same priority
        self._add_component("hybrid_orchestrator", priority=40)
        self._add_component("vision_analyzer", priority=40)  # Moved here
        self._add_component("display_monitor", priority=40)

        # Phase 5: Supporting services (parallel)
        self._add_component("dynamic_components", priority=50)

        # Phase 6: Agentic System (soft dependencies - will work even if deps not ready)
        # Removed hard dependencies - agentic system has fallback logic
        self._add_component("agentic_system", priority=55)

    def _add_component(
        self,
        name: str,
        priority: int = 50,
        is_critical: bool = False,
        dependencies: List[str] = None
    ):
        """Add a component to track"""
        self.components[name] = ComponentInit(
            name=name,
            priority=priority,
            is_critical=is_critical,
            dependencies=dependencies or []
        )

    async def minimal_setup(self):
        """
        Minimal setup that runs BEFORE yield.
        This should complete in <1 second.
        """
        self.started_at = time.time()
        logger.info("=" * 60)
        logger.info("JARVIS Parallel Startup v1.0.0")
        logger.info("=" * 60)

        # Initialize app state FIRST before marking any components
        self.app.state.parallel_initializer = self
        self.app.state.startup_phase = "STARTING"
        self.app.state.startup_progress = 0.0
        self.app.state.components_ready = set()
        self.app.state.components_failed = set()

        # Mark config as complete (it's just loading env vars)
        await self._mark_complete("config")

        # Server is ready for basic health checks
        self._ready_event.set()
        logger.info("Server ready for health checks")

        # Launch background initialization
        self.background_task = asyncio.create_task(
            self._background_initialization(),
            name="parallel_init"
        )
        self._tasks.append(self.background_task)

    async def _background_initialization(self):
        """
        Background task that initializes all heavy components.
        This runs AFTER the server starts serving requests.
        """
        logger.info("")
        logger.info("=" * 60)
        logger.info("Background Initialization Starting...")
        logger.info("=" * 60)

        self.app.state.startup_phase = "INITIALIZING"

        try:
            # Group components by priority
            priority_groups = self._group_by_priority()

            # Initialize each priority group
            for priority, group in sorted(priority_groups.items()):
                if self._shutdown_event.is_set():
                    break

                logger.info(f"Initializing priority {priority} components: {[c.name for c in group]}")

                # Run group in parallel
                tasks = []
                for comp in group:
                    # Check dependencies
                    deps_ready = all(
                        self.components.get(d, ComponentInit(name=d)).phase == InitPhase.COMPLETE
                        for d in comp.dependencies
                    )
                    if deps_ready:
                        tasks.append(self._init_component(comp.name))
                    else:
                        logger.warning(f"Skipping {comp.name} - dependencies not ready: {comp.dependencies}")
                        await self._mark_skipped(comp.name, "Dependencies not ready")

                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

                # Update progress
                self._update_progress()

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
                self._full_mode_event.set()
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

    async def _init_component(self, name: str):
        """
        Initialize a single component with timeout protection and graceful degradation.

        v2.0 Enhancements:
        - Per-component timeout protection (60s default, 120s for heavy components)
        - Graceful degradation for non-critical components
        - Better error context and logging
        """
        comp = self.components.get(name)
        if not comp:
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

            # Broadcast completion via WebSocket
            broadcaster = get_startup_broadcaster()
            await broadcaster.broadcast_component_complete(
                component=name,
                message=f"{name.replace('_', ' ').title()} ready",
                duration_ms=duration_ms
            )

    async def _mark_failed(self, name: str, error: str):
        """Mark a component as failed"""
        comp = self.components.get(name)
        if comp:
            comp.phase = InitPhase.FAILED
            comp.end_time = time.time()
            comp.error = error
            self.app.state.components_failed.add(name)
            logger.warning(f"[FAILED] {name}: {error}")

            # Broadcast failure via WebSocket
            broadcaster = get_startup_broadcaster()
            await broadcaster.broadcast_component_failed(
                component=name,
                error=error,
                is_critical=comp.is_critical
            )

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

    async def _init_cloud_sql_proxy(self):
        """
        Initialize Cloud SQL proxy with non-blocking startup and graceful degradation.

        v2.0 Enhancements:
        - Non-blocking proxy startup (starts in background, doesn't wait for full readiness)
        - Graceful degradation to SQLite if proxy fails
        - Marks connection manager as "starting up" to suppress noisy errors
        - Signals readiness to connection manager when proxy is confirmed running
        """
        try:
            # Add backend dir to path
            backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if backend_dir not in sys.path:
                sys.path.insert(0, backend_dir)

            from intelligence.cloud_sql_proxy_manager import get_proxy_manager
            from intelligence.cloud_sql_connection_manager import get_connection_manager

            # Get connection manager and signal we're in startup mode
            conn_mgr = get_connection_manager()
            conn_mgr.set_proxy_ready(False)  # Suppress connection errors during startup

            proxy_manager = get_proxy_manager()
            if not proxy_manager.is_running():
                # Start proxy asynchronously with timeout protection
                try:
                    await asyncio.wait_for(
                        proxy_manager.start(force_restart=False),
                        timeout=30.0  # 30s max for proxy startup
                    )
                except asyncio.TimeoutError:
                    logger.warning("⚠️ Cloud SQL proxy startup timeout (30s) - will retry in background")
                    # Continue anyway - proxy might still be starting
                    pass

            # Start health monitor in background (non-blocking)
            asyncio.create_task(proxy_manager.monitor(check_interval=60))

            # Store in app state
            self.app.state.cloud_sql_proxy_manager = proxy_manager

            # Give proxy a moment to fully initialize, then signal readiness
            async def signal_proxy_ready():
                """Signal to connection manager that proxy is ready after brief delay"""
                await asyncio.sleep(2.0)  # Give proxy time to fully initialize
                if proxy_manager.is_running():
                    conn_mgr.set_proxy_ready(True)
                    logger.info(f"   ✅ Cloud SQL proxy confirmed ready on 127.0.0.1:{proxy_manager.config['cloud_sql']['port']}")
                else:
                    logger.warning("   ⚠️ Cloud SQL proxy not confirmed running - will use fallback")

            # Run readiness signal in background (don't block startup)
            asyncio.create_task(signal_proxy_ready())

            logger.info(f"   Cloud SQL proxy starting on 127.0.0.1:{proxy_manager.config['cloud_sql']['port']}")

        except Exception as e:
            logger.warning(f"⚠️ Cloud SQL proxy initialization failed: {e}")
            logger.info("   System will use SQLite fallback for local storage")
            # Don't raise - proxy is non-critical, we have SQLite fallback
            # Mark as skipped rather than failed
            pass

    async def _init_learning_database(self):
        """
        Initialize learning database with graceful degradation and retry logic.

        v2.0 Enhancements:
        - Retries initialization if proxy isn't ready yet (common during startup)
        - Gracefully falls back to SQLite if CloudSQL unavailable
        - Non-blocking initialization with timeout protection (handled by _init_component)
        """
        try:
            from intelligence.learning_database import JARVISLearningDatabase

            learning_db = JARVISLearningDatabase()

            # Try initialization with retries (proxy might still be starting)
            max_retries = 3
            retry_delay = 2.0

            for attempt in range(max_retries):
                try:
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

            startup_decision = await determine_startup_mode()
            self.app.state.startup_decision = startup_decision

            logger.info(f"   Memory mode: {startup_decision.mode.value}")
            logger.info(f"   Reason: {startup_decision.reason}")

            if startup_decision.gcp_vm_required:
                cloud_result = await activate_cloud_ml_if_needed(startup_decision)
                self.app.state.cloud_ml_result = cloud_result

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

            # Launch background pre-warm task (fire and forget)
            asyncio.create_task(background_prewarm())

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
            _event_state = {"stale_components": set(), "open_circuits": set()}
            
            # Subscribe to health events for logging/alerting
            async def handle_health_event(event_type: str, data: dict):
                """Handle health events from VBI monitor.
                
                Only logs state CHANGES to avoid spam. Events that repeat
                (like stale heartbeats) are only logged once per component.
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
                    # Only log NEW stale components
                    component = data.get("component", "unknown")
                    if component not in _event_state["stale_components"]:
                        _event_state["stale_components"].add(component)
                        last_beat = data.get("last_heartbeat_seconds_ago", 0)
                        logger.warning(
                            f"⚠️ VBI Heartbeat Stale: {component} - no heartbeat for {last_beat:.1f}s"
                        )
                elif event_type == "heartbeat_received":
                    # Clear stale state when heartbeat is received
                    component = data.get("component", "unknown")
                    _event_state["stale_components"].discard(component)
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
        """Initialize hybrid orchestrator"""
        try:
            from core.hybrid_orchestrator import get_orchestrator

            orchestrator = get_orchestrator()
            await orchestrator.start()
            self.app.state.hybrid_orchestrator = orchestrator
            logger.info("   Hybrid orchestrator ready")

        except Exception as e:
            logger.warning(f"Hybrid orchestrator failed: {e}")

    async def _init_vision_analyzer(self):
        """Initialize vision analyzer"""
        try:
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                logger.info("   Vision analyzer skipped (no API key)")
                return

            from vision.claude_vision_analyzer import ClaudeVisionAnalyzer

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
                asyncio.create_task(manager.start_monitoring())
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

            # Step 2: Initialize Computer Use Tool
            try:
                from autonomy.computer_use_tool import get_computer_use_tool

                # Get TTS callback if available
                tts_callback = None
                if hasattr(self.app.state, 'vbi_orchestrator'):
                    vbi = self.app.state.vbi_orchestrator
                    if hasattr(vbi, 'speak'):
                        tts_callback = vbi.speak

                computer_use_tool = get_computer_use_tool(
                    tts_callback=tts_callback,
                    config=agentic_config,
                )
                self.app.state.computer_use_tool = computer_use_tool
                logger.info("   Computer Use Tool initialized")

            except ImportError as e:
                logger.warning(f"   Computer Use Tool not available: {e}")
                self.app.state.computer_use_tool = None

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

            # Step 4: Register Computer Use agent with Neural Mesh
            neural_mesh = getattr(self.app.state, 'neural_mesh', None)
            if neural_mesh:
                try:
                    from autonomy.neural_mesh_integration import register_computer_use_agent

                    agent = await register_computer_use_agent(
                        tts_callback=tts_callback
                    )
                    if agent:
                        self.app.state.computer_use_agent = agent
                        logger.info("   Computer Use agent registered with Neural Mesh")

                except ImportError:
                    logger.debug("   Neural Mesh integration not available")
                except Exception as e:
                    logger.warning(f"   Neural Mesh agent registration failed: {e}")

            # Step 5: Initialize workflow executor
            try:
                from autonomy.neural_mesh_integration import get_workflow_executor

                executor = get_workflow_executor(
                    neural_mesh=neural_mesh.get('coordinator') if isinstance(neural_mesh, dict) else None,
                    tts_callback=tts_callback,
                )
                await executor.initialize()
                self.app.state.agentic_workflow_executor = executor
                logger.info("   Agentic workflow executor ready")

            except ImportError:
                logger.debug("   Workflow executor not available")
            except Exception as e:
                logger.warning(f"   Workflow executor failed: {e}")

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
        pending_components = [
            name for name, c in self.components.items()
            if c.phase in (InitPhase.PENDING, InitPhase.RUNNING)
        ]

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
        is_complete = self._full_mode_event.is_set()

        # Generate status message
        if is_complete:
            message = "JARVIS startup complete!"
        elif failed_components:
            message = f"Startup in progress ({len(failed_components)} failures)"
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
                "pending_count": len(pending_components),
                "total": len(self.components),
            },
            "ready_for_requests": self._ready_event.is_set(),
            "full_mode": is_complete,
            "is_complete": is_complete,
            # Detailed component info for debugging
            "components_detail": {
                name: {
                    "status": comp.phase.value,
                    "duration_ms": comp.duration_ms,
                    "error": comp.error,
                }
                for name, comp in self.components.items()
            },
            # Raw component lists for advanced debugging
            "internal_components": {
                "ready": ready_components,
                "failed": failed_components,
                "pending": pending_components,
            }
        }

    async def wait_for_full_mode(self, timeout: float = 300.0) -> bool:
        """Wait for FULL_MODE to be reached"""
        try:
            await asyncio.wait_for(self._full_mode_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # =========================================================================
    # Shutdown
    # =========================================================================

    async def shutdown(self):
        """Clean shutdown of all components"""
        logger.info("Shutting down parallel initializer...")
        self._shutdown_event.set()

        # Cancel background task
        if self.background_task and not self.background_task.done():
            self.background_task.cancel()
            try:
                await self.background_task
            except asyncio.CancelledError:
                pass

        # Cancel all tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        logger.info("Parallel initializer shutdown complete")
