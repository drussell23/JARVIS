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
        # Phase 1: Critical infrastructure (parallel)
        self._add_component("config", priority=1, is_critical=True)
        self._add_component("cloud_sql_proxy", priority=10, dependencies=["config"])
        self._add_component("learning_database", priority=15, dependencies=["cloud_sql_proxy"])

        # Phase 2: ML Infrastructure (parallel, non-blocking)
        self._add_component("memory_aware_startup", priority=20)
        self._add_component("cloud_ml_router", priority=21, dependencies=["memory_aware_startup"])
        self._add_component("cloud_ecapa_client", priority=22)
        self._add_component("vbi_prewarm", priority=23, dependencies=["cloud_ecapa_client"])  # VBI pre-warming after ECAPA client
        self._add_component("vbi_health_monitor", priority=24)  # VBI health monitoring for operation tracking
        self._add_component("ml_engine_registry", priority=25)

        # Phase 3: Voice System (parallel)
        self._add_component("speaker_verification", priority=30, dependencies=["learning_database"])
        self._add_component("voice_unlock_api", priority=31, dependencies=["speaker_verification"])
        self._add_component("jarvis_voice_api", priority=32)  # JARVIS voice interface for frontend
        self._add_component("unified_websocket", priority=33)  # WebSocket for frontend communication

        # Phase 4: Intelligence Systems (parallel, can be slow)
        self._add_component("neural_mesh", priority=40)
        self._add_component("goal_inference", priority=40)
        self._add_component("uae_engine", priority=50)
        self._add_component("hybrid_orchestrator", priority=45)

        # Phase 5: Supporting services (parallel)
        self._add_component("vision_analyzer", priority=35)
        self._add_component("display_monitor", priority=40)
        self._add_component("dynamic_components", priority=60)

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
        """Initialize a single component"""
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

        try:
            # Dispatch to component-specific initializer
            initializer = getattr(self, f"_init_{name}", None)
            if initializer:
                await initializer()
            else:
                logger.debug(f"No initializer for {name}, marking complete")

            await self._mark_complete(name)

        except Exception as e:
            await self._mark_failed(name, str(e))

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
        """Initialize Cloud SQL proxy"""
        try:
            # Add backend dir to path
            backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if backend_dir not in sys.path:
                sys.path.insert(0, backend_dir)

            from intelligence.cloud_sql_proxy_manager import get_proxy_manager

            proxy_manager = get_proxy_manager()
            if not proxy_manager.is_running():
                await proxy_manager.start(force_restart=False)

            # Start health monitor
            asyncio.create_task(proxy_manager.monitor(check_interval=60))
            self.app.state.cloud_sql_proxy_manager = proxy_manager
            logger.info(f"   Cloud SQL proxy listening on 127.0.0.1:{proxy_manager.config['cloud_sql']['port']}")

        except Exception as e:
            logger.warning(f"Cloud SQL proxy not available: {e}")
            # Non-critical - will use SQLite fallback
            raise

    async def _init_learning_database(self):
        """Initialize learning database"""
        try:
            from intelligence.learning_database import JARVISLearningDatabase

            learning_db = JARVISLearningDatabase()
            await learning_db.initialize()
            self.app.state.learning_db = learning_db
            logger.info("   Learning database ready")

        except Exception as e:
            logger.warning(f"Learning database failed: {e}")
            raise

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
        Initialize VBI (Voice Biometric Intelligence) Pre-Warming

        This ensures ECAPA embedding extraction is ready BEFORE any unlock requests.
        Eliminates cold starts during 'unlock my screen' commands.
        """
        try:
            from core.vbi_debug_tracer import (
                prewarm_vbi_at_startup,
                get_prewarmer,
                get_orchestrator,
                get_tracer
            )

            logger.info("=" * 60)
            logger.info("VBI PRE-WARMING SEQUENCE")
            logger.info("=" * 60)

            # Initialize the VBI components
            prewarmer = get_prewarmer()
            orchestrator = get_orchestrator()
            tracer = get_tracer()

            # Store references in app state for access throughout the application
            self.app.state.vbi_prewarmer = prewarmer
            self.app.state.vbi_orchestrator = orchestrator
            self.app.state.vbi_tracer = tracer

            # Perform the pre-warm (this triggers Cloud ECAPA model loading)
            logger.info("   Starting ECAPA pre-warm (eliminates cold starts)...")

            # Set a generous timeout for initial pre-warm (model may need to load)
            prewarm_timeout = float(os.environ.get("VBI_PREWARM_TIMEOUT", "45"))

            try:
                prewarm_result = await asyncio.wait_for(
                    prewarmer.warmup(force=True),
                    timeout=prewarm_timeout
                )

                if prewarm_result.get("status") == "success":
                    warmup_ms = prewarm_result.get("total_duration_ms", 0)
                    logger.info(f"   ✅ VBI pre-warm COMPLETE in {warmup_ms:.0f}ms")
                    logger.info(f"   ✅ ECAPA is now HOT - no cold starts during unlock!")

                    # Store pre-warm status
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
                logger.warning(f"   ⚠️ VBI pre-warm timed out after {prewarm_timeout}s")
                logger.warning("   Voice unlock will work but may have initial delay")
                self.app.state.vbi_prewarm_status = {
                    "status": "timeout",
                    "timeout_seconds": prewarm_timeout,
                    "timestamp": time.time()
                }

            logger.info("=" * 60)

        except ImportError as e:
            logger.warning(f"VBI pre-warmer not available: {e}")
            logger.warning("Voice unlock will work but may have cold start delays")

        except Exception as e:
            logger.error(f"VBI pre-warm failed: {e}")
            logger.error(f"   This is non-fatal - voice unlock will still work")
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

            # Subscribe to health events for logging/alerting
            async def handle_health_event(event_type: str, data: dict):
                """Handle health events from VBI monitor"""
                if event_type == "operation_timeout":
                    op_id = data.get("operation_id", "unknown")
                    op_type = data.get("operation_type", "unknown")
                    component = data.get("component", "unknown")
                    elapsed = data.get("elapsed_seconds", 0)
                    logger.warning(
                        f"⚠️ VBI Operation Timeout: {op_type} ({op_id}) on {component} "
                        f"after {elapsed:.1f}s"
                    )
                elif event_type == "heartbeat_stale":
                    component = data.get("component", "unknown")
                    last_beat = data.get("last_heartbeat_seconds_ago", 0)
                    logger.warning(
                        f"⚠️ VBI Heartbeat Stale: {component} - no heartbeat for {last_beat:.1f}s"
                    )
                elif event_type == "circuit_opened":
                    component = data.get("component", "unknown")
                    failure_rate = data.get("failure_rate", 0)
                    logger.warning(
                        f"🔴 Circuit Breaker OPENED: {component} (failure rate: {failure_rate:.1%})"
                    )
                elif event_type == "circuit_closed":
                    component = data.get("component", "unknown")
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
        """Initialize speaker verification service"""
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

    # =========================================================================
    # Status and health
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get current initialization status"""
        elapsed = time.time() - self.started_at if self.started_at else 0

        ready_count = sum(1 for c in self.components.values() if c.phase == InitPhase.COMPLETE)
        failed_count = sum(1 for c in self.components.values() if c.phase == InitPhase.FAILED)
        pending_count = sum(1 for c in self.components.values() if c.phase in (InitPhase.PENDING, InitPhase.RUNNING))

        return {
            "phase": self.app.state.startup_phase,
            "progress": self.app.state.startup_progress,
            "elapsed_seconds": elapsed,
            "components": {
                "ready": ready_count,
                "failed": failed_count,
                "pending": pending_count,
                "total": len(self.components),
            },
            "ready_for_requests": self._ready_event.is_set(),
            "full_mode": self._full_mode_event.is_set(),
            "components_detail": {
                name: {
                    "status": comp.phase.value,
                    "duration_ms": comp.duration_ms,
                    "error": comp.error,
                }
                for name, comp in self.components.items()
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
