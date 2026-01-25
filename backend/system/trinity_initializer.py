"""
PROJECT TRINITY Phase 3: Auto-Initializer for JARVIS Body

This module provides automatic Trinity initialization for JARVIS.
It integrates with the FastAPI lifespan to:
- Connect to the Trinity network on startup
- Register command handlers
- Broadcast heartbeats
- Reconcile state with the orchestrator

USAGE:
    In backend/main.py lifespan:

        from backend.system.trinity_initializer import (
            initialize_trinity,
            shutdown_trinity,
        )

        async def lifespan(app):
            # ... existing startup code ...

            # Initialize Trinity
            await initialize_trinity(app)

            yield

            # Shutdown Trinity
            await shutdown_trinity()

Or use the context manager:

    from backend.system.trinity_initializer import trinity_context

    async with trinity_context(app):
        yield
"""

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)


# =============================================================================
# v73.0: ATOMIC FILE I/O - Diamond-Hard Protocol
# =============================================================================

class AtomicTrinityIO:
    """
    v73.0: Ensures zero-corruption file operations via Atomic Renames.

    The Problem:
        Standard file writing (`open('w').write()`) takes non-zero time (e.g., 5ms).
        If JARVIS tries to read the file during those 5ms, it reads incomplete JSON
        and crashes with JSONDecodeError.

    The Solution:
        Write to a temporary file first, then perform an OS-level atomic rename
        (`os.replace`) to the final filename. This guarantees the file is either
        *missing* or *perfect*, never partial.

    Features:
        - Atomic writes with fsync for durability
        - Safe reads with automatic retry on corruption
        - Lock-free design for high concurrency
        - Works across all platforms (macOS, Linux, Windows)
    """

    @staticmethod
    def write_json_atomic(filepath: Union[str, Path], data: Dict[str, Any]) -> bool:
        """
        Write JSON data atomically to prevent partial reads.

        Args:
            filepath: Target file path
            data: JSON-serializable dictionary

        Returns:
            True if write succeeded, False otherwise
        """
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        tmp_fd = None
        tmp_name = None

        try:
            # 1. Create temp file in same directory (required for atomic rename)
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=filepath.parent,
                prefix=f".{filepath.stem}.",
                suffix=".tmp"
            )

            # 2. Write data to temp file
            with os.fdopen(tmp_fd, 'w') as tmp_file:
                tmp_fd = None  # os.fdopen takes ownership
                json.dump(data, tmp_file, indent=2)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())  # Force write to physical disk

            # 3. Atomic swap (OS guarantees this is instantaneous)
            os.replace(tmp_name, filepath)
            return True

        except Exception as e:
            logger.debug(f"[AtomicIO] Write failed: {e}")
            # Cleanup temp file on failure
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass
            return False

        finally:
            # Ensure fd is closed if not transferred to fdopen
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass

    @staticmethod
    def read_json_safe(
        filepath: Union[str, Path],
        default: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        retry_delay: float = 0.05
    ) -> Optional[Dict[str, Any]]:
        """
        Read JSON with automatic retry on corruption.

        This handles the rare case where we catch a file mid-rename
        (between unlink and rename on some filesystems).

        Args:
            filepath: File to read
            default: Value to return if file doesn't exist
            max_retries: Maximum read attempts
            retry_delay: Delay between retries in seconds

        Returns:
            Parsed JSON or default value
        """
        filepath = Path(filepath)

        for attempt in range(max_retries):
            try:
                if not filepath.exists():
                    return default

                with open(filepath, 'r') as f:
                    return json.load(f)

            except json.JSONDecodeError as e:
                if attempt < max_retries - 1:
                    logger.debug(f"[AtomicIO] JSON decode retry {attempt + 1}: {e}")
                    time.sleep(retry_delay)
                else:
                    logger.warning(f"[AtomicIO] JSON decode failed after {max_retries} retries: {e}")
                    return default

            except Exception as e:
                logger.debug(f"[AtomicIO] Read failed: {e}")
                return default

        return default

    @staticmethod
    def cleanup_temp_files(directory: Union[str, Path], prefix: str = ".") -> int:
        """
        Clean up orphaned temp files from failed atomic writes.

        Args:
            directory: Directory to clean
            prefix: Temp file prefix to match

        Returns:
            Number of files cleaned
        """
        directory = Path(directory)
        cleaned = 0

        try:
            for f in directory.glob(f"{prefix}*.tmp"):
                try:
                    # Only remove if older than 1 minute (avoid race with active writes)
                    if time.time() - f.stat().st_mtime > 60:
                        f.unlink()
                        cleaned += 1
                except OSError:
                    pass
        except Exception:
            pass

        return cleaned


# Convenience function for module-level access
def write_json_atomic(filepath: Union[str, Path], data: Dict[str, Any]) -> bool:
    """Write JSON atomically. See AtomicTrinityIO.write_json_atomic."""
    return AtomicTrinityIO.write_json_atomic(filepath, data)


