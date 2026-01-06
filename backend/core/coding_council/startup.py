"""
v77.3: Coding Council Startup Integration (Anthropic-Powered)
==============================================================

Integrates the Unified Coding Council with JARVIS startup sequence.

v77.3 Features:
- Anthropic Claude API integration (primary engine)
- No external tool dependencies required
- Aider-style editing via Claude
- MetaGPT-style multi-agent planning via Claude
- Cross-repo Trinity synchronization

This module provides:
- Single-command startup integration
- run_supervisor.py hook
- Lifespan event handlers
- Health check endpoints
- Anthropic engine auto-initialization

Usage in run_supervisor.py:
    from backend.core.coding_council.startup import (
        initialize_coding_council_startup,
        shutdown_coding_council_startup,
        get_coding_council_health,
    )

    # During JARVIS startup phase
    await initialize_coding_council_startup()

    # During JARVIS shutdown
    await shutdown_coding_council_startup()

Author: JARVIS v77.3
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .orchestrator import UnifiedCodingCouncil
    from .adapters.anthropic_engine import AnthropicUnifiedEngine

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

CODING_COUNCIL_ENABLED = os.getenv("CODING_COUNCIL_ENABLED", "true").lower() == "true"
CODING_COUNCIL_STARTUP_TIMEOUT = float(os.getenv("CODING_COUNCIL_STARTUP_TIMEOUT", "30.0"))
CODING_COUNCIL_VOICE_ANNOUNCE = os.getenv("CODING_COUNCIL_VOICE_ANNOUNCE", "true").lower() == "true"


# =============================================================================
# Global State
# =============================================================================

_council: Optional["UnifiedCodingCouncil"] = None
_anthropic_engine: Optional[Any] = None
_startup_time: Optional[float] = None
_initialized = False


# =============================================================================
# Startup Functions
# =============================================================================

async def initialize_coding_council_startup(
    narrator=None,
    logger_instance=None,
) -> bool:
    """
    Initialize Coding Council during JARVIS startup.

    This should be called from run_supervisor.py during the
    SUPERVISOR_INIT or JARVIS_START phase.

    Args:
        narrator: Optional narrator for voice announcements
        logger_instance: Optional logger instance

    Returns:
        True if initialization succeeded
    """
    global _council, _startup_time, _initialized

    if not CODING_COUNCIL_ENABLED:
        logger.info("[CodingCouncilStartup] Disabled via CODING_COUNCIL_ENABLED=false")
        return False

    if _initialized:
        logger.debug("[CodingCouncilStartup] Already initialized")
        return True

    log = logger_instance or logger
    start_time = time.time()

    try:
        log.info("=" * 60)
        log.info("v77.0 UNIFIED CODING COUNCIL: Initializing")
        log.info("=" * 60)

        # Voice announcement if narrator available
        if narrator and CODING_COUNCIL_VOICE_ANNOUNCE:
            try:
                await narrator.speak(
                    "Initializing Unified Coding Council for self-evolution.",
                    wait=False
                )
            except Exception:
                pass  # Voice is optional

        # Initialize with timeout
        try:
            _council = await asyncio.wait_for(
                _initialize_council_full(),
                timeout=CODING_COUNCIL_STARTUP_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.warning(
                f"[CodingCouncilStartup] Initialization timed out after "
                f"{CODING_COUNCIL_STARTUP_TIMEOUT}s, continuing in background"
            )
            # Start in background instead
            asyncio.create_task(_initialize_council_full())
            return False

        _startup_time = time.time()
        _initialized = True

        # Log success
        duration = time.time() - start_time
        log.info("=" * 60)
        log.info(f"v77.0 UNIFIED CODING COUNCIL: Online ({duration:.2f}s)")
        log.info(f"  Frameworks: Aider, MetaGPT, RepoMaster, OpenHands, Continue")
        log.info(f"  Cross-Repo: {os.getenv('CODING_COUNCIL_CROSS_REPO', 'true')}")
        log.info("=" * 60)

        # Voice success
        if narrator and CODING_COUNCIL_VOICE_ANNOUNCE:
            try:
                await narrator.speak(
                    "Coding Council online. Self-evolution capabilities active.",
                    wait=False
                )
            except Exception:
                pass

        return True

    except Exception as e:
        log.error(f"[CodingCouncilStartup] Initialization failed: {e}")
        return False


async def _initialize_council_full() -> "UnifiedCodingCouncil":
    """Internal: Full council initialization with Trinity and Anthropic engine."""
    global _anthropic_engine

    from . import initialize_coding_council_full

    # Initialize council
    council = await initialize_coding_council_full()

    # Initialize Anthropic engine (primary engine for Aider/MetaGPT style operations)
    try:
        from .adapters.anthropic_engine import get_anthropic_engine
        _anthropic_engine = await get_anthropic_engine()
        if _anthropic_engine:
            logger.info("[CodingCouncilStartup] Anthropic engine initialized (Claude API)")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] Anthropic engine not available: {e}")
        _anthropic_engine = None

    return council


async def shutdown_coding_council_startup() -> None:
    """
    Shutdown Coding Council during JARVIS shutdown.

    This should be called from run_supervisor.py during shutdown.
    """
    global _council, _anthropic_engine, _initialized

    if not _initialized:
        return

    logger.info("[CodingCouncilStartup] Shutting down Coding Council...")

    # Shutdown Anthropic engine
    try:
        if _anthropic_engine:
            await _anthropic_engine.close()
            logger.info("[CodingCouncilStartup] Anthropic engine closed")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] Anthropic engine shutdown warning: {e}")

    # Shutdown council
    try:
        from . import shutdown_coding_council
        await shutdown_coding_council()
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] Shutdown warning: {e}")

    _council = None
    _anthropic_engine = None
    _initialized = False
    logger.info("[CodingCouncilStartup] Shutdown complete")


# =============================================================================
# Health Check
# =============================================================================

async def get_coding_council_health() -> Dict[str, Any]:
    """
    Get Coding Council health status for health endpoints.

    Returns:
        Health status dict
    """
    global _council, _startup_time, _initialized

    if not CODING_COUNCIL_ENABLED:
        return {
            "enabled": False,
            "status": "disabled",
        }

    if not _initialized:
        return {
            "enabled": True,
            "status": "not_initialized",
        }

    try:
        status = _council.get_status() if _council else {}
        return {
            "enabled": True,
            "status": "healthy",
            "uptime_seconds": time.time() - _startup_time if _startup_time else 0,
            "active_tasks": status.get("active_tasks", 0),
            "circuit_breakers": status.get("circuit_breakers", {}),
            "frameworks": status.get("frameworks_available", []),
        }
    except Exception as e:
        return {
            "enabled": True,
            "status": "error",
            "error": str(e),
        }


# =============================================================================
# API Registration for FastAPI
# =============================================================================

def register_coding_council_routes(app):
    """
    Register Coding Council routes with FastAPI app.

    Usage:
        from backend.core.coding_council.startup import register_coding_council_routes
        register_coding_council_routes(app)
    """
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/coding-council", tags=["Coding Council"])

    @router.get("/health")
    async def health():
        """Get Coding Council health status."""
        return await get_coding_council_health()

    @router.get("/status")
    async def status():
        """Get detailed Coding Council status."""
        if not _initialized or not _council:
            raise HTTPException(status_code=503, detail="Coding Council not initialized")
        return _council.get_status()

    @router.post("/evolve")
    async def evolve(request: dict):
        """
        Trigger code evolution.

        Request body:
        {
            "description": "What to change",
            "target_files": ["file1.py", "file2.py"],  // optional
            "require_approval": true,  // optional, default true
            "require_sandbox": false,  // optional
            "require_planning": false  // optional
        }
        """
        if not _initialized or not _council:
            raise HTTPException(status_code=503, detail="Coding Council not initialized")

        description = request.get("description")
        if not description:
            raise HTTPException(status_code=400, detail="description required")

        result = await _council.evolve(
            description=description,
            target_files=request.get("target_files"),
            require_approval=request.get("require_approval", True),
            require_sandbox=request.get("require_sandbox", False),
            require_planning=request.get("require_planning", False),
        )

        return {
            "success": result.success,
            "task_id": result.task_id,
            "changes_made": result.changes_made,
            "files_modified": result.files_modified,
            "error": result.error,
        }

    @router.get("/frameworks")
    async def frameworks():
        """Get available framework status."""
        if not _initialized or not _council:
            raise HTTPException(status_code=503, detail="Coding Council not initialized")

        framework_status = {}

        # Check Anthropic engine first (primary engine)
        if _anthropic_engine:
            try:
                available = await _anthropic_engine.is_available()
                framework_status["anthropic_engine"] = {
                    "available": available,
                    "type": "primary",
                    "features": ["aider_style", "metagpt_style", "git_aware", "no_external_deps"],
                    "tokens_used": _anthropic_engine.tokens_used,
                }
            except Exception as e:
                framework_status["anthropic_engine"] = {"available": False, "error": str(e)}
        else:
            framework_status["anthropic_engine"] = {"available": False, "error": "Not initialized"}

        # Check traditional adapters (fallback)
        for name in ["aider", "repomaster", "metagpt", "openhands", "continue"]:
            adapter = getattr(_council, f"_{name}", None)
            if adapter:
                try:
                    available = await adapter.is_available()
                    framework_status[name] = {"available": available, "type": "adapter"}
                except Exception as e:
                    framework_status[name] = {"available": False, "error": str(e)}
            else:
                framework_status[name] = {"available": False, "error": "Not loaded"}

        return {"frameworks": framework_status}

    # Register routes
    app.include_router(router)
    logger.info("[CodingCouncilStartup] API routes registered at /coding-council")


# =============================================================================
# Hook for run_supervisor.py Integration
# =============================================================================

async def coding_council_startup_hook(
    bootstrapper=None,
    phase: str = "supervisor_init",
) -> bool:
    """
    Hook for run_supervisor.py to call during startup.

    Args:
        bootstrapper: SupervisorBootstrapper instance
        phase: Current startup phase

    Returns:
        True if hook completed successfully
    """
    if not CODING_COUNCIL_ENABLED:
        return True

    # Only initialize during supervisor_init phase
    if phase != "supervisor_init":
        return True

    narrator = None
    logger_instance = None

    if bootstrapper:
        narrator = getattr(bootstrapper, 'narrator', None)
        logger_instance = getattr(bootstrapper, 'logger', None)

    return await initialize_coding_council_startup(
        narrator=narrator,
        logger_instance=logger_instance,
    )


async def coding_council_shutdown_hook(
    bootstrapper=None,
) -> None:
    """
    Hook for run_supervisor.py to call during shutdown.

    Args:
        bootstrapper: SupervisorBootstrapper instance
    """
    await shutdown_coding_council_startup()


# =============================================================================
# Accessor Functions
# =============================================================================

def get_council() -> Optional["UnifiedCodingCouncil"]:
    """Get the global Coding Council instance."""
    return _council


def is_initialized() -> bool:
    """Check if Coding Council is initialized."""
    return _initialized
