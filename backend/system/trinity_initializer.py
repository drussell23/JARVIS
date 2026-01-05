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
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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

        # Step 5: Attach to app state if available
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
    global _trinity_initialized, _heartbeat_task, _bridge

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

    # Disconnect bridge
    bridge = _get_reactor_bridge()
    if bridge and bridge.is_connected():
        await bridge.disconnect_async()

    _trinity_initialized = False
    _bridge = None

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
    """Write state to orchestrator's component directory."""
    import json

    try:
        components_dir = Path.home() / ".jarvis" / "trinity" / "components"
        components_dir.mkdir(parents=True, exist_ok=True)

        state_file = components_dir / "jarvis_body.json"
        with open(state_file, "w") as f:
            json.dump({
                "component_type": "jarvis_body",
                "instance_id": JARVIS_INSTANCE_ID,
                "timestamp": time.time(),
                "metrics": state,
            }, f, indent=2)

    except Exception as e:
        logger.debug(f"[Trinity] Could not write state: {e}")


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
    "initialize_trinity",
    "shutdown_trinity",
    "trinity_context",
    "is_trinity_initialized",
    "get_trinity_status",
    "JARVIS_INSTANCE_ID",
]