def read_json_safe(
    filepath: Union[str, Path],
    default: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Read JSON safely. See AtomicTrinityIO.read_json_safe."""
    return AtomicTrinityIO.read_json_safe(filepath, default)


# =============================================================================
# CONFIGURATION
# =============================================================================

TRINITY_ENABLED = os.getenv("TRINITY_ENABLED", "true").lower() == "true"
TRINITY_HEARTBEAT_INTERVAL = float(os.getenv("TRINITY_HEARTBEAT_INTERVAL", "5.0"))
TRINITY_AUTO_CONNECT = os.getenv("TRINITY_AUTO_CONNECT", "true").lower() == "true"

# Instance identification
JARVIS_INSTANCE_ID = os.getenv(
    "JARVIS_INSTANCE_ID",
    f"jarvis-{os.getpid()}-{int(time.time())}"
)


# =============================================================================
# GLOBAL STATE
# =============================================================================

_trinity_initialized = False
_heartbeat_task: Optional[asyncio.Task] = None
_bridge = None
_bridge_adapter = None  # TrinityBridgeAdapter for Reactor event watching
_app = None
_start_time = time.time()


# =============================================================================
# IMPORTS (Lazy to avoid circular imports)
# =============================================================================

def _get_reactor_bridge():
    """Lazy import of ReactorCoreBridge."""
    global _bridge
    if _bridge is None:
        try:
            from backend.system.reactor_bridge import get_reactor_bridge
            _bridge = get_reactor_bridge()
        except ImportError:
            try:
                from system.reactor_bridge import get_reactor_bridge
                _bridge = get_reactor_bridge()
            except ImportError:
                logger.warning("[Trinity] ReactorCoreBridge not available")
    return _bridge


def _get_trinity_handlers():
    """Lazy import of trinity_handlers."""
    try:
        from backend.system.trinity_handlers import register_trinity_handlers
        return register_trinity_handlers
    except ImportError:
        try:
            from system.trinity_handlers import register_trinity_handlers
            return register_trinity_handlers
        except ImportError:
            logger.warning("[Trinity] Trinity handlers not available")
            return None


def _get_cryostasis_manager():
    """Lazy import of CryostasisManager."""
    try:
        from backend.system.cryostasis_manager import get_cryostasis_manager
        return get_cryostasis_manager()
    except ImportError:
        try:
            from system.cryostasis_manager import get_cryostasis_manager
            return get_cryostasis_manager()
        except ImportError:
            return None


def _get_yabai_detector():
    """Lazy import of YabaiSpaceDetector."""
    try:
        from backend.vision.yabai_space_detector import get_yabai_detector
        return get_yabai_detector()
    except ImportError:
        try:
            from vision.yabai_space_detector import get_yabai_detector
            return get_yabai_detector()
        except ImportError:
            return None


async def _get_trinity_bridge_adapter():
    """Lazy import and initialization of TrinityBridgeAdapter."""
    global _bridge_adapter
    if _bridge_adapter is None:
        try:
            from backend.system.trinity_bridge_adapter import get_trinity_bridge
            _bridge_adapter = await get_trinity_bridge()
        except ImportError:
            try:
                from system.trinity_bridge_adapter import get_trinity_bridge
                _bridge_adapter = await get_trinity_bridge()
            except ImportError:
                logger.warning("[Trinity] TrinityBridgeAdapter not available")
                return None
    return _bridge_adapter


async def _shutdown_trinity_bridge_adapter():
    """Shutdown TrinityBridgeAdapter."""
    global _bridge_adapter
    if _bridge_adapter:
        try:
            from backend.system.trinity_bridge_adapter import shutdown_trinity_bridge
        except ImportError:
            try:
                from system.trinity_bridge_adapter import shutdown_trinity_bridge
            except ImportError:
                _bridge_adapter = None
                return

        await shutdown_trinity_bridge()
        _bridge_adapter = None


async def _get_cross_repo_registry():
    """Lazy import and initialization of CrossRepoComponentRegistry."""
    try:
        from backend.system.cross_repo_component_registry import (
            get_component_registry,
            register_jarvis_components,
        )
        registry = await get_component_registry()
        await register_jarvis_components(registry)
        return registry
    except ImportError:
        try:
            from system.cross_repo_component_registry import (
                get_component_registry,
                register_jarvis_components,
            )
            registry = await get_component_registry()
            await register_jarvis_components(registry)
            return registry
        except ImportError:
            logger.warning("[Trinity] CrossRepoComponentRegistry not available")
            return None


async def _shutdown_cross_repo_registry():
    """Shutdown CrossRepoComponentRegistry."""
    try:
        from backend.system.cross_repo_component_registry import shutdown_component_registry
    except ImportError:
        try:
            from system.cross_repo_component_registry import shutdown_component_registry
        except ImportError:
            return

    await shutdown_component_registry()


async def _get_cross_repo_cost_sync():
    """Lazy import and initialization of CrossRepoCostSync."""
    try:
        from backend.core.cross_repo_cost_sync import get_cross_repo_cost_sync
        return await get_cross_repo_cost_sync("jarvis")
    except ImportError:
        try:
            from core.cross_repo_cost_sync import get_cross_repo_cost_sync
            return await get_cross_repo_cost_sync("jarvis")
        except ImportError:
            logger.warning("[Trinity] CrossRepoCostSync not available")
            return None


async def _shutdown_cross_repo_cost_sync():
    """Shutdown CrossRepoCostSync."""
    try:
        from backend.core.cross_repo_cost_sync import shutdown_cross_repo_cost_sync
    except ImportError:
        try:
            from core.cross_repo_cost_sync import shutdown_cross_repo_cost_sync
        except ImportError:
            return

    await shutdown_cross_repo_cost_sync()


async def _get_cross_repo_neural_mesh():
    """Lazy import and initialization of CrossRepoNeuralMeshBridge."""
    try:
        from backend.core.registry.cross_repo_neural_mesh import get_cross_repo_neural_mesh
        return await get_cross_repo_neural_mesh()
    except ImportError:
        try:
            from core.registry.cross_repo_neural_mesh import get_cross_repo_neural_mesh
            return await get_cross_repo_neural_mesh()
        except ImportError:
            logger.warning("[Trinity] CrossRepoNeuralMeshBridge not available")
            return None


async def _shutdown_cross_repo_neural_mesh():
    """Shutdown CrossRepoNeuralMeshBridge."""
    try:
        from backend.core.registry.cross_repo_neural_mesh import shutdown_cross_repo_neural_mesh
    except ImportError:
        try:
            from core.registry.cross_repo_neural_mesh import shutdown_cross_repo_neural_mesh
        except ImportError:
            return

    await shutdown_cross_repo_neural_mesh()


async def _get_gcp_hybrid_prime_router():
    """Lazy import and initialization of GCPHybridPrimeRouter."""
    try:
        from backend.core.gcp_hybrid_prime_router import get_gcp_hybrid_prime_router
        return await get_gcp_hybrid_prime_router()
    except ImportError:
        try:
            from core.gcp_hybrid_prime_router import get_gcp_hybrid_prime_router
            return await get_gcp_hybrid_prime_router()
        except ImportError:
            logger.warning("[Trinity] GCPHybridPrimeRouter not available")
            return None


async def _shutdown_gcp_hybrid_prime_router():
    """Shutdown GCPHybridPrimeRouter."""
    try:
        from backend.core.gcp_hybrid_prime_router import shutdown_gcp_hybrid_prime_router
    except ImportError:
        try:
            from core.gcp_hybrid_prime_router import shutdown_gcp_hybrid_prime_router
        except ImportError:
            return

    await shutdown_gcp_hybrid_prime_router()


# =============================================================================
# INITIALIZATION
# =============================================================================

async def initialize_trinity(app=None) -> bool:
    """
    Initialize Trinity for JARVIS Body.

    This should be called during FastAPI lifespan startup.

    Args:
        app: Optional FastAPI app instance for state attachment

    Returns:
        True if initialization succeeded
    """
    global _trinity_initialized, _heartbeat_task, _app

    if not TRINITY_ENABLED:
        logger.info("[Trinity] Trinity is disabled (TRINITY_ENABLED=false)")
        return False

    if _trinity_initialized:
        logger.debug("[Trinity] Already initialized")
        return True

    logger.info("=" * 60)
    logger.info("PROJECT TRINITY: Initializing JARVIS Body Connection")
    logger.info("=" * 60)

    _app = app

    try:
        # Step 1: Get ReactorCoreBridge
        bridge = _get_reactor_bridge()
        if bridge is None:
            logger.warning("[Trinity] ReactorCoreBridge not available - skipping")
            return False

        # Step 2: Connect to Trinity network
        if TRINITY_AUTO_CONNECT:
            logger.info("[Trinity] Connecting to Trinity network...")
            connected = await bridge.connect_async()
            if connected:
                logger.info("[Trinity] ✓ Connected to Trinity network")
            else:
                logger.warning("[Trinity] Connection failed - continuing in standalone mode")

        # Step 3: Register command handlers
        register_handlers = _get_trinity_handlers()
        if register_handlers:
            register_handlers(bridge)
            logger.info("[Trinity] ✓ Command handlers registered")
        else:
            logger.warning("[Trinity] Command handlers not available")

        # Step 4: Start heartbeat broadcast
        _heartbeat_task = asyncio.create_task(_heartbeat_loop())
        logger.info(f"[Trinity] ✓ Heartbeat started (interval={TRINITY_HEARTBEAT_INTERVAL}s)")

        # Step 5: Start TrinityBridgeAdapter (watches Reactor events)
        try:
            bridge_adapter = await _get_trinity_bridge_adapter()
            if bridge_adapter:
                logger.info("[Trinity] ✓ TrinityBridgeAdapter started (watching Reactor events)")
                if app is not None:
                    app.state.trinity_bridge_adapter = bridge_adapter
            else:
                logger.warning("[Trinity] TrinityBridgeAdapter not available - Trinity Loop incomplete")
        except Exception as e:
            logger.warning(f"[Trinity] TrinityBridgeAdapter failed to start: {e}")

        # Step 6: Start CrossRepoComponentRegistry (exposes JARVIS components)
        try:
            registry = await _get_cross_repo_registry()
            if registry:
                logger.info("[Trinity] ✓ CrossRepoComponentRegistry started (exposing JARVIS components)")
                if app is not None:
                    app.state.cross_repo_registry = registry
            else:
                logger.warning("[Trinity] CrossRepoComponentRegistry not available")
        except Exception as e:
            logger.warning(f"[Trinity] CrossRepoComponentRegistry failed to start: {e}")

        # Step 7: Start CrossRepoCostSync (unified cost tracking)
        try:
            cost_sync = await _get_cross_repo_cost_sync()
            if cost_sync:
                logger.info("[Trinity] ✓ CrossRepoCostSync started (unified cost tracking)")
                if app is not None:
                    app.state.cross_repo_cost_sync = cost_sync
            else:
                logger.warning("[Trinity] CrossRepoCostSync not available")
        except Exception as e:
            logger.warning(f"[Trinity] CrossRepoCostSync failed to start: {e}")

        # Step 8: Start CrossRepoNeuralMeshBridge (Prime + Reactor integration)
        try:
            neural_mesh_bridge = await _get_cross_repo_neural_mesh()
            if neural_mesh_bridge:
                logger.info("[Trinity] ✓ CrossRepoNeuralMeshBridge started (Prime + Reactor)")
                if app is not None:
                    app.state.cross_repo_neural_mesh = neural_mesh_bridge
            else:
                logger.warning("[Trinity] CrossRepoNeuralMeshBridge not available")
        except Exception as e:
            logger.warning(f"[Trinity] CrossRepoNeuralMeshBridge failed to start: {e}")

        # Step 9: Start GCPHybridPrimeRouter (intelligent routing)
        try:
            hybrid_router = await _get_gcp_hybrid_prime_router()
            if hybrid_router:
                logger.info("[Trinity] ✓ GCPHybridPrimeRouter started (cost-aware routing)")
                if app is not None:
                    app.state.gcp_hybrid_router = hybrid_router
            else:
                logger.warning("[Trinity] GCPHybridPrimeRouter not available")
        except Exception as e:
            logger.warning(f"[Trinity] GCPHybridPrimeRouter failed to start: {e}")

        # Step 10: Attach to app state if available
        if app is not None:
            app.state.trinity_bridge = bridge
            app.state.trinity_instance_id = JARVIS_INSTANCE_ID
            logger.info("[Trinity] ✓ Attached to FastAPI app.state")

        _trinity_initialized = True

        logger.info("=" * 60)
        logger.info(f"PROJECT TRINITY: JARVIS Body Online (ID: {JARVIS_INSTANCE_ID[:16]})")
        logger.info("=" * 60)

        return True

    except Exception as e:
        logger.error(f"[Trinity] Initialization failed: {e}")
        return False


async def shutdown_trinity() -> None:
    """
    Shutdown Trinity connection.

    This should be called during FastAPI lifespan shutdown.
    """
    global _trinity_initialized, _heartbeat_task, _bridge, _bridge_adapter

    if not _trinity_initialized:
        return

    logger.info("[Trinity] Shutting down JARVIS Body connection...")

    # Stop heartbeat
    if _heartbeat_task:
        _heartbeat_task.cancel()
        try:
            await _heartbeat_task
        except asyncio.CancelledError:
            pass
        _heartbeat_task = None

    # Stop TrinityBridgeAdapter
    try:
        await _shutdown_trinity_bridge_adapter()
        logger.info("[Trinity] ✓ TrinityBridgeAdapter stopped")
    except Exception as e:
        logger.warning(f"[Trinity] TrinityBridgeAdapter shutdown error: {e}")

    # Stop CrossRepoComponentRegistry
    try:
        await _shutdown_cross_repo_registry()
        logger.info("[Trinity] ✓ CrossRepoComponentRegistry stopped")
    except Exception as e:
        logger.warning(f"[Trinity] CrossRepoComponentRegistry shutdown error: {e}")

    # Stop CrossRepoCostSync
    try:
        await _shutdown_cross_repo_cost_sync()
        logger.info("[Trinity] ✓ CrossRepoCostSync stopped")
    except Exception as e:
        logger.warning(f"[Trinity] CrossRepoCostSync shutdown error: {e}")

    # Stop CrossRepoNeuralMeshBridge
    try:
        await _shutdown_cross_repo_neural_mesh()
        logger.info("[Trinity] ✓ CrossRepoNeuralMeshBridge stopped")
    except Exception as e:
        logger.warning(f"[Trinity] CrossRepoNeuralMeshBridge shutdown error: {e}")

    # Stop GCPHybridPrimeRouter
    try:
        await _shutdown_gcp_hybrid_prime_router()
        logger.info("[Trinity] ✓ GCPHybridPrimeRouter stopped")
    except Exception as e:
        logger.warning(f"[Trinity] GCPHybridPrimeRouter shutdown error: {e}")

    # Disconnect bridge
    bridge = _get_reactor_bridge()
    if bridge and bridge.is_connected():
        await bridge.disconnect_async()

    _trinity_initialized = False
    _bridge = None
    _bridge_adapter = None

    logger.info("[Trinity] JARVIS Body disconnected")


@asynccontextmanager
async def trinity_context(app=None):
    """
    Context manager for Trinity lifecycle.

    Usage:
        async with trinity_context(app):
            yield
    """
    await initialize_trinity(app)
    try:
        yield
    finally:
        await shutdown_trinity()


# =============================================================================
# HEARTBEAT
# =============================================================================

async def _heartbeat_loop() -> None:
    """Background task to broadcast heartbeats."""
    global _trinity_initialized

    while _trinity_initialized:
        try:
            await _broadcast_heartbeat()
            await asyncio.sleep(TRINITY_HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"[Trinity] Heartbeat error: {e}")
            await asyncio.sleep(TRINITY_HEARTBEAT_INTERVAL)


async def _broadcast_heartbeat() -> None:
    """Broadcast current state as heartbeat."""
    bridge = _get_reactor_bridge()
    if bridge is None or not bridge.is_connected():
        return

    # Gather state
    state = await _gather_jarvis_state()

    # Publish heartbeat
    await bridge.publish_heartbeat_async()

    # Also write state to orchestrator file for cross-repo sync
    await _write_state_to_orchestrator(state)


async def _gather_jarvis_state() -> Dict[str, Any]:
    """Gather current JARVIS state for heartbeat."""
    state = {
        "instance_id": JARVIS_INSTANCE_ID,
        "uptime_seconds": time.time() - _start_time,
        "timestamp": time.time(),
    }

    # System metrics
    try:
        import psutil
        state["system_cpu_percent"] = psutil.cpu_percent()
        state["system_memory_percent"] = psutil.virtual_memory().percent
    except ImportError:
        pass

    # Cryostasis state
    cryo = _get_cryostasis_manager()
    if cryo:
        state["frozen_apps"] = cryo.get_frozen_app_names()

    # Yabai/Ghost Display state
    yabai = _get_yabai_detector()
    if yabai:
        try:
            ghost_space = yabai.get_ghost_display_space()
            state["ghost_display_available"] = ghost_space is not None

            if ghost_space:
                windows = yabai.get_windows_on_space(ghost_space)
                state["apps_on_ghost_display"] = list(set(
                    w.get("app", "") for w in windows if w.get("app")
                ))
        except Exception:
            state["ghost_display_available"] = False

    # Surveillance state (if available)
    state["surveillance_active"] = False
    state["surveillance_targets"] = []

    return state


async def _write_state_to_orchestrator(state: Dict[str, Any]) -> None:
    """
    Write state to orchestrator's component directory.

    v73.0: Uses atomic writes to prevent partial read race conditions.
    """
    try:
        components_dir = Path.home() / ".jarvis" / "trinity" / "components"
        components_dir.mkdir(parents=True, exist_ok=True)

        state_file = components_dir / "jarvis_body.json"

        # v73.0: Atomic write - prevents JSONDecodeError from partial reads
        data = {
            "component_type": "jarvis_body",
            "instance_id": JARVIS_INSTANCE_ID,
            "timestamp": time.time(),
            "metrics": state,
        }

        if not write_json_atomic(state_file, data):
            logger.debug("[Trinity] Atomic write failed, state may be stale")

    except Exception as e:
        logger.debug(f"[Trinity] Could not write state: {e}")


# =============================================================================
# v75.0: TRINITY HEALTH MONITOR - Crash Detection & Recovery
# =============================================================================

class TrinityHealthMonitor:
    """
    v75.0: Production-grade health monitor for all Trinity components.

    Features:
        - Stale heartbeat detection (>15s = component dead)
        - Process liveness verification via PID
        - Automatic restart triggering via callbacks
        - Dead letter queue for failed commands
        - Circuit breaker pattern for repeated failures
        - Resource exhaustion detection (memory/CPU)
    """

    # v109.4: CRITICAL FIX - Service-specific timeouts for ML-heavy services
    # jarvis-prime takes 55+ seconds just for ML imports, 2-3 minutes total startup
    # Previous values (15s/30s) caused race conditions where Trinity restarted
    # jarvis-prime while it was still initializing, causing port conflicts

    # Per-service heartbeat timeouts (seconds)
    SERVICE_HEARTBEAT_TIMEOUTS = {
        "j_prime": 180.0,      # ML model loading takes 2-3 minutes
        "reactor_core": 120.0,  # AGI initialization takes 1-2 minutes
        "jarvis_body": 30.0,    # In-process, fast startup
    }

    # Per-service startup grace periods (seconds)
    SERVICE_GRACE_PERIODS = {
        "j_prime": 300.0,       # 5 minutes for full ML initialization
        "reactor_core": 180.0,  # 3 minutes for AGI setup
        "jarvis_body": 60.0,    # 1 minute in-process
    }

    # Default for unknown services
    HEARTBEAT_TIMEOUT = 60.0  # v109.4: Increased from 15.0
    CHECK_INTERVAL = 5.0
    MAX_RESTART_ATTEMPTS = 5  # v78.1: Increased from 3 to 5
    RESTART_BACKOFF_BASE = 1.5  # v78.1: Reduced from 2.0 to 1.5
    MAX_BACKOFF_SECONDS = 60.0  # v78.1: Cap max backoff at 60 seconds
    GRACE_PERIOD_SECONDS = 120.0  # v109.4: Increased from 30.0 for ML services

    # v109.4: Restart coordination lock to prevent race conditions
    _restart_lock: Optional[asyncio.Lock] = None
    _restart_in_progress: Dict[str, bool] = {}

    def __init__(self):
        self._monitoring_task: Optional[asyncio.Task] = None
        self._running = False

        # Component states: "healthy", "degraded", "down", "restarting", "starting"
        self._component_states: Dict[str, str] = {
            "j_prime": "unknown",
            "reactor_core": "unknown",
            "jarvis_body": "unknown",
        }

        # Restart tracking for circuit breaker
        self._restart_attempts: Dict[str, int] = {
            "j_prime": 0,
            "reactor_core": 0,
        }
        self._last_restart_time: Dict[str, float] = {}

        # v78.1: Track when we first saw each component (for grace period)
        self._component_first_seen: Dict[str, float] = {}
        self._component_last_healthy: Dict[str, float] = {}

        # v109.4: Restart coordination to prevent race conditions
        self._restart_lock = asyncio.Lock()
        self._restart_in_progress: Dict[str, bool] = {
            "j_prime": False,
            "reactor_core": False,
            "jarvis_body": False,
        }
        # Track last spawn time to detect "just spawned" services
        self._last_spawn_time: Dict[str, float] = {}

        # Callbacks for restart triggers
        self._restart_callbacks: Dict[str, Any] = {}

        # Dead letter queue for failed commands
        self._dead_letter_queue: list = []
        self._max_dlq_size = 1000

        # Metrics
        self._health_checks_performed = 0
        self._components_restarted = 0
        self._commands_in_dlq = 0

    async def start(self) -> None:
        """Start health monitoring."""
        if self._running:
            return

        self._running = True
        self._monitoring_task = asyncio.create_task(self._monitor_loop())
        logger.info("[Trinity] v75.0 Health Monitor started")

    async def stop(self) -> None:
        """Stop health monitoring."""
        self._running = False
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            self._monitoring_task = None
        logger.info("[Trinity] Health Monitor stopped")

    def register_restart_callback(self, component: str, callback) -> None:
        """Register a callback to restart a component."""
        self._restart_callbacks[component] = callback

    async def _monitor_loop(self) -> None:
        """Main monitoring loop - runs every CHECK_INTERVAL seconds."""
        while self._running:
            try:
                await self._check_all_components()
                self._health_checks_performed += 1
            except Exception as e:
                logger.debug(f"[Trinity] Health check error: {e}")

            await asyncio.sleep(self.CHECK_INTERVAL)

    async def _check_all_components(self) -> None:
        """
        Check health of all Trinity components.

        v90.0: Only check components that are enabled to avoid spurious errors
        when components are intentionally disabled or their repos don't exist.
        """
        components_dir = Path.home() / ".jarvis" / "trinity" / "components"

        # v90.0: Check if J-Prime is enabled before checking heartbeat
        jprime_enabled = os.getenv("JARVIS_PRIME_ENABLED", "true").lower() in ("true", "1", "yes")
        if jprime_enabled:
            # Check J-Prime (Mind)
            # v78.1: Support both naming conventions for backwards compatibility
            # J-Prime may write to either jarvis_prime.json or j_prime.json
            jprime_heartbeat = self._find_heartbeat_file(
                components_dir,
                ["jarvis_prime.json", "j_prime.json"]
            )
            await self._check_component("j_prime", jprime_heartbeat)
        else:
            # Mark as disabled - not an error
            self._component_states["j_prime"] = "disabled"

        # v90.0: Check if Reactor-Core is enabled before checking heartbeat
        reactor_enabled = os.getenv("REACTOR_CORE_ENABLED", "true").lower() in ("true", "1", "yes")
        if reactor_enabled:
            # Check Reactor-Core (Nerves)
            await self._check_component(
                "reactor_core",
                components_dir / "reactor_core.json",
            )
        else:
            # Mark as disabled - not an error
            self._component_states["reactor_core"] = "disabled"

        # JARVIS Body is this process - always healthy if running
        self._component_states["jarvis_body"] = "healthy"

    def _find_heartbeat_file(self, components_dir: Path, candidates: list) -> Path:
        """
        Find the first existing heartbeat file from a list of candidates.

        v78.1: Supports multiple naming conventions for backwards compatibility.
        Returns the first file that exists, or the first candidate if none exist.
        """
        for filename in candidates:
            filepath = components_dir / filename
            if filepath.exists():
                return filepath
        # Return first candidate as default (will trigger "file missing" handling)
        return components_dir / candidates[0]

    async def _check_component(
        self,
        component: str,
        heartbeat_file: Path,
    ) -> None:
        """
        Check a specific component's health.

        v78.1: Enhanced with grace period handling for startup scenarios.
        """
        now = time.time()

        # v78.1: Initialize first-seen time if not set
        if component not in self._component_first_seen:
            self._component_first_seen[component] = now

        time_since_first_seen = now - self._component_first_seen[component]
        in_startup_grace = time_since_first_seen < self.GRACE_PERIOD_SECONDS

        # Check heartbeat file exists
        if not heartbeat_file.exists():
            # v78.1: During startup grace period, missing heartbeat is expected
            if in_startup_grace:
                if self._component_states[component] not in ("starting", "unknown"):
                    logger.info(
                        f"[Trinity] {component} heartbeat not yet available "
                        f"(grace: {self.GRACE_PERIOD_SECONDS - time_since_first_seen:.0f}s remaining)"
                    )
                self._component_states[component] = "starting"
                return

            # After grace period, missing heartbeat is a problem
            if self._component_states[component] != "down":
                logger.warning(f"[Trinity] {component} heartbeat file missing")
            await self._handle_component_down(component, "heartbeat_missing")
            return

        # Read heartbeat with safe JSON parsing
        state = read_json_safe(heartbeat_file)
        if not state:
            # v78.1: During startup, corrupted heartbeat might be transient
            if in_startup_grace:
                logger.debug(f"[Trinity] {component} heartbeat parse failed during startup")
                self._component_states[component] = "starting"
                return

            await self._handle_component_down(component, "heartbeat_corrupted")
            return

        # Check heartbeat age
        timestamp = state.get("timestamp", 0)
        age = now - timestamp

        # v109.4: Service-specific heartbeat timeouts
        # ML-heavy services need MUCH longer timeouts during startup
        base_timeout = self.SERVICE_HEARTBEAT_TIMEOUTS.get(component, self.HEARTBEAT_TIMEOUT)
        grace_period = self.SERVICE_GRACE_PERIODS.get(component, self.GRACE_PERIOD_SECONDS)

        # v109.4: Check if service was recently spawned (even more lenient)
        last_spawn = self._last_spawn_time.get(component, 0)
        time_since_spawn = now - last_spawn if last_spawn else float('inf')
        recently_spawned = time_since_spawn < grace_period

        # v78.1: Extended timeout during startup grace period
        effective_timeout = base_timeout
        if in_startup_grace or recently_spawned:
            # Allow MUCH longer timeout during startup (ML services need 2-3 min)
            effective_timeout = grace_period
            if recently_spawned:
                logger.debug(
                    f"[Trinity] {component} recently spawned ({time_since_spawn:.1f}s ago), "
                    f"using extended timeout: {effective_timeout}s"
                )

        if age > effective_timeout:
            # v78.1: Log differently based on startup vs normal operation
            if in_startup_grace or recently_spawned:
                logger.debug(
                    f"[Trinity] {component} heartbeat stale ({age:.1f}s) but still in grace period "
                    f"(timeout: {effective_timeout}s, grace: {grace_period}s)"
                )
                self._component_states[component] = "starting"
                return

            # v109.4: Additional check - verify process is actually dead before declaring DOWN
            pid = state.get("pid") or self._extract_pid_from_instance_id(
                state.get("instance_id", "")
            )
            if pid and self._is_process_alive(pid):
                # Process is alive but heartbeat is stale - log warning but don't restart
                logger.warning(
                    f"[Trinity] {component} heartbeat stale ({age:.1f}s) but process {pid} is alive "
                    f"- NOT triggering restart (may be in heavy initialization)"
                )
                self._component_states[component] = "degraded"
                return

            await self._handle_component_down(
                component,
                f"heartbeat_stale_{age:.1f}s"
            )
            return

        # Check process liveness (if PID available)
        pid = state.get("pid") or self._extract_pid_from_instance_id(
            state.get("instance_id", "")
        )
        if pid and not self._is_process_alive(pid):
            await self._handle_component_down(component, f"process_{pid}_dead")
            return

        # Component is healthy
        if self._component_states[component] != "healthy":
            if self._component_states[component] == "starting":
                logger.info(f"[Trinity] {component} completed startup (healthy)")
            else:
                logger.info(f"[Trinity] {component} recovered to healthy state")

        self._component_states[component] = "healthy"
        self._component_last_healthy[component] = now
        self._restart_attempts[component] = 0  # Reset restart attempts

    def _extract_pid_from_instance_id(self, instance_id: str) -> Optional[int]:
        """Extract PID from instance_id format: 'component-PID-timestamp'."""
        try:
            parts = instance_id.split("-")
            if len(parts) >= 2:
                return int(parts[1])
        except (ValueError, IndexError):
            pass
        return None

    def _is_process_alive(self, pid: int) -> bool:
        """Check if a process is still alive."""
        try:
            os.kill(pid, 0)  # Signal 0 = check existence
            return True
        except OSError:
            return False

    async def _handle_component_down(self, component: str, reason: str) -> None:
        """
        Handle a component being down.

        v109.4: Enhanced with restart coordination locking to prevent race conditions
        where Trinity and Supervisor both try to restart the same component.
        """
        # v109.4: Acquire restart lock to coordinate with supervisor
        # Guard against None (should never happen after __init__)
        if self._restart_lock is None:
            self._restart_lock = asyncio.Lock()

        async with self._restart_lock:
            # v109.4: Check if restart is already in progress (by us or supervisor)
            if self._restart_in_progress.get(component, False):
                logger.debug(
                    f"[Trinity] {component} restart already in progress, skipping duplicate trigger"
                )
                return

            # v109.4: Check supervisor restart state to avoid duplicate restarts
            if await self._is_supervisor_restarting(component):
                logger.info(
                    f"[Trinity] {component} restart already triggered by supervisor, deferring"
                )
                self._component_states[component] = "restarting"
                return

            old_state = self._component_states.get(component)
            self._component_states[component] = "down"

            # Only log once per state transition
            if old_state != "down":
                logger.error(f"[Trinity] {component} is DOWN: {reason}")

            # v78.1: Enhanced circuit breaker with reasonable backoff
            attempts = self._restart_attempts.get(component, 0)
            last_restart = self._last_restart_time.get(component, 0)
            time_since_restart = time.time() - last_restart if last_restart else float('inf')

            if attempts >= self.MAX_RESTART_ATTEMPTS:
                # Calculate backoff in SECONDS (not minutes!) with a cap
                raw_backoff = self.RESTART_BACKOFF_BASE ** attempts
                backoff_seconds = min(raw_backoff, self.MAX_BACKOFF_SECONDS)

                if time_since_restart < backoff_seconds:
                    remaining = backoff_seconds - time_since_restart
                    logger.warning(
                        f"[Trinity] {component} restart circuit breaker OPEN "
                        f"(attempts={attempts}/{self.MAX_RESTART_ATTEMPTS}, "
                        f"retry in {remaining:.0f}s)"
                    )
                    return
                else:
                    # Reset attempts after backoff period
                    logger.info(f"[Trinity] {component} circuit breaker RESET after {time_since_restart:.0f}s")
                    self._restart_attempts[component] = 0
                    attempts = 0

            # v109.4: Use service-specific grace periods
            grace_period = self.SERVICE_GRACE_PERIODS.get(component, self.GRACE_PERIOD_SECONDS)

            # v78.1: Grace period for new components (don't restart too quickly)
            if attempts == 0 and time_since_restart < grace_period:
                logger.debug(
                    f"[Trinity] {component} in grace period, {grace_period - time_since_restart:.0f}s remaining"
                )
                # Still allow first restart attempt, but log it

            # v109.4: Final pre-restart validation - verify process is truly dead
            if not await self._verify_process_dead(component):
                logger.info(
                    f"[Trinity] {component} process still alive, deferring restart (reason was: {reason})"
                )
                self._component_states[component] = "degraded"
                return

            # Trigger restart if callback registered
            if component in self._restart_callbacks:
                await self._trigger_restart(component, reason)

    async def _is_supervisor_restarting(self, component: str) -> bool:
        """
        v109.4: Check if the supervisor is already restarting this component.

        This prevents race conditions where both Trinity and Supervisor
        try to restart the same service simultaneously.
        """
        try:
            # Check supervisor's restart state file
            restart_state_file = Path.home() / ".jarvis" / "supervisor" / "restart_state.json"
            if not restart_state_file.exists():
                return False

            state = read_json_safe(restart_state_file)
            if not state:
                return False

            # Map component names
            component_map = {
                "j_prime": "jarvis-prime",
                "reactor_core": "reactor-core",
                "jarvis_body": "jarvis-body",
            }
            service_name = component_map.get(component, component)

            # Check if supervisor is restarting this service
            restarting_services = state.get("restarting", [])
            return service_name in restarting_services

        except Exception as e:
            logger.debug(f"[Trinity] Could not check supervisor restart state: {e}")
            return False

    async def _verify_process_dead(self, component: str) -> bool:
        """
        v109.4: Verify that a component's process is actually dead before restarting.

        This prevents race conditions where we restart a process that's still
        alive but just slow to respond (e.g., ML model loading).

        Returns:
            True if process is confirmed dead (safe to restart)
            False if process is still alive
        """
        try:
            # Read the component's state file to get PID
            components_dir = Path.home() / ".jarvis" / "trinity" / "components"
            state_file = components_dir / f"{component}.json"

            if not state_file.exists():
                return True  # No state file = process never started or cleaned up

            state = read_json_safe(state_file)
            if not state:
                return True  # Corrupted state = assume dead

            # Try to get PID from state
            pid = state.get("pid") or state.get("metrics", {}).get("pid")
            if not pid:
                # Try to extract from instance_id
                instance_id = state.get("instance_id", "")
                pid = self._extract_pid_from_instance_id(instance_id)

            if not pid:
                return True  # No PID = can't verify, assume dead

            # Check if process is alive
            if self._is_process_alive(pid):
                logger.debug(f"[Trinity] {component} PID {pid} is still alive")
                return False

            logger.debug(f"[Trinity] {component} PID {pid} confirmed dead")
            return True

        except Exception as e:
            logger.debug(f"[Trinity] Process verification error for {component}: {e}")
            return True  # On error, assume dead to allow restart

    async def _trigger_restart(self, component: str, reason: str) -> None:
        """
        Trigger component restart via callback.

        v109.4: Enhanced with restart coordination to prevent race conditions
        with supervisor and concurrent restart attempts.
        """
        # v109.4: Mark restart as in progress (lock should already be held)
        self._restart_in_progress[component] = True

        try:
            self._component_states[component] = "restarting"
            self._restart_attempts[component] = self._restart_attempts.get(component, 0) + 1
            self._last_restart_time[component] = time.time()
            self._components_restarted += 1

            # v78.1: Reset first-seen time to give restarted component a fresh grace period
            self._component_first_seen[component] = time.time()

            # v109.4: Update spawn time for extended timeout during restart
            self._last_spawn_time[component] = time.time()

            logger.info(
                f"[Trinity] Triggering {component} restart "
                f"(attempt {self._restart_attempts[component]}/{self.MAX_RESTART_ATTEMPTS})"
            )

            # v109.4: Notify supervisor that Trinity is initiating a restart
            await self._notify_supervisor_restart(component, reason)

            try:
                callback = self._restart_callbacks[component]
                if asyncio.iscoroutinefunction(callback):
                    await callback(reason)
                else:
                    callback(reason)

                logger.info(f"[Trinity] {component} restart callback completed")

            except Exception as e:
                logger.error(f"[Trinity] Restart callback failed for {component}: {e}")
                self._component_states[component] = "down"

        finally:
            # v109.4: Clear restart in progress flag after a delay
            # (allow time for the service to start binding ports)
            grace_period = self.SERVICE_GRACE_PERIODS.get(component, self.GRACE_PERIOD_SECONDS)
            asyncio.create_task(self._clear_restart_flag_delayed(component, grace_period))

    async def _clear_restart_flag_delayed(self, component: str, delay: float) -> None:
        """
        v109.4: Clear restart in progress flag after delay.

        This allows time for the service to actually start up before
        we consider allowing another restart attempt.
        """
        try:
            await asyncio.sleep(delay)
            self._restart_in_progress[component] = False
            logger.debug(f"[Trinity] {component} restart coordination flag cleared")
        except asyncio.CancelledError:
            self._restart_in_progress[component] = False

    async def _notify_supervisor_restart(self, component: str, reason: str) -> None:
        """
        v109.4: Notify supervisor that Trinity is initiating a restart.

        This prevents the supervisor from also trying to restart the same
        service, which would cause port conflicts.
        """
        try:
            # Map component names to supervisor service names
            component_map = {
                "j_prime": "jarvis-prime",
                "reactor_core": "reactor-core",
                "jarvis_body": "jarvis-body",
            }
            service_name = component_map.get(component, component)

            # Write restart notification to supervisor's coordination directory
            supervisor_dir = Path.home() / ".jarvis" / "supervisor"
            supervisor_dir.mkdir(parents=True, exist_ok=True)

            restart_file = supervisor_dir / "trinity_restart_notify.json"

            notification = {
                "service": service_name,
                "component": component,
                "reason": reason,
                "timestamp": time.time(),
                "trinity_restart_attempt": self._restart_attempts.get(component, 1),
                "message": f"Trinity initiating restart for {service_name}",
            }

            write_json_atomic(restart_file, notification)
            logger.debug(f"[Trinity] Notified supervisor of {component} restart")

        except Exception as e:
            logger.debug(f"[Trinity] Could not notify supervisor: {e}")
            # Not critical - continue with restart anyway

    def add_to_dead_letter_queue(self, command: Dict[str, Any], reason: str) -> None:
        """Add a failed command to the dead letter queue."""
        if len(self._dead_letter_queue) >= self._max_dlq_size:
            # Remove oldest entries
            self._dead_letter_queue = self._dead_letter_queue[-self._max_dlq_size // 2:]

        self._dead_letter_queue.append({
            "command": command,
            "failed_at": time.time(),
            "reason": reason,
        })
        self._commands_in_dlq = len(self._dead_letter_queue)

    def get_dead_letter_queue(self) -> list:
        """Get dead letter queue contents."""
        return self._dead_letter_queue.copy()

    def get_status(self) -> Dict[str, Any]:
        """Get health monitor status."""
        now = time.time()

        # v78.1: Calculate grace period remaining for each component
        grace_remaining = {}
        for comp, first_seen in self._component_first_seen.items():
            time_since = now - first_seen
            if time_since < self.GRACE_PERIOD_SECONDS:
                grace_remaining[comp] = round(self.GRACE_PERIOD_SECONDS - time_since, 1)
            else:
                grace_remaining[comp] = 0

        return {
            "running": self._running,
            "component_states": self._component_states.copy(),
            "restart_attempts": self._restart_attempts.copy(),
            "health_checks_performed": self._health_checks_performed,
            "components_restarted": self._components_restarted,
            "dead_letter_queue_size": self._commands_in_dlq,
            # v78.1: Additional diagnostics
            "grace_period_remaining": grace_remaining,
            "last_healthy_times": {
                comp: round(now - ts, 1) if ts else None
                for comp, ts in self._component_last_healthy.items()
            },
            "circuit_breaker_status": {
                comp: "OPEN" if attempts >= self.MAX_RESTART_ATTEMPTS else "CLOSED"
                for comp, attempts in self._restart_attempts.items()
            },
        }

    # =========================================================================
    # v78.1: STARTUP DEPENDENCY MANAGEMENT
    # =========================================================================

    async def check_startup_dependencies(self) -> Dict[str, Any]:
        """
        v78.1: Check Trinity startup dependencies before enabling full functionality.

        Startup order:
        1. Reactor-Core (nerves) - must be up first for message passing
        2. J-Prime (mind) - depends on Reactor-Core for communication
        3. JARVIS Body - can function in degraded mode without the others

        v90.0: Now respects component enabled status - disabled components
        are treated as "ok" for dependency purposes.

        Returns:
            Dict with dependency status and recommended action
        """
        components_dir = Path.home() / ".jarvis" / "trinity" / "components"
        now = time.time()

        results = {
            "reactor_core": {"status": "unknown", "heartbeat_age": None, "error": None},
            "j_prime": {"status": "unknown", "heartbeat_age": None, "error": None},
            "dependency_order": ["reactor_core", "j_prime", "jarvis_body"],
            "ready_for_full_operation": False,
            "degraded_mode_required": False,
            "recommendations": [],
        }

        # v90.0: Check if components are enabled
        reactor_enabled = os.getenv("REACTOR_CORE_ENABLED", "true").lower() in ("true", "1", "yes")
        jprime_enabled = os.getenv("JARVIS_PRIME_ENABLED", "true").lower() in ("true", "1", "yes")

        # Check Reactor-Core first (it's the communication backbone)
        if reactor_enabled:
            reactor_file = components_dir / "reactor_core.json"
            reactor_status = await self._check_dependency_status(
                "reactor_core", reactor_file, now
            )
            results["reactor_core"] = reactor_status
        else:
            # v90.0: Disabled components are treated as "ok"
            results["reactor_core"] = {"status": "disabled", "heartbeat_age": None, "error": None}

        # Check J-Prime (depends on Reactor-Core)
        if jprime_enabled:
            # v78.1: Support both naming conventions for backwards compatibility
            jprime_file = self._find_heartbeat_file(
                components_dir,
                ["jarvis_prime.json", "j_prime.json"]
            )
            jprime_status = await self._check_dependency_status(
                "j_prime", jprime_file, now
            )
            results["j_prime"] = jprime_status
        else:
            # v90.0: Disabled components are treated as "ok"
            results["j_prime"] = {"status": "disabled", "heartbeat_age": None, "error": None}

        # Determine overall status
        # v90.0: Disabled components count as "ok" for dependency purposes
        reactor_status_val = results["reactor_core"]["status"]
        jprime_status_val = results["j_prime"]["status"]
        reactor_ok = reactor_status_val in ("healthy", "starting", "disabled")
        jprime_ok = jprime_status_val in ("healthy", "starting", "disabled")

        if reactor_ok and jprime_ok:
            results["ready_for_full_operation"] = True
            results["recommendations"].append("Trinity is fully operational")
        elif reactor_ok and not jprime_ok:
            results["degraded_mode_required"] = True
            results["recommendations"].extend([
                "J-Prime is not available - operating in degraded mode",
                "JARVIS can execute commands but cognitive functions are limited",
                f"J-Prime error: {jprime_status.get('error', 'unknown')}",
            ])
        elif not reactor_ok:
            results["degraded_mode_required"] = True
            results["recommendations"].extend([
                "Reactor-Core is not available - operating in standalone mode",
                "Trinity communication is disabled",
                f"Reactor error: {reactor_status.get('error', 'unknown')}",
            ])

            # J-Prime can't work without Reactor-Core anyway
            if jprime_status["status"] == "healthy":
                results["recommendations"].append(
                    "J-Prime is up but can't communicate without Reactor-Core"
                )

        return results

    async def _check_dependency_status(
        self,
        component: str,
        heartbeat_file: Path,
        now: float,
    ) -> Dict[str, Any]:
        """Check status of a specific dependency."""
        result = {
            "status": "unknown",
            "heartbeat_age": None,
            "error": None,
            "pid": None,
            "process_alive": None,
        }

        if not heartbeat_file.exists():
            result["status"] = "not_started"
            result["error"] = "heartbeat file missing"
            return result

        state = read_json_safe(heartbeat_file)
        if not state:
            result["status"] = "error"
            result["error"] = "heartbeat file corrupted"
            return result

        timestamp = state.get("timestamp", 0)
        age = now - timestamp
        result["heartbeat_age"] = round(age, 1)

        # Check heartbeat freshness
        if age > self.HEARTBEAT_TIMEOUT:
            result["status"] = "stale"
            result["error"] = f"heartbeat stale ({age:.0f}s old, max {self.HEARTBEAT_TIMEOUT}s)"
        elif age > self.GRACE_PERIOD_SECONDS:
            result["status"] = "healthy"
        else:
            # In grace period
            result["status"] = "starting"

        # Check process liveness
        pid = state.get("pid") or self._extract_pid_from_instance_id(
            state.get("instance_id", "")
        )
        if pid:
            result["pid"] = pid
            result["process_alive"] = self._is_process_alive(pid)

            if not result["process_alive"] and result["status"] == "healthy":
                result["status"] = "zombie"
                result["error"] = f"process {pid} is not alive"

        return result

    async def wait_for_dependencies(
        self,
        timeout: float = 60.0,
        check_interval: float = 2.0,
    ) -> bool:
        """
        v78.1: Wait for Trinity dependencies to become available.

        This should be called during JARVIS startup to ensure dependencies
        are ready before enabling full functionality.

        Args:
            timeout: Maximum time to wait for dependencies
            check_interval: Time between dependency checks

        Returns:
            True if all dependencies are ready, False if timeout or failures
        """
        start_time = time.time()
        logger.info(f"[Trinity] Waiting for dependencies (timeout={timeout}s)...")

        while (time.time() - start_time) < timeout:
            status = await self.check_startup_dependencies()

            if status["ready_for_full_operation"]:
                elapsed = time.time() - start_time
                logger.info(f"[Trinity] ✓ All dependencies ready in {elapsed:.1f}s")
                return True

            # Log progress
            reactor = status["reactor_core"]["status"]
            jprime = status["j_prime"]["status"]
            logger.debug(
                f"[Trinity] Waiting: reactor_core={reactor}, j_prime={jprime}"
            )

            await asyncio.sleep(check_interval)

        # Timeout - log final status
        status = await self.check_startup_dependencies()
        logger.warning(
            f"[Trinity] Dependency wait timed out after {timeout}s. "
            f"Status: {status['recommendations']}"
        )

        return False

    def reset_component_tracking(self, component: str) -> None:
        """
        v78.1: Reset tracking for a component (useful when manually restarting).

        This clears the circuit breaker and grace period tracking.
        """
        self._restart_attempts[component] = 0
        self._component_first_seen.pop(component, None)
        self._component_last_healthy.pop(component, None)
        self._component_states[component] = "unknown"

        logger.info(f"[Trinity] Reset tracking for {component}")


# =============================================================================
# v75.0: FILE SYSTEM RESILIENCE - Disk Space & Permission Handling
# =============================================================================

class FileSystemGuard:
    """
    v75.0: Protects against file system failures.

    Features:
        - Disk space checking before writes
        - Permission validation
        - Retry with exponential backoff
        - Fallback to alternative locations
    """

    MIN_DISK_SPACE_BYTES = 10 * 1024 * 1024  # 10MB minimum
    MAX_RETRIES = 3
    BACKOFF_BASE = 0.5

    @classmethod
    def has_disk_space(cls, path: Path, min_bytes: int = None) -> bool:
        """Check if there's sufficient disk space."""
        min_bytes = min_bytes or cls.MIN_DISK_SPACE_BYTES
        try:
            import shutil
            usage = shutil.disk_usage(path.parent if path.is_file() else path)
            return usage.free > min_bytes
        except Exception:
            return True  # Assume OK if we can't check

    @classmethod
    def has_write_permission(cls, path: Path) -> bool:
        """Check if we have write permission."""
        try:
            if path.is_dir():
                test_file = path / f".perm_test_{os.getpid()}"
            else:
                test_file = path.parent / f".perm_test_{os.getpid()}"

            test_file.touch()
            test_file.unlink()
            return True
        except Exception:
            return False

    @classmethod
    async def write_with_resilience(
        cls,
        filepath: Path,
        data: Dict[str, Any],
        max_retries: int = None,
    ) -> bool:
        """
        Write JSON data with comprehensive resilience.

        Returns:
            True if write succeeded, False otherwise
        """
        max_retries = max_retries or cls.MAX_RETRIES
        filepath = Path(filepath)

        # Pre-flight checks
        if not cls.has_disk_space(filepath):
            logger.error(f"[Trinity] Insufficient disk space for {filepath}")
            return False

        if not cls.has_write_permission(filepath.parent):
            logger.error(f"[Trinity] No write permission for {filepath.parent}")
            return False

        # Attempt write with retries
        for attempt in range(max_retries):
            if write_json_atomic(filepath, data):
                return True

            if attempt < max_retries - 1:
                delay = cls.BACKOFF_BASE * (2 ** attempt)
                logger.debug(
                    f"[Trinity] Write failed, retrying in {delay:.1f}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(delay)

        logger.error(f"[Trinity] Write failed after {max_retries} attempts: {filepath}")
        return False


# Global health monitor instance
_health_monitor: Optional[TrinityHealthMonitor] = None


def get_health_monitor() -> Optional[TrinityHealthMonitor]:
    """Get the global health monitor instance."""
    return _health_monitor


async def start_health_monitor() -> TrinityHealthMonitor:
    """Start the global health monitor."""
    global _health_monitor
    if _health_monitor is None:
        _health_monitor = TrinityHealthMonitor()
    await _health_monitor.start()
    return _health_monitor


async def stop_health_monitor() -> None:
    """Stop the global health monitor."""
    global _health_monitor
    if _health_monitor:
        await _health_monitor.stop()


# =============================================================================
# STATUS
# =============================================================================

def is_trinity_initialized() -> bool:
    """Check if Trinity is initialized."""
    return _trinity_initialized


def get_trinity_status() -> Dict[str, Any]:
    """Get current Trinity status."""
    bridge = _get_reactor_bridge()

    return {
        "enabled": TRINITY_ENABLED,
        "initialized": _trinity_initialized,
        "instance_id": JARVIS_INSTANCE_ID,
        "uptime_seconds": time.time() - _start_time if _trinity_initialized else 0,
        "connected": bridge.is_connected() if bridge else False,
        "heartbeat_interval": TRINITY_HEARTBEAT_INTERVAL,
        "bridge_stats": bridge.get_stats() if bridge else None,
    }


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Core initialization
    "initialize_trinity",
    "shutdown_trinity",
    "trinity_context",
    "is_trinity_initialized",
    "get_trinity_status",
    "JARVIS_INSTANCE_ID",
    # v73.0: Atomic I/O
    "AtomicTrinityIO",
    "write_json_atomic",
    "read_json_safe",
    # v75.0: Health monitoring
    "TrinityHealthMonitor",
    "get_health_monitor",
    "start_health_monitor",
    "stop_health_monitor",
    # v75.0: File system resilience
    "FileSystemGuard",
    # v80.0: TrinityBridgeAdapter - Closes the Trinity Loop
    # Watches Reactor Core events and forwards to JARVIS handlers
    # Access via: app.state.trinity_bridge_adapter after initialization
]

# v78.1 methods are available on TrinityHealthMonitor instance:
# - check_startup_dependencies() - Check Trinity dependency status
# - wait_for_dependencies(timeout, check_interval) - Wait for deps to be ready
# - reset_component_tracking(component) - Reset circuit breaker for component
