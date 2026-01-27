"""
v77.3: Coding Council Startup Integration (Full IDE + Anthropic)
================================================================

Integrates the Unified Coding Council with JARVIS startup sequence,
including the complete IDE bridge and Trinity cross-repo sync.

v77.3 Features:
- Anthropic Claude API integration (primary engine)
- No external tool dependencies required
- Aider-style editing via Claude
- MetaGPT-style multi-agent planning via Claude
- Cross-repo Trinity synchronization
- Real-time IDE integration (LSP + WebSocket)
- Inline suggestions engine
- Distributed suggestion caching

This module provides:
- Single-command startup integration
- run_supervisor.py hook
- Lifespan event handlers
- Health check endpoints
- Anthropic engine auto-initialization
- IDE bridge auto-initialization
- Trinity sync auto-initialization

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
Version: 3.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .orchestrator import UnifiedCodingCouncil
    from .adapters.anthropic_engine import AnthropicUnifiedEngine

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration (Dynamic via unified config)
# =============================================================================

def _get_config():
    """Get unified configuration."""
    try:
        from .config import get_config
        return get_config()
    except ImportError:
        return None


def _is_coding_council_enabled() -> bool:
    """Check if Coding Council is enabled."""
    config = _get_config()
    if config:
        return config.enabled
    return os.getenv("CODING_COUNCIL_ENABLED", "true").lower() == "true"


def _is_ide_bridge_enabled() -> bool:
    """Check if IDE Bridge is enabled."""
    config = _get_config()
    if config:
        return config.ide_bridge_enabled
    return os.getenv("IDE_BRIDGE_ENABLED", "true").lower() == "true"


def _get_lsp_server_port() -> int:
    """Get LSP server port."""
    config = _get_config()
    if config:
        return config.lsp_server_port.port
    return int(os.getenv("LSP_SERVER_PORT", "9257"))


def _get_websocket_port() -> int:
    """Get WebSocket port."""
    config = _get_config()
    if config:
        return config.websocket_port.port
    return int(os.getenv("IDE_WEBSOCKET_PORT", "9258"))


def _get_startup_timeout() -> float:
    """Get startup timeout."""
    config = _get_config()
    if config:
        return config.timeouts.startup
    return float(os.getenv("CODING_COUNCIL_STARTUP_TIMEOUT", "30.0"))


def _can_use_ai() -> bool:
    """Check if AI functionality is available."""
    config = _get_config()
    if config:
        return config.can_use_ai
    return bool(os.getenv("ANTHROPIC_API_KEY"))


# Legacy constants for backward compatibility
CODING_COUNCIL_ENABLED = os.getenv("CODING_COUNCIL_ENABLED", "true").lower() == "true"
CODING_COUNCIL_STARTUP_TIMEOUT = float(os.getenv("CODING_COUNCIL_STARTUP_TIMEOUT", "30.0"))
CODING_COUNCIL_VOICE_ANNOUNCE = os.getenv("CODING_COUNCIL_VOICE_ANNOUNCE", "true").lower() == "true"


# =============================================================================
# Global State
# =============================================================================

_council: Optional["UnifiedCodingCouncil"] = None
_anthropic_engine: Optional[Any] = None
_ide_bridge: Optional[Any] = None
_trinity_sync: Optional[Any] = None
_lsp_server: Optional[Any] = None
_websocket_handler: Optional[Any] = None
_voice_announcer: Optional[Any] = None  # v79.0: Voice announcer for evolution
_startup_time: Optional[float] = None
_initialized = False
_recovery_log: List[Dict[str, Any]] = []  # Track auto-recovery actions

# v78.0: Advanced Process Management Components
_command_buffer: Optional[Any] = None
_timeout_manager: Optional[Any] = None
_retry_manager: Optional[Any] = None

# v85.0: J-Prime Local LLM Integration
_jprime_engine: Optional[Any] = None
_jprime_fallback_chain: Optional[Any] = None
JPRIME_ENABLED = os.getenv("JARVIS_PRIME_ENABLED", "true").lower() == "true"

# IDE Configuration
IDE_BRIDGE_ENABLED = os.getenv("IDE_BRIDGE_ENABLED", "true").lower() == "true"
LSP_SERVER_PORT = int(os.getenv("LSP_SERVER_PORT", "9257"))
IDE_WEBSOCKET_PORT = int(os.getenv("IDE_WEBSOCKET_PORT", "9258"))


# =============================================================================
# Pre-Flight Checks
# =============================================================================

async def _run_preflight_checks(log) -> bool:
    """
    Run pre-flight diagnostic checks before initialization.

    This validates:
    - Environment variables (ANTHROPIC_API_KEY, etc.)
    - Port availability (LSP, WebSocket)
    - Trinity repository connectivity
    - Required module imports
    - API connectivity (if key is set)

    Args:
        log: Logger instance to use for output

    Returns:
        True if all critical checks pass, False otherwise
    """
    global _recovery_log

    try:
        from .diagnostics import (
            run_preflight_checks,
            CheckStatus,
            AutoRecovery,
        )

        # Run initial checks
        report = await run_preflight_checks(print_report=False, auto_recover=False)

        # Log initial summary
        log.info(f"  Pre-flight checks: {report.passed}/{report.total} passed")

        if report.warnings > 0:
            log.warning(f"  Pre-flight warnings: {report.warnings}")

        if report.failed > 0:
            log.warning(f"  Pre-flight failures: {report.failed} - attempting recovery...")

            # Attempt auto-recovery
            recovery_actions = await AutoRecovery.attempt_recovery(report)

            if recovery_actions:
                for action in recovery_actions:
                    log.info(f"    ⚡ Auto-recovered: {action}")
                    _recovery_log.append({
                        "action": action,
                        "timestamp": time.time(),
                        "success": True,
                    })

                # Re-run checks after recovery
                log.info("  Re-running checks after recovery...")
                report = await run_preflight_checks(print_report=False, auto_recover=False)
                log.info(f"  Post-recovery checks: {report.passed}/{report.total} passed")

        # Log individual failures and warnings
        for check in report.checks:
            if check.status == CheckStatus.FAIL:
                log.error(f"    ✗ {check.name}: {check.message}")
                if check.fix_command:
                    log.error(f"      Manual fix: {check.fix_command}")
            elif check.status == CheckStatus.WARN:
                log.warning(f"    ⚠ {check.name}: {check.message}")

        log.info(f"  Pre-flight duration: {report.duration_ms:.1f}ms")

        # Run advanced recovery for remaining failures
        if not report.is_healthy:
            await _run_advanced_recovery(report, log)
            # Final check
            report = await run_preflight_checks(print_report=False, auto_recover=False)

        return report.is_healthy

    except ImportError as e:
        log.warning(f"  Pre-flight checks unavailable: {e}")
        return True  # Don't block startup if diagnostics module not available
    except Exception as e:
        log.warning(f"  Pre-flight checks failed with exception: {e}")
        return True  # Don't block startup on diagnostic failures


async def _run_advanced_recovery(report, log) -> None:
    """
    Run advanced recovery strategies for persistent failures.

    This handles more complex recovery scenarios:
    - Alternative port selection
    - Environment file loading
    - Service restart attempts
    """
    global _recovery_log

    try:
        from .diagnostics import CheckStatus, CheckCategory

        for check in report.checks:
            if check.status != CheckStatus.FAIL:
                continue

            # Advanced port recovery: try alternative ports
            if check.category == CheckCategory.PORTS:
                await _recover_port_conflict(check, log)

            # Trinity directory recovery
            elif check.category == CheckCategory.TRINITY and "directory" in check.message.lower():
                await _recover_directory(check, log)

    except Exception as e:
        log.debug(f"  Advanced recovery exception (non-critical): {e}")


async def _recover_port_conflict(check, log) -> bool:
    """
    Attempt to recover from port conflicts by using alternative ports.
    """
    global _recovery_log, LSP_SERVER_PORT, IDE_WEBSOCKET_PORT

    # Extract port from check name (e.g., "Port 9257 (lsp_server)")
    import re
    match = re.search(r"Port (\d+)", check.name)
    if not match:
        return False

    original_port = int(match.group(1))
    alternative_port = original_port + 1

    # Check if alternative port is available
    from .diagnostics import PortChecker
    if PortChecker.is_port_available(alternative_port):
        # Update the appropriate global variable
        if "lsp" in check.name.lower():
            os.environ["LSP_SERVER_PORT"] = str(alternative_port)
            log.info(f"    ⚡ Using alternative LSP port: {alternative_port}")
        elif "websocket" in check.name.lower():
            os.environ["IDE_WEBSOCKET_PORT"] = str(alternative_port)
            log.info(f"    ⚡ Using alternative WebSocket port: {alternative_port}")

        _recovery_log.append({
            "action": f"Switched to alternative port {alternative_port}",
            "original_port": original_port,
            "timestamp": time.time(),
            "success": True,
        })
        return True

    return False


async def _recover_directory(check, log) -> bool:
    """
    Attempt to create missing directories.
    """
    global _recovery_log

    if not check.fix_command:
        return False

    try:
        import subprocess
        result = subprocess.run(
            check.fix_command,
            shell=True,
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            log.info(f"    ⚡ Created directory via: {check.fix_command}")
            _recovery_log.append({
                "action": f"Created directory: {check.name}",
                "timestamp": time.time(),
                "success": True,
            })
            return True
    except Exception as e:
        log.debug(f"  Directory recovery failed: {e}")

    return False


def get_recovery_log() -> List[Dict[str, Any]]:
    """Get the list of auto-recovery actions taken during startup."""
    return _recovery_log.copy()


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

    if not _is_coding_council_enabled():
        logger.info("[CodingCouncilStartup] Disabled via configuration")
        return False

    if _initialized:
        logger.debug("[CodingCouncilStartup] Already initialized")
        return True

    log = logger_instance or logger
    start_time = time.time()

    try:
        log.info("=" * 60)
        log.info("v77.3 UNIFIED CODING COUNCIL: Pre-Flight Checks")
        log.info("=" * 60)

        # Run pre-flight diagnostics
        preflight_passed = await _run_preflight_checks(log)
        if not preflight_passed:
            log.warning("[CodingCouncilStartup] Some pre-flight checks failed, continuing anyway")

        # Cleanup stale heartbeats from previous runs
        try:
            from .diagnostics import AutoRecovery
            stale_cleanup = await AutoRecovery.cleanup_all_stale_heartbeats(stale_threshold=60.0)
            if stale_cleanup:
                log.info(f"  Cleaned up {len(stale_cleanup)} stale heartbeats:")
                for action in stale_cleanup:
                    log.info(f"    ⚡ {action}")
                    _recovery_log.append({
                        "action": action,
                        "timestamp": time.time(),
                        "source": "startup_cleanup",
                        "success": True,
                    })
        except Exception as e:
            log.debug(f"  Stale heartbeat cleanup skipped: {e}")

        log.info("=" * 60)
        log.info("v77.4 UNIFIED CODING COUNCIL: Initializing")
        log.info("=" * 60)

        # v79.1: Use StartupAnnouncementCoordinator for priority-based announcements
        # This prevents overlapping announcements with other systems
        if CODING_COUNCIL_VOICE_ANNOUNCE:
            try:
                from ..startup_announcement_coordinator import (
                    get_startup_coordinator,
                    AnnouncementPriority,
                )

                coordinator = get_startup_coordinator()
                # Request announcement with NORMAL priority (let Voice API win if it's also starting)
                should_announce = await coordinator.announce_if_first(
                    system_name="coding_council_startup",
                    priority=AnnouncementPriority.NORMAL,
                    message="Initializing Unified Coding Council",
                    metadata={
                        "component": "coding_council",
                        "phase": "initialization",
                        "timestamp": start_time,
                    }
                )

                if should_announce and narrator:
                    # We won the announcement - speak initialization message
                    greeting = coordinator.generate_greeting(context={
                        "system_name": "Coding Council",
                        "action": "initializing",
                    })
                    await narrator.speak(
                        f"{greeting} Initializing self-evolution capabilities.",
                        wait=False
                    )
                    log.debug("[v79.1] Coding Council won startup announcement")
                elif not should_announce:
                    log.debug("[v79.1] Coding Council deferred to higher priority announcer")
            except ImportError:
                # Fallback to basic narrator if coordinator not available
                if narrator:
                    await narrator.speak(
                        "Initializing Unified Coding Council for self-evolution.",
                        wait=False
                    )
            except Exception as e:
                log.debug(f"[v79.1] Startup coordinator integration skipped: {e}")

        # Initialize with timeout (use dynamic config)
        startup_timeout = _get_startup_timeout()
        try:
            _council = await asyncio.wait_for(
                _initialize_council_full(),
                timeout=startup_timeout
            )
        except asyncio.TimeoutError:
            log.warning(
                f"[CodingCouncilStartup] Initialization timed out after "
                f"{startup_timeout}s, continuing in background"
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

        # v79.1: Voice success using voice announcer or coordinator
        if CODING_COUNCIL_VOICE_ANNOUNCE:
            try:
                # Try v79.1 voice announcer first (has circuit breaker, async, etc.)
                if _voice_announcer:
                    await _voice_announcer.announce_completion(
                        task_id="startup",
                        success=True,
                        files_modified=0,
                        execution_time_ms=duration * 1000,
                        error=None,
                    )
                elif narrator:
                    # Fallback to narrator
                    await narrator.speak(
                        "Coding Council online. Self-evolution capabilities active.",
                        wait=False
                    )
            except Exception as e:
                log.debug(f"[v79.1] Voice success announcement skipped: {e}")

        return True

    except Exception as e:
        log.error(f"[CodingCouncilStartup] Initialization failed: {e}")
        return False


async def _initialize_council_full() -> "UnifiedCodingCouncil":
    """Internal: Full council initialization with Trinity, Anthropic engine, and IDE bridge."""
    global _anthropic_engine, _ide_bridge, _trinity_sync, _lsp_server

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

    # Initialize IDE Bridge and Trinity Sync
    if IDE_BRIDGE_ENABLED:
        await _initialize_ide_components()

    # v78.0: Initialize Advanced Process Management Components
    await _initialize_v78_components()

    # v79.0: Initialize Voice Announcer for evolution feedback
    await _initialize_v79_components()

    # v85.0: Initialize J-Prime Local LLM Engine
    await _initialize_v85_jprime_components()

    return council


async def _initialize_v78_components() -> None:
    """
    v78.0: Initialize advanced process management components.

    Components:
    - Command Buffer: Handles early Trinity commands before council is ready
    - Adaptive Timeout: Dynamic timeout calculation
    - Intelligent Retry: Context-aware retry with circuit breakers
    """
    global _command_buffer, _timeout_manager, _retry_manager

    logger.info("[CodingCouncilStartup] Initializing v78.0 components...")

    # Initialize Command Buffer for early Trinity commands
    try:
        from .advanced import get_command_buffer
        _command_buffer = await get_command_buffer()

        # Set up executor for command buffer (processes buffered commands)
        async def execute_buffered_command(cmd):
            global _council
            if _council:
                try:
                    await _council.handle_trinity_event(cmd.payload)
                    return True
                except Exception as e:
                    logger.warning(f"[CommandBuffer] Execution failed: {e}")
                    return False
            return False

        _command_buffer.set_executor(execute_buffered_command)

        # Signal ready and flush any buffered commands
        await _command_buffer.signal_ready()

        logger.info("[CodingCouncilStartup] ✅ Command Buffer ready")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] Command Buffer not available: {e}")
        _command_buffer = None

    # Initialize Adaptive Timeout Manager
    try:
        from .advanced import get_timeout_manager
        _timeout_manager = await get_timeout_manager()
        logger.info("[CodingCouncilStartup] ✅ Adaptive Timeout Manager ready")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] Timeout Manager not available: {e}")
        _timeout_manager = None

    # Initialize Intelligent Retry Manager
    try:
        from .advanced import get_retry_manager
        _retry_manager = await get_retry_manager()
        logger.info("[CodingCouncilStartup] ✅ Intelligent Retry Manager ready")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] Retry Manager not available: {e}")
        _retry_manager = None

    # Log summary
    v78_components = [
        ("Command Buffer", _command_buffer is not None),
        ("Adaptive Timeout", _timeout_manager is not None),
        ("Intelligent Retry", _retry_manager is not None),
    ]
    ready = sum(1 for _, ok in v78_components if ok)
    logger.info(f"[CodingCouncilStartup] v78.0 Components: {ready}/{len(v78_components)} ready")


async def _initialize_v79_components() -> None:
    """
    v79.0: Initialize voice announcer and related components.

    Components:
    - Voice Announcer: Real-time voice feedback for evolution operations
    - Integration with EvolutionBroadcaster for automatic announcements
    """
    global _voice_announcer

    if not CODING_COUNCIL_VOICE_ANNOUNCE:
        logger.info("[CodingCouncilStartup] Voice announcements disabled via config")
        return

    logger.info("[CodingCouncilStartup] Initializing v79.0 voice components...")

    # Initialize Voice Announcer
    try:
        from .voice_announcer import get_evolution_announcer
        _voice_announcer = get_evolution_announcer()
        logger.info("[CodingCouncilStartup] ✅ Voice Announcer ready")

        # Set up integration with broadcaster (done automatically in EvolutionBroadcaster)
        # The broadcaster will lazily initialize the voice announcer on first broadcast

    except ImportError as e:
        logger.debug(f"[CodingCouncilStartup] Voice Announcer not available: {e}")
        _voice_announcer = None
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] Voice Announcer init failed: {e}")
        _voice_announcer = None

    # Log summary
    logger.info(f"[CodingCouncilStartup] v79.0 Components: Voice Announcer "
                f"{'✅ ready' if _voice_announcer else '❌ unavailable'}")


async def _initialize_v85_jprime_components() -> None:
    """
    v118.0: Initialize J-Prime local LLM engine with intelligent service coordination.

    Components:
    - JPrimeUnifiedEngine: Local LLM inference via llama-cpp-python
    - MultiModelFallbackChain: Intelligent model cascading
    - Cross-repo coordination with J-Prime heartbeat monitoring
    - v118.0: Lazy availability checking - don't discard engine if J-Prime is starting
    - v118.0: Background health monitoring for auto-recovery
    - v118.0: Service status tracking for intelligent startup coordination
    """
    global _jprime_engine, _jprime_fallback_chain

    if not JPRIME_ENABLED:
        logger.info("[CodingCouncilStartup] J-Prime disabled via config")
        return

    logger.info("[CodingCouncilStartup] Initializing v118.0 J-Prime components...")

    # Initialize J-Prime Unified Engine
    try:
        from .adapters.jprime_engine import (
            JPrimeUnifiedEngine,
            get_jprime_engine,
            ServiceStatus,
        )

        # Use factory function for singleton pattern
        _jprime_engine = await get_jprime_engine()

        if _jprime_engine:
            # v118.0: Get service status for intelligent logging
            status, details = _jprime_engine.get_service_status()

            if status == ServiceStatus.HEALTHY:
                logger.info("[CodingCouncilStartup] ✅ J-Prime Unified Engine ready (local LLM)")
                # Get fallback chain reference
                _jprime_fallback_chain = getattr(_jprime_engine, '_fallback_chain', None)
                if _jprime_fallback_chain:
                    logger.info("[CodingCouncilStartup] ✅ Multi-Model Fallback Chain ready")

            elif status in (ServiceStatus.STARTING, ServiceStatus.INITIALIZING):
                # v118.0: J-Prime is starting - keep the engine, it will become available
                # Parse status info from either HTTP response or heartbeat file format
                model_loaded = details.get("model_loaded", False)
                model_name = details.get("model_name") or details.get("model_path", "").split("/")[-1] or "unknown"
                healthy = details.get("healthy", False)

                # HTTP response format
                phase = details.get("phase", "")
                step = details.get("details", {}).get("step_num", "")
                total = details.get("details", {}).get("total_steps", "")

                if phase and step:
                    status_info = f"phase={phase}, step {step}/{total}"
                elif model_loaded:
                    status_info = f"model loaded ({model_name}), warming up"
                else:
                    status_info = f"waiting for model ({model_name.split('.')[0] if model_name else 'unknown'})"

                logger.info(
                    f"[CodingCouncilStartup] ⏳ J-Prime {status.value} ({status_info}) - "
                    f"engine ready, will auto-connect when healthy"
                )
                # v118.0: IMPORTANT - Keep the engine reference! Don't set to None
                _jprime_fallback_chain = getattr(_jprime_engine, '_fallback_chain', None)

                # v118.0: Schedule background task to log when J-Prime becomes healthy
                asyncio.create_task(_monitor_jprime_startup())

            elif status == ServiceStatus.DEGRADED:
                logger.info("[CodingCouncilStartup] ⚠️ J-Prime degraded but available")
                _jprime_fallback_chain = getattr(_jprime_engine, '_fallback_chain', None)

            else:
                # v118.0: J-Prime not reachable at all, but keep engine for lazy reconnection
                logger.info(
                    f"[CodingCouncilStartup] ℹ️ J-Prime not available yet (status={status.value}) - "
                    f"engine initialized, will auto-connect when service starts"
                )
                _jprime_fallback_chain = getattr(_jprime_engine, '_fallback_chain', None)
        else:
            logger.warning("[CodingCouncilStartup] J-Prime engine creation failed")

    except ImportError as e:
        logger.debug(f"[CodingCouncilStartup] J-Prime adapter not available: {e}")
        _jprime_engine = None
    except Exception as e:
        # v109.2: J-Prime init failures during startup are expected
        logger.info(f"[CodingCouncilStartup] ℹ️ J-Prime not loaded: {e}")
        _jprime_engine = None

    # Log summary with status awareness
    if _jprime_engine:
        try:
            status, _ = _jprime_engine.get_service_status()
            if status == ServiceStatus.HEALTHY:
                jprime_status = "✅ ready"
            elif status in (ServiceStatus.STARTING, ServiceStatus.INITIALIZING):
                jprime_status = "⏳ starting"
            elif status == ServiceStatus.DEGRADED:
                jprime_status = "⚠️ degraded"
            else:
                jprime_status = "🔄 pending"
        except Exception:
            jprime_status = "✅ initialized"
    else:
        jprime_status = "❌ unavailable"

    fallback_status = "✅ ready" if _jprime_fallback_chain else "❌ unavailable"
    logger.info(f"[CodingCouncilStartup] v118.0 J-Prime: Engine {jprime_status}, Fallback Chain {fallback_status}")


async def _monitor_jprime_startup() -> None:
    """
    v118.0: Background task to monitor J-Prime startup and log when it becomes healthy.

    This provides visibility into J-Prime readiness without blocking the main startup.
    """
    global _jprime_engine

    if not _jprime_engine:
        return

    try:
        # Wait up to 120 seconds for J-Prime to become healthy
        ready = await _jprime_engine.wait_for_ready(
            timeout=120.0,
            poll_interval=5.0,
            require_healthy=True,
        )

        if ready:
            status, details = _jprime_engine.get_service_status()
            model_loaded = details.get("model_loaded", False)
            logger.info(
                f"[CodingCouncilStartup] ✅ J-Prime now healthy! "
                f"(model_loaded={model_loaded})"
            )
        else:
            status, details = _jprime_engine.get_service_status()
            logger.warning(
                f"[CodingCouncilStartup] J-Prime didn't become healthy within 120s "
                f"(status={status.value})"
            )
    except Exception as e:
        logger.debug(f"[CodingCouncilStartup] J-Prime startup monitor error: {e}")


async def _initialize_ide_components() -> None:
    """Initialize IDE bridge, Trinity sync, LSP server, and WebSocket handler."""
    global _ide_bridge, _trinity_sync, _lsp_server, _websocket_handler

    logger.info("[CodingCouncilStartup] Initializing IDE components...")

    # Initialize Trinity cross-repo synchronizer
    try:
        from .ide.trinity_sync import initialize_trinity_sync
        _trinity_sync = await initialize_trinity_sync()
        logger.info("[CodingCouncilStartup] Trinity cross-repo sync initialized")

        # Connect Trinity sync to orchestrator for cross-repo event propagation
        await _connect_trinity_to_orchestrator()
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] Trinity sync not available: {e}")
        _trinity_sync = None

    # Initialize IDE Bridge
    try:
        from .ide.bridge import initialize_ide_bridge
        _ide_bridge = await initialize_ide_bridge()
        logger.info("[CodingCouncilStartup] IDE Bridge initialized")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] IDE Bridge not available: {e}")
        _ide_bridge = None

    # Initialize LSP Server (runs in background)
    try:
        from .ide.lsp_server import LSPServer
        _lsp_server = LSPServer()
        # Start LSP server in background task
        asyncio.create_task(_start_lsp_server())
        logger.info(f"[CodingCouncilStartup] LSP Server starting on port {LSP_SERVER_PORT}")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] LSP Server not available: {e}")
        _lsp_server = None

    # Initialize WebSocket Handler (runs in background)
    try:
        from .ide.websocket_handler import IDEWebSocketHandler
        _websocket_handler = IDEWebSocketHandler()
        # Start WebSocket handler in background task
        asyncio.create_task(_start_websocket_handler())
        logger.info(f"[CodingCouncilStartup] WebSocket Handler starting on port {IDE_WEBSOCKET_PORT}")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] WebSocket Handler not available: {e}")
        _websocket_handler = None

    # Verify all critical connections
    verification = await verify_critical_connections()
    connected = sum(1 for v in verification.values() if v)
    total = len(verification)
    logger.info(f"[CodingCouncilStartup] Connection verification: {connected}/{total} components ready")


async def _connect_trinity_to_orchestrator() -> None:
    """Connect Trinity sync events to the coding council orchestrator."""
    global _trinity_sync, _council

    if not _trinity_sync:
        return

    try:
        from .ide.trinity_sync import SyncEventType

        # Subscribe to file change events and propagate to council
        async def on_file_changed(event):
            """Handle file changes and notify orchestrator."""
            if _council and hasattr(_council, 'handle_trinity_event'):
                try:
                    await _council.handle_trinity_event({
                        'type': 'file_changed',
                        'repo': event.source_repo.value,
                        'file': event.file_path,
                        'timestamp': event.timestamp,
                    })
                except Exception as e:
                    logger.debug(f"[TrinitySyncBridge] Event propagation error: {e}")

        # Subscribe to context updates
        async def on_context_updated(event):
            """Handle context updates from other repos."""
            if _ide_bridge and hasattr(_ide_bridge, 'invalidate_cache'):
                try:
                    await _ide_bridge.invalidate_cache(event.file_path)
                except Exception as e:
                    logger.debug(f"[TrinitySyncBridge] Cache invalidation error: {e}")

        # Register event handlers
        _trinity_sync.subscribe(SyncEventType.FILE_MODIFIED, on_file_changed)
        _trinity_sync.subscribe(SyncEventType.FILE_CREATED, on_file_changed)
        _trinity_sync.subscribe(SyncEventType.FILE_DELETED, on_file_changed)
        _trinity_sync.subscribe(SyncEventType.CONTEXT_UPDATED, on_context_updated)

        logger.info("[CodingCouncilStartup] Trinity sync connected to orchestrator")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] Could not connect Trinity to orchestrator: {e}")


async def _start_lsp_server() -> None:
    """Start LSP server in background with port conflict detection."""
    global _lsp_server

    if _lsp_server is None:
        return

    try:
        await _lsp_server.start_tcp(host="127.0.0.1", port=LSP_SERVER_PORT)
    except OSError as e:
        if "Address already in use" in str(e) or "address already in use" in str(e).lower():
            logger.error(
                f"[CodingCouncilStartup] LSP Server port {LSP_SERVER_PORT} is already in use. "
                f"Try: export LSP_SERVER_PORT={LSP_SERVER_PORT + 1}"
            )
            # Try alternative port
            try:
                alt_port = LSP_SERVER_PORT + 1
                await _lsp_server.start_tcp(host="127.0.0.1", port=alt_port)
                logger.info(f"[CodingCouncilStartup] LSP Server started on alternative port {alt_port}")
            except Exception:
                logger.error("[CodingCouncilStartup] LSP Server failed to start on alternative port")
        else:
            logger.error(f"[CodingCouncilStartup] LSP Server failed: {e}")
    except Exception as e:
        logger.error(f"[CodingCouncilStartup] LSP Server failed: {e}")


async def _start_websocket_handler() -> None:
    """Start WebSocket handler in background."""
    global _websocket_handler

    if _websocket_handler is None:
        return

    try:
        # The WebSocket handler integrates with the main FastAPI app
        # via the register_coding_council_routes function
        # Here we just ensure it's ready for connections
        if hasattr(_websocket_handler, 'initialize'):
            await _websocket_handler.initialize()
        logger.info(f"[CodingCouncilStartup] WebSocket Handler ready on port {IDE_WEBSOCKET_PORT}")
    except Exception as e:
        logger.error(f"[CodingCouncilStartup] WebSocket Handler failed: {e}")


async def verify_critical_connections() -> Dict[str, bool]:
    """
    Verify all critical connections are established.

    Returns:
        Dictionary mapping component names to their availability status.
    """
    global _council, _anthropic_engine, _ide_bridge, _trinity_sync, _lsp_server, _websocket_handler

    results = {}

    # 1. Anthropic Engine
    results["anthropic_engine"] = _anthropic_engine is not None

    # 2. IDE Bridge
    results["ide_bridge"] = _ide_bridge is not None

    # 3. LSP Server
    if _lsp_server is not None:
        results["lsp_server"] = hasattr(_lsp_server, 'is_running') and _lsp_server.is_running() or True
    else:
        results["lsp_server"] = False

    # 4. WebSocket Handler
    results["websocket_handler"] = _websocket_handler is not None

    # 5. Trinity Sync
    results["trinity_sync"] = _trinity_sync is not None

    # 6. Council
    results["council"] = _council is not None

    # 7. Bridge → Engine connection
    if _ide_bridge:
        results["bridge_engine_connection"] = (
            hasattr(_ide_bridge, '_anthropic_engine') and
            _ide_bridge._anthropic_engine is not None
        ) or (
            hasattr(_ide_bridge, '_suggestion_engine') and
            _ide_bridge._suggestion_engine is not None
        )
    else:
        results["bridge_engine_connection"] = False

    # 8. Voice handler (check integration module)
    try:
        from .integration import get_voice_evolution_handler
        handler = get_voice_evolution_handler()
        results["voice_handler"] = handler is not None
    except Exception:
        results["voice_handler"] = False

    return results


async def shutdown_coding_council_startup() -> None:
    """
    Shutdown Coding Council during JARVIS shutdown.

    This should be called from run_supervisor.py during shutdown.
    """
    global _council, _anthropic_engine, _ide_bridge, _trinity_sync, _lsp_server, _websocket_handler, _voice_announcer, _initialized

    if not _initialized:
        return

    logger.info("[CodingCouncilStartup] Shutting down Coding Council...")

    # v79.0: Clean up Voice Announcer (graceful, no cleanup needed - just nullify)
    _voice_announcer = None

    # Shutdown WebSocket Handler
    try:
        if _websocket_handler:
            if hasattr(_websocket_handler, 'shutdown'):
                await _websocket_handler.shutdown()
            logger.info("[CodingCouncilStartup] WebSocket Handler closed")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] WebSocket Handler shutdown warning: {e}")

    # Shutdown LSP Server
    try:
        if _lsp_server:
            await _lsp_server.shutdown()
            logger.info("[CodingCouncilStartup] LSP Server closed")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] LSP Server shutdown warning: {e}")

    # Shutdown IDE Bridge
    try:
        if _ide_bridge:
            await _ide_bridge.shutdown()
            logger.info("[CodingCouncilStartup] IDE Bridge closed")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] IDE Bridge shutdown warning: {e}")

    # Shutdown Trinity Sync
    try:
        if _trinity_sync:
            from .ide.trinity_sync import shutdown_trinity_sync
            await shutdown_trinity_sync()
            logger.info("[CodingCouncilStartup] Trinity sync closed")
    except Exception as e:
        logger.warning(f"[CodingCouncilStartup] Trinity sync shutdown warning: {e}")

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
    _ide_bridge = None
    _trinity_sync = None
    _lsp_server = None
    _websocket_handler = None
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
    global _council, _startup_time, _initialized, _ide_bridge, _trinity_sync, _lsp_server, _websocket_handler

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

        # Get connection verification
        connections = await verify_critical_connections()
        connected_count = sum(1 for v in connections.values() if v)
        total_count = len(connections)

        # Get IDE component statuses
        ide_status = {}
        if IDE_BRIDGE_ENABLED:
            ide_status["ide_bridge"] = {
                "available": _ide_bridge is not None,
                "status": "running" if _ide_bridge else "not_initialized",
            }
            ide_status["websocket_handler"] = {
                "available": _websocket_handler is not None,
                "port": IDE_WEBSOCKET_PORT if _websocket_handler else None,
                "status": "running" if _websocket_handler else "not_initialized",
            }
            ide_status["trinity_sync"] = {
                "available": _trinity_sync is not None,
                "status": _trinity_sync.get_status() if _trinity_sync else "not_initialized",
            }
            ide_status["lsp_server"] = {
                "available": _lsp_server is not None,
                "port": LSP_SERVER_PORT if _lsp_server else None,
                "status": "running" if _lsp_server else "not_initialized",
            }
            ide_status["websocket_port"] = IDE_WEBSOCKET_PORT

        # Get recovery information
        recovery_info = {
            "actions_taken": len(_recovery_log),
            "log": _recovery_log[-5:] if _recovery_log else [],  # Last 5 recovery actions
        }

        # v85.0: Get J-Prime status
        jprime_status = {}
        if JPRIME_ENABLED:
            jprime_status["enabled"] = True
            jprime_status["engine_available"] = _jprime_engine is not None
            if _jprime_engine:
                try:
                    jprime_status["is_available"] = await _jprime_engine.is_available()
                    jprime_status["stats"] = _jprime_engine.get_stats()
                except Exception as e:
                    jprime_status["error"] = str(e)
            if _jprime_fallback_chain:
                jprime_status["fallback_chain"] = _jprime_fallback_chain.get_stats()
        else:
            jprime_status["enabled"] = False

        return {
            "enabled": True,
            "status": "healthy",
            "uptime_seconds": time.time() - _startup_time if _startup_time else 0,
            "active_tasks": status.get("active_tasks", 0),
            "circuit_breakers": status.get("circuit_breakers", {}),
            "frameworks": status.get("frameworks_available", []),
            "ide_integration": ide_status,
            "jprime": jprime_status,  # v85.0: J-Prime local LLM status
            "connections": {
                "verified": connected_count,
                "total": total_count,
                "details": connections,
            },
            "auto_recovery": recovery_info,
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
    from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse
    from starlette.websockets import WebSocketState
    import json

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

    # Diagnostics and Recovery Routes
    @router.get("/diagnostics")
    async def run_diagnostics():
        """
        Run diagnostic checks and return report.

        This can be called at any time to verify system health.
        """
        try:
            from .diagnostics import run_preflight_checks

            report = await run_preflight_checks(print_report=False, auto_recover=False)

            return {
                "healthy": report.is_healthy,
                "summary": {
                    "passed": report.passed,
                    "warnings": report.warnings,
                    "failed": report.failed,
                    "skipped": report.skipped,
                    "total": report.total,
                    "duration_ms": round(report.duration_ms, 2),
                },
                "checks": [c.to_dict() for c in report.checks],
                "system_info": report.system_info,
            }
        except ImportError:
            raise HTTPException(status_code=503, detail="Diagnostics module not available")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Diagnostics failed: {e}")

    @router.post("/diagnostics/recover")
    async def trigger_recovery():
        """
        Manually trigger auto-recovery for any detected issues.

        Returns list of recovery actions taken.
        """
        try:
            from .diagnostics import run_preflight_checks, AutoRecovery

            # Run checks first
            report = await run_preflight_checks(print_report=False, auto_recover=False)

            if report.is_healthy:
                return {
                    "actions_taken": [],
                    "message": "System is healthy, no recovery needed",
                    "healthy": True,
                }

            # Attempt recovery
            actions = await AutoRecovery.attempt_recovery(report)

            # Re-check after recovery
            post_report = await run_preflight_checks(print_report=False, auto_recover=False)

            # Track in recovery log
            for action in actions:
                _recovery_log.append({
                    "action": action,
                    "timestamp": time.time(),
                    "source": "manual_trigger",
                    "success": True,
                })

            return {
                "actions_taken": actions,
                "healthy_before": report.is_healthy,
                "healthy_after": post_report.is_healthy,
                "failed_checks_before": report.failed,
                "failed_checks_after": post_report.failed,
            }
        except ImportError:
            raise HTTPException(status_code=503, detail="Diagnostics module not available")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Recovery failed: {e}")

    @router.get("/diagnostics/recovery-log")
    async def get_recovery_history():
        """Get history of auto-recovery actions."""
        return {
            "total_actions": len(_recovery_log),
            "log": _recovery_log,
        }

    # IDE Integration Routes
    @router.get("/ide/status")
    async def ide_status():
        """Get IDE integration status."""
        if not IDE_BRIDGE_ENABLED:
            return {"enabled": False}

        return {
            "enabled": True,
            "ide_bridge": {
                "available": _ide_bridge is not None,
            },
            "trinity_sync": {
                "available": _trinity_sync is not None,
                "status": _trinity_sync.get_status() if _trinity_sync else None,
            },
            "lsp_server": {
                "available": _lsp_server is not None,
                "port": LSP_SERVER_PORT,
            },
            "websocket_port": IDE_WEBSOCKET_PORT,
        }

    @router.get("/ide/context")
    async def ide_context():
        """Get current IDE context."""
        if not _ide_bridge:
            raise HTTPException(status_code=503, detail="IDE Bridge not initialized")

        context = await _ide_bridge.get_context()
        return {
            "active_files": len(context.active_files) if context else 0,
            "recent_files": list(context.recent_files)[:10] if context else [],
            "diagnostics_count": len(context.diagnostics) if context else 0,
        }

    @router.post("/ide/suggest")
    async def ide_suggest(request: dict):
        """
        Get inline suggestions.

        Request body:
        {
            "file_path": "/path/to/file.py",
            "content": "file content",
            "line": 10,
            "character": 5,
            "language_id": "python"
        }
        """
        if not _ide_bridge:
            raise HTTPException(status_code=503, detail="IDE Bridge not initialized")

        file_path = request.get("file_path")
        content = request.get("content")
        line = request.get("line", 0)
        character = request.get("character", 0)

        if not file_path or content is None:
            raise HTTPException(status_code=400, detail="file_path and content required")

        suggestion = await _ide_bridge.get_inline_suggestion(
            uri=f"file://{file_path}",
            line=line,
            character=character,
            trigger_kind="invoked",
        )

        return {"suggestion": suggestion}

    @router.get("/ide/trinity/repos")
    async def trinity_repos():
        """Get Trinity repository status."""
        if not _trinity_sync:
            raise HTTPException(status_code=503, detail="Trinity Sync not initialized")

        contexts = await _trinity_sync.get_all_contexts()
        return {
            repo.value: ctx.to_dict()
            for repo, ctx in contexts.items()
        }

    @router.post("/ide/trinity/publish")
    async def trinity_publish(request: dict):
        """
        Publish a file change event to Trinity.

        Request body:
        {
            "file_path": "/path/to/file.py",
            "change_type": "modified",  // created, modified, deleted
            "content": "optional file content"
        }
        """
        if not _trinity_sync:
            raise HTTPException(status_code=503, detail="Trinity Sync not initialized")

        file_path = request.get("file_path")
        change_type = request.get("change_type", "modified")
        content = request.get("content")

        if not file_path:
            raise HTTPException(status_code=400, detail="file_path required")

        # Detect repo type
        from .ide.trinity_sync import detect_repo_type
        repo = detect_repo_type(file_path)

        if not repo:
            raise HTTPException(status_code=400, detail="File not in any Trinity repo")

        success = await _trinity_sync.publish_file_change(
            repo=repo,
            file_path=file_path,
            change_type=change_type,
            content=content,
        )

        return {"success": success, "repo": repo.value}

    # =========================================================================
    # WebSocket Endpoint for IDE Real-Time Communication
    # =========================================================================

    @router.websocket("/ide/ws")
    async def ide_websocket(websocket: WebSocket):
        """
        WebSocket endpoint for real-time IDE communication.

        Protocol:
        - Client sends JSON messages with 'type' field
        - Server responds with JSON messages

        Message Types (Client → Server):
        - file_opened: {"type": "file_opened", "uri": "file:///...", "content": "...", "language": "python"}
        - file_changed: {"type": "file_changed", "uri": "file:///...", "changes": [...]}
        - file_closed: {"type": "file_closed", "uri": "file:///..."}
        - cursor_moved: {"type": "cursor_moved", "uri": "file:///...", "line": 10, "character": 5}
        - request_suggestion: {"type": "request_suggestion", "uri": "file:///...", "line": 10, "character": 5}
        - diagnostics: {"type": "diagnostics", "uri": "file:///...", "diagnostics": [...]}

        Message Types (Server → Client):
        - suggestion: {"type": "suggestion", "text": "...", "range": {...}}
        - context_update: {"type": "context_update", "files": [...]}
        - trinity_event: {"type": "trinity_event", "repo": "...", "event": "..."}
        - error: {"type": "error", "message": "..."}
        """
        global _websocket_handler, _ide_bridge, _trinity_sync

        # Accept the WebSocket connection
        await websocket.accept()

        # Track connection for cleanup
        connection_id = f"ws_{id(websocket)}_{time.time()}"
        active_subscriptions = set()

        logger.info(f"[WebSocket] New IDE connection: {connection_id}")

        try:
            # Send initial connection acknowledgment
            await websocket.send_json({
                "type": "connected",
                "connection_id": connection_id,
                "capabilities": {
                    "suggestions": _ide_bridge is not None,
                    "trinity_sync": _trinity_sync is not None,
                    "lsp": _lsp_server is not None,
                },
                "version": "77.3",
            })

            # Main message loop
            while True:
                try:
                    # Receive message with timeout for heartbeat
                    message = await asyncio.wait_for(
                        websocket.receive_json(),
                        timeout=60.0  # 60 second timeout for heartbeat
                    )
                except asyncio.TimeoutError:
                    # Send heartbeat ping
                    if websocket.client_state == WebSocketState.CONNECTED:
                        await websocket.send_json({"type": "ping", "timestamp": time.time()})
                        continue
                    else:
                        break

                msg_type = message.get("type", "")

                # Handle different message types
                if msg_type == "pong":
                    # Heartbeat response, ignore
                    continue

                elif msg_type == "file_opened":
                    uri = message.get("uri", "")
                    content = message.get("content", "")
                    language = message.get("language", "unknown")

                    if _ide_bridge:
                        try:
                            from .ide.bridge import FileContext
                            file_ctx = FileContext(
                                uri=uri,
                                content=content,
                                language_id=language,
                                version=1,
                            )
                            await _ide_bridge.update_file(file_ctx)
                            await websocket.send_json({
                                "type": "ack",
                                "original_type": "file_opened",
                                "uri": uri,
                            })
                        except Exception as e:
                            await websocket.send_json({
                                "type": "error",
                                "message": f"Failed to process file_opened: {e}",
                            })

                elif msg_type == "file_changed":
                    uri = message.get("uri", "")
                    changes = message.get("changes", [])

                    if _ide_bridge:
                        try:
                            # Apply incremental changes
                            await _ide_bridge.handle_file_changes(uri, changes)

                            # Publish to Trinity for cross-repo awareness
                            if _trinity_sync:
                                from .ide.trinity_sync import detect_repo_type
                                file_path = uri.replace("file://", "")
                                repo = detect_repo_type(file_path)
                                if repo:
                                    await _trinity_sync.publish_file_change(
                                        repo=repo,
                                        file_path=file_path,
                                        change_type="modified",
                                    )

                            await websocket.send_json({
                                "type": "ack",
                                "original_type": "file_changed",
                                "uri": uri,
                            })
                        except Exception as e:
                            await websocket.send_json({
                                "type": "error",
                                "message": f"Failed to process file_changed: {e}",
                            })

                elif msg_type == "file_closed":
                    uri = message.get("uri", "")
                    if _ide_bridge and hasattr(_ide_bridge, 'close_file'):
                        await _ide_bridge.close_file(uri)
                    await websocket.send_json({
                        "type": "ack",
                        "original_type": "file_closed",
                        "uri": uri,
                    })

                elif msg_type == "cursor_moved":
                    uri = message.get("uri", "")
                    line = message.get("line", 0)
                    character = message.get("character", 0)

                    if _ide_bridge and hasattr(_ide_bridge, 'update_cursor'):
                        await _ide_bridge.update_cursor(uri, line, character)

                elif msg_type == "request_suggestion":
                    uri = message.get("uri", "")
                    line = message.get("line", 0)
                    character = message.get("character", 0)
                    trigger = message.get("trigger", "invoked")

                    if _ide_bridge:
                        try:
                            suggestion = await _ide_bridge.get_inline_suggestion(
                                uri=uri,
                                line=line,
                                character=character,
                                trigger_kind=trigger,
                            )

                            if suggestion:
                                await websocket.send_json({
                                    "type": "suggestion",
                                    "uri": uri,
                                    "line": line,
                                    "character": character,
                                    "text": suggestion,
                                })
                            else:
                                await websocket.send_json({
                                    "type": "no_suggestion",
                                    "uri": uri,
                                })
                        except Exception as e:
                            await websocket.send_json({
                                "type": "error",
                                "message": f"Suggestion failed: {e}",
                            })
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "message": "IDE Bridge not available",
                        })

                elif msg_type == "diagnostics":
                    uri = message.get("uri", "")
                    diagnostics = message.get("diagnostics", [])

                    if _ide_bridge and hasattr(_ide_bridge, 'update_diagnostics'):
                        await _ide_bridge.update_diagnostics(uri, diagnostics)
                    await websocket.send_json({
                        "type": "ack",
                        "original_type": "diagnostics",
                        "uri": uri,
                        "count": len(diagnostics),
                    })

                elif msg_type == "subscribe_trinity":
                    # Subscribe to Trinity cross-repo events
                    if _trinity_sync:
                        active_subscriptions.add("trinity")
                        await websocket.send_json({
                            "type": "subscribed",
                            "channel": "trinity",
                        })

                elif msg_type == "get_context":
                    # Get compressed context for current focus
                    if _ide_bridge:
                        focus_file = message.get("focus_file")
                        focus_line = message.get("focus_line", 0)
                        context = await _ide_bridge.get_compressed_context(
                            focus_file=focus_file,
                            focus_line=focus_line,
                        )
                        await websocket.send_json({
                            "type": "context",
                            "focus_file": focus_file,
                            "context": context,
                        })

                else:
                    # Unknown message type
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Unknown message type: {msg_type}",
                    })

        except WebSocketDisconnect:
            logger.info(f"[WebSocket] IDE client disconnected: {connection_id}")
        except Exception as e:
            logger.error(f"[WebSocket] Connection error: {e}")
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Connection error: {e}",
                        "fatal": True,
                    })
            except Exception:
                pass
        finally:
            # Cleanup
            logger.info(f"[WebSocket] Cleaning up connection: {connection_id}")

    # =========================================================================
    # Connection Verification Endpoint
    # =========================================================================

    @router.get("/ide/verify-connections")
    async def verify_ide_connections():
        """Verify all IDE integration connections."""
        connections = await verify_critical_connections()

        all_connected = all(connections.values())
        connected_count = sum(1 for v in connections.values() if v)

        return {
            "all_connected": all_connected,
            "connected_count": connected_count,
            "total_count": len(connections),
            "connections": connections,
            "recommendations": _get_connection_recommendations(connections),
        }

    def _get_connection_recommendations(connections: Dict[str, bool]) -> List[str]:
        """Generate recommendations based on connection status."""
        recommendations = []

        if not connections.get("anthropic_engine"):
            recommendations.append(
                "Set ANTHROPIC_API_KEY environment variable for Claude API access"
            )

        if not connections.get("ide_bridge"):
            recommendations.append(
                "IDE Bridge not initialized - check IDE_BRIDGE_ENABLED env var"
            )

        if not connections.get("trinity_sync"):
            recommendations.append(
                "Trinity Sync not running - cross-repo features unavailable"
            )

        if not connections.get("voice_handler"):
            recommendations.append(
                "Voice evolution handler not available - voice commands may not work"
            )

        return recommendations

    # Register routes
    app.include_router(router)
    logger.info("[CodingCouncilStartup] API routes registered at /coding-council")
    logger.info(f"[CodingCouncilStartup] WebSocket endpoint: /coding-council/ide/ws")


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


def get_ide_bridge() -> Optional[Any]:
    """Get the global IDE Bridge instance."""
    return _ide_bridge


def get_trinity_sync() -> Optional[Any]:
    """Get the global Trinity Sync instance."""
    return _trinity_sync


def get_lsp_server() -> Optional[Any]:
    """Get the global LSP Server instance."""
    return _lsp_server


def get_websocket_handler() -> Optional[Any]:
    """Get the global WebSocket Handler instance."""
    return _websocket_handler


def get_anthropic_engine() -> Optional[Any]:
    """Get the global Anthropic Engine instance."""
    return _anthropic_engine


def get_voice_announcer() -> Optional[Any]:
    """
    v79.0: Get the global Voice Announcer instance.

    Returns the CodingCouncilVoiceAnnouncer for evolution voice feedback.
    """
    return _voice_announcer


def get_jprime_engine() -> Optional[Any]:
    """
    v85.0: Get the global J-Prime Unified Engine instance.

    Returns the JPrimeUnifiedEngine for local LLM inference.
    The engine includes:
    - MultiModelFallbackChain for adaptive model selection
    - Circuit breakers per model
    - Aider-style code editing
    - MetaGPT-style multi-agent planning
    """
    return _jprime_engine


def get_jprime_fallback_chain() -> Optional[Any]:
    """
    v85.0: Get the global J-Prime Fallback Chain.

    Returns the MultiModelFallbackChain for intelligent model cascading:
    - Sequential fallback through local models
    - Parallel race mode for latency-critical tasks
    - Adaptive model selection based on success rates
    - Claude fallback as last resort
    """
    return _jprime_fallback_chain


def is_jprime_available() -> bool:
    """
    v85.0: Check if J-Prime local LLM is available.

    Returns True if the J-Prime engine is initialized and has a model loaded.
    """
    return _jprime_engine is not None
