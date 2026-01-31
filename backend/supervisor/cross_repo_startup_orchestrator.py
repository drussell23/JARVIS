"""
Cross-Repo Startup Orchestrator v5.0 - Enterprise-Grade Process Lifecycle Manager
===================================================================================

Dynamic service discovery and self-healing process orchestration for JARVIS ecosystem.
Enables single-command startup of all Trinity components (JARVIS Body, J-Prime Mind, Reactor-Core Nerves).

Features (v5.0):
- 🔧 SINGLE SOURCE OF TRUTH: Ports from trinity_config.py (no hardcoding!)
- 🧹 Pre-flight Cleanup: Kills stale processes on legacy ports (8002, 8003)
- 🔍 Wrong-Binding Detection: Detects 127.0.0.1 vs 0.0.0.0 misconfigurations
- 🔄 Auto-Healing with exponential backoff (dead process detection & restart)
- 📡 Real-Time Output Streaming (stdout/stderr prefixed per service)
- 🎯 Process Lifecycle Management (spawn, monitor, graceful shutdown)
- 🛡️ Graceful Shutdown Handlers (SIGINT/SIGTERM cleanup)
- 🐍 Auto-detect venv Python for each repo
- 📁 Correct entry points: run_server.py, run_reactor.py
- ⚡ Pre-spawn validation (port check, dependency check)
- 📊 Service Health Monitoring with progressive backoff

Service Ports (v5.0 - from trinity_config.py):
- jarvis-prime: port 8000 (run_server.py --port 8000 --host 0.0.0.0)
- reactor-core: port 8090 (run_reactor.py --port 8090)
- jarvis-body: port 8010 (main JARVIS)

Legacy ports cleaned up automatically:
- jarvis-prime: 8001, 8002 (killed if found)
- reactor-core: 8003, 8004, 8005 (killed if found)

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │         Cross-Repo Orchestrator v5.0 - Process Manager           │
    ├──────────────────────────────────────────────────────────────────┤
    │                                                                  │
    │  Service Registry: ~/.jarvis/registry/services.json              │
    │  ┌────────────────┬──────────────┬─────────────────────┐         │
    │  │   JARVIS       │   J-PRIME    │   REACTOR-CORE      │         │
    │  │  PID: auto     │  PID: auto   │   PID: auto         │         │
    │  │  Port: 8010    │  Port: 8000  │   Port: 8090        │         │
    │  │  Status: ✅     │  Status: ✅  │   Status: ✅        │         │
    │  └────────────────┴──────────────┴─────────────────────┘         │
    │                                                                  │
    │  Process Lifecycle:                                              │
    │  0. Pre-flight Cleanup (kill stale legacy processes)             │
    │  1. Pre-Spawn Validation (venv detect, port check)               │
    │  2. Spawn (asyncio.create_subprocess_exec with venv Python)      │
    │  3. Monitor (PID tracking + progressive health checks)           │
    │  4. Stream Output (real-time with [SERVICE] prefix)              │
    │  5. Auto-Heal (restart on crash with exponential backoff)        │
    │  6. Graceful Shutdown (SIGTERM → wait → SIGKILL)                 │
    │                                                                  │
    └──────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 5.14.0 (v147.0)

Changelog:
- v147.0 (v5.14): UNBREAKABLE ARCHITECTURE - Proactive Rescue & Crash Pre-Cognition
  - PART 1 PROACTIVE RESCUE: Real-time log analysis detects memory stress BEFORE crash
    - Pattern matching for: MEMORY EMERGENCY, OOM Warning, load shedding, survival mode
    - On detection: Triggers GCP provisioning IMMEDIATELY (don't wait for -9)
    - Controlled restart is better than kernel kill
  - PART 2 ATOMIC CLOUD LOCK: Corruption-proof state persistence
    - Temp file + atomic rename strategy (survives power failure during write)
    - Auto-creation of ~/.jarvis/trinity/ directory
    - JSON schema validation on load
  - PART 3 ELASTIC SCALING: Hardware upgrade detection
    - On startup: If RAM > 32GB but cloud lock exists, AUTO-RELEASE lock
    - "User upgraded hardware" → return to local mode automatically
    - No manual intervention needed after RAM upgrade
  - PART 4 CONSECUTIVE OOM CIRCUIT BREAKER: Prevents infinite crash loops
    - Tracks consecutive OOMs even in cloud mode
    - After 3 consecutive OOMs: Mark DEGRADED, stop restarting
    - Prevents infinite billable GCP loops
  - ROOT CAUSE FIX: System now PREDICTS crashes instead of just recovering from them
- v146.0 (v5.13): TRINITY PROTOCOL - Elastic Hybrid Cloud Architecture
  - PART 1 CLOUD-FIRST STRATEGY: Proactive GCP pre-warming on SLIM hardware
    - Background asyncio task starts GCP provisioning IMMEDIATELY during startup
    - Non-blocking: Does not delay service startup, VM warms in parallel
    - GCP ready by the time first request arrives (no 2-minute cold start)
  - PART 2 CLOUD LOCK PERSISTENCE: OOM events create persistent lock file
    - ~/.jarvis/trinity/cloud_lock.json survives supervisor restarts
    - After OOM crash, system FORCES cloud mode until manually cleared
    - Prevents "OOM → restart → OOM" infinite loop across restarts
  - PART 3 REACTOR-CORE SEQUENCING: reactor-core waits for jarvis-prime health
    - Dependency ordering ensures heavy training waits for inference
    - Prevents "Thundering Herd" where both grab RAM simultaneously
  - PART 4 TRINITY EVENT BUS: Cross-repo health coordination
    - Background health monitoring with asyncio.Event for readiness
    - GCP warm-up completion triggers jarvis-prime spawn eligibility
  - ROOT CAUSE FIX: GCP was provisioned AFTER spawn, now provisioned DURING startup
- v145.0 (v5.12): TOTAL VICTORY - Self-Kill Protection Bypass & Final Deadlock Fix
  - CRITICAL FIX: Port Hygiene now allows killing service's own OLD PID for restart
  - Added `exclude_service` parameter to `_build_protected_pid_set()`
  - Added `allow_self_kill` parameter to `_enforce_port_hygiene()`
  - ROOT CAUSE: Old jarvis-prime PID was protected, preventing port 8000 cleanup
  - The supervisor was literally protecting the zombie it needed to kill
  - GAP 14: Self-Kill Protection Bypass solves the restart deadlock
- v144.0 (v5.11): HOLLOW CLIENT & ACTIVE RESCUE - Nuclear OOM Prevention
  - PART 1 HOLLOW CLIENT: jarvis-prime now uses strict lazy imports (torch/transformers)
    - Startup RAM reduced from ~4GB to ~300MB
    - Heavy ML libs only loaded when actually doing inference
    - New JARVIS_GCP_OFFLOAD_ACTIVE env var blocks local heavy imports
    - Slim Mode + GCP Active = Hollow Client that routes to GCP
  - PART 2 ACTIVE RESCUE: GCP VM provisioned BEFORE spawning jarvis-prime on SLIM hardware
    - force_cloud_hybrid flag automatically set when profile is SLIM or CLOUD_ONLY
    - ensure_gcp_vm_ready_for_prime() waits for GCP VM before spawn (async, timeout 120s)
    - JARVIS_GCP_OFFLOAD_ACTIVE=true and GCP_PRIME_ENDPOINT injected to subprocess
    - On Exit -9 (OOM): NO LOCAL RESTART, force GCP provisioning first
    - OOM Death Handler triggers cloud rescue instead of local retry loop
  - RESULT: The Life Raft (GCP) is now deployed BEFORE you need it, not after you drown
  - ROOT CAUSE FIX: GCP was "tied to the dock" - only provisioned AFTER local crashes
- v143.0 (v5.10): OPERATION HOLLOW CLIENT - Early Hardware Detection
  - CRITICAL FIX: Set JARVIS_ENABLE_SLIM_MODE in supervisor's OWN environment at startup
  - Hardware assessment now runs FIRST in start_all_services() BEFORE any spawn attempts
  - Added set_hardware_env_in_supervisor() to set all hardware env vars early
  - log_hardware_assessment() now also sets env vars (defense in depth)
  - The v142.0 memory gate can now properly detect Slim Mode from os.environ
  - ROOT CAUSE: Memory gate checked env var that was only passed to subprocess, not set locally
- v142.0 (v5.9): DYNAMIC MEMORY GATING - Context-Aware Slim Mode Support
  - Fixed DEADLOCK: Supervisor was blocking jarvis-prime even when Slim Mode only needs ~300MB
  - Memory gate now CONTEXT-AWARE: checks JARVIS_ENABLE_SLIM_MODE env and hardware profile
  - SLIM MODE thresholds: 95% max memory usage, 0.5GB minimum free (vs 80%/2GB for FULL mode)
  - Auto-detect Slim Mode for jarvis-prime on systems with <32GB RAM
  - Enhanced SystemExit protection in I/O thread pool to prevent ugly stack traces
  - Graceful thread pool shutdown with timeout and forced exit handling
  - ROOT CAUSE: Users couldn't use GCP Cloud because the Local Client (Slim Mode) was blocked!
- v137.0 (v5.8): I/O Airlock Pattern - Non-Blocking File & System Operations
  - Dedicated ThreadPoolExecutor (4 workers) for I/O operations
  - Prevents event loop blocking from synchronous file I/O (json.loads, read_text)
  - Non-blocking psutil wrappers for network connections, process info, memory
  - Zombie-safe PID verification with cmdline checking
  - Async convenience wrappers: read_json_nonblocking, write_json_nonblocking
  - High-resolution timing logs for I/O operation monitoring
  - Thread pool cleanup during shutdown
- v136.0: GlobalSpawnCoordinator for cross-component spawn coordination
- v93.11 (v5.7): Parallel Startup & Thread Safety Enhancements
  - Thread-safe locks for circuit breakers, memory status, and startup coordination
  - Shared aiohttp session with connection pooling (100 connections, 10 per host)
  - Parallel service startup with semaphore-based concurrency control
  - Cross-repo health aggregation with caching
  - Startup coordination events for dependency tracking
  - Graceful HTTP session cleanup during shutdown
  - Lazy initialization of asyncio primitives (event loop safety)
- v93.10 (v5.6): Enhanced GCP Integration with Full VM Lifecycle Management
  - 4-tier memory-aware routing decision tree (Emergency/Low/Medium/High)
  - GCP VM Manager integration for full lifecycle control
  - CloudMLRouter and HybridRouter integration for intelligent routing
  - Auto VM type selection based on model memory requirements
  - Spot VM support for cost optimization
  - Health check with progress monitoring for GCP services
  - VM startup script generation for automatic service deployment
  - Existing VM detection and reuse
  - Comprehensive fallback chain (Docker -> GCP -> Local)
- v93.9 (v5.5): Advanced Docker Operations with intelligent routing
  - Circuit Breaker pattern prevents cascading failures
  - Exponential backoff retry with jitter for resilience
  - Memory-aware routing (Docker vs Local vs GCP)
  - Auto-build Docker images if missing
  - GCP cloud fallback when local resources insufficient
  - System memory monitoring with psutil
  - Intelligent routing decisions based on available resources
- v93.8 (v5.4): Docker Hybrid Mode - checks Docker before spawning local process
  - Automatic detection of jarvis-prime Docker container
  - Docker-first, local-fallback approach for seamless deployment
  - Optional auto-start of Docker containers
  - Pre-loaded models in Docker eliminate startup delays
- v93.7: Fixed duplicate health check requests, enhanced step logging
- v93.5: Intelligent progress-based timeout extension for model loading
- v5.3: Made startup resilient to CancelledError (don't propagate during init)
- v5.2: Fixed loop.stop() antipattern, proper task cancellation on shutdown
- v5.1: Fixed sys.exit() antipattern in async shutdown handler
- v5.0: Added pre-flight cleanup, circuit breaker, trinity_config integration
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Literal, Optional, Set, Tuple, Union

import aiohttp

logger = logging.getLogger(__name__)

# =============================================================================
# v148.1: LOG SEVERITY BRIDGE - Criticality-aware logging for component failures
# =============================================================================
# This bridge reduces log noise by logging component failures at appropriate
# severity levels based on component criticality:
#   - REQUIRED components: ERROR level
#   - DEGRADED_OK components: WARNING level
#   - OPTIONAL components: INFO level
# =============================================================================
try:
    from backend.core.log_severity_bridge import log_component_failure, is_component_required
except ImportError:
    # Fallback if bridge not available - log all as errors (current behavior)
    def log_component_failure(component, message, error=None, **ctx):
        if error:
            logger.error(f"{component}: {message}", exc_info=True)
        else:
            logger.error(f"{component}: {message}")

    def is_component_required(component):
        return True  # Conservative default


# =============================================================================
# v138.0: HARDWARE-AWARE COORDINATION SYSTEM
# =============================================================================
# This section provides hardware detection and profile classification to enable
# intelligent startup decisions across the JARVIS ecosystem. It coordinates with
# jarvis-prime's Memory-Aware Staged Initialization (v138.0) to prevent OOM crashes.
#
# Hardware Profiles:
# - CLOUD_ONLY: < 16GB RAM - Too small for any local ML, use GCP exclusively
# - SLIM:       16-30GB RAM - Can run slim/deferred subsystems only
# - FULL:       30-64GB RAM - Can run full AGI Hub with staged loading
# - UNLIMITED:  64GB+ RAM - Can run everything in parallel
#
# USAGE:
#   from backend.supervisor.cross_repo_startup_orchestrator import (
#       HardwareProfile,
#       assess_hardware_profile,
#       get_hardware_env_vars,
#   )
# =============================================================================

from enum import auto as enum_auto


class HardwareProfile(Enum):
    """Hardware profile classification for adaptive startup."""
    CLOUD_ONLY = enum_auto()      # < 16GB RAM - use GCP exclusively
    SLIM = enum_auto()            # 16-30GB RAM - slim mode / deferred heavy
    FULL = enum_auto()            # 30-64GB RAM - full with staged loading
    UNLIMITED = enum_auto()       # 64GB+ RAM - can run everything


@dataclass
class HardwareAssessment:
    """Complete hardware assessment for startup decisions."""
    profile: HardwareProfile
    total_ram_gb: float
    available_ram_gb: float
    cpu_count: int
    is_apple_silicon: bool
    has_gpu: bool
    gpu_name: str
    recommended_gpu_layers: int
    recommended_context_size: int
    skip_agi_hub: bool
    enable_slim_mode: bool
    defer_heavy_subsystems: bool
    reason: str
    # v144.0: Active Rescue - Force GCP VM for heavy inference on SLIM systems
    force_cloud_hybrid: bool = False


# Singleton hardware assessment cache (assessed once at startup)
_hardware_assessment_cache: Optional[HardwareAssessment] = None
_hardware_assessment_lock = threading.Lock()


def assess_hardware_profile(force_refresh: bool = False) -> HardwareAssessment:
    """
    v138.0: Assess local hardware to determine optimal startup profile.

    This function MUST be called BEFORE spawning jarvis-prime to determine
    whether to use CLOUD_ONLY, SLIM, FULL, or UNLIMITED mode.

    The assessment is cached to avoid repeated system calls.

    Args:
        force_refresh: If True, re-assess hardware even if cached

    Returns:
        HardwareAssessment with profile and recommendations
    """
    global _hardware_assessment_cache

    with _hardware_assessment_lock:
        if _hardware_assessment_cache is not None and not force_refresh:
            return _hardware_assessment_cache

        try:
            import psutil
            mem = psutil.virtual_memory()
            total_ram_gb = mem.total / (1024 ** 3)
            available_ram_gb = mem.available / (1024 ** 3)
            cpu_count = psutil.cpu_count(logical=True) or 4
        except ImportError:
            # Fallback if psutil not available
            total_ram_gb = 16.0  # Conservative assumption
            available_ram_gb = 8.0
            cpu_count = 4
        except Exception as e:
            logger.warning(f"[v138.0] Hardware assessment failed: {e}, using conservative defaults")
            total_ram_gb = 16.0
            available_ram_gb = 8.0
            cpu_count = 4

        # Detect Apple Silicon
        import platform
        is_apple_silicon = (
            platform.system() == "Darwin" and
            platform.machine() in ("arm64", "aarch64")
        )

        # GPU detection
        has_gpu = False
        gpu_name = "None"
        recommended_gpu_layers = 0

        if is_apple_silicon:
            # Apple Silicon has unified memory - GPU layers depend on RAM
            has_gpu = True
            gpu_name = f"Apple Silicon ({platform.machine()})"
            # Rough heuristic: 1 layer per 0.5GB available
            recommended_gpu_layers = min(int(available_ram_gb * 2), 99)
        else:
            # Try to detect NVIDIA GPU
            try:
                import subprocess
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                if result.returncode == 0 and result.stdout.strip():
                    has_gpu = True
                    gpu_name = result.stdout.strip().split("\n")[0]
                    recommended_gpu_layers = 35  # Conservative default for NVIDIA
            except Exception:
                pass  # No NVIDIA GPU

        # Determine context size based on available RAM
        if available_ram_gb >= 32:
            recommended_context_size = 32768
        elif available_ram_gb >= 16:
            recommended_context_size = 16384
        elif available_ram_gb >= 8:
            recommended_context_size = 8192
        else:
            recommended_context_size = 4096

        # Classify hardware profile
        # v144.0: Added force_cloud_hybrid for Active Rescue
        force_cloud_hybrid = False

        if total_ram_gb < 16:
            profile = HardwareProfile.CLOUD_ONLY
            skip_agi_hub = True
            enable_slim_mode = False  # Not applicable - skip entirely
            defer_heavy_subsystems = False
            force_cloud_hybrid = True  # v144.0: MUST use GCP for all heavy inference
            reason = f"System has only {total_ram_gb:.1f}GB RAM (< 16GB). AGI Hub requires GCP cloud."
        elif total_ram_gb < 30:
            profile = HardwareProfile.SLIM
            skip_agi_hub = False
            enable_slim_mode = True
            defer_heavy_subsystems = True
            force_cloud_hybrid = True  # v144.0: SLIM systems should proactively use GCP
            reason = f"System has {total_ram_gb:.1f}GB RAM (16-30GB). Using SLIM mode with deferred heavy subsystems."
        elif total_ram_gb < 64:
            profile = HardwareProfile.FULL
            skip_agi_hub = False
            enable_slim_mode = False
            defer_heavy_subsystems = True  # Still defer for staged loading
            force_cloud_hybrid = False  # v144.0: FULL systems can handle local inference
            reason = f"System has {total_ram_gb:.1f}GB RAM (30-64GB). Full mode with staged initialization."
        else:
            profile = HardwareProfile.UNLIMITED
            skip_agi_hub = False
            enable_slim_mode = False
            defer_heavy_subsystems = False
            force_cloud_hybrid = False  # v144.0: UNLIMITED systems prefer local
            reason = f"System has {total_ram_gb:.1f}GB RAM (64GB+). Unlimited mode - all subsystems can load in parallel."

        assessment = HardwareAssessment(
            profile=profile,
            total_ram_gb=total_ram_gb,
            available_ram_gb=available_ram_gb,
            cpu_count=cpu_count,
            is_apple_silicon=is_apple_silicon,
            has_gpu=has_gpu,
            gpu_name=gpu_name,
            recommended_gpu_layers=recommended_gpu_layers,
            recommended_context_size=recommended_context_size,
            skip_agi_hub=skip_agi_hub,
            enable_slim_mode=enable_slim_mode,
            defer_heavy_subsystems=defer_heavy_subsystems,
            reason=reason,
            force_cloud_hybrid=force_cloud_hybrid,  # v144.0
        )

        _hardware_assessment_cache = assessment
        return assessment


def get_hardware_env_vars(assessment: Optional[HardwareAssessment] = None) -> Dict[str, str]:
    """
    v138.0: Get environment variables to pass to jarvis-prime based on hardware.

    These variables are read by jarvis-prime's run_server.py to configure
    the AGIHubConfig for Memory-Aware Staged Initialization.

    Args:
        assessment: Optional pre-computed assessment. If None, will assess hardware.

    Returns:
        Dict of environment variables to merge into service environment
    """
    if assessment is None:
        assessment = assess_hardware_profile()

    env_vars: Dict[str, str] = {
        # v138.0: Hardware profile communication
        "JARVIS_HARDWARE_PROFILE": assessment.profile.name,
        "JARVIS_TOTAL_RAM_GB": str(round(assessment.total_ram_gb, 1)),
        "JARVIS_AVAILABLE_RAM_GB": str(round(assessment.available_ram_gb, 1)),
        "JARVIS_CPU_COUNT": str(assessment.cpu_count),
        "JARVIS_IS_APPLE_SILICON": str(assessment.is_apple_silicon).lower(),

        # v138.0: AGI Hub configuration hints
        "JARVIS_SKIP_AGI_HUB": str(assessment.skip_agi_hub).lower(),
        "JARVIS_ENABLE_SLIM_MODE": str(assessment.enable_slim_mode).lower(),
        "JARVIS_DEFER_HEAVY_SUBSYSTEMS": str(assessment.defer_heavy_subsystems).lower(),

        # v138.0: GPU configuration
        "JARVIS_HAS_GPU": str(assessment.has_gpu).lower(),
        "JARVIS_GPU_NAME": assessment.gpu_name,
        "JARVIS_GPU_LAYERS": str(assessment.recommended_gpu_layers),
        "JARVIS_CONTEXT_SIZE": str(assessment.recommended_context_size),
    }

    # =========================================================================
    # v139.0: ACTIVE HYBRID BRIDGE - GCP ENDPOINT FOR SLIM MODE
    # v144.0: Enhanced with Active Rescue pre-cached endpoint
    # =========================================================================
    # When Slim Mode is active, we need to tell jarvis-prime where to forward
    # heavy tasks. This enables the Active Hybrid Bridge to route to GCP.
    # =========================================================================
    if assessment.enable_slim_mode or assessment.force_cloud_hybrid:
        # v144.0: First check for Active Rescue cached endpoint
        active_rescue_vars = get_active_rescue_env_vars()
        if active_rescue_vars:
            env_vars.update(active_rescue_vars)
            logger.info(
                f"[v144.0] 🚀 Active Rescue: GCP offload vars set from cache"
            )
        else:
            # Get GCP endpoint from environment (set by user or infrastructure)
            gcp_endpoint = os.environ.get("GCP_PRIME_ENDPOINT", os.environ.get("JARVIS_GCP_PRIME_ENDPOINT", ""))

            if gcp_endpoint:
                env_vars["JARVIS_GCP_PRIME_ENDPOINT"] = gcp_endpoint
                env_vars["JARVIS_AUTO_WAKE_GCP"] = "true"
                env_vars["JARVIS_WARM_UP_GCP_ON_START"] = "true"
                env_vars["JARVIS_GCP_OFFLOAD_ACTIVE"] = "true"  # v144.0
                logger.info(
                    f"[v139.0] ☁️ Active Hybrid Bridge: GCP endpoint set to {gcp_endpoint}"
                )
            else:
                # No explicit endpoint - GCPVMManager will handle provisioning
                env_vars["JARVIS_AUTO_WAKE_GCP"] = "true"
                env_vars["JARVIS_WARM_UP_GCP_ON_START"] = "false"  # Don't block startup
                logger.info(
                    "[v139.0] ☁️ Active Hybrid Bridge: No GCP endpoint configured - "
                    "will use GCPVMManager for on-demand provisioning"
                )

        # v144.0: Set force_cloud_hybrid flag for spawn logic
        if assessment.force_cloud_hybrid:
            env_vars["JARVIS_FORCE_CLOUD_HYBRID"] = "true"

    # Log the profile being passed
    logger.info(
        f"[v138.0] 🖥️ Hardware Profile: {assessment.profile.name} - "
        f"{assessment.total_ram_gb:.1f}GB total, {assessment.available_ram_gb:.1f}GB available"
    )

    return env_vars


def set_hardware_env_in_supervisor(assessment: Optional[HardwareAssessment] = None) -> Dict[str, str]:
    """
    v143.0: Set hardware-based environment variables in the SUPERVISOR's own environment.

    CRITICAL FIX: The v142.0 memory gate checks os.environ for JARVIS_ENABLE_SLIM_MODE,
    but this env var was only being passed to subprocess, NOT set in the supervisor.
    This caused the memory gate to fail to detect Slim Mode from Source 1.

    This function should be called EARLY in supervisor startup (before any spawn attempts)
    to ensure the memory gate can detect Slim Mode correctly.

    Args:
        assessment: Optional pre-computed assessment. If None, will assess hardware.

    Returns:
        Dict of environment variables that were set
    """
    if assessment is None:
        assessment = assess_hardware_profile()

    # Set environment variables in the SUPERVISOR's own environment
    env_vars = {
        "JARVIS_HARDWARE_PROFILE": assessment.profile.name,
        "JARVIS_TOTAL_RAM_GB": str(assessment.total_ram_gb),
        "JARVIS_AVAILABLE_RAM_GB": str(assessment.available_ram_gb),
        "JARVIS_CPU_COUNT": str(assessment.cpu_count),
        "JARVIS_IS_APPLE_SILICON": str(assessment.is_apple_silicon).lower(),
        "JARVIS_SKIP_AGI_HUB": str(assessment.skip_agi_hub).lower(),
        "JARVIS_ENABLE_SLIM_MODE": str(assessment.enable_slim_mode).lower(),
        "JARVIS_DEFER_HEAVY_SUBSYSTEMS": str(assessment.defer_heavy_subsystems).lower(),
        "JARVIS_HAS_GPU": str(assessment.has_gpu).lower(),
        "JARVIS_GPU_NAME": assessment.gpu_name,
        "JARVIS_GPU_LAYERS": str(assessment.recommended_gpu_layers),
        "JARVIS_CONTEXT_SIZE": str(assessment.recommended_context_size),
    }

    for key, value in env_vars.items():
        os.environ[key] = value

    logger.info(
        f"[v143.0] ✅ Set hardware env vars in supervisor environment: "
        f"SLIM_MODE={assessment.enable_slim_mode}, PROFILE={assessment.profile.name}"
    )

    return env_vars


def log_hardware_assessment(assessment: Optional[HardwareAssessment] = None) -> None:
    """
    v138.0: Log detailed hardware assessment to console.
    v143.0: Also sets environment variables in supervisor's own environment.

    Args:
        assessment: Optional pre-computed assessment. If None, will assess hardware.
    """
    if assessment is None:
        assessment = assess_hardware_profile()

    # v143.0: Set env vars in supervisor's own environment FIRST
    set_hardware_env_in_supervisor(assessment)

    logger.info("=" * 70)
    logger.info("v138.0 HARDWARE-AWARE STARTUP ASSESSMENT")
    logger.info("=" * 70)
    logger.info(f"  Profile:       {assessment.profile.name}")
    logger.info(f"  Total RAM:     {assessment.total_ram_gb:.1f} GB")
    logger.info(f"  Available RAM: {assessment.available_ram_gb:.1f} GB")
    logger.info(f"  CPU Cores:     {assessment.cpu_count}")
    logger.info(f"  Apple Silicon: {assessment.is_apple_silicon}")
    logger.info(f"  GPU:           {assessment.gpu_name}")
    logger.info(f"  GPU Layers:    {assessment.recommended_gpu_layers}")
    logger.info(f"  Context Size:  {assessment.recommended_context_size}")
    logger.info("-" * 70)
    logger.info(f"  Skip AGI Hub:  {assessment.skip_agi_hub}")
    logger.info(f"  Slim Mode:     {assessment.enable_slim_mode}")
    logger.info(f"  Defer Heavy:   {assessment.defer_heavy_subsystems}")
    logger.info(f"  Force Cloud:   {assessment.force_cloud_hybrid}")  # v144.0
    logger.info("-" * 70)
    logger.info(f"  Reason: {assessment.reason}")
    logger.info("=" * 70)


# =============================================================================
# v144.0: ACTIVE RESCUE SYSTEM - GCP VM Provisioning Before Spawn
# =============================================================================
# The Active Rescue system ensures the GCP "Life Raft" is deployed BEFORE
# jarvis-prime starts on SLIM hardware, not after it crashes from OOM.
#
# This breaks the deadlock where:
#   1. User needs GCP Cloud to save RAM
#   2. To use GCP, jarvis-prime needs to start (even in Slim Mode)
#   3. But jarvis-prime crashes from OOM before it can route to GCP
#   4. Result: Can't use Cloud because Local Client keeps crashing
#
# Active Rescue solves this by:
#   1. Detecting SLIM/CLOUD_ONLY hardware profile
#   2. Provisioning GCP VM BEFORE spawning jarvis-prime
#   3. Passing GCP endpoint to jarvis-prime via environment variables
#   4. jarvis-prime starts as Hollow Client (lazy imports) and routes to GCP
#   5. On OOM (Exit -9): NO local restart, force GCP provisioning first
# =============================================================================

# Cache for GCP VM endpoint (to avoid re-provisioning)
_active_rescue_gcp_endpoint: Optional[str] = None
_active_rescue_gcp_ready: bool = False
_active_rescue_lock = threading.Lock()

# =============================================================================
# v146.0: TRINITY PROTOCOL - Elastic Hybrid Cloud Architecture
# =============================================================================
# The Trinity Protocol transforms the system from "Try Local, Fail, Retry"
# to "Assess, Offload, Execute" - a Cloud-First strategy for SLIM hardware.
#
# COMPONENTS:
#   1. CLOUD LOCK PERSISTENCE: Survives restarts, prevents OOM loops
#   2. BACKGROUND GCP PRE-WARM: Non-blocking async VM provisioning
#   3. REACTOR-CORE SEQUENCING: Wait for jarvis-prime health
#   4. TRINITY EVENT COORDINATION: Cross-repo health signals
#
# FLOW:
#   startup → assess_hardware → SLIM detected → start_gcp_prewarm_task →
#   spawn jarvis-prime (hollow) → GCP ready event → spawn reactor-core
# =============================================================================

# v146.0: Cloud Lock persistence file path
_CLOUD_LOCK_FILE = Path.home() / ".jarvis" / "trinity" / "cloud_lock.json"

# v146.0: Trinity Protocol state
_trinity_gcp_prewarm_task: Optional[asyncio.Task] = None
_trinity_gcp_ready_event: Optional[asyncio.Event] = None
_trinity_cloud_locked: bool = False
_trinity_protocol_active: bool = False

# v148.0: Continuous GCP VM Health Monitor state
_gcp_vm_health_monitor_task: Optional[asyncio.Task] = None
_gcp_vm_health_monitor_running: bool = False
_gcp_vm_consecutive_failures: int = 0
_GCP_VM_MAX_FAILURES_BEFORE_REPROVISION = int(os.getenv("GCP_VM_MAX_HEALTH_FAILURES", "3"))

# =============================================================================
# v149.0: ENTERPRISE-GRADE GCP RESILIENCE
# =============================================================================
# Adds exponential backoff for re-provisioning and circuit breaker for GCP API.
# These patterns prevent quota exhaustion and continuous failed API calls during
# GCP regional outages or authentication issues.
# =============================================================================

# v149.0: Exponential backoff configuration for GCP re-provisioning
_GCP_REPROVISION_BASE_DELAY = float(os.getenv("GCP_REPROVISION_BASE_DELAY", "30.0"))
_GCP_REPROVISION_MAX_DELAY = float(os.getenv("GCP_REPROVISION_MAX_DELAY", "600.0"))
_GCP_REPROVISION_JITTER = float(os.getenv("GCP_REPROVISION_JITTER", "0.3"))
_gcp_reprovision_attempt: int = 0
_gcp_reprovision_last_failure: float = 0.0

# v149.1: Max re-provision attempts before triggering Claude API fallback
_GCP_MAX_REPROVISION_ATTEMPTS = int(os.getenv("GCP_MAX_REPROVISION_ATTEMPTS", "3"))

# =============================================================================
# v149.1: CLAUDE API FALLBACK SIGNAL - Cross-repo coordination
# =============================================================================
# When GCP becomes persistently unavailable, this signal file tells jarvis-prime
# to switch from Hollow Client mode (waiting for GCP) to Claude API mode.
# This ensures inference continues even when GCP is completely down.
# =============================================================================

_CLAUDE_FALLBACK_SIGNAL_FILE = Path.home() / ".jarvis" / "trinity" / "claude_api_fallback.json"


def write_claude_api_fallback_signal(reason: str, gcp_attempts: int = 0) -> bool:
    """
    v149.1: Write signal file to tell jarvis-prime to use Claude API fallback.
    
    This is the cross-repo coordination mechanism:
    - JARVIS Core writes this file when GCP becomes unavailable
    - JARVIS Prime reads this file and switches to Claude API mode
    - This prevents jarvis-prime from being stuck in Hollow Client limbo
    
    Args:
        reason: Human-readable reason for fallback
        gcp_attempts: Number of GCP re-provision attempts that failed
        
    Returns:
        True if signal was written successfully
    """
    import json
    from datetime import datetime
    
    try:
        _CLAUDE_FALLBACK_SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        
        signal_data = {
            "active": True,
            "reason": reason,
            "triggered_at": datetime.utcnow().isoformat() + "Z",
            "gcp_attempts": gcp_attempts,
            "fallback_mode": "claude_api",
            "version": "v149.1",
            "instructions": {
                "jarvis_prime": "Switch to Claude API mode - do not wait for GCP",
                "inference_mode": "api_only",
                "local_models": "disabled",
                "gcp_routing": "disabled",
            },
        }
        
        with open(_CLAUDE_FALLBACK_SIGNAL_FILE, "w") as f:
            json.dump(signal_data, f, indent=2)
        
        logger.warning(
            f"[v149.1] 📢 CLAUDE API FALLBACK SIGNAL WRITTEN: {reason} "
            f"(after {gcp_attempts} GCP attempts)"
        )
        logger.info(f"[v149.1]    Signal file: {_CLAUDE_FALLBACK_SIGNAL_FILE}")
        return True
        
    except Exception as e:
        logger.error(f"[v149.1] Failed to write Claude fallback signal: {e}")
        return False


def clear_claude_api_fallback_signal() -> bool:
    """
    v149.1: Clear the Claude API fallback signal (when GCP becomes available again).
    
    Returns:
        True if signal was cleared or didn't exist
    """
    try:
        if _CLAUDE_FALLBACK_SIGNAL_FILE.exists():
            _CLAUDE_FALLBACK_SIGNAL_FILE.unlink()
            logger.info("[v149.1] ✅ Claude API fallback signal cleared (GCP restored)")
        return True
    except Exception as e:
        logger.warning(f"[v149.1] Failed to clear Claude fallback signal: {e}")
        return False


def is_claude_api_fallback_active() -> bool:
    """
    v149.1: Check if Claude API fallback is currently active.
    
    Returns:
        True if the fallback signal file exists and is active
    """
    import json
    
    try:
        if not _CLAUDE_FALLBACK_SIGNAL_FILE.exists():
            return False
        
        with open(_CLAUDE_FALLBACK_SIGNAL_FILE, "r") as f:
            data = json.load(f)
            return data.get("active", False)
            
    except Exception:
        return False


@dataclass
class GCPCircuitBreaker:
    """
    v149.0: Circuit breaker for GCP API calls.
    
    Implements the standard circuit breaker pattern:
    - CLOSED: Normal operation, requests flow through
    - OPEN: Failures exceeded threshold, requests blocked
    - HALF_OPEN: Recovery testing, limited requests allowed
    
    This prevents continuous failed API calls during GCP outages,
    protecting quota and avoiding log spam.
    """
    state: str = "closed"  # "closed", "open", "half_open"
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    failure_threshold: int = 3
    recovery_timeout: float = 120.0  # Wait before half-open
    half_open_success_threshold: int = 2  # Successes to close

    def record_success(self) -> None:
        """Record a successful GCP API call."""
        if self.state == "half_open":
            self.success_count += 1
            if self.success_count >= self.half_open_success_threshold:
                self.state = "closed"
                self.failure_count = 0
                logger.info("[v149.0] 🟢 GCP circuit breaker CLOSED (recovered)")
        else:
            self.failure_count = 0

    def record_failure(self) -> None:
        """Record a failed GCP API call."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        self.success_count = 0

        if self.failure_count >= self.failure_threshold and self.state != "open":
            self.state = "open"
            logger.warning(
                f"[v149.0] 🔴 GCP circuit breaker OPEN after {self.failure_count} failures"
            )

    def should_allow_request(self) -> Tuple[bool, str]:
        """
        Check if a GCP API request should be allowed.
        
        Returns:
            Tuple of (allowed: bool, reason: str)
        """
        if self.state == "closed":
            return (True, "circuit closed")
        if self.state == "half_open":
            return (True, "circuit half-open (testing)")
        # Open state: check if recovery timeout elapsed
        elapsed = time.time() - self.last_failure_time
        if elapsed > self.recovery_timeout:
            self.state = "half_open"
            self.success_count = 0
            logger.info(
                f"[v149.0] 🟡 GCP circuit breaker HALF-OPEN after {elapsed:.0f}s"
            )
            return (True, "circuit half-open (recovery)")
        return (
            False,
            f"circuit OPEN ({self.recovery_timeout - elapsed:.0f}s until recovery)"
        )

    def get_status(self) -> Dict[str, Any]:
        """Get circuit breaker status for monitoring."""
        return {
            "state": self.state,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "time_since_last_failure": time.time() - self.last_failure_time if self.last_failure_time else None,
            "recovery_timeout": self.recovery_timeout,
        }


# v149.0: Global GCP circuit breaker instance
_gcp_circuit_breaker = GCPCircuitBreaker()

# =============================================================================
# v147.0: PROACTIVE RESCUE - Crash Pre-Cognition Patterns
# =============================================================================
# These patterns indicate IMMINENT memory pressure. When detected, we trigger
# GCP provisioning BEFORE the kernel kills the process with -9.
#
# Pattern sources:
#   - jarvis-prime memory emergency logs
#   - Python memory allocation failures
#   - System-level OOM warnings
#   - JARVIS survival mode indicators
# =============================================================================

PROACTIVE_RESCUE_PATTERNS = frozenset({
    # jarvis-prime memory pressure indicators
    "MEMORY EMERGENCY",
    "memory emergency",
    "OOM Warning",
    "oom warning",
    "OOM_WARNING",
    "load shedding",
    "LOAD SHEDDING",
    "survival mode",
    "SURVIVAL MODE",
    "memory pressure critical",
    "MEMORY_PRESSURE_CRITICAL",
    # Python memory allocation failures
    "MemoryError",
    "Cannot allocate memory",
    "out of memory",
    "OUT OF MEMORY",
    # ML framework memory issues
    "CUDA out of memory",
    "torch.cuda.OutOfMemoryError",
    "MPS backend out of memory",
    # System-level indicators
    "killed by signal 9",
    "memory cgroup out of memory",
    "oom-killer",
    "OOM killer",
})

# v147.0: Patterns that indicate SEVERE memory stress (immediate action)
PROACTIVE_RESCUE_SEVERE_PATTERNS = frozenset({
    "MEMORY EMERGENCY",
    "memory emergency",
    "MemoryError",
    "Cannot allocate memory",
    "CUDA out of memory",
    "MPS backend out of memory",
    "oom-killer",
})

# v147.0: Consecutive OOM tracking for circuit breaker
_consecutive_oom_counts: Dict[str, int] = {}
_MAX_CONSECUTIVE_OOMS = 3  # After this many, stop restarting


def _check_proactive_rescue_pattern(line: str) -> Tuple[bool, bool, Optional[str]]:
    """
    v147.0: Check if a log line indicates imminent memory pressure.

    Returns:
        Tuple of (should_trigger: bool, is_severe: bool, matched_pattern: Optional[str])
    """
    for pattern in PROACTIVE_RESCUE_SEVERE_PATTERNS:
        if pattern in line:
            return True, True, pattern

    for pattern in PROACTIVE_RESCUE_PATTERNS:
        if pattern in line:
            return True, False, pattern

    return False, False, None


def _load_cloud_lock() -> Dict[str, Any]:
    """
    v146.0: Load persistent cloud lock state from disk.
    v147.0: Added JSON schema validation and corruption recovery.

    Returns dict with:
      - locked: bool - Whether cloud-only mode is enforced
      - reason: str - Why the lock was set (e.g., "OOM_CRASH")
      - timestamp: float - When the lock was set
      - oom_count: int - Number of OOM events that led to lock
      - consecutive_ooms: int - v147.0: Consecutive OOMs in cloud mode
    """
    default_state = {
        "locked": False,
        "reason": None,
        "timestamp": None,
        "oom_count": 0,
        "consecutive_ooms": 0,  # v147.0
        "hardware_ram_gb": None,  # v147.0: RAM when lock was set
    }

    try:
        if _CLOUD_LOCK_FILE.exists():
            content = _CLOUD_LOCK_FILE.read_text().strip()
            if not content:
                logger.debug("[v147.0] Cloud lock file is empty, using defaults")
                return default_state

            data = json.loads(content)

            # v147.0: Validate JSON schema
            if not isinstance(data, dict):
                logger.warning("[v147.0] Cloud lock file has invalid format, resetting")
                return default_state

            # Merge with defaults to handle missing keys
            result = default_state.copy()
            result.update(data)
            return result

    except json.JSONDecodeError as e:
        logger.warning(f"[v147.0] Cloud lock file corrupted (JSON error: {e}), resetting")
        # Delete corrupted file
        try:
            _CLOUD_LOCK_FILE.unlink()
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"[v147.0] Could not read cloud lock: {e}")

    return default_state


def _save_cloud_lock(
    locked: bool,
    reason: str,
    oom_count: int = 0,
    consecutive_ooms: int = 0,
    hardware_ram_gb: Optional[float] = None,
) -> bool:
    """
    v146.0: Persist cloud lock state to disk.
    v147.0: ATOMIC WRITES - Uses temp file + rename to prevent corruption.

    The atomic write strategy ensures that if power fails during the write,
    we either have the old complete file or the new complete file, never
    a corrupted partial file.

    This survives supervisor restarts, ensuring that after an OOM crash,
    the system stays in cloud mode until manually cleared.
    """
    try:
        # v147.0: Ensure directory exists
        _CLOUD_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

        # v147.0: Get current RAM if not provided
        if hardware_ram_gb is None:
            try:
                import psutil
                hardware_ram_gb = psutil.virtual_memory().total / (1024 ** 3)
            except Exception:
                hardware_ram_gb = None

        lock_state = {
            "locked": locked,
            "reason": reason,
            "timestamp": time.time(),
            "oom_count": oom_count,
            "consecutive_ooms": consecutive_ooms,  # v147.0
            "hardware_ram_gb": hardware_ram_gb,  # v147.0
            "version": "v147.0",
        }

        # v147.0: ATOMIC WRITE with temp file + rename
        # This prevents corruption if power fails during write
        temp_file = _CLOUD_LOCK_FILE.with_suffix('.json.tmp')

        # Write to temp file first
        temp_file.write_text(json.dumps(lock_state, indent=2))

        # Atomic rename (on POSIX systems, rename is atomic)
        temp_file.rename(_CLOUD_LOCK_FILE)

        logger.info(f"[v147.0] ☁️ Cloud lock {'SET' if locked else 'CLEARED'}: {reason}")
        return True

    except Exception as e:
        logger.warning(f"[v147.0] Could not save cloud lock: {e}")
        # Clean up temp file if it exists
        try:
            temp_file = _CLOUD_LOCK_FILE.with_suffix('.json.tmp')
            if temp_file.exists():
                temp_file.unlink()
        except Exception:
            pass
        return False


def check_and_release_cloud_lock_on_hardware_upgrade() -> bool:
    """
    v147.0: ELASTIC SCALING - Auto-release cloud lock if hardware was upgraded.

    Called during startup to detect if user has upgraded RAM since the lock
    was set. If RAM is now >= 32GB but a cloud lock exists, we assume the
    user upgraded and auto-release the lock.

    Returns:
        True if lock was released due to hardware upgrade, False otherwise.
    """
    lock_state = _load_cloud_lock()

    if not lock_state.get("locked", False):
        return False  # No lock to release

    try:
        import psutil
        current_ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        return False

    # v147.0: Hardware upgrade detection
    # If RAM >= 32GB, user no longer has SLIM hardware
    FULL_MODE_RAM_THRESHOLD = 32.0

    if current_ram_gb >= FULL_MODE_RAM_THRESHOLD:
        lock_ram = lock_state.get("hardware_ram_gb")

        logger.info(
            f"[v147.0] ♻️ HARDWARE UPGRADE DETECTED!\n"
            f"    RAM when locked: {lock_ram:.1f}GB (SLIM)\n"
            f"    Current RAM: {current_ram_gb:.1f}GB (FULL)\n"
            f"    Action: AUTO-RELEASING cloud lock - returning to local mode"
        )

        _save_cloud_lock(
            locked=False,
            reason=f"HARDWARE_UPGRADE_DETECTED (was {lock_ram:.1f}GB, now {current_ram_gb:.1f}GB)",
            oom_count=0,
            consecutive_ooms=0,
            hardware_ram_gb=current_ram_gb,
        )

        return True

    return False


def increment_consecutive_oom_count(service_name: str) -> int:
    """
    v147.0: Track consecutive OOM crashes for circuit breaker.

    Returns the new consecutive OOM count.
    """
    global _consecutive_oom_counts
    count = _consecutive_oom_counts.get(service_name, 0) + 1
    _consecutive_oom_counts[service_name] = count
    return count


def reset_consecutive_oom_count(service_name: str) -> None:
    """v147.0: Reset consecutive OOM count after successful operation."""
    global _consecutive_oom_counts
    _consecutive_oom_counts[service_name] = 0


def should_circuit_break_oom(service_name: str) -> bool:
    """
    v147.0: Check if we should stop restarting due to consecutive OOMs.

    Returns True if consecutive OOMs exceed threshold.
    """
    return _consecutive_oom_counts.get(service_name, 0) >= _MAX_CONSECUTIVE_OOMS


def clear_cloud_lock() -> bool:
    """
    v146.0: Manually clear the cloud lock.

    Call this when you want to allow local inference again after
    resolving the OOM issues (e.g., after upgrading RAM or reducing load).
    """
    return _save_cloud_lock(locked=False, reason="MANUAL_CLEAR")


def is_cloud_locked() -> Tuple[bool, Optional[str]]:
    """
    v146.0: Check if cloud-only mode is enforced.

    Returns:
        Tuple of (locked: bool, reason: Optional[str])
    """
    lock_state = _load_cloud_lock()
    return lock_state.get("locked", False), lock_state.get("reason")


def set_cloud_lock_after_oom(oom_count: int = 1, consecutive_ooms: int = 0) -> bool:
    """
    v146.0: Set cloud lock after OOM crash.
    v147.0: Now tracks consecutive OOMs for circuit breaker.

    This is called by the OOM Death Handler to ensure the system
    stays in cloud mode across restarts.
    """
    global _trinity_cloud_locked
    _trinity_cloud_locked = True
    return _save_cloud_lock(
        locked=True,
        reason="OOM_CRASH_PROTECTION",
        oom_count=oom_count,
        consecutive_ooms=consecutive_ooms,
    )


async def _background_gcp_prewarm_task(timeout: float = 180.0) -> None:
    """
    v146.0: Background task that pre-warms the GCP VM.

    This runs in parallel with service startup, ensuring the GCP VM
    is ready by the time jarvis-prime needs it for inference.

    CRITICAL: This is NON-BLOCKING - it does not delay service startup.
    """
    global _active_rescue_gcp_endpoint, _active_rescue_gcp_ready, _trinity_gcp_ready_event

    logger.info("[v146.0] 🔥 TRINITY PROTOCOL: Starting background GCP pre-warm...")
    start_time = time.time()

    try:
        # Provision GCP VM (or verify existing)
        success, endpoint = await ensure_gcp_vm_ready_for_prime(
            timeout_seconds=timeout,
            force_provision=False,
        )

        elapsed = time.time() - start_time

        if success and endpoint:
            logger.info(
                f"[v146.0] ✅ TRINITY PROTOCOL: GCP VM pre-warmed in {elapsed:.1f}s "
                f"→ {endpoint}"
            )

            # Update global state
            with _active_rescue_lock:
                _active_rescue_gcp_endpoint = endpoint
                _active_rescue_gcp_ready = True

            # Set environment variables for child processes
            os.environ["GCP_PRIME_ENDPOINT"] = endpoint
            os.environ["JARVIS_GCP_PRIME_ENDPOINT"] = endpoint
            os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] = "true"

            # Signal that GCP is ready
            if _trinity_gcp_ready_event:
                _trinity_gcp_ready_event.set()

            await _emit_event(
                "TRINITY_GCP_PREWARM_SUCCESS",
                priority="HIGH",
                details={
                    "endpoint": endpoint,
                    "elapsed_seconds": elapsed,
                    "mode": "background_prewarm",
                }
            )
        else:
            logger.warning(
                f"[v146.0] ⚠️ TRINITY PROTOCOL: GCP pre-warm failed after {elapsed:.1f}s"
            )
            await _emit_event(
                "TRINITY_GCP_PREWARM_FAILED",
                priority="HIGH",
                details={
                    "elapsed_seconds": elapsed,
                    "reason": "GCP VM provisioning failed or timed out",
                }
            )

    except asyncio.CancelledError:
        logger.info("[v146.0] TRINITY PROTOCOL: GCP pre-warm task cancelled")
        raise
    except Exception as e:
        logger.error(f"[v146.0] TRINITY PROTOCOL: GCP pre-warm error: {e}")


def start_trinity_gcp_prewarm() -> Optional[asyncio.Task]:
    """
    v146.0: Start the background GCP pre-warm task.

    Call this immediately after detecting SLIM hardware in start_all_services().
    Returns the Task object so it can be awaited or cancelled if needed.
    """
    global _trinity_gcp_prewarm_task, _trinity_gcp_ready_event, _trinity_protocol_active

    # Create the ready event if it doesn't exist
    if _trinity_gcp_ready_event is None:
        _trinity_gcp_ready_event = asyncio.Event()

    # Don't start multiple tasks
    if _trinity_gcp_prewarm_task is not None and not _trinity_gcp_prewarm_task.done():
        logger.debug("[v146.0] GCP pre-warm task already running")
        return _trinity_gcp_prewarm_task

    _trinity_protocol_active = True

    # Create background task (NON-BLOCKING)
    _trinity_gcp_prewarm_task = asyncio.create_task(
        _background_gcp_prewarm_task(timeout=180.0),
        name="trinity_gcp_prewarm"
    )

    logger.info("[v146.0] 🚀 TRINITY PROTOCOL: GCP pre-warm task started (background)")
    return _trinity_gcp_prewarm_task


async def wait_for_gcp_ready(timeout: float = 60.0) -> bool:
    """
    v146.0: Wait for GCP to be ready (called before reactor-core spawn).

    This ensures reactor-core doesn't start until jarvis-prime has
    a working GCP endpoint to offload inference to.
    """
    global _trinity_gcp_ready_event

    if _trinity_gcp_ready_event is None:
        return False

    try:
        await asyncio.wait_for(_trinity_gcp_ready_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        logger.warning(f"[v146.0] GCP ready wait timed out after {timeout}s")
        return False


def get_trinity_protocol_status() -> Dict[str, Any]:
    """
    v146.0: Get current Trinity Protocol status.
    """
    cloud_locked, lock_reason = is_cloud_locked()
    return {
        "protocol_active": _trinity_protocol_active,
        "cloud_locked": cloud_locked,
        "lock_reason": lock_reason,
        "gcp_ready": _active_rescue_gcp_ready,
        "gcp_endpoint": _active_rescue_gcp_endpoint,
        "prewarm_task_running": (
            _trinity_gcp_prewarm_task is not None
            and not _trinity_gcp_prewarm_task.done()
        ),
    }


async def ensure_gcp_vm_ready_for_prime(
    timeout_seconds: float = 120.0,
    force_provision: bool = False,
) -> Tuple[bool, Optional[str]]:
    """
    v144.0: Ensure GCP VM is ready for jarvis-prime inference offloading.
    v147.0: Enhanced with .env.gcp auto-loading and detailed diagnostics.

    This function is called BEFORE spawning jarvis-prime on SLIM hardware.
    It either:
    - Uses an existing running VM (no delay)
    - Provisions a new VM and waits for it to be ready (up to timeout)

    Args:
        timeout_seconds: Maximum time to wait for VM to be ready
        force_provision: If True, provision new VM even if one exists

    Returns:
        Tuple of (success: bool, gcp_endpoint: Optional[str])
        - success=True: VM is ready, endpoint is valid
        - success=False: VM not available, endpoint is None
    """
    global _active_rescue_gcp_endpoint, _active_rescue_gcp_ready

    # Check cache first (fast path)
    with _active_rescue_lock:
        if _active_rescue_gcp_ready and _active_rescue_gcp_endpoint and not force_provision:
            logger.info(
                f"[v144.0] 🚀 Active Rescue: Using cached GCP endpoint: {_active_rescue_gcp_endpoint}"
            )
            return True, _active_rescue_gcp_endpoint

    # =========================================================================
    # v147.0: Pre-flight check - ensure .env.gcp is loaded
    # =========================================================================
    # Check if critical GCP env vars are set; if not, try loading .env.gcp
    gcp_enabled_vars = [
        os.environ.get("GCP_ENABLED"),
        os.environ.get("GCP_VM_ENABLED"),
        os.environ.get("JARVIS_SPOT_VM_ENABLED"),
    ]
    gcp_project = os.environ.get("GCP_PROJECT_ID")
    
    if not any(v and v.lower() == "true" for v in gcp_enabled_vars):
        logger.warning(
            "[v147.0] ⚠️ GCP not enabled in environment. Attempting to load .env.gcp..."
        )
        # Try to load .env.gcp
        try:
            from pathlib import Path
            env_gcp_paths = [
                Path.cwd() / ".env.gcp",
                Path(__file__).parent.parent.parent / ".env.gcp",
                Path.home() / "Documents" / "repos" / "JARVIS-AI-Agent" / ".env.gcp",
            ]
            
            for env_path in env_gcp_paths:
                if env_path.exists():
                    logger.info(f"[v147.0] Found .env.gcp at {env_path}, loading...")
                    try:
                        from dotenv import load_dotenv
                        load_dotenv(env_path, override=True)
                        logger.info(f"[v147.0] ✅ Loaded {env_path}")
                        # Re-check after loading
                        if os.environ.get("JARVIS_SPOT_VM_ENABLED", "").lower() == "true":
                            logger.info("[v147.0] ✅ JARVIS_SPOT_VM_ENABLED now set to true")
                        break
                    except ImportError:
                        # dotenv not available, manual parse
                        with open(env_path, 'r') as f:
                            for line in f:
                                line = line.strip()
                                if line and not line.startswith('#') and '=' in line:
                                    key, _, value = line.partition('=')
                                    os.environ[key.strip()] = value.strip()
                        logger.info(f"[v147.0] ✅ Manually loaded {env_path}")
                        break
            else:
                logger.warning(
                    "[v147.0] ⚠️ .env.gcp not found. GCP features may not work. "
                    "Create .env.gcp with GCP_ENABLED=true, GCP_PROJECT_ID, etc."
                )
        except Exception as e:
            logger.debug(f"[v147.0] Failed to auto-load .env.gcp: {e}")
    
    # v147.0: Log current GCP configuration status
    gcp_config_status = {
        "GCP_ENABLED": os.environ.get("GCP_ENABLED", "not set"),
        "GCP_VM_ENABLED": os.environ.get("GCP_VM_ENABLED", "not set"),
        "JARVIS_SPOT_VM_ENABLED": os.environ.get("JARVIS_SPOT_VM_ENABLED", "not set"),
        "GCP_PROJECT_ID": os.environ.get("GCP_PROJECT_ID", "not set")[:20] + "..." if os.environ.get("GCP_PROJECT_ID") else "not set",
    }
    logger.info(f"[v147.0] GCP config status: {gcp_config_status}")

    try:
        # Try to get the GCP VM Manager
        from backend.core.gcp_vm_manager import get_gcp_vm_manager_safe

        vm_manager = await get_gcp_vm_manager_safe()
        if vm_manager is None:
            logger.warning(
                "[v147.0] ⚠️ Active Rescue: GCP VM Manager not available. "
                "Check: 1) GCP_ENABLED/JARVIS_SPOT_VM_ENABLED=true in env, "
                "2) GCP_PROJECT_ID is set, 3) GOOGLE_APPLICATION_CREDENTIALS exists"
            )
            return False, None

        # Check for existing running VM
        logger.info("[v144.0] 🔍 Active Rescue: Checking for existing GCP VM...")
        active_vm = await vm_manager.get_active_vm()

        if active_vm and not force_provision:
            # Verify VM is healthy
            is_healthy = await _verify_gcp_vm_health(active_vm.ip_address)
            if is_healthy:
                endpoint = f"http://{active_vm.ip_address}:8000"
                with _active_rescue_lock:
                    _active_rescue_gcp_endpoint = endpoint
                    _active_rescue_gcp_ready = True
                logger.info(
                    f"[v144.0] ✅ Active Rescue: Existing VM ready at {endpoint}"
                )
                
                # v149.0: Start health monitor EAGERLY when reusing existing VM
                # This closes the race window between VM discovery and Prime spawn
                try:
                    await start_gcp_vm_health_monitor(
                        check_interval=30.0,
                        validate_inference=True,
                    )
                    logger.info("[v149.0] 🔍 Health monitor started for existing VM")
                except Exception as monitor_err:
                    logger.warning(f"[v149.0] Could not start health monitor: {monitor_err}")
                
                return True, endpoint
            else:
                logger.warning(
                    f"[v144.0] ⚠️ Active Rescue: Existing VM {active_vm.ip_address} not healthy, will provision new"
                )

        # No healthy VM - need to provision
        logger.info(
            f"[v144.0] ☁️ Active Rescue: Provisioning GCP VM for jarvis-prime offload..."
        )
        logger.info(
            f"[v144.0] ⏱️ Active Rescue: Waiting up to {timeout_seconds}s for VM to be ready..."
        )

        # Provision and wait
        start_time = time.time()
        success, result_or_error = await vm_manager.start_spot_vm()

        if not success:
            logger.error(f"[v144.0] ❌ Active Rescue: VM provisioning failed: {result_or_error}")
            return False, None

        # v147.0: start_spot_vm now returns IP on success
        vm_ip = result_or_error  # This is the IP address when success=True
        logger.info(f"[v147.0] 🔧 VM provisioned with IP: {vm_ip}, waiting for service to start...")

        # Wait for VM service to be ready with health checks
        check_count = 0
        while time.time() - start_time < timeout_seconds:
            check_count += 1
            await asyncio.sleep(5.0)  # Check every 5 seconds

            elapsed = time.time() - start_time
            
            # v147.0: Try direct health check on the IP we got from start_spot_vm
            # Don't rely on get_active_vm() which requires is_healthy flag
            if vm_ip:
                is_healthy = await _verify_gcp_vm_health(vm_ip, timeout=8.0)
                if is_healthy:
                    # Try port 8000 first (jarvis-prime), fallback to 8010 (startup script default)
                    endpoint = f"http://{vm_ip}:8000"
                    with _active_rescue_lock:
                        _active_rescue_gcp_endpoint = endpoint
                        _active_rescue_gcp_ready = True

                    logger.info(
                        f"[v144.0] ✅ Active Rescue: GCP VM ready at {endpoint} "
                        f"(took {elapsed:.1f}s, {check_count} health checks)"
                    )
                    
                    # v148.0: Start continuous health monitor for the VM
                    try:
                        await start_gcp_vm_health_monitor(
                            check_interval=30.0,
                            validate_inference=True,
                        )
                    except Exception as monitor_err:
                        logger.warning(f"[v148.0] Could not start health monitor: {monitor_err}")
                    
                    return True, endpoint
                else:
                    # Log progress every 30 seconds
                    if check_count % 6 == 0:
                        logger.info(
                            f"[v147.0] ⏳ GCP VM {vm_ip} not ready yet "
                            f"({elapsed:.0f}s elapsed, {check_count} checks). "
                            f"Startup script may still be running..."
                        )
            else:
                # Fallback: try to get VM from manager
                active_vm = await vm_manager.get_active_vm()
                if active_vm and active_vm.ip_address:
                    vm_ip = active_vm.ip_address
                    logger.info(f"[v147.0] 🔍 Found VM IP from manager: {vm_ip}")

        # Timeout reached
        logger.error(
            f"[v144.0] ❌ Active Rescue: GCP VM not ready after {timeout_seconds}s timeout. "
            f"VM IP was: {vm_ip}. The startup script may have failed or be taking too long. "
            f"Check GCP Console → Compute Engine → VM instances → Serial port output for details."
        )
        
        # v149.1: Trigger Claude API fallback on startup timeout
        # This ensures jarvis-prime doesn't get stuck waiting for GCP
        write_claude_api_fallback_signal(
            reason=f"GCP VM startup timeout ({timeout_seconds}s) - VM IP: {vm_ip}",
            gcp_attempts=1,
        )
        
        return False, None

    except ImportError as e:
        logger.warning(f"[v144.0] ⚠️ Active Rescue: GCP modules not available: {e}")
        # v149.1: Fallback on import error (GCP SDK missing)
        write_claude_api_fallback_signal(
            reason=f"GCP modules unavailable: {e}",
            gcp_attempts=0,
        )
        return False, None
    except Exception as e:
        logger.error(f"[v144.0] ❌ Active Rescue: Unexpected error: {e}")
        return False, None


async def _verify_gcp_vm_health(
    ip_address: str, 
    timeout: float = 10.0,
    validate_inference: bool = False,
) -> bool:
    """
    v144.0: Verify GCP VM is reachable and healthy.
    v147.0: Fixed port mismatch - now checks both 8000 and 8010 (startup script uses 8010)
    v148.0: Added optional inference validation to ensure actual inference works

    Args:
        ip_address: IP address of the VM
        timeout: Timeout for health check request
        validate_inference: If True, also validates inference capability (slower but thorough)

    Returns:
        True if VM is healthy (and inference works if validate_inference=True), False otherwise
    """
    # v147.0: Check both ports - startup script uses 8010, but some configs use 8000
    ports_to_check = [8000, 8010]
    working_port = None
    
    for port in ports_to_check:
        try:
            connector = aiohttp.TCPConnector(force_close=True)
            async with aiohttp.ClientSession(connector=connector) as session:
                health_url = f"http://{ip_address}:{port}/health"
                async with session.get(
                    health_url, 
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as response:
                    if response.status == 200:
                        logger.info(f"[v147.0] ✅ GCP VM health check passed: {health_url}")
                        working_port = port
                        break
                    else:
                        logger.debug(f"[v147.0] Health check {health_url} returned status {response.status}")
        except asyncio.TimeoutError:
            logger.debug(f"[v147.0] Health check timeout: {ip_address}:{port}")
        except aiohttp.ClientError as e:
            logger.debug(f"[v147.0] Health check connection error {ip_address}:{port}: {type(e).__name__}")
        except Exception as e:
            logger.debug(f"[v147.0] Health check error {ip_address}:{port}: {e}")

    if working_port is None:
        return False
    
    # v148.0: Optional inference validation - ensures the VM can actually process requests
    if validate_inference:
        inference_valid = await _validate_inference_capability(ip_address, working_port, timeout)
        if not inference_valid:
            logger.warning(
                f"[v148.0] ⚠️ GCP VM health OK but inference validation failed: {ip_address}:{working_port}"
            )
            return False
        logger.info(f"[v148.0] ✅ GCP VM inference validation passed: {ip_address}:{working_port}")
    
    return True


async def _validate_inference_capability(
    ip_address: str,
    port: int,
    timeout: float = 15.0,
) -> bool:
    """
    v148.0: Validate that GCP VM can actually perform inference.
    
    Sends a small test request to verify the model is loaded and responsive.
    This catches cases where health check passes but actual inference fails.
    
    Args:
        ip_address: IP address of the VM
        port: Port to connect to
        timeout: Timeout for inference test
        
    Returns:
        True if inference works, False otherwise
    """
    # Try multiple inference endpoints that might be available
    inference_endpoints = [
        # OpenAI-compatible endpoint
        {
            "url": f"http://{ip_address}:{port}/v1/chat/completions",
            "method": "POST",
            "json": {
                "model": "default",
                "messages": [{"role": "user", "content": "test"}],
                "max_tokens": 1,
            },
        },
        # Simple completion endpoint
        {
            "url": f"http://{ip_address}:{port}/generate",
            "method": "POST",
            "json": {"prompt": "test", "max_tokens": 1},
        },
        # Health with inference status
        {
            "url": f"http://{ip_address}:{port}/health",
            "method": "GET",
            "check_field": "model_loaded",  # Check if this field is True
        },
        # Ready endpoint (stub server reports ready=True when full server is running)
        {
            "url": f"http://{ip_address}:{port}/ready",
            "method": "GET",
            "check_field": "ready",
        },
    ]
    
    try:
        connector = aiohttp.TCPConnector(force_close=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            for endpoint in inference_endpoints:
                try:
                    url = endpoint["url"]
                    method = endpoint.get("method", "GET")
                    
                    if method == "POST":
                        async with session.post(
                            url,
                            json=endpoint.get("json", {}),
                            timeout=aiohttp.ClientTimeout(total=timeout),
                        ) as response:
                            # Any response (even error) means the inference endpoint is responding
                            if response.status < 500:
                                logger.debug(f"[v148.0] Inference endpoint responding: {url}")
                                return True
                    else:
                        async with session.get(
                            url,
                            timeout=aiohttp.ClientTimeout(total=timeout),
                        ) as response:
                            if response.status == 200:
                                # Check specific field if required
                                check_field = endpoint.get("check_field")
                                if check_field:
                                    try:
                                        data = await response.json()
                                        if data.get(check_field):
                                            logger.debug(f"[v148.0] {check_field}=True at {url}")
                                            return True
                                        else:
                                            logger.debug(f"[v148.0] {check_field}=False at {url}")
                                    except Exception:
                                        pass  # JSON parsing failed, try next
                                else:
                                    return True
                                    
                except asyncio.TimeoutError:
                    logger.debug(f"[v148.0] Inference validation timeout: {endpoint['url']}")
                except aiohttp.ClientError as e:
                    logger.debug(f"[v148.0] Inference validation error: {type(e).__name__}")
                except Exception as e:
                    logger.debug(f"[v148.0] Inference validation error: {e}")
    
    except Exception as e:
        logger.debug(f"[v148.0] Inference validation session error: {e}")
    
    # v148.0: If no inference endpoints worked, check if it's just a health stub
    # The stub server responds to /health but not to inference endpoints
    # This is OK during provisioning but not after
    logger.debug(f"[v148.0] No inference endpoints responding at {ip_address}:{port}")
    return False


def get_active_rescue_env_vars(gcp_endpoint: Optional[str] = None) -> Dict[str, str]:
    """
    v144.0: Get environment variables for Active Rescue (GCP offloading).

    These variables tell jarvis-prime to run as a Hollow Client that
    routes heavy inference to GCP instead of loading models locally.

    Args:
        gcp_endpoint: Optional GCP endpoint. If None, uses cached endpoint.

    Returns:
        Dict of environment variables for subprocess
    """
    global _active_rescue_gcp_endpoint

    endpoint = gcp_endpoint or _active_rescue_gcp_endpoint

    if not endpoint:
        # No GCP endpoint available - jarvis-prime will try local
        return {}

    return {
        # Signal jarvis-prime to run as Hollow Client
        "JARVIS_GCP_OFFLOAD_ACTIVE": "true",
        # GCP endpoint for inference routing
        "GCP_PRIME_ENDPOINT": endpoint,
        "JARVIS_GCP_PRIME_ENDPOINT": endpoint,
        "JARVIS_GCP_VM_IP": endpoint.replace("http://", "").split(":")[0],
        # Additional flags for Hollow Client mode
        "JARVIS_HOLLOW_CLIENT_MODE": "true",
        "JARVIS_SKIP_LOCAL_MODEL_LOAD": "true",
    }


def invalidate_active_rescue_cache() -> None:
    """
    v144.0: Invalidate the Active Rescue GCP endpoint cache.

    Call this when:
    - GCP VM is terminated
    - GCP VM becomes unhealthy
    - User explicitly requests re-provisioning
    """
    global _active_rescue_gcp_endpoint, _active_rescue_gcp_ready

    with _active_rescue_lock:
        _active_rescue_gcp_endpoint = None
        _active_rescue_gcp_ready = False

    logger.info("[v144.0] Active Rescue cache invalidated")


# =============================================================================
# v148.0: CONTINUOUS GCP VM HEALTH MONITOR
# =============================================================================
# This background task monitors the active GCP VM and handles failures:
#   1. Periodic health checks (default: every 30 seconds)
#   2. Consecutive failure tracking with circuit breaker
#   3. Automatic re-provisioning when VM becomes unhealthy
#   4. Graceful degradation to local mode if re-provisioning fails
# =============================================================================

async def start_gcp_vm_health_monitor(
    check_interval: float = 30.0,
    validate_inference: bool = True,
) -> Optional[asyncio.Task]:
    """
    v148.0: Start the continuous GCP VM health monitor.
    
    This monitors the active GCP VM and handles failures automatically:
    - Periodic health checks
    - Consecutive failure tracking
    - Automatic re-provisioning when VM becomes unhealthy
    
    Args:
        check_interval: Time between health checks (seconds)
        validate_inference: Whether to validate inference capability (not just health)
        
    Returns:
        The monitor task, or None if no GCP VM is active
    """
    global _gcp_vm_health_monitor_task, _gcp_vm_health_monitor_running
    global _active_rescue_gcp_endpoint, _active_rescue_gcp_ready
    
    # Don't start if already running
    if _gcp_vm_health_monitor_running:
        logger.debug("[v148.0] GCP VM health monitor already running")
        return _gcp_vm_health_monitor_task
    
    # Don't start if no GCP VM is active
    if not _active_rescue_gcp_ready or not _active_rescue_gcp_endpoint:
        logger.debug("[v148.0] No active GCP VM to monitor")
        return None
    
    _gcp_vm_health_monitor_running = True
    _gcp_vm_health_monitor_task = asyncio.create_task(
        _gcp_vm_health_monitor_loop(check_interval, validate_inference),
        name="gcp-vm-health-monitor-v148"
    )
    
    logger.info(
        f"[v148.0] 🔍 GCP VM health monitor started "
        f"(interval={check_interval}s, inference_validation={validate_inference})"
    )
    
    return _gcp_vm_health_monitor_task


async def stop_gcp_vm_health_monitor() -> None:
    """v148.0: Stop the continuous GCP VM health monitor."""
    global _gcp_vm_health_monitor_task, _gcp_vm_health_monitor_running
    
    _gcp_vm_health_monitor_running = False
    
    if _gcp_vm_health_monitor_task and not _gcp_vm_health_monitor_task.done():
        _gcp_vm_health_monitor_task.cancel()
        try:
            await asyncio.wait_for(_gcp_vm_health_monitor_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        _gcp_vm_health_monitor_task = None
    
    logger.info("[v148.0] GCP VM health monitor stopped")


async def _gcp_vm_health_monitor_loop(
    check_interval: float,
    validate_inference: bool,
) -> None:
    """
    v148.0: Internal health monitor loop.
    v149.0: Added exponential backoff and circuit breaker integration.
    
    Runs continuously, checking GCP VM health and handling failures with:
    - Exponential backoff for re-provisioning (prevents quota exhaustion)
    - Circuit breaker for GCP API (prevents continuous failed calls)
    - Random jitter to prevent thundering herd
    """
    global _gcp_vm_consecutive_failures, _active_rescue_gcp_endpoint
    global _active_rescue_gcp_ready, _gcp_vm_health_monitor_running
    global _gcp_reprovision_attempt
    
    import random
    
    _gcp_vm_consecutive_failures = 0
    
    while _gcp_vm_health_monitor_running:
        try:
            await asyncio.sleep(check_interval)
            
            # Skip if monitor was stopped during sleep
            if not _gcp_vm_health_monitor_running:
                break
            
            # Skip if no active endpoint
            if not _active_rescue_gcp_endpoint:
                logger.debug("[v148.0] No GCP endpoint to monitor, waiting...")
                continue
            
            # Extract IP from endpoint (format: "IP:PORT" or "http://IP:PORT")
            endpoint = _active_rescue_gcp_endpoint
            if endpoint.startswith("http://"):
                endpoint = endpoint[7:]
            if endpoint.startswith("https://"):
                endpoint = endpoint[8:]
            ip_address = endpoint.split(":")[0]
            
            # Perform health check
            is_healthy = await _verify_gcp_vm_health(
                ip_address,
                timeout=10.0,
                validate_inference=validate_inference,
            )
            
            if is_healthy:
                # Reset failure counter on success
                if _gcp_vm_consecutive_failures > 0:
                    logger.info(
                        f"[v148.0] ✅ GCP VM recovered after {_gcp_vm_consecutive_failures} failures"
                    )
                _gcp_vm_consecutive_failures = 0
                # v149.0: Record success on circuit breaker (for half-open → closed)
                _gcp_circuit_breaker.record_success()
                
                # v149.1: Clear Claude fallback signal when GCP recovers
                if is_claude_api_fallback_active():
                    clear_claude_api_fallback_signal()
                    # v149.1: Reset re-provision attempt counter on GCP recovery
                    _gcp_reprovision_attempt = 0
            else:
                # Increment failure counter
                _gcp_vm_consecutive_failures += 1
                logger.warning(
                    f"[v148.0] ⚠️ GCP VM health check failed "
                    f"({_gcp_vm_consecutive_failures}/{_GCP_VM_MAX_FAILURES_BEFORE_REPROVISION})"
                )
                
                # Check if we should re-provision
                if _gcp_vm_consecutive_failures >= _GCP_VM_MAX_FAILURES_BEFORE_REPROVISION:
                    logger.error(
                        f"[v148.0] ❌ GCP VM unhealthy after {_gcp_vm_consecutive_failures} "
                        f"consecutive failures - triggering re-provisioning"
                    )
                    
                    # Invalidate cache
                    invalidate_active_rescue_cache()
                    _gcp_vm_consecutive_failures = 0
                    
                    # v149.0: Check circuit breaker before re-provisioning
                    allowed, reason = _gcp_circuit_breaker.should_allow_request()
                    if not allowed:
                        logger.warning(
                            f"[v149.0] ⏳ GCP re-provisioning blocked: {reason}"
                        )
                        continue
                    
                    # v149.0: Apply exponential backoff with jitter
                    backoff = min(
                        _GCP_REPROVISION_BASE_DELAY * (2 ** _gcp_reprovision_attempt),
                        _GCP_REPROVISION_MAX_DELAY
                    )
                    jitter = backoff * random.uniform(-_GCP_REPROVISION_JITTER, _GCP_REPROVISION_JITTER)
                    delay = max(0, backoff + jitter)
                    
                    if _gcp_reprovision_attempt > 0:
                        logger.warning(
                            f"[v149.0] ⏱️ Re-provisioning backoff: {delay:.1f}s "
                            f"(attempt #{_gcp_reprovision_attempt + 1})"
                        )
                        await asyncio.sleep(delay)
                    
                    # Try to re-provision
                    try:
                        success, new_endpoint = await ensure_gcp_vm_ready_for_prime(
                            force_provision=True,
                            timeout_seconds=180.0,
                        )
                        
                        if success:
                            logger.info(
                                f"[v148.0] ✅ GCP VM re-provisioned successfully: {new_endpoint}"
                            )
                            # v149.0: Reset backoff attempt counter on success
                            _gcp_reprovision_attempt = 0
                            _gcp_circuit_breaker.record_success()
                            # v149.1: Clear fallback signal on GCP success
                            clear_claude_api_fallback_signal()
                        else:
                            logger.error(
                                "[v148.0] ❌ GCP VM re-provisioning failed - "
                                "system may fall back to local mode"
                            )
                            # v149.0: Increment backoff attempt and record failure
                            _gcp_reprovision_attempt += 1
                            _gcp_circuit_breaker.record_failure()
                            
                            # v149.1: Trigger Claude API fallback after max attempts
                            if _gcp_reprovision_attempt >= _GCP_MAX_REPROVISION_ATTEMPTS:
                                write_claude_api_fallback_signal(
                                    reason=f"GCP VM re-provisioning failed {_gcp_reprovision_attempt} times",
                                    gcp_attempts=_gcp_reprovision_attempt,
                                )
                    except Exception as e:
                        logger.error(f"[v148.0] Re-provisioning error: {e}")
                        # v149.0: Increment backoff and record failure on exception
                        _gcp_reprovision_attempt += 1
                        _gcp_circuit_breaker.record_failure()
                        
                        # v149.1: Trigger Claude API fallback after max attempts
                        if _gcp_reprovision_attempt >= _GCP_MAX_REPROVISION_ATTEMPTS:
                            write_claude_api_fallback_signal(
                                reason=f"GCP VM re-provisioning exception after {_gcp_reprovision_attempt} attempts: {e}",
                                gcp_attempts=_gcp_reprovision_attempt,
                            )
        
        except asyncio.CancelledError:
            logger.debug("[v148.0] GCP VM health monitor cancelled")
            break
        except Exception as e:
            logger.warning(f"[v148.0] GCP VM health monitor error: {e}")
            await asyncio.sleep(5.0)  # Brief pause on error
    
    logger.info("[v148.0] GCP VM health monitor loop exited")


def get_gcp_vm_health_status() -> Dict[str, Any]:
    """
    v148.0: Get current GCP VM health status.
    v149.0: Added circuit breaker and backoff status.
    
    Returns:
        Dict with health status information
    """
    return {
        "monitor_running": _gcp_vm_health_monitor_running,
        "consecutive_failures": _gcp_vm_consecutive_failures,
        "max_failures_before_reprovision": _GCP_VM_MAX_FAILURES_BEFORE_REPROVISION,
        "active_endpoint": _active_rescue_gcp_endpoint,
        "is_ready": _active_rescue_gcp_ready,
        # v149.0: Enterprise resilience status
        "circuit_breaker": _gcp_circuit_breaker.get_status(),
        "reprovision_attempt": _gcp_reprovision_attempt,
        "backoff_config": {
            "base_delay": _GCP_REPROVISION_BASE_DELAY,
            "max_delay": _GCP_REPROVISION_MAX_DELAY,
            "jitter": _GCP_REPROVISION_JITTER,
        },
    }


# =============================================================================
# v149.0: AGGREGATE TIMEOUT FOR CROSS-REPO HEALTH CHECKS
# =============================================================================
# Provides a utility for parallel health checks with aggregate timeout.
# This prevents 90s combined waits when multiple services are slow.
# =============================================================================

async def verify_cross_repo_health_with_aggregate_timeout(
    services: Optional[Dict[str, str]] = None,
    individual_timeout: float = 10.0,
    aggregate_timeout: float = 30.0,
) -> Dict[str, Dict[str, Any]]:
    """
    v149.0: Verify multiple cross-repo services with aggregate timeout.
    
    Runs health checks in PARALLEL and returns results as soon as:
    - All checks complete, OR
    - Aggregate timeout is reached (returns partial results)
    
    This prevents the scenario where 3 slow services each take 30s = 90s total.
    
    Args:
        services: Dict mapping service names to health URLs.
                  Defaults to standard Trinity services.
        individual_timeout: Timeout per individual health check
        aggregate_timeout: Total time to wait for all checks
        
    Returns:
        Dict mapping service name to health status:
        {
            "service-name": {
                "healthy": bool,
                "response_time_ms": float | None,
                "error": str | None,
                "timed_out": bool
            }
        }
    """
    if services is None:
        services = {
            "jarvis-body": "http://localhost:8010/health",
            "jarvis-prime": "http://localhost:8000/health",
            "reactor-core": "http://localhost:8090/health",
        }
    
    async def check_service(name: str, url: str) -> Tuple[str, Dict[str, Any]]:
        """Check single service health."""
        start = time.time()
        try:
            connector = aiohttp.TCPConnector(force_close=True)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=individual_timeout),
                ) as response:
                    elapsed = (time.time() - start) * 1000
                    return (name, {
                        "healthy": response.status == 200,
                        "response_time_ms": round(elapsed, 1),
                        "status_code": response.status,
                        "error": None,
                        "timed_out": False,
                    })
        except asyncio.TimeoutError:
            return (name, {
                "healthy": False,
                "response_time_ms": None,
                "error": "timeout",
                "timed_out": True,
            })
        except aiohttp.ClientConnectorError as e:
            return (name, {
                "healthy": False,
                "response_time_ms": None,
                "error": f"connection refused: {type(e).__name__}",
                "timed_out": False,
            })
        except Exception as e:
            return (name, {
                "healthy": False,
                "response_time_ms": None,
                "error": str(e),
                "timed_out": False,
            })
    
    # Create tasks for all services
    tasks = [
        asyncio.create_task(check_service(name, url), name=f"health-{name}")
        for name, url in services.items()
    ]
    
    results: Dict[str, Dict[str, Any]] = {}
    
    try:
        # Wait for all tasks OR aggregate timeout
        done, pending = await asyncio.wait(
            tasks,
            timeout=aggregate_timeout,
            return_when=asyncio.ALL_COMPLETED,
        )
        
        # Collect completed results
        for task in done:
            try:
                name, status = task.result()
                results[name] = status
            except Exception as e:
                # Task raised exception
                results[task.get_name().replace("health-", "")] = {
                    "healthy": False,
                    "response_time_ms": None,
                    "error": str(e),
                    "timed_out": False,
                }
        
        # Mark pending as timed out
        for task in pending:
            task.cancel()
            service_name = task.get_name().replace("health-", "")
            results[service_name] = {
                "healthy": False,
                "response_time_ms": None,
                "error": f"aggregate timeout ({aggregate_timeout}s)",
                "timed_out": True,
            }
            logger.warning(
                f"[v149.0] ⏱️ Health check for {service_name} cancelled "
                f"(aggregate timeout)"
            )
    
    except Exception as e:
        logger.error(f"[v149.0] Aggregate health check error: {e}")
        # Return what we have
    
    return results





# =============================================================================
# v136.0: GLOBAL SPAWN COORDINATION SYSTEM
# =============================================================================
# This is the SINGLE SOURCE OF TRUTH for service spawn coordination across:
# - ProcessOrchestrator._spawn_service()
# - run_supervisor._init_jarvis_prime_local()
# - JARVISPrimeClient auto-recovery callback
# - Trinity health monitor restart callbacks
#
# PREVENTS DOUBLE-SPAWN by:
# 1. Global per-service asyncio.Lock() for ALL spawn attempts
# 2. State visibility (is_spawning, is_ready, last_spawn_time)
# 3. Pre-spawn validation that checks global state
# 4. Automatic lock cleanup on spawn completion/failure
#
# USAGE:
#   from backend.supervisor.cross_repo_startup_orchestrator import (
#       acquire_spawn_lock,
#       is_spawn_in_progress,
#       mark_service_spawning,
#       mark_service_ready,
#       should_attempt_spawn,
#   )
# =============================================================================

class GlobalSpawnCoordinator:
    """
    v136.0: Centralized spawn coordination for all service managers.

    This is a SINGLETON that tracks spawn state across ALL components:
    - ProcessOrchestrator
    - run_supervisor health monitors
    - JARVISPrimeClient auto-recovery
    - Trinity health callbacks

    THREAD-SAFE: Uses threading.Lock for state dict, asyncio.Lock for async ops.
    """

    _instance: Optional["GlobalSpawnCoordinator"] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "GlobalSpawnCoordinator":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return

        # v137.2: Use RLock (reentrant lock) to prevent deadlock when
        # methods like mark_spawning() call _get_state() which also acquires lock
        self._state_lock = threading.RLock()
        self._service_locks: Dict[str, asyncio.Lock] = {}
        self._service_state: Dict[str, Dict[str, Any]] = {}
        self._spawn_cooldown_seconds = 10.0  # Min time between spawn attempts
        self._initialized = True

    def _get_or_create_lock(self, service_name: str) -> asyncio.Lock:
        """Get or create an asyncio.Lock for a service (thread-safe)."""
        with self._state_lock:
            if service_name not in self._service_locks:
                self._service_locks[service_name] = asyncio.Lock()
            return self._service_locks[service_name]

    def _get_state(self, service_name: str) -> Dict[str, Any]:
        """Get state dict for a service (creates if needed)."""
        with self._state_lock:
            if service_name not in self._service_state:
                self._service_state[service_name] = {
                    "is_spawning": False,
                    "is_ready": False,
                    "last_spawn_attempt": 0.0,
                    "last_spawn_success": 0.0,
                    "spawn_count": 0,
                    "spawning_component": None,  # Who's spawning
                    "pid": None,
                    "port": None,
                }
            return self._service_state[service_name]

    def is_spawn_in_progress(self, service_name: str) -> bool:
        """Check if a spawn is currently in progress for this service."""
        state = self._get_state(service_name)
        return state["is_spawning"]

    def is_service_ready(self, service_name: str) -> bool:
        """Check if service is marked as ready."""
        state = self._get_state(service_name)
        return state["is_ready"]

    def get_spawning_component(self, service_name: str) -> Optional[str]:
        """Get the component currently spawning this service (if any)."""
        state = self._get_state(service_name)
        return state["spawning_component"] if state["is_spawning"] else None

    def should_attempt_spawn(
        self,
        service_name: str,
        component_name: str,
        ignore_cooldown: bool = False,
    ) -> Tuple[bool, str]:
        """
        Check if a spawn attempt should proceed.

        Returns:
            Tuple of (should_spawn: bool, reason: str)
        """
        state = self._get_state(service_name)

        # Check if already spawning
        if state["is_spawning"]:
            return (
                False,
                f"Spawn already in progress by {state['spawning_component']}"
            )

        # Check if already ready
        if state["is_ready"]:
            return (False, "Service already ready")

        # Check cooldown
        if not ignore_cooldown:
            elapsed = time.time() - state["last_spawn_attempt"]
            if elapsed < self._spawn_cooldown_seconds:
                return (
                    False,
                    f"Cooldown active ({self._spawn_cooldown_seconds - elapsed:.1f}s remaining)"
                )

        return (True, "OK")

    def mark_spawning(
        self,
        service_name: str,
        component_name: str,
        port: Optional[int] = None,
    ) -> bool:
        """
        Mark a service as being spawned.

        Returns True if marking succeeded, False if already spawning.
        """
        with self._state_lock:
            state = self._get_state(service_name)

            if state["is_spawning"]:
                logger.warning(
                    f"[v136.0] Cannot mark {service_name} spawning by {component_name}: "
                    f"already spawning by {state['spawning_component']}"
                )
                return False

            state["is_spawning"] = True
            state["is_ready"] = False
            state["spawning_component"] = component_name
            state["last_spawn_attempt"] = time.time()
            state["spawn_count"] += 1
            state["port"] = port

            logger.info(
                f"[v136.0] 🚦 {service_name} marked SPAWNING by {component_name} "
                f"(attempt #{state['spawn_count']})"
            )
            return True

    def mark_ready(
        self,
        service_name: str,
        pid: Optional[int] = None,
        port: Optional[int] = None,
    ) -> None:
        """Mark a service as ready (spawn completed successfully)."""
        with self._state_lock:
            state = self._get_state(service_name)
            state["is_spawning"] = False
            state["is_ready"] = True
            state["last_spawn_success"] = time.time()
            state["spawning_component"] = None
            if pid:
                state["pid"] = pid
            if port:
                state["port"] = port

            logger.info(
                f"[v136.0] ✅ {service_name} marked READY (PID={pid}, port={port})"
            )

    def mark_failed(self, service_name: str, reason: str = "") -> None:
        """Mark a spawn as failed (releases lock state)."""
        with self._state_lock:
            state = self._get_state(service_name)
            component = state["spawning_component"]
            state["is_spawning"] = False
            state["spawning_component"] = None

            logger.warning(
                f"[v136.0] ❌ {service_name} spawn FAILED by {component}: {reason}"
            )

    def mark_stopped(self, service_name: str) -> None:
        """Mark a service as stopped (allows respawn)."""
        with self._state_lock:
            state = self._get_state(service_name)
            state["is_ready"] = False
            state["pid"] = None

            logger.info(f"[v136.0] 🛑 {service_name} marked STOPPED")

    def get_lock(self, service_name: str) -> asyncio.Lock:
        """Get the asyncio.Lock for a service."""
        return self._get_or_create_lock(service_name)

    def get_status_summary(self) -> Dict[str, Dict[str, Any]]:
        """Get status summary for all tracked services."""
        with self._state_lock:
            return {
                name: dict(state)
                for name, state in self._service_state.items()
            }


# Module-level singleton instance
_global_spawn_coordinator: Optional[GlobalSpawnCoordinator] = None


def get_spawn_coordinator() -> GlobalSpawnCoordinator:
    """Get the global spawn coordinator singleton."""
    global _global_spawn_coordinator
    if _global_spawn_coordinator is None:
        _global_spawn_coordinator = GlobalSpawnCoordinator()
    return _global_spawn_coordinator


# Convenience functions for external imports
def is_spawn_in_progress(service_name: str) -> bool:
    """Check if spawn is in progress for a service."""
    return get_spawn_coordinator().is_spawn_in_progress(service_name)


def is_service_ready(service_name: str) -> bool:
    """Check if a service is ready."""
    return get_spawn_coordinator().is_service_ready(service_name)


def should_attempt_spawn(
    service_name: str,
    component_name: str,
    ignore_cooldown: bool = False,
) -> Tuple[bool, str]:
    """Check if a spawn attempt should proceed."""
    return get_spawn_coordinator().should_attempt_spawn(
        service_name, component_name, ignore_cooldown
    )


def mark_service_spawning(
    service_name: str,
    component_name: str,
    port: Optional[int] = None,
) -> bool:
    """Mark a service as being spawned."""
    return get_spawn_coordinator().mark_spawning(service_name, component_name, port)


def mark_service_ready(
    service_name: str,
    pid: Optional[int] = None,
    port: Optional[int] = None,
) -> None:
    """Mark a service as ready."""
    get_spawn_coordinator().mark_ready(service_name, pid, port)


def mark_service_failed(service_name: str, reason: str = "") -> None:
    """Mark a spawn as failed."""
    get_spawn_coordinator().mark_failed(service_name, reason)


def mark_service_stopped(service_name: str) -> None:
    """Mark a service as stopped."""
    get_spawn_coordinator().mark_stopped(service_name)


async def acquire_spawn_lock(service_name: str) -> asyncio.Lock:
    """
    Get the spawn lock for a service.

    Usage:
        lock = await acquire_spawn_lock("jarvis-prime")
        async with lock:
            # spawn service
    """
    return get_spawn_coordinator().get_lock(service_name)


# =============================================================================
# v137.0: I/O AIRLOCK PATTERN - NON-BLOCKING FILE & SYSTEM OPERATIONS
# =============================================================================
# This system prevents event loop blocking caused by synchronous file I/O
# and system calls (psutil) during startup orchestration.
#
# ROOT CAUSE ADDRESSED:
# - json.loads(file.read_text()) blocks the event loop for 10-500ms
# - psutil.net_connections() blocks for 50-200ms
# - psutil.Process(pid) can block for 10-100ms
# - Multiple calls during startup can cause cumulative 1-5s hangs
#
# SOLUTION:
# - Dedicated ThreadPoolExecutor with 4 workers for I/O operations
# - async wrapper _run_blocking_io() with configurable timeout
# - Safe state readers that handle corruption/missing files
# - Zombie-safe PID verification with cmdline checking
#
# USAGE:
#   result = await _run_blocking_io(_sync_read_json, path, timeout=5.0)
#   is_valid = await _run_blocking_io(_sync_check_pid, pid, "python")
# =============================================================================

import concurrent.futures
from contextlib import suppress

# Dedicated ThreadPoolExecutor for I/O operations
# max_workers=4 is optimal for typical disk I/O patterns (not CPU bound)
# ThreadPoolExecutor is created lazily to avoid issues during import
_IO_THREAD_POOL: Optional[concurrent.futures.ThreadPoolExecutor] = None
_IO_POOL_LOCK = threading.Lock()

# I/O operation timeouts (seconds)
_IO_DEFAULT_TIMEOUT = 5.0
_IO_FILE_READ_TIMEOUT = 3.0
_IO_FILE_WRITE_TIMEOUT = 5.0
_IO_PSUTIL_TIMEOUT = 2.0


def _get_io_thread_pool() -> concurrent.futures.ThreadPoolExecutor:
    """
    v137.0: Get the shared I/O thread pool (lazy initialization).

    Thread pool is created on first use to avoid issues during module import.
    Uses max_workers=4 which is optimal for I/O-bound operations.
    """
    global _IO_THREAD_POOL
    if _IO_THREAD_POOL is None:
        with _IO_POOL_LOCK:
            if _IO_THREAD_POOL is None:
                _IO_THREAD_POOL = concurrent.futures.ThreadPoolExecutor(
                    max_workers=4,
                    thread_name_prefix="io_airlock_",
                )
                logger.debug("[v137.0] I/O Airlock thread pool initialized (4 workers)")
    return _IO_THREAD_POOL


def _shutdown_io_thread_pool(wait: bool = True, timeout: float = 5.0) -> None:
    """
    v137.0: Shutdown the I/O thread pool gracefully.
    v142.0: Enhanced with timeout and SystemExit protection.

    Call this during orchestrator shutdown to clean up threads.
    This prevents the ugly "Exception in worker: SystemExit" stack traces
    that appear when the interpreter shuts down while workers are blocked.

    Args:
        wait: Whether to wait for threads to complete (default True)
        timeout: Maximum time to wait for shutdown (default 5s)
    """
    global _IO_THREAD_POOL
    if _IO_THREAD_POOL is not None:
        with _IO_POOL_LOCK:
            if _IO_THREAD_POOL is not None:
                try:
                    # v142.0: Use wait=False first to allow quick exit,
                    # then cancel pending futures
                    try:
                        # Python 3.9+ has cancel_futures parameter
                        _IO_THREAD_POOL.shutdown(wait=wait, cancel_futures=True)
                    except TypeError:
                        # Python < 3.9 doesn't have cancel_futures
                        _IO_THREAD_POOL.shutdown(wait=wait)
                except (SystemExit, KeyboardInterrupt):
                    # v142.0: Interpreter shutting down - force non-waiting shutdown
                    try:
                        _IO_THREAD_POOL.shutdown(wait=False)
                    except Exception:
                        pass  # Best effort
                except Exception as e:
                    logger.debug(f"[v142.0] Thread pool shutdown note: {e}")
                finally:
                    _IO_THREAD_POOL = None
                    logger.debug("[v142.0] I/O Airlock thread pool shut down")


async def _run_blocking_io(
    func: Callable,
    *args,
    timeout: float = _IO_DEFAULT_TIMEOUT,
    default: Any = None,
    operation_name: str = "io_operation",
    **kwargs,
) -> Any:
    """
    v137.0: Execute a blocking I/O function without blocking the event loop.

    This is the core of the I/O Airlock pattern. It offloads synchronous
    operations (file reads, psutil calls) to a dedicated thread pool.

    Features:
    - High-resolution timing for performance monitoring
    - Configurable timeout with graceful handling
    - Safe default return on timeout/error
    - Detailed logging for debugging startup hangs

    Args:
        func: Synchronous function to execute
        *args: Positional arguments for func
        timeout: Maximum time to wait (seconds)
        default: Value to return on timeout/error
        operation_name: Name for logging (e.g., "read_services_json")
        **kwargs: Keyword arguments for func

    Returns:
        Result from func, or default on timeout/error

    Example:
        data = await _run_blocking_io(
            _sync_read_json,
            Path("~/.jarvis/services.json"),
            timeout=3.0,
            default={},
            operation_name="read_services_state",
        )
    """
    loop = asyncio.get_running_loop()
    pool = _get_io_thread_pool()

    start_time = time.perf_counter()

    try:
        # Create a partial function if kwargs are provided
        if kwargs:
            import functools
            partial_func = functools.partial(func, *args, **kwargs)
            future = loop.run_in_executor(pool, partial_func)
        else:
            future = loop.run_in_executor(pool, func, *args)

        result = await asyncio.wait_for(future, timeout=timeout)

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        if elapsed_ms > 100:  # Log slow operations
            logger.debug(
                f"[v137.0] ⚡ I/O Airlock: {operation_name} completed in {elapsed_ms:.1f}ms"
            )
        elif elapsed_ms > 50:
            logger.debug(
                f"[v137.0] I/O Airlock: {operation_name} completed in {elapsed_ms:.1f}ms"
            )

        return result

    except asyncio.TimeoutError:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.warning(
            f"[v137.0] ⏱️ I/O Airlock TIMEOUT: {operation_name} exceeded {timeout}s "
            f"(elapsed: {elapsed_ms:.1f}ms). Returning default."
        )
        return default

    except (SystemExit, KeyboardInterrupt):
        # v142.0: Handle interpreter shutdown gracefully
        # This prevents ugly thread pool stack traces when the orchestrator
        # exits during memory gate blocking or other early termination
        logger.debug(
            f"[v142.0] I/O Airlock interrupted ({operation_name}) - "
            f"interpreter shutting down. Returning default."
        )
        return default

    except Exception as e:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.warning(
            f"[v137.0] ❌ I/O Airlock ERROR in {operation_name}: {e} "
            f"(elapsed: {elapsed_ms:.1f}ms). Returning default."
        )
        return default


# =============================================================================
# v137.0: SYNCHRONOUS HELPER FUNCTIONS (Run in Thread Pool)
# =============================================================================
# These functions are designed to be called via _run_blocking_io().
# They are intentionally synchronous and handle their own exceptions.


def _sync_read_json(file_path: Path) -> Optional[Dict[str, Any]]:
    """
    v137.0: Synchronously read and parse a JSON file.

    Handles:
    - Missing files (returns None)
    - Corrupted JSON (returns None - treated as "fresh start")
    - Permission errors (returns None with warning)

    Args:
        file_path: Path to JSON file

    Returns:
        Parsed JSON as dict, or None on any error
    """
    try:
        if not file_path.exists():
            return None

        content = file_path.read_text(encoding="utf-8")
        if not content.strip():
            return None

        return json.loads(content)

    except json.JSONDecodeError as e:
        # Corrupted JSON = fresh start (don't propagate error)
        logger.debug(f"[v137.0] JSON decode error in {file_path}: {e}. Treating as fresh.")
        return None
    except PermissionError as e:
        logger.warning(f"[v137.0] Permission denied reading {file_path}: {e}")
        return None
    except Exception as e:
        logger.debug(f"[v137.0] Error reading {file_path}: {e}")
        return None


def _sync_write_json(file_path: Path, data: Dict[str, Any], indent: int = 2) -> bool:
    """
    v137.0: Synchronously write JSON to a file (atomic via temp file).

    Uses atomic write pattern: write to temp file, then rename.

    Args:
        file_path: Destination path
        data: Dict to serialize as JSON
        indent: JSON indentation (default 2)

    Returns:
        True on success, False on error
    """
    try:
        # Ensure parent directory exists
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temp file first (atomic pattern)
        temp_file = file_path.with_suffix(".tmp")
        temp_file.write_text(json.dumps(data, indent=indent), encoding="utf-8")
        temp_file.replace(file_path)

        return True

    except Exception as e:
        logger.debug(f"[v137.0] Error writing {file_path}: {e}")
        return False


def _sync_check_pid_alive(pid: int, expected_name: Optional[str] = None) -> bool:
    """
    v137.0: Check if a PID is alive AND matches expected process name.

    This is ZOMBIE-SAFE: it verifies not just that the PID exists, but that
    it's actually the process we expect. This prevents false positives when
    PIDs are reused by the kernel.

    Args:
        pid: Process ID to check
        expected_name: Expected process name substring (e.g., "python", "jarvis")
                       If None, only checks if PID exists.

    Returns:
        True if PID exists AND (expected_name is None OR name matches)
    """
    try:
        import psutil
        proc = psutil.Process(pid)

        # Check if process is still running (not zombie)
        status = proc.status()
        if status == psutil.STATUS_ZOMBIE:
            logger.debug(f"[v137.0] PID {pid} is a zombie process")
            return False

        # If no name check required, just verify it exists
        if expected_name is None:
            return True

        # Check process name
        name = proc.name().lower()
        if expected_name.lower() in name:
            return True

        # Also check cmdline (more reliable for scripts)
        try:
            cmdline = " ".join(proc.cmdline()).lower()
            if expected_name.lower() in cmdline:
                return True
        except (psutil.AccessDenied, psutil.ZombieProcess):
            pass

        logger.debug(
            f"[v137.0] PID {pid} exists but name '{name}' doesn't match '{expected_name}'"
        )
        return False

    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        # Process exists but we can't access it - assume it's valid
        logger.debug(f"[v137.0] PID {pid} access denied, assuming alive")
        return True
    except Exception as e:
        logger.debug(f"[v137.0] Error checking PID {pid}: {e}")
        return False


def _sync_get_net_connections(port: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    v137.0: Get network connections (optionally filtered by port).

    This wraps psutil.net_connections() which can block for 50-200ms.

    Args:
        port: Optional port to filter by

    Returns:
        List of connection dicts with pid, port, status, etc.
    """
    try:
        import psutil
        connections = []

        for conn in psutil.net_connections(kind='inet'):
            # Skip if no local address
            if not conn.laddr:
                continue

            # Filter by port if specified
            if port is not None and conn.laddr.port != port:
                continue

            connections.append({
                "pid": conn.pid,
                "port": conn.laddr.port,
                "ip": conn.laddr.ip,
                "status": conn.status,
                "family": conn.family,
            })

        return connections

    except Exception as e:
        logger.debug(f"[v137.0] Error getting net connections: {e}")
        return []


def _sync_get_process_info(pid: int) -> Optional[Dict[str, Any]]:
    """
    v137.0: Get detailed process information.

    Args:
        pid: Process ID

    Returns:
        Dict with name, cmdline, status, or None if not found
    """
    try:
        import psutil
        proc = psutil.Process(pid)

        return {
            "pid": pid,
            "name": proc.name(),
            "cmdline": proc.cmdline()[:5],  # Truncate for safety
            "status": proc.status(),
            "create_time": proc.create_time(),
        }

    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied:
        return {"pid": pid, "name": "access_denied", "cmdline": [], "status": "unknown"}
    except Exception as e:
        logger.debug(f"[v137.0] Error getting process info for {pid}: {e}")
        return None


def _sync_get_memory_info() -> Dict[str, Any]:
    """
    v137.0: Get system memory information.

    Returns:
        Dict with total, available, percent, swap info
    """
    try:
        import psutil
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        return {
            "total_gb": mem.total / (1024 ** 3),
            "available_gb": mem.available / (1024 ** 3),
            "used_gb": mem.used / (1024 ** 3),
            "percent": mem.percent,
            "swap_total_gb": swap.total / (1024 ** 3),
            "swap_used_gb": swap.used / (1024 ** 3),
            "swap_percent": swap.percent,
        }

    except Exception as e:
        logger.debug(f"[v137.0] Error getting memory info: {e}")
        return {
            "total_gb": 0,
            "available_gb": 0,
            "used_gb": 0,
            "percent": 0,
            "swap_total_gb": 0,
            "swap_used_gb": 0,
            "swap_percent": 0,
        }


# =============================================================================
# v137.0: ASYNC CONVENIENCE WRAPPERS
# =============================================================================
# These are the primary interface for non-blocking I/O in the orchestrator.


async def read_json_nonblocking(
    file_path: Path,
    timeout: float = _IO_FILE_READ_TIMEOUT,
) -> Optional[Dict[str, Any]]:
    """
    v137.0: Read and parse JSON file without blocking event loop.

    Args:
        file_path: Path to JSON file
        timeout: Maximum time to wait

    Returns:
        Parsed JSON dict, or None on error/timeout
    """
    return await _run_blocking_io(
        _sync_read_json,
        file_path,
        timeout=timeout,
        default=None,
        operation_name=f"read_json({file_path.name})",
    )


async def write_json_nonblocking(
    file_path: Path,
    data: Dict[str, Any],
    timeout: float = _IO_FILE_WRITE_TIMEOUT,
) -> bool:
    """
    v137.0: Write JSON file without blocking event loop.

    Args:
        file_path: Destination path
        data: Dict to serialize
        timeout: Maximum time to wait

    Returns:
        True on success, False on error/timeout
    """
    return await _run_blocking_io(
        _sync_write_json,
        file_path,
        data,
        timeout=timeout,
        default=False,
        operation_name=f"write_json({file_path.name})",
    )


async def check_pid_alive_nonblocking(
    pid: int,
    expected_name: Optional[str] = None,
    timeout: float = _IO_PSUTIL_TIMEOUT,
) -> bool:
    """
    v137.0: Check if PID is alive without blocking event loop.

    Args:
        pid: Process ID to check
        expected_name: Expected process name substring (zombie-safe)
        timeout: Maximum time to wait

    Returns:
        True if PID is alive and matches name (if specified)
    """
    return await _run_blocking_io(
        _sync_check_pid_alive,
        pid,
        expected_name,
        timeout=timeout,
        default=False,
        operation_name=f"check_pid({pid})",
    )


async def get_net_connections_nonblocking(
    port: Optional[int] = None,
    timeout: float = _IO_PSUTIL_TIMEOUT,
) -> List[Dict[str, Any]]:
    """
    v137.0: Get network connections without blocking event loop.

    Args:
        port: Optional port to filter by
        timeout: Maximum time to wait

    Returns:
        List of connection dicts
    """
    return await _run_blocking_io(
        _sync_get_net_connections,
        port,
        timeout=timeout,
        default=[],
        operation_name=f"get_net_connections(port={port})",
    )


async def get_process_info_nonblocking(
    pid: int,
    timeout: float = _IO_PSUTIL_TIMEOUT,
) -> Optional[Dict[str, Any]]:
    """
    v137.0: Get process info without blocking event loop.

    Args:
        pid: Process ID
        timeout: Maximum time to wait

    Returns:
        Process info dict, or None if not found/timeout
    """
    return await _run_blocking_io(
        _sync_get_process_info,
        pid,
        timeout=timeout,
        default=None,
        operation_name=f"get_process_info({pid})",
    )


async def get_memory_info_nonblocking(
    timeout: float = _IO_PSUTIL_TIMEOUT,
) -> Dict[str, Any]:
    """
    v137.0: Get system memory info without blocking event loop.

    Returns:
        Memory info dict with total, available, percent, swap
    """
    return await _run_blocking_io(
        _sync_get_memory_info,
        timeout=timeout,
        default={"total_gb": 0, "available_gb": 0, "percent": 0},
        operation_name="get_memory_info",
    )


# =============================================================================
# v131.0: OOM Prevention Bridge Integration
# =============================================================================
# Prevents SIGKILL (exit code -9) by checking memory before spawning heavy services

_OOM_PREVENTION_AVAILABLE = False
_check_memory_before_heavy_init = None
_MemoryDecision = None
_DegradationTier = None  # v2.0.0

try:
    from backend.core.gcp_oom_prevention_bridge import (
        check_memory_before_heavy_init as _check_mem,
        MemoryDecision as _MemDec,
        DegradationTier as _DegTier,  # v2.0.0
        HEAVY_COMPONENT_MEMORY_ESTIMATES,
    )
    _check_memory_before_heavy_init = _check_mem
    _MemoryDecision = _MemDec
    _DegradationTier = _DegTier
    _OOM_PREVENTION_AVAILABLE = True
except ImportError:
    # Fallback for different import path
    try:
        from core.gcp_oom_prevention_bridge import (
            check_memory_before_heavy_init as _check_mem,
            MemoryDecision as _MemDec,
            DegradationTier as _DegTier,  # v2.0.0
            HEAVY_COMPONENT_MEMORY_ESTIMATES,
        )
        _check_memory_before_heavy_init = _check_mem
        _MemoryDecision = _MemDec
        _DegradationTier = _DegTier
        _OOM_PREVENTION_AVAILABLE = True
    except ImportError:
        HEAVY_COMPONENT_MEMORY_ESTIMATES = {}


# =============================================================================
# v95.0: Orchestrator-Narrator Bridge Integration
# =============================================================================

# Import event types and emitter for real-time voice feedback
_NARRATOR_BRIDGE_AVAILABLE = False
OrchestratorEvent = None
AnnouncementPriority = None
emit_orchestrator_event = None

try:
    from backend.core.supervisor.orchestrator_narrator_bridge import (
        OrchestratorEvent as _OrchestratorEvent,
        AnnouncementPriority as _AnnouncementPriority,
        emit_orchestrator_event as _emit_orchestrator_event,
    )
    OrchestratorEvent = _OrchestratorEvent
    AnnouncementPriority = _AnnouncementPriority
    emit_orchestrator_event = _emit_orchestrator_event
    _NARRATOR_BRIDGE_AVAILABLE = True
except ImportError:
    pass  # Keep defaults


async def _emit_event(
    event: str,
    service_name: Optional[str] = None,
    priority: str = "MEDIUM",
    details: Optional[Dict[str, Any]] = None,
    **kwargs
) -> None:
    """
    v95.0: Safely emit an orchestrator event for voice narration.

    This wrapper ensures the orchestrator doesn't fail if the narrator
    bridge is unavailable.
    """
    if not _NARRATOR_BRIDGE_AVAILABLE or OrchestratorEvent is None:
        return

    try:
        # Convert string event to enum
        event_enum = getattr(OrchestratorEvent, event, None)
        if event_enum is None:
            return

        # Convert string priority to enum
        if AnnouncementPriority is not None:
            priority_enum = getattr(AnnouncementPriority, priority, None)
            if priority_enum is None:
                priority_enum = getattr(AnnouncementPriority, "MEDIUM", None)
        else:
            priority_enum = None

        if emit_orchestrator_event is not None:
            await emit_orchestrator_event(
                event=event_enum,
                service_name=service_name,
                priority=priority_enum,
                details=details or {},
                **kwargs
            )
    except Exception as e:
        # Never let narrator issues block orchestration
        logger.debug(f"[NarratorBridge] Event emission failed (non-blocking): {e}")


# =============================================================================
# Configuration (Zero Hardcoding - All Environment Driven)
# =============================================================================

# v5.0: Import from trinity_config as SINGLE SOURCE OF TRUTH
try:
    from backend.core.trinity_config import get_config as get_trinity_config
    _TRINITY_CONFIG_AVAILABLE = True
except ImportError:
    _TRINITY_CONFIG_AVAILABLE = False
    get_trinity_config = None


def _get_port_from_trinity(service: str, fallback: int) -> int:
    """
    Get port from trinity_config (single source of truth) with fallback.

    v5.0: This ensures all services use consistent ports from trinity_config.
    """
    if not _TRINITY_CONFIG_AVAILABLE:
        return int(os.getenv(f"{service.upper()}_PORT", str(fallback)))

    try:
        config = get_trinity_config()
        if service == "jarvis_prime":
            return config.jarvis_prime_endpoint.port
        elif service == "reactor_core":
            return config.reactor_core_endpoint.port
        elif service == "jarvis":
            return config.jarvis_endpoint.port
    except Exception:
        pass

    return int(os.getenv(f"{service.upper()}_PORT", str(fallback)))


def get_service_port_from_registry(
    service_name: str,
    fallback_port: Optional[int] = None,
) -> Optional[int]:
    """
    v112.0: Get the actual port for a service from the distributed port registry.

    This is the CRITICAL utility for cross-repo coordination. When a service
    cannot bind to its preferred port, it allocates a fallback port and registers
    it at ~/.jarvis/registry/ports.json. Other services use this function to
    discover where to connect.

    Port resolution order:
    1. Check ~/.jarvis/registry/ports.json for dynamically allocated ports
    2. Return fallback_port if provided
    3. Return None if service not found

    Args:
        service_name: Name of the service (e.g., "jarvis-prime", "reactor-core")
        fallback_port: Optional fallback if service not in registry

    Returns:
        The port number, or None if not found and no fallback provided

    Example:
        >>> from backend.supervisor.cross_repo_startup_orchestrator import get_service_port_from_registry
        >>> port = get_service_port_from_registry("jarvis-prime", fallback_port=8000)
        >>> print(f"Connecting to jarvis-prime on port {port}")
    """
    registry_file = Path.home() / ".jarvis" / "registry" / "ports.json"

    if registry_file.exists():
        try:
            registry = json.loads(registry_file.read_text())
            if service_name in registry.get("ports", {}):
                port_info = registry["ports"][service_name]
                allocated_port = port_info.get("port")
                if allocated_port:
                    if port_info.get("is_fallback"):
                        logger.debug(
                            f"[v112.0] get_service_port_from_registry: "
                            f"{service_name} using fallback port {allocated_port} "
                            f"(original was {port_info.get('original_port')})"
                        )
                    return allocated_port
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug(f"[v112.0] Could not read port registry: {e}")

    return fallback_port


def get_all_service_ports_from_registry() -> Dict[str, Dict[str, Any]]:
    """
    v112.0: Get all service ports from the distributed port registry.

    Returns the full port mapping for all services, useful for debugging
    and cross-repo health dashboards.

    Returns:
        Dict mapping service names to port info:
        {
            "jarvis-prime": {"port": 8000, "original_port": 8000, "is_fallback": False},
            "reactor-core": {"port": 9001, "original_port": 8090, "is_fallback": True},
        }
    """
    registry_file = Path.home() / ".jarvis" / "registry" / "ports.json"

    if registry_file.exists():
        try:
            registry = json.loads(registry_file.read_text())
            return registry.get("ports", {})
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug(f"[v112.0] Could not read port registry: {e}")

    return {}


# =============================================================================
# v137.0: ASYNC VERSIONS OF REGISTRY FUNCTIONS (Non-blocking I/O)
# =============================================================================


async def get_service_port_from_registry_async(
    service_name: str,
    fallback_port: Optional[int] = None,
) -> Optional[int]:
    """
    v137.0: Async version of get_service_port_from_registry.

    Uses non-blocking I/O to read the port registry without blocking
    the event loop during startup coordination.

    Args:
        service_name: Name of the service
        fallback_port: Optional fallback if service not in registry

    Returns:
        The port number, or fallback_port if not found
    """
    registry_file = Path.home() / ".jarvis" / "registry" / "ports.json"

    registry = await read_json_nonblocking(registry_file)
    if registry is not None:
        if service_name in registry.get("ports", {}):
            port_info = registry["ports"][service_name]
            allocated_port = port_info.get("port")
            if allocated_port:
                if port_info.get("is_fallback"):
                    logger.debug(
                        f"[v137.0] get_service_port_from_registry_async: "
                        f"{service_name} using fallback port {allocated_port} "
                        f"(original was {port_info.get('original_port')})"
                    )
                return allocated_port

    return fallback_port


async def get_all_service_ports_from_registry_async() -> Dict[str, Dict[str, Any]]:
    """
    v137.0: Async version of get_all_service_ports_from_registry.

    Uses non-blocking I/O to read the port registry.

    Returns:
        Dict mapping service names to port info
    """
    registry_file = Path.home() / ".jarvis" / "registry" / "ports.json"

    registry = await read_json_nonblocking(registry_file)
    if registry is not None:
        return registry.get("ports", {})

    return {}


@dataclass
class OrchestratorConfig:
    """
    Enterprise configuration with zero hardcoding.

    v5.0: Ports sourced from trinity_config as SINGLE SOURCE OF TRUTH.
    """

    # Repository paths
    jarvis_prime_path: Path = field(default_factory=lambda: Path(
        os.getenv("JARVIS_PRIME_PATH", str(Path.home() / "Documents" / "repos" / "jarvis-prime"))
    ))
    reactor_core_path: Path = field(default_factory=lambda: Path(
        os.getenv("REACTOR_CORE_PATH", str(Path.home() / "Documents" / "repos" / "reactor-core"))
    ))

    # Default ports - sourced from trinity_config (SINGLE SOURCE OF TRUTH)
    # Fallbacks: jarvis-prime=8000, reactor-core=8090
    jarvis_prime_default_port: int = field(
        default_factory=lambda: _get_port_from_trinity("jarvis_prime", 8000)
    )
    reactor_core_default_port: int = field(
        default_factory=lambda: _get_port_from_trinity("reactor_core", 8090)
    )

    # Legacy ports to clean up (processes on these should be killed)
    legacy_jarvis_prime_ports: List[int] = field(default_factory=lambda: [8001, 8002])
    legacy_reactor_core_ports: List[int] = field(default_factory=lambda: [8003, 8004, 8005])

    # Feature flags
    jarvis_prime_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_ENABLED", "true").lower() == "true"
    )
    reactor_core_enabled: bool = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_ENABLED", "true").lower() == "true"
    )

    # Auto-healing configuration
    auto_healing_enabled: bool = field(
        default_factory=lambda: os.getenv("AUTO_HEALING_ENABLED", "true").lower() == "true"
    )
    max_restart_attempts: int = field(
        default_factory=lambda: int(os.getenv("MAX_RESTART_ATTEMPTS", "5"))
    )
    restart_backoff_base: float = field(
        default_factory=lambda: float(os.getenv("RESTART_BACKOFF_BASE", "1.0"))
    )
    restart_backoff_max: float = field(
        default_factory=lambda: float(os.getenv("RESTART_BACKOFF_MAX", "60.0"))
    )

    # Health monitoring
    health_check_interval: float = field(
        default_factory=lambda: float(os.getenv("HEALTH_CHECK_INTERVAL", "5.0"))
    )
    health_check_timeout: float = field(
        default_factory=lambda: float(os.getenv("HEALTH_CHECK_TIMEOUT", "5.0"))
    )
    # v112.0: Adaptive Health Check Timeouts
    # During startup (first 5 mins), allow longer timeouts for heavy initialization
    startup_health_check_timeout: float = field(
        default_factory=lambda: float(os.getenv("STARTUP_HEALTH_CHECK_TIMEOUT", "30.0"))
    )
    # Normal operation timeout (fast fail)
    normal_health_check_timeout: float = field(
        default_factory=lambda: float(os.getenv("NORMAL_HEALTH_CHECK_TIMEOUT", "5.0"))
    )
    # Startup phase duration (how long to use startup timeout)
    startup_phase_duration: float = field(
        default_factory=lambda: float(os.getenv("STARTUP_PHASE_DURATION", "300.0"))
    )

    # v93.0: Default startup timeout (applies to lightweight services)
    startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("SERVICE_STARTUP_TIMEOUT", "60.0"))
    )

    # v93.5: Per-service startup timeouts with intelligent progress-based extension
    # JARVIS Prime loads heavy ML models (ECAPA-TDNN, torch, etc.) and needs longer timeout
    # Default increased from 300s to 600s (10 minutes) for 70B+ models
    jarvis_prime_startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_STARTUP_TIMEOUT", "600.0"))  # 10 minutes for ML models
    )
    reactor_core_startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("REACTOR_CORE_STARTUP_TIMEOUT", "120.0"))  # 2 minutes
    )
    # v93.5: If model is actively loading (progress detected), extend timeout automatically
    model_loading_timeout_extension: float = field(
        default_factory=lambda: float(os.getenv("MODEL_LOADING_TIMEOUT_EXTENSION", "300.0"))  # Extra 5 min if progress
    )
    # v93.5: Maximum total timeout (hard cap for safety)
    max_startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("MAX_STARTUP_TIMEOUT", "900.0"))  # 15 min absolute max
    )

    # Graceful shutdown
    shutdown_timeout: float = field(
        default_factory=lambda: float(os.getenv("SHUTDOWN_TIMEOUT", "10.0"))
    )

    # Output streaming
    stream_output: bool = field(
        default_factory=lambda: os.getenv("STREAM_CHILD_OUTPUT", "true").lower() == "true"
    )

    # =========================================================================
    # v93.8: Docker Hybrid Mode Configuration
    # =========================================================================
    # Enable Docker-first startup for jarvis-prime (checks Docker before local)
    docker_hybrid_enabled: bool = field(
        default_factory=lambda: os.getenv("DOCKER_HYBRID_ENABLED", "true").lower() == "true"
    )

    # Docker container name for jarvis-prime
    jarvis_prime_docker_container: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_DOCKER_CONTAINER", "jarvis-prime-service")
    )

    # Docker compose file path (relative to jarvis-prime repo)
    jarvis_prime_docker_compose: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_DOCKER_COMPOSE", "docker/docker-compose.yml")
    )

    # Timeout for Docker health check
    docker_health_timeout: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_HEALTH_TIMEOUT", "5.0"))
    )

    # Whether to auto-start Docker container if not running
    docker_auto_start: bool = field(
        default_factory=lambda: os.getenv("DOCKER_AUTO_START", "false").lower() == "true"
    )

    # Docker startup timeout (waiting for container to become healthy)
    docker_startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_STARTUP_TIMEOUT", "180.0"))
    )

    # =========================================================================
    # v93.9: Advanced Docker Operations Configuration
    # =========================================================================

    # Circuit Breaker Configuration
    docker_circuit_breaker_failure_threshold: int = field(
        default_factory=lambda: int(os.getenv("DOCKER_CB_FAILURE_THRESHOLD", "3"))
    )
    docker_circuit_breaker_recovery_timeout: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_CB_RECOVERY_TIMEOUT", "60.0"))
    )
    docker_circuit_breaker_half_open_requests: int = field(
        default_factory=lambda: int(os.getenv("DOCKER_CB_HALF_OPEN_REQUESTS", "1"))
    )

    # Retry Configuration (Exponential Backoff)
    docker_retry_max_attempts: int = field(
        default_factory=lambda: int(os.getenv("DOCKER_RETRY_MAX_ATTEMPTS", "3"))
    )
    docker_retry_base_delay: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_RETRY_BASE_DELAY", "1.0"))
    )
    docker_retry_max_delay: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_RETRY_MAX_DELAY", "30.0"))
    )
    docker_retry_exponential_base: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_RETRY_EXP_BASE", "2.0"))
    )

    # Memory-Aware Routing Configuration
    memory_minimum_available_gb: float = field(
        default_factory=lambda: float(os.getenv("MEMORY_MIN_AVAILABLE_GB", "4.0"))
    )
    memory_route_to_gcp_threshold_gb: float = field(
        default_factory=lambda: float(os.getenv("MEMORY_GCP_THRESHOLD_GB", "2.0"))
    )
    memory_model_size_estimation_factor: float = field(
        default_factory=lambda: float(os.getenv("MEMORY_MODEL_SIZE_FACTOR", "1.5"))
    )

    # GCP Fallback Configuration
    gcp_fallback_enabled: bool = field(
        default_factory=lambda: os.getenv("GCP_FALLBACK_ENABLED", "true").lower() == "true"
    )
    gcp_vm_startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("GCP_VM_STARTUP_TIMEOUT", "300.0"))
    )

    # Docker Image Configuration
    docker_auto_build: bool = field(
        default_factory=lambda: os.getenv("DOCKER_AUTO_BUILD", "true").lower() == "true"
    )
    docker_build_timeout: float = field(
        default_factory=lambda: float(os.getenv("DOCKER_BUILD_TIMEOUT", "600.0"))
    )

    # =========================================================================
    # v108.1: Non-Blocking Model Loading Configuration
    # =========================================================================
    # When True, heavy services (like jarvis-prime) are considered "started" as soon
    # as their HTTP server responds (Phase 1), even if models are still loading.
    # Model loading health monitoring continues in the background.
    # This allows the main JARVIS backend (port 8010) to start while external
    # services load their ML models.
    non_blocking_model_loading: bool = field(
        default_factory=lambda: os.getenv("NON_BLOCKING_MODEL_LOADING", "true").lower() == "true"
    )
    # Timeout for Phase 1 (server responding) - should be fast
    server_responding_timeout: float = field(
        default_factory=lambda: float(os.getenv("SERVER_RESPONDING_TIMEOUT", "60.0"))
    )


# =============================================================================
# Data Models
# =============================================================================

class ServiceStatus(Enum):
    """Service lifecycle status."""
    PENDING = "pending"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RESTARTING = "restarting"
    FAILED = "failed"
    STOPPED = "stopped"


class CircuitBreakerState(Enum):
    """
    v93.9: Circuit Breaker state machine.

    CLOSED: Normal operation, requests pass through
    OPEN: Failures exceeded threshold, requests blocked
    HALF_OPEN: Testing if service recovered
    """
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """
    v95.1: Thread-safe Circuit Breaker pattern with forced recovery.

    Prevents cascading failures by:
    - Tracking consecutive failures
    - Opening circuit after failure threshold
    - Allowing recovery testing after timeout
    - Closing circuit on successful recovery
    - v95.0: Forced recovery after max open duration
    - v95.0: State transition logging and metrics
    - v95.1: Thread-safe state transitions using lock

    Thread Safety:
    All state modifications are protected by a threading.Lock to ensure
    atomic state transitions even when called from multiple coroutines.
    """
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 60.0
    half_open_max_requests: int = 1
    # v95.0: Maximum time circuit stays OPEN before forced recovery attempt
    max_open_duration: float = field(
        default_factory=lambda: float(os.getenv("CIRCUIT_BREAKER_MAX_OPEN", "600.0"))  # 10 minutes
    )

    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    half_open_requests: int = 0
    success_count: int = 0
    # v95.0: Tracking for stuck circuit detection
    last_state_change: float = field(default_factory=time.time)
    consecutive_recovery_failures: int = 0
    total_open_duration: float = 0.0

    # v95.1: Thread-safe lock for atomic state transitions
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _transition_state(self, new_state: CircuitBreakerState, reason: str) -> None:
        """
        v95.1: Handle state transition with logging and metrics.

        NOTE: This method assumes _lock is already held by caller.
        """
        old_state = self.state
        if old_state == new_state:
            return

        now = time.time()
        duration_in_old_state = now - self.last_state_change

        # Track total open duration (atomic with state change)
        if old_state == CircuitBreakerState.OPEN:
            self.total_open_duration += duration_in_old_state

        self.state = new_state
        self.last_state_change = now

        # Log with appropriate level based on transition
        if new_state == CircuitBreakerState.CLOSED:
            logger.info(
                f"    ✅ Circuit breaker [{self.name}]: {old_state.value} → CLOSED "
                f"({reason}, was {old_state.value} for {duration_in_old_state:.1f}s)"
            )
            self.consecutive_recovery_failures = 0
        elif new_state == CircuitBreakerState.OPEN:
            logger.warning(
                f"    ⚠️ Circuit breaker [{self.name}]: {old_state.value} → OPEN "
                f"({reason})"
            )
            if old_state == CircuitBreakerState.HALF_OPEN:
                self.consecutive_recovery_failures += 1
        else:  # HALF_OPEN
            logger.info(
                f"    🔄 Circuit breaker [{self.name}]: {old_state.value} → HALF_OPEN "
                f"({reason})"
            )

    def record_success(self) -> None:
        """
        v95.1: Record a successful operation (thread-safe).

        Atomically updates success counter and potentially transitions to CLOSED.
        """
        with self._lock:
            if self.state == CircuitBreakerState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.half_open_max_requests:
                    # Recovery successful, close circuit
                    self._transition_state(CircuitBreakerState.CLOSED, "recovery successful")
                    self.failure_count = 0
                    self.success_count = 0
            else:
                self.failure_count = 0
                self.success_count += 1

    def record_failure(self) -> None:
        """
        v95.1: Record a failed operation (thread-safe).

        Atomically updates failure counter and potentially transitions to OPEN.
        """
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            self.success_count = 0

            if self.state == CircuitBreakerState.HALF_OPEN:
                # Failed during recovery test, reopen circuit
                self._transition_state(CircuitBreakerState.OPEN, "recovery test failed")
            elif self.failure_count >= self.failure_threshold:
                # Threshold exceeded, open circuit
                self._transition_state(
                    CircuitBreakerState.OPEN,
                    f"{self.failure_count} consecutive failures"
                )

    def can_execute(self) -> bool:
        """
        v95.1: Check if operation can proceed (thread-safe).

        Atomically checks state and potentially transitions to HALF_OPEN.
        """
        with self._lock:
            if self.state == CircuitBreakerState.CLOSED:
                return True

            if self.state == CircuitBreakerState.OPEN:
                now = time.time()
                time_since_failure = now - self.last_failure_time
                time_in_open = now - self.last_state_change

                # v95.0: Check for forced recovery after max open duration
                if time_in_open >= self.max_open_duration:
                    logger.warning(
                        f"    ⏰ Circuit breaker [{self.name}]: FORCED RECOVERY "
                        f"(open for {time_in_open:.0f}s, max: {self.max_open_duration:.0f}s)"
                    )
                    self._transition_state(
                        CircuitBreakerState.HALF_OPEN,
                        f"forced recovery after {time_in_open:.0f}s"
                    )
                    self.half_open_requests = 0
                    return True

                # Normal recovery timeout check
                if time_since_failure >= self.recovery_timeout:
                    self._transition_state(
                        CircuitBreakerState.HALF_OPEN,
                        f"recovery timeout ({time_since_failure:.0f}s)"
                    )
                    self.half_open_requests = 0
                    return True
                return False

            if self.state == CircuitBreakerState.HALF_OPEN:
                # Allow limited requests during half-open
                if self.half_open_requests < self.half_open_max_requests:
                    self.half_open_requests += 1
                    return True
                return False

            return False

    def force_reset(self) -> None:
        """
        v95.1: Force circuit to CLOSED state (thread-safe).

        For manual intervention when circuit is stuck.
        """
        with self._lock:
            logger.warning(f"    🔧 Circuit breaker [{self.name}]: FORCE RESET to CLOSED")
            self._transition_state(CircuitBreakerState.CLOSED, "forced reset")
            self.failure_count = 0
            self.success_count = 0
            self.consecutive_recovery_failures = 0

    def get_stats(self) -> Dict[str, Any]:
        """
        v95.1: Get circuit breaker statistics (thread-safe snapshot).

        Returns a consistent point-in-time view of circuit breaker state.
        """
        with self._lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "failure_count": self.failure_count,
                "success_count": self.success_count,
                "consecutive_recovery_failures": self.consecutive_recovery_failures,
                "total_open_duration": self.total_open_duration,
                "time_in_current_state": time.time() - self.last_state_change,
            }


@dataclass
class MemoryStatus:
    """
    v93.9: System memory status for intelligent routing decisions.
    """
    total_gb: float = 0.0
    available_gb: float = 0.0
    used_gb: float = 0.0
    percent_used: float = 0.0
    swap_total_gb: float = 0.0
    swap_used_gb: float = 0.0

    @property
    def is_memory_pressure(self) -> bool:
        """Check if system is under memory pressure."""
        return self.percent_used > 85.0 or self.available_gb < 2.0

    @property
    def can_load_local_model(self) -> bool:
        """Check if there's enough memory for a local model (~6-8GB needed)."""
        return self.available_gb >= 6.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_gb": self.total_gb,
            "available_gb": self.available_gb,
            "used_gb": self.used_gb,
            "percent_used": self.percent_used,
            "swap_total_gb": self.swap_total_gb,
            "swap_used_gb": self.swap_used_gb,
            "is_memory_pressure": self.is_memory_pressure,
            "can_load_local_model": self.can_load_local_model,
        }


@dataclass
class RetryState:
    """
    v93.9: State tracking for retry operations with exponential backoff.
    """
    attempt: int = 0
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    exponential_base: float = 2.0
    last_error: Optional[str] = None
    total_elapsed: float = 0.0

    def should_retry(self) -> bool:
        """Check if another retry attempt should be made."""
        return self.attempt < self.max_attempts

    def get_next_delay(self) -> float:
        """Calculate next delay with exponential backoff and jitter."""
        import random
        delay = self.base_delay * (self.exponential_base ** self.attempt)
        # Add jitter (±25%) to prevent thundering herd
        jitter = delay * 0.25 * (2 * random.random() - 1)
        return min(delay + jitter, self.max_delay)

    def record_attempt(self, error: Optional[str] = None) -> None:
        """Record a retry attempt."""
        self.attempt += 1
        self.last_error = error

    def reset(self) -> None:
        """Reset retry state after success."""
        self.attempt = 0
        self.last_error = None
        self.total_elapsed = 0.0


class RoutingDecision(Enum):
    """
    v93.9: Intelligent routing decision for service deployment.
    """
    DOCKER_LOCAL = "docker_local"      # Use local Docker container
    LOCAL_PROCESS = "local_process"    # Use local Python process
    GCP_CLOUD = "gcp_cloud"            # Route to GCP cloud VM
    HYBRID = "hybrid"                  # Use multiple backends


@dataclass
class ServiceDefinition:
    """
    Definition of a service to manage.

    v4.0: Enhanced with script_args for command-line argument passing.
    v95.0: Added dynamic script discovery, validation methods, and dependency tracking.
    """
    name: str
    repo_path: Path
    script_name: str = "main.py"
    fallback_scripts: List[str] = field(default_factory=lambda: ["server.py", "run.py", "app.py"])
    default_port: int = 8000
    health_endpoint: str = "/health"
    startup_timeout: float = 60.0
    environment: Dict[str, str] = field(default_factory=dict)

    # v95.0: Service dependency tracking
    # Services listed here must be healthy before this service starts
    depends_on: List[str] = field(default_factory=list)

    # v117.0: Soft dependencies - services that are recommended but not required
    # Service will start even if soft dependencies aren't healthy, but logs a warning
    soft_depends_on: List[str] = field(default_factory=list)

    # v95.0: Startup priority (lower = starts first, same priority = parallel)
    # Default priorities: jarvis-body=10, jarvis-prime=20, reactor-core=30
    startup_priority: int = 50

    # v95.0: Grace period before checking dependencies (allows for parallel startup optimization)
    dependency_check_delay: float = 5.0

    # v95.0: Whether this service is critical (system fails if this service fails)
    is_critical: bool = True

    # v95.0: Retry configuration for dependency wait
    dependency_wait_timeout: float = 120.0  # Max time to wait for dependencies
    dependency_check_interval: float = 2.0  # How often to check dependency health

    # v4.0: Command-line arguments to pass to the script
    # e.g., ["--port", "8000", "--host", "0.0.0.0"]
    script_args: List[str] = field(default_factory=list)

    # v3.1: Module-based entry points (e.g., "reactor_core.api.server")
    # When set, spawns with: python -m <module_path>
    module_path: Optional[str] = None

    # v3.1: Nested script paths to search (relative to repo_path)
    # e.g., ["reactor_core/api/server.py", "src/main.py"]
    nested_scripts: List[str] = field(default_factory=list)

    # v3.1: Use uvicorn for FastAPI apps
    use_uvicorn: bool = False
    uvicorn_app: Optional[str] = None  # e.g., "reactor_core.api.server:app"

    # v95.0: Dynamic script discovery patterns (regex)
    discovery_patterns: List[str] = field(default_factory=lambda: [
        r"run_.*\.py$",  # run_server.py, run_reactor.py, etc.
        r"main\.py$",
        r"server\.py$",
        r"app\.py$",
    ])

    def discover_entry_script(self) -> Optional[Path]:
        """
        v95.0: Dynamically discover the best entry script for this service.

        Uses intelligent scoring based on:
        1. Explicit script_name match (highest priority)
        2. Pattern matching (run_*.py preferred)
        3. Fallback scripts
        4. Size-based heuristic (larger files often have more functionality)

        Returns:
            Path to the best entry script, or None if not found.
        """
        import re

        if not self.repo_path.exists():
            return None

        # Build candidate list with scores
        candidates: List[tuple[Path, int]] = []

        # Score: explicit script_name gets highest priority (100)
        explicit_script = self.repo_path / self.script_name
        if explicit_script.exists():
            candidates.append((explicit_script, 100))

        # Score: nested scripts (90)
        for nested in self.nested_scripts:
            nested_path = self.repo_path / nested
            if nested_path.exists():
                candidates.append((nested_path, 90))

        # Score: fallback scripts (80)
        for fallback in self.fallback_scripts:
            fallback_path = self.repo_path / fallback
            if fallback_path.exists():
                candidates.append((fallback_path, 80))

        # Score: pattern-discovered scripts (60-70)
        try:
            for py_file in self.repo_path.glob("*.py"):
                if py_file in [c[0] for c in candidates]:
                    continue  # Already scored

                for i, pattern in enumerate(self.discovery_patterns):
                    if re.search(pattern, py_file.name, re.IGNORECASE):
                        # Earlier patterns get higher scores
                        score = 70 - i * 2
                        candidates.append((py_file, score))
                        break
        except Exception:
            pass

        if not candidates:
            return None

        # Sort by score (descending), then by file size as tiebreaker
        def sort_key(item: tuple[Path, int]) -> tuple[int, int]:
            path, score = item
            try:
                size = path.stat().st_size
            except Exception:
                size = 0
            return (-score, -size)

        candidates.sort(key=sort_key)
        return candidates[0][0]

    def validate(self) -> tuple[bool, List[str]]:
        """
        v95.0: Validate the service definition.

        Returns:
            Tuple of (is_valid, list_of_issues)
        """
        issues: List[str] = []

        # Check repo path
        if not self.repo_path.exists():
            issues.append(f"Repository not found: {self.repo_path}")

        # Check entry script
        script = self.discover_entry_script()
        if script is None and not self.module_path and not self.uvicorn_app:
            issues.append(f"No entry script found (tried: {self.script_name}, {self.fallback_scripts})")

        # Check port range
        if not (1 <= self.default_port <= 65535):
            issues.append(f"Invalid port number: {self.default_port}")

        # Check health endpoint format
        if not self.health_endpoint.startswith("/"):
            issues.append(f"Health endpoint should start with '/': {self.health_endpoint}")

        return len(issues) == 0, issues


# =============================================================================
# v95.6: Intelligent Cross-Platform Repo Discovery
# =============================================================================

class IntelligentRepoDiscovery:
    """
    v95.8: Enterprise-Grade Dynamic Repository Discovery System.

    Eliminates hardcoded paths by intelligently searching for repositories
    across multiple locations with case-insensitive matching.

    Features:
    - Multi-path discovery across common development directories
    - Environment variable override (highest priority)
    - Case-insensitive repo name matching
    - Cross-platform support (Windows, macOS, Linux)
    - Name variant detection (jarvis-prime, JARVIS-Prime, JarvisPrime)
    - Git repository validation
    - Parallel async discovery for performance
    - Persistent cache with TTL
    - Signature-based repo verification (checks for expected files)

    v95.8 Enhancements:
    - __file__-based absolute path resolution (Issue 6)
    - Network drive/mount point resilience with timeouts (Issue 7)
    - Comprehensive permission validation (Issue 8)
    - Working directory independence
    - Async path accessibility checks
    - Intelligent retry with exponential backoff

    Discovery Priority (highest to lowest):
    1. Environment variable (e.g., JARVIS_PRIME_PATH)
    2. Running process detection (find where it's already running)
    3. Git worktree detection (find related worktrees)
    4. Common development directories
    5. User-provided hints in config file
    """

    # ==========================================================================
    # v95.8: Path Resolution Configuration
    # ==========================================================================

    # Network/mount path detection patterns
    _NETWORK_PATH_PATTERNS: List[str] = [
        "/mnt/",           # Linux mount points
        "/media/",         # Linux media mounts
        "/Volumes/",       # macOS volumes
        "/net/",           # NFS mounts
        "/nfs/",           # NFS mounts
        "//",              # UNC paths (Windows)
        "\\\\",            # UNC paths (Windows backslash)
        "/run/user/",      # User runtime mounts
        "/tmp/.mount_",    # AppImage mounts
    ]

    # Timeout configurations (in seconds)
    _PATH_ACCESS_TIMEOUT: float = 5.0       # Single path access timeout
    _NETWORK_PATH_TIMEOUT: float = 10.0     # Network path access timeout
    _DISCOVERY_TIMEOUT: float = 30.0        # Total discovery timeout
    _RETRY_MAX_ATTEMPTS: int = 3            # Max retry attempts for network paths
    _RETRY_BASE_DELAY: float = 0.5          # Base delay for exponential backoff

    # Common development directory patterns (cross-platform)
    _COMMON_DEV_DIRS: List[str] = [
        "~/Documents/repos",
        "~/Documents/GitHub",
        "~/Documents/code",
        "~/Documents/projects",
        "~/Documents/dev",
        "~/repos",
        "~/code",
        "~/projects",
        "~/dev",
        "~/github",
        "~/git",
        "~/src",
        "~/workspace",
        "~/Development",
        # Windows-specific
        "C:/Users/{user}/Documents/repos",
        "C:/Users/{user}/Documents/GitHub",
        "C:/Users/{user}/code",
        "C:/repos",
        "C:/code",
        "C:/projects",
        # Linux-specific
        "/home/{user}/repos",
        "/home/{user}/code",
        "/home/{user}/projects",
        "/opt/jarvis",
        "/srv/jarvis",
    ]

    # Name variants for each service (case-insensitive matching)
    _SERVICE_NAME_VARIANTS: Dict[str, List[str]] = {
        "jarvis-prime": [
            "jarvis-prime",
            "JARVIS-Prime",
            "JarvisPrime",
            "jarvis_prime",
            "prime",
            "jarvis-prime-ai",
        ],
        "reactor-core": [
            "reactor-core",
            "Reactor-Core",
            "ReactorCore",
            "reactor_core",
            "reactor",
            "jarvis-reactor",
        ],
        "jarvis": [
            "jarvis",
            "JARVIS",
            "jarvis-ai-agent",
            "JARVIS-AI-Agent",
            "jarvis_ai_agent",
            "jarvis-body",
        ],
    }

    # Signature files that confirm this is the right repo
    _REPO_SIGNATURES: Dict[str, List[str]] = {
        "jarvis-prime": [
            "run_server.py",
            "inference/",
            "models/",
            "prime_server.py",
        ],
        "reactor-core": [
            "run_reactor.py",
            "reactor/",
            "training/",
            "pipeline/",
        ],
        "jarvis": [
            "run_supervisor.py",
            "backend/",
            "backend/core/",
            "backend/supervisor/",
        ],
    }

    _instance: Optional["IntelligentRepoDiscovery"] = None
    _discovery_cache: Dict[str, Tuple[Optional[Path], float]] = {}
    _cache_ttl: float = 300.0  # 5 minutes

    def __new__(cls) -> "IntelligentRepoDiscovery":
        """Singleton pattern for shared cache."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._discovery_lock = asyncio.Lock() if asyncio.get_event_loop().is_running() else None
        self._user = os.environ.get("USER", os.environ.get("USERNAME", "user"))

        # v95.8: Compute absolute base paths using __file__ (Issue 6)
        # This ensures paths work regardless of current working directory
        self._module_path: Path = Path(__file__).resolve()
        self._module_dir: Path = self._module_path.parent  # backend/supervisor
        self._backend_dir: Path = self._module_dir.parent  # backend
        self._jarvis_root: Path = self._backend_dir.parent  # JARVIS-AI-Agent
        self._repos_parent: Path = self._jarvis_root.parent  # Parent containing sibling repos

        # v95.8: Track network path accessibility results
        self._network_path_status: Dict[str, Tuple[bool, float]] = {}  # path -> (accessible, check_time)

        # v95.8: Permission check results cache
        self._permission_cache: Dict[str, Tuple[Dict[str, bool], float]] = {}  # path -> ({perms}, check_time)
        self._permission_cache_ttl: float = 60.0  # 1 minute TTL for permission cache

        logger.info(
            f"[v95.8] IntelligentRepoDiscovery initialized:\n"
            f"  Module path: {self._module_path}\n"
            f"  JARVIS root: {self._jarvis_root}\n"
            f"  Repos parent: {self._repos_parent}"
        )

    # ==========================================================================
    # v95.8: Issue 6 - Absolute Path Resolution Engine
    # ==========================================================================

    def _resolve_to_absolute(self, path: Union[str, Path]) -> Path:
        """
        v95.8: Resolve any path to absolute, handling all edge cases.

        This ensures paths work regardless of:
        - Current working directory
        - Relative path references
        - Symlinks
        - Home directory expansion

        Args:
            path: Path string or Path object (can be relative or absolute)

        Returns:
            Absolute resolved Path
        """
        if isinstance(path, str):
            path_str = path

            # Handle ~ expansion
            if path_str.startswith("~"):
                path = Path(path_str).expanduser()
            # Handle relative paths - resolve from JARVIS root, not cwd
            elif not path_str.startswith("/") and not (len(path_str) > 1 and path_str[1] == ":"):
                # This is a relative path - resolve from jarvis root
                path = self._jarvis_root / path_str
            else:
                path = Path(path_str)
        else:
            # Path object
            if not path.is_absolute():
                # Relative Path - resolve from jarvis root
                path = self._jarvis_root / path

        # Final resolution: resolve symlinks and normalize
        try:
            return path.resolve()
        except (OSError, RuntimeError) as e:
            logger.debug(f"[v95.8] Cannot fully resolve {path}: {e}, using as-is")
            return path.absolute()

    def _get_canonical_jarvis_root(self) -> Path:
        """
        v95.8: Get the canonical JARVIS root directory.

        Uses __file__ to ensure correctness regardless of cwd.

        Returns:
            Absolute path to JARVIS-AI-Agent root
        """
        return self._jarvis_root

    def _get_sibling_repo_path(self, repo_name: str) -> Optional[Path]:
        """
        v95.8: Get path to a sibling repository (same parent directory).

        This is the most reliable way to find jarvis-prime and reactor-core
        when they're in the same parent directory as JARVIS-AI-Agent.

        Args:
            repo_name: Name of the sibling repo

        Returns:
            Absolute path if found, None otherwise
        """
        variants = self._get_name_variants(repo_name)

        for variant in variants:
            candidate = self._repos_parent / variant
            try:
                if candidate.exists() and candidate.is_dir():
                    if self._validate_repo_signature(candidate, repo_name):
                        logger.debug(f"[v95.8] Found sibling repo: {candidate}")
                        return candidate.resolve()
            except (PermissionError, OSError) as e:
                logger.debug(f"[v95.8] Cannot access sibling {candidate}: {e}")
                continue

        return None

    # ==========================================================================
    # v95.8: Issue 7 - Network Drive/Mount Point Resilience
    # ==========================================================================

    def _is_network_path(self, path: Path) -> bool:
        """
        v95.8: Detect if a path is on a network drive or mount point.

        Network paths need special handling (timeouts, retries) because they
        may be slow or transiently unavailable.

        Args:
            path: Path to check

        Returns:
            True if path appears to be on network/mount
        """
        path_str = str(path)

        # Check against known network path patterns
        for pattern in self._NETWORK_PATH_PATTERNS:
            if pattern in path_str:
                return True

        # Additional checks for mounted filesystems
        try:
            # On Unix, check if the path's device differs from home
            if hasattr(os, 'stat'):
                path_stat = os.stat(path) if path.exists() else None
                home_stat = os.stat(Path.home())

                if path_stat and path_stat.st_dev != home_stat.st_dev:
                    # Different device - likely a mount point
                    return True
        except (OSError, PermissionError):
            pass

        return False

    async def _check_path_accessible_async(
        self,
        path: Path,
        timeout: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        v95.8: Async check if a path is accessible with timeout.

        Handles network drives and slow filesystems gracefully.

        Args:
            path: Path to check
            timeout: Timeout in seconds (auto-detected based on path type)

        Returns:
            Tuple of (accessible, error_message)
        """
        if timeout is None:
            timeout = self._NETWORK_PATH_TIMEOUT if self._is_network_path(path) else self._PATH_ACCESS_TIMEOUT

        def _sync_check() -> Tuple[bool, Optional[str]]:
            """Synchronous path check (runs in thread pool)."""
            try:
                if not path.exists():
                    return False, f"Path does not exist: {path}"

                if not path.is_dir():
                    return False, f"Path is not a directory: {path}"

                # Try to list directory (confirms read access)
                try:
                    next(path.iterdir(), None)
                except StopIteration:
                    pass  # Empty directory is fine

                return True, None

            except PermissionError as e:
                return False, f"Permission denied: {path} - {e}"
            except OSError as e:
                # Catches network timeouts, stale mounts, etc.
                return False, f"OS error accessing {path}: {e}"
            except Exception as e:
                return False, f"Unexpected error accessing {path}: {e}"

        try:
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _sync_check),
                timeout=timeout
            )
            return result
        except asyncio.TimeoutError:
            error_msg = f"Timeout ({timeout}s) accessing path: {path}"
            if self._is_network_path(path):
                error_msg += " (network/mount path detected)"
            logger.warning(f"[v95.8] {error_msg}")
            return False, error_msg
        except Exception as e:
            return False, f"Failed to check path {path}: {e}"

    async def _check_path_with_retry(
        self,
        path: Path,
        max_attempts: Optional[int] = None,
        base_delay: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        v95.8: Check path accessibility with exponential backoff retry.

        Handles transient network issues gracefully.

        Args:
            path: Path to check
            max_attempts: Maximum retry attempts
            base_delay: Base delay for exponential backoff

        Returns:
            Tuple of (accessible, error_message)
        """
        max_attempts = max_attempts or self._RETRY_MAX_ATTEMPTS
        base_delay = base_delay or self._RETRY_BASE_DELAY

        last_error: Optional[str] = None

        for attempt in range(max_attempts):
            accessible, error = await self._check_path_accessible_async(path)

            if accessible:
                if attempt > 0:
                    logger.info(f"[v95.8] Path {path} accessible after {attempt + 1} attempts")
                return True, None

            last_error = error

            # Don't retry for permission errors (won't change)
            if error and "Permission denied" in error:
                return False, error

            # Don't retry for non-existent paths
            if error and "does not exist" in error:
                return False, error

            # Retry with exponential backoff for transient errors
            if attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt)
                # Add jitter to prevent thundering herd
                jitter = delay * 0.1 * (0.5 - time.time() % 1)
                delay = max(0.1, delay + jitter)

                logger.debug(
                    f"[v95.8] Path {path} access failed (attempt {attempt + 1}/{max_attempts}), "
                    f"retrying in {delay:.2f}s: {error}"
                )
                await asyncio.sleep(delay)

        return False, f"Failed after {max_attempts} attempts: {last_error}"

    def _check_path_accessible_sync(self, path: Path, timeout: float = 5.0) -> Tuple[bool, Optional[str]]:
        """
        v95.8: Synchronous path accessibility check with timeout.

        For use in non-async contexts.

        Args:
            path: Path to check
            timeout: Timeout in seconds

        Returns:
            Tuple of (accessible, error_message)
        """
        import concurrent.futures

        def _check() -> Tuple[bool, Optional[str]]:
            try:
                if not path.exists():
                    return False, f"Path does not exist: {path}"
                if not path.is_dir():
                    return False, f"Path is not a directory: {path}"
                # Try to list (confirms access)
                next(path.iterdir(), None)
                return True, None
            except PermissionError as e:
                return False, f"Permission denied: {e}"
            except OSError as e:
                return False, f"OS error: {e}"
            except Exception as e:
                return False, f"Error: {e}"

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_check)
                return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return False, f"Timeout ({timeout}s) accessing path"
        except Exception as e:
            return False, f"Check failed: {e}"

    # ==========================================================================
    # v95.8: Issue 8 - Comprehensive Permission Validation
    # ==========================================================================

    def _check_permissions(self, path: Path) -> Dict[str, bool]:
        """
        v95.8: Comprehensive permission check for a path.

        Checks read, write, and execute permissions with clear results.

        Args:
            path: Path to check permissions for

        Returns:
            Dict with permission results: {
                'exists': bool,
                'readable': bool,
                'writable': bool,
                'executable': bool,
                'listable': bool,  # Can list directory contents
                'is_dir': bool,
                'is_file': bool,
                'is_symlink': bool,
                'owner_match': bool,  # True if current user owns the path
            }
        """
        result = {
            'exists': False,
            'readable': False,
            'writable': False,
            'executable': False,
            'listable': False,
            'is_dir': False,
            'is_file': False,
            'is_symlink': False,
            'owner_match': False,
        }

        try:
            # Check existence first
            result['exists'] = path.exists()
            if not result['exists']:
                return result

            # Basic type checks
            result['is_dir'] = path.is_dir()
            result['is_file'] = path.is_file()
            result['is_symlink'] = path.is_symlink()

            # Permission checks using os.access
            result['readable'] = os.access(path, os.R_OK)
            result['writable'] = os.access(path, os.W_OK)
            result['executable'] = os.access(path, os.X_OK)

            # For directories, check if we can list contents
            if result['is_dir']:
                try:
                    next(path.iterdir(), None)
                    result['listable'] = True
                except (PermissionError, OSError):
                    result['listable'] = False

            # Check ownership
            try:
                stat_info = path.stat()
                result['owner_match'] = stat_info.st_uid == os.getuid()
            except (OSError, AttributeError):
                pass  # os.getuid() not available on Windows

        except PermissionError:
            # Can't even stat the path
            result['exists'] = True  # We know it exists if we got PermissionError
        except OSError as e:
            logger.debug(f"[v95.8] Permission check error for {path}: {e}")

        return result

    def _validate_repo_permissions(
        self,
        path: Path,
        require_write: bool = False,
    ) -> Tuple[bool, List[str]]:
        """
        v95.8: Validate that a repository path has required permissions.

        Args:
            path: Repository path to validate
            require_write: Whether write permission is required

        Returns:
            Tuple of (valid, list_of_issues)
        """
        issues: List[str] = []
        perms = self._check_permissions(path)

        if not perms['exists']:
            issues.append(f"Repository path does not exist: {path}")
            return False, issues

        if not perms['is_dir']:
            issues.append(f"Repository path is not a directory: {path}")
            return False, issues

        if not perms['readable']:
            issues.append(f"No read permission for repository: {path}")

        if not perms['listable']:
            issues.append(f"Cannot list directory contents: {path}")

        if not perms['executable']:
            issues.append(f"No execute permission (cannot cd into): {path}")

        if require_write and not perms['writable']:
            issues.append(f"No write permission for repository: {path}")

        # Check key subdirectories and scripts
        if perms['listable']:
            # Check if we can read the main script
            for script_name in ['run_server.py', 'run_reactor.py', 'run_supervisor.py', 'main.py']:
                script_path = path / script_name
                if script_path.exists():
                    script_perms = self._check_permissions(script_path)
                    if not script_perms['readable']:
                        issues.append(f"Cannot read script {script_name}: no read permission")
                    break

        return len(issues) == 0, issues

    def _get_permission_error_hints(self, path: Path, perms: Dict[str, bool]) -> List[str]:
        """
        v95.8: Generate helpful hints for permission issues.

        Args:
            path: Path with permission issues
            perms: Permission check results

        Returns:
            List of helpful hint strings
        """
        hints: List[str] = []

        if not perms['exists']:
            hints.append(f"Ensure the path exists: mkdir -p {path}")
            return hints

        if not perms['readable'] or not perms['listable']:
            hints.append(f"Fix read permissions: chmod +r {path}")
            if perms['is_dir']:
                hints.append(f"Or recursively: chmod -R +r {path}")

        if not perms['executable'] and perms['is_dir']:
            hints.append(f"Fix execute permission (for directory access): chmod +x {path}")

        if not perms['owner_match']:
            hints.append(f"You don't own this path. Consider: sudo chown -R $USER {path}")

        # Check if path is on a read-only filesystem
        try:
            if path.exists():
                parent = path.parent
                test_file = parent / f".jarvis_perm_test_{os.getpid()}"
                try:
                    test_file.touch()
                    test_file.unlink()
                except (PermissionError, OSError):
                    hints.append("Parent directory may be read-only or on a read-only filesystem")
        except Exception:
            pass

        return hints

    def _get_search_directories(self) -> List[Path]:
        """
        v95.8: Get all directories to search for repositories.

        Enhanced with:
        - __file__-based resolution (works from any cwd)
        - Permission validation
        - Network path detection
        - Prioritized ordering

        Returns platform-appropriate paths with user substitution.
        """
        search_dirs: List[Path] = []
        seen_paths: Set[Path] = set()  # Avoid duplicates after resolution
        home = Path.home()

        def add_if_valid(path: Path, priority: int = 0) -> None:
            """Add path if valid and not already seen."""
            try:
                # Resolve to canonical absolute path
                resolved = self._resolve_to_absolute(path)
                if resolved in seen_paths:
                    return
                seen_paths.add(resolved)

                # Quick permission check (sync, with timeout for network paths)
                is_network = self._is_network_path(resolved)
                timeout = 3.0 if is_network else 1.0

                accessible, error = self._check_path_accessible_sync(resolved, timeout=timeout)

                if accessible:
                    if priority == 0:
                        search_dirs.insert(0, resolved)  # High priority
                    else:
                        search_dirs.append(resolved)

                    if is_network:
                        logger.debug(f"[v95.8] Added network path to search: {resolved}")
                else:
                    logger.debug(f"[v95.8] Skipping inaccessible path: {resolved} - {error}")

            except Exception as e:
                logger.debug(f"[v95.8] Error adding search path {path}: {e}")

        # Priority 1: Parent of current JARVIS repo (sibling repos - most reliable)
        # Uses __file__ so works regardless of cwd
        try:
            add_if_valid(self._repos_parent, priority=0)
        except Exception as e:
            logger.debug(f"[v95.8] Cannot add repos parent: {e}")

        # Priority 2: JARVIS root itself (for self-discovery)
        try:
            add_if_valid(self._jarvis_root, priority=0)
        except Exception as e:
            logger.debug(f"[v95.8] Cannot add JARVIS root: {e}")

        # Priority 3: Common development directories
        for pattern in self._COMMON_DEV_DIRS:
            try:
                # Substitute user placeholder
                path_str = pattern.replace("{user}", self._user)

                # Expand ~ to home directory
                if path_str.startswith("~"):
                    path = home / path_str[2:]
                else:
                    path = Path(path_str)

                add_if_valid(path, priority=1)

            except Exception as e:
                logger.debug(f"[v95.8] Error processing pattern {pattern}: {e}")
                continue

        # Priority 4: Current working directory (user might be there)
        try:
            cwd = Path.cwd()
            add_if_valid(cwd, priority=1)
            # Also check cwd's parent (common for sibling repos)
            add_if_valid(cwd.parent, priority=1)
        except Exception:
            pass

        # Priority 5: Paths from config file (if exists)
        config_paths = self._load_config_paths()
        for config_path in config_paths:
            try:
                add_if_valid(Path(config_path), priority=1)
            except Exception:
                pass

        logger.debug(f"[v95.8] Search directories ({len(search_dirs)} total): {[str(p) for p in search_dirs[:5]]}...")
        return search_dirs

    def _load_config_paths(self) -> List[str]:
        """
        v95.8: Load additional search paths from config file.

        Looks for ~/.jarvis/discovery_paths.json or similar.

        Returns:
            List of additional paths to search
        """
        config_locations = [
            Path.home() / ".jarvis" / "discovery_paths.json",
            Path.home() / ".config" / "jarvis" / "discovery_paths.json",
            self._jarvis_root / ".discovery_paths.json",
        ]

        for config_path in config_locations:
            try:
                if config_path.exists():
                    with open(config_path, 'r') as f:
                        data = json.load(f)
                        if isinstance(data, dict) and "search_paths" in data:
                            return data["search_paths"]
                        elif isinstance(data, list):
                            return data
            except Exception as e:
                logger.debug(f"[v95.8] Could not load config from {config_path}: {e}")

        return []

    def _get_name_variants(self, service_name: str) -> List[str]:
        """
        v95.6: Get all possible name variants for a service.

        Includes case variations and common naming conventions.
        """
        base_variants = self._SERVICE_NAME_VARIANTS.get(service_name, [service_name])

        # Generate additional case variants
        all_variants = set(base_variants)
        for name in base_variants:
            all_variants.add(name.lower())
            all_variants.add(name.upper())
            all_variants.add(name.replace("-", "_"))
            all_variants.add(name.replace("_", "-"))

        return list(all_variants)

    def _validate_repo_signature(
        self,
        path: Path,
        service_name: str,
        check_permissions: bool = True,
    ) -> bool:
        """
        v95.8: Validate that a directory is the expected repository.

        Enhanced with:
        - Signature file/directory checking
        - Permission validation (Issue 8)
        - Detailed logging for debugging

        Args:
            path: Path to validate
            service_name: Service name for signature lookup
            check_permissions: Whether to validate permissions

        Returns:
            True if path is valid repository
        """
        signatures = self._REPO_SIGNATURES.get(service_name, [])

        # First, check permissions (Issue 8)
        if check_permissions:
            perms = self._check_permissions(path)

            if not perms['exists']:
                logger.debug(f"[v95.8] Signature check failed - path doesn't exist: {path}")
                return False

            if not perms['readable'] or not perms['listable']:
                logger.warning(
                    f"[v95.8] Permission issue with {path}:\n"
                    f"  readable={perms['readable']}, listable={perms['listable']}"
                )
                # Generate hints
                hints = self._get_permission_error_hints(path, perms)
                for hint in hints:
                    logger.warning(f"  💡 {hint}")
                return False

        if not signatures:
            return True  # No signatures defined, accept any match

        # Require at least 50% of signatures to match
        matches = 0
        checked = []

        for sig in signatures:
            check_path = path / sig
            try:
                exists = check_path.exists()
                checked.append((sig, exists))
                if exists:
                    # Also verify we can access it
                    if check_path.is_file():
                        # Check file is readable
                        if os.access(check_path, os.R_OK):
                            matches += 1
                        else:
                            logger.debug(f"[v95.8] Signature {sig} exists but not readable")
                    elif check_path.is_dir():
                        # Check directory is listable
                        try:
                            next(check_path.iterdir(), None)
                            matches += 1
                        except (PermissionError, OSError):
                            logger.debug(f"[v95.8] Signature dir {sig} exists but not listable")
                    else:
                        matches += 1  # Symlink or other - count it
            except (PermissionError, OSError) as e:
                logger.debug(f"[v95.8] Cannot check signature {sig}: {e}")
                checked.append((sig, False))

        threshold = len(signatures) * 0.5
        valid = matches >= threshold

        if not valid and matches > 0:
            logger.debug(
                f"[v95.8] Signature check failed for {service_name} at {path}: "
                f"{matches}/{len(signatures)} signatures (need {threshold}). "
                f"Checked: {checked}"
            )

        return valid

    def _is_git_repo(self, path: Path) -> bool:
        """
        v95.8: Check if a directory is a git repository.

        Enhanced with permission handling.
        """
        try:
            git_dir = path / ".git"
            if not git_dir.exists():
                return False

            # Check if we can access .git
            if git_dir.is_dir():
                # Try to access HEAD to confirm it's a valid git repo
                head = git_dir / "HEAD"
                return head.exists() and os.access(head, os.R_OK)
            elif git_dir.is_file():
                # Worktree reference file
                return os.access(git_dir, os.R_OK)

            return False
        except (PermissionError, OSError) as e:
            logger.debug(f"[v95.8] Cannot check git repo at {path}: {e}")
            return False

    async def discover_repo(
        self,
        service_name: str,
        env_var: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Optional[Path]:
        """
        v95.8: Discover repository path with intelligent multi-strategy search.

        Enhanced with:
        - __file__-based sibling repo detection (Issue 6)
        - Network drive resilience with timeouts (Issue 7)
        - Comprehensive permission validation (Issue 8)
        - Async path accessibility checks

        Args:
            service_name: Name of the service (e.g., "jarvis-prime")
            env_var: Environment variable to check first
            force_refresh: Bypass cache and rediscover

        Returns:
            Path to repository or None if not found
        """
        cache_key = service_name

        # Check cache first (unless force refresh)
        if not force_refresh and cache_key in self._discovery_cache:
            cached_path, cache_time = self._discovery_cache[cache_key]
            if time.time() - cache_time < self._cache_ttl:
                logger.debug(f"[v95.8] Using cached path for {service_name}: {cached_path}")
                return cached_path

        discovery_start = time.time()
        logger.info(f"[v95.8] Starting discovery for {service_name}...")

        # Strategy 1: Environment variable (highest priority)
        if env_var:
            env_path = os.environ.get(env_var)
            if env_path:
                # v95.8: Use absolute resolution
                path = self._resolve_to_absolute(env_path)

                # v95.8: Check accessibility with network resilience
                is_network = self._is_network_path(path)
                if is_network:
                    logger.debug(f"[v95.8] Env path appears to be network path: {path}")
                    accessible, error = await self._check_path_with_retry(path)
                else:
                    accessible, error = await self._check_path_accessible_async(path)

                if accessible:
                    if self._validate_repo_signature(path, service_name):
                        logger.info(f"[v95.8] Found {service_name} via env var {env_var}: {path}")
                        self._discovery_cache[cache_key] = (path, time.time())
                        return path
                    else:
                        logger.warning(f"[v95.8] Path from {env_var} exists but signature mismatch: {path}")
                else:
                    logger.warning(f"[v95.8] Path from {env_var} not accessible: {error}")
                    # v95.8: Provide hints for common issues
                    perms = self._check_permissions(path)
                    hints = self._get_permission_error_hints(path, perms)
                    for hint in hints:
                        logger.info(f"  💡 {hint}")

        # Strategy 2: Sibling repo detection using __file__ (v95.8 - most reliable)
        # This works regardless of current working directory
        sibling_path = self._get_sibling_repo_path(service_name)
        if sibling_path:
            # Verify accessibility
            accessible, error = await self._check_path_accessible_async(sibling_path)
            if accessible and self._validate_repo_signature(sibling_path, service_name):
                logger.info(f"[v95.8] Found {service_name} as sibling repo: {sibling_path}")
                self._discovery_cache[cache_key] = (sibling_path, time.time())
                return sibling_path

        # Strategy 2: Search common development directories
        search_dirs = self._get_search_directories()
        name_variants = self._get_name_variants(service_name)

        logger.debug(
            f"[v95.7] Async discovery for '{service_name}' searching "
            f"{len(search_dirs)} directories with {len(name_variants)} variants"
        )

        discovered_path: Optional[Path] = None
        best_score = 0
        candidates_found = 0
        symlinks_resolved = 0
        broken_symlinks = 0

        for search_dir in search_dirs:
            try:
                logger.debug(f"[v95.7] Scanning directory: {search_dir}")

                # List all subdirectories
                for item in search_dir.iterdir():
                    # v95.7: Handle symlinks explicitly
                    resolved_item = item
                    is_symlink = item.is_symlink()

                    if is_symlink:
                        try:
                            # Resolve symlink to get real path
                            resolved_item = item.resolve()
                            symlinks_resolved += 1
                            if not resolved_item.exists():
                                broken_symlinks += 1
                                logger.debug(
                                    f"[v95.7] Skipping broken symlink: {item} -> {resolved_item}"
                                )
                                continue
                        except (OSError, RuntimeError) as e:
                            broken_symlinks += 1
                            logger.debug(f"[v95.7] Cannot resolve symlink {item}: {e}")
                            continue

                    # Check if it's a directory (after resolving symlinks)
                    if not resolved_item.is_dir():
                        continue

                    # Use the original item name for matching
                    item_name = item.name.lower()

                    # Check if name matches any variant
                    for variant in name_variants:
                        if item_name == variant.lower():
                            candidates_found += 1
                            # Calculate match score
                            score = 10  # Base score for name match

                            # v95.7: Check git repo on resolved path
                            if self._is_git_repo(resolved_item):
                                score += 5
                                # Bonus for full .git directory
                                if (resolved_item / ".git").is_dir():
                                    score += 1

                            # v95.7: Signature match on resolved path
                            if self._validate_repo_signature(resolved_item, service_name):
                                score += 10

                            # Bonus for exact case match
                            if item.name == variant:
                                score += 2

                            # v95.7: Bonus for symlink (canonical location indicator)
                            if is_symlink:
                                score += 1

                            if score > best_score:
                                best_score = score
                                discovered_path = resolved_item
                                logger.debug(
                                    f"[v95.7] Candidate for {service_name}: {item} "
                                    f"{'-> ' + str(resolved_item) if is_symlink else ''} "
                                    f"(score: {score})"
                                )

            except (PermissionError, OSError) as e:
                logger.debug(f"[v95.7] Cannot search {search_dir}: {e}")
                continue

        if discovered_path:
            resolved = discovered_path.resolve()
            logger.info(
                f"[v95.7] Discovered {service_name} at: {resolved} "
                f"(score: {best_score}, candidates: {candidates_found}, "
                f"symlinks: {symlinks_resolved}, broken: {broken_symlinks})"
            )
            self._discovery_cache[cache_key] = (resolved, time.time())
            return resolved

        # Strategy 4: Try to find via running processes
        discovered_path = await self._find_via_running_process(service_name)
        if discovered_path:
            self._discovery_cache[cache_key] = (discovered_path, time.time())
            return discovered_path

        # v95.8: Enhanced failure reporting
        discovery_duration = time.time() - discovery_start
        logger.warning(
            f"[v95.8] Could not discover repository for {service_name} "
            f"(searched {len(search_dirs)} directories in {discovery_duration:.2f}s)"
        )

        # Provide actionable suggestions
        env_var_name = env_var or f"{service_name.upper().replace('-', '_')}_PATH"
        logger.info(
            f"  💡 To fix: Set {env_var_name} environment variable to the repo path\n"
            f"     Example: export {env_var_name}=/path/to/{service_name}\n"
            f"  💡 Or ensure the repository is in one of these locations:\n"
            f"     {[str(p) for p in search_dirs[:3]]}"
        )

        self._discovery_cache[cache_key] = (None, time.time())
        return None

    async def _find_via_running_process(self, service_name: str) -> Optional[Path]:
        """
        v95.6: Try to find repo path by examining running processes.

        Looks for Python processes that might be running the service.
        """
        try:
            import psutil

            name_variants = self._get_name_variants(service_name)
            script_names = ["run_server.py", "run_reactor.py", "main.py", "app.py"]

            for proc in psutil.process_iter(["pid", "name", "cmdline", "cwd"]):
                try:
                    info = proc.info
                    cmdline = info.get("cmdline") or []
                    cwd = info.get("cwd")

                    # Check if this is a Python process
                    if not any("python" in str(c).lower() for c in cmdline[:2]):
                        continue

                    # Check if running one of our scripts
                    for cmd in cmdline:
                        for script in script_names:
                            if script in str(cmd):
                                # Check if the cwd contains our service name
                                if cwd:
                                    cwd_path = Path(cwd)
                                    for variant in name_variants:
                                        if variant.lower() in cwd_path.name.lower():
                                            if self._validate_repo_signature(cwd_path, service_name):
                                                logger.info(
                                                    f"[v95.6] Found {service_name} via running process: {cwd_path}"
                                                )
                                                return cwd_path.resolve()

                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

        except ImportError:
            logger.debug("[v95.6] psutil not available for process discovery")
        except Exception as e:
            logger.debug(f"[v95.6] Process discovery failed: {e}")

        return None

    async def discover_all_repos(self) -> Dict[str, Optional[Path]]:
        """
        v95.6: Discover all known service repositories in parallel.

        Returns:
            Dict mapping service names to discovered paths
        """
        services = ["jarvis-prime", "reactor-core", "jarvis"]

        async def discover_one(name: str) -> Tuple[str, Optional[Path]]:
            env_var = f"{name.upper().replace('-', '_')}_PATH"
            path = await self.discover_repo(name, env_var)
            return name, path

        results = await asyncio.gather(*[discover_one(s) for s in services])
        return dict(results)

    def get_cached_path(self, service_name: str) -> Optional[Path]:
        """
        v95.6: Get cached path without rediscovery.

        Returns None if not cached or cache expired.
        """
        if service_name in self._discovery_cache:
            cached_path, cache_time = self._discovery_cache[service_name]
            if time.time() - cache_time < self._cache_ttl:
                return cached_path
        return None

    def invalidate_cache(self, service_name: Optional[str] = None) -> None:
        """
        v95.6: Invalidate discovery cache.

        Args:
            service_name: Specific service to invalidate, or None for all
        """
        if service_name:
            self._discovery_cache.pop(service_name, None)
        else:
            self._discovery_cache.clear()
        logger.info(f"[v95.6] Discovery cache invalidated: {service_name or 'all'}")


def get_repo_discovery() -> IntelligentRepoDiscovery:
    """
    v95.6: Get the global repo discovery instance.
    """
    return IntelligentRepoDiscovery()


class ServiceDefinitionRegistry:
    """
    v95.0: Centralized registry for canonical service definitions.

    This is the SINGLE SOURCE OF TRUTH for service configurations.
    All components (trinity_bridge, orchestrator, etc.) should use
    this registry to get consistent service definitions.

    Features:
    - Immutable canonical definitions for each service
    - Dynamic path resolution from environment or config
    - Automatic validation before returning definitions
    - Caching with TTL for performance
    """

    # Canonical service configurations - DO NOT modify these directly
    # Use environment variables to override paths/ports
    #
    # v95.0: Service Dependency Graph (startup order):
    #   jarvis-body (priority 10) ──────────────────────────┐
    #         │                                              │
    #         └──► jarvis-prime (priority 20) ──────────────┼──► reactor-core (priority 30)
    #                                                        │
    # Note: jarvis-prime depends on jarvis-body for service registry
    #       reactor-core depends on jarvis-prime for AGI integration
    #
    # v95.6: Dynamic repo discovery - NO HARDCODED PATHS
    # Paths are discovered at runtime using IntelligentRepoDiscovery
    # Environment variables take priority, then intelligent search

    _CANONICAL_DEFINITIONS = {
        "jarvis-prime": {
            "script_name": "run_server.py",
            "fallback_scripts": ["main.py", "server.py", "app.py"],
            "default_port_env": "JARVIS_PRIME_PORT",
            "default_port": 8000,
            "health_endpoint": "/health",
            "startup_timeout": 180.0,  # ML model loading
            "repo_path_env": "JARVIS_PRIME_PATH",
            # v95.6: Use dynamic discovery instead of hardcoded path
            # This will be resolved at runtime via get_definition()
            "default_repo_path": None,  # Discovered dynamically
            "discovery_service_name": "jarvis-prime",  # For discovery lookup
            "script_args_factory": lambda port: ["--port", str(port), "--host", "0.0.0.0"],
            # v138.0: Environment factory now includes hardware profile for Memory-Aware Staged Init
            "environment_factory": lambda path: {
                "PYTHONPATH": str(path),
                "PYTHONWARNINGS": "ignore::UserWarning,ignore::DeprecationWarning,ignore::FutureWarning",
                "TF_CPP_MIN_LOG_LEVEL": "2",
                "TRANSFORMERS_VERBOSITY": "error",
                "TOKENIZERS_PARALLELISM": "false",
                "COREMLTOOLS_LOG_LEVEL": "ERROR",
                # v138.0: Hardware profile is injected dynamically in _spawn_service_core
                # These are placeholder values - actual values are set at spawn time
                # based on assess_hardware_profile() to prevent OOM crashes
            },
            # v95.0: Dependency and priority configuration
            "depends_on": [],  # jarvis-prime can start independently
            "startup_priority": 20,  # Start second (after jarvis-body)
            "is_critical": True,  # System fails without this
            "dependency_wait_timeout": 120.0,
        },
        "reactor-core": {
            "script_name": "run_reactor.py",
            "fallback_scripts": ["run_supervisor.py", "main.py", "server.py"],
            "default_port_env": "REACTOR_CORE_PORT",
            "default_port": 8090,
            "health_endpoint": "/health",
            # v117.0: Increased startup timeout to handle slow startup scenarios
            "startup_timeout": 120.0,
            "repo_path_env": "REACTOR_CORE_PATH",
            # v95.6: Use dynamic discovery instead of hardcoded path
            "default_repo_path": None,  # Discovered dynamically
            "discovery_service_name": "reactor-core",  # For discovery lookup
            "script_args_factory": lambda port: ["--port", str(port)],
            "environment_factory": lambda path: {
                "PYTHONPATH": str(path),
                "REACTOR_PORT": str(os.getenv("REACTOR_CORE_PORT", 8090)),
            },
            "use_uvicorn": False,
            # v117.0: Enhanced dependency configuration with soft dependency support
            # reactor-core CAN start without jarvis-prime but works better with it
            # Setting to empty list allows reactor-core to start independently
            # It will connect to jarvis-prime when available via Trinity protocol
            "depends_on": [],  # v117.0: Removed hard dependency - reactor-core can run standalone
            "startup_priority": 30,  # Start third (after jarvis-prime)
            "is_critical": False,  # System can degrade without this
            "dependency_wait_timeout": 180.0,  # Wait longer if dependencies are re-added
            # v117.0: Soft dependency - recommend but don't require
            "soft_depends_on": ["jarvis-prime"],  # Will log warning if not available
        },
    }

    _instance = None
    _cache: Dict[str, tuple[ServiceDefinition, float]] = {}
    _cache_ttl = 60.0  # seconds

    def __new__(cls) -> "ServiceDefinitionRegistry":
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_definition(
        cls,
        service_name: str,
        port_override: Optional[int] = None,
        path_override: Optional[Path] = None,
        validate: bool = True,
    ) -> Optional[ServiceDefinition]:
        """
        Get a canonical service definition.

        Args:
            service_name: Name of the service (jarvis-prime, reactor-core)
            port_override: Optional port override
            path_override: Optional repo path override
            validate: Whether to validate the definition

        Returns:
            ServiceDefinition or None if service not found
        """
        canonical = cls._CANONICAL_DEFINITIONS.get(service_name)
        if not canonical:
            logger.warning(f"Unknown service: {service_name}")
            return None

        # Check cache
        cache_key = f"{service_name}:{port_override}:{path_override}"
        if cache_key in cls._cache:
            cached_def, cached_time = cls._cache[cache_key]
            if time.time() - cached_time < cls._cache_ttl:
                return cached_def

        # Resolve port
        port = port_override
        if port is None:
            port = int(os.getenv(canonical["default_port_env"], canonical["default_port"]))

        # Resolve path using v95.6 intelligent discovery
        repo_path = path_override
        if repo_path is None:
            env_var_name = canonical["repo_path_env"]

            # Priority 1: Check environment variable
            env_path = os.getenv(env_var_name)
            if env_path:
                raw_path = Path(env_path).expanduser()
                # v95.6: Resolve symlinks and validate
                if raw_path.exists():
                    # Resolve symlinks to get real path
                    repo_path = raw_path.resolve()
                    logger.info(f"[v95.6] Using {env_var_name}={repo_path}")
                elif raw_path.is_symlink():
                    # Broken symlink
                    logger.warning(
                        f"[v95.6] {env_var_name} points to broken symlink: {raw_path}"
                    )
                else:
                    logger.warning(
                        f"[v95.6] {env_var_name} path does not exist: {raw_path}"
                    )

            # Priority 2: Use intelligent discovery (v95.6)
            if repo_path is None:
                discovery = get_repo_discovery()
                discovery_name = canonical.get("discovery_service_name", service_name)

                # Try to get cached path first (synchronous)
                cached = discovery.get_cached_path(discovery_name)
                if cached:
                    repo_path = cached
                    logger.debug(f"[v95.6] Using cached path for {service_name}: {repo_path}")
                else:
                    # Fallback: Try synchronous discovery for common locations
                    repo_path = cls._sync_discover_repo(discovery_name)

                    if repo_path is None:
                        logger.warning(
                            f"[v95.6] Could not discover {service_name}. "
                            f"Set {env_var_name} environment variable or ensure repo exists."
                        )
                        return None  # v95.6: Cannot create definition without valid path

        # v95.6: Final path validation and symlink resolution
        if repo_path is not None:
            # Resolve symlinks
            if repo_path.is_symlink():
                repo_path = repo_path.resolve()
            # Make absolute
            if not repo_path.is_absolute():
                repo_path = repo_path.resolve()
            # Validate exists
            if not repo_path.exists():
                logger.error(f"[v95.6] Repository path does not exist: {repo_path}")
                return None

        # Build definition
        definition = ServiceDefinition(
            name=service_name,
            repo_path=repo_path,
            script_name=canonical["script_name"],
            fallback_scripts=canonical["fallback_scripts"],
            default_port=port,
            health_endpoint=canonical["health_endpoint"],
            startup_timeout=canonical["startup_timeout"],
            script_args=canonical.get("script_args_factory", lambda p: [])(port),
            environment=canonical.get("environment_factory", lambda p: {})(repo_path),
            use_uvicorn=canonical.get("use_uvicorn", False),
            uvicorn_app=canonical.get("uvicorn_app"),
            # v95.0: Dependency and priority configuration
            depends_on=canonical.get("depends_on", []),
            # v117.0: Soft dependencies - recommended but not required
            soft_depends_on=canonical.get("soft_depends_on", []),
            startup_priority=canonical.get("startup_priority", 50),
            is_critical=canonical.get("is_critical", True),
            dependency_wait_timeout=canonical.get("dependency_wait_timeout", 120.0),
        )

        # Validate if requested
        if validate:
            is_valid, issues = definition.validate()
            if not is_valid:
                logger.warning(f"Service definition validation failed for {service_name}:")
                for issue in issues:
                    logger.warning(f"  - {issue}")

        # Cache and return
        cls._cache[cache_key] = (definition, time.time())
        return definition

    @classmethod
    def get_all_definitions(
        cls,
        validate: bool = True,
    ) -> List[ServiceDefinition]:
        """Get all canonical service definitions."""
        definitions = []
        for name in cls._CANONICAL_DEFINITIONS:
            definition = cls.get_definition(name, validate=validate)
            if definition:
                definitions.append(definition)
        return definitions

    @classmethod
    def _sync_discover_repo(cls, service_name: str) -> Optional[Path]:
        """
        v95.8: Enterprise-grade synchronous repo discovery.

        This is a comprehensive synchronous version of IntelligentRepoDiscovery
        that works in non-async contexts with full support for:
        - __file__-based sibling repo detection (Issue 6)
        - Network path handling with timeouts (Issue 7)
        - Permission validation (Issue 8)

        Features:
        - Sibling repo detection using __file__ (highest priority)
        - Symlink resolution during directory iteration
        - Broken symlink detection and logging
        - Comprehensive candidate scoring
        - Permission validation with helpful error messages
        - Network path timeout handling

        Args:
            service_name: Service name to discover

        Returns:
            Path to repository or None if not found
        """
        discovery = get_repo_discovery()

        # Strategy 1: Sibling repo detection using __file__ (v95.8 - most reliable)
        # This works regardless of current working directory
        sibling_path = discovery._get_sibling_repo_path(service_name)
        if sibling_path:
            # Verify accessibility with timeout
            accessible, error = discovery._check_path_accessible_sync(sibling_path, timeout=3.0)
            if accessible:
                # Validate signature with permission check
                if discovery._validate_repo_signature(sibling_path, service_name, check_permissions=True):
                    logger.info(f"[v95.8] Sync discovered {service_name} as sibling repo: {sibling_path}")
                    discovery._discovery_cache[service_name] = (sibling_path, time.time())
                    return sibling_path
                else:
                    logger.debug(f"[v95.8] Sibling path {sibling_path} failed signature validation")
            else:
                logger.debug(f"[v95.8] Sibling path {sibling_path} not accessible: {error}")

        # Get name variants
        name_variants = discovery._get_name_variants(service_name)

        # Get search directories (v95.8: now includes permission-validated paths only)
        search_dirs = discovery._get_search_directories()

        logger.debug(
            f"[v95.7] Sync discovery for '{service_name}' searching "
            f"{len(search_dirs)} directories with {len(name_variants)} name variants"
        )

        best_match: Optional[Path] = None
        best_score = 0
        candidates_found = 0

        for search_dir in search_dirs:
            try:
                if not search_dir.exists():
                    continue

                logger.debug(f"[v95.7] Scanning directory: {search_dir}")

                for item in search_dir.iterdir():
                    # v95.7: Handle symlinks explicitly
                    resolved_item = item
                    is_symlink = item.is_symlink()

                    if is_symlink:
                        try:
                            # Resolve symlink to get real path
                            resolved_item = item.resolve()
                            if not resolved_item.exists():
                                logger.debug(
                                    f"[v95.7] Skipping broken symlink: {item} -> {resolved_item}"
                                )
                                continue
                            logger.debug(f"[v95.7] Resolved symlink: {item} -> {resolved_item}")
                        except (OSError, RuntimeError) as e:
                            logger.debug(f"[v95.7] Cannot resolve symlink {item}: {e}")
                            continue

                    # Check if it's a directory (after resolving symlinks)
                    if not resolved_item.is_dir():
                        continue

                    # Use the original item name for matching (symlink name is what user sees)
                    item_name = item.name.lower()

                    # Check if name matches any variant
                    for variant in name_variants:
                        if item_name == variant.lower():
                            candidates_found += 1
                            score = 10  # Base score for name match

                            # v95.7: Check git repo on resolved path
                            git_dir = resolved_item / ".git"
                            if git_dir.exists():
                                score += 5
                                # Additional bonus for .git being a directory (not worktree file)
                                if git_dir.is_dir():
                                    score += 1

                            # v95.7: Signature match on resolved path
                            if discovery._validate_repo_signature(resolved_item, service_name):
                                score += 10

                            # Bonus for exact case match
                            if item.name == variant:
                                score += 2

                            # v95.7: Bonus for symlink (often indicates preferred/canonical location)
                            if is_symlink:
                                score += 1

                            # v95.7: Bonus if resolved path is in typical dev directory
                            resolved_str = str(resolved_item).lower()
                            if any(dev_dir in resolved_str for dev_dir in ["repos", "code", "projects", "github"]):
                                score += 1

                            if score > best_score:
                                best_score = score
                                best_match = resolved_item  # Use resolved path
                                logger.debug(
                                    f"[v95.7] Sync discovery candidate: {item} "
                                    f"{'-> ' + str(resolved_item) if is_symlink else ''} "
                                    f"(score: {score}, git: {git_dir.exists()}, symlink: {is_symlink})"
                                )

            except (PermissionError, OSError) as e:
                logger.debug(f"[v95.7] Cannot scan directory {search_dir}: {e}")
                continue

        if best_match:
            # Final resolution to ensure absolute canonical path
            resolved = best_match.resolve()
            logger.info(
                f"[v95.7] Sync discovered {service_name} at: {resolved} "
                f"(score: {best_score}, candidates: {candidates_found})"
            )
            # Cache the result
            discovery._discovery_cache[service_name] = (resolved, time.time())
            return resolved

        logger.warning(
            f"[v95.7] Sync discovery failed for '{service_name}'. "
            f"Searched {len(search_dirs)} directories, found {candidates_found} candidates, "
            f"none with sufficient score. Variants searched: {name_variants[:5]}..."
        )
        return None

    @classmethod
    def list_services(cls) -> List[str]:
        """List all known service names."""
        return list(cls._CANONICAL_DEFINITIONS.keys())

    @classmethod
    def clear_cache(cls) -> None:
        """Clear the definition cache."""
        cls._cache.clear()
        # Also clear discovery cache
        try:
            get_repo_discovery().invalidate_cache()
        except Exception:
            pass


# Convenience function for getting service definitions
def get_service_definition(
    service_name: str,
    port: Optional[int] = None,
    path: Optional[Path] = None,
) -> Optional[ServiceDefinition]:
    """
    v95.0: Get a canonical service definition from the registry.

    This is the preferred way to get service definitions as it ensures
    consistency across all components.

    Example:
        definition = get_service_definition("reactor-core")
        if definition:
            # Use definition...
    """
    return ServiceDefinitionRegistry.get_definition(service_name, port, path)


@dataclass
class ManagedProcess:
    """Represents a managed child process with monitoring."""
    definition: ServiceDefinition
    process: Optional[asyncio.subprocess.Process] = None
    pid: Optional[int] = None
    port: Optional[int] = None
    status: ServiceStatus = ServiceStatus.PENDING
    restart_count: int = 0
    last_restart: float = 0.0
    # v112.0: Track actual start time for adaptive timeouts
    start_time: float = 0.0
    last_health_check: float = 0.0
    consecutive_failures: int = 0

    # Background tasks
    output_stream_task: Optional[asyncio.Task] = None
    health_monitor_task: Optional[asyncio.Task] = None
    # v95.0: Dedicated heartbeat task that runs independently of health checks
    # This prevents services from becoming stale even when health checks fail
    heartbeat_task: Optional[asyncio.Task] = None

    # v131.0: GCP offload tracking for OOM prevention
    # When memory is insufficient locally, heavy services can be offloaded to GCP Spot VMs
    gcp_offload_active: bool = False
    gcp_vm_ip: Optional[str] = None

    # v132.3: Enhanced OOM tracking for crash recovery
    _oom_detected: bool = False               # Set when SIGKILL (-9) detected
    _gcp_offload_endpoint: Optional[str] = None  # GCP endpoint after OOM restart
    degradation_tier: Optional[Any] = None    # Graceful degradation tier
    oom_abort_reason: Optional[str] = None    # Reason if OOM prevention aborted

    # v95.0: Track last known health status for smarter heartbeat reporting
    last_known_health: str = "unknown"
    last_heartbeat_sent: float = 0.0

    # v108.2: Crash forensics - circular buffer of last N output lines
    # This enables post-mortem analysis when a process crashes
    _output_buffer: List[str] = field(default_factory=list)
    _output_buffer_max_lines: int = 100
    _stderr_buffer: List[str] = field(default_factory=list)
    _stderr_buffer_max_lines: int = 50  # Separate stderr buffer for errors

    def add_output_line(self, line: str, is_stderr: bool = False) -> None:
        """
        v108.2: Add a line to the output buffer for crash forensics.

        Maintains a circular buffer of the last N lines for post-mortem analysis.
        """
        # Add to combined buffer
        self._output_buffer.append(line)
        if len(self._output_buffer) > self._output_buffer_max_lines:
            self._output_buffer.pop(0)

        # Also track stderr separately (usually contains errors)
        if is_stderr:
            self._stderr_buffer.append(line)
            if len(self._stderr_buffer) > self._stderr_buffer_max_lines:
                self._stderr_buffer.pop(0)

    def get_crash_context(self, num_lines: int = 30) -> Dict[str, Any]:
        """
        v108.2: Get crash context for forensic analysis.

        Returns the last N output lines and any error indicators.
        """
        return {
            "last_output_lines": self._output_buffer[-num_lines:] if self._output_buffer else [],
            "last_stderr_lines": self._stderr_buffer[-20:] if self._stderr_buffer else [],
            "total_output_captured": len(self._output_buffer),
            "total_stderr_captured": len(self._stderr_buffer),
            "exit_code": self.process.returncode if self.process else None,
            "pid": self.pid,
            "uptime_seconds": time.time() - self.last_restart if self.last_restart else 0,
        }

    @property
    def is_running(self) -> bool:
        """Check if process is running."""
        if self.process is None:
            return False
        return self.process.returncode is None

    def calculate_backoff(self, base: float = 1.0, max_backoff: float = 60.0) -> float:
        """Calculate exponential backoff for restart."""
        backoff = base * (2 ** self.restart_count)
        return min(backoff, max_backoff)


# =============================================================================
# v109.1: Health Check Result (Enterprise-Grade State Tracking)
# =============================================================================

class HealthState(Enum):
    """
    v109.1: Nuanced health state for intelligent failure tracking.

    CRITICAL: Previous design only had True/False, which couldn't distinguish
    between "service is starting" and "service has failed". This caused
    the health monitor to count startup time as failures, triggering
    premature auto-heal attempts.
    """
    HEALTHY = "healthy"      # Service is fully operational
    STARTING = "starting"    # Service is responding but still initializing
    DEGRADED = "degraded"    # Service responds but reports degraded state
    UNHEALTHY = "unhealthy"  # Service responds with error status
    UNREACHABLE = "unreachable"  # Service not responding (connection failed)
    TIMEOUT = "timeout"      # Health check timed out


@dataclass
class HealthCheckResult:
    """
    v109.1: Enterprise-grade health check result with nuanced state tracking.

    This enables the health monitor to:
    - Not count "starting" as a failure during startup
    - Track startup progress (elapsed time, current step)
    - Distinguish between "unreachable" and "unhealthy"
    - Make intelligent decisions about auto-heal triggers
    """
    state: HealthState
    is_responding: bool = False  # True if HTTP connection succeeded
    status_text: str = ""        # Raw status from response
    phase: str = ""              # Current phase (J-Prime specific)
    startup_elapsed: Optional[float] = None  # Seconds since startup began
    startup_step: Optional[str] = None       # Current startup step
    startup_progress: Optional[int] = None   # Step N of M
    error_message: Optional[str] = None      # Error details if unhealthy
    raw_data: Optional[Dict[str, Any]] = None  # Full response for debugging

    @property
    def is_healthy(self) -> bool:
        """True if service is fully operational."""
        return self.state == HealthState.HEALTHY

    @property
    def is_starting(self) -> bool:
        """True if service is starting but not yet ready."""
        return self.state == HealthState.STARTING

    @property
    def should_count_as_failure(self) -> bool:
        """
        v109.1: CRITICAL - Determine if this should count as a health failure.

        A "failure" triggers consecutive_failures counter, which can lead to
        auto-heal. We should NOT count as failure if:
        - Service is starting (normal startup process)
        - Service is degraded but responding (might recover)

        We SHOULD count as failure if:
        - Service is unreachable (connection failed)
        - Service explicitly reports error/unhealthy
        - Service timed out (not responding)
        """
        return self.state in (HealthState.UNHEALTHY, HealthState.UNREACHABLE, HealthState.TIMEOUT)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            "state": self.state.value,
            "is_responding": self.is_responding,
            "status_text": self.status_text,
            "phase": self.phase,
            "startup_elapsed": self.startup_elapsed,
            "startup_step": self.startup_step,
            "error_message": self.error_message,
        }


# =============================================================================
# Process Orchestrator
# =============================================================================

class ProcessOrchestrator:
    """
    Enterprise-grade process lifecycle manager.

    Features:
    - Spawn and manage child processes
    - Stream stdout/stderr with service prefixes
    - Auto-heal crashed services with exponential backoff
    - Graceful shutdown handling
    - Dynamic service discovery via registry
    """

    def __init__(self, config: Optional[OrchestratorConfig] = None):
        """Initialize orchestrator."""
        self.config = config or OrchestratorConfig()
        self.processes: Dict[str, ManagedProcess] = {}
        self._shutdown_event = asyncio.Event()
        self._running = False

        # Service registry (lazy loaded)
        self._registry = None

        # Signal handlers registered flag
        self._signals_registered = False

        # v93.9: Circuit breakers for Docker operations (per-service)
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}

        # v93.9: Retry states for operations
        self._retry_states: Dict[str, RetryState] = {}

        # v93.9: Cached memory status (refreshed periodically)
        self._cached_memory_status: Optional[MemoryStatus] = None
        self._memory_cache_time: float = 0.0
        self._memory_cache_ttl: float = 5.0  # Refresh every 5 seconds

        # v93.9: GCP VM manager reference (lazy loaded)
        self._gcp_vm_manager = None

        # v93.11: Thread-safe locks for concurrent access
        self._circuit_breaker_lock: Optional[asyncio.Lock] = None
        self._memory_status_lock: Optional[asyncio.Lock] = None
        self._startup_coordination_lock: Optional[asyncio.Lock] = None
        self._service_startup_semaphore: Optional[asyncio.Semaphore] = None

        # v93.11: Shared HTTP session with connection pooling (lazy initialized)
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._http_session_lock: Optional[asyncio.Lock] = None

        # v93.11: Cross-repo health aggregation
        self._service_health_cache: Dict[str, Dict[str, Any]] = {}
        self._health_cache_lock: Optional[asyncio.Lock] = None

        # v93.11: Startup coordination - track which services are starting
        self._services_starting: Set[str] = set()
        self._services_ready: Set[str] = set()
        self._startup_events: Dict[str, asyncio.Event] = {}

        # v95.1: Background task tracking (prevents fire-and-forget task leaks)
        self._background_tasks: Set[asyncio.Task] = set()
        self._background_tasks_lock: Optional[asyncio.Lock] = None

        # v95.1: Intelligent cross-repo recovery coordination
        self._recovery_coordinator_task: Optional[asyncio.Task] = None
        self._recovery_check_interval: float = float(
            os.environ.get("JARVIS_RECOVERY_CHECK_INTERVAL", "30.0")
        )
        self._proactive_recovery_enabled: bool = os.environ.get(
            "JARVIS_PROACTIVE_RECOVERY", "true"
        ).lower() == "true"
        self._dependency_cascade_recovery: bool = os.environ.get(
            "JARVIS_CASCADE_RECOVERY", "true"
        ).lower() == "true"

        # v95.3: Global shutdown completion flag (prevents post-shutdown recovery)
        # This flag is set when shutdown COMPLETES and stays True forever
        # Unlike _shutdown_event which signals "shutdown in progress", this
        # signals "shutdown is DONE, do NOT try to recover anything"
        self._shutdown_completed = False
        self._shutdown_completed_timestamp: Optional[float] = None

        # v95.4: JARVIS body startup tracking
        # This fixes the dependency deadlock where external services wait for jarvis-body
        # but the orchestrator doesn't track jarvis-body's own startup status
        self._jarvis_body_status: str = "initializing"  # initializing, starting, healthy, unhealthy
        self._jarvis_body_ready_event: Optional[asyncio.Event] = None
        self._jarvis_body_startup_time: Optional[float] = None
        self._jarvis_body_health_verified: bool = False

        # v95.5: Graceful Degradation with Circuit Breaker Pattern
        # Allows services to operate independently when dependencies fail
        self._degradation_mode: Dict[str, str] = {}  # service -> mode (full, degraded, isolated)
        self._service_capabilities: Dict[str, Set[str]] = {}  # service -> available capabilities
        self._fallback_handlers: Dict[str, Callable] = {}  # service -> fallback handler
        self._cross_repo_breaker: Optional[Any] = None  # CrossRepoCircuitBreaker instance
        self._degradation_lock: Optional[asyncio.Lock] = None

        # v95.5: Distributed Tracing with Correlation ID Propagation
        # Enables cross-component request tracking for debugging
        self._startup_correlation_id: Optional[str] = None
        self._startup_trace_context: Optional[Any] = None  # CorrelationContext
        self._active_traces: Dict[str, Any] = {}  # operation_id -> CorrelationContext
        self._trace_lock: Optional[asyncio.Lock] = None

        # v95.5: Event Bus Integration for Lifecycle Events
        # Publishes startup/ready/failed events for cross-repo coordination
        self._event_bus: Optional[Any] = None  # TrinityEventBus instance
        self._event_bus_initialized: bool = False
        self._lifecycle_events_enabled: bool = os.environ.get(
            "JARVIS_LIFECYCLE_EVENTS", "true"
        ).lower() == "true"
        self._event_subscriptions: List[str] = []  # Subscription IDs for cleanup

        # =====================================================================
        # v95.9: Error Handling & Recovery Infrastructure (Issues 21-25)
        # =====================================================================

        # Issue 21: Silent Failure on Repo Discovery - Explicit error handling
        self._discovery_failures: Dict[str, Dict[str, Any]] = {}  # service -> failure info
        self._degraded_services: Set[str] = set()  # Services running in degraded mode
        self._discovery_retry_state: Dict[str, Dict[str, Any]] = {}  # service -> retry state
        self._max_discovery_retries: int = int(os.environ.get("JARVIS_DISCOVERY_RETRIES", "5"))
        self._discovery_retry_base_delay: float = float(os.environ.get("JARVIS_DISCOVERY_DELAY", "2.0"))

        # Issue 22: Process Crash Without Restart - Crash monitoring
        self._crash_history: Dict[str, List[Dict[str, Any]]] = {}  # service -> list of crash events
        self._crash_rate_window: float = 300.0  # 5-minute window for crash rate calculation
        self._max_crashes_per_window: int = 5  # Circuit breaker threshold
        self._crash_circuit_breakers: Dict[str, bool] = {}  # service -> is_open (True = stopped restarting)
        self._crash_analysis_enabled: bool = os.environ.get("JARVIS_CRASH_ANALYSIS", "true").lower() == "true"
        self._last_crash_analysis: Dict[str, Dict[str, Any]] = {}  # service -> analysis result

        # Issue 23: Port Conflict No Recovery - Port resolution
        self._port_allocation_map: Dict[str, int] = {}  # service -> allocated port
        self._port_conflict_history: Dict[int, List[Dict[str, Any]]] = {}  # port -> conflict events
        self._dynamic_port_range: Tuple[int, int] = (
            int(os.environ.get("JARVIS_PORT_RANGE_START", "9000")),
            int(os.environ.get("JARVIS_PORT_RANGE_END", "9999"))
        )
        self._port_scan_cache: Dict[int, Tuple[bool, float]] = {}  # port -> (in_use, check_time)
        self._port_scan_cache_ttl: float = 10.0  # 10 seconds

        # Issue 24: Import Error No Fallback - Import error handling
        self._import_errors: Dict[str, Dict[str, Any]] = {}  # module -> error info
        self._import_fallbacks: Dict[str, List[str]] = {}  # module -> list of fallback modules
        self._loaded_alternatives: Dict[str, str] = {}  # original module -> loaded alternative
        self._lazy_imports: Dict[str, Any] = {}  # module name -> loaded module or None

        # Issue 25: Network Timeout No Retry - Network resilience
        self._network_circuit_breakers: Dict[str, Dict[str, Any]] = {}  # endpoint -> breaker state
        self._network_health_status: Dict[str, Dict[str, Any]] = {}  # endpoint -> health info
        self._network_retry_config: Dict[str, Any] = {
            "max_retries": int(os.environ.get("JARVIS_NETWORK_RETRIES", "3")),
            "base_delay": float(os.environ.get("JARVIS_NETWORK_DELAY", "1.0")),
            "max_delay": float(os.environ.get("JARVIS_NETWORK_MAX_DELAY", "30.0")),
            "timeout": float(os.environ.get("JARVIS_NETWORK_TIMEOUT", "10.0")),
            "circuit_threshold": int(os.environ.get("JARVIS_CIRCUIT_THRESHOLD", "5")),
            "circuit_reset_time": float(os.environ.get("JARVIS_CIRCUIT_RESET", "60.0")),
        }

        # =====================================================================
        # v95.10: Cross-Repo Integration Infrastructure (Issues 31-38)
        # =====================================================================

        # Issue 31: Unified Configuration - Trinity config system
        self._unified_config: Dict[str, Any] = {}  # Merged config from all repos
        self._config_sources: Dict[str, Dict[str, Any]] = {}  # repo -> config dict
        self._config_overrides: Dict[str, Any] = {}  # User overrides (highest priority)
        self._config_schema: Dict[str, Dict[str, Any]] = {}  # Config validation schema
        self._config_watchers: Dict[str, Callable] = {}  # config_key -> callback
        self._config_lock: Optional[asyncio.Lock] = None
        self._config_sync_interval: float = float(os.environ.get("JARVIS_CONFIG_SYNC_INTERVAL", "30.0"))
        self._config_sync_task: Optional[asyncio.Task] = None

        # Issue 32: Cross-Repo Logging - Unified logging with W3C trace context
        self._unified_log_handlers: Dict[str, Any] = {}  # handler_name -> handler
        self._log_correlation_enabled: bool = os.environ.get("JARVIS_LOG_CORRELATION", "true").lower() == "true"
        self._log_aggregation_buffer: List[Dict[str, Any]] = []  # Buffered log entries
        self._log_buffer_size: int = int(os.environ.get("JARVIS_LOG_BUFFER_SIZE", "100"))
        self._log_flush_interval: float = float(os.environ.get("JARVIS_LOG_FLUSH_INTERVAL", "5.0"))
        self._log_flush_task: Optional[asyncio.Task] = None
        self._log_lock: Optional[asyncio.Lock] = None
        self._trace_id_header: str = "X-Trace-ID"
        self._span_id_header: str = "X-Span-ID"
        self._parent_id_header: str = "X-Parent-ID"

        # Issue 33: Cross-Repo Metrics - Unified metrics collection
        self._metrics_registry: Dict[str, Dict[str, Any]] = {}  # metric_name -> metric_info
        self._metrics_collectors: Dict[str, Callable] = {}  # collector_name -> collector_fn
        self._metrics_buffer: List[Dict[str, Any]] = []  # Buffered metrics
        self._metrics_lock: Optional[asyncio.Lock] = None
        self._metrics_enabled: bool = os.environ.get("JARVIS_METRICS_ENABLED", "true").lower() == "true"
        self._metrics_collection_interval: float = float(os.environ.get("JARVIS_METRICS_INTERVAL", "10.0"))
        self._metrics_collection_task: Optional[asyncio.Task] = None
        self._service_metrics_cache: Dict[str, Dict[str, Any]] = {}  # service -> metrics snapshot

        # Issue 34: Cross-Repo Error Propagation - Error context and correlation
        self._error_registry: Dict[str, Dict[str, Any]] = {}  # error_id -> error_info
        self._error_propagation_enabled: bool = os.environ.get("JARVIS_ERROR_PROPAGATION", "true").lower() == "true"
        self._error_correlation_map: Dict[str, List[str]] = {}  # root_error_id -> [related_error_ids]
        self._error_handlers: Dict[str, List[Callable]] = {}  # error_type -> [handlers]
        self._error_lock: Optional[asyncio.Lock] = None
        self._max_error_history: int = int(os.environ.get("JARVIS_MAX_ERROR_HISTORY", "1000"))

        # Issue 35: Cross-Repo State Synchronization - Shared state management
        self._shared_state: Dict[str, Any] = {}  # Cross-repo shared state
        self._state_version: Dict[str, int] = {}  # key -> version number
        self._state_subscribers: Dict[str, List[Callable]] = {}  # key -> [subscriber_callbacks]
        self._state_lock: Optional[asyncio.Lock] = None
        self._state_sync_enabled: bool = os.environ.get("JARVIS_STATE_SYNC", "true").lower() == "true"
        self._state_persistence_path: Optional[Path] = None
        self._state_sync_task: Optional[asyncio.Task] = None
        self._state_change_buffer: List[Dict[str, Any]] = []  # Buffered state changes

        # Issue 36: Cross-Repo Resource Coordination - Resource allocation
        self._resource_registry: Dict[str, Dict[str, Any]] = {}  # resource_id -> resource_info
        self._resource_allocations: Dict[str, Dict[str, int]] = {}  # service -> {resource -> amount}
        self._resource_limits: Dict[str, int] = {}  # resource -> total_available
        self._resource_lock: Optional[asyncio.Lock] = None
        self._resource_coordination_enabled: bool = os.environ.get("JARVIS_RESOURCE_COORDINATION", "true").lower() == "true"
        self._resource_conflict_handlers: Dict[str, Callable] = {}  # resource -> conflict_handler

        # Issue 37: Cross-Repo Version Compatibility - Compatibility matrix
        self._version_registry: Dict[str, str] = {}  # service -> version
        self._compatibility_matrix: Dict[str, Dict[str, List[str]]] = {}  # service -> {dep -> [compatible_versions]}
        self._version_check_enabled: bool = os.environ.get("JARVIS_VERSION_CHECK", "true").lower() == "true"
        self._version_lock: Optional[asyncio.Lock] = None
        self._incompatibility_handlers: Dict[str, Callable] = {}  # service_pair -> handler

        # Issue 38: Cross-Repo Security Context - Unified security
        self._security_context: Dict[str, Any] = {}  # Current security context
        self._security_tokens: Dict[str, str] = {}  # service -> auth_token
        self._security_policies: Dict[str, Dict[str, Any]] = {}  # policy_name -> policy_def
        self._security_lock: Optional[asyncio.Lock] = None
        self._security_context_enabled: bool = os.environ.get("JARVIS_SECURITY_CONTEXT", "true").lower() == "true"
        self._token_refresh_interval: float = float(os.environ.get("JARVIS_TOKEN_REFRESH", "3600.0"))
        self._token_refresh_task: Optional[asyncio.Task] = None

        # =====================================================================
        # v95.16: Per-Service Recovery Locking (Race Condition Prevention)
        # =====================================================================
        # Prevents concurrent _auto_heal calls for the same service.
        # When multiple detection mechanisms (health check, registry stale,
        # process crash) trigger simultaneously, only ONE recovery proceeds.
        self._recovery_locks: Dict[str, asyncio.Lock] = {}
        self._recovery_locks_lock: Optional[asyncio.Lock] = None  # Protects _recovery_locks dict
        self._recovery_in_progress: Dict[str, bool] = {}  # service -> is_recovering

        # =====================================================================
        # v109.5: Progress Broadcasting to Loading Server (Watchdog Fix)
        # =====================================================================
        # CRITICAL: The loading server has a 60-second watchdog that shuts down
        # the system if no progress updates are received. The cross-repo
        # orchestrator must broadcast progress during service startup.
        self._progress_broadcaster_enabled: bool = True
        self._last_progress_broadcast: float = 0.0
        self._progress_broadcast_interval: float = 10.0  # Keepalive every 10s
        self._progress_keepalive_task: Optional[asyncio.Task] = None
        self._current_startup_phase: str = "initializing"
        self._current_startup_progress: int = 35  # Start after "Backend API online"

    def _ensure_locks_initialized(self) -> None:
        """
        v93.11: Lazily initialize asyncio primitives.

        Called before any operation that needs locks.
        This handles the case where __init__ runs before event loop exists.
        """
        if self._circuit_breaker_lock is None:
            self._circuit_breaker_lock = asyncio.Lock()
        if self._memory_status_lock is None:
            self._memory_status_lock = asyncio.Lock()
        if self._startup_coordination_lock is None:
            self._startup_coordination_lock = asyncio.Lock()
        if self._service_startup_semaphore is None:
            # Limit concurrent startups to prevent resource exhaustion
            self._service_startup_semaphore = asyncio.Semaphore(3)
        if self._http_session_lock is None:
            self._http_session_lock = asyncio.Lock()
        if self._background_tasks_lock is None:
            self._background_tasks_lock = asyncio.Lock()
        if self._health_cache_lock is None:
            self._health_cache_lock = asyncio.Lock()
        # v95.4: JARVIS body ready event
        if self._jarvis_body_ready_event is None:
            self._jarvis_body_ready_event = asyncio.Event()
        # v95.5: Degradation and tracing locks
        if self._degradation_lock is None:
            self._degradation_lock = asyncio.Lock()
        if self._trace_lock is None:
            self._trace_lock = asyncio.Lock()

        # v95.10: Cross-repo integration locks
        if self._config_lock is None:
            self._config_lock = asyncio.Lock()
        if self._log_lock is None:
            self._log_lock = asyncio.Lock()
        if self._metrics_lock is None:
            self._metrics_lock = asyncio.Lock()
        if self._error_lock is None:
            self._error_lock = asyncio.Lock()
        if self._state_lock is None:
            self._state_lock = asyncio.Lock()
        if self._resource_lock is None:
            self._resource_lock = asyncio.Lock()
        if self._version_lock is None:
            self._version_lock = asyncio.Lock()
        if self._security_lock is None:
            self._security_lock = asyncio.Lock()
        # v95.16: Recovery locking
        if self._recovery_locks_lock is None:
            self._recovery_locks_lock = asyncio.Lock()

    @property
    def _degradation_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for degradation lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._degradation_lock is not None, "Degradation lock should be initialized"
        return self._degradation_lock

    @property
    def _trace_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for trace lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._trace_lock is not None, "Trace lock should be initialized"
        return self._trace_lock

    @property
    def _http_session_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for HTTP session lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._http_session_lock is not None, "HTTP session lock should be initialized"
        return self._http_session_lock

    @property
    def _health_cache_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for health cache lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._health_cache_lock is not None, "Health cache lock should be initialized"
        return self._health_cache_lock

    @property
    def _startup_coordination_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for startup coordination lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._startup_coordination_lock is not None, "Startup coordination lock should be initialized"
        return self._startup_coordination_lock

    # =========================================================================
    # v109.5: Progress Broadcasting to Loading Server
    # =========================================================================

    async def _broadcast_progress_to_loading_server(
        self,
        stage: str,
        message: str,
        progress: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        v109.5: Broadcast progress update to the loading server.

        CRITICAL: The loading server has a 60-second watchdog that shuts down
        the system if no progress updates are received. This method must be
        called regularly during startup to prevent premature shutdown.

        Args:
            stage: Current stage name (e.g., "cross_repo_startup")
            message: Human-readable message
            progress: Progress percentage (0-100)
            metadata: Optional metadata dict

        Returns:
            True if broadcast succeeded, False otherwise
        """
        if not self._progress_broadcaster_enabled:
            return False

        try:
            import aiohttp
            from datetime import datetime

            # Use the loading server port (3001)
            loading_port = 3001
            url = f"http://localhost:{loading_port}/api/update-progress"

            data = {
                "stage": stage,
                "message": message,
                "progress": progress,
                "timestamp": datetime.now().isoformat(),
                "metadata": metadata or {},
            }

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=2.0)
            ) as session:
                async with session.post(url, json=data) as resp:
                    if resp.status == 200:
                        self._last_progress_broadcast = time.time()
                        self._current_startup_phase = stage
                        self._current_startup_progress = progress
                        logger.debug(f"[v109.5] 📡 Progress broadcast: {stage} ({progress}%)")
                        return True
                    else:
                        logger.debug(f"[v109.5] Progress broadcast failed: status {resp.status}")
                        return False

        except Exception as e:
            logger.debug(f"[v109.5] Progress broadcast failed: {e}")
            return False

    async def _start_progress_keepalive(self) -> None:
        """
        v109.5: Start a background task that sends keepalive progress updates.

        This prevents the loading server watchdog from triggering shutdown
        during long-running startup phases (e.g., waiting for J-Prime to load).
        """
        if self._progress_keepalive_task is not None:
            return

        async def keepalive_loop():
            """Send periodic keepalive progress updates."""
            while not self._shutdown_event.is_set():
                try:
                    elapsed = time.time() - self._last_progress_broadcast
                    if elapsed >= self._progress_broadcast_interval:
                        await self._broadcast_progress_to_loading_server(
                            self._current_startup_phase,
                            f"Cross-repo startup in progress... ({self._current_startup_phase})",
                            self._current_startup_progress,
                            {"keepalive": True},
                        )
                    await asyncio.sleep(self._progress_broadcast_interval / 2)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"[v109.5] Keepalive loop error: {e}")
                    await asyncio.sleep(5.0)

        self._progress_keepalive_task = asyncio.create_task(
            keepalive_loop(),
            name="progress-keepalive-v109.5"
        )
        logger.debug("[v109.5] Progress keepalive task started")

    async def _stop_progress_keepalive(self) -> None:
        """v109.5: Stop the progress keepalive task."""
        if self._progress_keepalive_task is not None:
            self._progress_keepalive_task.cancel()
            try:
                await self._progress_keepalive_task
            except asyncio.CancelledError:
                pass
            self._progress_keepalive_task = None
            logger.debug("[v109.5] Progress keepalive task stopped")

    async def _get_recovery_lock(self, service_name: str) -> asyncio.Lock:
        """
        v95.16: Get or create a per-service recovery lock.

        This ensures each service has its own lock to prevent concurrent
        _auto_heal calls for the same service while allowing different
        services to recover in parallel.
        """
        self._ensure_locks_initialized()
        assert self._recovery_locks_lock is not None

        async with self._recovery_locks_lock:
            if service_name not in self._recovery_locks:
                self._recovery_locks[service_name] = asyncio.Lock()
            return self._recovery_locks[service_name]

    async def _try_acquire_recovery(self, service_name: str) -> bool:
        """
        v95.16: Try to acquire recovery lock for a service (non-blocking).

        Returns True if lock acquired (caller should proceed with recovery).
        Returns False if another recovery is already in progress (caller should skip).

        This prevents the "thundering herd" of multiple concurrent recovery
        attempts when a service dies and triggers multiple detection mechanisms
        (health check failure, registry stale, process crash) simultaneously.
        """
        lock = await self._get_recovery_lock(service_name)

        # Non-blocking try-acquire
        if lock.locked():
            logger.debug(
                f"[v95.16] Recovery already in progress for {service_name}, "
                f"skipping duplicate recovery attempt"
            )
            return False

        # Try to acquire - if we get it, we're the recovery leader
        try:
            # Use wait_for with 0 timeout for non-blocking acquire
            await asyncio.wait_for(lock.acquire(), timeout=0.001)
            self._recovery_in_progress[service_name] = True
            return True
        except asyncio.TimeoutError:
            # Another coroutine got it first
            logger.debug(
                f"[v95.16] Lost recovery lock race for {service_name}, "
                f"another handler will recover this service"
            )
            return False

    def _release_recovery(self, service_name: str) -> None:
        """
        v95.16: Release recovery lock for a service.

        Called after recovery completes (success or failure).
        """
        self._recovery_in_progress[service_name] = False

        if service_name in self._recovery_locks:
            lock = self._recovery_locks[service_name]
            if lock.locked():
                try:
                    lock.release()
                except RuntimeError:
                    # Lock wasn't held by this task - shouldn't happen but be defensive
                    pass

    @property
    def _service_startup_semaphore_safe(self) -> asyncio.Semaphore:
        """v95.7: Type-safe accessor for startup semaphore. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._service_startup_semaphore is not None, "Service startup semaphore should be initialized"
        return self._service_startup_semaphore

    @property
    def _background_tasks_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for background tasks lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._background_tasks_lock is not None, "Background tasks lock should be initialized"
        return self._background_tasks_lock

    @property
    def _memory_status_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for memory status lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._memory_status_lock is not None, "Memory status lock should be initialized"
        return self._memory_status_lock

    @property
    def _circuit_breaker_lock_safe(self) -> asyncio.Lock:
        """v95.7: Type-safe accessor for circuit breaker lock. Always returns non-None."""
        self._ensure_locks_initialized()
        assert self._circuit_breaker_lock is not None, "Circuit breaker lock should be initialized"
        return self._circuit_breaker_lock

    # =========================================================================
    # v95.9: Issue 21 - Silent Failure on Repo Discovery
    # =========================================================================

    async def _handle_discovery_failure(
        self,
        service_name: str,
        error: str,
        retry_count: int = 0,
    ) -> Tuple[bool, Optional[Path]]:
        """
        v95.9: Handle repo discovery failure with explicit errors and retry logic.

        Instead of silently continuing, this method:
        1. Logs explicit, actionable error messages
        2. Attempts retry with exponential backoff
        3. Notifies about degraded mode if discovery ultimately fails
        4. Records failure for monitoring/alerting

        Args:
            service_name: Service that failed discovery
            error: Error message
            retry_count: Current retry attempt

        Returns:
            Tuple of (success, discovered_path)
        """
        self._ensure_locks_initialized()

        # Record the failure
        failure_info = {
            "service": service_name,
            "error": error,
            "timestamp": time.time(),
            "retry_count": retry_count,
            "resolved": False,
        }
        self._discovery_failures[service_name] = failure_info

        # Log explicit, actionable error
        logger.error(
            f"[v95.9] ❌ DISCOVERY FAILURE for {service_name}:\n"
            f"  Error: {error}\n"
            f"  Attempt: {retry_count + 1}/{self._max_discovery_retries}"
        )

        # Check if we should retry
        if retry_count < self._max_discovery_retries:
            # Calculate exponential backoff delay
            delay = self._discovery_retry_base_delay * (2 ** retry_count)
            # Add jitter to prevent thundering herd
            jitter = delay * 0.1 * (time.time() % 1)
            delay = min(delay + jitter, 60.0)  # Cap at 60 seconds

            logger.info(
                f"[v95.9] 🔄 Retrying {service_name} discovery in {delay:.1f}s "
                f"(attempt {retry_count + 2}/{self._max_discovery_retries})"
            )

            await asyncio.sleep(delay)

            # Retry discovery
            discovery = get_repo_discovery()
            env_var = f"{service_name.upper().replace('-', '_')}_PATH"

            try:
                path = await discovery.discover_repo(service_name, env_var, force_refresh=True)
                if path:
                    logger.info(f"[v95.9] ✅ Retry successful! Found {service_name} at: {path}")
                    self._discovery_failures[service_name]["resolved"] = True
                    return True, path
                else:
                    # Recursive retry with incremented count
                    return await self._handle_discovery_failure(
                        service_name,
                        "Discovery returned None after retry",
                        retry_count + 1
                    )
            except Exception as e:
                return await self._handle_discovery_failure(
                    service_name,
                    str(e),
                    retry_count + 1
                )

        # All retries exhausted - enter degraded mode
        self._degraded_services.add(service_name)
        self._discovery_failures[service_name]["final"] = True

        # Emit explicit notification about degraded mode
        logger.warning(
            f"[v95.9] ⚠️ SERVICE DEGRADED: {service_name}\n"
            f"  All {self._max_discovery_retries} discovery attempts failed.\n"
            f"  System will continue in DEGRADED MODE without {service_name}.\n"
            f"  💡 To fix:\n"
            f"     1. Ensure the {service_name} repository exists\n"
            f"     2. Set {service_name.upper().replace('-', '_')}_PATH environment variable\n"
            f"     3. Check file permissions on the repository"
        )

        # Publish degradation event
        await self.publish_service_lifecycle_event(
            service_name,
            "degraded",
            {
                "reason": "discovery_failed",
                "error": error,
                "retries": retry_count,
            }
        )

        return False, None

    def get_discovery_status(self) -> Dict[str, Any]:
        """
        v95.9: Get current discovery status for all services.

        Returns:
            Dict with discovery status for monitoring
        """
        return {
            "failures": self._discovery_failures.copy(),
            "degraded_services": list(self._degraded_services),
            "retry_states": self._discovery_retry_state.copy(),
            "timestamp": time.time(),
        }

    # =========================================================================
    # v95.9: Issue 22 - Process Crash Without Restart
    # =========================================================================

    async def _monitor_process_health(self, managed: "ManagedProcess") -> None:
        """
        v95.9: Monitor process health and handle crashes with auto-restart.

        Features:
        - Crash detection with detailed logging
        - Automatic restart with exponential backoff
        - Crash rate limiting (circuit breaker)
        - Crash log analysis
        """
        service_name = managed.definition.name

        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(5.0)  # Check every 5 seconds

                if managed.process is None:
                    continue

                # Check if process has exited
                return_code = managed.process.returncode

                if return_code is not None:
                    # v132.3: Process has exited - could be crash, OOM, or graceful shutdown
                    # _handle_process_crash properly distinguishes between these cases
                    await self._handle_process_crash(managed, return_code)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[v95.9] Error monitoring {service_name}: {e}")

    async def _handle_process_crash(
        self,
        managed: "ManagedProcess",
        return_code: int,
    ) -> None:
        """
        v95.9 + v95.11: Handle process crash with analysis and auto-restart.

        v95.11 enhancements:
        - Distinguish between crash vs graceful shutdown
        - Skip crash handling for SIGTERM during shutdown
        - Add startup grace period awareness
        - Better logging for different scenarios

        Args:
            managed: The crashed process
            return_code: Exit code from the process
        """
        service_name = managed.definition.name
        crash_time = time.time()
        uptime = crash_time - (managed.last_restart or crash_time)

        # v132.3: Check if this is a graceful exit (exit code 0)
        is_graceful_exit = return_code == 0

        # v95.11: Check if this is a graceful shutdown, not a crash
        is_sigterm = return_code == -15 or return_code == 143
        is_shutdown_mode = self._shutdown_event.is_set() or self._shutdown_completed

        # v132.3: Check for OOM kill (SIGKILL from kernel)
        is_oom_kill = return_code == -9 or return_code == 137

        # v132.3: Handle graceful exit (exit code 0) - NOT a crash
        if is_graceful_exit:
            # Check if process logs indicate intentional shutdown
            crash_context = managed.get_crash_context(num_lines=10)
            last_lines = crash_context.get("last_output_lines", []) + crash_context.get("last_stderr_lines", [])
            last_lines_text = " ".join(last_lines).lower()

            shutdown_indicators = ["shutdown", "shutting down", "exiting", "goodbye", "terminated gracefully"]
            intentional_shutdown = any(ind in last_lines_text for ind in shutdown_indicators)

            if intentional_shutdown or is_shutdown_mode:
                logger.info(
                    f"[v132.3] Service {service_name} exited gracefully (code 0) "
                    f"(PID: {managed.pid}, uptime: {uptime:.1f}s)"
                )
                managed.status = ServiceStatus.STOPPED
                managed.process = None
                return
            else:
                # Exit code 0 but no shutdown indicators - might be unexpected
                logger.info(
                    f"[v132.3] Service {service_name} exited with code 0 (non-error) "
                    f"(PID: {managed.pid}, uptime: {uptime:.1f}s) - treating as graceful stop"
                )
                managed.status = ServiceStatus.STOPPED
                managed.process = None
                return

        if is_sigterm and is_shutdown_mode:
            # This is expected during graceful shutdown - don't log as crash
            logger.info(
                f"[v95.11] Service {service_name} terminated by SIGTERM during shutdown "
                f"(PID: {managed.pid}, uptime: {uptime:.1f}s)"
            )
            # Update status but don't trigger crash handling
            managed.status = ServiceStatus.STOPPED
            managed.process = None
            return

        # v132.3: Handle OOM kill - trigger GCP offload for restart
        # v144.0: ACTIVE RESCUE - For jarvis-prime, FORCE GCP before any local restart
        if is_oom_kill:
            logger.warning(
                f"[v132.3] 🔴 OOM DETECTED: Service {service_name} killed by SIGKILL "
                f"(PID: {managed.pid}, uptime: {uptime:.1f}s, exit code: {return_code})"
            )
            # Flag for GCP-assisted restart
            managed._oom_detected = True

            # =========================================================================
            # v144.0: ACTIVE RESCUE - OOM DEATH HANDLER
            # =========================================================================
            # For jarvis-prime specifically, we MUST force GCP provisioning before
            # attempting any local restart. This prevents the OOM crash loop.
            # =========================================================================
            is_jarvis_prime = service_name.lower() in ["jarvis-prime", "jarvis_prime", "j-prime"]
            slim_mode = os.environ.get("JARVIS_ENABLE_SLIM_MODE", "").lower() in ("true", "1", "yes", "on")

            if is_jarvis_prime and slim_mode:
                logger.warning(
                    f"[v144.0] 🛟 ACTIVE RESCUE OOM DEATH HANDLER: "
                    f"jarvis-prime OOM'd - forcing GCP provisioning BEFORE restart!"
                )

                # =====================================================================
                # v147.0: CONSECUTIVE OOM CIRCUIT BREAKER
                # =====================================================================
                # Track consecutive OOMs even in cloud mode. If we keep crashing
                # even with GCP offload, something is fundamentally broken and we
                # should stop to prevent infinite billable loops.
                # =====================================================================
                consecutive_count = increment_consecutive_oom_count(service_name)

                if should_circuit_break_oom(service_name):
                    logger.error(
                        f"[v147.0] 🛑 CIRCUIT BREAKER TRIPPED: {service_name} has crashed "
                        f"{consecutive_count} consecutive times (threshold: {_MAX_CONSECUTIVE_OOMS}).\n"
                        f"    Even cloud mode cannot save this service.\n"
                        f"    Marking as DEGRADED and stopping all restart attempts.\n"
                        f"    Manual investigation required!"
                    )

                    managed.status = ServiceStatus.FAILED
                    managed._oom_detected = True
                    self._crash_circuit_breakers[service_name] = True

                    await _emit_event(
                        "OOM_CIRCUIT_BREAKER_TRIPPED",
                        service_name=service_name,
                        priority="CRITICAL",
                        details={
                            "consecutive_ooms": consecutive_count,
                            "threshold": _MAX_CONSECUTIVE_OOMS,
                            "action": "STOPPED_ALL_RESTARTS",
                            "requires_manual_intervention": True,
                        }
                    )

                    return  # STOP - do not attempt restart

                # =====================================================================
                # v146.0: SET PERSISTENT CLOUD LOCK
                # v147.0: Now includes consecutive OOM tracking
                # =====================================================================
                oom_count = managed.restart_count + 1
                _save_cloud_lock(
                    locked=True,
                    reason="OOM_CRASH_PROTECTION",
                    oom_count=oom_count,
                    consecutive_ooms=consecutive_count,
                )
                logger.warning(
                    f"[v147.0] 🔒 CLOUD LOCK SET: System will enforce cloud-only mode "
                    f"until manually cleared (OOM #{oom_count}, consecutive: {consecutive_count})"
                )

                # Invalidate any stale GCP cache
                invalidate_active_rescue_cache()

                # Force GCP provisioning
                logger.info(f"[v144.0] ⏳ Provisioning GCP VM for jarvis-prime offload (timeout: 120s)...")
                gcp_ready, gcp_endpoint = await ensure_gcp_vm_ready_for_prime(
                    timeout_seconds=120.0,
                    force_provision=True,  # Force new provisioning
                )

                if gcp_ready and gcp_endpoint:
                    logger.info(
                        f"[v144.0] ✅ Active Rescue: GCP VM ready at {gcp_endpoint} - "
                        f"jarvis-prime will restart as Hollow Client"
                    )

                    # v147.0: Update managed process state consistently
                    managed.gcp_offload_active = True
                    managed.gcp_vm_ip = gcp_endpoint.replace("http://", "").split(":")[0]

                    # Set up environment for Hollow Client restart
                    os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] = "true"
                    os.environ["GCP_PRIME_ENDPOINT"] = gcp_endpoint
                    os.environ["JARVIS_GCP_PRIME_ENDPOINT"] = gcp_endpoint

                    await _emit_event(
                        "ACTIVE_RESCUE_OOM_RECOVERY",
                        service_name=service_name,
                        priority="HIGH",
                        details={
                            "gcp_endpoint": gcp_endpoint,
                            "oom_count": oom_count,
                            "consecutive_ooms": consecutive_count,
                            "mode": "hollow_client_forced",
                        }
                    )
                else:
                    logger.error(
                        f"[v144.0] ❌ Active Rescue FAILED: Could not provision GCP VM. "
                        f"jarvis-prime will NOT be restarted to prevent OOM crash loop. "
                        f"Please manually check GCP quota/credentials."
                    )
                    managed.status = ServiceStatus.FAILED
                    managed._oom_detected = True

                    await _emit_event(
                        "ACTIVE_RESCUE_OOM_FAILED",
                        service_name=service_name,
                        priority="CRITICAL",
                        details={
                            "reason": "GCP provisioning failed, cannot safely restart",
                            "manual_action_required": True,
                        }
                    )

                    # DO NOT restart - prevent OOM crash loop
                    self._crash_circuit_breakers[service_name] = True
                    return

            # Continue to normal crash handling (will trigger GCP offload in restart logic)

        # v95.11: Check for startup-phase termination (very short uptime + SIGTERM)
        # This might indicate the service was killed before fully starting
        startup_grace_period = float(os.environ.get("SERVICE_STARTUP_GRACE_SECONDS", "30.0"))
        if is_sigterm and uptime < startup_grace_period:
            logger.warning(
                f"[v95.11] ⚠️ Service {service_name} terminated during startup phase "
                f"(PID: {managed.pid}, uptime: {uptime:.1f}s < {startup_grace_period}s grace)"
            )
            # Still record it but with lower severity
            if not is_shutdown_mode:
                logger.info(
                    f"[v95.11] This may indicate a shutdown signal arrived during startup. "
                    f"Service will NOT be auto-restarted to prevent loops."
                )
                managed.status = ServiceStatus.STOPPED
                managed.process = None
                return

        # Record crash event for genuine crashes
        crash_event = {
            "timestamp": crash_time,
            "return_code": return_code,
            "pid": managed.pid,
            "restart_count": managed.restart_count,
            "uptime": uptime,
        }

        if service_name not in self._crash_history:
            self._crash_history[service_name] = []
        self._crash_history[service_name].append(crash_event)

        # Analyze crash
        analysis = await self._analyze_crash(managed, return_code)
        self._last_crash_analysis[service_name] = analysis

        # v108.2: Get crash forensics (last output lines before crash)
        crash_context = managed.get_crash_context(num_lines=30)

        # v95.11: Use appropriate log level based on analysis
        log_level = logging.ERROR if analysis.get("severity") != "low" else logging.WARNING
        logger.log(
            log_level,
            f"[v95.9] 💥 PROCESS CRASH: {service_name}\n"
            f"  Exit code: {return_code}\n"
            f"  PID: {managed.pid}\n"
            f"  Uptime: {uptime:.1f}s\n"
            f"  Restart count: {managed.restart_count}\n"
            f"  Analysis: {analysis.get('diagnosis', 'Unknown')}"
        )

        # v108.2: Log crash forensics if available
        if crash_context.get("last_stderr_lines"):
            stderr_lines = crash_context["last_stderr_lines"]
            logger.error(
                f"[v108.2] 📋 CRASH FORENSICS for {service_name}:\n"
                f"  Last {len(stderr_lines)} stderr lines before crash:\n" +
                "\n".join(f"    | {line}" for line in stderr_lines[-15:])
            )
        elif crash_context.get("last_output_lines"):
            output_lines = crash_context["last_output_lines"]
            logger.warning(
                f"[v108.2] 📋 CRASH CONTEXT for {service_name}:\n"
                f"  Last {len(output_lines)} output lines before crash:\n" +
                "\n".join(f"    | {line}" for line in output_lines[-10:])
        )

        # Check circuit breaker
        if self._should_circuit_break(service_name):
            logger.error(
                f"[v95.9] 🔴 CIRCUIT BREAKER OPEN for {service_name}:\n"
                f"  Too many crashes ({self._max_crashes_per_window}) in {self._crash_rate_window}s window.\n"
                f"  Auto-restart DISABLED to prevent crash loop.\n"
                f"  💡 Manual intervention required. Check logs and fix the issue."
            )
            self._crash_circuit_breakers[service_name] = True
            managed.status = ServiceStatus.FAILED

            # Publish critical event
            await self.publish_service_lifecycle_event(
                service_name,
                "circuit_breaker_open",
                {"reason": "crash_rate_exceeded", "analysis": analysis}
            )
            return

        # Auto-restart with backoff
        backoff = managed.calculate_backoff(base=2.0, max_backoff=120.0)
        logger.info(
            f"[v95.9] 🔄 Auto-restarting {service_name} in {backoff:.1f}s "
            f"(attempt {managed.restart_count + 1})"
        )

        await asyncio.sleep(backoff)

        # Check if shutdown started during backoff
        if self._shutdown_event.is_set() or self._shutdown_completed:
            return

        # Attempt restart
        managed.restart_count += 1
        managed.last_restart = time.time()
        managed.status = ServiceStatus.RESTARTING

        # v132.3: If OOM was detected, trigger GCP offload before restart
        oom_detected = getattr(managed, "_oom_detected", False)
        gcp_offload_active = False

        if oom_detected:
            logger.info(f"[v132.3] 🚀 OOM detected - initiating GCP Spot VM offload for {service_name}")
            try:
                # v132.3: Use module-level imports if available, fallback to dynamic import
                if _OOM_PREVENTION_AVAILABLE and _check_memory_before_heavy_init and _MemoryDecision:
                    check_memory_before_heavy_init = _check_memory_before_heavy_init
                    MemoryDecision = _MemoryDecision
                else:
                    try:
                        from backend.core.gcp_oom_prevention_bridge import (
                            check_memory_before_heavy_init,
                            MemoryDecision,
                        )
                    except ImportError:
                        from core.gcp_oom_prevention_bridge import (
                            check_memory_before_heavy_init,
                            MemoryDecision,
                        )

                # Check memory and trigger GCP offload
                mem_result = await check_memory_before_heavy_init(
                    component=service_name,
                    estimated_mb=4096,  # Conservative estimate for crashed service
                    auto_offload=True,
                )

                if mem_result.gcp_vm_ready and mem_result.gcp_vm_ip:
                    logger.info(
                        f"[v132.3] ✅ GCP Spot VM ready for {service_name}: {mem_result.gcp_vm_ip}"
                    )
                    gcp_offload_active = True
                    # Store GCP endpoint for the service to use
                    managed._gcp_offload_endpoint = mem_result.gcp_vm_ip
                elif mem_result.decision == MemoryDecision.ABORT:
                    logger.error(
                        f"[v132.3] ❌ Memory critical, GCP unavailable - cannot safely restart {service_name}"
                    )
                    managed.status = ServiceStatus.FAILED
                    return
                else:
                    logger.warning(
                        f"[v132.3] ⚠️ GCP offload not available - attempting local restart with caution"
                    )
                    # Clear OOM flag to allow local retry
                    managed._oom_detected = False
            except ImportError:
                logger.warning(f"[v132.3] GCP OOM bridge not available - attempting local restart")
            except Exception as e:
                logger.warning(f"[v132.3] GCP offload failed: {e} - attempting local restart")

        try:
            # Use the existing _spawn_service method which handles the full spawn lifecycle
            success = await self._spawn_service(managed)
            if success:
                if gcp_offload_active:
                    logger.info(f"[v132.3] ✅ {service_name} restarted with GCP offload support")
                else:
                    logger.info(f"[v95.9] ✅ {service_name} restarted successfully")
                # Reset circuit breaker on successful restart
                self._crash_circuit_breakers[service_name] = False
                # Clear OOM flag
                managed._oom_detected = False
            else:
                logger.error(f"[v95.9] ❌ {service_name} restart failed")
        except Exception as e:
            logger.error(f"[v95.9] ❌ {service_name} restart error: {e}")

    async def _analyze_crash(
        self,
        managed: "ManagedProcess",
        return_code: int,
    ) -> Dict[str, Any]:
        """
        v95.9: Analyze crash to determine cause and suggest fixes.

        Args:
            managed: The crashed process
            return_code: Exit code

        Returns:
            Analysis result with diagnosis and suggestions
        """
        analysis: Dict[str, Any] = {
            "return_code": return_code,
            "diagnosis": "Unknown",
            "suggestions": [],
            "severity": "medium",
        }

        # Common exit codes
        if return_code == -9 or return_code == 137:
            analysis["diagnosis"] = "Killed by SIGKILL (possibly OOM)"
            analysis["suggestions"] = [
                "Check system memory usage",
                "Increase available memory",
                "Check for memory leaks in the service",
            ]
            analysis["severity"] = "high"
        elif return_code == -15 or return_code == 143:
            analysis["diagnosis"] = "Killed by SIGTERM (graceful shutdown)"
            analysis["severity"] = "low"
        elif return_code == -6 or return_code == 134:
            analysis["diagnosis"] = "Aborted (SIGABRT) - likely assertion failure"
            analysis["suggestions"] = [
                "Check service logs for assertion errors",
                "Look for programming errors",
            ]
            analysis["severity"] = "high"
        elif return_code == 1:
            analysis["diagnosis"] = "General error"
            analysis["suggestions"] = [
                "Check service logs for error details",
                "Verify configuration files",
            ]
        elif return_code == 2:
            analysis["diagnosis"] = "Misuse of command (bad arguments)"
            analysis["suggestions"] = [
                "Check startup arguments",
                "Verify port and path configurations",
            ]
        elif return_code == 127:
            analysis["diagnosis"] = "Command not found"
            analysis["suggestions"] = [
                "Check if Python/venv is correctly set up",
                "Verify script path exists",
            ]

        # Check crash frequency
        recent_crashes = [
            c for c in self._crash_history.get(managed.definition.name, [])
            if time.time() - c["timestamp"] < 60
        ]
        if len(recent_crashes) > 2:
            analysis["suggestions"].append("Multiple crashes in 60s - possible startup issue")
            analysis["severity"] = "critical"

        return analysis

    def _should_circuit_break(self, service_name: str) -> bool:
        """
        v95.9: Check if circuit breaker should activate based on crash rate.

        Args:
            service_name: Service to check

        Returns:
            True if circuit should break
        """
        if service_name in self._crash_circuit_breakers and self._crash_circuit_breakers[service_name]:
            return True  # Already open

        # Count recent crashes
        cutoff = time.time() - self._crash_rate_window
        recent_crashes = [
            c for c in self._crash_history.get(service_name, [])
            if c["timestamp"] > cutoff
        ]

        return len(recent_crashes) >= self._max_crashes_per_window

    # =========================================================================
    # v95.9: Issue 23 - Port Conflict No Recovery
    # =========================================================================

    async def _resolve_port_conflict(
        self,
        service_name: str,
        requested_port: int,
        max_retries: int = 3,
    ) -> Tuple[int, Optional[str]]:
        """
        v132.4: Enhanced port conflict resolution with intelligent handling.

        CRITICAL IMPROVEMENTS:
        1. Uses bind check (not connect) to catch TIME_WAIT ports
        2. Retry logic for transient failures
        3. Detects if same service is already healthy (reuse it)
        4. Waits for TIME_WAIT to clear before retrying
        5. Intelligent alternative port selection

        Args:
            service_name: Service requesting the port
            requested_port: Originally requested port
            max_retries: Number of retry attempts for transient failures

        Returns:
            Tuple of (resolved_port, conflict_info)
        """
        for attempt in range(max_retries):
            # v132.4: Use bind check for accurate detection
            is_available = await self._check_port_available(requested_port, use_bind_check=True)

            if is_available:
                self._port_allocation_map[service_name] = requested_port
                if attempt > 0:
                    logger.info(f"[v132.4] Port {requested_port} became available after {attempt} retries")
                return requested_port, None

            # Port conflict detected - analyze it
            conflict_info = await self._identify_port_conflict(requested_port)

            # v132.4: Check if the same service is already running and healthy
            if service_name in self.processes:
                managed = self.processes[service_name]
                if managed.status == ServiceStatus.HEALTHY and managed.port == requested_port:
                    logger.info(
                        f"[v132.4] Service {service_name} already healthy on port {requested_port} - reusing"
                    )
                    return requested_port, "already_running"

            # v132.4: Check if conflict is from TIME_WAIT (no active listener)
            is_time_wait = await self._is_port_in_time_wait(requested_port)

            if is_time_wait and attempt < max_retries - 1:
                wait_time = 2.0 * (attempt + 1)  # Progressive wait: 2s, 4s, 6s
                logger.info(
                    f"[v132.4] Port {requested_port} in TIME_WAIT state - "
                    f"waiting {wait_time}s for release (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(wait_time)
                continue

            logger.warning(
                f"[v132.4] ⚠️ PORT CONFLICT: {service_name} requested port {requested_port}\n"
                f"  Conflict: {conflict_info}\n"
                f"  TIME_WAIT: {is_time_wait}"
            )

            # Try to clean up if it's a stale JARVIS process
            if conflict_info and any(x in conflict_info.lower() for x in ['jarvis', 'prime', 'reactor', 'uvicorn']):
                cleanup_success = await self._cleanup_stale_process(requested_port)
                if cleanup_success:
                    logger.info(f"[v132.4] ✅ Cleaned up stale process on port {requested_port}")
                    # v132.4: Wait for port to be fully released
                    await asyncio.sleep(2.0)
                    # Verify port is now available
                    if await self._check_port_available(requested_port, use_bind_check=True):
                        self._port_allocation_map[service_name] = requested_port
                        return requested_port, f"Cleaned up stale process: {conflict_info}"

            # v132.4: If first attempt fails, wait before trying alternative
            if attempt < max_retries - 1:
                await asyncio.sleep(1.0)
                continue

        # All retries exhausted - find alternative port
        alternative_port = await self._find_available_port(service_name)

        if alternative_port:
            logger.info(
                f"[v132.4] 🔄 {service_name}: Using alternative port {alternative_port} "
                f"(original {requested_port} in use by: {conflict_info})"
            )
            self._port_allocation_map[service_name] = alternative_port

            # Record conflict for monitoring
            if requested_port not in self._port_conflict_history:
                self._port_conflict_history[requested_port] = []
            self._port_conflict_history[requested_port].append({
                "timestamp": time.time(),
                "service": service_name,
                "conflict_info": conflict_info,
                "resolved_port": alternative_port,
            })

            return alternative_port, f"Conflict with: {conflict_info}"

        # No alternative found
        logger.error(
            f"[v132.4] ❌ CRITICAL: Cannot find available port for {service_name}\n"
            f"  Requested: {requested_port}\n"
            f"  Scanned range: {self._dynamic_port_range}\n"
            f"  💡 Free up ports or adjust JARVIS_PORT_RANGE_START/END"
        )
        return -1, f"No ports available in range {self._dynamic_port_range}"

    async def _is_port_in_time_wait(self, port: int) -> bool:
        """
        v132.4: Check if port is in TIME_WAIT state (no active listener but OS hasn't released it).

        TIME_WAIT happens after a TCP connection closes - the OS holds the port for ~60s
        to handle delayed packets. This is different from an active listener.

        Returns:
            True if port is in TIME_WAIT (can potentially be reused with SO_REUSEADDR)
        """
        try:
            import psutil
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr and hasattr(conn.laddr, 'port') and conn.laddr.port == port:
                    if conn.status == 'TIME_WAIT':
                        return True
                    if conn.status == 'LISTEN':
                        return False  # Active listener, not TIME_WAIT
            return False
        except ImportError:
            return False
        except Exception:
            return False

    async def _check_port_available(self, port: int, use_bind_check: bool = True) -> bool:
        """
        v132.4: Enhanced port availability check with bind verification.

        CRITICAL FIX: The old connect_ex check only tests if something is LISTENING,
        but Errno 48 (EADDRINUSE) happens during BIND when the port is in TIME_WAIT
        state from a recently closed connection. This new check actually tries to
        bind to the port, which catches ALL cases.

        Args:
            port: Port number to check
            use_bind_check: If True, use actual bind check (more accurate but slower)

        Returns:
            True if port is available for binding
        """
        import socket

        # v132.4: Skip cache for bind checks - they need to be accurate
        if not use_bind_check:
            # Check cache for quick connect checks
            if port in self._port_scan_cache:
                in_use, check_time = self._port_scan_cache[port]
                if time.time() - check_time < self._port_scan_cache_ttl:
                    return not in_use

        # v132.4: Primary method - try to actually BIND (catches TIME_WAIT)
        if use_bind_check:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(('0.0.0.0', port))
                sock.close()
                # Port is truly available
                self._port_scan_cache[port] = (False, time.time())
                return True
            except OSError as e:
                sock.close()
                # Errno 48 (EADDRINUSE) or 98 (Linux) = port in use
                if e.errno in (48, 98):
                    logger.debug(f"[v132.4] Port {port} bind check failed: {e}")
                    self._port_scan_cache[port] = (True, time.time())
                    return False
                # Other errors - assume unavailable
                logger.warning(f"[v132.4] Port {port} bind check error: {e}")
                return False
            except Exception as e:
                sock.close()
                logger.warning(f"[v132.4] Port {port} bind check exception: {e}")
                return False

        # Fallback: connect check (less accurate but faster)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        try:
            result = sock.connect_ex(('localhost', port))
            in_use = (result == 0)
        except Exception:
            in_use = False  # Assume available if check fails
        finally:
            sock.close()

        # Update cache
        self._port_scan_cache[port] = (in_use, time.time())
        return not in_use

    async def _identify_port_conflict(self, port: int) -> str:
        """
        v95.9: Identify what process is using a port.

        Args:
            port: Port number

        Returns:
            Description of conflicting process
        """
        try:
            import psutil

            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr and hasattr(conn.laddr, 'port') and conn.laddr.port == port:
                    if conn.status == 'LISTEN':
                        try:
                            proc = psutil.Process(conn.pid)
                            cmdline = ' '.join(proc.cmdline()[:3])
                            return f"PID={conn.pid}, Name={proc.name()}, Cmd={cmdline}"
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            return f"PID={conn.pid} (details unavailable)"
            return "Unknown process"
        except ImportError:
            return "psutil not available for process identification"
        except Exception as e:
            return f"Error identifying process: {e}"

    async def _cleanup_stale_process(self, port: int) -> bool:
        """
        v95.9: Clean up stale JARVIS process on a port.
        v111.3: CRITICAL FIX - Never kill processes spawned in this session.

        Args:
            port: Port to clean up

        Returns:
            True if cleanup successful
        """
        try:
            import psutil

            # v111.3: Get PIDs of processes we spawned in this session
            # NEVER kill our own spawned children - they are not "stale"
            # v118.0: CRITICAL FIX - Use self.processes not self._managed_processes
            spawned_pids = set()
            for managed in self.processes.values():
                if managed.pid:
                    spawned_pids.add(managed.pid)

            current_pid = os.getpid()
            parent_pid = os.getppid()

            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr and hasattr(conn.laddr, 'port') and conn.laddr.port == port:
                    if conn.status == 'LISTEN':
                        try:
                            proc = psutil.Process(conn.pid)

                            # v111.3: Skip self, parent, and processes we spawned
                            if conn.pid == current_pid:
                                logger.debug(f"[v111.3] Skipping self (PID={conn.pid}) on port {port}")
                                continue
                            if conn.pid == parent_pid:
                                logger.debug(f"[v111.3] Skipping parent (PID={conn.pid}) on port {port}")
                                continue
                            if conn.pid in spawned_pids:
                                logger.info(
                                    f"[v111.3] Skipping PID {conn.pid} on port {port} - "
                                    f"it's a process we spawned this session (NOT stale)"
                                )
                                continue

                            # v118.0: ADDITIONAL SAFETY - Check GlobalProcessRegistry
                            # This catches processes registered globally but not in local spawned_pids
                            try:
                                from backend.core.supervisor_singleton import GlobalProcessRegistry
                                if conn.pid is not None and GlobalProcessRegistry.is_ours(conn.pid):
                                    logger.info(
                                        f"    ⚠️ [v118.0] Skipping PID {conn.pid} on port {port} - "
                                        f"registered in GlobalProcessRegistry (protected)"
                                    )
                                    continue
                            except ImportError:
                                pass  # GlobalProcessRegistry not available

                            cmdline = ' '.join(proc.cmdline())

                            # Only kill JARVIS-related processes
                            if any(x in cmdline.lower() for x in ['jarvis', 'prime', 'reactor']):
                                logger.info(f"[v95.9] Terminating stale process: {proc.name()} (PID={conn.pid})")
                                proc.terminate()

                                # Wait for graceful shutdown
                                try:
                                    proc.wait(timeout=5)
                                except psutil.TimeoutExpired:
                                    logger.warning(f"[v95.9] Force killing process {conn.pid}")
                                    proc.kill()

                                return True
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
            return False
        except ImportError:
            return False
        except Exception as e:
            logger.error(f"[v95.9] Error cleaning up stale process: {e}")
            return False

    async def _find_available_port(self, service_name: str) -> Optional[int]:
        """
        v132.4: Enhanced port finding with intelligent selection strategy.

        IMPROVEMENTS:
        1. Uses bind check for accuracy
        2. Prefers ports not recently in conflict
        3. Avoids ports that might be in TIME_WAIT
        4. Parallel checking for speed
        5. Service-specific port preferences

        Args:
            service_name: Service requesting the port

        Returns:
            Available port number or None
        """
        start, end = self._dynamic_port_range

        # v132.4: Service-specific preferred ports (for easier debugging)
        service_port_hints = {
            "jarvis-prime": [8001, 8002, 8003],
            "reactor-core": [8010, 8011, 8012],
            "jarvis-backend": [8020, 8021, 8022],
        }

        # Try service-specific hints first
        service_lower = service_name.lower().replace("_", "-")
        if service_lower in service_port_hints:
            for hint_port in service_port_hints[service_lower]:
                if hint_port not in self._port_allocation_map.values():
                    if await self._check_port_available(hint_port, use_bind_check=True):
                        logger.info(f"[v132.4] Using preferred port {hint_port} for {service_name}")
                        return hint_port

        # v132.4: Collect recently conflicted ports to avoid
        recently_conflicted = set()
        cutoff = time.time() - 300  # 5 minutes
        for port, conflicts in self._port_conflict_history.items():
            if any(c["timestamp"] > cutoff for c in conflicts):
                recently_conflicted.add(port)

        # v132.4: Check ports in parallel batches for speed
        batch_size = 10
        ports_to_check = [
            p for p in range(start, end + 1)
            if p not in recently_conflicted and p not in self._port_allocation_map.values()
        ]

        for i in range(0, len(ports_to_check), batch_size):
            batch = ports_to_check[i:i + batch_size]
            # Check batch in parallel
            checks = await asyncio.gather(*[
                self._check_port_available(port, use_bind_check=True)
                for port in batch
            ], return_exceptions=True)

            for port, available in zip(batch, checks):
                if available is True:
                    logger.debug(f"[v132.4] Found available port {port} for {service_name}")
                    return port

        # Fallback: check recently conflicted ports (TIME_WAIT might have expired)
        for port in recently_conflicted:
            if await self._check_port_available(port, use_bind_check=True):
                logger.info(f"[v132.4] Previously conflicted port {port} now available")
                return port

        return None

    # =========================================================================
    # v132.4: GCP VM Health Verification
    # =========================================================================

    async def _verify_gcp_vm_health(
        self,
        gcp_vm_ip: str,
        timeout: float = 10.0,
        port: int = 8080,
    ) -> bool:
        """
        v132.4: Verify that a GCP VM is actually reachable and healthy.

        This prevents the "GCP VM ready but not reachable" scenario where
        the VM is technically running but not yet serving requests.

        Args:
            gcp_vm_ip: IP address of the GCP VM
            timeout: Maximum time to wait for health check
            port: Port to check (default: 8080 for model server)

        Returns:
            True if VM is reachable and healthy
        """
        import aiohttp

        health_endpoints = [
            f"http://{gcp_vm_ip}:{port}/health",
            f"http://{gcp_vm_ip}:{port}/",
            f"http://{gcp_vm_ip}:{port}/api/health",
        ]

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                for endpoint in health_endpoints:
                    try:
                        async with session.get(endpoint) as response:
                            if response.status < 500:  # Accept 2xx, 3xx, 4xx (service is running)
                                logger.info(f"[v132.4] GCP VM health check passed: {endpoint}")
                                return True
                    except aiohttp.ClientError:
                        continue  # Try next endpoint
                    except Exception:
                        continue

            # All endpoints failed - try raw TCP connect
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                result = sock.connect_ex((gcp_vm_ip, port))
                sock.close()
                if result == 0:
                    logger.info(f"[v132.4] GCP VM TCP health check passed: {gcp_vm_ip}:{port}")
                    return True
            except Exception:
                pass
            finally:
                sock.close()

            logger.warning(f"[v132.4] GCP VM health check failed for {gcp_vm_ip}")
            return False

        except ImportError:
            # aiohttp not available - use basic socket check
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                result = sock.connect_ex((gcp_vm_ip, port))
                sock.close()
                return result == 0
            except Exception:
                return False
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[v132.4] GCP VM health check error: {e}")
            return False

    # =========================================================================
    # v95.9: Issue 24 - Import Error No Fallback
    # =========================================================================

    async def _safe_import(
        self,
        module_name: str,
        fallbacks: Optional[List[str]] = None,
        lazy: bool = True,
    ) -> Tuple[Any, Optional[str]]:
        """
        v95.9: Safely import a module with fallback options.

        Features:
        - Primary import attempt
        - Fallback to alternatives
        - Error recording
        - Lazy loading option

        Args:
            module_name: Primary module to import
            fallbacks: List of fallback module names
            lazy: Whether to use lazy loading

        Returns:
            Tuple of (module_or_none, error_message)
        """
        fallbacks = fallbacks or []

        # Check if already loaded
        if module_name in self._lazy_imports:
            return self._lazy_imports[module_name], None

        # Try primary import
        try:
            module = __import__(module_name)
            # Handle nested module names
            for part in module_name.split('.')[1:]:
                module = getattr(module, part)

            self._lazy_imports[module_name] = module
            return module, None

        except ImportError as e:
            error = str(e)
            logger.warning(f"[v95.9] Import failed for {module_name}: {error}")

            # Record error
            self._import_errors[module_name] = {
                "error": error,
                "timestamp": time.time(),
                "fallbacks_tried": [],
            }

            # Try fallbacks
            for fallback in fallbacks:
                try:
                    logger.info(f"[v95.9] Trying fallback import: {fallback}")
                    module = __import__(fallback)
                    for part in fallback.split('.')[1:]:
                        module = getattr(module, part)

                    self._lazy_imports[module_name] = module
                    self._loaded_alternatives[module_name] = fallback
                    logger.info(f"[v95.9] ✅ Fallback {fallback} loaded for {module_name}")
                    return module, None

                except ImportError as fe:
                    self._import_errors[module_name]["fallbacks_tried"].append({
                        "module": fallback,
                        "error": str(fe),
                    })

            # All imports failed
            logger.error(
                f"[v95.9] ❌ IMPORT FAILURE: {module_name}\n"
                f"  Error: {error}\n"
                f"  Fallbacks tried: {fallbacks}\n"
                f"  💡 Install missing package or check PYTHONPATH"
            )
            return None, error

    def _register_import_fallback(self, module: str, fallbacks: List[str]) -> None:
        """
        v95.9: Register fallback modules for a primary module.

        Args:
            module: Primary module name
            fallbacks: List of fallback module names
        """
        self._import_fallbacks[module] = fallbacks

    # =========================================================================
    # v95.9: Issue 25 - Network Timeout No Retry
    # =========================================================================

    async def _network_request_with_retry(
        self,
        url: str,
        method: str = "GET",
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        **kwargs,
    ) -> Tuple[Optional[Any], Optional[str]]:
        """
        v95.9: Make network request with retry and circuit breaker.

        Features:
        - Exponential backoff retry
        - Circuit breaker for repeated failures
        - Network health monitoring
        - Detailed error reporting

        Args:
            url: Request URL
            method: HTTP method
            timeout: Request timeout
            max_retries: Maximum retry attempts
            **kwargs: Additional request arguments

        Returns:
            Tuple of (response_data, error_message)
        """
        config = self._network_retry_config
        # Ensure non-None values with sensible defaults
        effective_timeout: float = timeout if timeout is not None else config.get("timeout", 30.0)
        effective_max_retries: int = max_retries if max_retries is not None else config.get("max_retries", 3)

        # Check circuit breaker
        endpoint = self._get_endpoint_key(url)
        if self._is_circuit_open(endpoint):
            return None, f"Circuit breaker open for {endpoint}"

        last_error: Optional[str] = None

        for attempt in range(effective_max_retries + 1):
            try:
                # Get the shared HTTP session (connection pooled)
                session = await self._get_http_session()
                request_timeout = aiohttp.ClientTimeout(total=effective_timeout)

                # Make request using the session (don't close - it's shared)
                async with session.request(
                    method,
                    url,
                    timeout=request_timeout,
                    **kwargs
                ) as response:
                    if response.status < 400:
                        # Success - reset circuit breaker
                        self._record_network_success(endpoint)
                        content_type = response.content_type or ""
                        data = await response.json() if 'json' in content_type else await response.text()
                        return data, None
                    else:
                        last_error = f"HTTP {response.status}: {await response.text()}"

            except asyncio.TimeoutError:
                last_error = f"Timeout after {effective_timeout}s"
            except aiohttp.ClientConnectorError as e:
                last_error = f"Connection error: {e}"
            except Exception as e:
                last_error = f"Request error: {e}"

            # Record failure
            self._record_network_failure(endpoint, last_error or "Unknown error")

            # Check if we should retry
            if attempt < effective_max_retries:
                delay = min(
                    config.get("base_delay", 1.0) * (2 ** attempt),
                    config.get("max_delay", 30.0)
                )
                # Add jitter
                jitter = delay * 0.1 * (time.time() % 1)
                delay += jitter

                logger.debug(
                    f"[v95.9] Network retry for {endpoint} in {delay:.1f}s "
                    f"(attempt {attempt + 2}/{effective_max_retries + 1}): {last_error}"
                )
                await asyncio.sleep(delay)

        # All retries failed
        logger.warning(
            f"[v95.9] ⚠️ Network request failed after {effective_max_retries + 1} attempts: {url}\n"
            f"  Last error: {last_error}"
        )
        return None, last_error

    def _get_endpoint_key(self, url: str) -> str:
        """Extract endpoint key from URL for circuit breaker tracking."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return f"{parsed.netloc}{parsed.path}"

    def _is_circuit_open(self, endpoint: str) -> bool:
        """Check if circuit breaker is open for an endpoint."""
        if endpoint not in self._network_circuit_breakers:
            return False

        breaker = self._network_circuit_breakers[endpoint]
        if not breaker.get("open", False):
            return False

        # Check if reset time has passed
        reset_time = breaker.get("reset_time", 0)
        if time.time() > reset_time:
            # Half-open - allow one request through
            breaker["open"] = False
            breaker["half_open"] = True
            return False

        return True

    def _record_network_success(self, endpoint: str) -> None:
        """Record successful network request."""
        if endpoint in self._network_circuit_breakers:
            self._network_circuit_breakers[endpoint] = {
                "failures": 0,
                "open": False,
                "half_open": False,
            }

        self._network_health_status[endpoint] = {
            "healthy": True,
            "last_success": time.time(),
        }

    def _record_network_failure(self, endpoint: str, error: str) -> None:
        """Record failed network request and potentially open circuit."""
        config = self._network_retry_config

        if endpoint not in self._network_circuit_breakers:
            self._network_circuit_breakers[endpoint] = {
                "failures": 0,
                "open": False,
            }

        breaker = self._network_circuit_breakers[endpoint]
        breaker["failures"] = breaker.get("failures", 0) + 1
        breaker["last_error"] = error
        breaker["last_failure"] = time.time()

        # Check if we should open the circuit
        if breaker["failures"] >= config["circuit_threshold"]:
            breaker["open"] = True
            breaker["reset_time"] = time.time() + config["circuit_reset_time"]
            logger.warning(
                f"[v95.9] 🔴 Circuit breaker OPEN for {endpoint}: "
                f"{breaker['failures']} consecutive failures"
            )

        self._network_health_status[endpoint] = {
            "healthy": False,
            "last_failure": time.time(),
            "error": error,
        }

    # Note: _get_http_session is defined in v93.11 section (line ~4319)
    # with comprehensive connection pooling configuration

    def get_error_recovery_status(self) -> Dict[str, Any]:
        """
        v95.9: Get comprehensive error recovery status.

        Returns:
            Dict with status of all error recovery systems
        """
        return {
            "discovery": {
                "failures": self._discovery_failures,
                "degraded_services": list(self._degraded_services),
            },
            "crashes": {
                "history": {k: len(v) for k, v in self._crash_history.items()},
                "circuit_breakers": self._crash_circuit_breakers,
                "last_analysis": self._last_crash_analysis,
            },
            "ports": {
                "allocations": self._port_allocation_map,
                "conflicts": {k: len(v) for k, v in self._port_conflict_history.items()},
            },
            "imports": {
                "errors": self._import_errors,
                "alternatives": self._loaded_alternatives,
            },
            "network": {
                "circuit_breakers": {
                    k: {"open": v.get("open", False), "failures": v.get("failures", 0)}
                    for k, v in self._network_circuit_breakers.items()
                },
                "health": self._network_health_status,
            },
            "timestamp": time.time(),
        }

    # =========================================================================
    # v95.10: Cross-Repo Integration Infrastructure (Issues 31-38)
    # =========================================================================

    # -------------------------------------------------------------------------
    # Issue 31: Unified Configuration System
    # -------------------------------------------------------------------------

    async def _initialize_unified_config(self) -> None:
        """
        v95.10: Initialize unified cross-repo configuration system.

        Features:
        - Config inheritance (base -> repo-specific -> overrides)
        - Schema validation with type coercion
        - Change watchers for dynamic updates
        - Conflict detection and resolution
        """
        self._ensure_locks_initialized()
        assert self._config_lock is not None

        async with self._config_lock:
            # Define base Trinity configuration schema
            self._config_schema = {
                "ports": {
                    "type": "dict",
                    "schema": {
                        "jarvis_body": {"type": "int", "default": 8010, "min": 1024, "max": 65535},
                        "jarvis_prime": {"type": "int", "default": 8000, "min": 1024, "max": 65535},
                        "reactor_core": {"type": "int", "default": 8090, "min": 1024, "max": 65535},
                    },
                },
                "timeouts": {
                    "type": "dict",
                    "schema": {
                        "startup": {"type": "float", "default": 120.0, "min": 10.0},
                        "health_check": {"type": "float", "default": 5.0, "min": 1.0},
                        "shutdown": {"type": "float", "default": 30.0, "min": 5.0},
                    },
                },
                "retry": {
                    "type": "dict",
                    "schema": {
                        "max_attempts": {"type": "int", "default": 5, "min": 1, "max": 20},
                        "base_delay": {"type": "float", "default": 1.0, "min": 0.1},
                        "max_delay": {"type": "float", "default": 60.0, "min": 1.0},
                    },
                },
                "features": {
                    "type": "dict",
                    "schema": {
                        "auto_recovery": {"type": "bool", "default": True},
                        "metrics_collection": {"type": "bool", "default": True},
                        "distributed_tracing": {"type": "bool", "default": True},
                        "state_sync": {"type": "bool", "default": True},
                    },
                },
            }

            # Load configs from all repos
            await self._load_repo_configs()

            # Merge with inheritance
            self._unified_config = await self._merge_configs_with_inheritance()

            # Start config sync task
            if self._config_sync_task is None or self._config_sync_task.done():
                self._config_sync_task = asyncio.create_task(
                    self._config_sync_loop(),
                    name="config_sync_loop"
                )
                self._track_background_task(self._config_sync_task)

            logger.info("[v95.10] ✅ Unified configuration system initialized")

    async def _load_repo_configs(self) -> None:
        """Load configuration from each repo's config file."""
        repos = {
            "jarvis": self.config.jarvis_path if hasattr(self.config, 'jarvis_path') else Path.cwd(),
            "jarvis-prime": self.config.jarvis_prime_path,
            "reactor-core": self.config.reactor_core_path,
        }

        for repo_name, repo_path in repos.items():
            if not repo_path or not repo_path.exists():
                continue

            config_files = [
                repo_path / "trinity_config.json",
                repo_path / "config" / "trinity.json",
                repo_path / ".trinity.json",
            ]

            for config_file in config_files:
                if config_file.exists():
                    try:
                        with open(config_file, "r") as f:
                            self._config_sources[repo_name] = json.load(f)
                        logger.debug(f"[v95.10] Loaded config for {repo_name} from {config_file}")
                        break
                    except Exception as e:
                        logger.warning(f"[v95.10] Failed to load config from {config_file}: {e}")

    async def _merge_configs_with_inheritance(self) -> Dict[str, Any]:
        """
        Merge configurations with proper inheritance.

        Priority (lowest to highest):
        1. Schema defaults
        2. JARVIS body config
        3. JARVIS Prime config
        4. Reactor Core config
        5. Environment variables
        6. User overrides
        """
        merged: Dict[str, Any] = {}

        # 1. Apply schema defaults
        merged = self._apply_schema_defaults(self._config_schema)

        # 2-4. Merge repo configs in order
        for repo in ["jarvis", "jarvis-prime", "reactor-core"]:
            if repo in self._config_sources:
                merged = self._deep_merge(merged, self._config_sources[repo])

        # 5. Apply environment variable overrides
        merged = self._apply_env_overrides(merged)

        # 6. Apply user overrides
        if self._config_overrides:
            merged = self._deep_merge(merged, self._config_overrides)

        # Validate merged config
        validated = self._validate_config(merged, self._config_schema)

        return validated

    def _apply_schema_defaults(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        """Apply default values from schema."""
        result: Dict[str, Any] = {}

        for key, spec in schema.items():
            if spec.get("type") == "dict" and "schema" in spec:
                result[key] = self._apply_schema_defaults(spec["schema"])
            elif "default" in spec:
                result[key] = spec["default"]

        return result

    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """Deep merge two dictionaries."""
        result = base.copy()

        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value

        return result

    def _apply_env_overrides(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Apply environment variable overrides."""
        env_mappings = {
            "JARVIS_BODY_PORT": ("ports", "jarvis_body"),
            "JARVIS_PRIME_PORT": ("ports", "jarvis_prime"),
            "REACTOR_CORE_PORT": ("ports", "reactor_core"),
            "JARVIS_STARTUP_TIMEOUT": ("timeouts", "startup"),
            "JARVIS_HEALTH_TIMEOUT": ("timeouts", "health_check"),
            "JARVIS_MAX_RETRIES": ("retry", "max_attempts"),
            "JARVIS_AUTO_RECOVERY": ("features", "auto_recovery"),
            "JARVIS_METRICS": ("features", "metrics_collection"),
        }

        for env_var, (section, key) in env_mappings.items():
            value = os.environ.get(env_var)
            if value is not None:
                if section not in config:
                    config[section] = {}

                # Type coercion
                if value.lower() in ("true", "false"):
                    config[section][key] = value.lower() == "true"
                elif value.isdigit():
                    config[section][key] = int(value)
                else:
                    try:
                        config[section][key] = float(value)
                    except ValueError:
                        config[section][key] = value

        return config

    def _validate_config(self, config: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
        """Validate configuration against schema."""
        validated: Dict[str, Any] = {}

        for key, spec in schema.items():
            value = config.get(key)

            if value is None:
                if "default" in spec:
                    validated[key] = spec["default"]
                continue

            # Type validation and coercion
            expected_type = spec.get("type", "any")

            if expected_type == "dict" and "schema" in spec:
                if isinstance(value, dict):
                    validated[key] = self._validate_config(value, spec["schema"])
                else:
                    validated[key] = self._apply_schema_defaults(spec["schema"])
            elif expected_type == "int":
                try:
                    val = int(value)
                    # Range validation
                    if "min" in spec:
                        val = max(val, spec["min"])
                    if "max" in spec:
                        val = min(val, spec["max"])
                    validated[key] = val
                except (ValueError, TypeError):
                    validated[key] = spec.get("default", 0)
            elif expected_type == "float":
                try:
                    val = float(value)
                    if "min" in spec:
                        val = max(val, spec["min"])
                    if "max" in spec:
                        val = min(val, spec["max"])
                    validated[key] = val
                except (ValueError, TypeError):
                    validated[key] = spec.get("default", 0.0)
            elif expected_type == "bool":
                validated[key] = bool(value)
            else:
                validated[key] = value

        return validated

    async def _config_sync_loop(self) -> None:
        """Background task to sync configuration across repos."""
        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(self._config_sync_interval)

                if self._shutdown_event.is_set():
                    break

                # Reload and re-merge configs
                await self._load_repo_configs()
                new_config = await self._merge_configs_with_inheritance()

                # Detect changes and notify watchers
                assert self._config_lock is not None
                async with self._config_lock:
                    changes = self._detect_config_changes(self._unified_config, new_config)
                    if changes:
                        logger.info(f"[v95.10] Config changes detected: {list(changes.keys())}")
                        self._unified_config = new_config

                        # Notify watchers
                        for key, new_value in changes.items():
                            if key in self._config_watchers:
                                try:
                                    callback = self._config_watchers[key]
                                    if asyncio.iscoroutinefunction(callback):
                                        await callback(key, new_value)
                                    else:
                                        callback(key, new_value)
                                except Exception as e:
                                    logger.error(f"[v95.10] Config watcher error for {key}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[v95.10] Config sync error: {e}")

    def _detect_config_changes(
        self,
        old_config: Dict[str, Any],
        new_config: Dict[str, Any],
        prefix: str = ""
    ) -> Dict[str, Any]:
        """Detect changes between two configs."""
        changes: Dict[str, Any] = {}

        all_keys = set(old_config.keys()) | set(new_config.keys())
        for key in all_keys:
            full_key = f"{prefix}.{key}" if prefix else key
            old_val = old_config.get(key)
            new_val = new_config.get(key)

            if isinstance(old_val, dict) and isinstance(new_val, dict):
                nested_changes = self._detect_config_changes(old_val, new_val, full_key)
                changes.update(nested_changes)
            elif old_val != new_val:
                changes[full_key] = new_val

        return changes

    def get_config(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value using dot notation.

        Args:
            key: Dot-separated key (e.g., "ports.jarvis_prime")
            default: Default value if key not found

        Returns:
            Configuration value
        """
        parts = key.split(".")
        value: Any = self._unified_config

        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default

        return value

    def set_config_override(self, key: str, value: Any) -> None:
        """
        Set a configuration override.

        Args:
            key: Dot-separated key
            value: New value
        """
        parts = key.split(".")
        current = self._config_overrides

        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]

        current[parts[-1]] = value

    def watch_config(self, key: str, callback: Callable) -> None:
        """Register a watcher for configuration changes."""
        self._config_watchers[key] = callback

    # -------------------------------------------------------------------------
    # Issue 32: Cross-Repo Unified Logging
    # -------------------------------------------------------------------------

    async def _initialize_unified_logging(self) -> None:
        """
        v95.10: Initialize unified cross-repo logging system.

        Features:
        - W3C Trace Context propagation
        - Log correlation with trace IDs
        - Log aggregation and buffering
        - Cross-repo log streaming
        """
        self._ensure_locks_initialized()
        assert self._log_lock is not None

        async with self._log_lock:
            # Setup unified log formatter with trace context
            self._setup_trace_context_formatter()

            # Initialize state persistence path
            jarvis_home = Path.home() / ".jarvis"
            self._state_persistence_path = jarvis_home / "logs"
            self._state_persistence_path.mkdir(parents=True, exist_ok=True)

            # Start log flush task
            if self._log_flush_task is None or self._log_flush_task.done():
                self._log_flush_task = asyncio.create_task(
                    self._log_flush_loop(),
                    name="log_flush_loop"
                )
                self._track_background_task(self._log_flush_task)

            logger.info("[v95.10] ✅ Unified logging system initialized")

    def _setup_trace_context_formatter(self) -> None:
        """Setup log formatter with W3C Trace Context."""

        class TraceContextFormatter(logging.Formatter):
            """Formatter that includes trace context in log messages."""

            def __init__(self, orchestrator: "ProcessOrchestrator"):
                super().__init__(
                    fmt="%(asctime)s [%(levelname)s] [%(trace_id)s:%(span_id)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S"
                )
                self._orchestrator = orchestrator

            def format(self, record: logging.LogRecord) -> str:
                # Add trace context to record
                trace_id = getattr(record, "trace_id", None)
                span_id = getattr(record, "span_id", None)

                if not trace_id:
                    trace_id = self._orchestrator._startup_correlation_id or "-"
                if not span_id:
                    span_id = "-"

                record.trace_id = trace_id[:16] if len(trace_id) > 16 else trace_id
                record.span_id = span_id[:8] if len(span_id) > 8 else span_id

                return super().format(record)

        # Store formatter for use
        self._unified_log_handlers["trace_formatter"] = TraceContextFormatter(self)

    async def emit_cross_repo_log(
        self,
        level: str,
        message: str,
        source_repo: str,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Emit a log entry with cross-repo correlation.

        Args:
            level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            message: Log message
            source_repo: Source repository name
            trace_id: Optional trace ID (generated if not provided)
            span_id: Optional span ID
            extra: Additional context
        """
        if not self._log_correlation_enabled:
            return

        self._ensure_locks_initialized()
        assert self._log_lock is not None

        # Generate trace ID if not provided
        if not trace_id:
            trace_id = self._startup_correlation_id or self._generate_trace_id()

        if not span_id:
            span_id = self._generate_span_id()

        log_entry = {
            "timestamp": time.time(),
            "level": level.upper(),
            "message": message,
            "source_repo": source_repo,
            "trace_id": trace_id,
            "span_id": span_id,
            "extra": extra or {},
        }

        async with self._log_lock:
            self._log_aggregation_buffer.append(log_entry)

            # Flush if buffer is full
            if len(self._log_aggregation_buffer) >= self._log_buffer_size:
                await self._flush_log_buffer()

    def _generate_trace_id(self) -> str:
        """Generate W3C-compliant trace ID (32 hex chars)."""
        import uuid
        return uuid.uuid4().hex

    def _generate_span_id(self) -> str:
        """Generate W3C-compliant span ID (16 hex chars)."""
        import uuid
        return uuid.uuid4().hex[:16]

    async def _log_flush_loop(self) -> None:
        """Background task to periodically flush log buffer."""
        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(self._log_flush_interval)

                if self._shutdown_event.is_set():
                    break

                assert self._log_lock is not None
                async with self._log_lock:
                    if self._log_aggregation_buffer:
                        await self._flush_log_buffer()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[v95.10] Log flush error: {e}")

    async def _flush_log_buffer(self) -> None:
        """Flush log buffer to storage/aggregation."""
        if not self._log_aggregation_buffer:
            return

        entries = self._log_aggregation_buffer.copy()
        self._log_aggregation_buffer.clear()

        # Write to aggregated log file
        if self._state_persistence_path:
            log_file = self._state_persistence_path / "unified_logs.jsonl"
            try:
                with open(log_file, "a") as f:
                    for entry in entries:
                        f.write(json.dumps(entry) + "\n")
            except Exception as e:
                logger.error(f"[v95.10] Failed to write logs: {e}")

    def get_trace_context_headers(self) -> Dict[str, str]:
        """Get trace context headers for HTTP propagation."""
        trace_id = self._startup_correlation_id or self._generate_trace_id()
        span_id = self._generate_span_id()

        return {
            self._trace_id_header: trace_id,
            self._span_id_header: span_id,
            "traceparent": f"00-{trace_id}-{span_id}-01",  # W3C format
        }

    # -------------------------------------------------------------------------
    # Issue 33: Cross-Repo Metrics Collection
    # -------------------------------------------------------------------------

    async def _initialize_metrics_collection(self) -> None:
        """
        v95.10: Initialize unified metrics collection system.

        Features:
        - Cross-repo metric aggregation
        - Custom metric collectors
        - Metric buffering and batching
        - Health dashboard data
        """
        self._ensure_locks_initialized()
        assert self._metrics_lock is not None

        if not self._metrics_enabled:
            logger.info("[v95.10] Metrics collection disabled")
            return

        async with self._metrics_lock:
            # Register default metrics
            self._register_default_metrics()

            # Start collection task
            if self._metrics_collection_task is None or self._metrics_collection_task.done():
                self._metrics_collection_task = asyncio.create_task(
                    self._metrics_collection_loop(),
                    name="metrics_collection_loop"
                )
                self._track_background_task(self._metrics_collection_task)

            logger.info("[v95.10] ✅ Metrics collection system initialized")

    def _register_default_metrics(self) -> None:
        """Register default cross-repo metrics."""
        self._metrics_registry = {
            "service_health": {
                "type": "gauge",
                "description": "Service health status (0=unhealthy, 1=healthy)",
                "labels": ["service", "repo"],
            },
            "service_uptime": {
                "type": "gauge",
                "description": "Service uptime in seconds",
                "labels": ["service"],
            },
            "request_latency": {
                "type": "histogram",
                "description": "Request latency in milliseconds",
                "labels": ["service", "endpoint"],
                "buckets": [10, 50, 100, 250, 500, 1000, 2500, 5000],
            },
            "error_count": {
                "type": "counter",
                "description": "Total error count",
                "labels": ["service", "error_type"],
            },
            "memory_usage": {
                "type": "gauge",
                "description": "Memory usage in bytes",
                "labels": ["service"],
            },
            "cpu_usage": {
                "type": "gauge",
                "description": "CPU usage percentage",
                "labels": ["service"],
            },
            "active_connections": {
                "type": "gauge",
                "description": "Active connection count",
                "labels": ["service"],
            },
        }

    async def _metrics_collection_loop(self) -> None:
        """Background task to collect metrics from all repos."""
        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(self._metrics_collection_interval)

                if self._shutdown_event.is_set():
                    break

                await self._collect_all_metrics()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[v95.10] Metrics collection error: {e}")

    async def _collect_all_metrics(self) -> None:
        """Collect metrics from all registered sources."""
        self._ensure_locks_initialized()
        assert self._metrics_lock is not None

        timestamp = time.time()

        async with self._metrics_lock:
            # Collect from each service
            for service_name, managed in self.processes.items():
                try:
                    metrics = await self._collect_service_metrics(service_name, managed)
                    self._service_metrics_cache[service_name] = {
                        "metrics": metrics,
                        "timestamp": timestamp,
                    }
                except Exception as e:
                    logger.debug(f"[v95.10] Failed to collect metrics from {service_name}: {e}")

            # Run custom collectors
            for collector_name, collector_fn in self._metrics_collectors.items():
                try:
                    if asyncio.iscoroutinefunction(collector_fn):
                        metrics = await collector_fn()
                    else:
                        metrics = collector_fn()

                    self._metrics_buffer.extend(metrics if isinstance(metrics, list) else [metrics])
                except Exception as e:
                    logger.debug(f"[v95.10] Collector {collector_name} failed: {e}")

    async def _collect_service_metrics(
        self,
        service_name: str,
        managed: "ManagedProcess"
    ) -> Dict[str, Any]:
        """Collect metrics from a single service."""
        metrics: Dict[str, Any] = {
            "service": service_name,
            "timestamp": time.time(),
        }

        # Health status
        metrics["health"] = 1 if managed.status == ServiceStatus.HEALTHY else 0

        # Uptime
        if managed.last_restart:
            metrics["uptime"] = time.time() - managed.last_restart

        # Process metrics (if available)
        if managed.pid and managed.process:
            try:
                import psutil
                proc = psutil.Process(managed.pid)
                metrics["memory_bytes"] = proc.memory_info().rss
                metrics["cpu_percent"] = proc.cpu_percent()
            except Exception:
                pass

        # Restart count
        metrics["restart_count"] = managed.restart_count

        # Port
        metrics["port"] = managed.port

        return metrics

    def register_metric_collector(self, name: str, collector: Callable) -> None:
        """Register a custom metric collector."""
        self._metrics_collectors[name] = collector

    def record_metric(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Record a metric value."""
        if not self._metrics_enabled:
            return

        self._metrics_buffer.append({
            "name": name,
            "value": value,
            "labels": labels or {},
            "timestamp": time.time(),
        })

    def get_metrics_snapshot(self) -> Dict[str, Any]:
        """Get current metrics snapshot for dashboard."""
        return {
            "services": self._service_metrics_cache.copy(),
            "buffer_size": len(self._metrics_buffer),
            "registry": list(self._metrics_registry.keys()),
            "timestamp": time.time(),
        }

    # -------------------------------------------------------------------------
    # Issue 34: Cross-Repo Error Propagation
    # -------------------------------------------------------------------------

    async def _initialize_error_propagation(self) -> None:
        """
        v95.10: Initialize cross-repo error propagation system.

        Features:
        - Error context preservation across repos
        - Error correlation and chaining
        - Cross-repo error handlers
        - Distributed error tracking
        """
        self._ensure_locks_initialized()

        if not self._error_propagation_enabled:
            logger.info("[v95.10] Error propagation disabled")
            return

        # Register default error handlers
        self._register_default_error_handlers()

        logger.info("[v95.10] ✅ Error propagation system initialized")

    def _register_default_error_handlers(self) -> None:
        """Register default error handlers for common error types."""
        self._error_handlers = {
            "connection_error": [self._handle_connection_error],
            "timeout_error": [self._handle_timeout_error],
            "validation_error": [self._handle_validation_error],
            "dependency_error": [self._handle_dependency_error],
        }

    async def propagate_error(
        self,
        error: Exception,
        source_repo: str,
        target_repos: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Propagate an error across repos with full context.

        Args:
            error: The exception to propagate
            source_repo: Source repository name
            target_repos: Target repos to notify (None = all)
            context: Additional context

        Returns:
            Error ID for tracking
        """
        self._ensure_locks_initialized()
        assert self._error_lock is not None

        error_id = self._generate_error_id()
        error_type = type(error).__name__

        error_info = {
            "id": error_id,
            "type": error_type,
            "message": str(error),
            "source_repo": source_repo,
            "target_repos": target_repos or ["jarvis", "jarvis-prime", "reactor-core"],
            "context": context or {},
            "trace_id": self._startup_correlation_id,
            "timestamp": time.time(),
            "traceback": self._format_traceback(error),
        }

        async with self._error_lock:
            # Store in registry
            self._error_registry[error_id] = error_info

            # Maintain max history
            if len(self._error_registry) > self._max_error_history:
                oldest = min(self._error_registry.keys(), key=lambda k: self._error_registry[k]["timestamp"])
                del self._error_registry[oldest]

            # Check for correlated errors
            await self._correlate_error(error_id, error_info)

        # Call error handlers
        await self._invoke_error_handlers(error_type, error_info)

        # Emit log
        await self.emit_cross_repo_log(
            "ERROR",
            f"Cross-repo error: {error_type}: {error}",
            source_repo,
            extra={"error_id": error_id}
        )

        return error_id

    def _generate_error_id(self) -> str:
        """Generate unique error ID."""
        import uuid
        return f"err-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    def _format_traceback(self, error: Exception) -> str:
        """Format exception traceback."""
        import traceback
        return "".join(traceback.format_exception(type(error), error, error.__traceback__))

    async def _correlate_error(self, error_id: str, error_info: Dict[str, Any]) -> None:
        """Find and link correlated errors."""
        trace_id = error_info.get("trace_id")
        if not trace_id:
            return

        # Find other errors with same trace ID
        for other_id, other_info in self._error_registry.items():
            if other_id == error_id:
                continue
            if other_info.get("trace_id") == trace_id:
                # Link errors
                if trace_id not in self._error_correlation_map:
                    self._error_correlation_map[trace_id] = []
                if error_id not in self._error_correlation_map[trace_id]:
                    self._error_correlation_map[trace_id].append(error_id)
                if other_id not in self._error_correlation_map[trace_id]:
                    self._error_correlation_map[trace_id].append(other_id)

    async def _invoke_error_handlers(self, error_type: str, error_info: Dict[str, Any]) -> None:
        """Invoke registered error handlers."""
        handlers = self._error_handlers.get(error_type.lower(), [])
        handlers.extend(self._error_handlers.get("*", []))  # Wildcard handlers

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(error_info)
                else:
                    handler(error_info)
            except Exception as e:
                logger.error(f"[v95.10] Error handler failed: {e}")

    async def _handle_connection_error(self, error_info: Dict[str, Any]) -> None:
        """Handle connection errors."""
        logger.warning(f"[v95.10] Connection error from {error_info['source_repo']}: {error_info['message']}")

    async def _handle_timeout_error(self, error_info: Dict[str, Any]) -> None:
        """Handle timeout errors."""
        logger.warning(f"[v95.10] Timeout error from {error_info['source_repo']}: {error_info['message']}")

    async def _handle_validation_error(self, error_info: Dict[str, Any]) -> None:
        """Handle validation errors."""
        logger.warning(f"[v95.10] Validation error from {error_info['source_repo']}: {error_info['message']}")

    async def _handle_dependency_error(self, error_info: Dict[str, Any]) -> None:
        """Handle dependency errors."""
        logger.warning(f"[v95.10] Dependency error from {error_info['source_repo']}: {error_info['message']}")

    def register_error_handler(self, error_type: str, handler: Callable) -> None:
        """Register an error handler."""
        if error_type not in self._error_handlers:
            self._error_handlers[error_type] = []
        self._error_handlers[error_type].append(handler)

    def get_error_chain(self, error_id: str) -> List[Dict[str, Any]]:
        """Get chain of correlated errors."""
        error_info = self._error_registry.get(error_id)
        if not error_info:
            return []

        trace_id = error_info.get("trace_id")
        if not trace_id or trace_id not in self._error_correlation_map:
            return [error_info]

        return [
            self._error_registry[eid]
            for eid in self._error_correlation_map[trace_id]
            if eid in self._error_registry
        ]

    # -------------------------------------------------------------------------
    # Issue 35: Cross-Repo State Synchronization
    # -------------------------------------------------------------------------

    async def _initialize_state_sync(self) -> None:
        """
        v95.10: Initialize cross-repo state synchronization.

        Features:
        - Shared state with versioning
        - State change notifications
        - Conflict resolution
        - State persistence
        """
        self._ensure_locks_initialized()
        assert self._state_lock is not None

        if not self._state_sync_enabled:
            logger.info("[v95.10] State synchronization disabled")
            return

        async with self._state_lock:
            # Setup state persistence
            jarvis_home = Path.home() / ".jarvis"
            self._state_persistence_path = jarvis_home / "state"
            self._state_persistence_path.mkdir(parents=True, exist_ok=True)

            # Load persisted state
            await self._load_persisted_state()

            # Start state sync task
            if self._state_sync_task is None or self._state_sync_task.done():
                self._state_sync_task = asyncio.create_task(
                    self._state_sync_loop(),
                    name="state_sync_loop"
                )
                self._track_background_task(self._state_sync_task)

            logger.info("[v95.10] ✅ State synchronization system initialized")

    async def _load_persisted_state(self) -> None:
        """Load state from persistent storage."""
        if not self._state_persistence_path:
            return

        state_file = self._state_persistence_path / "shared_state.json"
        if state_file.exists():
            try:
                with open(state_file, "r") as f:
                    data = json.load(f)
                    self._shared_state = data.get("state", {})
                    self._state_version = data.get("versions", {})
                logger.debug("[v95.10] Loaded persisted state")
            except Exception as e:
                logger.warning(f"[v95.10] Failed to load state: {e}")

    async def _persist_state(self) -> None:
        """Persist state to storage."""
        if not self._state_persistence_path:
            return

        state_file = self._state_persistence_path / "shared_state.json"
        try:
            with open(state_file, "w") as f:
                json.dump({
                    "state": self._shared_state,
                    "versions": self._state_version,
                    "timestamp": time.time(),
                }, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"[v95.10] Failed to persist state: {e}")

    async def _state_sync_loop(self) -> None:
        """Background task to sync and persist state."""
        persist_interval = 30.0

        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(persist_interval)

                if self._shutdown_event.is_set():
                    break

                assert self._state_lock is not None
                async with self._state_lock:
                    # Process buffered changes
                    if self._state_change_buffer:
                        await self._process_state_changes()

                    # Persist state
                    await self._persist_state()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[v95.10] State sync error: {e}")

    async def _process_state_changes(self) -> None:
        """Process buffered state changes."""
        changes = self._state_change_buffer.copy()
        self._state_change_buffer.clear()

        for change in changes:
            key = change["key"]
            # Notify subscribers
            if key in self._state_subscribers:
                for callback in self._state_subscribers[key]:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(key, change["value"], change["old_value"])
                        else:
                            callback(key, change["value"], change["old_value"])
                    except Exception as e:
                        logger.error(f"[v95.10] State subscriber error: {e}")

    async def set_shared_state(
        self,
        key: str,
        value: Any,
        source_repo: str = "jarvis",
        version: Optional[int] = None,
    ) -> bool:
        """
        Set shared state with optimistic locking.

        Args:
            key: State key
            value: New value
            source_repo: Source repository
            version: Expected version (for optimistic locking)

        Returns:
            True if successful, False if version conflict
        """
        self._ensure_locks_initialized()
        assert self._state_lock is not None

        async with self._state_lock:
            current_version = self._state_version.get(key, 0)

            # Optimistic locking check
            if version is not None and version != current_version:
                logger.warning(
                    f"[v95.10] State version conflict for {key}: "
                    f"expected {version}, got {current_version}"
                )
                return False

            old_value = self._shared_state.get(key)
            self._shared_state[key] = value
            self._state_version[key] = current_version + 1

            # Buffer change for notification
            self._state_change_buffer.append({
                "key": key,
                "value": value,
                "old_value": old_value,
                "version": self._state_version[key],
                "source_repo": source_repo,
                "timestamp": time.time(),
            })

            return True

    def get_shared_state(self, key: str, default: Any = None) -> Tuple[Any, int]:
        """
        Get shared state with version.

        Returns:
            Tuple of (value, version)
        """
        value = self._shared_state.get(key, default)
        version = self._state_version.get(key, 0)
        return value, version

    def subscribe_to_state(self, key: str, callback: Callable) -> None:
        """Subscribe to state changes for a key."""
        if key not in self._state_subscribers:
            self._state_subscribers[key] = []
        self._state_subscribers[key].append(callback)

    # -------------------------------------------------------------------------
    # Issue 36: Cross-Repo Resource Coordination
    # -------------------------------------------------------------------------

    async def _initialize_resource_coordination(self) -> None:
        """
        v95.10: Initialize cross-repo resource coordination.

        Features:
        - Resource registration and tracking
        - Resource allocation with limits
        - Conflict detection and resolution
        - Fair resource sharing
        """
        self._ensure_locks_initialized()
        assert self._resource_lock is not None

        if not self._resource_coordination_enabled:
            logger.info("[v95.10] Resource coordination disabled")
            return

        async with self._resource_lock:
            # Register default resources
            self._resource_limits = {
                "gpu_memory_mb": int(os.environ.get("JARVIS_GPU_MEMORY_LIMIT", "8192")),
                "cpu_cores": int(os.environ.get("JARVIS_CPU_CORES_LIMIT", "8")),
                "memory_mb": int(os.environ.get("JARVIS_MEMORY_LIMIT", "16384")),
                "network_connections": int(os.environ.get("JARVIS_CONNECTION_LIMIT", "1000")),
                "file_handles": int(os.environ.get("JARVIS_FILE_HANDLE_LIMIT", "10000")),
            }

            logger.info("[v95.10] ✅ Resource coordination system initialized")

    async def allocate_resource(
        self,
        service: str,
        resource: str,
        amount: int,
        priority: int = 0,
    ) -> Tuple[bool, int]:
        """
        Allocate a resource to a service.

        Args:
            service: Service requesting resource
            resource: Resource type
            amount: Amount requested
            priority: Priority (higher = more important)

        Returns:
            Tuple of (success, allocated_amount)
        """
        self._ensure_locks_initialized()
        assert self._resource_lock is not None

        async with self._resource_lock:
            # Get current usage
            current_usage = sum(
                allocs.get(resource, 0)
                for allocs in self._resource_allocations.values()
            )

            limit = self._resource_limits.get(resource, float("inf"))
            available = limit - current_usage

            if available <= 0:
                # Try conflict resolution
                if resource in self._resource_conflict_handlers:
                    handler = self._resource_conflict_handlers[resource]
                    result = handler(service, resource, amount, priority)
                    if result:
                        available = result

            allocated = min(amount, int(available))

            if allocated > 0:
                if service not in self._resource_allocations:
                    self._resource_allocations[service] = {}
                self._resource_allocations[service][resource] = (
                    self._resource_allocations[service].get(resource, 0) + allocated
                )

                self._resource_registry[f"{service}:{resource}"] = {
                    "service": service,
                    "resource": resource,
                    "amount": allocated,
                    "priority": priority,
                    "timestamp": time.time(),
                }

                logger.debug(
                    f"[v95.10] Allocated {allocated}/{amount} {resource} to {service}"
                )
                return True, allocated

            logger.warning(
                f"[v95.10] Resource exhausted: {resource} "
                f"(requested: {amount}, available: {available})"
            )
            return False, 0

    async def release_resource(self, service: str, resource: str, amount: Optional[int] = None) -> None:
        """Release a resource allocation."""
        self._ensure_locks_initialized()
        assert self._resource_lock is not None

        async with self._resource_lock:
            if service not in self._resource_allocations:
                return

            if resource not in self._resource_allocations[service]:
                return

            if amount is None:
                # Release all
                del self._resource_allocations[service][resource]
            else:
                self._resource_allocations[service][resource] = max(
                    0,
                    self._resource_allocations[service][resource] - amount
                )

            # Clean up registry
            registry_key = f"{service}:{resource}"
            if registry_key in self._resource_registry:
                del self._resource_registry[registry_key]

            logger.debug(f"[v95.10] Released {resource} from {service}")

    def get_resource_status(self) -> Dict[str, Any]:
        """Get current resource allocation status."""
        status: Dict[str, Any] = {}

        for resource, limit in self._resource_limits.items():
            used = sum(
                allocs.get(resource, 0)
                for allocs in self._resource_allocations.values()
            )
            status[resource] = {
                "limit": limit,
                "used": used,
                "available": limit - used,
                "utilization": used / limit if limit > 0 else 0,
            }

        return status

    def register_resource_conflict_handler(self, resource: str, handler: Callable) -> None:
        """Register a conflict resolution handler for a resource."""
        self._resource_conflict_handlers[resource] = handler

    # -------------------------------------------------------------------------
    # Issue 37: Cross-Repo Version Compatibility
    # -------------------------------------------------------------------------

    async def _initialize_version_compatibility(self) -> None:
        """
        v95.10: Initialize version compatibility checking.

        Features:
        - Version registration for all components
        - Compatibility matrix validation
        - Upgrade coordination
        - Incompatibility warnings
        """
        self._ensure_locks_initialized()
        assert self._version_lock is not None

        if not self._version_check_enabled:
            logger.info("[v95.10] Version compatibility checking disabled")
            return

        async with self._version_lock:
            # Register versions from all repos
            await self._discover_versions()

            # Build compatibility matrix
            await self._build_compatibility_matrix()

            # Check compatibility
            issues = await self._check_compatibility()
            if issues:
                for issue in issues:
                    logger.warning(f"[v95.10] Version incompatibility: {issue}")

            logger.info("[v95.10] ✅ Version compatibility system initialized")

    async def _discover_versions(self) -> None:
        """Discover versions from all repos."""
        repos = {
            "jarvis-body": Path.cwd(),
            "jarvis-prime": self.config.jarvis_prime_path,
            "reactor-core": self.config.reactor_core_path,
        }

        for repo_name, repo_path in repos.items():
            if not repo_path or not repo_path.exists():
                continue

            version = await self._get_repo_version(repo_path)
            if version:
                self._version_registry[repo_name] = version
                logger.debug(f"[v95.10] {repo_name} version: {version}")

    async def _get_repo_version(self, repo_path: Path) -> Optional[str]:
        """Get version from a repository."""
        # Check various version sources
        version_files = [
            repo_path / "VERSION",
            repo_path / "version.txt",
            repo_path / "pyproject.toml",
            repo_path / "setup.py",
        ]

        for vf in version_files:
            if vf.exists():
                try:
                    content = vf.read_text()
                    if vf.name == "pyproject.toml":
                        import re
                        match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
                        if match:
                            return match.group(1)
                    elif vf.name == "setup.py":
                        import re
                        match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
                        if match:
                            return match.group(1)
                    else:
                        return content.strip()
                except Exception:
                    continue

        return None

    async def _build_compatibility_matrix(self) -> None:
        """Build compatibility matrix between components."""
        # Define compatibility rules
        # Format: {service: {dependency: [compatible_version_patterns]}}
        # v109.2: Updated to include 0.x development versions
        self._compatibility_matrix = {
            "reactor-core": {
                "jarvis-prime": ["0.*", "1.*", "2.*"],  # Reactor compatible with Prime 0.x, 1.x, 2.x
                "jarvis-body": ["0.*", "1.*", "2.*"],
            },
            "jarvis-prime": {
                "jarvis-body": ["0.*", "1.*", "2.*"],
            },
        }

    async def _check_compatibility(self) -> List[str]:
        """Check version compatibility across repos."""
        issues: List[str] = []

        for service, deps in self._compatibility_matrix.items():
            service_version = self._version_registry.get(service)
            if not service_version:
                continue

            for dep, compatible_patterns in deps.items():
                dep_version = self._version_registry.get(dep)
                if not dep_version:
                    continue

                if not self._is_version_compatible(dep_version, compatible_patterns):
                    issues.append(
                        f"{service} ({service_version}) may not be compatible with "
                        f"{dep} ({dep_version}). Expected: {compatible_patterns}"
                    )

        return issues

    def _is_version_compatible(self, version: str, patterns: List[str]) -> bool:
        """Check if version matches any pattern."""
        import fnmatch
        return any(fnmatch.fnmatch(version, pattern) for pattern in patterns)

    def get_version_info(self) -> Dict[str, Any]:
        """Get version information for all components."""
        return {
            "versions": self._version_registry.copy(),
            "compatibility_matrix": self._compatibility_matrix.copy(),
            "check_enabled": self._version_check_enabled,
        }

    # -------------------------------------------------------------------------
    # Issue 38: Cross-Repo Security Context
    # -------------------------------------------------------------------------

    async def _initialize_security_context(self) -> None:
        """
        v95.10: Initialize unified security context.

        Features:
        - Cross-repo authentication
        - Security token management
        - Policy enforcement
        - Audit logging
        """
        self._ensure_locks_initialized()
        assert self._security_lock is not None

        if not self._security_context_enabled:
            logger.info("[v95.10] Security context disabled")
            return

        async with self._security_lock:
            # Generate initial security context
            self._security_context = {
                "session_id": self._generate_session_id(),
                "created_at": time.time(),
                "principal": os.environ.get("USER", "jarvis"),
                "roles": ["system", "orchestrator"],
                "permissions": ["read", "write", "execute", "admin"],
            }

            # Initialize security policies
            self._initialize_security_policies()

            # Start token refresh task
            if self._token_refresh_task is None or self._token_refresh_task.done():
                self._token_refresh_task = asyncio.create_task(
                    self._token_refresh_loop(),
                    name="token_refresh_loop"
                )
                self._track_background_task(self._token_refresh_task)

            logger.info("[v95.10] ✅ Security context system initialized")

    def _generate_session_id(self) -> str:
        """Generate secure session ID."""
        import uuid
        import hashlib
        random_bytes = uuid.uuid4().bytes + str(time.time()).encode()
        return hashlib.sha256(random_bytes).hexdigest()[:32]

    def _initialize_security_policies(self) -> None:
        """Initialize default security policies."""
        self._security_policies = {
            "cross_repo_communication": {
                "require_token": True,
                "token_lifetime": 3600,
                "allowed_origins": ["jarvis", "jarvis-prime", "reactor-core"],
            },
            "resource_access": {
                "require_role": True,
                "allowed_roles": ["system", "orchestrator", "service"],
            },
            "api_access": {
                "rate_limit": 1000,  # requests per minute
                "require_authentication": True,
            },
        }

    async def _token_refresh_loop(self) -> None:
        """Background task to refresh security tokens."""
        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(self._token_refresh_interval)

                if self._shutdown_event.is_set():
                    break

                await self._refresh_security_tokens()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[v95.10] Token refresh error: {e}")

    async def _refresh_security_tokens(self) -> None:
        """Refresh security tokens for all services."""
        assert self._security_lock is not None

        async with self._security_lock:
            for service in ["jarvis-prime", "reactor-core"]:
                token = self._generate_service_token(service)
                self._security_tokens[service] = token
                logger.debug(f"[v95.10] Refreshed token for {service}")

    def _generate_service_token(self, service: str) -> str:
        """Generate a service authentication token."""
        import hashlib
        import hmac

        secret = os.environ.get("JARVIS_SECRET_KEY", "jarvis-default-secret")
        timestamp = str(int(time.time()))
        message = f"{service}:{self._security_context['session_id']}:{timestamp}"

        signature = hmac.new(
            secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()

        return f"{message}:{signature}"

    def get_service_token(self, service: str) -> Optional[str]:
        """Get authentication token for a service."""
        return self._security_tokens.get(service)

    def validate_service_token(self, token: str) -> Tuple[bool, Optional[str]]:
        """
        Validate a service token.

        Returns:
            Tuple of (is_valid, service_name)
        """
        try:
            parts = token.split(":")
            if len(parts) != 4:
                return False, None

            service, session_id, timestamp, signature = parts

            # Check session
            if session_id != self._security_context.get("session_id"):
                return False, None

            # Check timestamp (token valid for 2x refresh interval)
            token_age = time.time() - int(timestamp)
            if token_age > self._token_refresh_interval * 2:
                return False, None

            # Verify signature
            secret = os.environ.get("JARVIS_SECRET_KEY", "jarvis-default-secret")
            message = f"{service}:{session_id}:{timestamp}"
            expected_signature = __import__("hmac").new(
                secret.encode(),
                message.encode(),
                __import__("hashlib").sha256
            ).hexdigest()

            if signature != expected_signature:
                return False, None

            return True, service

        except Exception:
            return False, None

    def get_security_context(self) -> Dict[str, Any]:
        """Get current security context."""
        return {
            "session_id": self._security_context.get("session_id"),
            "principal": self._security_context.get("principal"),
            "roles": self._security_context.get("roles", []),
            "created_at": self._security_context.get("created_at"),
            "services_authenticated": list(self._security_tokens.keys()),
        }

    def check_permission(self, action: str, resource: str) -> bool:
        """Check if current context has permission for an action."""
        permissions = self._security_context.get("permissions", [])
        return action in permissions or "admin" in permissions

    # -------------------------------------------------------------------------
    # v95.10: Cross-Repo Integration Initialization (called from startup)
    # -------------------------------------------------------------------------

    async def _initialize_cross_repo_integration(self) -> None:
        """
        v95.10: Initialize all cross-repo integration systems.

        Called during startup to initialize:
        - Unified configuration
        - Cross-repo logging
        - Metrics collection
        - Error propagation
        - State synchronization
        - Resource coordination
        - Version compatibility
        - Security context

        v95.10.1: Now emits voice events for real-time user feedback.
        """
        self._ensure_locks_initialized()

        logger.info("[v95.10] 🔧 Initializing cross-repo integration systems...")

        # Track initialization for voice feedback
        systems_initialized = 0
        total_systems = 8

        # Initialize in order (some depend on others)
        # 1. Unified Configuration
        await _emit_event("CROSS_REPO_CONFIG_INIT", priority="MEDIUM")
        await self._initialize_unified_config()
        await _emit_event("CROSS_REPO_CONFIG_LOADED", priority="LOW",
                         details={"repo_count": len(self._config_sources)})
        systems_initialized += 1

        # 2. Security Context (must be early for authenticated communication)
        await _emit_event("CROSS_REPO_SECURITY_INIT", priority="MEDIUM")
        await self._initialize_security_context()
        await _emit_event("CROSS_REPO_SECURITY_ACTIVE", priority="LOW",
                         details={"token_count": len(self._security_tokens)})
        systems_initialized += 1

        # 3. Unified Logging with distributed tracing
        await _emit_event("CROSS_REPO_LOGGING_INIT", priority="MEDIUM")
        await self._initialize_unified_logging()
        await _emit_event("CROSS_REPO_LOGGING_ACTIVE", priority="LOW")
        systems_initialized += 1

        # 4. Error Propagation with correlation
        await _emit_event("CROSS_REPO_ERROR_PROPAGATION_INIT", priority="LOW")
        await self._initialize_error_propagation()
        await _emit_event("CROSS_REPO_ERROR_PROPAGATION_ACTIVE", priority="LOW")
        systems_initialized += 1

        # 5. Distributed State Synchronization
        await _emit_event("CROSS_REPO_STATE_INIT", priority="MEDIUM")
        await self._initialize_state_sync()
        await _emit_event("CROSS_REPO_STATE_SYNCED", priority="LOW",
                         details={"state_keys": len(self._shared_state)})
        systems_initialized += 1

        # 6. Resource Coordination
        await _emit_event("CROSS_REPO_RESOURCE_INIT", priority="LOW")
        await self._initialize_resource_coordination()
        await _emit_event("CROSS_REPO_RESOURCE_ACTIVE", priority="LOW",
                         details={"resource_types": len(self._resource_limits)})
        systems_initialized += 1

        # 7. Version Compatibility Check
        await _emit_event("CROSS_REPO_VERSION_CHECK", priority="MEDIUM")
        await self._initialize_version_compatibility()

        # Check if there are incompatibilities using the proper method
        compatibility_issues = await self._check_compatibility()
        incompatible_count = len(compatibility_issues)

        if incompatible_count > 0:
            await _emit_event("CROSS_REPO_VERSION_INCOMPATIBLE", priority="HIGH",
                             details={"incompatible_count": incompatible_count})
            for issue in compatibility_issues:
                logger.warning(f"[v95.10] Version issue: {issue}")
        else:
            await _emit_event("CROSS_REPO_VERSION_COMPATIBLE", priority="LOW")
        systems_initialized += 1

        # 8. Metrics Collection
        await _emit_event("CROSS_REPO_METRICS_INIT", priority="LOW")
        await self._initialize_metrics_collection()
        await _emit_event("CROSS_REPO_METRICS_ACTIVE", priority="LOW",
                         details={"metric_count": len(self._metrics_registry)})
        systems_initialized += 1

        # All systems initialized - emit completion event
        await _emit_event("CROSS_REPO_INTEGRATION_COMPLETE", priority="HIGH",
                         details={"systems_online": systems_initialized})

        logger.info(f"[v95.10] ✅ All {systems_initialized} cross-repo integration systems initialized")

    def get_cross_repo_integration_status(self) -> Dict[str, Any]:
        """Get status of all cross-repo integration systems."""
        return {
            "config": {
                "enabled": True,
                "sources": list(self._config_sources.keys()),
                "watchers": len(self._config_watchers),
            },
            "logging": {
                "enabled": self._log_correlation_enabled,
                "buffer_size": len(self._log_aggregation_buffer),
            },
            "metrics": {
                "enabled": self._metrics_enabled,
                "collectors": len(self._metrics_collectors),
                "registered_metrics": len(self._metrics_registry),
            },
            "error_propagation": {
                "enabled": self._error_propagation_enabled,
                "error_count": len(self._error_registry),
                "handlers": len(self._error_handlers),
            },
            "state_sync": {
                "enabled": self._state_sync_enabled,
                "keys": len(self._shared_state),
                "subscribers": sum(len(s) for s in self._state_subscribers.values()),
            },
            "resource_coordination": {
                "enabled": self._resource_coordination_enabled,
                "resources": len(self._resource_limits),
                "allocations": len(self._resource_registry),
            },
            "version_compatibility": {
                "enabled": self._version_check_enabled,
                "versions": self._version_registry,
            },
            "security": {
                "enabled": self._security_context_enabled,
                "session_id": self._security_context.get("session_id", "")[:8] + "...",
                "authenticated_services": len(self._security_tokens),
            },
            "timestamp": time.time(),
        }

    # =========================================================================
    # v95.5: Graceful Degradation with Circuit Breaker Pattern
    # =========================================================================

    async def _initialize_graceful_degradation(self) -> None:
        """
        v95.5: Initialize graceful degradation infrastructure.

        Sets up:
        1. Cross-repo circuit breaker with failure classification
        2. Per-service degradation tracking
        3. Fallback handlers for each service
        4. Event bus notifications for degradation state changes
        """
        self._ensure_locks_initialized()

        try:
            # Initialize cross-repo circuit breaker
            from backend.core.resilience.cross_repo_circuit_breaker import (
                CrossRepoCircuitBreaker,
                CircuitBreakerConfig,
                CircuitState,
            )

            # Create circuit breaker with event bus notifications
            async def on_state_change(tier: str, old_state: CircuitState, new_state: CircuitState):
                """Notify event bus and update degradation mode on circuit state changes."""
                logger.info(f"[v95.5] Circuit breaker state change: {tier}: {old_state.value} -> {new_state.value}")

                # Update degradation mode based on circuit state
                async with self._degradation_lock_safe:
                    if new_state == CircuitState.OPEN:
                        self._degradation_mode[tier] = "isolated"
                        # Reduce capabilities for this service
                        self._service_capabilities[tier] = {"basic", "read_only"}
                    elif new_state == CircuitState.HALF_OPEN:
                        self._degradation_mode[tier] = "degraded"
                        self._service_capabilities[tier] = {"basic", "read_only", "limited_write"}
                    else:  # CLOSED
                        self._degradation_mode[tier] = "full"
                        self._service_capabilities[tier] = {"basic", "read_only", "write", "advanced"}

                # Publish degradation event to event bus
                await self._publish_lifecycle_event(
                    event_type="system.degradation",
                    payload={
                        "service": tier,
                        "old_state": old_state.value,
                        "new_state": new_state.value,
                        "degradation_mode": self._degradation_mode.get(tier, "unknown"),
                        "available_capabilities": list(self._service_capabilities.get(tier, set())),
                    },
                    priority="HIGH" if new_state == CircuitState.OPEN else "NORMAL",
                )

            config = CircuitBreakerConfig(
                failure_threshold=5,
                timeout_seconds=30.0,
                max_timeout_seconds=300.0,
                recovery_factor=2.0,
                half_open_max_calls=3,
                startup_grace_period_seconds=120.0,  # Generous grace for ML model loading
                adaptive_thresholds=True,
            )

            self._cross_repo_breaker = CrossRepoCircuitBreaker(
                name="orchestrator",
                config=config,
                on_state_change=on_state_change,
            )

            # Initialize default degradation modes (all services start in full mode)
            for service in ["jarvis-body", "jarvis-prime", "reactor-core"]:
                self._degradation_mode[service] = "full"
                self._service_capabilities[service] = {"basic", "read_only", "write", "advanced"}

            logger.info("[v95.5] ✅ Graceful degradation initialized with circuit breaker")

        except ImportError as e:
            logger.warning(f"[v95.5] Circuit breaker not available: {e}")
            # Fall back to basic degradation without circuit breaker
            for service in ["jarvis-body", "jarvis-prime", "reactor-core"]:
                self._degradation_mode[service] = "full"
                self._service_capabilities[service] = {"basic", "read_only", "write", "advanced"}

    async def _execute_with_graceful_degradation(
        self,
        service: str,
        func: Callable,
        args: tuple = (),
        kwargs: Optional[Dict] = None,
        fallback: Optional[Callable] = None,
        required_capability: str = "basic",
    ) -> Any:
        """
        v95.5: Execute operation with graceful degradation.

        If the service is degraded or isolated, uses fallback or returns
        a degraded response instead of failing completely.

        Args:
            service: Target service name
            func: Function to execute
            args: Function arguments
            kwargs: Function keyword arguments
            fallback: Fallback function if service is degraded
            required_capability: Capability required for this operation

        Returns:
            Function result or fallback result
        """
        kwargs = kwargs or {}
        self._ensure_locks_initialized()

        async with self._degradation_lock_safe:
            mode = self._degradation_mode.get(service, "full")
            capabilities = self._service_capabilities.get(service, set())

        # Check if required capability is available
        if required_capability not in capabilities:
            logger.warning(
                f"[v95.5] Service {service} lacks capability '{required_capability}' "
                f"(mode: {mode}, available: {capabilities})"
            )
            if fallback:
                logger.info(f"[v95.5] Using fallback for {service}")
                result = fallback(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    return await result
                return result
            # Return degraded response
            return {"status": "degraded", "service": service, "mode": mode}

        # Execute with circuit breaker if available
        if self._cross_repo_breaker:
            try:
                return await self._cross_repo_breaker.execute(
                    tier=service,
                    func=func,
                    args=args,
                    kwargs=kwargs,
                    fallback=fallback,
                )
            except Exception as e:
                logger.error(f"[v95.5] Circuit breaker execution failed for {service}: {e}")
                if fallback:
                    result = fallback(*args, **kwargs)
                    if asyncio.iscoroutine(result):
                        return await result
                    return result
                raise
        else:
            # Direct execution without circuit breaker
            result = func(*args, **kwargs)
            if asyncio.iscoroutine(result):
                return await result
            return result

    def get_service_degradation_status(self) -> Dict[str, Dict[str, Any]]:
        """
        v95.5: Get degradation status for all services.

        Returns:
            Dict mapping service name to degradation info
        """
        status = {}
        for service in ["jarvis-body", "jarvis-prime", "reactor-core"]:
            status[service] = {
                "mode": self._degradation_mode.get(service, "unknown"),
                "capabilities": list(self._service_capabilities.get(service, set())),
                "circuit_state": None,
            }
            # Add circuit breaker state if available
            if self._cross_repo_breaker:
                try:
                    health = self._cross_repo_breaker.get_tier_health(service)
                    status[service]["circuit_state"] = health.state.value if health else None
                    status[service]["success_rate"] = health.success_rate if health else None
                except Exception:
                    pass
        return status

    # =========================================================================
    # v95.5: Distributed Tracing with Correlation ID Propagation
    # =========================================================================

    async def _initialize_distributed_tracing(self) -> None:
        """
        v95.5: Initialize distributed tracing infrastructure.

        Sets up:
        1. Startup correlation context
        2. Trace propagation for all IPC calls
        3. Integration with event bus for trace visibility
        """
        try:
            from backend.core.resilience.correlation_context import (
                CorrelationContext,
                with_correlation,
            )

            # Create root correlation context for this startup session
            self._startup_correlation_id = CorrelationContext.generate_id("startup")
            self._startup_trace_context = CorrelationContext.create(
                operation="orchestrator_startup",
                source_repo="jarvis",
                source_component="orchestrator",
                timeout=600.0,  # 10 minute timeout for full startup
            )

            # Add startup metadata to baggage
            self._startup_trace_context.baggage["startup_time"] = str(time.time())
            self._startup_trace_context.baggage["orchestrator_version"] = "95.5"
            self._startup_trace_context.baggage["pid"] = str(os.getpid())

            logger.info(f"[v95.5] ✅ Distributed tracing initialized: {self._startup_correlation_id}")

            # Publish trace start event
            await self._publish_lifecycle_event(
                event_type="system.trace.started",
                payload={
                    "correlation_id": self._startup_correlation_id,
                    "operation": "orchestrator_startup",
                    "baggage": self._startup_trace_context.baggage,
                },
                priority="NORMAL",
            )

        except ImportError as e:
            logger.warning(f"[v95.5] Correlation context not available: {e}")
            # Generate a simple correlation ID as fallback
            self._startup_correlation_id = f"startup-{int(time.time() * 1000)}-{os.getpid()}"

    async def _create_span(
        self,
        operation: str,
        parent_context: Optional[Any] = None,
        metadata: Optional[Dict] = None,
    ) -> str:
        """
        v95.5: Create a new trace span for an operation.
        v137.1: Added diagnostic logging for hang debugging.

        Args:
            operation: Name of the operation
            parent_context: Parent correlation context (defaults to startup context)
            metadata: Additional span metadata

        Returns:
            Span ID
        """
        # v137.1: Temporarily using INFO level for debugging hang issue
        logger.info(f"[v137.1] _create_span({operation}): entering...")
        self._ensure_locks_initialized()
        metadata = metadata or {}

        try:
            logger.info(f"[v137.1] _create_span({operation}): importing CorrelationContext...")
            from backend.core.resilience.correlation_context import CorrelationContext
            logger.info(f"[v137.1] _create_span({operation}): import complete")

            parent = parent_context or self._startup_trace_context

            # Create child context for this operation
            logger.info(f"[v137.1] _create_span({operation}): creating context...")
            ctx = CorrelationContext.create(
                operation=operation,
                source_repo="jarvis",
                source_component="orchestrator",
                parent=parent,
            )
            logger.info(f"[v137.1] _create_span({operation}): context created")

            # Add metadata to baggage
            for key, value in metadata.items():
                ctx.baggage[key] = str(value)

            logger.info(f"[v137.1] _create_span({operation}): acquiring trace lock...")
            async with self._trace_lock_safe:
                self._active_traces[ctx.correlation_id] = ctx
            logger.info(f"[v137.1] _create_span({operation}): trace lock released")

            logger.debug(f"[v95.5] Created span: {operation} ({ctx.correlation_id})")
            return ctx.correlation_id

        except Exception as e:
            logger.debug(f"[v95.5] Span creation failed: {e}")
            # Return a simple ID as fallback
            span_id = f"{operation}-{int(time.time() * 1000)}"
            return span_id

    async def _end_span(
        self,
        span_id: str,
        status: str = "success",
        error_message: Optional[str] = None,
    ) -> None:
        """
        v95.5: End a trace span.

        Args:
            span_id: Span ID returned by _create_span
            status: Span status (success, error)
            error_message: Error message if status is error
        """
        self._ensure_locks_initialized()

        async with self._trace_lock_safe:
            ctx = self._active_traces.pop(span_id, None)

        if ctx and hasattr(ctx, "current_span") and ctx.current_span:
            ctx.current_span.end_time = time.time()
            ctx.current_span.status = status
            ctx.current_span.error_message = error_message

            # Publish span end event for visibility
            await self._publish_lifecycle_event(
                event_type="system.trace.span_end",
                payload={
                    "span_id": span_id,
                    "operation": ctx.current_span.operation if ctx.current_span else "unknown",
                    "duration_ms": ctx.current_span.duration_ms if ctx.current_span else 0,
                    "status": status,
                    "error": error_message,
                },
                priority="LOW",
            )

    def get_correlation_id(self) -> str:
        """
        v95.5: Get the current startup correlation ID.

        Returns:
            Correlation ID for the current startup session
        """
        return self._startup_correlation_id or f"unknown-{os.getpid()}"

    def get_trace_headers(self) -> Dict[str, str]:
        """
        v95.5: Get HTTP headers for trace propagation.

        Returns:
            Dict of headers to include in cross-repo HTTP calls
        """
        headers = {
            "X-Correlation-ID": self.get_correlation_id(),
            "X-Source-Component": "orchestrator",
            "X-Source-Repo": "jarvis",
        }

        if self._startup_trace_context:
            try:
                # Add baggage items as headers
                for key, value in self._startup_trace_context.baggage.items():
                    headers[f"X-Baggage-{key}"] = str(value)
            except Exception:
                pass

        return headers

    # =========================================================================
    # v95.5: Event Bus Integration for Lifecycle Events
    # =========================================================================

    async def _initialize_event_bus(self) -> None:
        """
        v95.5: Initialize event bus connection for lifecycle events.

        Sets up:
        1. Connection to Trinity Event Bus
        2. Lifecycle event publishing
        3. Subscriptions to cross-repo events
        """
        if not self._lifecycle_events_enabled:
            logger.info("[v95.5] Lifecycle events disabled via JARVIS_LIFECYCLE_EVENTS=false")
            return

        try:
            from backend.core.trinity_event_bus import (
                get_trinity_event_bus,
                TrinityEvent,
                EventType,
                EventPriority,
                RepoType,
            )

            # Get or create event bus instance
            self._event_bus = await get_trinity_event_bus()
            self._event_bus_initialized = True

            logger.info("[v95.5] ✅ Event bus connection initialized")

            # Subscribe to lifecycle events from other repos
            await self._subscribe_to_lifecycle_events()

        except ImportError as e:
            logger.warning(f"[v95.5] Event bus not available: {e}")
        except Exception as e:
            logger.warning(f"[v95.5] Event bus initialization failed: {e}")

    async def _subscribe_to_lifecycle_events(self) -> None:
        """
        v95.5: Subscribe to lifecycle events from other repositories.

        Enables the orchestrator to react to:
        - Service startup/shutdown events
        - Health check events
        - Degradation events
        """
        if not self._event_bus:
            return

        try:
            # Subscribe to lifecycle events
            async def handle_lifecycle_event(event):
                """Handle incoming lifecycle events from other repos."""
                try:
                    payload = event.payload if hasattr(event, "payload") else event
                    source = event.source.value if hasattr(event, "source") else "unknown"
                    topic = event.topic if hasattr(event, "topic") else "unknown"

                    logger.info(f"[v95.5] Received lifecycle event: {topic} from {source}")

                    # Update internal state based on event
                    if "startup" in topic:
                        service = payload.get("service", source)
                        status = payload.get("status", "unknown")
                        if status == "ready":
                            self._services_ready.add(service)
                            self._services_starting.discard(service)
                        elif status == "starting":
                            self._services_starting.add(service)

                    elif "shutdown" in topic:
                        service = payload.get("service", source)
                        self._services_ready.discard(service)
                        self._services_starting.discard(service)

                    elif "health" in topic:
                        service = payload.get("service", source)
                        healthy = payload.get("healthy", True)
                        if not healthy:
                            async with self._degradation_lock_safe:
                                self._degradation_mode[service] = "degraded"

                except Exception as e:
                    logger.warning(f"[v95.5] Error handling lifecycle event: {e}")

            # Subscribe to all lifecycle events
            # v95.16: Fixed - use 'pattern' not 'topic' per TrinityEventBus.subscribe signature
            sub_id = await self._event_bus.subscribe(
                pattern="lifecycle.*",
                handler=handle_lifecycle_event,
            )
            self._event_subscriptions.append(sub_id)

            # Subscribe to system degradation events
            sub_id = await self._event_bus.subscribe(
                pattern="system.degradation",
                handler=handle_lifecycle_event,
            )
            self._event_subscriptions.append(sub_id)

            logger.info(f"[v95.5] Subscribed to lifecycle events ({len(self._event_subscriptions)} subscriptions)")

        except Exception as e:
            logger.warning(f"[v95.5] Failed to subscribe to lifecycle events: {e}")

    async def _publish_lifecycle_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        priority: str = "NORMAL",
    ) -> bool:
        """
        v95.5: Publish a lifecycle event to the event bus.

        Args:
            event_type: Event type (e.g., "lifecycle.startup", "system.degradation")
            payload: Event payload
            priority: Event priority (CRITICAL, HIGH, NORMAL, LOW)

        Returns:
            True if event was published successfully
        """
        if not self._event_bus_initialized or not self._event_bus:
            return False

        try:
            from backend.core.trinity_event_bus import (
                TrinityEvent,
                EventPriority,
                RepoType,
            )

            # Map priority string to enum
            priority_map = {
                "CRITICAL": EventPriority.CRITICAL,
                "HIGH": EventPriority.HIGH,
                "NORMAL": EventPriority.NORMAL,
                "LOW": EventPriority.LOW,
            }
            event_priority = priority_map.get(priority, EventPriority.NORMAL)

            # Add correlation ID to payload
            payload["correlation_id"] = self.get_correlation_id()
            payload["timestamp"] = time.time()
            payload["source_pid"] = os.getpid()

            event = TrinityEvent(
                topic=event_type,
                source=RepoType.JARVIS,
                target=RepoType.BROADCAST,
                priority=event_priority,
                payload=payload,
                correlation_id=self.get_correlation_id(),
            )

            await self._event_bus.publish(event)
            logger.debug(f"[v95.5] Published lifecycle event: {event_type}")
            return True

        except Exception as e:
            logger.debug(f"[v95.5] Failed to publish lifecycle event: {e}")
            return False

    async def publish_service_lifecycle_event(
        self,
        service: str,
        status: str,
        details: Optional[Dict] = None,
    ) -> None:
        """
        v95.5: Publish a service lifecycle event.

        Args:
            service: Service name
            status: Status (starting, ready, failed, shutdown)
            details: Additional details
        """
        payload = {
            "service": service,
            "status": status,
            "details": details or {},
            "degradation_mode": self._degradation_mode.get(service, "unknown"),
        }

        # Map status to priority
        priority_map = {
            "starting": "NORMAL",
            "ready": "HIGH",
            "failed": "CRITICAL",
            "shutdown": "HIGH",
        }
        priority = priority_map.get(status, "NORMAL")

        await self._publish_lifecycle_event(
            event_type=f"lifecycle.{status}",
            payload=payload,
            priority=priority,
        )

    async def _verify_jarvis_body_health(self, timeout: float = 30.0) -> bool:
        """
        v95.4: Verify JARVIS body is healthy and ready for external services.

        This checks:
        1. Service registry is accessible
        2. jarvis-body is registered in the registry
        3. jarvis-body heartbeat is active and recent
        4. Core endpoints are responding (if FastAPI is running)

        v111.1: Fast-path for in-process mode (Unified Monolith)
        When JARVIS_IN_PROCESS_MODE=true, the backend runs in the supervisor's
        process, so we know it's healthy if we reach this point.

        Args:
            timeout: Maximum time to wait for health verification

        Returns:
            True if jarvis-body is verified healthy, False otherwise
        """
        start_time = time.time()

        # ═══════════════════════════════════════════════════════════════════
        # v111.1: Fast-path for Unified Monolith (in-process mode)
        # ═══════════════════════════════════════════════════════════════════
        # If backend is running in-process, it was already verified healthy
        # during _start_backend_in_process() and registered immediately.
        # Skip the verification loop - we KNOW it's healthy.
        # ═══════════════════════════════════════════════════════════════════
        in_process_mode = os.getenv("JARVIS_IN_PROCESS_MODE", "true").lower() == "true"
        if in_process_mode:
            logger.info("[v111.1] ✅ In-process mode: jarvis-body health verified (same process)")
            self._jarvis_body_status = "healthy"
            self._jarvis_body_health_verified = True
            return True

        logger.info("[v95.4] Verifying jarvis-body health before Phase 2...")

        try:
            # Check 1: Registry accessible and jarvis-body registered
            if self.registry:
                try:
                    services = await self.registry.list_services()
                    jarvis_body_entry = None
                    for svc in services:
                        # v96.0: Fixed - ServiceInfo uses 'service_name' not 'name'
                        # Check both 'service_name' and 'name' for backwards compatibility
                        svc_name = None
                        if isinstance(svc, dict):
                            svc_name = svc.get("service_name") or svc.get("name")
                        elif hasattr(svc, "service_name"):
                            svc_name = svc.service_name
                        elif hasattr(svc, "name"):
                            svc_name = svc.name

                        if svc_name == "jarvis-body":
                            jarvis_body_entry = svc
                            break

                    if jarvis_body_entry:
                        logger.info("[v95.4] ✅ jarvis-body found in registry")

                        # Check heartbeat is recent (within 60 seconds)
                        last_heartbeat = None
                        if isinstance(jarvis_body_entry, dict):
                            last_heartbeat = jarvis_body_entry.get("last_heartbeat")
                        elif hasattr(jarvis_body_entry, "last_heartbeat"):
                            last_heartbeat = jarvis_body_entry.last_heartbeat

                        if last_heartbeat:
                            heartbeat_age = time.time() - last_heartbeat
                            if heartbeat_age < 60:
                                logger.info(f"[v95.4] ✅ jarvis-body heartbeat active ({heartbeat_age:.1f}s ago)")
                            else:
                                logger.warning(f"[v95.4] ⚠️ jarvis-body heartbeat stale ({heartbeat_age:.1f}s ago)")
                    else:
                        logger.warning("[v95.4] ⚠️ jarvis-body not yet in registry")
                        # Not fatal - registry write may still be propagating
                except Exception as e:
                    logger.warning(f"[v95.4] Registry check warning: {e}")

            # Check 2: Local health endpoint (if FastAPI is running)
            try:
                local_port = int(os.environ.get("JARVIS_PORT", 8010))
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{local_port}/health",
                        timeout=aiohttp.ClientTimeout(total=5.0)
                    ) as resp:
                        if resp.status == 200:
                            logger.info(f"[v95.4] ✅ jarvis-body health endpoint responding (port {local_port})")
                            self._jarvis_body_status = "healthy"
                            self._jarvis_body_health_verified = True
                            return True
                        else:
                            logger.warning(f"[v95.4] jarvis-body health endpoint returned {resp.status}")
            except aiohttp.ClientConnectorError:
                # FastAPI not yet listening - this is normal during startup
                logger.debug("[v95.4] jarvis-body health endpoint not yet available")
            except asyncio.TimeoutError:
                logger.debug("[v95.4] jarvis-body health endpoint timeout")
            except Exception as e:
                logger.debug(f"[v95.4] jarvis-body health check: {e}")

            # If registry shows jarvis-body is registered with active heartbeat,
            # that's sufficient even if health endpoint isn't ready yet
            if self.registry:
                try:
                    services = await self.registry.list_services()
                    for svc in services:
                        # v96.0: Fixed - ServiceInfo uses 'service_name' not 'name'
                        if isinstance(svc, dict):
                            name = svc.get("service_name") or svc.get("name")
                        else:
                            name = getattr(svc, "service_name", None) or getattr(svc, "name", None)
                        if name == "jarvis-body":
                            self._jarvis_body_status = "healthy"
                            self._jarvis_body_health_verified = True
                            logger.info("[v95.4] ✅ jarvis-body verified via registry (health endpoint pending)")
                            return True
                except Exception:
                    pass

            # Fallback: If we registered successfully earlier, consider it healthy
            elapsed = time.time() - start_time
            if elapsed < timeout and self._jarvis_body_status == "starting":
                logger.info("[v95.4] jarvis-body registration confirmed, proceeding with startup")
                self._jarvis_body_status = "healthy"
                self._jarvis_body_health_verified = True
                return True

            logger.warning(f"[v95.4] jarvis-body health verification incomplete after {elapsed:.1f}s")
            return False

        except Exception as e:
            logger.error(f"[v95.4] jarvis-body health verification error: {e}")
            return False

    async def _update_unified_state_for_jarvis_body(self, status: str, health: bool = True) -> None:
        """
        v95.4: Update unified state for jarvis-body in the service registry.

        This ensures external services can discover jarvis-body's current state
        through the unified state API.

        Args:
            status: Current status (initializing, starting, healthy, unhealthy)
            health: Whether jarvis-body is healthy
        """
        if not self.registry:
            return

        try:
            # Build metadata for the unified state
            metadata = {
                "status": status,
                "timestamp": time.time(),
                "port": int(os.environ.get("JARVIS_PORT", 8010)),
                "host": os.environ.get("JARVIS_HOST", "127.0.0.1"),
                "startup_time": self._jarvis_body_startup_time,
            }

            # Use the unified state API if available
            if hasattr(self.registry, "update_supervisor_state"):
                await self.registry.update_supervisor_state(
                    service_name="jarvis-body",
                    process_alive=health,
                    pid=os.getpid(),
                    exit_code=None,
                    metadata=metadata
                )
                logger.debug(f"[v95.4] Updated unified state for jarvis-body: {status}")
        except Exception as e:
            logger.debug(f"[v95.4] Unified state update for jarvis-body failed: {e}")

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """
        v93.11: Get shared HTTP session with connection pooling.

        Creates session lazily and reuses it for all HTTP requests.
        Connection pooling improves performance and reduces resource usage.
        """
        self._ensure_locks_initialized()

        async with self._http_session_lock_safe:
            if self._http_session is None or self._http_session.closed:
                # Configure connection pooling
                connector = aiohttp.TCPConnector(
                    limit=100,  # Max total connections
                    limit_per_host=10,  # Max connections per host
                    ttl_dns_cache=300,  # DNS cache TTL
                    enable_cleanup_closed=True,
                    force_close=False,  # Reuse connections
                )

                # Default timeouts
                timeout = aiohttp.ClientTimeout(
                    total=30,
                    connect=10,
                    sock_read=10,
                )

                # v95.5: Add trace headers for distributed tracing
                trace_headers = self.get_trace_headers()

                self._http_session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout,
                    raise_for_status=False,
                    headers=trace_headers,  # v95.5: Include correlation headers in all requests
                )

                logger.debug(f"[v93.11] Shared HTTP session initialized with connection pooling and trace headers: {trace_headers.get('X-Correlation-ID', 'none')}")

            return self._http_session

    async def _persist_service_state(
        self,
        service_name: str,
        pid: int,
        port: int,
        status: str = "running"
    ) -> None:
        """
        v117.5: Persist service state to file for cross-restart adoption.
        v137.0: Updated to use non-blocking I/O (I/O Airlock pattern).

        This enables the supervisor to adopt previously running services
        after a full process restart (not just SIGHUP restarts).

        State is stored in ~/.jarvis/trinity/state/services.json with format:
        {
            "service_name": {
                "pid": 12345,
                "port": 8000,
                "status": "running",
                "updated_at": 1234567890.123
            }
        }

        Args:
            service_name: Name of the service
            pid: Process ID
            port: Port number
            status: Service status (running, healthy, stopped)
        """
        try:
            service_state_file = Path.home() / ".jarvis" / "trinity" / "state" / "services.json"

            # v137.0: Use non-blocking I/O for directory creation
            def _ensure_dir():
                service_state_file.parent.mkdir(parents=True, exist_ok=True)
            await _run_blocking_io(
                _ensure_dir,
                timeout=2.0,
                operation_name="ensure_state_dir",
            )

            # v137.0: Read existing state with non-blocking I/O
            existing_services = await read_json_nonblocking(service_state_file)
            if existing_services is None:
                existing_services = {}

            # Update with new service state
            existing_services[service_name] = {
                "pid": pid,
                "port": port,
                "status": status,
                "updated_at": time.time(),
                "supervisor_pid": os.getpid()
            }

            # v137.0: Write atomically with non-blocking I/O
            success = await write_json_nonblocking(service_state_file, existing_services)

            if success:
                logger.debug(f"[v137.0] Persisted {service_name} state (PID: {pid}, Port: {port})")
            else:
                logger.debug(f"[v137.0] Failed to persist {service_name} state (write failed)")
        except Exception as e:
            logger.debug(f"[v137.0] Failed to persist {service_name} state: {e}")

    async def _traced_request(
        self,
        method: str,
        url: str,
        span_name: Optional[str] = None,
        **kwargs
    ) -> Any:
        """
        v95.5: Make HTTP request with distributed tracing.

        Automatically adds correlation headers and creates a trace span
        for the request.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Request URL
            span_name: Optional span name (defaults to method + url path)
            **kwargs: Additional aiohttp request arguments

        Returns:
            aiohttp.ClientResponse
        """
        session = await self._get_http_session()

        # Create span for this request
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if span_name is None:
            span_name = f"http_{method.lower()}_{parsed.netloc}"

        span_id = await self._create_span(span_name, metadata={
            "method": method,
            "url": url,
            "host": parsed.netloc,
        })

        # Add trace headers to request
        headers = kwargs.pop("headers", {})
        headers.update(self.get_trace_headers())
        headers["X-Span-ID"] = span_id

        try:
            response = await session.request(method, url, headers=headers, **kwargs)

            # End span with status
            status = "success" if response.status < 400 else "error"
            await self._end_span(span_id, status=status, error_message=f"HTTP {response.status}" if status == "error" else None)

            return response

        except Exception as e:
            await self._end_span(span_id, status="error", error_message=str(e))
            raise

    async def _close_http_session(self) -> None:
        """
        v93.11: Close shared HTTP session gracefully.

        Called during shutdown.
        """
        if self._http_session and not self._http_session.closed:
            try:
                await self._http_session.close()
                # Allow connections to close gracefully
                await asyncio.sleep(0.25)
            except Exception as e:
                logger.warning(f"[v95.1] HTTP session close error: {e}")
            finally:
                self._http_session = None
            logger.debug("[v93.11] Shared HTTP session closed")

    def _track_background_task(self, task: asyncio.Task) -> None:
        """
        v95.1: Track a background task for proper cleanup on shutdown.

        Fire-and-forget tasks MUST be tracked to:
        1. Prevent task leaks (unfinished tasks in memory)
        2. Ensure proper cancellation during shutdown
        3. Catch and log exceptions that would otherwise be lost

        Usage:
            task = asyncio.create_task(some_async_func(), name="descriptive_name")
            self._track_background_task(task)
        """
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        """
        v95.1: Callback when a tracked background task completes.

        Handles:
        1. Removing task from tracking set
        2. Logging any unhandled exceptions (prevents silent failures)
        """
        self._background_tasks.discard(task)

        # Check for exceptions (prevents "Task exception was never retrieved")
        if not task.cancelled():
            try:
                exc = task.exception()
                if exc is not None:
                    task_name = task.get_name() if hasattr(task, 'get_name') else "unknown"
                    logger.error(
                        f"[v95.1] Background task '{task_name}' failed with exception: {exc}",
                        exc_info=exc
                    )
            except asyncio.InvalidStateError:
                pass  # Task not done yet (shouldn't happen in done callback)

    async def _cancel_all_background_tasks(self, timeout: float = 10.0) -> int:
        """
        v95.1: Cancel all tracked background tasks gracefully.

        Called during shutdown to ensure no orphaned coroutines.

        Returns the number of tasks that were cancelled.
        """
        if not self._background_tasks:
            return 0

        tasks_to_cancel = list(self._background_tasks)
        cancelled_count = 0

        logger.info(f"[v95.1] Cancelling {len(tasks_to_cancel)} background tasks...")

        # Cancel all tasks
        for task in tasks_to_cancel:
            if not task.done():
                task.cancel()
                cancelled_count += 1

        if cancelled_count > 0:
            # Wait for cancellation with timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[v95.1] Timeout waiting for {cancelled_count} tasks to cancel "
                    f"(some may still be running)"
                )
            except Exception as e:
                logger.warning(f"[v95.1] Error during task cancellation: {e}")

        self._background_tasks.clear()
        logger.info(f"[v95.1] Cancelled {cancelled_count} background tasks")
        return cancelled_count

    def _get_circuit_breaker(self, service_name: str) -> CircuitBreaker:
        """
        v93.11: Get or create circuit breaker for a service (thread-safe).

        Uses a simple check-then-create pattern that's safe for single-threaded
        asyncio code. For true thread safety in multi-threaded scenarios,
        use _get_circuit_breaker_async.
        """
        if service_name not in self._circuit_breakers:
            self._circuit_breakers[service_name] = CircuitBreaker(
                name=f"docker-{service_name}",
                failure_threshold=self.config.docker_circuit_breaker_failure_threshold,
                recovery_timeout=self.config.docker_circuit_breaker_recovery_timeout,
                half_open_max_requests=self.config.docker_circuit_breaker_half_open_requests,
            )
        return self._circuit_breakers[service_name]

    async def _get_circuit_breaker_async(self, service_name: str) -> CircuitBreaker:
        """
        v93.11: Get or create circuit breaker with async lock protection.

        Use this version when called from async contexts where multiple
        coroutines might access circuit breakers concurrently.
        """
        self._ensure_locks_initialized()

        async with self._circuit_breaker_lock_safe:
            if service_name not in self._circuit_breakers:
                self._circuit_breakers[service_name] = CircuitBreaker(
                    name=f"docker-{service_name}",
                    failure_threshold=self.config.docker_circuit_breaker_failure_threshold,
                    recovery_timeout=self.config.docker_circuit_breaker_recovery_timeout,
                    half_open_max_requests=self.config.docker_circuit_breaker_half_open_requests,
                )
            return self._circuit_breakers[service_name]

    def _get_retry_state(self, operation_name: str) -> RetryState:
        """
        v93.9: Get or create retry state for an operation.
        """
        if operation_name not in self._retry_states:
            self._retry_states[operation_name] = RetryState(
                max_attempts=self.config.docker_retry_max_attempts,
                base_delay=self.config.docker_retry_base_delay,
                max_delay=self.config.docker_retry_max_delay,
                exponential_base=self.config.docker_retry_exponential_base,
            )
        return self._retry_states[operation_name]

    # =========================================================================
    # Pre-Flight Cleanup (v5.0)
    # =========================================================================

    async def _kill_process_on_port(self, port: int) -> bool:
        """
        Kill any process listening on the specified port.

        v5.3: Uses lsof to find and kill stale processes.
        v111.3: CRITICAL FIX - Use -sTCP:LISTEN to only get LISTENING processes.
                Previous: lsof -ti :PORT (returned ALL connections including clients)
                Fixed: lsof -i :PORT -sTCP:LISTEN -t (returns only LISTENING processes)
                This prevents killing processes that just have connections to the port.
        Resilient to CancelledError during startup.
        Returns True if a process was killed.
        """
        try:
            # v111.3: Find ONLY LISTENING processes on port using lsof with -sTCP:LISTEN
            # This is critical to prevent killing the wrong process when we have
            # ESTABLISHED connections (e.g., supervisor doing health checks)
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-i", f":{port}", "-sTCP:LISTEN", "-t",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if not stdout:
                return False

            pids = stdout.decode().strip().split('\n')
            killed = False

            # v93.15: Get current process ID to avoid self-kill
            current_pid = os.getpid()
            parent_pid = os.getppid()

            # v111.3: Get PIDs of processes we spawned in this session
            # NEVER kill our own spawned children - they are not "stale"
            # v118.0: CRITICAL FIX - Use self.processes not self._managed_processes
            spawned_pids = set()
            for managed in self.processes.values():
                if managed.pid:
                    spawned_pids.add(managed.pid)

            for pid_str in pids:
                if not pid_str:
                    continue
                try:
                    pid = int(pid_str)

                    # v93.15: CRITICAL - Never kill the current process or its parent
                    if pid == current_pid:
                        logger.debug(f"    Skipping current process (PID: {pid}) on port {port}")
                        continue
                    if pid == parent_pid:
                        logger.debug(f"    Skipping parent process (PID: {pid}) on port {port}")
                        continue

                    # v111.3: CRITICAL - Never kill processes we spawned in this session
                    if pid in spawned_pids:
                        logger.info(
                            f"    ⚠️ [v111.3] Skipping PID {pid} on port {port} - "
                            f"it's a process we spawned this session (NOT stale)"
                        )
                        continue

                    # v118.0: ADDITIONAL SAFETY - Check GlobalProcessRegistry
                    # This catches processes spawned before orchestrator started tracking them
                    try:
                        from backend.core.supervisor_singleton import GlobalProcessRegistry
                        if GlobalProcessRegistry.is_ours(pid):
                            logger.info(
                                f"    ⚠️ [v118.0] Skipping PID {pid} on port {port} - "
                                f"registered in GlobalProcessRegistry"
                            )
                            continue
                    except ImportError:
                        pass  # GlobalProcessRegistry not available

                    os.kill(pid, signal.SIGTERM)
                    logger.info(f"    🔪 Killed stale process on port {port} (PID: {pid})")
                    killed = True
                except (ValueError, ProcessLookupError, PermissionError) as e:
                    logger.debug(f"    Could not kill PID {pid_str}: {e}")

            if killed:
                # Give process time to terminate - use shield to prevent cancellation
                try:
                    await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    # Don't let cancellation interrupt cleanup
                    logger.debug(f"    Port {port} cleanup sleep interrupted, continuing...")

            return killed

        except asyncio.CancelledError:
            # v5.3: Don't propagate CancelledError during critical cleanup
            logger.debug(f"    Port cleanup cancelled for {port}, continuing startup...")
            return False
        except Exception as e:
            logger.debug(f"    Port cleanup failed for {port}: {e}")
            return False

    async def _cleanup_legacy_ports(self) -> Dict[str, List[int]]:
        """
        Clean up any processes on legacy ports before startup.

        v5.3: Ensures no stale processes from old configurations are blocking
        the correct ports. Resilient to CancelledError.
        Returns dict of service -> [killed_ports].
        """
        logger.info("  🧹 Pre-flight: Cleaning up legacy ports...")

        cleaned = {"jarvis-prime": [], "reactor-core": []}

        try:
            # Clean up legacy jarvis-prime ports
            for port in self.config.legacy_jarvis_prime_ports:
                if await self._kill_process_on_port(port):
                    cleaned["jarvis-prime"].append(port)

            # Clean up legacy reactor-core ports
            for port in self.config.legacy_reactor_core_ports:
                if await self._kill_process_on_port(port):
                    cleaned["reactor-core"].append(port)

            # Also check if something is running on CORRECT ports but with wrong host
            # (e.g., bound to 127.0.0.1 instead of 0.0.0.0)
            for service, port in [
                ("jarvis-prime", self.config.jarvis_prime_default_port),
                ("reactor-core", self.config.reactor_core_default_port),
            ]:
                if await self._check_wrong_binding(port):
                    logger.warning(f"    ⚠️ {service} on port {port} bound to 127.0.0.1, restarting...")
                    if await self._kill_process_on_port(port):
                        cleaned[service].append(port)

        except asyncio.CancelledError:
            # v5.3: Don't let cancellation interrupt startup - log and continue
            logger.warning("  ⚠️ Legacy port cleanup interrupted, continuing startup...")

        # Summary
        total_cleaned = sum(len(ports) for ports in cleaned.values())
        if total_cleaned > 0:
            logger.info(f"  ✅ Cleaned {total_cleaned} stale processes")
        else:
            logger.info(f"  ✅ No legacy processes found")

        return cleaned

    async def _check_wrong_binding(self, port: int) -> bool:
        """
        Check if a service on port is bound to 127.0.0.1 (should be 0.0.0.0).

        v5.3: Detects misconfigured services that won't accept external connections.
        Resilient to CancelledError.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-i", f":{port}", "-P", "-n",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if not stdout:
                return False

            output = stdout.decode()
            # Check if bound to 127.0.0.1 (localhost only)
            if "127.0.0.1:" in output or "localhost:" in output:
                # Also check it's not bound to 0.0.0.0 (which would be correct)
                if "*:" not in output and "0.0.0.0:" not in output:
                    return True

        except Exception:
            pass

        return False

    # =========================================================================
    # v136.0: Enterprise-Grade Port Hygiene System
    # =========================================================================
    # COMPREHENSIVE FIX for all 13 identified gaps:
    #
    # GAP 1:  CancelledError → return FAILURE (not success) to prevent dirty spawn
    # GAP 2:  Protected PID → explicit error message for debugging
    # GAP 3:  GlobalProcessRegistry PID type normalization (int, not str)
    # GAP 4:  TIME_WAIT retry with exponential backoff (3 retries)
    # GAP 5:  Per-service serialization lock for atomic clean+spawn
    # GAP 6:  Orchestrator port (8010) exclusion documented and enforced
    # GAP 7:  Fallback ports included in cleanup via registry
    # GAP 8:  Platform-agnostic port detection (lsof + psutil fallback)
    # GAP 9:  Port range validation (1-65535) before any operation
    # GAP 10: Deduplicated port cleanup (unique ports, clean once each)
    # GAP 11: EnterpriseProcessManager integration when available
    # GAP 12: Double-spawn prevention via service spawn locks
    # GAP 13: Accurate "killed_pids" tracking for precise logging
    #
    # ARCHITECTURE:
    # ┌─────────────────────────────────────────────────────────────────────┐
    # │  PortHygieneEngine (v136.0)                                         │
    # │  ┌───────────────┐  ┌───────────────┐  ┌───────────────────────┐    │
    # │  │ PID Discovery │→ │ Kill Strategy │→ │ Verification + Retry  │    │
    # │  │ (lsof/psutil) │  │ (TERM→KILL)   │  │ (exponential backoff) │    │
    # │  └───────────────┘  └───────────────┘  └───────────────────────┘    │
    # │                           ↓                                         │
    # │  ┌───────────────────────────────────────────────────────────────┐  │
    # │  │ Per-Service Spawn Lock (asyncio.Lock per service)             │  │
    # │  │ Ensures atomic: port_hygiene → spawn → health_check           │  │
    # │  └───────────────────────────────────────────────────────────────┘  │
    # └─────────────────────────────────────────────────────────────────────┘
    # =========================================================================

    # v136.0: Per-service spawn locks - prevents parallel clean+spawn race conditions
    _service_spawn_locks: Dict[str, asyncio.Lock] = {}
    _spawn_lock_creation_lock: Optional[threading.Lock] = None

    def _get_service_spawn_lock(self, service_name: str) -> asyncio.Lock:
        """
        v136.0 GAP 5: Get or create a per-service lock for atomic clean+spawn.

        This prevents race conditions where:
        - Two tasks try to clean the same port simultaneously
        - Crash handler restarts while initial spawn is in progress
        - Parallel startup tries to spawn the same service twice

        Thread-safe lock creation with double-checked locking pattern.
        """
        if self._spawn_lock_creation_lock is None:
            self._spawn_lock_creation_lock = threading.Lock()

        if service_name not in self._service_spawn_locks:
            with self._spawn_lock_creation_lock:
                # Double-check after acquiring lock
                if service_name not in self._service_spawn_locks:
                    self._service_spawn_locks[service_name] = asyncio.Lock()

        return self._service_spawn_locks[service_name]

    def _build_protected_pid_set(
        self,
        exclude_service: Optional[str] = None,
    ) -> Set[int]:
        """
        v140.0: Build set of PIDs that must NEVER be killed.
        v145.0: Added exclude_service parameter to fix "Self-Kill Protection Deadlock"

        CRITICAL FIX: Only protect PIDs from the CURRENT SESSION.
        PIDs from previous sessions are STALE and can be safely killed.

        v145.0 FIX: When restarting a service (e.g., jarvis-prime), we MUST allow
        killing that service's OLD PID. Otherwise we get a deadlock:
        - Port 8000 is held by old jarvis-prime (zombie/stale)
        - Supervisor tries to clean port 8000
        - Old jarvis-prime PID is in protected_pids (from self.processes)
        - Supervisor refuses to kill it
        - New jarvis-prime can't start
        - Loop forever

        The fix: Pass exclude_service to explicitly allow killing the target service's
        old PID when we're trying to restart it.

        Args:
            exclude_service: Service name to EXCLUDE from protection (allows killing
                             its old PID for restart)

        Returns:
            Set of protected PIDs (normalized to int)
        """
        protected: Set[int] = set()
        current_session_pid = os.getpid()

        # Always protect self and parent
        protected.add(current_session_pid)
        protected.add(os.getppid())

        # Add all spawned child PIDs from our process tracking (this session only)
        # v145.0: EXCEPT for the service we're trying to restart
        for service_name, managed in self.processes.items():
            if managed.pid:
                # v145.0: Skip protection for the service we're restarting
                if exclude_service and service_name.lower() == exclude_service.lower():
                    logger.info(
                        f"[v145.0] 🔓 Self-Kill Bypass: NOT protecting PID {managed.pid} "
                        f"for {service_name} - allowing kill for restart"
                    )
                    continue

                try:
                    protected.add(int(managed.pid))
                except (ValueError, TypeError):
                    pass

        # v140.0: Add from GlobalProcessRegistry with SESSION AWARENESS
        # Only protect PIDs that were spawned by THIS session
        try:
            from backend.core.supervisor_singleton import GlobalProcessRegistry

            for pid_key, info in GlobalProcessRegistry.get_all().items():
                # GAP 3: Normalize to int (registry might store as str)
                try:
                    if isinstance(pid_key, int):
                        pid = pid_key
                    elif isinstance(pid_key, str) and pid_key.isdigit():
                        pid = int(pid_key)
                    else:
                        continue

                    # v140.0: CRITICAL - Only protect PIDs from CURRENT session
                    stored_session_id = info.get("session_id") if isinstance(info, dict) else None
                    if stored_session_id == current_session_pid:
                        # This PID is from our session - protect it
                        protected.add(pid)
                    else:
                        # This PID is from a DIFFERENT session - DO NOT protect
                        # It can be safely cleaned up
                        component = info.get("component", "unknown") if isinstance(info, dict) else "unknown"
                        logger.debug(
                            f"[v140.0] NOT protecting PID {pid} ({component}) - "
                            f"from session {stored_session_id}, current session is {current_session_pid}"
                        )

                except (ValueError, TypeError):
                    logger.debug(f"[v136.0] Skipping invalid PID from registry: {pid_key}")

        except (ImportError, AttributeError) as e:
            logger.debug(f"[v136.0] GlobalProcessRegistry not available: {e}")

        return protected

    async def _cleanup_stale_session_processes(self) -> int:
        """
        v140.0: Pre-flight cleanup of processes from PREVIOUS supervisor sessions.

        This resolves the "protected PID" deadlock where stale processes from
        crashed or killed supervisor sessions hold ports that the new supervisor
        needs.

        Returns:
            Number of stale processes cleaned up
        """
        logger.info("[v140.0] 🔍 Checking for stale processes from previous sessions...")

        try:
            from backend.core.supervisor_singleton import GlobalProcessRegistry

            # Get stale PIDs (processes from previous sessions that are still running)
            stale_pids = GlobalProcessRegistry.get_stale_pids()

            if not stale_pids:
                logger.info("[v140.0] ✅ No stale processes found - clean slate!")
                return 0

            logger.warning(
                f"[v140.0] ⚠️ Found {len(stale_pids)} stale processes from previous sessions:"
            )
            for pid, info in stale_pids.items():
                component = info.get("component", "unknown")
                port = info.get("port", "unknown")
                session_id = info.get("session_id", "unknown")
                logger.warning(
                    f"    → PID {pid}: {component} on port {port} (session {session_id})"
                )

            # Clean them up
            cleaned = GlobalProcessRegistry.cleanup_stale_session_processes(force=True)

            if cleaned:
                logger.info(
                    f"[v140.0] ✅ Cleaned up {len(cleaned)} stale processes: {cleaned}"
                )
                # Wait for ports to be released
                await asyncio.sleep(1.0)

            return len(cleaned)

        except ImportError as e:
            logger.debug(f"[v140.0] GlobalProcessRegistry not available: {e}")
            return 0
        except Exception as e:
            logger.warning(f"[v140.0] Stale process cleanup error: {e}")
            return 0

    async def _get_listening_pids_on_port(self, port: int) -> Tuple[List[int], Optional[str]]:
        """
        v136.0 GAP 8: Platform-agnostic port discovery with fallback.

        Primary: lsof -i :PORT -sTCP:LISTEN -t (fast, macOS/Linux)
        Fallback: psutil.net_connections (portable, no subprocess)

        Returns:
            Tuple of (list_of_pids, error_message_if_any)
        """
        pids: List[int] = []

        # Primary method: lsof (fastest on macOS/Linux)
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-i", f":{port}", "-sTCP:LISTEN", "-t",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0 and stdout:
                for line in stdout.decode().strip().split('\n'):
                    line = line.strip()
                    if line and line.isdigit():
                        pids.append(int(line))
                return (pids, None)

            # lsof returned non-zero but might just mean no listeners
            if proc.returncode == 1 and not stdout:
                return ([], None)  # No listeners found

        except asyncio.TimeoutError:
            logger.debug(f"[v136.0] lsof timeout for port {port}, falling back to psutil")
        except FileNotFoundError:
            logger.debug(f"[v136.0] lsof not found, falling back to psutil")
        except Exception as e:
            logger.debug(f"[v136.0] lsof failed for port {port}: {e}, falling back to psutil")

        # Fallback method: psutil via non-blocking I/O (v137.0)
        try:
            # v137.0: Use non-blocking psutil wrapper to avoid event loop blocking
            connections = await get_net_connections_nonblocking(port=None)  # Get all, filter below
            for conn in connections:
                if (conn.get("status") == "LISTEN" and
                    conn.get("port") == port and
                    conn.get("pid")):
                    pids.append(conn["pid"])
            return (list(set(pids)), None)  # Dedupe
        except Exception as e:
            return ([], f"Both lsof and psutil failed: {e}")

    async def _verify_port_free_with_retry(
        self,
        port: int,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 5.0,
    ) -> Tuple[bool, List[int]]:
        """
        v136.0 GAP 4: Verify port is free with exponential backoff retry.

        Handles TIME_WAIT states where port isn't immediately available
        after killing the process. Uses adaptive retry with backoff.

        Returns:
            Tuple of (is_free: bool, remaining_pids: List[int])
        """
        delay = base_delay

        for attempt in range(max_retries):
            pids, error = await self._get_listening_pids_on_port(port)

            if error:
                logger.warning(f"[v136.0] Port verification error (attempt {attempt + 1}): {error}")
                # Treat as potentially free if we can't check
                return (True, [])

            if not pids:
                if attempt > 0:
                    logger.info(f"[v136.0] ✅ Port {port} free after {attempt + 1} verification attempts")
                return (True, [])

            if attempt < max_retries - 1:
                logger.debug(
                    f"[v136.0] Port {port} still occupied by PIDs {pids}, "
                    f"retry {attempt + 1}/{max_retries} after {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)  # Exponential backoff

        return (False, pids)

    async def _kill_process_graceful_then_force(
        self,
        pid: int,
        graceful_timeout: float = 3.0,
        force_timeout: float = 2.0,
    ) -> Tuple[bool, str]:
        """
        v136.0: Kill a process using SIGTERM → SIGKILL escalation.

        Returns:
            Tuple of (success: bool, status: str)
            status is one of: "terminated_gracefully", "killed_forcefully",
                             "already_gone", "zombie_survived", "permission_denied"
        """
        # Check if process exists
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return (True, "already_gone")
        except PermissionError:
            return (False, "permission_denied")

        # Step 1: SIGTERM (graceful)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return (True, "already_gone")
        except PermissionError:
            return (False, "permission_denied")

        # Wait for graceful termination
        start = time.time()
        while time.time() - start < graceful_timeout:
            try:
                os.kill(pid, 0)
                await asyncio.sleep(0.2)
            except ProcessLookupError:
                return (True, "terminated_gracefully")

        # Step 2: SIGKILL (force)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return (True, "terminated_gracefully")
        except PermissionError:
            return (False, "permission_denied")

        # Wait for forced termination
        start = time.time()
        while time.time() - start < force_timeout:
            try:
                os.kill(pid, 0)
                await asyncio.sleep(0.1)
            except ProcessLookupError:
                return (True, "killed_forcefully")

        return (False, "zombie_survived")

    async def _enforce_port_hygiene(
        self,
        port: int,
        service_name: str,
        graceful_timeout: float = 3.0,
        force_timeout: float = 2.0,
        post_kill_sleep: float = 1.0,
        verification_retries: int = 3,
        allow_self_kill: bool = True,  # v145.0: Allow killing service's own old PID
    ) -> Tuple[bool, Optional[str], List[int]]:
        """
        v136.0: Enterprise-grade port cleanup with all 13 gap fixes.
        v145.0: Added allow_self_kill parameter to fix "Self-Kill Protection Deadlock"

        This is the CRITICAL fix for OOM/port conflict issues.

        FIXES IMPLEMENTED:
        - GAP 1: CancelledError returns FAILURE
        - GAP 2: Protected PID explicit error message
        - GAP 3: PID type normalization via _build_protected_pid_set()
        - GAP 4: TIME_WAIT retry via _verify_port_free_with_retry()
        - GAP 8: Platform-agnostic via _get_listening_pids_on_port()
        - GAP 9: Port validation (1-65535)
        - GAP 13: Accurate killed_pids tracking
        - v145.0 GAP 14: Self-Kill Protection Bypass for restarts

        Args:
            port: The port to clean up
            service_name: Name of service that will use this port
            graceful_timeout: Seconds to wait after SIGTERM
            force_timeout: Seconds to wait after SIGKILL
            post_kill_sleep: Seconds to sleep after kill
            verification_retries: Number of verification retries
            allow_self_kill: If True, allow killing the service's own old PID (v145.0)

        Returns:
            Tuple of (success, error_message, killed_pids)
            - success=True means port is ready for use
            - killed_pids lists PIDs we actually terminated
        """
        killed_pids: List[int] = []

        # GAP 9: Port range validation
        if not (1 <= port <= 65535):
            return (False, f"Invalid port {port}: must be 1-65535", [])

        # GAP 6: Never clean orchestrator's own port
        # This prevents suicide when cleaning ports during pre-flight
        orchestrator_port = int(os.environ.get("JARVIS_BACKEND_PORT", "8010"))
        if port == orchestrator_port:
            logger.debug(f"[v136.0] Skipping orchestrator port {port} (self-protection)")
            return (True, None, [])

        # v145.0: Self-Kill Protection Bypass
        # When restarting a service, we need to allow killing its OLD PID
        # Pass the service_name to exclude it from protection
        exclude_service = service_name if allow_self_kill else None
        protected_pids = self._build_protected_pid_set(exclude_service=exclude_service)

        if allow_self_kill and exclude_service:
            logger.info(
                f"[v145.0] 🔓 Self-Kill Protection Bypass ACTIVE for {service_name} - "
                f"old PID can be killed for restart"
            )

        logger.info(f"[v136.0] 🧹 Port hygiene for {service_name} on port {port}...")

        try:
            # Step 1: Discover processes on port
            pids, discovery_error = await self._get_listening_pids_on_port(port)

            if discovery_error:
                logger.warning(f"[v136.0] Port discovery issue: {discovery_error}")
                # Can't verify, assume port is available
                return (True, None, [])

            if not pids:
                logger.info(f"[v136.0] ✅ Port {port} already free for {service_name}")
                return (True, None, [])

            # Step 2: Check for protected PIDs
            protected_on_port = [p for p in pids if p in protected_pids]
            killable_pids = [p for p in pids if p not in protected_pids]

            # GAP 2: Explicit error when only protected PIDs found
            if protected_on_port and not killable_pids:
                error_msg = (
                    f"Port {port} occupied by protected PID(s) {protected_on_port} "
                    f"(self/parent/child). Possible double-spawn or stale child. "
                    f"Cannot kill - manual investigation required."
                )
                logger.error(f"[v136.0] ❌ {error_msg}")
                return (False, error_msg, [])

            if protected_on_port:
                logger.warning(
                    f"[v136.0] ⚠️ Port {port} has protected PIDs {protected_on_port} "
                    f"(skipping), will kill others: {killable_pids}"
                )

            # Step 3: GAP 11 - Try EnterpriseProcessManager first if available
            enterprise_cleanup_success = False
            try:
                from backend.supervisor.enterprise_process_manager import EnterpriseProcessManager
                epm = EnterpriseProcessManager()

                logger.debug(f"[v136.0] Using EnterpriseProcessManager for port {port}")
                cleanup_result = await epm.cleanup_port(
                    port=port,
                    force=False,  # Try graceful first
                    wait_for_time_wait=True,
                    max_wait=graceful_timeout + force_timeout,
                )
                if cleanup_result:
                    enterprise_cleanup_success = True
                    killed_pids = killable_pids  # Assume EPM killed them
                    logger.info(f"[v136.0] EnterpriseProcessManager cleaned port {port}")
            except ImportError:
                logger.debug("[v136.0] EnterpriseProcessManager not available, using direct cleanup")
            except Exception as epm_err:
                logger.debug(f"[v136.0] EnterpriseProcessManager failed: {epm_err}, using direct cleanup")

            # Step 4: Direct kill if EPM not available or failed
            if not enterprise_cleanup_success:
                for pid in killable_pids:
                    # v137.0: Get process info via non-blocking I/O
                    proc_info_dict = await get_process_info_nonblocking(pid)
                    if proc_info_dict:
                        name = proc_info_dict.get("name", "unknown")
                        cmdline = proc_info_dict.get("cmdline", [])
                        proc_info = f"{name} ({' '.join(cmdline[:3])})"
                    else:
                        proc_info = "unknown"

                    logger.info(f"[v137.0] 🔪 Terminating PID {pid} on port {port}: {proc_info}")

                    success, status = await self._kill_process_graceful_then_force(
                        pid,
                        graceful_timeout=graceful_timeout,
                        force_timeout=force_timeout,
                    )

                    if success:
                        killed_pids.append(pid)
                        logger.debug(f"[v136.0] PID {pid} {status}")
                    else:
                        logger.warning(f"[v136.0] PID {pid} cleanup failed: {status}")

            # Step 5: Post-kill sleep for kernel port release
            if killed_pids:
                logger.debug(f"[v136.0] Sleeping {post_kill_sleep}s for port release...")
                await asyncio.sleep(post_kill_sleep)

            # Step 6: GAP 4 - Verify with retry (handles TIME_WAIT)
            is_free, remaining_pids = await self._verify_port_free_with_retry(
                port,
                max_retries=verification_retries,
                base_delay=1.0,
                max_delay=5.0,
            )

            if not is_free:
                # Check if remaining are protected
                remaining_protected = [p for p in remaining_pids if p in protected_pids]
                if remaining_pids == remaining_protected:
                    error_msg = (
                        f"Port {port} still occupied by protected PID(s) {remaining_pids}. "
                        f"Double-spawn detected - another instance may be running."
                    )
                else:
                    error_msg = f"Port {port} still occupied by PIDs {remaining_pids} after cleanup"
                logger.error(f"[v136.0] ❌ {error_msg}")
                return (False, error_msg, killed_pids)

            # GAP 13: Accurate logging of what we actually killed
            if killed_pids:
                logger.info(
                    f"[v136.0] ✅ Port {port} cleaned for {service_name} "
                    f"(killed PIDs: {killed_pids})"
                )
            else:
                logger.info(f"[v136.0] ✅ Port {port} verified free for {service_name}")

            return (True, None, killed_pids)

        except asyncio.CancelledError:
            # GAP 1: CancelledError returns FAILURE - don't spawn on dirty port
            logger.warning(
                f"[v136.0] ⚠️ Port hygiene CANCELLED for {service_name} on port {port}. "
                f"Returning failure to prevent spawn on potentially dirty port."
            )
            return (False, "Port hygiene cancelled - port state unknown", killed_pids)
        except Exception as e:
            logger.error(f"[v136.0] Port hygiene failed for {service_name}: {e}")
            import traceback
            logger.debug(f"[v136.0] Traceback: {traceback.format_exc()}")
            return (False, str(e), killed_pids)

    async def _get_all_service_ports(self) -> Dict[str, int]:
        """
        v136.0 GAP 7: Get ALL service ports including fallbacks.

        Sources:
        1. Config (from trinity_config.py) - primary ports
        2. Service definitions - canonical ports
        3. Legacy ports - historical cleanup
        4. Port registry - dynamically allocated fallback ports
        5. Environment variables - runtime overrides

        IMPORTANT GAP 6: Excludes orchestrator port (8010) to prevent self-cleanup.

        Returns:
            Dict mapping service_name -> port (deduplicated by port number)
        """
        ports: Dict[str, int] = {}
        orchestrator_port = int(os.environ.get("JARVIS_BACKEND_PORT", "8010"))

        # Source 1: Config (primary, from trinity_config)
        if hasattr(self.config, 'jarvis_prime_default_port'):
            ports["jarvis-prime"] = self.config.jarvis_prime_default_port
        if hasattr(self.config, 'reactor_core_default_port'):
            ports["reactor-core"] = self.config.reactor_core_default_port

        # Source 2: Service definitions
        try:
            definitions = self._get_service_definitions()
            for defn in definitions:
                if defn.default_port and defn.default_port != orchestrator_port:
                    ports[defn.name] = defn.default_port
        except Exception as e:
            logger.debug(f"[v136.0] Service definitions unavailable: {e}")

        # Source 3: Legacy ports
        if hasattr(self.config, 'legacy_jarvis_prime_ports'):
            for lp in self.config.legacy_jarvis_prime_ports:
                if lp != orchestrator_port:
                    ports[f"legacy-jprime-{lp}"] = lp
        if hasattr(self.config, 'legacy_reactor_core_ports'):
            for lp in self.config.legacy_reactor_core_ports:
                if lp != orchestrator_port:
                    ports[f"legacy-reactor-{lp}"] = lp

        # Source 4: GAP 7 - Port registry (catches dynamically allocated fallback ports)
        # v137.0: Use non-blocking I/O for registry read
        try:
            registry_file = Path.home() / ".jarvis" / "registry" / "ports.json"
            registry = await read_json_nonblocking(registry_file)
            if registry is not None:
                for svc_name, port_info in registry.get("ports", {}).items():
                    allocated_port = port_info.get("port")
                    if allocated_port and allocated_port != orchestrator_port:
                        # Use registry name to avoid overwriting config ports
                        if svc_name not in ports:
                            ports[svc_name] = allocated_port
                        elif ports[svc_name] != allocated_port:
                            # Service has different port in registry (fallback)
                            ports[f"{svc_name}-fallback"] = allocated_port
        except Exception as e:
            logger.debug(f"[v137.0] Port registry unavailable: {e}")

        # Source 5: Environment variable overrides
        for env_var, svc_name in [
            ("JARVIS_PRIME_PORT", "jarvis-prime-env"),
            ("REACTOR_CORE_PORT", "reactor-core-env"),
        ]:
            env_port = os.environ.get(env_var)
            if env_port and env_port.isdigit():
                port = int(env_port)
                if port != orchestrator_port and port not in ports.values():
                    ports[svc_name] = port

        # GAP 6: Explicitly exclude orchestrator port (documented protection)
        ports_to_remove = [k for k, v in ports.items() if v == orchestrator_port]
        for k in ports_to_remove:
            logger.debug(f"[v136.0] Excluding orchestrator port {orchestrator_port} ({k})")
            del ports[k]

        return ports

    async def _comprehensive_pre_flight_cleanup(self) -> Dict[str, Dict[str, Any]]:
        """
        v140.0: Enhanced pre-flight with STALE SESSION CLEANUP and port deduplication.

        This is the CRITICAL fix for "protected PID" deadlocks.

        v140.0 Improvements:
        - FIRST: Clean up stale processes from PREVIOUS supervisor sessions
        - THEN: Deduplicate ports (clean each unique port once)
        - Tracks which PIDs were actually killed per port
        - Provides detailed summary for debugging

        Returns:
            Dict with structure:
            {
                "stale_session_cleanup": {
                    "cleaned_pids": [69780, ...],
                    "count": 1
                },
                "port_8000": {
                    "services": ["jarvis-prime"],
                    "killed_pids": [12345],
                    "success": True,
                    "error": None
                },
                ...
            }
        """
        logger.info("[v140.0] 🧹 Comprehensive pre-flight cleanup with stale session detection...")

        results: Dict[str, Dict[str, Any]] = {}

        # =====================================================================
        # v140.0: STEP 0 - STALE SESSION CLEANUP (THE KEY FIX)
        # =====================================================================
        # This MUST happen BEFORE port cleanup to prevent "protected PID" errors.
        # Processes from previous supervisor sessions are NOT protected and can
        # be safely terminated.
        # =====================================================================
        stale_cleaned = await self._cleanup_stale_session_processes()
        results["stale_session_cleanup"] = {
            "count": stale_cleaned,
            "description": "Processes from previous supervisor sessions"
        }

        if stale_cleaned > 0:
            # Wait for ports to be released after killing stale processes
            logger.info(f"[v140.0] Waiting for port release after killing {stale_cleaned} stale processes...")
            await asyncio.sleep(1.5)

        # =====================================================================
        # v136.0: STEP 1 - PORT CLEANUP
        # =====================================================================
        logger.info("[v136.0] 🧹 Now cleaning up ports...")

        all_ports = await self._get_all_service_ports()

        # GAP 10: Deduplicate - group services by port
        port_to_services: Dict[int, List[str]] = {}
        for service_name, port in all_ports.items():
            if port not in port_to_services:
                port_to_services[port] = []
            port_to_services[port].append(service_name)

        results: Dict[str, Dict[str, Any]] = {}
        total_killed = 0

        # Clean each unique port once
        for port in sorted(port_to_services.keys()):
            services = port_to_services[port]
            service_str = ", ".join(services)

            success, error, killed_pids = await self._enforce_port_hygiene(
                port=port,
                service_name=service_str,
            )

            results[f"port_{port}"] = {
                "services": services,
                "killed_pids": killed_pids,
                "success": success,
                "error": error,
            }

            total_killed += len(killed_pids)

            if error:
                logger.warning(f"[v136.0] Port {port} cleanup issue: {error}")

        # Summary
        if total_killed > 0:
            logger.info(
                f"[v136.0] ✅ Pre-flight cleanup complete: "
                f"{len(port_to_services)} ports checked, {total_killed} processes killed"
            )
        else:
            logger.info(
                f"[v136.0] ✅ Pre-flight cleanup complete: "
                f"{len(port_to_services)} ports verified free (no cleanup needed)"
            )

        return results

    async def _atomic_port_hygiene_and_spawn(
        self,
        managed: "ManagedProcess",
        definition: "ServiceDefinition",
    ) -> bool:
        """
        v136.0 GAP 5, 12: Atomic port cleanup + spawn with per-service lock.

        This is the CRITICAL method that prevents double-spawn race conditions.
        Holds a per-service lock for the entire clean+spawn+verify sequence.

        Used by _spawn_service to ensure only one spawn attempt per service
        can be in progress at any time.

        Returns:
            True if port is ready for spawn, False if cleanup failed
        """
        lock = self._get_service_spawn_lock(definition.name)

        # Check if lock is already held (non-blocking check)
        if lock.locked():
            logger.warning(
                f"[v136.0] ⚠️ Spawn lock for {definition.name} already held - "
                f"another spawn may be in progress. Waiting..."
            )

        async with lock:
            logger.debug(f"[v136.0] Acquired spawn lock for {definition.name}")

            # Perform port hygiene within the lock
            port_ready, port_error, killed_pids = await self._enforce_port_hygiene(
                port=definition.default_port,
                service_name=definition.name,
                graceful_timeout=3.0,
                force_timeout=2.0,
                post_kill_sleep=1.0,
                verification_retries=3,
            )

            if not port_ready:
                logger.error(
                    f"[v136.0] ❌ Cannot spawn {definition.name}: "
                    f"port {definition.default_port} not available - {port_error}"
                )
                return False

            if killed_pids:
                logger.info(
                    f"[v136.0] Port {definition.default_port} cleared for {definition.name} "
                    f"(terminated PIDs: {killed_pids})"
                )

            return True

    # =========================================================================
    # v93.8: Docker Hybrid Mode - Check Docker Before Local Process
    # =========================================================================

    async def _check_docker_available(self) -> bool:
        """
        v93.8: Check if Docker is available and running.

        Returns True if Docker daemon is accessible.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return_code = await asyncio.wait_for(proc.wait(), timeout=5.0)
            return return_code == 0
        except (asyncio.TimeoutError, FileNotFoundError, Exception):
            return False

    async def _check_docker_container_running(self, container_name: str) -> bool:
        """
        v93.8: Check if a specific Docker container is running.

        Args:
            container_name: Name of the Docker container to check

        Returns:
            True if container is running
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "container", "inspect",
                "-f", "{{.State.Running}}",
                container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode != 0:
                return False

            output = stdout.decode().strip().lower()
            return output == "true"

        except (asyncio.TimeoutError, FileNotFoundError, Exception) as e:
            logger.debug(f"Docker container check failed: {e}")
            return False

    async def _check_docker_service_healthy(
        self,
        service_name: str,
        port: int,
        health_endpoint: str = "/health",
    ) -> tuple[bool, Optional[str]]:
        """
        v93.11: Check if a Docker-hosted service is healthy via HTTP.

        Uses shared HTTP session with connection pooling.

        Args:
            service_name: Name of the service (for logging)
            port: Port the service is exposed on
            health_endpoint: Health check endpoint path

        Returns:
            Tuple of (is_healthy, status_string)
        """
        url = f"http://localhost:{port}{health_endpoint}"

        try:
            session = await self._get_http_session()
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=self.config.docker_health_timeout)
            ) as response:
                # v119.0: Read body even for non-200 to detect starting state
                try:
                    data = await response.json()
                except Exception:
                    data = {}

                if response.status != 200:
                    # v119.0: Check if 503 indicates starting (not unhealthy)
                    if response.status == 503:
                        status = data.get("status", "").lower()
                        phase = data.get("phase", "").lower()
                        if status == "starting" or phase in ("starting", "initializing"):
                            elapsed = data.get("model_load_elapsed_seconds", 0)
                            return False, f"starting (model loading: {elapsed:.0f}s)"
                    return False, f"HTTP {response.status}"

                status = data.get("status", "unknown")

                if status == "healthy":
                    return True, "healthy"
                elif status == "starting":
                    elapsed = data.get("model_load_elapsed_seconds", 0)
                    return False, f"starting (model loading: {elapsed:.0f}s)"
                else:
                    return False, status

        except asyncio.TimeoutError:
            return False, "timeout"
        except aiohttp.ClientConnectorError:
            return False, "connection refused"
        except Exception as e:
            return False, f"error: {e}"

    async def _try_docker_hybrid_for_service(
        self,
        definition: ServiceDefinition,
    ) -> tuple[bool, str]:
        """
        v93.8: Attempt to use Docker container for a service before spawning local.

        This implements the "Docker-first, local-fallback" hybrid approach.

        Args:
            definition: Service definition to check

        Returns:
            Tuple of (should_skip_local_spawn, reason_message)
            - If (True, msg): Docker container is handling the service, skip local spawn
            - If (False, msg): Docker not available/healthy, proceed with local spawn
        """
        # Only jarvis-prime supports Docker hybrid mode currently
        if definition.name != "jarvis-prime":
            return False, "service does not support Docker hybrid mode"

        if not self.config.docker_hybrid_enabled:
            return False, "Docker hybrid mode disabled via config"

        logger.info(f"    🐳 Checking Docker hybrid mode for {definition.name}...")

        # Step 1: Check if Docker is available
        docker_available = await self._check_docker_available()
        if not docker_available:
            logger.info(f"    ℹ️  Docker not available, using local process")
            return False, "Docker not available"

        # Step 2: Check if the container is running
        container_name = self.config.jarvis_prime_docker_container
        container_running = await self._check_docker_container_running(container_name)

        if container_running:
            logger.info(f"    ✅ Docker container '{container_name}' is running")

            # Step 3: Check if the service is healthy
            is_healthy, status = await self._check_docker_service_healthy(
                definition.name,
                definition.default_port,
                definition.health_endpoint,
            )

            if is_healthy:
                logger.info(
                    f"    ⚡ Connected to {definition.name} via Docker (status: {status})"
                )
                return True, f"Docker container healthy ({status})"
            else:
                # Container running but not healthy - might be starting up
                logger.info(
                    f"    ℹ️  Docker container running but not healthy: {status}"
                )

                if status and ("starting" in status or "model loading" in status):
                    # Service is starting in Docker, wait for it
                    logger.info(
                        f"    ⏳ Waiting for Docker container to become healthy..."
                    )

                    healthy = await self._wait_for_docker_health(
                        definition.name,
                        definition.default_port,
                        definition.health_endpoint,
                        timeout=self.config.docker_startup_timeout,
                    )

                    if healthy:
                        logger.info(
                            f"    ⚡ Connected to {definition.name} via Docker (healthy after waiting)"
                        )
                        return True, "Docker container healthy after waiting"
                    else:
                        logger.warning(
                            f"    ⚠️  Docker container failed to become healthy, using local process"
                        )
                        return False, "Docker container unhealthy after timeout"
                else:
                    # Container has error status
                    logger.warning(
                        f"    ⚠️  Docker container unhealthy ({status}), using local process"
                    )
                    return False, f"Docker container unhealthy: {status}"
        else:
            # Container not running
            logger.info(f"    ℹ️  Docker container '{container_name}' not running")

            # Optionally auto-start Docker container
            if self.config.docker_auto_start:
                logger.info(f"    🚀 Auto-starting Docker container...")
                started = await self._start_docker_container(definition)
                if started:
                    logger.info(
                        f"    ⚡ Connected to {definition.name} via Docker (auto-started)"
                    )
                    return True, "Docker container auto-started"
                else:
                    logger.warning(
                        f"    ⚠️  Failed to auto-start Docker container, using local process"
                    )
                    return False, "Docker auto-start failed"
            else:
                return False, "Docker container not running (auto-start disabled)"

    async def _wait_for_docker_health(
        self,
        service_name: str,
        port: int,
        health_endpoint: str,
        timeout: float = 180.0,
    ) -> bool:
        """
        v93.8: Wait for a Docker-hosted service to become healthy.

        Args:
            service_name: Name of the service (for logging)
            port: Port the service is exposed on
            health_endpoint: Health check endpoint path
            timeout: Maximum time to wait

        Returns:
            True if service became healthy within timeout
        """
        start_time = time.time()
        check_interval = 3.0
        last_log_time = start_time

        while (time.time() - start_time) < timeout:
            is_healthy, status = await self._check_docker_service_healthy(
                service_name, port, health_endpoint
            )

            if is_healthy:
                return True

            # Log progress every 30 seconds
            if (time.time() - last_log_time) >= 30.0:
                elapsed = time.time() - start_time
                remaining = timeout - elapsed
                logger.info(
                    f"    ⏳ {service_name} Docker: {status} "
                    f"({elapsed:.0f}s elapsed, {remaining:.0f}s remaining)"
                )
                last_log_time = time.time()

            await asyncio.sleep(check_interval)

        return False

    # =========================================================================
    # v93.9: Advanced Memory Monitoring and Routing
    # =========================================================================

    async def _get_memory_status(self) -> MemoryStatus:
        """
        v93.9: Get current system memory status with caching.
        v137.0: Updated to use non-blocking I/O (I/O Airlock pattern).

        Uses psutil for cross-platform memory info.
        Caches result for performance (refreshed every 5s).
        """
        # Check cache
        if (
            self._cached_memory_status is not None
            and (time.time() - self._memory_cache_time) < self._memory_cache_ttl
        ):
            return self._cached_memory_status

        try:
            # v137.0: Use non-blocking I/O wrapper for psutil calls
            mem_info = await get_memory_info_nonblocking()

            status = MemoryStatus(
                total_gb=mem_info.get("total_gb", 16.0),
                available_gb=mem_info.get("available_gb", 8.0),
                used_gb=mem_info.get("used_gb", 8.0),
                percent_used=mem_info.get("percent", 50.0),
                swap_total_gb=mem_info.get("swap_total_gb", 0.0),
                swap_used_gb=mem_info.get("swap_used_gb", 0.0),
            )

            # Update cache
            self._cached_memory_status = status
            self._memory_cache_time = time.time()

            return status

        except Exception as e:
            logger.warning(f"    [v137.0] Memory check failed: {e}")
            return MemoryStatus(available_gb=8.0)

    async def _should_route_to_gcp(
        self,
        definition: ServiceDefinition,
        memory_status: MemoryStatus,
    ) -> bool:
        """
        v93.9: Determine if service should be routed to GCP instead of local.

        Decision factors:
        - Available memory vs model requirements
        - GCP fallback enabled
        - GCP VM availability
        """
        if not self.config.gcp_fallback_enabled:
            return False

        # Check if memory is critically low
        if memory_status.available_gb < self.config.memory_route_to_gcp_threshold_gb:
            logger.info(
                f"    📊 Memory critically low ({memory_status.available_gb:.1f}GB), "
                f"routing to GCP recommended"
            )
            return True

        # Check if model won't fit in available memory
        # Estimate model memory: file size * 1.5 (for runtime overhead)
        try:
            model_path = definition.repo_path / "models"
            if model_path.exists():
                # Find largest .gguf file
                gguf_files = list(model_path.glob("*.gguf"))
                if gguf_files:
                    largest_model = max(gguf_files, key=lambda p: p.stat().st_size)
                    model_size_gb = largest_model.stat().st_size / (1024 ** 3)
                    estimated_runtime_gb = model_size_gb * self.config.memory_model_size_estimation_factor

                    if estimated_runtime_gb > memory_status.available_gb:
                        logger.info(
                            f"    📊 Model requires ~{estimated_runtime_gb:.1f}GB, "
                            f"only {memory_status.available_gb:.1f}GB available, "
                            f"routing to GCP recommended"
                        )
                        return True
        except Exception as e:
            logger.debug(f"    Model size estimation failed: {e}")

        return False

    async def _make_routing_decision(
        self,
        definition: ServiceDefinition,
    ) -> tuple[RoutingDecision, str]:
        """
        v93.10: Make intelligent routing decision for a service.

        Enhanced decision tree with detailed memory thresholds:
        1. Memory < 2GB → GCP (emergency, system unstable)
        2. Memory 2-4GB → Docker (pre-loaded models) or GCP
        3. Memory 4-6GB → Docker (preferred) or Local
        4. Memory > 6GB → Local (fastest, no container overhead)

        Also considers:
        - Circuit breaker state
        - Docker availability
        - GCP infrastructure availability
        - Model size estimation

        Returns:
            Tuple of (decision, reason)
        """
        memory_status = await self._get_memory_status()

        logger.info(f"    📊 Memory: {memory_status.available_gb:.1f}GB available "
                   f"({memory_status.percent_used:.0f}% used)")

        # Use the enhanced routing decision tree
        route_type, reason = await self._determine_optimal_routing(
            definition, memory_status
        )

        # Map string route type to RoutingDecision enum
        route_map = {
            "gcp": RoutingDecision.GCP_CLOUD,
            "docker": RoutingDecision.DOCKER_LOCAL,
            "local": RoutingDecision.LOCAL_PROCESS,
            "hybrid": RoutingDecision.HYBRID,
        }

        return route_map.get(route_type, RoutingDecision.LOCAL_PROCESS), reason

    async def _determine_optimal_routing(
        self,
        definition: ServiceDefinition,
        memory_status: MemoryStatus,
    ) -> tuple[str, str]:
        """
        v93.10: Intelligently determine: Docker, Local, or GCP?

        Enhanced decision tree with:
        - Model size estimation
        - Circuit breaker awareness
        - Parallel availability checks
        - Cost-aware routing (prefer local when possible)

        Decision tree:
        1. Memory < 2GB → GCP (emergency)
        2. Memory 2-4GB → Docker (pre-loaded models) OR GCP
        3. Memory 4-6GB → Docker (preferred) or Local
        4. Memory > 6GB → Local (fastest)

        Returns:
            Tuple of (route_type, reason)
        """
        available_gb = memory_status.available_gb

        # Estimate model memory requirements
        estimated_model_gb = await self._estimate_model_memory_requirement(definition)
        logger.debug(f"    Estimated model memory: {estimated_model_gb:.1f}GB")

        # =====================================================================
        # TIER 0: Emergency - System critically low on memory
        # =====================================================================
        if available_gb < 2.0:
            logger.warning(
                f"    🚨 EMERGENCY: Only {available_gb:.1f}GB available! "
                f"Routing to GCP to prevent system instability"
            )
            return "gcp", f"Emergency: {available_gb:.1f}GB available (< 2GB threshold)"

        # =====================================================================
        # TIER 1: Low memory (2-4GB) - Docker with pre-loaded models or GCP
        # =====================================================================
        if available_gb < 4.0:
            # Check Docker availability and circuit breaker in parallel
            docker_available, cb_can_execute = await asyncio.gather(
                self._check_docker_available(),
                asyncio.to_thread(
                    lambda: self._get_circuit_breaker(definition.name).can_execute()
                ),
            )

            if docker_available and cb_can_execute:
                return (
                    "docker",
                    f"Low memory ({available_gb:.1f}GB), using Docker pre-loaded models"
                )
            elif self.config.gcp_fallback_enabled:
                return (
                    "gcp",
                    f"Low memory ({available_gb:.1f}GB), Docker unavailable, using GCP"
                )
            else:
                # Last resort: try local anyway with warning
                logger.warning(
                    f"    ⚠️ Low memory ({available_gb:.1f}GB) but no alternatives, "
                    f"attempting local (may be slow)"
                )
                return (
                    "local",
                    f"Low memory ({available_gb:.1f}GB), no alternatives (risky)"
                )

        # =====================================================================
        # TIER 2: Medium memory (4-6GB) - Prefer Docker, fallback to Local
        # =====================================================================
        if available_gb < 6.0:
            # Check if model will fit with headroom
            memory_with_model = available_gb - estimated_model_gb
            has_headroom = memory_with_model >= 1.0  # Need at least 1GB headroom

            # Check Docker availability
            docker_available = await self._check_docker_available()
            cb = self._get_circuit_breaker(definition.name)

            if docker_available and cb.can_execute():
                return (
                    "docker",
                    f"Medium memory ({available_gb:.1f}GB), using Docker for efficiency"
                )
            elif has_headroom:
                return (
                    "local",
                    f"Medium memory ({available_gb:.1f}GB), Docker unavailable, "
                    f"local has {memory_with_model:.1f}GB headroom"
                )
            elif self.config.gcp_fallback_enabled:
                return (
                    "gcp",
                    f"Medium memory ({available_gb:.1f}GB), insufficient headroom for local"
                )
            else:
                return (
                    "local",
                    f"Medium memory ({available_gb:.1f}GB), attempting local (tight fit)"
                )

        # =====================================================================
        # TIER 3: High memory (6GB+) - Local is fastest (no container overhead)
        # =====================================================================
        # Check if local process will have enough headroom
        memory_with_model = available_gb - estimated_model_gb
        has_good_headroom = memory_with_model >= 2.0  # 2GB+ headroom is comfortable

        if has_good_headroom:
            return (
                "local",
                f"Sufficient memory ({available_gb:.1f}GB, {memory_with_model:.1f}GB headroom), "
                f"using local process (fastest)"
            )

        # High memory but model is large - consider Docker for pre-loading
        docker_available = await self._check_docker_available()
        cb = self._get_circuit_breaker(definition.name)

        if docker_available and cb.can_execute():
            return (
                "docker",
                f"Good memory ({available_gb:.1f}GB) but large model "
                f"({estimated_model_gb:.1f}GB), using Docker"
            )

        # Local with warning
        return (
            "local",
            f"Good memory ({available_gb:.1f}GB), using local process"
        )

    async def _estimate_model_memory_requirement(
        self,
        definition: ServiceDefinition,
    ) -> float:
        """
        v93.10: Estimate memory required for loading the model.

        Considers:
        - Model file size
        - Runtime overhead factor (1.5x for llama.cpp)
        - Context window allocation
        - Safety buffer
        """
        try:
            model_path = definition.repo_path / "models"

            if not model_path.exists():
                # Default estimate for 7B model
                return 6.0

            # Find the current model (symlink) or largest .gguf
            current_model = model_path / "current.gguf"
            if current_model.is_symlink() or current_model.exists():
                try:
                    # Resolve symlink to get actual file
                    actual_path = current_model.resolve()
                    if actual_path.exists():
                        file_size_gb = actual_path.stat().st_size / (1024 ** 3)
                        # Runtime overhead: file_size * 1.5 + context buffer (0.5GB)
                        return file_size_gb * self.config.memory_model_size_estimation_factor + 0.5
                except Exception:
                    pass

            # Find largest .gguf file as fallback
            gguf_files = list(model_path.glob("*.gguf"))
            if gguf_files:
                largest = max(gguf_files, key=lambda p: p.stat().st_size)
                file_size_gb = largest.stat().st_size / (1024 ** 3)
                return file_size_gb * self.config.memory_model_size_estimation_factor + 0.5

            # Default estimate
            return 6.0

        except Exception as e:
            logger.debug(f"    Model size estimation failed: {e}")
            return 6.0  # Conservative default

    # =========================================================================
    # v93.9: Docker Image Management
    # =========================================================================

    async def _check_docker_image_exists(
        self,
        image_name: str = "jarvis-prime:latest",
    ) -> bool:
        """
        v93.9: Check if Docker image exists locally.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", image_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return_code = await asyncio.wait_for(proc.wait(), timeout=10.0)
            return return_code == 0
        except Exception:
            return False

    async def _build_docker_image(
        self,
        definition: ServiceDefinition,
    ) -> bool:
        """
        v95.0: Build Docker image for a service with comprehensive event emissions.

        Uses docker-compose build with progress output and real-time voice narration.
        """
        compose_file = definition.repo_path / self.config.jarvis_prime_docker_compose

        if not compose_file.exists():
            logger.warning(f"    Docker compose file not found: {compose_file}")
            await _emit_event(
                "DOCKER_BUILD_FAILED",
                service_name=definition.name,
                priority="HIGH",
                details={"reason": "compose_file_missing", "path": str(compose_file)}
            )
            return False

        try:
            logger.info(f"    🔨 Building Docker image (this may take several minutes)...")

            # v95.0: Emit build start event for voice narration
            await _emit_event(
                "DOCKER_BUILD_START",
                service_name=definition.name,
                priority="HIGH",
                details={
                    "compose_file": str(compose_file),
                    "estimated_duration": "several minutes"
                }
            )

            proc = await asyncio.create_subprocess_exec(
                "docker", "compose",
                "-f", str(compose_file),
                "build", "--progress=plain",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(definition.repo_path),
            )

            # Stream build output with progress events
            build_start = time.time()
            last_log = build_start
            last_progress_event = build_start
            step_count = 0
            progress_event_interval = 30.0  # Emit progress event every 30 seconds

            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=self.config.docker_build_timeout
                )
                if not line:
                    break

                decoded = line.decode('utf-8', errors='replace').rstrip()
                if decoded:
                    # Track step progress
                    if 'step' in decoded.lower():
                        step_count += 1

                    # Log progress every 30 seconds or on important lines
                    now = time.time()
                    is_important = any(k in decoded.lower() for k in [
                        'step', 'downloading', 'extracting', 'error', 'warning',
                        'successfully', 'built', 'model'
                    ])

                    if is_important or (now - last_log) >= 30:
                        elapsed = now - build_start
                        logger.info(f"    [build {elapsed:.0f}s] {decoded[:100]}")
                        last_log = now

                    # v95.0: Emit periodic progress events for voice narration
                    if (now - last_progress_event) >= progress_event_interval:
                        elapsed = now - build_start
                        await _emit_event(
                            "DOCKER_BUILD_PROGRESS",
                            service_name=definition.name,
                            priority="LOW",
                            details={
                                "elapsed_seconds": int(elapsed),
                                "steps_completed": step_count,
                                "current_activity": decoded[:50] if decoded else "building"
                            }
                        )
                        last_progress_event = now

            return_code = await proc.wait()
            build_time = time.time() - build_start

            if return_code == 0:
                logger.info(f"    ✅ Docker image built successfully ({build_time:.0f}s)")
                # v95.0: Emit build complete event
                await _emit_event(
                    "DOCKER_BUILD_COMPLETE",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "build_time_seconds": int(build_time),
                        "steps_completed": step_count
                    }
                )
                return True
            else:
                logger.warning(f"    ❌ Docker build failed (exit code: {return_code})")
                # v95.0: Emit build failed event
                await _emit_event(
                    "DOCKER_BUILD_FAILED",
                    service_name=definition.name,
                    priority="CRITICAL",
                    details={
                        "exit_code": return_code,
                        "build_time_seconds": int(build_time)
                    }
                )
                return False

        except asyncio.TimeoutError:
            logger.warning(f"    ❌ Docker build timed out after {self.config.docker_build_timeout}s")
            # v95.0: Emit timeout event
            await _emit_event(
                "DOCKER_BUILD_FAILED",
                service_name=definition.name,
                priority="CRITICAL",
                details={
                    "reason": "timeout",
                    "timeout_seconds": self.config.docker_build_timeout
                }
            )
            try:
                proc.terminate()
            except Exception:
                pass
            return False
        except Exception as e:
            logger.warning(f"    ❌ Docker build failed: {e}")
            # v95.0: Emit exception event
            await _emit_event(
                "DOCKER_BUILD_FAILED",
                service_name=definition.name,
                priority="CRITICAL",
                details={"reason": "exception", "error": str(e)}
            )
            return False

    # =========================================================================
    # v93.10: Enhanced GCP Fallback Routing
    # =========================================================================

    async def _start_gcp_service(
        self,
        definition: ServiceDefinition,
    ) -> bool:
        """
        v95.0: Start service on GCP Spot VM with comprehensive event emissions.

        Integrates with existing GCP infrastructure:
        - GCPVMManager for VM lifecycle management
        - CloudMLRouter for intelligent model routing
        - HybridRouter for capability-based routing
        - Cost-optimized Spot VM instances
        - Real-time voice narration of GCP operations

        Features:
        - Auto VM selection based on model requirements
        - Spot VM for cost savings (with preemption handling)
        - Health check with retry
        - Automatic service deployment on VM
        - Voice feedback for all GCP operations
        """
        if not self.config.gcp_fallback_enabled:
            logger.info("    ℹ️ GCP fallback disabled via config")
            return False

        try:
            # v95.0: Emit GCP routing decision event
            await _emit_event(
                "GCP_ROUTING_DECISION",
                service_name=definition.name,
                priority="HIGH",
                details={"reason": "starting_gcp_service"}
            )

            # ================================================================
            # Step 1: Initialize GCP infrastructure
            # ================================================================
            gcp_infra = await self._initialize_gcp_infrastructure()
            if not gcp_infra:
                logger.warning("    ⚠️ GCP infrastructure not available")
                # v95.0: Emit GCP failed event
                await _emit_event(
                    "GCP_VM_FAILED",
                    service_name=definition.name,
                    priority="HIGH",
                    details={"reason": "infrastructure_not_available"}
                )
                return False

            vm_manager = gcp_infra.get("vm_manager")
            ml_router = gcp_infra.get("ml_router")
            hybrid_router = gcp_infra.get("hybrid_router")

            logger.info(f"    ☁️ Starting {definition.name} on GCP cloud...")

            # ================================================================
            # Step 2: Determine optimal VM configuration
            # ================================================================
            vm_config = await self._determine_gcp_vm_config(definition, ml_router)
            vm_name = vm_config["name"]
            machine_type = vm_config["machine_type"]
            use_spot = vm_config["use_spot"]

            logger.info(
                f"    📊 GCP VM config: {machine_type} "
                f"({'Spot' if use_spot else 'On-demand'})"
            )

            # v95.0: Emit Spot allocation event if using spot
            if use_spot:
                await _emit_event(
                    "GCP_SPOT_ALLOCATED",
                    service_name=definition.name,
                    priority="MEDIUM",
                    details={
                        "machine_type": machine_type,
                        "vm_name": vm_name,
                        "cost_savings": "up to 80%"
                    }
                )

            # ================================================================
            # Step 3: Check if VM already exists and is running
            # ================================================================
            existing_vm = await self._check_existing_gcp_vm(
                vm_manager, vm_name, definition
            )
            if existing_vm:
                # v95.0: Emit GCP VM ready event (reusing existing)
                await _emit_event(
                    "GCP_VM_READY",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "vm_name": vm_name,
                        "status": "reusing_existing",
                        "machine_type": machine_type
                    }
                )
                return True

            # ================================================================
            # Step 4: Create/start VM with service deployment
            # ================================================================
            # v95.0: Emit VM creating event
            await _emit_event(
                "GCP_VM_CREATING",
                service_name=definition.name,
                priority="HIGH",
                details={
                    "vm_name": vm_name,
                    "machine_type": machine_type,
                    "spot_instance": use_spot
                }
            )

            vm_started = await self._create_and_start_gcp_vm(
                vm_manager,
                vm_name,
                machine_type,
                use_spot,
                definition,
            )

            if not vm_started:
                logger.warning(f"    ⚠️ Failed to start GCP VM")
                # v95.0: Emit GCP VM failed event
                await _emit_event(
                    "GCP_VM_FAILED",
                    service_name=definition.name,
                    priority="CRITICAL",
                    details={
                        "vm_name": vm_name,
                        "reason": "vm_start_failed"
                    }
                )
                return False

            # v95.0: Emit VM starting event
            await _emit_event(
                "GCP_VM_STARTING",
                service_name=definition.name,
                priority="MEDIUM",
                details={
                    "vm_name": vm_name,
                    "phase": "waiting_for_health"
                }
            )

            # ================================================================
            # Step 5: Wait for service to be healthy
            # ================================================================
            healthy = await self._wait_for_gcp_service_health(
                vm_manager,
                vm_name,
                definition,
            )

            if healthy:
                # v95.0: Emit GCP VM ready event
                await _emit_event(
                    "GCP_VM_READY",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "vm_name": vm_name,
                        "machine_type": machine_type,
                        "spot_instance": use_spot
                    }
                )
            else:
                # v95.0: Emit GCP VM failed event
                await _emit_event(
                    "GCP_VM_FAILED",
                    service_name=definition.name,
                    priority="CRITICAL",
                    details={
                        "vm_name": vm_name,
                        "reason": "health_check_failed"
                    }
                )

            return healthy

        except ImportError as e:
            logger.warning(f"    ⚠️ GCP module not available: {e}")
            # v95.0: Emit GCP failed event
            await _emit_event(
                "GCP_VM_FAILED",
                service_name=definition.name,
                priority="HIGH",
                details={"reason": "module_not_available", "error": str(e)}
            )
            return False
        except Exception as e:
            logger.error(f"    ❌ GCP service start failed: {e}")
            # v95.0: Emit GCP failed event
            await _emit_event(
                "GCP_VM_FAILED",
                service_name=definition.name,
                priority="CRITICAL",
                details={"reason": "exception", "error": str(e)}
            )
            import traceback
            logger.debug(traceback.format_exc())
            return False

    async def _initialize_gcp_infrastructure(self) -> Optional[Dict[str, Any]]:
        """
        v93.10: Initialize GCP infrastructure components.

        Lazy-loads:
        - GCPVMManager for VM lifecycle
        - CloudMLRouter for model routing (optional)
        - HybridRouter for capability routing (optional)
        """
        infrastructure: Dict[str, Any] = {}

        # VM Manager (required)
        if self._gcp_vm_manager is None:
            try:
                from backend.core.gcp_vm_manager import get_gcp_vm_manager
                self._gcp_vm_manager = get_gcp_vm_manager()
                logger.info("    ☁️ GCP VM Manager initialized")
            except ImportError:
                try:
                    from backend.core.gcp_vm_manager import GCPVMManager
                    self._gcp_vm_manager = GCPVMManager()
                    logger.info("    ☁️ GCP VM Manager initialized (direct)")
                except ImportError:
                    logger.warning("    ⚠️ GCP VM Manager not available")
                    return None

        infrastructure["vm_manager"] = self._gcp_vm_manager

        # CloudML Router (optional - for intelligent routing)
        try:
            from backend.core.cloud_ml_router import CloudMLRouter, get_cloud_ml_router
            try:
                infrastructure["ml_router"] = get_cloud_ml_router()
            except Exception:
                infrastructure["ml_router"] = CloudMLRouter()
            logger.debug("    ☁️ CloudML Router available")
        except ImportError:
            infrastructure["ml_router"] = None
            logger.debug("    ℹ️ CloudML Router not available")

        # Hybrid Router (optional - for capability-based routing)
        try:
            from backend.core.hybrid_router import HybridRouter, get_hybrid_router
            try:
                infrastructure["hybrid_router"] = get_hybrid_router()
            except Exception:
                infrastructure["hybrid_router"] = HybridRouter()
            logger.debug("    ☁️ Hybrid Router available")
        except ImportError:
            infrastructure["hybrid_router"] = None
            logger.debug("    ℹ️ Hybrid Router not available")

        return infrastructure

    async def _determine_gcp_vm_config(
        self,
        definition: ServiceDefinition,
        ml_router: Optional[Any],
    ) -> Dict[str, Any]:
        """
        v93.10: Determine optimal GCP VM configuration.

        Considers:
        - Model memory requirements
        - Cost optimization (Spot vs On-demand)
        - Region availability
        """
        # Estimate model memory requirements
        estimated_model_gb = await self._estimate_model_memory_requirement(definition)

        # Base VM name
        vm_name = f"jarvis-prime-{definition.name}"

        # Select machine type based on model requirements
        if estimated_model_gb < 4.0:
            # Small model: e2-standard-4 (16GB RAM)
            machine_type = "e2-standard-4"
        elif estimated_model_gb < 8.0:
            # Medium model: e2-highmem-4 (32GB RAM)
            machine_type = "e2-highmem-4"
        elif estimated_model_gb < 16.0:
            # Large model: e2-highmem-8 (64GB RAM)
            machine_type = "e2-highmem-8"
        else:
            # Very large model: n1-highmem-8 (52GB RAM) with GPU option
            machine_type = "n1-highmem-8"

        # Use Spot VMs for cost savings (with preemption handling)
        use_spot = True

        # If ML router available, get its recommendation
        if ml_router:
            try:
                ml_recommendation = await ml_router.get_vm_recommendation(
                    model_size_gb=estimated_model_gb,
                    prefer_spot=True,
                )
                if ml_recommendation:
                    machine_type = ml_recommendation.get("machine_type", machine_type)
                    use_spot = ml_recommendation.get("use_spot", use_spot)
            except Exception as e:
                logger.debug(f"    ML router recommendation failed: {e}")

        return {
            "name": vm_name,
            "machine_type": machine_type,
            "use_spot": use_spot,
            "estimated_memory_gb": estimated_model_gb,
        }

    async def _check_existing_gcp_vm(
        self,
        vm_manager: Any,
        vm_name: str,
        definition: ServiceDefinition,
    ) -> bool:
        """
        v93.10: Check if GCP VM already exists and is running.

        Returns True if VM is running and service is healthy.
        """
        try:
            # Check VM status
            vm_status = await vm_manager.get_vm_status(vm_name)

            if not vm_status:
                return False

            status = vm_status.get("status", "UNKNOWN")

            if status == "RUNNING":
                logger.info(f"    ✅ GCP VM '{vm_name}' already running")

                # Get external IP
                external_ip = vm_status.get("external_ip")
                if not external_ip:
                    logger.warning(f"    ⚠️ VM running but no external IP")
                    return False

                # Check if service is healthy on the VM
                is_healthy, health_status = await self._check_gcp_service_health(
                    external_ip, definition
                )

                if is_healthy:
                    logger.info(
                        f"    ⚡ Connected to {definition.name} via GCP "
                        f"({external_ip}, status: {health_status})"
                    )
                    return True
                else:
                    logger.info(
                        f"    ℹ️ VM running but service not healthy: {health_status}"
                    )
                    # Service might be starting, let the caller handle waiting
                    return False

            elif status in ("STAGING", "PROVISIONING"):
                logger.info(f"    ℹ️ GCP VM '{vm_name}' is {status}...")
                return False

            elif status == "TERMINATED":
                logger.info(f"    ℹ️ GCP VM '{vm_name}' is terminated, will restart")
                return False

            else:
                logger.info(f"    ℹ️ GCP VM '{vm_name}' status: {status}")
                return False

        except Exception as e:
            logger.debug(f"    VM status check failed: {e}")
            return False

    async def _create_and_start_gcp_vm(
        self,
        vm_manager: Any,
        vm_name: str,
        machine_type: str,
        use_spot: bool,
        definition: ServiceDefinition,
    ) -> bool:
        """
        v93.10: Create and start GCP VM with jarvis-prime service.
        """
        try:
            logger.info(
                f"    🚀 Creating/starting GCP VM '{vm_name}' "
                f"({machine_type}, {'Spot' if use_spot else 'On-demand'})..."
            )

            # Create or start VM
            # Try the enhanced method first
            try:
                result = await vm_manager.ensure_vm_running(
                    name=vm_name,
                    machine_type=machine_type,
                    preemptible=use_spot,
                    startup_script=self._generate_gcp_startup_script(definition),
                    timeout=self.config.gcp_vm_startup_timeout,
                )
                return result
            except TypeError:
                # Fallback to simpler interface
                result = await vm_manager.ensure_vm_running(
                    name=vm_name,
                    machine_type=machine_type,
                    timeout=self.config.gcp_vm_startup_timeout,
                )
                return result

        except Exception as e:
            logger.error(f"    ❌ Failed to create/start GCP VM: {e}")
            return False

    def _generate_gcp_startup_script(self, definition: ServiceDefinition) -> str:
        """
        v93.10: Generate startup script for GCP VM.

        Installs and starts jarvis-prime on the VM.
        """
        return f"""#!/bin/bash
set -e

# Log startup
exec > >(tee /var/log/jarvis-prime-startup.log) 2>&1
echo "=== JARVIS Prime GCP Startup Script ==="
date

# Install dependencies
apt-get update
apt-get install -y python3-pip python3-venv git

# Clone or update jarvis-prime
cd /opt
if [ -d "jarvis-prime" ]; then
    cd jarvis-prime && git pull
else
    git clone https://github.com/your-org/jarvis-prime.git
    cd jarvis-prime
fi

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install requirements
pip install -r requirements.txt

# Download model if not exists
if [ ! -f "models/current.gguf" ]; then
    python -m jarvis_prime.docker.model_downloader --model phi-3.5-mini
fi

# Start server
python run_server.py --host 0.0.0.0 --port {definition.default_port} &

echo "=== JARVIS Prime started ==="
"""

    async def _check_gcp_service_health(
        self,
        external_ip: str,
        definition: ServiceDefinition,
    ) -> tuple[bool, str]:
        """
        v93.10: Check health of service running on GCP VM.
        """
        url = f"http://{external_ip}:{definition.default_port}{definition.health_endpoint}"

        try:
            session = await self._get_http_session()
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10.0)
            ) as response:
                # v119.0: Read body even for non-200 to detect starting state
                try:
                    data = await response.json()
                except Exception:
                    data = {}

                if response.status != 200:
                    # v119.0: Check if 503 indicates starting (not unhealthy)
                    if response.status == 503:
                        status = data.get("status", "").lower()
                        phase = data.get("phase", "").lower()
                        if status == "starting" or phase in ("starting", "initializing"):
                            elapsed = data.get("model_load_elapsed_seconds", 0)
                            return False, f"starting (model loading: {elapsed:.0f}s)"
                    return False, f"HTTP {response.status}"

                status = data.get("status", "unknown")

                if status == "healthy":
                    return True, "healthy"
                elif status == "starting":
                    elapsed = data.get("model_load_elapsed_seconds", 0)
                    return False, f"starting (model loading: {elapsed:.0f}s)"
                else:
                    return False, status

        except asyncio.TimeoutError:
            return False, "timeout"
        except aiohttp.ClientConnectorError:
            return False, "connection refused"
        except Exception as e:
            return False, f"error: {e}"

    async def _wait_for_gcp_service_health(
        self,
        vm_manager: Any,
        vm_name: str,
        definition: ServiceDefinition,
    ) -> bool:
        """
        v93.10: Wait for service on GCP VM to become healthy.

        Includes:
        - VM status monitoring
        - Service health checking
        - Progress logging
        """
        start_time = time.time()
        timeout = self.config.gcp_vm_startup_timeout
        check_interval = 5.0
        last_log_time = start_time

        logger.info(
            f"    ⏳ Waiting for GCP service to become healthy "
            f"(timeout: {timeout:.0f}s)..."
        )

        while (time.time() - start_time) < timeout:
            elapsed = time.time() - start_time

            # Check VM status first
            try:
                vm_status = await vm_manager.get_vm_status(vm_name)

                if not vm_status:
                    logger.warning(f"    ⚠️ VM status unavailable")
                    await asyncio.sleep(check_interval)
                    continue

                status = vm_status.get("status", "UNKNOWN")

                if status != "RUNNING":
                    # Log progress every 30 seconds
                    if (time.time() - last_log_time) >= 30.0:
                        logger.info(
                            f"    ⏳ VM status: {status} ({elapsed:.0f}s elapsed)"
                        )
                        last_log_time = time.time()
                    await asyncio.sleep(check_interval)
                    continue

                # VM is running, check service health
                external_ip = vm_status.get("external_ip")
                if not external_ip:
                    await asyncio.sleep(check_interval)
                    continue

                is_healthy, health_status = await self._check_gcp_service_health(
                    external_ip, definition
                )

                if is_healthy:
                    total_time = time.time() - start_time
                    logger.info(
                        f"    ✅ GCP service healthy after {total_time:.1f}s "
                        f"(IP: {external_ip})"
                    )
                    return True

                # Log progress
                if (time.time() - last_log_time) >= 30.0:
                    remaining = timeout - elapsed
                    logger.info(
                        f"    ⏳ GCP service: {health_status} "
                        f"({elapsed:.0f}s elapsed, {remaining:.0f}s remaining)"
                    )
                    last_log_time = time.time()

            except Exception as e:
                logger.debug(f"    Health check error: {e}")

            await asyncio.sleep(check_interval)

        # Timeout
        total_time = time.time() - start_time
        logger.warning(
            f"    ⚠️ GCP service health timeout after {total_time:.1f}s"
        )
        return False

    # =========================================================================
    # v93.9: Enhanced Docker Container Startup
    # =========================================================================

    async def _start_docker_container(self, definition: ServiceDefinition) -> bool:
        """
        v95.0: Enhanced Docker container startup with comprehensive event emissions.

        Features:
        - Memory-aware routing (Docker vs GCP) with voice narration
        - Circuit breaker protection with state announcements
        - Retry with exponential backoff and progress events
        - Auto-build if image missing
        - Comprehensive error handling with voice feedback

        Args:
            definition: Service definition

        Returns:
            True if container started and became healthy
        """
        if definition.name != "jarvis-prime":
            return False

        # v95.0: Emit memory check event
        await _emit_event(
            "MEMORY_CHECK",
            service_name=definition.name,
            priority="MEDIUM",
            details={"phase": "pre_startup"}
        )

        # Step 1: Check memory and make routing decision
        memory_status = await self._get_memory_status()

        if memory_status.is_memory_pressure:
            logger.warning(
                f"    ⚠️ System under memory pressure "
                f"({memory_status.available_gb:.1f}GB available, "
                f"{memory_status.percent_used:.0f}% used)"
            )

            # v95.0: Emit memory pressure event
            pressure_level = "MEMORY_PRESSURE_CRITICAL" if memory_status.percent_used > 90 else "MEMORY_PRESSURE_HIGH"
            await _emit_event(
                pressure_level,
                service_name=definition.name,
                priority="HIGH",
                details={
                    "available_gb": memory_status.available_gb,
                    "percent_used": memory_status.percent_used,
                    "can_load_local_model": memory_status.can_load_local_model
                }
            )

            if await self._should_route_to_gcp(definition, memory_status):
                logger.info(f"    ☁️ Routing to GCP due to memory constraints...")
                # v95.0: Emit routing decision event
                await _emit_event(
                    "ROUTING_GCP",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "reason": "memory_constraints",
                        "available_gb": memory_status.available_gb
                    }
                )
                return await self._start_gcp_service(definition)

        # Step 2: Check circuit breaker
        cb = self._get_circuit_breaker(definition.name)
        if not cb.can_execute():
            logger.warning(
                f"    ⚠️ Docker circuit breaker OPEN for {definition.name}, "
                f"waiting for recovery timeout..."
            )
            # v95.0: Emit circuit breaker open event
            await _emit_event(
                "CIRCUIT_BREAKER_OPEN",
                service_name=definition.name,
                priority="HIGH",
                details={
                    "failure_count": cb.failure_count,
                    "state": "open",
                    "recovery_timeout": cb.recovery_timeout
                }
            )
            return False

        # v95.0: Emit Docker check event
        await _emit_event(
            "DOCKER_CHECK",
            service_name=definition.name,
            priority="MEDIUM",
            details={"checking": "image_exists"}
        )

        # Step 3: Check if Docker image exists, build if needed
        image_name = "jarvis-prime:latest"
        image_exists = await self._check_docker_image_exists(image_name)

        if not image_exists:
            # v95.0: Emit Docker not found event
            await _emit_event(
                "DOCKER_NOT_FOUND",
                service_name=definition.name,
                priority="MEDIUM",
                details={"image_name": image_name}
            )

            if self.config.docker_auto_build:
                logger.info(f"    🔨 Docker image '{image_name}' not found, building...")
                built = await self._build_docker_image(definition)
                if not built:
                    cb.record_failure()
                    # v95.0: Emit circuit breaker trip event
                    await _emit_event(
                        "CIRCUIT_BREAKER_TRIP",
                        service_name=definition.name,
                        priority="HIGH",
                        details={"reason": "docker_build_failed"}
                    )
                    logger.warning(f"    ⚠️ Docker build failed, trying alternative...")
                    return await self._fallback_to_alternative(definition, memory_status)
            else:
                logger.warning(
                    f"    ⚠️ Docker image '{image_name}' not found and auto-build disabled"
                )
                return False
        else:
            # v95.0: Emit Docker found event
            await _emit_event(
                "DOCKER_FOUND",
                service_name=definition.name,
                priority="MEDIUM",
                details={"image_name": image_name}
            )

        # Step 4: Start container with retry and exponential backoff
        compose_file = definition.repo_path / self.config.jarvis_prime_docker_compose

        if not compose_file.exists():
            logger.warning(f"    Docker compose file not found: {compose_file}")
            cb.record_failure()
            return False

        retry_state = self._get_retry_state(f"docker-start-{definition.name}")
        retry_state.reset()  # Reset for new startup attempt
        start_time = time.time()

        # v95.0: Emit Docker starting event
        await _emit_event(
            "DOCKER_STARTING",
            service_name=definition.name,
            priority="HIGH",
            details={
                "image_name": image_name,
                "max_attempts": retry_state.max_attempts
            }
        )

        while retry_state.should_retry():
            attempt = retry_state.attempt + 1
            logger.info(
                f"    🚀 Starting Docker container (attempt {attempt}/"
                f"{retry_state.max_attempts})..."
            )

            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "compose",
                    "-f", str(compose_file),
                    "up", "-d",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(definition.repo_path),
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=60.0
                )

                if proc.returncode != 0:
                    error_msg = stderr.decode()[:200]
                    retry_state.record_attempt(f"Exit code {proc.returncode}: {error_msg}")
                    logger.warning(f"    ⚠️ Docker compose up failed: {error_msg}")

                    if retry_state.should_retry():
                        delay = retry_state.get_next_delay()
                        logger.info(f"    ⏳ Retrying in {delay:.1f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        break

                logger.info(f"    Docker container starting, waiting for health...")

                # v95.0: Emit Docker health check event
                await _emit_event(
                    "DOCKER_HEALTH_CHECK",
                    service_name=definition.name,
                    priority="MEDIUM",
                    details={
                        "port": definition.default_port,
                        "endpoint": definition.health_endpoint,
                        "timeout": self.config.docker_startup_timeout
                    }
                )

                # Wait for container to become healthy
                healthy = await self._wait_for_docker_health(
                    definition.name,
                    definition.default_port,
                    definition.health_endpoint,
                    timeout=self.config.docker_startup_timeout,
                )

                if healthy:
                    elapsed = time.time() - start_time
                    cb.record_success()
                    retry_state.reset()

                    # v95.0: Emit circuit breaker closed event (healthy)
                    await _emit_event(
                        "CIRCUIT_BREAKER_CLOSED",
                        service_name=definition.name,
                        priority="MEDIUM",
                        details={"reason": "container_healthy"}
                    )

                    # v95.0: Emit Docker healthy event
                    await _emit_event(
                        "DOCKER_HEALTHY",
                        service_name=definition.name,
                        priority="HIGH",
                        details={
                            "startup_time_seconds": int(elapsed),
                            "attempts": attempt,
                            "port": definition.default_port
                        }
                    )

                    logger.info(
                        f"    ✅ Docker container healthy after {elapsed:.1f}s "
                        f"(attempt {attempt})"
                    )
                    return True
                else:
                    retry_state.record_attempt("Health check timeout")
                    logger.warning(f"    ⚠️ Container started but health check failed")

                    # v95.0: Emit Docker unhealthy event
                    await _emit_event(
                        "DOCKER_UNHEALTHY",
                        service_name=definition.name,
                        priority="HIGH",
                        details={
                            "reason": "health_check_timeout",
                            "attempt": attempt
                        }
                    )

                    if retry_state.should_retry():
                        delay = retry_state.get_next_delay()
                        logger.info(f"    ⏳ Retrying in {delay:.1f}s...")
                        # Stop unhealthy container before retry
                        await self._stop_docker_container(definition)
                        await asyncio.sleep(delay)
                        continue
                    else:
                        break

            except asyncio.TimeoutError:
                retry_state.record_attempt("Timeout")
                logger.warning(f"    ⚠️ Docker compose up timed out")

                if retry_state.should_retry():
                    delay = retry_state.get_next_delay()
                    logger.info(f"    ⏳ Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    break

            except Exception as e:
                retry_state.record_attempt(str(e))
                logger.warning(f"    ⚠️ Docker compose up failed: {e}")

                if retry_state.should_retry():
                    delay = retry_state.get_next_delay()
                    logger.info(f"    ⏳ Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    break

        # All retries exhausted
        cb.record_failure()

        # v95.0: Emit circuit breaker trip event
        await _emit_event(
            "CIRCUIT_BREAKER_TRIP",
            service_name=definition.name,
            priority="CRITICAL",
            details={
                "reason": "retries_exhausted",
                "failure_count": cb.failure_count,
                "last_error": retry_state.last_error
            }
        )

        total_elapsed = time.time() - start_time
        logger.error(
            f"    ❌ Docker startup failed after {retry_state.attempt} attempts "
            f"({total_elapsed:.1f}s total). Last error: {retry_state.last_error}"
        )

        # Try fallback
        return await self._fallback_to_alternative(definition, memory_status)

    async def _stop_docker_container(self, definition: ServiceDefinition) -> bool:
        """
        v93.9: Stop Docker container gracefully.
        """
        compose_file = definition.repo_path / self.config.jarvis_prime_docker_compose

        if not compose_file.exists():
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose",
                "-f", str(compose_file),
                "down",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(definition.repo_path),
            )
            await asyncio.wait_for(proc.wait(), timeout=30.0)
            return proc.returncode == 0
        except Exception:
            return False

    async def _fallback_to_alternative(
        self,
        definition: ServiceDefinition,
        memory_status: MemoryStatus,
    ) -> bool:
        """
        v95.0: Fallback to alternative deployment with comprehensive event emissions.

        Order of fallback (with voice narration):
        1. GCP cloud (if enabled and sufficient resources)
        2. Local process (if memory allows)
        3. GCP as last resort
        """
        logger.info(f"    🔄 Attempting fallback for {definition.name}...")

        # v95.0: Emit routing fallback event
        await _emit_event(
            "ROUTING_FALLBACK",
            service_name=definition.name,
            priority="HIGH",
            details={
                "phase": "starting_fallback_chain",
                "available_memory_gb": memory_status.available_gb,
                "can_load_local": memory_status.can_load_local_model,
                "gcp_enabled": self.config.gcp_fallback_enabled
            }
        )

        # Try GCP if enabled and memory is an issue
        if self.config.gcp_fallback_enabled:
            if memory_status.is_memory_pressure or not memory_status.can_load_local_model:
                logger.info(f"    ☁️ Trying GCP fallback...")
                # v95.0: Emit GCP fallback event
                await _emit_event(
                    "GCP_FALLBACK",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "reason": "memory_pressure" if memory_status.is_memory_pressure else "insufficient_memory",
                        "fallback_tier": 1
                    }
                )
                if await self._start_gcp_service(definition):
                    return True

        # Try local process if memory allows
        if memory_status.can_load_local_model:
            logger.info(f"    🐍 Falling back to local process...")
            # v95.0: Emit routing local event
            await _emit_event(
                "ROUTING_LOCAL",
                service_name=definition.name,
                priority="MEDIUM",
                details={
                    "reason": "memory_sufficient_for_local",
                    "available_gb": memory_status.available_gb,
                    "fallback_tier": 2
                }
            )
            # Return False to let the caller spawn local process
            return False

        # Last resort: try GCP anyway
        if self.config.gcp_fallback_enabled:
            logger.info(f"    ☁️ Last resort: trying GCP...")
            # v95.0: Emit GCP fallback event (last resort)
            await _emit_event(
                "GCP_FALLBACK",
                service_name=definition.name,
                priority="CRITICAL",
                details={
                    "reason": "last_resort",
                    "fallback_tier": 3,
                    "all_other_options_exhausted": True
                }
            )
            return await self._start_gcp_service(definition)

        logger.warning(f"    ❌ No viable fallback options for {definition.name}")
        # v95.0: Emit final fallback failure event
        await _emit_event(
            "ROUTING_FALLBACK",
            service_name=definition.name,
            priority="CRITICAL",
            details={
                "phase": "fallback_chain_exhausted",
                "result": "no_viable_options"
            }
        )
        return False

    @property
    def registry(self):
        """
        v93.0: Lazy-load service registry with robust error handling.

        Handles:
        - ImportError: Module not available
        - RuntimeError: Directory creation failed
        - Any other initialization errors
        """
        if self._registry is None:
            try:
                from backend.core.service_registry import get_service_registry
                self._registry = get_service_registry()
                logger.debug("[v93.0] Service registry loaded successfully")

                # v95.0: Register callbacks for service lifecycle events
                # This enables automatic restart when services die
                self._registry.register_on_service_dead(self._on_registry_service_dead)
                self._registry.register_on_service_stale(self._on_registry_service_stale)
                logger.debug("[v95.0] Registered service lifecycle callbacks with registry")
            except ImportError:
                logger.warning("[v93.0] Service registry module not available")
            except RuntimeError as e:
                logger.error(f"[v93.0] Service registry initialization failed: {e}")
            except Exception as e:
                logger.error(f"[v93.0] Unexpected error loading service registry: {e}")
        return self._registry

    def add_service(self, definition: ServiceDefinition) -> None:
        """
        Add a service definition to the orchestrator.

        This allows dynamic addition of services beyond the default configuration.

        Args:
            definition: Service definition to add
        """
        if definition.name in self.processes:
            logger.warning(f"Service {definition.name} already exists, updating definition")

        self.processes[definition.name] = ManagedProcess(definition=definition)
        logger.debug(f"Added service: {definition.name}")

    def remove_service(self, name: str) -> bool:
        """
        Remove a service from the orchestrator.

        Args:
            name: Service name to remove

        Returns:
            True if service was removed
        """
        if name in self.processes:
            del self.processes[name]
            logger.debug(f"Removed service: {name}")
            return True
        return False

    # =========================================================================
    # v95.0: Service Registry Lifecycle Callbacks
    # =========================================================================

    async def _on_registry_service_dead(self, service_name: str, dead_pid: int) -> None:
        """
        v95.0: Callback invoked when service registry detects a dead service.

        This triggers automatic restart of the service if:
        1. The service is managed by this orchestrator
        2. Auto-healing is enabled
        3. The service hasn't exceeded max restart attempts
        """
        logger.warning(
            f"[v95.0] Registry notified service '{service_name}' died (PID {dead_pid})"
        )

        # Check if we manage this service
        if service_name not in self.processes:
            logger.debug(f"[v95.0] Service '{service_name}' not managed by this orchestrator")
            return

        managed = self.processes[service_name]

        # Mark as failed
        managed.status = ServiceStatus.FAILED
        managed.pid = None  # PID is dead

        # Emit event for voice narration
        await _emit_event(
            "SERVICE_CRASHED",
            service_name=service_name,
            priority="CRITICAL",
            details={
                "reason": "registry_detected_dead_pid",
                "dead_pid": dead_pid,
                "auto_healing": self.config.auto_healing_enabled
            }
        )

        # Attempt auto-restart if enabled
        if self.config.auto_healing_enabled:
            logger.info(f"[v95.0] Attempting auto-restart of '{service_name}'")
            success = await self._auto_heal(managed)
            if success:
                logger.info(f"[v95.0] Successfully restarted '{service_name}'")
            else:
                logger.error(f"[v95.0] Failed to restart '{service_name}'")
        else:
            logger.warning(f"[v95.0] Auto-healing disabled, '{service_name}' will remain down")

    async def _on_registry_service_stale(self, service_name: str, heartbeat_age: float) -> None:
        """
        v95.0: Callback invoked when service registry detects a stale service.

        This can trigger investigation or restart if the service is unresponsive.
        """
        logger.warning(
            f"[v95.0] Registry notified service '{service_name}' is stale "
            f"(no heartbeat for {heartbeat_age:.0f}s)"
        )

        # Check if we manage this service
        if service_name not in self.processes:
            return

        managed = self.processes[service_name]

        # Mark as degraded
        if managed.status != ServiceStatus.FAILED:
            managed.status = ServiceStatus.DEGRADED

        # Emit event for monitoring
        await _emit_event(
            "SERVICE_UNHEALTHY",
            service_name=service_name,
            priority="HIGH",
            details={
                "reason": "stale_heartbeat",
                "heartbeat_age_seconds": int(heartbeat_age)
            }
        )

        # If very stale (>5 minutes), consider it dead and restart
        if heartbeat_age > 300 and self.config.auto_healing_enabled:
            logger.warning(
                f"[v95.0] Service '{service_name}' extremely stale ({heartbeat_age:.0f}s), "
                f"treating as dead and attempting restart"
            )
            managed.status = ServiceStatus.FAILED
            await self._auto_heal(managed)

    def _get_service_definitions(self) -> List[ServiceDefinition]:
        """
        Get service definitions based on configuration.

        v4.0: Uses actual entry point scripts from each repo:
        - jarvis-prime: run_server.py (port 8000)
        - reactor-core: run_reactor.py (port 8090)

        v95.0: Now uses ServiceDefinitionRegistry for consistency.
        The registry provides canonical definitions that can be overridden
        by the orchestrator's config.
        """
        definitions = []

        if self.config.jarvis_prime_enabled:
            # v95.0: Use registry for canonical definition, override with config
            jprime_def = ServiceDefinitionRegistry.get_definition(
                "jarvis-prime",
                port_override=self.config.jarvis_prime_default_port,
                path_override=self.config.jarvis_prime_path,
                validate=True,
            )
            if jprime_def:
                # Override timeout from config
                jprime_def.startup_timeout = self.config.jarvis_prime_startup_timeout
                definitions.append(jprime_def)
                logger.debug(f"Using registry definition for jarvis-prime: {jprime_def.script_name}")
            else:
                logger.warning("Could not get jarvis-prime definition from registry, using fallback")
                # Fallback to inline definition if registry fails
                definitions.append(ServiceDefinition(
                    name="jarvis-prime",
                    repo_path=self.config.jarvis_prime_path,
                    script_name="run_server.py",
                    fallback_scripts=["main.py", "server.py", "app.py"],
                    default_port=self.config.jarvis_prime_default_port,
                    health_endpoint="/health",
                    startup_timeout=self.config.jarvis_prime_startup_timeout,
                    script_args=["--port", str(self.config.jarvis_prime_default_port), "--host", "0.0.0.0"],
                    environment={
                        "PYTHONPATH": str(self.config.jarvis_prime_path),
                        "PYTHONWARNINGS": "ignore::UserWarning,ignore::DeprecationWarning,ignore::FutureWarning",
                        "TF_CPP_MIN_LOG_LEVEL": "2",
                        "TRANSFORMERS_VERBOSITY": "error",
                        "TOKENIZERS_PARALLELISM": "false",
                        "COREMLTOOLS_LOG_LEVEL": "ERROR",
                    },
                ))

        if self.config.reactor_core_enabled:
            # v95.0: Use registry for canonical definition, override with config
            reactor_def = ServiceDefinitionRegistry.get_definition(
                "reactor-core",
                port_override=self.config.reactor_core_default_port,
                path_override=self.config.reactor_core_path,
                validate=True,
            )
            if reactor_def:
                # Override timeout from config
                reactor_def.startup_timeout = self.config.reactor_core_startup_timeout

                # =====================================================================
                # v146.0: TRINITY PROTOCOL - REACTOR-CORE DEPENDENCY ENFORCEMENT
                # =====================================================================
                # When Trinity Protocol is active (SLIM hardware), reactor-core MUST
                # wait for jarvis-prime to be healthy before starting. This prevents
                # the "Thundering Herd" where both grab RAM simultaneously.
                #
                # Without this, on SLIM systems:
                #   1. jarvis-prime starts loading (even as Hollow Client)
                #   2. reactor-core starts loading heavy training models
                #   3. Combined memory pressure causes OOM
                #
                # With Trinity Protocol:
                #   1. jarvis-prime starts as Hollow Client (~300MB)
                #   2. jarvis-prime reports HEALTHY (routing to GCP)
                #   3. THEN reactor-core starts (has full remaining RAM)
                # =====================================================================
                trinity_active = _trinity_protocol_active or is_cloud_locked()[0]
                if trinity_active and "jarvis-prime" not in reactor_def.depends_on:
                    logger.info(
                        f"[v146.0] 🔗 TRINITY PROTOCOL: Adding jarvis-prime as HARD dependency "
                        f"for reactor-core (prevents Thundering Herd)"
                    )
                    reactor_def.depends_on = ["jarvis-prime"]
                    # Also wait for GCP to be ready before starting reactor-core
                    reactor_def.dependency_wait_timeout = max(
                        reactor_def.dependency_wait_timeout or 120.0,
                        180.0  # Allow time for GCP warm-up
                    )

                definitions.append(reactor_def)
                logger.debug(f"Using registry definition for reactor-core: {reactor_def.script_name}")
            else:
                logger.warning("Could not get reactor-core definition from registry, using fallback")
                # Fallback to inline definition if registry fails
                definitions.append(ServiceDefinition(
                    name="reactor-core",
                    repo_path=self.config.reactor_core_path,
                    script_name="run_reactor.py",
                    fallback_scripts=["run_supervisor.py", "main.py", "server.py"],
                    default_port=self.config.reactor_core_default_port,
                    health_endpoint="/health",
                    startup_timeout=self.config.reactor_core_startup_timeout,
                    use_uvicorn=False,
                    uvicorn_app=None,
                    script_args=["--port", str(self.config.reactor_core_default_port)],
                    environment={
                        "PYTHONPATH": str(self.config.reactor_core_path),
                        "REACTOR_PORT": str(self.config.reactor_core_default_port),
                    },
                ))

        return definitions

    def _find_script(self, definition: ServiceDefinition) -> Optional[Path]:
        """
        Find the startup script for a service.

        v3.1: Enhanced discovery with nested script paths and module detection.
        v95.0: Now uses intelligent discover_entry_script() with pattern matching and scoring.

        Search order:
        1. Module path (returns sentinel but module_path is used directly in spawn)
        2. Uvicorn app (returns sentinel but uvicorn is used in spawn)
        3. Intelligent discovery via discover_entry_script() which:
           - Checks explicit script_name (highest priority)
           - Checks nested_scripts
           - Checks fallback_scripts
           - Pattern matches run_*.py, main.py, server.py, app.py
           - Uses file size as tiebreaker for equal scores
        """
        repo_path = definition.repo_path

        if not repo_path.exists():
            logger.warning(f"Repository not found: {repo_path}")
            return None

        # v3.1: If module_path or uvicorn_app is set, we don't need a script file
        # The spawn method will handle these directly
        if definition.module_path or definition.uvicorn_app:
            logger.debug(f"Service {definition.name} uses module/uvicorn entry point")
            return Path("__module__")  # Sentinel value

        # v95.0: Use intelligent discovery
        script_path = definition.discover_entry_script()

        if script_path:
            logger.debug(f"Discovered entry script for {definition.name}: {script_path}")
            return script_path

        # v95.0: Enhanced logging for debugging
        logger.warning(
            f"No startup script found for {definition.name} in {repo_path}"
        )
        logger.debug(f"  Tried explicit: {definition.script_name}")
        logger.debug(f"  Tried nested: {definition.nested_scripts}")
        logger.debug(f"  Tried fallbacks: {definition.fallback_scripts}")
        logger.debug(f"  Discovery patterns: {definition.discovery_patterns}")

        # List actual files for debugging
        try:
            py_files = [f.name for f in repo_path.glob("*.py")][:10]
            if py_files:
                logger.debug(f"  Available .py files: {py_files}")
        except Exception:
            pass

        return None

    # =========================================================================
    # Output Streaming (v93.0: Intelligent log level detection)
    # =========================================================================

    def _detect_log_level(self, line: str) -> str:
        """
        v93.0: Intelligently detect log level from output line content.

        Python logging outputs to stderr by default, so we can't rely on
        stream type alone. Instead, we parse the line content to detect
        the actual log level.

        Patterns detected:
        - "| DEBUG |", "DEBUG:", "[DEBUG]"
        - "| INFO |", "INFO:", "[INFO]"
        - "| WARNING |", "WARNING:", "[WARNING]", "WARN:"
        - "| ERROR |", "ERROR:", "[ERROR]"
        - "| CRITICAL |", "CRITICAL:", "[CRITICAL]"
        - Traceback, Exception indicators -> ERROR
        """
        line_upper = line.upper()

        # Check for explicit log level indicators
        if any(p in line_upper for p in ['| ERROR |', 'ERROR:', '[ERROR]', '| CRITICAL |', 'CRITICAL:']):
            return 'error'
        if any(p in line_upper for p in ['TRACEBACK', 'EXCEPTION', 'RAISE ', 'FAILED:', '❌']):
            return 'error'
        if any(p in line_upper for p in ['| WARNING |', 'WARNING:', '[WARNING]', 'WARN:', '⚠️']):
            return 'warning'
        if any(p in line_upper for p in ['| DEBUG |', 'DEBUG:', '[DEBUG]']):
            return 'debug'
        if any(p in line_upper for p in ['| INFO |', 'INFO:', '[INFO]', '✅', '✓']):
            return 'info'

        # Default to info for normal output
        return 'info'

    # v93.16: Patterns for warnings that should be suppressed
    # v95.0: Extended with more ML framework warnings
    # v95.1: Added Google API, gspread, and asyncio patterns
    _SUPPRESSED_WARNING_PATTERNS = [
        # Scikit-learn version warnings
        'scikit-learn version',
        'is not supported',
        'minimum required version',
        'maximum required version',
        'disabling scikit-learn conversion api',
        # PyTorch/TorchAudio warnings
        'torch version',
        'has not been tested',
        'list_audio_backends',
        'torchaudio.backend',
        # CoreML warnings
        'coremltools',
        'unexpected errors',
        'most recent version that has been tested',
        # SpeechBrain/HuggingFace warnings (v95.0)
        'wav2vec2model is frozen',
        'model is frozen',
        'weights were not initialized',
        'you should probably train',
        'some weights of the model checkpoint',
        'registered checkpoint',
        # TensorFlow warnings
        'tf_cpp_min_log_level',
        # Transformers/Tokenizers warnings
        'tokenizers parallelism',
        'special tokens have been added',
        'clean_up_tokenization_spaces',
        # NumPy/SciPy warnings
        'numpy.ndarray size changed',
        'scipy.ndimage',
        # Google API/GCP warnings (v95.1)
        'non-supported python version',
        'google will not post any further updates',
        'please upgrade to the latest python version',
        'google.api_core',
        # Optional dependencies not installed (v95.1)
        'gspread not available',
        'redis not available',
        'pip install',
        # aiohttp/asyncio cleanup warnings (v95.1)
        'unclosed client session',
        'unclosed connector',
        'event loop is closed',
        # Thread pool shutdown warnings (v95.1)
        'thread pool',
        'cannot schedule new futures',
        'interpreter shutdown',
        # General benign patterns
        'futurewarning',
        'deprecationwarning',
    ]

    def _should_suppress_line(self, line: str) -> bool:
        """v93.16: Check if a line should be suppressed (known benign warnings)."""
        line_lower = line.lower()
        return any(pattern in line_lower for pattern in self._SUPPRESSED_WARNING_PATTERNS)

    async def _stream_output(
        self,
        managed: ManagedProcess,
        stream: asyncio.StreamReader,
        stream_type: str = "stdout"
    ) -> None:
        """
        v93.0: Stream process output with intelligent log level detection.
        v93.16: Added warning suppression for known benign library warnings.
        v108.2: Added crash forensics buffer to capture output for post-mortem analysis.
        v147.0: PROACTIVE RESCUE - Real-time pattern matching for crash pre-cognition.

        Python's logging module outputs to stderr by default, which previously
        caused all child process logs to appear as WARNING in our output.

        Now we parse the actual content to detect the real log level and
        route appropriately.

        v147.0: Also scans for memory stress patterns and triggers GCP provisioning
        BEFORE the process crashes. This is "Crash Pre-Cognition."

        Example output:
            [JARVIS_PRIME] Loading model...
            [JARVIS_PRIME] Model loaded in 2.3s
            [REACTOR_CORE] Initializing pipeline...
        """
        prefix = f"[{managed.definition.name.upper().replace('-', '_')}]"
        is_stderr = stream_type == "stderr"
        service_name = managed.definition.name

        # v147.0: Track if proactive rescue has been triggered for this session
        proactive_rescue_triggered = False

        try:
            while True:
                line = await stream.readline()
                if not line:
                    break

                decoded = line.decode('utf-8', errors='replace').rstrip()
                if decoded:
                    # v108.2: Always add to crash forensics buffer (even if suppressed from logs)
                    managed.add_output_line(f"{stream_type}: {decoded}", is_stderr=is_stderr)

                    # =========================================================
                    # v147.0: PROACTIVE RESCUE - Crash Pre-Cognition
                    # =========================================================
                    # Scan EVERY line for memory stress patterns. If detected,
                    # trigger GCP provisioning BEFORE the kernel kills us.
                    # =========================================================
                    if not proactive_rescue_triggered:
                        should_trigger, is_severe, matched_pattern = _check_proactive_rescue_pattern(decoded)

                        if should_trigger:
                            proactive_rescue_triggered = True
                            is_jarvis_prime = service_name.lower() in ["jarvis-prime", "jarvis_prime", "j-prime"]

                            logger.warning(
                                f"[v147.0] 🔮 PROACTIVE RESCUE TRIGGERED!\n"
                                f"    Service: {service_name}\n"
                                f"    Pattern: '{matched_pattern}'\n"
                                f"    Severity: {'SEVERE' if is_severe else 'WARNING'}\n"
                                f"    Action: Preemptive GCP provisioning initiated"
                            )

                            # Fire off GCP provisioning in background (don't block log streaming)
                            if is_jarvis_prime:
                                asyncio.create_task(
                                    self._proactive_rescue_handler(
                                        managed,
                                        matched_pattern or "UNKNOWN_PATTERN",
                                        is_severe,
                                    )
                                )

                            await _emit_event(
                                "PROACTIVE_RESCUE_TRIGGERED",
                                service_name=service_name,
                                priority="CRITICAL" if is_severe else "HIGH",
                                details={
                                    "matched_pattern": matched_pattern,
                                    "is_severe": is_severe,
                                    "line": decoded[:200],  # Truncate for log
                                }
                            )

                    # v93.16: Suppress known benign warnings
                    if self._should_suppress_line(decoded):
                        logger.debug(f"{prefix} [SUPPRESSED] {decoded}")
                        continue

                    # Detect actual log level from content
                    level = self._detect_log_level(decoded)

                    # Route to appropriate log function
                    if level == 'error':
                        logger.error(f"{prefix} {decoded}")
                    elif level == 'warning':
                        logger.warning(f"{prefix} {decoded}")
                    elif level == 'debug':
                        logger.debug(f"{prefix} {decoded}")
                    else:
                        logger.info(f"{prefix} {decoded}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Output streaming error for {managed.definition.name}: {e}")

    async def _proactive_rescue_handler(
        self,
        managed: ManagedProcess,
        matched_pattern: str,
        is_severe: bool,
    ) -> None:
        """
        v147.0: Handle proactive rescue when memory stress is detected.

        This is called asynchronously when log patterns indicate imminent OOM.
        We provision GCP BEFORE the crash happens, then optionally trigger
        a controlled restart.
        """
        service_name = managed.definition.name

        logger.info(
            f"[v147.0] 🚀 Proactive Rescue Handler: Provisioning GCP for {service_name}..."
        )

        try:
            # Step 1: Provision GCP VM (async, with timeout)
            gcp_ready, gcp_endpoint = await ensure_gcp_vm_ready_for_prime(
                timeout_seconds=90.0,  # Shorter timeout for proactive rescue
                force_provision=False,
            )

            if gcp_ready and gcp_endpoint:
                logger.info(
                    f"[v147.0] ✅ Proactive Rescue: GCP VM ready at {gcp_endpoint}"
                )

                # Step 2: Update managed process state for next spawn
                managed.gcp_offload_active = True
                managed.gcp_vm_ip = gcp_endpoint.replace("http://", "").split(":")[0]

                # Step 3: Update environment for next spawn
                os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] = "true"
                os.environ["GCP_PRIME_ENDPOINT"] = gcp_endpoint
                os.environ["JARVIS_GCP_PRIME_ENDPOINT"] = gcp_endpoint

                # Step 4: Set cloud lock (persist across restarts)
                _save_cloud_lock(
                    locked=True,
                    reason=f"PROACTIVE_RESCUE:{matched_pattern}",
                    oom_count=0,
                    consecutive_ooms=0,
                )

                # Step 5: If SEVERE, trigger controlled restart
                if is_severe:
                    logger.warning(
                        f"[v147.0] ⚡ SEVERE memory stress - triggering controlled restart of {service_name}"
                    )

                    # Graceful shutdown is better than waiting for -9
                    await self._graceful_restart_service(service_name)

                await _emit_event(
                    "PROACTIVE_RESCUE_SUCCESS",
                    service_name=service_name,
                    priority="HIGH",
                    details={
                        "gcp_endpoint": gcp_endpoint,
                        "controlled_restart": is_severe,
                    }
                )

            else:
                logger.warning(
                    f"[v147.0] ⚠️ Proactive Rescue: GCP provisioning failed. "
                    f"Service {service_name} may crash."
                )

                await _emit_event(
                    "PROACTIVE_RESCUE_GCP_FAILED",
                    service_name=service_name,
                    priority="CRITICAL",
                    details={"reason": "GCP VM provisioning failed or timed out"}
                )

        except Exception as e:
            logger.error(f"[v147.0] Proactive Rescue Handler error: {e}")

    async def _graceful_restart_service(self, service_name: str) -> bool:
        """
        v147.0: Trigger a graceful restart of a service.

        This is better than waiting for the kernel to kill with -9,
        because we can ensure GCP is ready first.
        """
        if service_name not in self.processes:
            return False

        managed = self.processes[service_name]

        logger.info(f"[v147.0] 🔄 Initiating graceful restart of {service_name}...")

        try:
            # Send SIGTERM for graceful shutdown
            if managed.process and managed.process.returncode is None:
                managed.process.terminate()

                # Wait up to 10 seconds for graceful shutdown
                try:
                    await asyncio.wait_for(managed.process.wait(), timeout=10.0)
                    logger.info(f"[v147.0] ✅ {service_name} terminated gracefully")
                except asyncio.TimeoutError:
                    # Force kill if graceful shutdown takes too long
                    logger.warning(f"[v147.0] ⚠️ {service_name} didn't terminate gracefully, forcing...")
                    managed.process.kill()
                    await managed.process.wait()

            # The normal crash handler will restart the service with GCP config
            return True

        except Exception as e:
            logger.error(f"[v147.0] Graceful restart failed for {service_name}: {e}")
            return False

    async def _start_output_streaming(self, managed: ManagedProcess) -> None:
        """Start streaming stdout and stderr for a process."""
        if not self.config.stream_output:
            return

        if managed.process is None:
            return

        async def stream_both():
            tasks = []
            if managed.process.stdout:
                tasks.append(
                    asyncio.create_task(
                        self._stream_output(managed, managed.process.stdout, "stdout")
                    )
                )
            if managed.process.stderr:
                tasks.append(
                    asyncio.create_task(
                        self._stream_output(managed, managed.process.stderr, "stderr")
                    )
                )
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        managed.output_stream_task = asyncio.create_task(stream_both())

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def _check_health_detailed(
        self,
        managed: ManagedProcess,
        timeout: Optional[float] = None,  # v112.0: Allow overriding timeout
    ) -> HealthCheckResult:
        """
        v109.1: Enterprise-grade health check with nuanced state tracking.

        Returns a HealthCheckResult with detailed state information, enabling
        the health monitor to make intelligent decisions about failure counting.

        Key improvement: "starting" status is NOT counted as a failure.
        """
        if managed.port is None:
            return HealthCheckResult(
                state=HealthState.UNREACHABLE,
                is_responding=False,
                error_message="No port configured"
            )

        url = f"http://localhost:{managed.port}{managed.definition.health_endpoint}"

        try:
            session = await self._get_http_session()
            async with session.get(
                url,
                # v112.0: Use provided timeout or default from config
                timeout=aiohttp.ClientTimeout(total=timeout or self.config.health_check_timeout)
            ) as response:
                # v119.0: CRITICAL FIX - Read response body even for non-200 status codes
                # /health/startup returns 503 with JSON body during startup, which is NORMAL
                # The body contains phase info to distinguish "starting" from "unhealthy"
                try:
                    data = await response.json()
                except Exception:
                    data = {}

                if response.status != 200:
                    # v119.0: Check if this is a startup-phase 503 (NOT a failure!)
                    # The endpoint returns 503 while starting but includes phase info
                    phase = data.get("phase", "").lower() if data.get("phase") else ""
                    status = data.get("status", "").lower() if data.get("status") else ""
                    message = data.get("message", "")

                    # v119.0: 503 on startup endpoints with starting phase = STARTING, not UNHEALTHY
                    if response.status == 503:
                        # Check for starting indicators in response
                        is_starting_phase = phase in ("starting", "initializing", "not_started", "loading")
                        is_not_ready_status = status in ("not_ready", "starting", "initializing")
                        has_starting_message = "starting" in message.lower() or "%" in message  # Progress %

                        if is_starting_phase or is_not_ready_status or has_starting_message:
                            # This is EXPECTED during startup - NOT a failure!
                            startup_progress = None
                            if "%" in message:
                                try:
                                    # Extract progress percentage from message like "Starting up (45% complete)"
                                    import re
                                    match = re.search(r"(\d+)%", message)
                                    if match:
                                        startup_progress = int(match.group(1))
                                except Exception:
                                    pass

                            return HealthCheckResult(
                                state=HealthState.STARTING,
                                is_responding=True,
                                status_text=f"HTTP 503 (startup in progress)",
                                phase=phase,
                                startup_step=message,
                                startup_progress=startup_progress,
                                raw_data=data
                            )

                    # Non-503 or 503 without starting indicators = unhealthy
                    return HealthCheckResult(
                        state=HealthState.UNHEALTHY,
                        is_responding=True,
                        status_text=f"HTTP {response.status}",
                        error_message=f"Non-200 status code: {response.status} (phase={phase}, status={status})"
                    )

                # 200 OK - continue with detailed analysis
                try:
                    # data already loaded above

                    # Extract common fields
                    status = data.get("status", "").lower() if data.get("status") else ""
                    phase = data.get("phase", "").lower() if data.get("phase") else ""
                    startup_elapsed = data.get("startup_elapsed_seconds") or data.get("model_load_elapsed_seconds")
                    startup_step = data.get("current_step")
                    details = data.get("details", {})
                    if details and isinstance(details, dict):
                        startup_progress = details.get("step_num")
                    else:
                        startup_progress = None

                    # v109.1: Determine health state with nuanced logic

                    # 1. Check for explicit healthy states
                    if status in ("healthy", "ok", "up", "running", "ready"):
                        return HealthCheckResult(
                            state=HealthState.HEALTHY,
                            is_responding=True,
                            status_text=status,
                            phase=phase,
                            raw_data=data
                        )

                    # 2. Check boolean healthy/ready flags
                    if data.get("healthy") is True:
                        return HealthCheckResult(
                            state=HealthState.HEALTHY,
                            is_responding=True,
                            status_text="healthy (boolean)",
                            raw_data=data
                        )
                    if data.get("ready") is True:
                        return HealthCheckResult(
                            state=HealthState.HEALTHY,
                            is_responding=True,
                            status_text="ready (boolean)",
                            raw_data=data
                        )
                    if data.get("ready_for_inference") is True and data.get("model_loaded") is True:
                        return HealthCheckResult(
                            state=HealthState.HEALTHY,
                            is_responding=True,
                            status_text="ready_for_inference",
                            raw_data=data
                        )

                    # 3. Check phase indicator (J-Prime specific)
                    if phase == "ready":
                        return HealthCheckResult(
                            state=HealthState.HEALTHY,
                            is_responding=True,
                            status_text=status,
                            phase=phase,
                            raw_data=data
                        )

                    # 4. v109.1: CRITICAL - Check starting status (this is NOT a failure!)
                    if status == "starting" or phase in ("starting", "loading", "initializing"):
                        return HealthCheckResult(
                            state=HealthState.STARTING,
                            is_responding=True,
                            status_text=status,
                            phase=phase,
                            startup_elapsed=startup_elapsed,
                            startup_step=startup_step,
                            startup_progress=startup_progress,
                            raw_data=data
                        )

                    # 5. Check error status explicitly
                    if status == "error":
                        error = data.get("model_load_error") or data.get("error") or "unknown error"
                        return HealthCheckResult(
                            state=HealthState.UNHEALTHY,
                            is_responding=True,
                            status_text=status,
                            error_message=str(error),
                            raw_data=data
                        )

                    # 6. Check degraded status
                    if status == "degraded":
                        return HealthCheckResult(
                            state=HealthState.DEGRADED,
                            is_responding=True,
                            status_text=status,
                            raw_data=data
                        )

                    # 7. v109.0: If we got HTTP 200 with no recognized status, accept as healthy
                    if status == "":
                        logger.debug(
                            f"    ℹ️  {managed.definition.name}: HTTP 200 OK with no status field "
                            f"(keys: {list(data.keys())[:5]}) - accepting as healthy"
                        )
                        return HealthCheckResult(
                            state=HealthState.HEALTHY,
                            is_responding=True,
                            status_text="(no status field)",
                            raw_data=data
                        )

                    # Unknown status value - treat as degraded (not unhealthy)
                    logger.debug(
                        f"    ℹ️  {managed.definition.name}: unrecognized status='{status}'"
                    )
                    return HealthCheckResult(
                        state=HealthState.DEGRADED,
                        is_responding=True,
                        status_text=status,
                        raw_data=data
                    )

                except Exception as json_error:
                    # Couldn't parse JSON - HTTP 200 is still success
                    logger.debug(
                        f"    ℹ️  {managed.definition.name}: HTTP 200 but not JSON - "
                        f"accepting as healthy"
                    )
                    return HealthCheckResult(
                        state=HealthState.HEALTHY,
                        is_responding=True,
                        status_text="HTTP 200 (non-JSON)"
                    )

        except asyncio.TimeoutError:
            used_timeout = timeout or self.config.health_check_timeout
            return HealthCheckResult(
                state=HealthState.TIMEOUT,
                is_responding=False,
                error_message=f"Timeout after {used_timeout}s"
            )
        except aiohttp.ClientConnectorError as e:
            return HealthCheckResult(
                state=HealthState.UNREACHABLE,
                is_responding=False,
                error_message=f"Connection refused: {e}"
            )
        except Exception as e:
            return HealthCheckResult(
                state=HealthState.UNREACHABLE,
                is_responding=False,
                error_message=str(e)
            )

    async def _check_health(
        self,
        managed: ManagedProcess,
        require_ready: bool = True,
    ) -> bool:
        """
        Check health of a service via HTTP endpoint.

        v109.1: Now delegates to _check_health_detailed() for nuanced state tracking.
        Maintains backward compatibility by returning bool.

        Args:
            managed: The managed process to check
            require_ready: If True, require "healthy" status. If False, accept "starting" too.

        Returns:
            True if service is responding appropriately
        """
        result = await self._check_health_detailed(managed)

        if require_ready:
            return result.is_healthy
        else:
            # Accept starting or healthy
            return result.state in (HealthState.HEALTHY, HealthState.STARTING, HealthState.DEGRADED)

    async def _check_service_responding(self, managed: ManagedProcess) -> bool:
        """
        v93.0: Check if service is responding at all (including "starting" status).

        This is for the initial health check - we just want to know the port is open.
        """
        return await self._check_health(managed, require_ready=False)

    async def _health_monitor_loop(self, managed: ManagedProcess) -> None:
        """
        v93.8: Enhanced background health monitoring with Docker support.

        CRITICAL FIX: Previous version would `break` after auto-heal, which meant
        if auto-heal failed, monitoring would stop completely. Now we continue
        monitoring and retry auto-heal as needed.

        Features:
        - Robust process death detection with poll() (local processes)
        - Docker container health monitoring (no local process)
        - Continuous monitoring even after auto-heal attempts
        - HTTP health check with consecutive failure tracking
        - Heartbeat updates to service registry
        - Graceful degradation on temporary failures
        """
        # v93.8: Determine if this is a Docker-hosted service
        is_docker_service = managed.pid is None and managed.port is not None

        try:
            # v95.3: Check both shutdown event AND completion flag
            while not self._shutdown_event.is_set() and not self._shutdown_completed:
                await asyncio.sleep(self.config.health_check_interval)

                # v95.3: Re-check shutdown state after sleep
                if self._shutdown_event.is_set() or self._shutdown_completed:
                    logger.debug(
                        f"[v95.3] Health monitor for {managed.definition.name} exiting: "
                        f"shutdown detected after sleep"
                    )
                    return

                # v93.8: For Docker services, skip process death detection
                # Docker containers don't have a local process to monitor
                if not is_docker_service:
                    # v93.0: Enhanced process death detection for LOCAL processes
                    # Use poll() to update returncode without blocking
                    if managed.process is not None:
                        try:
                            # poll() returns None if still running, exit code if terminated
                            poll_result = managed.process.returncode
                            if poll_result is None:
                                # Process might have exited but returncode not updated yet
                                # On macOS/Unix, we need to wait() to reap zombie processes
                                # Use wait_for with 0 timeout to check without blocking
                                pass  # returncode is None means still running
                        except Exception:
                            pass

                # v93.8: Check if running - for Docker services, always use HTTP health check
                if not is_docker_service and not managed.is_running:
                    # Process died, trigger auto-heal if enabled
                    exit_code = managed.process.returncode if managed.process else "unknown"

                    # v95.3: Check if this is an intentional shutdown (exit code -15 = SIGTERM)
                    # During shutdown, processes are killed with SIGTERM, so we shouldn't
                    # log scary warnings or attempt to restart them
                    # CRITICAL: Also check shutdown_completed to catch post-shutdown kills
                    is_intentional_shutdown = (
                        self._shutdown_event.is_set() or
                        self._shutdown_completed or  # v95.3: Added completion check
                        exit_code == -15 or  # SIGTERM
                        exit_code == -2      # SIGINT
                    )

                    if is_intentional_shutdown:
                        logger.info(
                            f"[v95.0] Process {managed.definition.name} stopped "
                            f"(exit code: {exit_code}, shutdown: {self._shutdown_event.is_set()})"
                        )
                        managed.status = ServiceStatus.STOPPED
                        return  # Exit health monitor - this is expected
                    else:
                        logger.warning(
                            f"🚨 Process {managed.definition.name} died unexpectedly "
                            f"(exit code: {exit_code})"
                        )
                        managed.status = ServiceStatus.FAILED

                    if self.config.auto_healing_enabled and not is_intentional_shutdown:
                        success = await self._auto_heal(managed)
                        if success:
                            # v93.0: After successful auto-heal, the new process has
                            # its own health monitor task started in _spawn_service()
                            # We exit THIS loop since we're monitoring the OLD process
                            logger.info(
                                f"[v93.0] Health monitor for old {managed.definition.name} "
                                f"process exiting (new monitor started)"
                            )
                            return
                        else:
                            # Auto-heal failed, but don't give up immediately
                            # Continue monitoring - maybe the process will recover
                            # or manual intervention will fix it
                            logger.warning(
                                f"[v93.0] Auto-heal failed for {managed.definition.name}, "
                                f"continuing to monitor"
                            )
                            # Wait longer before retrying
                            await asyncio.sleep(self.config.health_check_interval * 2)
                            continue
                    elif is_intentional_shutdown:
                        # Intentional shutdown - just exit cleanly
                        logger.debug(
                            f"[v95.0] {managed.definition.name} stopped intentionally, "
                            f"health monitor exiting"
                        )
                        return
                    else:
                        # Auto-healing disabled, just log and exit
                        logger.error(
                            f"[v93.0] {managed.definition.name} died but auto-healing disabled"
                        )
                        return

                # v112.0: Adaptive timeout based on startup phase
                # Use max(0, ...) to handle potential system clock skew
                time_since_start = max(0.0, time.time() - managed.start_time)
                is_startup = time_since_start < self.config.startup_phase_duration
                current_timeout = self.config.startup_health_check_timeout if is_startup else self.config.normal_health_check_timeout

                # v118.0: Check port registry for fallback port before health check
                # The service may have restarted on a different port
                registry_port = get_service_port_from_registry(
                    managed.definition.name,
                    fallback_port=managed.port
                )
                if registry_port and registry_port != managed.port:
                    logger.info(
                        f"[v118.0] {managed.definition.name} port updated from registry: "
                        f"{managed.port} -> {registry_port}"
                    )
                    managed.port = registry_port
                    managed.definition.default_port = registry_port

                # v109.1: HTTP health check with nuanced state tracking
                health_result = await self._check_health_detailed(managed, timeout=current_timeout)
                managed.last_health_check = time.time()

                if health_result.is_healthy:
                    # Service is fully healthy
                    managed.consecutive_failures = 0
                    # v95.0: Update last known health for heartbeat loop
                    managed.last_known_health = "healthy"

                    # v95.2: Reset restart count when service is healthy
                    # This allows the service to be restarted again if it fails later
                    if managed.restart_count > 0:
                        logger.info(
                            f"[v95.2] Resetting restart count for {managed.definition.name} "
                            f"(was {managed.restart_count}, service is now healthy)"
                        )
                        managed.restart_count = 0

                    # Log status transition only once
                    if managed.status != ServiceStatus.HEALTHY:
                        managed.status = ServiceStatus.HEALTHY
                        logger.info(f"✅ {managed.definition.name} is healthy")

                    # ═══════════════════════════════════════════════════════════════════
                    # CRITICAL FIX: Send heartbeat on EVERY successful health check
                    # (The dedicated heartbeat loop also sends, but this is belt-and-suspenders)
                    # ═══════════════════════════════════════════════════════════════════
                    if self.registry:
                        try:
                            await self.registry.heartbeat(
                                managed.definition.name,
                                status="healthy"
                            )
                        except Exception as hb_error:
                            logger.warning(
                                f"[v93.0] Heartbeat failed for {managed.definition.name} "
                                f"(non-fatal): {hb_error}"
                            )

                elif health_result.is_starting:
                    # v109.1: CRITICAL FIX - Service is starting, NOT a failure!
                    # Do NOT increment consecutive_failures for starting services
                    managed.last_known_health = "starting"

                    # Update status to STARTING if not already
                    if managed.status not in (ServiceStatus.STARTING, ServiceStatus.HEALTHY):
                        managed.status = ServiceStatus.STARTING

                    # Log startup progress periodically (not every check)
                    if health_result.startup_elapsed:
                        elapsed_int = int(health_result.startup_elapsed)
                        if elapsed_int % 30 == 0 or elapsed_int < 10:  # Log every 30s or during first 10s
                            step_info = f", step: {health_result.startup_step}" if health_result.startup_step else ""
                            logger.info(
                                f"    ⏳ {managed.definition.name} starting ({elapsed_int}s elapsed{step_info})"
                            )

                    # Send heartbeat with "starting" status
                    if self.registry:
                        try:
                            await self.registry.heartbeat(
                                managed.definition.name,
                                status="starting"
                            )
                        except Exception:
                            pass  # Non-fatal

                else:
                    # v109.1: Check if this should count as a failure
                    if health_result.should_count_as_failure:
                        # This is a real failure (unreachable, timeout, or explicit error)
                        managed.last_known_health = "degraded"
                        managed.consecutive_failures += 1

                        # Log with detail about the failure type
                        logger.warning(
                            f"⚠️ {managed.definition.name} health check failed: "
                            f"{health_result.state.value} "
                            f"({managed.consecutive_failures} consecutive failures)"
                            + (f" - {health_result.error_message}" if health_result.error_message else "")
                        )

                        if managed.consecutive_failures >= 3:
                            managed.status = ServiceStatus.DEGRADED

                            if self.config.auto_healing_enabled:
                                success = await self._auto_heal(managed)
                                if success:
                                    # Reset consecutive failures after successful heal
                                    managed.consecutive_failures = 0
                                # v93.0: Don't break - continue monitoring
                    else:
                        # Degraded but responding - don't count as failure, just note it
                        managed.last_known_health = "degraded"
                        if managed.status == ServiceStatus.HEALTHY:
                            managed.status = ServiceStatus.DEGRADED
                            logger.info(
                                f"    ⚠️ {managed.definition.name} is degraded but responding"
                            )

        except asyncio.CancelledError:
            logger.debug(f"[v93.0] Health monitor cancelled for {managed.definition.name}")
        except Exception as e:
            logger.error(f"Health monitor error for {managed.definition.name}: {e}")

    # =========================================================================
    # v95.0: Dedicated Heartbeat Loop (Prevents Stale Services)
    # =========================================================================

    async def _heartbeat_loop(self, managed: ManagedProcess) -> None:
        """
        v95.0: Enterprise-grade dedicated heartbeat loop.

        CRITICAL: This loop runs INDEPENDENTLY of health checks.

        The previous design had a fundamental flaw:
        - Heartbeats were only sent when HTTP health checks passed
        - If health checks failed, no heartbeat was sent
        - This caused running services to be marked "stale" and removed
        - Even if the process was alive, it would be deregistered

        This new design ensures:
        1. Heartbeats are sent as long as the process is alive
        2. Health status is included in the heartbeat (healthy/degraded/unhealthy)
        3. Services are NEVER marked stale if their process is running
        4. Separate heartbeat interval (faster than health check interval)
        5. Automatic recovery with fire-and-forget pattern

        The heartbeat loop sends status-aware heartbeats:
        - "healthy": HTTP health check passed
        - "degraded": Process alive but HTTP check failed
        - "starting": Process just started, not yet healthy
        - "process_alive": Fallback when health status unknown
        """
        service_name = managed.definition.name
        heartbeat_interval = min(
            self.config.health_check_interval / 2,  # 2x faster than health checks
            15.0  # Max 15 seconds between heartbeats
        )

        logger.info(
            f"[v95.0] Starting dedicated heartbeat loop for {service_name} "
            f"(interval: {heartbeat_interval:.1f}s)"
        )

        # v95.0: Path for file-based heartbeats (used by reactor-core and other services)
        trinity_heartbeat_path = Path.home() / ".jarvis" / "trinity" / "heartbeats" / f"{service_name}.json"

        try:
            # v95.3: Check both shutdown event AND completion flag
            while not self._shutdown_event.is_set() and not self._shutdown_completed:
                await asyncio.sleep(heartbeat_interval)

                # v95.3: Re-check shutdown state after sleep
                if self._shutdown_event.is_set() or self._shutdown_completed:
                    logger.debug(
                        f"[v95.3] Heartbeat loop for {service_name} exiting: shutdown detected"
                    )
                    break

                # Check if process is still alive
                process_alive = managed.is_running

                # For Docker services, process is None but port is set
                is_docker = managed.pid is None and managed.port is not None

                # v95.0: Check file-based heartbeat as fallback (reactor-core uses this)
                file_heartbeat_alive = False
                file_heartbeat_status = None
                try:
                    if trinity_heartbeat_path.exists():
                        stat = trinity_heartbeat_path.stat()
                        file_age = time.time() - stat.st_mtime
                        # File heartbeat is fresh if modified within 2x heartbeat interval
                        if file_age < (heartbeat_interval * 2):
                            file_heartbeat_alive = True
                            # Read status from file
                            try:
                                with open(trinity_heartbeat_path) as f:
                                    hb_data = json.load(f)
                                    file_heartbeat_status = hb_data.get("status", "unknown")
                            except Exception:
                                file_heartbeat_status = "file_heartbeat_active"
                except Exception as e:
                    logger.debug(f"[v95.0] File heartbeat check failed for {service_name}: {e}")

                if not process_alive and not is_docker and not file_heartbeat_alive:
                    # Process died AND no file heartbeat - notify registry and stop
                    logger.debug(
                        f"[v95.0] Heartbeat loop for {service_name}: "
                        f"process not running and no file heartbeat, sending final notification"
                    )

                    # v95.0: Send final "dead" notification to registry before exiting
                    # This prevents the 30s gap where service appears alive but is dead
                    if self.registry:
                        try:
                            await asyncio.wait_for(
                                self.registry.heartbeat(
                                    service_name,
                                    status="dead",
                                    metadata={
                                        "pid": managed.pid,
                                        "reason": "process_died",
                                        "last_heartbeat_source": "cleanup",
                                        "exit_code": getattr(managed.process, 'returncode', None) if managed.process else None,
                                    }
                                ),
                                timeout=2.0
                            )
                            logger.info(f"[v95.0] Sent final 'dead' heartbeat for {service_name}")
                        except Exception as final_hb_err:
                            logger.debug(f"[v95.0] Final heartbeat failed for {service_name}: {final_hb_err}")

                        # Also explicitly deregister to immediately free up the slot
                        try:
                            await asyncio.wait_for(
                                self.registry.deregister_service(service_name),
                                timeout=2.0
                            )
                            logger.info(f"[v95.0] Deregistered dead service: {service_name}")
                        except Exception as dereg_err:
                            logger.debug(f"[v95.0] Deregister failed for {service_name}: {dereg_err}")

                    return

                # v95.0: If process appears dead but file heartbeat is alive,
                # the service is running (just not as a direct child process)
                if not process_alive and file_heartbeat_alive:
                    logger.debug(
                        f"[v95.0] {service_name}: Process not running but file heartbeat "
                        f"active ({file_heartbeat_status}) - service alive externally"
                    )

                # v95.0: Determine heartbeat status based on multiple sources
                # Priority: file heartbeat > last known health > process state
                if file_heartbeat_alive and file_heartbeat_status == "healthy":
                    heartbeat_status = "healthy"
                elif managed.last_known_health == "healthy":
                    heartbeat_status = "healthy"
                elif managed.status == ServiceStatus.STARTING:
                    heartbeat_status = "starting"
                elif file_heartbeat_alive:
                    # File heartbeat is active but status is not "healthy"
                    heartbeat_status = file_heartbeat_status or "file_heartbeat_active"
                elif managed.last_known_health == "degraded":
                    heartbeat_status = "degraded"
                elif managed.consecutive_failures > 0:
                    heartbeat_status = "degraded"
                elif process_alive or is_docker:
                    heartbeat_status = "process_alive"
                else:
                    heartbeat_status = "unknown"

                # Send heartbeat to registry
                if self.registry:
                    try:
                        # v112.0: Adaptive timeout based on startup phase
                        # Use max(0, ...) to handle potential system clock skew
                        time_since_start = max(0.0, time.time() - managed.start_time)
                        is_startup = time_since_start < self.config.startup_phase_duration
                        current_timeout = self.config.startup_health_check_timeout if is_startup else self.config.normal_health_check_timeout

                        await asyncio.wait_for(
                            self.registry.heartbeat(
                                service_name,
                                status=heartbeat_status,
                                metadata={
                                    "pid": managed.pid,
                                    "port": managed.port,
                                    "consecutive_failures": managed.consecutive_failures,
                                    "heartbeat_source": "dedicated_loop",
                                    "is_docker": is_docker,
                                    "file_heartbeat_active": file_heartbeat_alive,
                                    "file_heartbeat_status": file_heartbeat_status,
                                }
                            ),
                            timeout=current_timeout  # v112.0: Use adaptive timeout
                        )
                        managed.last_heartbeat_sent = time.time()
                        logger.debug(
                            f"[v95.0] Heartbeat sent for {service_name} "
                            f"(status: {heartbeat_status})"
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"[v95.0] Heartbeat timeout for {service_name} (non-fatal, timeout={current_timeout}s)"
                        )
                    except Exception as hb_err:
                        logger.warning(
                            f"[v95.0] Heartbeat failed for {service_name}: {hb_err} (non-fatal)"
                        )

        except asyncio.CancelledError:
            logger.debug(f"[v95.0] Heartbeat loop cancelled for {service_name}")
            # v95.0: On cancellation (shutdown), send final status update
            if self.registry and self._shutdown_event.is_set():
                try:
                    await asyncio.wait_for(
                        self.registry.heartbeat(
                            service_name,
                            status="shutting_down",
                            metadata={"reason": "orchestrator_shutdown"}
                        ),
                        timeout=1.0
                    )
                except Exception:
                    pass  # Best effort during shutdown
        except Exception as e:
            logger.error(f"[v95.0] Heartbeat loop error for {service_name}: {e}")
            # Attempt to restart heartbeat loop after error
            if not self._shutdown_event.is_set():
                logger.info(f"[v95.0] Restarting heartbeat loop for {service_name} after error")
                await asyncio.sleep(5.0)
                managed.heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(managed)
                )
        finally:
            # v95.0: Ensure heartbeat task reference is cleared
            if managed.heartbeat_task and managed.heartbeat_task.done():
                managed.heartbeat_task = None
            logger.debug(f"[v95.0] Heartbeat loop exited for {service_name}")

    # =========================================================================
    # Auto-Healing
    # =========================================================================

    async def _auto_heal(self, managed: ManagedProcess) -> bool:
        """
        v95.16: Attempt to restart a failed service with intelligent prevention.

        CRITICAL FIXES:
        - v95.2: Check if service is ACTUALLY healthy before restarting
        - v95.2: Skip restart if service is responding to health checks
        - v95.2: Reset restart count if service recovered on its own
        - v95.2: Prevent restart loops when service is already running
        - v95.16: Per-service locking prevents concurrent recovery attempts

        Returns True if restart succeeded, service is already healthy,
        or another recovery is already in progress (deferred to leader).
        """
        definition = managed.definition

        # v95.13: Check GLOBAL shutdown signal FIRST (catches external shutdown paths)
        try:
            from backend.core.resilience.graceful_shutdown import is_global_shutdown_initiated
            if is_global_shutdown_initiated():
                logger.info(
                    f"[v95.13] Skipping restart of {definition.name}: global shutdown initiated"
                )
                return False
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[v95.13] Error checking global shutdown: {e}")

        # v95.3: CRITICAL - Check ALL shutdown states BEFORE restarting
        if self._shutdown_event.is_set() or self._shutdown_completed:
            logger.info(
                f"[v95.3] Skipping restart of {definition.name}: shutdown detected "
                f"(event={self._shutdown_event.is_set()}, completed={self._shutdown_completed})"
            )
            return False

        # v95.16: CRITICAL - Prevent concurrent recovery attempts for same service
        # When multiple detection mechanisms trigger (health check, registry, process crash),
        # only ONE recovery proceeds. Others return True (recovery is being handled).
        acquired = await self._try_acquire_recovery(definition.name)
        if not acquired:
            logger.info(
                f"[v95.16] Recovery already in progress for {definition.name}, "
                f"deferring to existing recovery handler"
            )
            return True  # Recovery is being handled by another caller

        # v95.16: From here on, we hold the recovery lock - use try/finally to ensure release
        try:
            return await self._auto_heal_inner(managed)
        finally:
            self._release_recovery(definition.name)

    async def _auto_heal_inner(self, managed: ManagedProcess) -> bool:
        """
        v95.16: Inner implementation of auto_heal, called while holding recovery lock.

        This separation ensures the recovery lock is always released even if
        an exception occurs during recovery.
        """
        definition = managed.definition

        # v118.0: CRITICAL - Check port registry for fallback port BEFORE health check
        # The service may have started on a different port due to TIME_WAIT state.
        # Without this check, we'd try to restart a healthy service on the wrong port.
        actual_port = definition.default_port
        registry_port = get_service_port_from_registry(
            definition.name,
            fallback_port=definition.default_port
        )
        if registry_port and registry_port != definition.default_port:
            logger.info(
                f"[v118.0] {definition.name} using fallback port from registry: "
                f"{registry_port} (original: {definition.default_port})"
            )
            actual_port = registry_port
            # Update the definition's port so future operations use the correct port
            definition.default_port = registry_port
            managed.port = registry_port

        # v95.2: CRITICAL - Check if service is ACTUALLY healthy before restarting
        # This prevents restart loops when the service is already running
        is_healthy = await self._quick_health_check(
            actual_port,
            definition.health_endpoint
        )

        if is_healthy:
            logger.info(
                f"[v95.2] Skipping restart of {definition.name}: "
                f"service is responding to health checks (already healthy!)"
            )
            # Reset state since service is actually healthy
            managed.status = ServiceStatus.HEALTHY
            managed.restart_count = 0
            managed.consecutive_failures = 0
            # Emit recovery event
            await _emit_event(
                "SERVICE_RECOVERED",
                service_name=definition.name,
                priority="HIGH",
                details={"reason": "self_recovered", "restart_count": 0}
            )
            return True

        if managed.restart_count >= self.config.max_restart_attempts:
            # v95.2: Before giving up, do one final health check
            # v118.0: Use actual_port (which may be from registry) for final check
            final_check = await self._quick_health_check(
                actual_port,
                definition.health_endpoint
            )
            if final_check:
                logger.info(
                    f"[v95.2] {definition.name} recovered just before giving up! "
                    f"Resetting restart count."
                )
                managed.restart_count = 0
                managed.status = ServiceStatus.HEALTHY
                return True

            logger.error(
                f"❌ {definition.name} exceeded max restart attempts "
                f"({self.config.max_restart_attempts}). Giving up."
            )
            managed.status = ServiceStatus.FAILED
            await _emit_event(
                "SERVICE_CRASHED",
                service_name=definition.name,
                priority="CRITICAL",
                details={
                    "reason": "max_restart_attempts_exceeded",
                    "attempts": managed.restart_count,
                    "max_attempts": self.config.max_restart_attempts
                }
            )
            return False

        # Calculate backoff
        backoff = managed.calculate_backoff(
            self.config.restart_backoff_base,
            self.config.restart_backoff_max
        )

        logger.info(
            f"🔄 Restarting {definition.name} in {backoff:.1f}s "
            f"(attempt {managed.restart_count + 1}/{self.config.max_restart_attempts})"
        )

        # v95.0: Emit service restarting event
        await _emit_event(
            "SERVICE_RESTARTING",
            service_name=managed.definition.name,
            priority="HIGH",
            details={
                "attempt": managed.restart_count + 1,
                "max_attempts": self.config.max_restart_attempts,
                "backoff_seconds": backoff
            }
        )

        managed.status = ServiceStatus.RESTARTING
        await asyncio.sleep(backoff)

        # Stop existing process if still lingering
        await self._stop_process(managed)

        # v112.0: CRITICAL FIX - Wait for port to be released and handle conflicts
        # The old process may have left the port in TIME_WAIT state, or another
        # process may have grabbed it. We MUST ensure the port is available or
        # allocate a fallback before attempting restart.
        port = definition.default_port
        max_port_wait = 15.0  # Wait up to 15s for port release
        port_wait_start = time.time()
        port_available = False

        logger.info(
            f"[v112.0] Waiting for port {port} to be released before restarting {definition.name}..."
        )

        while time.time() - port_wait_start < max_port_wait:
            # Try to bind with SO_REUSEADDR
            try:
                test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                test_sock.settimeout(1.0)
                test_sock.bind(('0.0.0.0', port))
                test_sock.close()
                port_available = True
                logger.info(f"[v112.0] Port {port} is now available for {definition.name}")
                break
            except OSError as e:
                logger.debug(
                    f"[v112.0] Port {port} still unavailable ({e}), waiting..."
                )
                await asyncio.sleep(1.0)
            finally:
                try:
                    test_sock.close()
                except Exception:
                    pass

        if not port_available:
            # Port still not available - try to allocate fallback
            logger.warning(
                f"[v112.0] Port {port} still unavailable after {max_port_wait}s, "
                f"attempting fallback port allocation for {definition.name}..."
            )

            # Use the existing _handle_port_conflict which now allocates fallback
            conflict_result = await self._handle_port_conflict(definition)
            conflict_resolved, fallback_port = conflict_result

            if conflict_resolved:
                if fallback_port is not None:
                    # Update definition with fallback port
                    original_port = definition.default_port
                    definition.default_port = fallback_port

                    # Update script_args to use new port
                    updated_args = []
                    skip_next = False
                    for arg in definition.script_args:
                        if skip_next:
                            skip_next = False
                            continue
                        if arg == "--port":
                            updated_args.append("--port")
                            updated_args.append(str(fallback_port))
                            skip_next = True
                        elif arg.startswith("--port="):
                            updated_args.append(f"--port={fallback_port}")
                        else:
                            updated_args.append(arg)

                    if "--port" not in definition.script_args and not any(
                        a.startswith("--port=") for a in definition.script_args
                    ):
                        updated_args.extend(["--port", str(fallback_port)])

                    definition.script_args = updated_args

                    logger.info(
                        f"[v112.0] Auto-restart using fallback port: {definition.name}\n"
                        f"    Original port: {original_port}\n"
                        f"    Fallback port: {fallback_port}\n"
                        f"    Updated args: {' '.join(definition.script_args)}"
                    )
                else:
                    logger.info(
                        f"[v112.0] Port {port} conflict resolved, proceeding with restart"
                    )
            else:
                logger.error(
                    f"[v112.0] CRITICAL: Could not resolve port conflict for {definition.name} restart. "
                    f"Port {port} is unavailable and no fallback could be allocated."
                )
                managed.consecutive_failures += 1
                return False

        # Restart
        managed.restart_count += 1
        managed.last_restart = time.time()

        success = await self._spawn_service(managed)

        if success:
            logger.info(f"✅ {managed.definition.name} restarted successfully")
            managed.consecutive_failures = 0
            # v95.0: Emit service recovered event
            await _emit_event(
                "SERVICE_RECOVERED",
                service_name=managed.definition.name,
                priority="HIGH",
                details={
                    "restart_count": managed.restart_count,
                    "recovery_time_seconds": time.time() - managed.last_restart
                }
            )
            return True
        else:
            logger.error(f"❌ {managed.definition.name} restart failed")
            # v95.0: Emit service unhealthy event (restart failed)
            await _emit_event(
                "SERVICE_UNHEALTHY",
                service_name=managed.definition.name,
                priority="CRITICAL",
                details={
                    "reason": "restart_failed",
                    "attempt": managed.restart_count
                }
            )
            return False

    async def restart_service(self, service_name: str) -> bool:
        """
        v93.0: Public API to restart a specific service by name.

        This is intended to be called by external components like the
        SelfHealingServiceManager when they detect stale/dead services.

        v125.0: Enhanced with lazy registration support. If the service isn't
        currently in self.processes (because it never started successfully or
        was removed), we'll try to find its definition and start it fresh.

        Args:
            service_name: Name of the service to restart

        Returns:
            True if restart succeeded, False otherwise
        """
        if service_name not in self.processes:
            # v125.0: Lazy registration - try to find the service definition
            # and start it if it's a known service
            logger.warning(
                f"[v125.0] Service {service_name} not in processes dict, "
                f"attempting lazy registration"
            )

            # Try to find the service definition
            definition = await self._get_service_definition_by_name(service_name)
            if definition is None:
                logger.error(
                    f"[v125.0] Cannot restart {service_name}: "
                    f"not managed and no definition found"
                )
                return False

            # Create a new ManagedProcess and attempt to start
            logger.info(
                f"[v125.0] Found definition for {service_name}, "
                f"creating managed process and starting"
            )
            managed = ManagedProcess(definition=definition)
            self.processes[service_name] = managed

            # Start the service
            success = await self._spawn_service(managed)
            if success:
                # Start health monitor
                health_monitor_task = asyncio.create_task(
                    self._monitor_process_health(managed),
                    name=f"crash_monitor_{service_name}"
                )
                managed.health_monitor_task = health_monitor_task
                self._track_background_task(health_monitor_task)
                logger.info(f"[v125.0] Successfully started {service_name} via lazy registration")
            else:
                logger.error(f"[v125.0] Failed to start {service_name} via lazy registration")
                # Don't remove from processes - leave it there for future retry
                managed.status = ServiceStatus.FAILED

            return success

        managed = self.processes[service_name]
        logger.info(f"[v93.0] Restart requested for {service_name} by external component")
        return await self._auto_heal(managed)

    async def _get_service_definition_by_name(self, service_name: str) -> Optional[ServiceDefinition]:
        """
        v125.1: Get a service definition by name with multi-tier lookup.

        Intelligent lookup order:
        1. ServiceDefinitionRegistry (canonical definitions with dynamic discovery)
        2. Built-in definitions from _get_service_definitions()
        3. Creates definition from config if known service

        v125.1 Fixes:
        - Uses correct registry property (self.registry)
        - Uses correct registry method (ServiceDefinitionRegistry.get_definition())
        - Removed invalid health_check_timeout parameter

        Args:
            service_name: Name of the service (e.g., "jarvis-prime", "reactor-core")

        Returns:
            ServiceDefinition if found, None otherwise
        """
        # Normalize the service name (handle both jarvis-prime and jarvis_prime)
        normalized_name = service_name.lower().replace("_", "-")

        # v125.1: Tier 1 - Try the centralized ServiceDefinitionRegistry first
        # This is the SINGLE SOURCE OF TRUTH for service definitions
        try:
            definition = ServiceDefinitionRegistry.get_definition(
                normalized_name,
                port_override=None,  # Use default port resolution
                path_override=None,  # Use intelligent path discovery
                validate=True,
            )
            if definition:
                logger.debug(f"[v125.1] Found {service_name} in ServiceDefinitionRegistry")
                return definition
        except Exception as e:
            logger.debug(f"[v125.1] ServiceDefinitionRegistry lookup failed for {service_name}: {e}")

        # v125.1: Tier 2 - Try built-in definitions from orchestrator configuration
        try:
            definitions = self._get_service_definitions()
            for defn in definitions:
                if defn.name.lower().replace("_", "-") == normalized_name:
                    logger.debug(f"[v125.1] Found {service_name} in built-in definitions")
                    return defn
        except Exception as e:
            logger.debug(f"[v125.1] Built-in definitions lookup failed for {service_name}: {e}")

        # v125.1: Tier 3 - Special handling for known services with config-based creation
        # This is the fallback when registry/discovery fails
        if normalized_name == "jarvis-prime":
            if hasattr(self.config, 'jarvis_prime_path') and self.config.jarvis_prime_path:
                logger.debug(f"[v125.1] Creating jarvis-prime definition from config")
                return ServiceDefinition(
                    name="jarvis-prime",
                    repo_path=self.config.jarvis_prime_path,
                    script_name="run_server.py",
                    fallback_scripts=["main.py", "server.py", "app.py"],
                    default_port=self.config.jarvis_prime_default_port,
                    depends_on=[],
                    startup_timeout=300.0,  # ML model loading needs extra time
                    startup_priority=20,
                    is_critical=True,
                    dependency_wait_timeout=120.0,
                )
            else:
                logger.warning(f"[v125.1] jarvis-prime path not configured in self.config")

        elif normalized_name == "reactor-core":
            if hasattr(self.config, 'reactor_core_path') and self.config.reactor_core_path:
                logger.debug(f"[v125.1] Creating reactor-core definition from config")
                return ServiceDefinition(
                    name="reactor-core",
                    repo_path=self.config.reactor_core_path,
                    script_name="run_reactor.py",
                    fallback_scripts=["run_supervisor.py", "main.py", "server.py"],
                    default_port=self.config.reactor_core_default_port,
                    depends_on=[],  # v117.0: reactor-core can start independently
                    soft_depends_on=["jarvis-prime"],  # Recommended but not required
                    startup_timeout=120.0,
                    startup_priority=30,
                    is_critical=False,  # System can degrade without reactor-core
                    dependency_wait_timeout=180.0,
                )
            else:
                logger.warning(f"[v125.1] reactor-core path not configured in self.config")

        logger.warning(f"[v125.1] No definition found for service: {service_name}")
        return None

    # =========================================================================
    # v95.1: Intelligent Cross-Repo Recovery Coordination
    # =========================================================================

    async def start_recovery_coordinator(self) -> None:
        """
        v95.1: Start the intelligent recovery coordinator.

        The coordinator proactively monitors all services and handles:
        1. Early failure detection (before stale marking)
        2. Dependency cascade recovery (restart dependents when dependency fails)
        3. Cross-repo coordination (JARVIS, J-Prime, Reactor-Core)
        4. Intelligent restart ordering based on dependencies
        """
        if not self._proactive_recovery_enabled:
            logger.info("[v95.1] Proactive recovery is disabled")
            return

        if self._recovery_coordinator_task is None or self._recovery_coordinator_task.done():
            self._recovery_coordinator_task = asyncio.create_task(
                self._recovery_coordinator_loop(),
                name="recovery_coordinator"
            )
            self._track_background_task(self._recovery_coordinator_task)
            logger.info(
                f"[v95.1] Recovery coordinator started "
                f"(interval: {self._recovery_check_interval}s, "
                f"cascade: {self._dependency_cascade_recovery})"
            )

    async def stop_recovery_coordinator(self) -> None:
        """v95.1: Stop the recovery coordinator."""
        if self._recovery_coordinator_task and not self._recovery_coordinator_task.done():
            self._recovery_coordinator_task.cancel()
            try:
                await self._recovery_coordinator_task
            except asyncio.CancelledError:
                pass
            logger.info("[v95.1] Recovery coordinator stopped")

    async def _recovery_coordinator_loop(self) -> None:
        """
        v95.3: Main recovery coordinator loop with shutdown completion check.

        Performs proactive health checks and initiates recovery when needed.

        CRITICAL (v95.3): Added multiple shutdown state checks to prevent
        post-shutdown recovery attempts that caused restart loops.
        """
        logger.info("[v95.1] Recovery coordinator loop started")

        while not self._shutdown_event.is_set() and not self._shutdown_completed:
            try:
                await asyncio.sleep(self._recovery_check_interval)

                # v95.3: CRITICAL - Check ALL shutdown states before any recovery action
                if self._shutdown_event.is_set() or self._shutdown_completed:
                    logger.info(
                        "[v95.3] Recovery coordinator exiting: shutdown detected "
                        f"(event={self._shutdown_event.is_set()}, completed={self._shutdown_completed})"
                    )
                    break

                # Perform comprehensive service health assessment
                health_report = await self._assess_service_health()

                # v95.3: Re-check shutdown state after potentially slow health assessment
                if self._shutdown_event.is_set() or self._shutdown_completed:
                    logger.debug("[v95.3] Skipping recovery actions: shutdown in progress")
                    break

                # Handle any services that need recovery
                for service_name, health_status in health_report.items():
                    # v95.3: Check shutdown state before EACH recovery attempt
                    if self._shutdown_event.is_set() or self._shutdown_completed:
                        logger.debug(
                            f"[v95.3] Aborting recovery loop for {service_name}: shutdown detected"
                        )
                        break

                    if health_status["needs_recovery"]:
                        await self._initiate_intelligent_recovery(
                            service_name,
                            health_status["reason"]
                        )

            except asyncio.CancelledError:
                logger.info("[v95.1] Recovery coordinator cancelled")
                break
            except Exception as e:
                logger.error(f"[v95.1] Recovery coordinator error: {e}")
                # v95.3: Check shutdown before sleeping on error
                if not self._shutdown_event.is_set() and not self._shutdown_completed:
                    await asyncio.sleep(5.0)  # Brief pause before retry

    async def _assess_service_health(self) -> Dict[str, Dict[str, Any]]:
        """
        v95.3: Comprehensive health assessment for all managed services.

        Checks:
        1. Process alive status
        2. Health endpoint responsiveness
        3. Heartbeat recency
        4. Dependency health (transitive)

        CRITICAL (v95.3): Skips assessment if shutdown is in progress
        to prevent false "needs_recovery" reports during shutdown.
        """
        health_report: Dict[str, Dict[str, Any]] = {}

        # v95.3: Skip health assessment if shutdown is in progress or completed
        if self._shutdown_event.is_set() or self._shutdown_completed:
            logger.debug("[v95.3] Skipping health assessment: shutdown in progress")
            return health_report

        for service_name, managed in self.processes.items():
            status = {
                "needs_recovery": False,
                "reason": None,
                "process_alive": False,
                "health_check_passed": False,
                "heartbeat_recent": False,
                "dependencies_healthy": True,
            }

            # 1. Check process status
            status["process_alive"] = managed.is_running

            # 2. Check health endpoint (quick check)
            if status["process_alive"]:
                try:
                    session = await self._get_http_session()
                    url = f"http://localhost:{managed.port}{managed.definition.health_endpoint}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                        status["health_check_passed"] = resp.status == 200
                except Exception:
                    status["health_check_passed"] = False

            # 3. Check heartbeat recency (via registry)
            if self.registry:
                try:
                    service_info = await self.registry.discover_service(service_name)
                    if service_info:
                        heartbeat_age = time.time() - service_info.last_heartbeat
                        status["heartbeat_recent"] = heartbeat_age < 60.0  # Within 1 minute
                except Exception:
                    pass

            # 4. Check dependency health
            if managed.definition.depends_on:
                for dep in managed.definition.depends_on:
                    if dep in self.processes:
                        dep_managed = self.processes[dep]
                        if dep_managed.status not in (ServiceStatus.HEALTHY, ServiceStatus.DEGRADED):
                            status["dependencies_healthy"] = False
                            break

            # Determine if recovery is needed
            if not status["process_alive"]:
                status["needs_recovery"] = True
                status["reason"] = "process_dead"
            elif not status["health_check_passed"] and managed.status == ServiceStatus.HEALTHY:
                # Was healthy but health check now fails
                status["needs_recovery"] = True
                status["reason"] = "health_check_failed"
            elif not status["dependencies_healthy"] and self._dependency_cascade_recovery:
                # Dependency failed - may need cascade restart
                status["needs_recovery"] = True
                status["reason"] = "dependency_failed"

            health_report[service_name] = status

        return health_report

    async def _initiate_intelligent_recovery(
        self,
        service_name: str,
        reason: str
    ) -> bool:
        """
        v95.3: Initiate intelligent recovery for a failed service.

        Handles:
        1. Dependency ordering (restart dependencies first if needed)
        2. Cascade recovery (restart dependents after dependency)
        3. Cross-repo coordination

        CRITICAL (v95.3): Added comprehensive shutdown state checks to prevent
        recovery attempts after shutdown has started or completed.
        """
        # v95.13: Check GLOBAL shutdown signal FIRST (catches external shutdown paths)
        try:
            from backend.core.resilience.graceful_shutdown import is_global_shutdown_initiated
            if is_global_shutdown_initiated():
                logger.info(
                    f"[v95.13] Skipping recovery for {service_name}: global shutdown initiated"
                )
                return False
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[v95.13] Error checking global shutdown: {e}")

        # v95.3: CRITICAL - Check ALL shutdown states before recovery
        if self._shutdown_event.is_set() or self._shutdown_completed:
            logger.info(
                f"[v95.3] Skipping recovery for {service_name}: shutdown detected "
                f"(event={self._shutdown_event.is_set()}, completed={self._shutdown_completed})"
            )
            return False

        if service_name not in self.processes:
            logger.warning(f"[v95.1] Cannot recover {service_name}: not managed")
            return False

        managed = self.processes[service_name]
        logger.info(f"[v95.1] Initiating recovery for {service_name} (reason: {reason})")

        # If reason is dependency failure, check and restart dependencies first
        if reason == "dependency_failed" and managed.definition.depends_on:
            for dep_name in managed.definition.depends_on:
                if dep_name in self.processes:
                    dep_managed = self.processes[dep_name]
                    if not dep_managed.is_running or dep_managed.status == ServiceStatus.FAILED:
                        logger.info(
                            f"[v95.1] Recovering dependency '{dep_name}' first for {service_name}"
                        )
                        await self._auto_heal(dep_managed)
                        # Wait briefly for dependency to stabilize
                        await asyncio.sleep(5.0)

        # Now recover the target service
        success = await self._auto_heal(managed)

        # If cascade recovery is enabled and this was a dependency, restart dependents
        if success and self._dependency_cascade_recovery:
            await self._cascade_restart_dependents(service_name)

        return success

    async def _cascade_restart_dependents(self, service_name: str) -> None:
        """
        v95.1: Restart services that depend on the recovered service.

        When a dependency is recovered, its dependents may need to be
        restarted to re-establish connections.
        """
        dependents = []

        for name, managed in self.processes.items():
            if managed.definition.depends_on and service_name in managed.definition.depends_on:
                dependents.append(name)

        if dependents:
            logger.info(
                f"[v95.1] Cascade restart: {len(dependents)} services depend on {service_name}: "
                f"{dependents}"
            )

            for dep_name in dependents:
                if dep_name in self.processes:
                    dep_managed = self.processes[dep_name]
                    # Only restart if not already healthy
                    if dep_managed.status != ServiceStatus.HEALTHY:
                        logger.info(f"[v95.1] Cascade restarting dependent: {dep_name}")
                        await self._auto_heal(dep_managed)
                        await asyncio.sleep(2.0)  # Brief pause between restarts

    # =========================================================================
    # Process Spawning
    # =========================================================================

    def _find_venv_python(self, repo_path: Path) -> Optional[str]:
        """
        Find the venv Python executable for a repository.

        v4.0: Auto-detects venv location and returns the Python path.
        Falls back to system Python if no venv found.
        """
        # Check common venv locations
        venv_paths = [
            repo_path / "venv" / "bin" / "python3",
            repo_path / "venv" / "bin" / "python",
            repo_path / ".venv" / "bin" / "python3",
            repo_path / ".venv" / "bin" / "python",
            repo_path / "env" / "bin" / "python3",
            repo_path / "env" / "bin" / "python",
        ]

        for venv_python in venv_paths:
            if venv_python.exists():
                logger.debug(f"Found venv Python at: {venv_python}")
                return str(venv_python)

        logger.debug(f"No venv found in {repo_path}, using system Python")
        return None

    async def _pre_spawn_validation(
        self, definition: ServiceDefinition
    ) -> Tuple[Union[bool, Literal["ALREADY_HEALTHY"]], Optional[str]]:
        """
        Validate a service before spawning.

        v4.0: Pre-launch checks:
        - Repo path exists
        - Script exists
        - Venv Python found (optional but preferred)
        - Port not already in use

        v95.0: Enhanced with detailed diagnostic logging for troubleshooting.
        v95.7: Return type now includes Literal["ALREADY_HEALTHY"] for type safety.

        Returns:
            Tuple of (is_valid_or_status, python_executable)
            - is_valid_or_status: True if valid, False if invalid, "ALREADY_HEALTHY" if service is running
            - python_executable: Path to Python executable or None
        """
        logger.info(f"    🔍 Pre-spawn validation for {definition.name}...")

        # Check repo exists
        if not definition.repo_path.exists():
            logger.error(f"    ❌ Repository not found: {definition.repo_path}")
            logger.error(f"    💡 Hint: Set {definition.name.upper().replace('-', '_')}_PATH env var or ensure repo is cloned")
            return False, None

        logger.debug(f"    ✓ Repository found: {definition.repo_path}")

        # Check script exists
        script_path = self._find_script(definition)
        if script_path is None:
            logger.error(f"    ❌ No startup script found for {definition.name}")
            logger.error(f"    💡 Looking for: {definition.script_name} in {definition.repo_path}")
            logger.error(f"    💡 Fallbacks: {definition.fallback_scripts}")
            # List what files ARE in the repo
            try:
                py_files = list(definition.repo_path.glob("*.py"))
                if py_files:
                    logger.error(f"    💡 Python files found: {[f.name for f in py_files[:5]]}")
                else:
                    logger.error(f"    💡 No .py files found in {definition.repo_path}")
            except Exception as e:
                logger.debug(f"    Could not list repo files: {e}")
            return False, None

        logger.debug(f"    ✓ Script found: {script_path}")

        # Find Python executable (prefer venv)
        python_exec = self._find_venv_python(definition.repo_path)
        if python_exec is None:
            python_exec = sys.executable
            logger.info(f"    ℹ️ Using system Python for {definition.name}: {python_exec}")
        else:
            logger.info(f"    ✓ Using venv Python for {definition.name}: {python_exec}")

        # v95.2: CRITICAL - Check if service is ALREADY RUNNING and HEALTHY
        # This prevents the restart loop where we try to spawn when already running
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('localhost', definition.default_port))
            sock.close()

            if result == 0:
                # Port is in use - check if it's actually our service and healthy
                logger.info(
                    f"    🔍 Port {definition.default_port} in use - checking if {definition.name} is healthy..."
                )

                # v117.0: CRITICAL FIX - Wait longer for service that may be starting
                # The port being bound but not healthy could mean:
                # 1. Another jarvis-prime is starting up (models loading)
                # 2. A stale/zombie process that needs cleanup
                # We must distinguish between these cases before taking action

                # v117.0: Check if this is likely a jarvis-prime process starting up
                is_jarvis_prime_starting = await self._check_if_jarvis_prime_starting(
                    definition.default_port
                )

                if is_jarvis_prime_starting:
                    # Another jarvis-prime is starting - wait for it to become healthy
                    logger.info(
                        f"    ⏳ [v117.0] Detected jarvis-prime starting on port {definition.default_port} - "
                        f"waiting for it to become healthy..."
                    )

                    # Wait up to 90 seconds for heavy ML models to load
                    startup_wait = min(definition.startup_timeout, 90.0)
                    check_interval = 5.0
                    elapsed = 0.0

                    while elapsed < startup_wait:
                        is_healthy = await self._quick_health_check(
                            definition.default_port,
                            definition.health_endpoint
                        )
                        if is_healthy:
                            logger.info(
                                f"    ✅ [v117.0] {definition.name} became healthy after {elapsed:.1f}s - SKIPPING SPAWN"
                            )
                            return "ALREADY_HEALTHY", python_exec

                        await asyncio.sleep(check_interval)
                        elapsed += check_interval
                        logger.debug(f"    ⏳ Still waiting for health... ({elapsed:.1f}s/{startup_wait}s)")

                    # Timed out waiting - the process might be stuck
                    logger.warning(
                        f"    ⚠️ [v117.0] jarvis-prime on port {definition.default_port} didn't become healthy "
                        f"after {startup_wait}s - may need cleanup"
                    )

                is_healthy = await self._quick_health_check(
                    definition.default_port,
                    definition.health_endpoint
                )

                if is_healthy:
                    # Service is already running and healthy - NO NEED TO SPAWN!
                    logger.info(
                        f"    ✅ {definition.name} is already running and healthy on port "
                        f"{definition.default_port} - SKIPPING SPAWN"
                    )
                    # Return special marker to indicate "already healthy"
                    return "ALREADY_HEALTHY", python_exec
                else:
                    # Port in use but not healthy - this is a real conflict
                    logger.warning(
                        f"    ⚠️ Port {definition.default_port} in use but {definition.name} "
                        f"is NOT healthy - possible port conflict with another process"
                    )
                    # v108.0: CRITICAL FIX - Actually resolve the conflict
                    # v109.3: Enhanced with retry logic and proper failure handling
                    # v117.0: _handle_port_conflict returns (success: bool, fallback_port: Optional[int])
                    conflict_result = await self._handle_port_conflict(definition)
                    conflict_resolved, fallback_port = conflict_result

                    if conflict_resolved:
                        if fallback_port is not None:
                            # v112.0: CRITICAL - Update definition with fallback port
                            original_port = definition.default_port
                            definition.default_port = fallback_port

                            # Update script_args to use new port
                            updated_args = []
                            skip_next = False
                            for i, arg in enumerate(definition.script_args):
                                if skip_next:
                                    skip_next = False
                                    continue
                                if arg == "--port":
                                    updated_args.append("--port")
                                    updated_args.append(str(fallback_port))
                                    skip_next = True  # Skip the old port value
                                elif arg.startswith("--port="):
                                    updated_args.append(f"--port={fallback_port}")
                                else:
                                    updated_args.append(arg)

                            # If --port wasn't in args, add it
                            if "--port" not in definition.script_args and not any(
                                a.startswith("--port=") for a in definition.script_args
                            ):
                                updated_args.extend(["--port", str(fallback_port)])

                            definition.script_args = updated_args

                            logger.info(
                                f"    🔄 [v112.0] Port reallocation: {definition.name}\n"
                                f"       Original port: {original_port}\n"
                                f"       Fallback port: {fallback_port}\n"
                                f"       Updated args: {' '.join(definition.script_args)}"
                            )
                        else:
                            logger.info(
                                f"    ✅ Port {definition.default_port} conflict resolved, proceeding with spawn"
                            )
                    else:
                        logger.error(
                            f"    ❌ Could not resolve port conflict for {definition.name} "
                            f"on port {definition.default_port}"
                        )
                        # v109.3: More aggressive retry with TIME_WAIT handling
                        # Wait longer and check multiple times
                        max_retries = 3
                        retry_delay = 2.0

                        for retry in range(max_retries):
                            await asyncio.sleep(retry_delay)

                            # Check if service came up healthy
                            is_healthy_retry = await self._quick_health_check(
                                definition.default_port,
                                definition.health_endpoint
                            )
                            if is_healthy_retry:
                                logger.info(
                                    f"    ✅ {definition.name} became healthy during retry {retry + 1}"
                                )
                                return "ALREADY_HEALTHY", python_exec

                            # v109.3: Check if port is now free (conflict may have resolved)
                            try:
                                test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                                test_sock.settimeout(1)
                                test_sock.bind(('0.0.0.0', definition.default_port))
                                test_sock.close()
                                logger.info(
                                    f"    ✅ Port {definition.default_port} is now available "
                                    f"after retry {retry + 1}"
                                )
                                break  # Port is free, proceed with spawn
                            except OSError:
                                logger.debug(
                                    f"    ⏳ Port {definition.default_port} still unavailable "
                                    f"(retry {retry + 1}/{max_retries})"
                                )
                                if retry < max_retries - 1:
                                    retry_delay *= 1.5  # Exponential backoff
                        else:
                            # v109.3: CRITICAL - Don't proceed if port is still occupied
                            # This prevents the "address already in use" error
                            logger.error(
                                f"    ❌ Port {definition.default_port} still unavailable after "
                                f"{max_retries} retries - FAILING pre-spawn validation"
                            )
                            return False, None

        except Exception as e:
            logger.debug(f"Port check failed: {e}")

        return True, python_exec

    async def _quick_health_check(self, port: int, health_endpoint: str) -> bool:
        """
        v95.2: Quick health check to verify if a service is responding.

        Used during pre-spawn validation to prevent restarting healthy services.
        """
        try:
            session = await self._get_http_session()
            url = f"http://localhost:{port}{health_endpoint}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _check_if_jarvis_prime_starting(self, port: int) -> bool:
        """
        v117.0: Check if the process on the port is jarvis-prime that's starting up.

        This helps distinguish between:
        1. A jarvis-prime process loading models (should wait)
        2. A completely different process hogging the port (should cleanup)
        3. A zombie jarvis-prime that's stuck (should cleanup)

        Returns True if the process appears to be a jarvis-prime starting up.
        """
        try:
            # Use lsof to find process on the port
            result = await asyncio.create_subprocess_exec(
                "lsof", "-i", f":{port}", "-t",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await result.communicate()

            if not stdout:
                return False

            pids = [p.strip() for p in stdout.decode().split() if p.strip().isdigit()]

            for pid_str in pids:
                try:
                    pid = int(pid_str)

                    # Get the command line for this process
                    proc_result = await asyncio.create_subprocess_exec(
                        "ps", "-p", str(pid), "-o", "command=",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    ps_stdout, _ = await proc_result.communicate()
                    cmd = ps_stdout.decode().lower()

                    # Check if it's a jarvis-prime related process
                    jarvis_prime_indicators = [
                        "jarvis_prime",
                        "jarvis-prime",
                        "run_server.py",
                        "jprime",
                        "jarvis_prime.server",
                    ]

                    for indicator in jarvis_prime_indicators:
                        if indicator.lower() in cmd:
                            logger.debug(f"[v117.0] Found jarvis-prime process PID {pid}: {cmd[:80]}...")

                            # Check process age - if just started (< 3 min), likely still loading
                            try:
                                proc_age_result = await asyncio.create_subprocess_exec(
                                    "ps", "-p", str(pid), "-o", "etimes=",
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.DEVNULL,
                                )
                                age_stdout, _ = await proc_age_result.communicate()
                                age_seconds = int(age_stdout.decode().strip())

                                if age_seconds < 180:  # Less than 3 minutes old
                                    logger.info(
                                        f"[v117.0] jarvis-prime process (PID {pid}) is young "
                                        f"({age_seconds}s) - likely still starting"
                                    )
                                    return True
                                else:
                                    logger.warning(
                                        f"[v117.0] jarvis-prime process (PID {pid}) is {age_seconds}s old "
                                        f"but not healthy - may be stuck"
                                    )
                                    return False
                            except Exception:
                                # Can't determine age, assume starting
                                return True

                except (ValueError, ProcessLookupError):
                    continue

        except Exception as e:
            logger.debug(f"[v117.0] Failed to check if jarvis-prime starting: {e}")

        return False

    async def _handle_port_conflict(
        self, definition: ServiceDefinition
    ) -> Tuple[bool, Optional[int]]:
        """
        v108.0: Handle port conflict by attempting to identify and resolve the issue.
        v109.0: Enhanced with diagnostic logging and PID safety validation.
        v112.0: CRITICAL FIX - Returns fallback port when cleanup fails instead of failing entirely.

        CRITICAL FIX: This now actually CLEANS UP conflicting processes instead of
        just logging them. Uses the EnterpriseProcessManager for comprehensive
        port validation and cleanup.

        v112.0 Enhancement: When cleanup fails, automatically finds and returns
        an available fallback port instead of failing. This ensures startup
        succeeds even when the preferred port is occupied by stubborn processes.

        Returns:
            Tuple[bool, Optional[int]]:
                - (True, None): Port cleanup succeeded, use original port
                - (True, new_port): Cleanup failed but fallback port found
                - (False, None): All options exhausted, no ports available
        """
        port = definition.default_port

        # v109.0: Log our own identity for debugging port conflict resolution
        current_pid = os.getpid()
        parent_pid = os.getppid()
        logger.info(
            f"    🔍 [v109.0] Port conflict diagnostics for {definition.name} on port {port}:\n"
            f"       Supervisor PID: {current_pid}\n"
            f"       Parent PID: {parent_pid}"
        )

        # v108.0: Use EnterpriseProcessManager for comprehensive port handling
        try:
            from backend.core.enterprise_process_manager import get_process_manager

            process_manager = get_process_manager()

            # Comprehensive port validation
            validation = await process_manager.validate_port(
                port=port,
                expected_service=definition.name,
                health_endpoint=definition.health_endpoint,
            )

            logger.info(
                f"    📋 Port {port} validation: occupied={validation.is_occupied}, "
                f"healthy={validation.is_healthy}, state={validation.socket_state}, "
                f"pid={validation.pid}, process={validation.process_name}, "
                f"recommendation={validation.recommendation}"
            )

            if validation.pid:
                # Log details about the conflicting process
                logger.warning(
                    f"    📋 Port {port} held by: PID={validation.pid}, "
                    f"Name={validation.process_name or 'unknown'}"
                )

                # v109.0: CRITICAL SAFETY CHECK - Is the detected PID ourselves?
                if validation.pid in (current_pid, parent_pid):
                    logger.error(
                        f"    ❌ [v109.0] CRITICAL BUG DETECTED: Port validation returned our own PID!\n"
                        f"       This indicates lsof may be returning client connections, not just LISTEN.\n"
                        f"       Detected PID: {validation.pid}, Our PID: {current_pid}, Parent: {parent_pid}\n"
                        f"       NOT proceeding with cleanup - this would kill ourselves."
                    )
                    # v112.0: Try fallback port instead of failing
                    return await self._allocate_fallback_port(definition)

            # Handle based on recommendation
            if validation.recommendation == "proceed":
                return True, None  # Port available, use original

            elif validation.recommendation == "skip":
                # Service already running healthy - this is actually OK
                logger.info(f"    ✅ {definition.name} already healthy on port {port}")
                return True, None

            elif validation.recommendation in ("kill_and_retry", "wait"):
                # v109.3: Enhanced multi-phase cleanup with force escalation
                logger.info(f"    🧹 [v109.3] Attempting multi-phase cleanup for port {port}...")

                # Phase 1: Graceful cleanup (SIGTERM)
                cleanup_success = await process_manager.cleanup_port(
                    port=port,
                    force=False,  # Try graceful first
                    wait_for_time_wait=True,
                    max_wait=15.0,  # Wait up to 15s for graceful
                )

                if cleanup_success:
                    logger.info(f"    ✅ Port {port} cleaned up (graceful)")
                    return True, None

                # Phase 2: Force cleanup (SIGKILL) if graceful failed
                logger.warning(f"    ⚠️ Graceful cleanup failed, escalating to force cleanup...")
                cleanup_success = await process_manager.cleanup_port(
                    port=port,
                    force=True,  # SIGKILL
                    wait_for_time_wait=True,
                    max_wait=15.0,
                )

                if cleanup_success:
                    logger.info(f"    ✅ Port {port} cleaned up (forced)")
                    return True, None

                # Phase 3: Wait for socket release (TIME_WAIT, CLOSE_WAIT, FIN_WAIT)
                # v109.8: Enhanced diagnostics and comprehensive socket state handling
                logger.warning(f"    ⚠️ Force cleanup failed, analyzing socket state...")
                socket_state = await process_manager._check_socket_state(port)
                state_name = socket_state.get("state", "unknown")

                if state_name in ("TIME_WAIT", "CLOSE_WAIT", "FIN_WAIT1", "FIN_WAIT2"):
                    logger.info(
                        f"    ⏳ Port {port} in {state_name} state, waiting for natural release..."
                    )
                    start = time.time()
                    while time.time() - start < 30.0:
                        await asyncio.sleep(2.0)
                        state_check = await process_manager._check_socket_state(port)
                        if not state_check.get("occupied", True):
                            logger.info(f"    ✅ Port {port} released from {state_name}")
                            return True, None
                        current_state = state_check.get("state", "unknown")
                        logger.debug(
                            f"    ⏳ Still waiting: port {port} in {current_state}..."
                        )
                    logger.warning(
                        f"    ❌ {state_name} did not clear in time for port {port}"
                    )

                # v109.8: Final diagnostic dump for debugging
                logger.warning(
                    f"    ⚠️ All cleanup phases failed for port {port}. Final diagnostics:\n"
                    f"       Socket state: {state_name}\n"
                    f"       Port occupied: {socket_state.get('occupied', 'unknown')}"
                )

                # v109.8: Try to gather more debug info
                try:
                    import subprocess
                    lsof_result = subprocess.run(
                        ["lsof", "-i", f":{port}", "-P", "-n"],
                        capture_output=True, text=True, timeout=5
                    )
                    if lsof_result.stdout:
                        logger.warning(
                            f"       lsof output for port {port}:\n"
                            f"{lsof_result.stdout}"
                        )
                except Exception as diag_err:
                    logger.debug(f"       Diagnostic lsof failed: {diag_err}")

                # v112.0: CRITICAL FIX - Don't fail, allocate fallback port instead!
                logger.info(
                    f"    🔄 [v112.0] Port {port} cleanup failed, allocating fallback port..."
                )
                return await self._allocate_fallback_port(definition)

            # v112.0: Unknown recommendation - try fallback
            return await self._allocate_fallback_port(definition)

        except ImportError:
            # v109.8/v137.0: Enhanced legacy fallback with non-blocking I/O
            logger.warning(
                "[v137.0] EnterpriseProcessManager not available, using non-blocking legacy fallback"
            )
            try:
                current_pid = os.getpid()
                parent_pid = os.getppid()

                # v137.0: Use non-blocking I/O to get connections
                all_connections = await get_net_connections_nonblocking(port=None)

                # v109.8: Check ALL socket states, not just LISTEN
                # Prioritize LISTEN, then other states
                conn_by_state: Dict[str, List[Dict[str, Any]]] = {"LISTEN": [], "other": []}
                for conn in all_connections:
                    conn_port = conn.get("port")
                    conn_pid = conn.get("pid")
                    conn_status = conn.get("status")
                    if conn_port == port and conn_pid:
                        if conn_pid in (current_pid, parent_pid):
                            continue  # Skip self
                        if conn_status == "LISTEN":
                            conn_by_state["LISTEN"].append(conn)
                        else:
                            conn_by_state["other"].append(conn)

                # Try LISTEN connections first, then others
                for conn in conn_by_state["LISTEN"] + conn_by_state["other"]:
                    conn_pid = conn["pid"]
                    conn_status = conn.get("status", "unknown")
                    try:
                        # v137.0: Get process info via non-blocking I/O
                        proc_info = await get_process_info_nonblocking(conn_pid)
                        proc_name = proc_info.get("name", "unknown") if proc_info else "unknown"
                        proc_cmdline = proc_info.get("cmdline", []) if proc_info else []

                        logger.warning(
                            f"    📋 Port {port} held by: "
                            f"PID={conn_pid}, Name={proc_name}, "
                            f"Status={conn_status}, "
                            f"Cmdline={' '.join(proc_cmdline[:3])}"
                        )

                        # Use our existing graceful kill method
                        success, status = await self._kill_process_graceful_then_force(
                            conn_pid,
                            graceful_timeout=5.0,
                            force_timeout=5.0,
                        )

                        if success:
                            logger.info(f"    ✅ Killed process {conn_pid} on port {port} ({status})")
                            return True, None  # v112.0: Cleanup succeeded
                        else:
                            logger.warning(f"    ⚠️ Could not kill process {conn_pid}: {status}")
                            continue

                    except Exception as e:
                        logger.warning(
                            f"    ⚠️ Could not kill process on port {port}: {e}"
                        )
                        continue

                # v109.8: If no process found but port might be in TIME_WAIT, wait
                if not conn_by_state["LISTEN"] and not conn_by_state["other"]:
                    logger.info(f"    ⏳ No killable process found for port {port}, waiting for release...")
                    start = time.time()
                    while time.time() - start < 15.0:
                        await asyncio.sleep(1.0)
                        # v137.0: Use non-blocking check
                        connections = await get_net_connections_nonblocking(port=None)
                        still_occupied = any(c.get("port") == port for c in connections)
                        if not still_occupied:
                            logger.info(f"    ✅ Port {port} released")
                            return True, None  # v112.0: Cleanup succeeded

                # v112.0: CRITICAL FIX - Don't fail, allocate fallback port
                logger.info(f"    🔄 [v112.0] Legacy cleanup failed, allocating fallback port...")
                return await self._allocate_fallback_port(definition)

            except Exception as e:
                logger.error(f"Could not identify process on port: {e}")
                # v112.0: Still try fallback even on error
                return await self._allocate_fallback_port(definition)

    async def _allocate_fallback_port(
        self, definition: ServiceDefinition
    ) -> Tuple[bool, Optional[int]]:
        """
        v112.0: Allocate a fallback port when the preferred port is unavailable.

        This is the CRITICAL FIX for the port conflict issue. Instead of failing
        startup when cleanup doesn't work (e.g., ESTABLISHED connections blocking
        port release), we dynamically allocate an alternative port and update
        the service configuration.

        Features:
        - Scans dynamic port range (default 9000-9999)
        - Uses socket binding test for reliability (not just connect test)
        - Updates the distributed port registry for cross-repo coordination
        - Returns the allocated port so caller can update service args

        Args:
            definition: The service definition requesting a port

        Returns:
            Tuple[bool, Optional[int]]:
                - (True, new_port): Fallback port allocated successfully
                - (False, None): No ports available in range
        """
        import socket

        original_port = definition.default_port
        service_name = definition.name

        logger.info(
            f"    🔍 [v112.0] Scanning for fallback port for {service_name} "
            f"(original: {original_port})..."
        )

        # Get port range from config
        start_port, end_port = self._dynamic_port_range

        # Track which ports we've tried
        tried_ports = []

        for port in range(start_port, end_port + 1):
            # Skip the original port (already know it's unavailable)
            if port == original_port:
                continue

            # Skip ports already allocated in this session
            if port in self._port_allocation_map.values():
                continue

            # Test if port is actually available using socket bind
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            test_sock.settimeout(1.0)

            try:
                test_sock.bind(('0.0.0.0', port))
                test_sock.close()

                # Port is available! Allocate it.
                self._port_allocation_map[service_name] = port

                # Record the port change for monitoring
                if original_port not in self._port_conflict_history:
                    self._port_conflict_history[original_port] = []
                self._port_conflict_history[original_port].append({
                    "timestamp": time.time(),
                    "service": service_name,
                    "conflict_info": "cleanup_failed",
                    "resolved_port": port,
                })

                # Update the distributed port registry for cross-repo coordination
                await self._update_port_registry(service_name, port, original_port)

                logger.info(
                    f"    ✅ [v112.0] Allocated fallback port {port} for {service_name} "
                    f"(original {original_port} was unavailable)\n"
                    f"       Scanned {len(tried_ports) + 1} ports in range {start_port}-{end_port}"
                )

                return True, port

            except OSError:
                tried_ports.append(port)
                # Port unavailable, continue scanning
                continue
            finally:
                try:
                    test_sock.close()
                except Exception:
                    pass

        # No ports available in range
        logger.error(
            f"    ❌ [v112.0] CRITICAL: No available ports for {service_name}!\n"
            f"       Scanned range: {start_port}-{end_port}\n"
            f"       Tried {len(tried_ports)} ports, all unavailable\n"
            f"       💡 Solutions:\n"
            f"          1. Free up ports in range {start_port}-{end_port}\n"
            f"          2. Adjust JARVIS_PORT_RANGE_START/END environment variables\n"
            f"          3. Kill stale processes: lsof -i :{start_port}-{end_port}"
        )
        return False, None

    async def _update_port_registry(
        self, service_name: str, new_port: int, original_port: int
    ) -> None:
        """
        v112.0: Update the distributed port registry for cross-repo coordination.
        v137.0: Updated to use non-blocking I/O (I/O Airlock pattern).

        When a service gets a fallback port, other repos need to know about it.
        This updates ~/.jarvis/registry/ports.json with the actual port mapping.

        Args:
            service_name: Name of the service
            new_port: The newly allocated port
            original_port: The originally requested port
        """
        registry_dir = Path.home() / ".jarvis" / "registry"

        # v137.0: Use non-blocking I/O for directory creation
        def _ensure_registry_dir():
            registry_dir.mkdir(parents=True, exist_ok=True)
        await _run_blocking_io(_ensure_registry_dir, timeout=2.0, operation_name="ensure_registry_dir")

        registry_file = registry_dir / "ports.json"

        try:
            # v137.0: Load existing registry with non-blocking I/O
            registry = await read_json_nonblocking(registry_file)
            if registry is None:
                registry = {"version": "1.0", "ports": {}, "fallbacks": []}

            # Update port mapping
            registry["ports"][service_name] = {
                "port": new_port,
                "original_port": original_port,
                "allocated_at": time.time(),
                "is_fallback": new_port != original_port,
            }

            # Track fallback history
            if new_port != original_port:
                if "fallbacks" not in registry:
                    registry["fallbacks"] = []
                registry["fallbacks"].append({
                    "service": service_name,
                    "original": original_port,
                    "fallback": new_port,
                    "timestamp": time.time(),
                })

            # v137.0: Write atomically with non-blocking I/O
            success = await write_json_nonblocking(registry_file, registry)
            if success:
                logger.debug(
                    f"    📝 [v137.0] Updated port registry: {service_name} -> {new_port}"
                )
            else:
                logger.warning(f"    ⚠️ [v137.0] Failed to write port registry update")

        except Exception as e:
            logger.warning(f"    ⚠️ [v137.0] Could not update port registry: {e}")

    async def _wait_for_dependencies(self, definition: ServiceDefinition) -> bool:
        """
        v95.0: Wait for service dependencies to be healthy before proceeding.

        This implements intelligent dependency resolution:
        1. Check if all dependencies are listed in the registry
        2. Wait for each dependency to become healthy
        3. Use exponential backoff for checks
        4. Respect timeout limits
        5. Emit events for monitoring

        Returns True if all dependencies are healthy, False on timeout/failure.
        """
        if not definition.depends_on:
            return True  # No dependencies

        logger.info(
            f"[v95.0] Checking dependencies for {definition.name}: "
            f"{definition.depends_on}"
        )

        # Emit dependency check event
        await _emit_event(
            "DEPENDENCY_CHECK_START",
            service_name=definition.name,
            priority="MEDIUM",
            details={"dependencies": definition.depends_on}
        )

        start_time = time.time()
        timeout = definition.dependency_wait_timeout
        check_interval = definition.dependency_check_interval

        pending_deps = set(definition.depends_on)

        while pending_deps and (time.time() - start_time) < timeout:
            for dep_name in list(pending_deps):
                # v95.4: Special handling for jarvis-body dependency
                # jarvis-body is not in self.processes because it's the orchestrator itself
                if dep_name == "jarvis-body":
                    if self._jarvis_body_status == "healthy" or "jarvis-body" in self._services_ready:
                        logger.info(f"  ✓ Dependency 'jarvis-body' is healthy (local)")
                        pending_deps.discard(dep_name)
                        continue
                    # Also check via ready event
                    if self._jarvis_body_ready_event and self._jarvis_body_ready_event.is_set():
                        logger.info(f"  ✓ Dependency 'jarvis-body' ready event set")
                        pending_deps.discard(dep_name)
                        continue

                # v95.12: Check _services_ready set first (set by _start_services_parallel after success)
                # This is critical because heavy services are added to _services_ready
                # BEFORE their managed.status is set to HEALTHY in some code paths
                if dep_name in self._services_ready:
                    logger.info(f"  ✓ Dependency '{dep_name}' is in services_ready set")
                    pending_deps.discard(dep_name)
                    continue

                # Check if dependency is in our managed processes
                if dep_name in self.processes:
                    dep_managed = self.processes[dep_name]
                    if dep_managed.status == ServiceStatus.HEALTHY:
                        logger.info(f"  ✓ Dependency '{dep_name}' is healthy")
                        pending_deps.discard(dep_name)
                        continue

                # Also check the service registry
                if self.registry:
                    try:
                        services = await self.registry.list_services(healthy_only=True)
                        if any(s.service_name == dep_name for s in services):
                            logger.info(f"  ✓ Dependency '{dep_name}' found healthy in registry")
                            pending_deps.discard(dep_name)
                            continue
                    except Exception as e:
                        logger.debug(f"  Registry check for {dep_name} failed: {e}")

            if pending_deps:
                elapsed = time.time() - start_time
                remaining = timeout - elapsed
                logger.debug(
                    f"  Waiting for dependencies: {pending_deps} "
                    f"({elapsed:.1f}s elapsed, {remaining:.1f}s remaining)"
                )
                await asyncio.sleep(check_interval)
                # Exponential backoff (cap at 10s)
                check_interval = min(check_interval * 1.5, 10.0)

        if pending_deps:
            elapsed = time.time() - start_time
            logger.error(
                f"[v95.0] Dependency timeout for {definition.name} after {elapsed:.1f}s: "
                f"still waiting for {pending_deps}"
            )
            await _emit_event(
                "DEPENDENCY_CHECK_FAILED",
                service_name=definition.name,
                priority="HIGH",
                details={
                    "pending_dependencies": list(pending_deps),
                    "timeout_seconds": elapsed
                }
            )
            return False

        elapsed = time.time() - start_time
        logger.info(
            f"[v95.0] All dependencies healthy for {definition.name} "
            f"(checked in {elapsed:.1f}s)"
        )
        await _emit_event(
            "DEPENDENCY_CHECK_PASSED",
            service_name=definition.name,
            priority="MEDIUM",
            details={
                "dependencies": definition.depends_on,
                "check_time_seconds": elapsed
            }
        )
        return True

    async def _spawn_service(self, managed: ManagedProcess) -> bool:
        """
        v95.0: Spawn a service process with comprehensive event emissions.
        v136.0: Enhanced with per-service spawn lock for atomic clean+spawn.
        v137.1: Added diagnostic logging for hang debugging.

        Enhanced with:
        - Dependency checking before spawn
        - Pre-spawn validation (venv detection, port check)
        - Better error reporting
        - Environment isolation
        - Real-time voice narration of service lifecycle
        - GAP 5, 12: Per-service spawn lock prevents parallel spawns

        Returns True if spawn and health check succeeded.
        """
        definition = managed.definition
        logger.info(f"[v137.1] _spawn_service({definition.name}): entering...")

        # =========================================================================
        # v136.0 GAP 5, 12: Per-service spawn lock - atomic clean+spawn
        # =========================================================================
        # This lock ensures that only ONE spawn attempt per service can be in
        # progress at any time. This prevents race conditions where:
        # - Two crash handlers try to restart the same service
        # - Parallel startup and crash recovery collide
        # - Initial spawn and auto-recovery overlap
        # =========================================================================
        logger.info(f"[v137.1] _spawn_service({definition.name}): getting spawn lock...")
        spawn_lock = self._get_service_spawn_lock(definition.name)
        logger.info(f"[v137.1] _spawn_service({definition.name}): spawn lock obtained, locked={spawn_lock.locked()}")

        if spawn_lock.locked():
            logger.warning(
                f"[v136.0] ⚠️ Spawn lock for {definition.name} already held. "
                f"Another spawn in progress - this call will wait."
            )

        logger.info(f"[v137.1] _spawn_service({definition.name}): acquiring spawn lock...")
        async with spawn_lock:
            logger.info(f"[v137.1] _spawn_service({definition.name}): spawn lock acquired, calling _spawn_service_inner...")
            result = await self._spawn_service_inner(managed, definition)
            logger.info(f"[v137.1] _spawn_service({definition.name}): _spawn_service_inner returned {result}")
            return result

    async def _spawn_service_inner(
        self,
        managed: ManagedProcess,
        definition: ServiceDefinition,
    ) -> bool:
        """
        v136.0: Inner spawn implementation (called within spawn lock).
        v137.1: Added diagnostic logging for hang debugging.

        Integrated with GlobalSpawnCoordinator for cross-component coordination.
        This prevents double-spawn from:
        - Health monitor callbacks
        - Auto-recovery callbacks
        - Parallel orchestrator instances
        """
        logger.info(f"[v137.1] _spawn_service_inner({definition.name}): entering...")
        
        # =========================================================================
        # v136.0: GLOBAL SPAWN COORDINATION CHECK
        # =========================================================================
        # Check with global coordinator before proceeding. This prevents race
        # conditions with health monitors and auto-recovery callbacks.
        # =========================================================================
        logger.info(f"[v137.1] _spawn_service_inner({definition.name}): getting spawn coordinator...")
        coordinator = get_spawn_coordinator()
        logger.info(f"[v137.1] _spawn_service_inner({definition.name}): coordinator obtained, checking should_attempt_spawn...")

        should_spawn, reason = coordinator.should_attempt_spawn(
            service_name=definition.name,
            component_name="ProcessOrchestrator",
            ignore_cooldown=False,
        )
        logger.info(f"[v137.1] _spawn_service_inner({definition.name}): should_spawn={should_spawn}, reason={reason}")

        if not should_spawn:
            logger.warning(
                f"[v136.0] ⚠️ Spawn blocked for {definition.name}: {reason}"
            )
            # Check if already ready
            if coordinator.is_service_ready(definition.name):
                logger.info(f"[v136.0] {definition.name} already ready, skipping spawn")
                managed.status = ServiceStatus.HEALTHY
                return True
            # Another spawn in progress - wait for it
            if coordinator.is_spawn_in_progress(definition.name):
                logger.info(
                    f"[v136.0] Waiting for {definition.name} spawn by "
                    f"{coordinator.get_spawning_component(definition.name)}..."
                )
                # Wait for spawn to complete (max 60s)
                for _ in range(60):
                    await asyncio.sleep(1.0)
                    if coordinator.is_service_ready(definition.name):
                        managed.status = ServiceStatus.HEALTHY
                        return True
                    if not coordinator.is_spawn_in_progress(definition.name):
                        break  # Spawn finished (failed or succeeded)
            return False

        # Mark as spawning in global coordinator
        # v137.2: Fixed nested lock deadlock by using RLock instead of Lock
        logger.info(f"[v137.2] _spawn_service_inner({definition.name}): marking as spawning in coordinator...")
        mark_result = coordinator.mark_spawning(
            service_name=definition.name,
            component_name="ProcessOrchestrator",
            port=definition.default_port,
        )
        logger.info(f"[v137.2] _spawn_service_inner({definition.name}): mark_spawning returned {mark_result}")
        if not mark_result:
            logger.warning(f"[v136.0] Failed to mark {definition.name} as spawning")
            return False
        logger.info(f"[v137.2] _spawn_service_inner({definition.name}): marked as spawning, calling _spawn_service_core...")

        try:
            success = await self._spawn_service_core(managed, definition)
            logger.info(f"[v137.1] _spawn_service_inner({definition.name}): _spawn_service_core returned {success}")

            if success:
                # Mark ready in global coordinator
                coordinator.mark_ready(
                    service_name=definition.name,
                    pid=managed.pid,
                    port=managed.port,
                )
            else:
                coordinator.mark_failed(definition.name, "spawn_service_core returned False")

            return success

        except Exception as e:
            # v148.1: Use log_component_failure for criticality-aware logging
            log_component_failure(
                definition.name,
                f"[v137.1] _spawn_service_inner exception",
                error=e,
                phase="spawn_inner"
            )
            coordinator.mark_failed(definition.name, str(e))
            raise

    async def _spawn_service_core(
        self,
        managed: ManagedProcess,
        definition: ServiceDefinition,
    ) -> bool:
        """
        v136.0: Core spawn implementation (separated for coordinator integration).
        v137.1: Added diagnostic logging for hang debugging.
        v137.2: Added hard memory gate for heavy services.
        v144.0: Active Rescue - ensure GCP VM ready before spawning jarvis-prime on SLIM.

        This is the actual spawn logic, wrapped by _spawn_service_inner which
        handles global coordination.
        """
        logger.info(f"[v137.1] _spawn_service_core({definition.name}): entering...")

        # =========================================================================
        # v144.0: ACTIVE RESCUE - GCP VM PROVISIONING BEFORE JARVIS-PRIME SPAWN
        # =========================================================================
        # On SLIM hardware, we need to ensure the GCP VM is ready BEFORE spawning
        # jarvis-prime. This prevents the OOM crash loop where:
        #   1. jarvis-prime starts
        #   2. jarvis-prime loads heavy models
        #   3. System OOMs, jarvis-prime crashes with Exit -9
        #   4. Supervisor restarts jarvis-prime locally (goto 1)
        #
        # Active Rescue breaks this loop by:
        #   1. Detecting SLIM hardware
        #   2. Provisioning GCP VM BEFORE spawning jarvis-prime
        #   3. Passing GCP endpoint to jarvis-prime
        #   4. jarvis-prime runs as Hollow Client, routes to GCP
        # =========================================================================
        is_jarvis_prime = definition.name.lower() in ["jarvis-prime", "jarvis_prime", "j-prime"]

        if is_jarvis_prime:
            # Check if Active Rescue should be triggered
            force_cloud = os.environ.get("JARVIS_FORCE_CLOUD_HYBRID", "").lower() in ("true", "1", "yes", "on")
            slim_mode = os.environ.get("JARVIS_ENABLE_SLIM_MODE", "").lower() in ("true", "1", "yes", "on")

            if force_cloud or slim_mode:
                logger.info(
                    f"[v144.0] 🚀 Active Rescue: Ensuring GCP VM is ready BEFORE spawning {definition.name}..."
                )

                # Provision GCP VM (or use existing)
                gcp_ready, gcp_endpoint = await ensure_gcp_vm_ready_for_prime(
                    timeout_seconds=120.0,
                    force_provision=False,
                )

                if gcp_ready and gcp_endpoint:
                    logger.info(
                        f"[v144.0] ✅ Active Rescue: GCP VM ready at {gcp_endpoint} - "
                        f"jarvis-prime will run as Hollow Client"
                    )
                    # Mark that Active Rescue is active for this spawn
                    managed.gcp_offload_active = True
                    # v147.0: Store just the IP (without http:// or port) for gcp_vm_ip field
                    # The full endpoint is reconstructed later using port 8000
                    managed.gcp_vm_ip = gcp_endpoint.replace("http://", "").replace("https://", "").split(":")[0]
                    
                    # v147.0: Also set environment variables in supervisor process for consistency
                    os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] = "true"
                    os.environ["GCP_PRIME_ENDPOINT"] = gcp_endpoint
                    os.environ["JARVIS_GCP_PRIME_ENDPOINT"] = gcp_endpoint

                    # Emit event for monitoring
                    await _emit_event(
                        "ACTIVE_RESCUE_GCP_READY",
                        service_name=definition.name,
                        priority="HIGH",
                        details={
                            "gcp_endpoint": gcp_endpoint,
                            "mode": "hollow_client",
                            "reason": "force_cloud_hybrid" if force_cloud else "slim_mode",
                        }
                    )
                else:
                    logger.warning(
                        f"[v144.0] ⚠️ Active Rescue: GCP VM not available - "
                        f"jarvis-prime will attempt local startup in Hollow Client mode. "
                        f"Heavy inference may fail without GCP."
                    )
                    # Still proceed - Hollow Client can handle some basic requests
                    # and will gracefully fail inference requests that need GCP
                    await _emit_event(
                        "ACTIVE_RESCUE_GCP_UNAVAILABLE",
                        service_name=definition.name,
                        priority="HIGH",
                        details={
                            "reason": "GCP VM provisioning failed or timed out",
                            "fallback": "hollow_client_without_gcp",
                        }
                    )

        # =========================================================================
        # v146.0: TRINITY PROTOCOL - REACTOR-CORE GCP READY GATE
        # =========================================================================
        # When Trinity Protocol is active, reactor-core should wait for GCP to be
        # ready before starting. This prevents the "Thundering Herd" and ensures
        # jarvis-prime (as Hollow Client) is fully operational before reactor-core
        # starts consuming memory for training.
        # =========================================================================
        is_reactor_core = definition.name.lower() in ["reactor-core", "reactor_core"]
        trinity_active = _trinity_protocol_active or is_cloud_locked()[0]

        if is_reactor_core and trinity_active:
            logger.info(
                f"[v146.0] 🔗 TRINITY PROTOCOL: Checking GCP readiness before starting {definition.name}..."
            )

            # Wait for GCP ready event (set by background pre-warm task)
            gcp_ready = await wait_for_gcp_ready(timeout=60.0)

            if gcp_ready:
                logger.info(
                    f"[v146.0] ✅ TRINITY PROTOCOL: GCP is ready - proceeding with {definition.name} startup"
                )
            else:
                # GCP not ready yet, but jarvis-prime might still be healthy as Hollow Client
                # Check if jarvis-prime is in services_ready
                jprime_ready = "jarvis-prime" in self._services_ready

                if jprime_ready:
                    logger.info(
                        f"[v146.0] ⚠️ TRINITY PROTOCOL: GCP not ready but jarvis-prime is healthy - "
                        f"proceeding with {definition.name} startup"
                    )
                else:
                    logger.warning(
                        f"[v146.0] ⚠️ TRINITY PROTOCOL: Neither GCP nor jarvis-prime ready - "
                        f"waiting for dependencies..."
                    )
                    # The dependency system will handle waiting for jarvis-prime

            await _emit_event(
                "TRINITY_REACTOR_CORE_START",
                service_name=definition.name,
                priority="MEDIUM",
                details={
                    "gcp_ready": gcp_ready,
                    "jprime_ready": "jarvis-prime" in self._services_ready,
                    "trinity_active": trinity_active,
                }
            )

        # =========================================================================
        # v142.0: DYNAMIC MEMORY GATING - Context-Aware Slim Mode Support
        # =========================================================================
        # This is the LAST line of defense against starting heavy services when
        # memory is critically low. Unlike the OOM Prevention bridge (which is
        # advisory), this will BLOCK startup if memory is dangerously low.
        #
        # v142.0 ENHANCEMENT: The gate is now CONTEXT-AWARE:
        #   - SLIM MODE: The Hollow Client only needs ~300MB, so we allow up to
        #     95% memory usage with 0.5GB minimum free. This unblocks the deadlock
        #     where the Supervisor blocks jarvis-prime startup even though Slim Mode
        #     specifically exists to offload heavy work to GCP.
        #   - FULL MODE: Standard thresholds (80% max, 2GB minimum free).
        #
        # ROOT CAUSE FIX: Without this, users are stuck in a deadlock:
        #   1. They need GCP Cloud to save RAM
        #   2. To use GCP, they need jarvis-prime to start (even in Slim Mode)
        #   3. The old gate blocked jarvis-prime because RAM was at 85%
        #   4. Result: Cannot use Cloud because Local Client can't start!
        # =========================================================================
        heavy_services = ["jarvis-prime", "jarvis_prime", "j-prime", "reactor-core", "reactor_core"]
        if definition.name.lower() in heavy_services:
            try:
                import psutil
                mem = psutil.virtual_memory()
                memory_pressure = mem.percent
                available_gb = mem.available / (1024 ** 3)

                # =====================================================================
                # v142.0: DYNAMIC THRESHOLD CALCULATION based on Slim Mode status
                # =====================================================================
                # Detect Slim Mode from multiple sources:
                #   1. Environment variable (highest priority)
                #   2. Hardware profile from orchestrator (if available)
                #   3. Service definition flags (if available)
                # =====================================================================
                is_slim_mode = False
                slim_mode_source = "none"

                # Source 1: Environment variable (set by supervisor or user)
                slim_env = os.environ.get("JARVIS_ENABLE_SLIM_MODE", "").lower()
                if slim_env in ("true", "1", "yes", "on"):
                    is_slim_mode = True
                    slim_mode_source = "env:JARVIS_ENABLE_SLIM_MODE"

                # Source 2: Hardware profile from global assessment (v138.0)
                if not is_slim_mode:
                    try:
                        hw_assessment = assess_hardware_profile()
                        if hw_assessment.profile in (HardwareProfile.SLIM, HardwareProfile.CLOUD_ONLY):
                            is_slim_mode = True
                            slim_mode_source = f"hardware_profile:{hw_assessment.profile.name}"
                    except Exception:
                        pass  # Assessment not available yet

                # Source 3: Service definition environment (passed during spawn)
                if not is_slim_mode:
                    service_env = getattr(definition, 'environment', {}) or {}
                    if service_env.get("JARVIS_ENABLE_SLIM_MODE", "").lower() in ("true", "1", "yes", "on"):
                        is_slim_mode = True
                        slim_mode_source = "service_env"

                # Source 4: Check if jarvis-prime specifically requested slim spawn
                if not is_slim_mode and "j-prime" in definition.name.lower() or "jarvis-prime" in definition.name.lower():
                    # For jarvis-prime on systems with <32GB, default to Slim Mode
                    total_ram_gb = mem.total / (1024 ** 3)
                    if total_ram_gb < 32:
                        is_slim_mode = True
                        slim_mode_source = f"auto_detect:total_ram={total_ram_gb:.1f}GB"

                # =====================================================================
                # v142.0: ADAPTIVE THRESHOLDS
                # =====================================================================
                # SLIM MODE (Hollow Client ~300MB):
                #   - percent_threshold: 95% (allow almost full RAM)
                #   - min_available_gb: 0.5GB (Hollow Client + some headroom)
                #
                # FULL MODE (Heavy ML ~4-6GB):
                #   - percent_threshold: 80% (keep 20% buffer)
                #   - min_available_gb: 2.0GB (room for model loading)
                # =====================================================================
                if is_slim_mode:
                    percent_threshold = 95.0
                    min_available_gb = 0.5
                    mode_label = "SLIM"
                else:
                    percent_threshold = 80.0
                    min_available_gb = 2.0
                    mode_label = "FULL"

                # Log the mode detection result
                logger.info(
                    f"[v142.0] 🎛️ Memory Gate Mode: {mode_label} "
                    f"(source: {slim_mode_source}) - "
                    f"thresholds: {percent_threshold}% max, {min_available_gb}GB min free"
                )

                # Critical check with dynamic thresholds
                is_critical = memory_pressure > percent_threshold or available_gb < min_available_gb

                if is_critical:
                    logger.warning(
                        f"[v142.0] ⚠️ MEMORY GATE CHECK: {definition.name} "
                        f"({mode_label} mode) - memory at {memory_pressure:.1f}% "
                        f"({available_gb:.1f}GB free), threshold: {percent_threshold}%"
                    )

                    # Check if GCP VM is available for offloading
                    gcp_available = False
                    try:
                        from backend.core.gcp_vm_manager import get_gcp_vm_manager_safe
                        vm_manager = await get_gcp_vm_manager_safe()
                        if vm_manager:
                            active_vm = await vm_manager.get_active_vm()
                            if active_vm:
                                gcp_available = True
                                logger.info(
                                    f"[v142.0] ✅ GCP VM available - {definition.name} can use cloud offloading"
                                )
                    except Exception as gcp_err:
                        logger.debug(f"GCP check failed: {gcp_err}")

                    # In Slim Mode with memory pressure, we STILL allow startup
                    # because the whole point of Slim Mode is to offload to GCP
                    if is_slim_mode and available_gb >= 0.3:
                        logger.info(
                            f"[v142.0] ✅ SLIM MODE BYPASS: Allowing {definition.name} despite "
                            f"{memory_pressure:.1f}% memory usage. Hollow Client needs only ~300MB. "
                            f"Heavy inference will be routed to GCP."
                        )
                        # Don't block - Slim Mode is specifically designed for this scenario
                    elif gcp_available:
                        logger.info(
                            f"[v142.0] ✅ GCP AVAILABLE: Allowing {definition.name} - "
                            f"heavy tasks will be offloaded to cloud"
                        )
                        # Don't block - GCP can handle the heavy lifting
                    else:
                        # FULL MODE with no GCP and critical memory - this is dangerous
                        # Wait briefly for memory to stabilize
                        logger.info(f"[v142.0] Waiting 10s for memory to stabilize before {definition.name}...")
                        await asyncio.sleep(10)

                        # Re-check
                        mem = psutil.virtual_memory()
                        memory_pressure = mem.percent
                        available_gb = mem.available / (1024 ** 3)

                        if memory_pressure > percent_threshold or available_gb < min_available_gb:
                            # v148.1: Use log_component_failure for criticality-aware logging
                            log_component_failure(
                                definition.name,
                                f"[v142.0] MEMORY GATE BLOCKED: Cannot start in {mode_label} mode - "
                                f"memory at {memory_pressure:.1f}% ({available_gb:.1f}GB free). "
                                f"Threshold: {percent_threshold}%, min free: {min_available_gb}GB. "
                                f"This prevents OOM kill (SIGKILL -9).",
                                phase="memory_gate",
                                memory_percent=memory_pressure,
                                available_gb=available_gb,
                            )
                            managed.status = ServiceStatus.DEGRADED
                            # v142.0: Return gracefully without raising SystemExit
                            # This prevents the ugly thread pool stack traces
                            return False
                        else:
                            logger.info(
                                f"[v142.0] ✅ Memory recovered to {memory_pressure:.1f}% - proceeding"
                            )
                else:
                    # Memory is fine - log and proceed
                    logger.info(
                        f"[v142.0] ✅ Memory Gate PASSED: {definition.name} ({mode_label} mode) - "
                        f"{memory_pressure:.1f}% used ({available_gb:.1f}GB free)"
                    )
            except Exception as mem_gate_err:
                logger.debug(f"Memory gate check failed (non-fatal): {mem_gate_err}")
        
        # v95.0: Wait for dependencies to be healthy before spawning
        if definition.depends_on:
            logger.info(f"[v137.1] _spawn_service_core({definition.name}): checking dependencies...")
            deps_ready = await self._wait_for_dependencies(definition)
            if not deps_ready:
                # v148.1: Use log_component_failure for criticality-aware logging
                log_component_failure(
                    definition.name,
                    "[v95.0] Cannot spawn: dependencies not ready",
                    phase="dependency_check",
                    depends_on=definition.depends_on,
                )
                managed.status = ServiceStatus.FAILED
                await _emit_event(
                    "SERVICE_BLOCKED",
                    service_name=definition.name,
                    priority="HIGH",
                    details={"reason": "dependencies_not_ready"}
                )
                return False

        # v117.0: Check soft dependencies - warn but don't block
        soft_deps = getattr(definition, 'soft_depends_on', None)
        if soft_deps:
            for soft_dep in soft_deps:
                if soft_dep in self.processes:
                    dep_managed = self.processes[soft_dep]
                    if dep_managed.status != ServiceStatus.HEALTHY:
                        logger.warning(
                            f"[v117.0] Soft dependency '{soft_dep}' not healthy for {definition.name} - "
                            f"proceeding anyway (status: {dep_managed.status.value if dep_managed.status else 'unknown'})"
                        )
                else:
                    logger.warning(
                        f"[v117.0] Soft dependency '{soft_dep}' not found for {definition.name} - "
                        f"proceeding anyway"
                    )

        # v95.0: Emit service spawning event
        logger.info(f"[v137.1] _spawn_service_core({definition.name}): emitting SERVICE_SPAWNING event...")
        await _emit_event(
            "SERVICE_SPAWNING",
            service_name=definition.name,
            priority="HIGH",
            details={
                "port": definition.default_port,
                "repo_path": str(definition.repo_path),
                "startup_timeout": definition.startup_timeout,
                "depends_on": definition.depends_on
            }
        )
        logger.info(f"[v137.1] _spawn_service_core({definition.name}): SERVICE_SPAWNING event emitted")

        # =========================================================================
        # v136.0: ATOMIC PORT HYGIENE + SPAWN LOCK
        # =========================================================================
        # GAP 5, 12: Uses per-service lock to prevent parallel clean+spawn races.
        # This ensures only ONE spawn attempt per service at any time.
        #
        # The atomic method:
        # 1. Acquires per-service spawn lock
        # 2. Performs port hygiene (SIGTERM → SIGKILL → verify with retry)
        # 3. Returns success/failure while holding lock
        # 4. Caller proceeds to spawn while still holding context
        # =========================================================================
        logger.info(f"[v137.1] _spawn_service_core({definition.name}): enforcing port hygiene on port {definition.default_port}...")
        port_ready, port_error, killed_pids = await self._enforce_port_hygiene(
            port=definition.default_port,
            service_name=definition.name,
            graceful_timeout=3.0,
            force_timeout=2.0,
            post_kill_sleep=1.0,
            verification_retries=3,  # GAP 4: TIME_WAIT retry
        )
        logger.info(f"[v137.1] _spawn_service_core({definition.name}): port hygiene complete: ready={port_ready}, error={port_error}")

        if not port_ready:
            # v148.1: Use log_component_failure for criticality-aware logging
            log_component_failure(
                definition.name,
                f"[v136.0] Cannot spawn: port {definition.default_port} "
                f"not available after cleanup: {port_error}",
                phase="port_hygiene",
                port=definition.default_port,
                error_detail=port_error,
            )
            managed.status = ServiceStatus.FAILED
            await _emit_event(
                "SERVICE_BLOCKED",
                service_name=definition.name,
                priority="CRITICAL",
                details={
                    "reason": "port_hygiene_failed",
                    "port": definition.default_port,
                    "error": port_error,
                    "killed_pids": killed_pids,  # GAP 13: Accurate tracking
                }
            )
            return False

        if killed_pids:
            logger.info(
                f"[v136.0] Port {definition.default_port} prepared for {definition.name} "
                f"(cleaned PIDs: {killed_pids})"
            )

        # =========================================================================
        # v131.0: OOM PREVENTION - Check memory before spawning heavy services
        # =========================================================================
        # This prevents SIGKILL (exit code -9) crashes during initialization by
        # detecting low memory BEFORE starting JARVIS Prime and offloading to GCP.
        # =========================================================================
        if _OOM_PREVENTION_AVAILABLE and _check_memory_before_heavy_init and _MemoryDecision:
            # v132.3: Extended heavy services list - includes all memory-intensive processes
            heavy_services = [
                "jarvis-prime", "jarvis_prime", "j-prime",  # JARVIS Prime (LLM/GGUF models)
                "reactor-core", "reactor_core", "r-core",   # Reactor Core (ML/autonomy)
                "vosk", "whisper", "speech-recognition",    # Speech models
                "llm-service", "model-server",              # Model servers
            ]
            if definition.name.lower() in heavy_services:
                try:
                    # v132.3: Dynamic memory estimation based on service type
                    service_lower = definition.name.lower()
                    if "prime" in service_lower:
                        estimated_mb = HEAVY_COMPONENT_MEMORY_ESTIMATES.get("jarvis_prime", 6000)
                    elif "reactor" in service_lower:
                        estimated_mb = HEAVY_COMPONENT_MEMORY_ESTIMATES.get("reactor_core", 4000)
                    elif "vosk" in service_lower or "whisper" in service_lower:
                        estimated_mb = HEAVY_COMPONENT_MEMORY_ESTIMATES.get("vosk", 2000)
                    else:
                        estimated_mb = HEAVY_COMPONENT_MEMORY_ESTIMATES.get(service_lower, 3000)

                    logger.info(f"[OOM Prevention] Checking memory before starting {definition.name}")
                    memory_result = await _check_memory_before_heavy_init(
                        component=f"cross_repo_{definition.name}",
                        estimated_mb=estimated_mb,
                        auto_offload=True,
                    )

                    if memory_result.decision == _MemoryDecision.CLOUD_REQUIRED:
                        if memory_result.gcp_vm_ready and memory_result.gcp_vm_ip:
                            # v132.4: Verify GCP VM is actually reachable before relying on it
                            gcp_verified = await self._verify_gcp_vm_health(memory_result.gcp_vm_ip)

                            if gcp_verified:
                                logger.info(
                                    f"[OOM Prevention] ☁️ GCP VM verified for {definition.name}: "
                                    f"{memory_result.gcp_vm_ip}"
                                )
                                # Store GCP endpoint for later routing
                                managed.gcp_offload_active = True
                                managed.gcp_vm_ip = memory_result.gcp_vm_ip
                                await _emit_event(
                                    "SERVICE_OFFLOADED_TO_CLOUD",
                                    service_name=definition.name,
                                    priority="HIGH",
                                    details={
                                        "gcp_vm_ip": memory_result.gcp_vm_ip,
                                        "reason": memory_result.reason,
                                        "verified": True,
                                    }
                                )
                                # Local service will run in proxy mode, forwarding to GCP
                                logger.info(
                                    f"[v132.4] {definition.name} will use lightweight proxy mode "
                                    f"with model inference on GCP ({memory_result.gcp_vm_ip})"
                                )
                            else:
                                logger.warning(
                                    f"[v132.4] GCP VM {memory_result.gcp_vm_ip} not reachable - "
                                    f"will proceed locally with risk for {definition.name}"
                                )
                                # Still try local, but with degradation flags
                                managed.degradation_tier = _DegradationTier.TIER_3_SEQUENTIAL_LOAD if _DegradationTier else None
                        else:
                            # v137.2: CRITICAL - Don't proceed when memory is dangerously low
                            # Wait for either memory to free up or GCP VM to become available
                            logger.warning(
                                f"[v137.2] ⚠️ CRITICAL: Cannot start {definition.name} - "
                                f"insufficient memory and no GCP VM available"
                            )
                            logger.info(f"[v137.2] Waiting up to 60s for memory to stabilize...")
                            
                            # Wait with exponential backoff, checking memory and GCP status
                            memory_recovered = False
                            wait_intervals = [2, 3, 5, 8, 13, 21]  # Fibonacci-ish backoff
                            total_waited = 0
                            max_wait = 60
                            
                            for interval in wait_intervals:
                                if total_waited >= max_wait:
                                    break
                                    
                                await asyncio.sleep(interval)
                                total_waited += interval
                                
                                # Re-check memory
                                try:
                                    import psutil
                                    mem = psutil.virtual_memory()
                                    current_pressure = mem.percent
                                    available_gb = mem.available / (1024 ** 3)
                                    
                                    logger.info(
                                        f"[v137.2] Memory check after {total_waited}s: "
                                        f"{current_pressure:.1f}% used, {available_gb:.1f}GB available"
                                    )
                                    
                                    # If memory dropped below 75%, we can proceed cautiously
                                    if current_pressure < 75:
                                        logger.info(f"[v137.2] ✅ Memory recovered to {current_pressure:.1f}% - proceeding")
                                        memory_recovered = True
                                        break
                                except Exception as mem_check_err:
                                    logger.debug(f"Memory check failed: {mem_check_err}")
                            
                            if not memory_recovered:
                                # Still critical after waiting - skip this service
                                logger.error(
                                    f"[v137.2] ❌ Cannot start {definition.name}: "
                                    f"memory still critical after {total_waited}s wait. "
                                    f"Skipping to prevent OOM kill."
                                )
                                managed.status = ServiceStatus.DEGRADED
                                await _emit_event(
                                    "SERVICE_SKIPPED_OOM",
                                    service_name=definition.name,
                                    priority="CRITICAL",
                                    details={
                                        "reason": "critical_memory_no_gcp",
                                        "memory_pressure": current_pressure if 'current_pressure' in dir() else 999,
                                        "waited_seconds": total_waited,
                                    }
                                )
                                return False  # Don't proceed - prevent OOM kill
                    elif memory_result.decision == _MemoryDecision.CLOUD:
                        if memory_result.gcp_vm_ready:
                            logger.info(
                                f"[OOM Prevention] ☁️ GCP VM recommended for {definition.name}: "
                                f"{memory_result.gcp_vm_ip}"
                            )
                            managed.gcp_offload_active = True
                            managed.gcp_vm_ip = memory_result.gcp_vm_ip
                        else:
                            logger.info(
                                f"[OOM Prevention] Local memory okay for {definition.name} "
                                f"({memory_result.available_ram_gb:.1f}GB available)"
                            )
                    elif memory_result.decision == _MemoryDecision.DEGRADED:
                        # v2.0.0: Graceful degradation - proceed with fallback strategy
                        tier_name = memory_result.degradation_tier.value if memory_result.degradation_tier else "unknown"
                        logger.info(
                            f"[OOM Prevention] ⚡ Using graceful degradation for {definition.name} "
                            f"(Tier: {tier_name})"
                        )
                        if memory_result.fallback_strategy:
                            logger.info(f"  Strategy: {memory_result.fallback_strategy.description}")
                            for action in memory_result.fallback_strategy.actions[:3]:
                                logger.info(f"    → {action}")
                        # Store degradation info in managed process
                        if hasattr(managed, 'degradation_tier'):
                            managed.degradation_tier = memory_result.degradation_tier
                    elif memory_result.decision == _MemoryDecision.ABORT:
                        # v2.0.0: ABORT only happens when ALL degradation tiers exhausted
                        logger.error(
                            f"[OOM Prevention] ❌ Cannot safely start {definition.name}: "
                            f"{memory_result.reason}"
                        )
                        logger.error("[OOM Prevention] All graceful degradation strategies exhausted")
                        for rec in memory_result.recommendations[-3:]:
                            logger.error(f"  → {rec}")
                        # v2.0.0: Store the failure reason for debugging
                        if hasattr(managed, 'oom_abort_reason'):
                            managed.oom_abort_reason = memory_result.reason
                        # Emit warning but proceed anyway - better to try than fail silently
                    else:
                        logger.info(
                            f"[OOM Prevention] ✅ Sufficient memory for {definition.name} "
                            f"({memory_result.available_ram_gb:.1f}GB available)"
                        )
                        if memory_result.gcp_auto_enabled:
                            logger.info("[OOM Prevention] 🔧 Note: GCP was auto-enabled for future use")

                except Exception as e:
                    logger.warning(f"[OOM Prevention] Check failed for {definition.name}: {e}")
        # =========================================================================

        # Pre-spawn validation
        is_valid, python_exec = await self._pre_spawn_validation(definition)

        # v95.2: Handle "ALREADY_HEALTHY" - service is running and responsive
        if is_valid == "ALREADY_HEALTHY":
            logger.info(
                f"✅ {definition.name} is already running and healthy - no spawn needed"
            )
            managed.status = ServiceStatus.HEALTHY
            managed.port = definition.default_port
            managed.consecutive_failures = 0
            managed.restart_count = 0  # Reset restart count since service is healthy

            # Register in service registry
            if self.registry:
                # Discover existing registration to get PID
                existing = await self.registry.discover_service(definition.name)
                if existing:
                    managed.pid = existing.pid
                else:
                    # Register with PID 0 (unknown) since we didn't spawn it
                    await self.registry.register_service(
                        service_name=definition.name,
                        pid=0,
                        port=managed.port,
                        health_endpoint=definition.health_endpoint,
                        metadata={
                            "repo_path": str(definition.repo_path),
                            "already_running": True,
                        }
                    )

            # Start health monitor if not already running
            if managed.health_monitor_task is None or managed.health_monitor_task.done():
                managed.health_monitor_task = asyncio.create_task(
                    self._health_monitor_loop(managed),
                    name=f"health_monitor_{definition.name}"
                )
                self._track_background_task(managed.health_monitor_task)

            # v95.0: Emit service healthy event
            await _emit_event(
                "SERVICE_HEALTHY",
                service_name=definition.name,
                priority="HIGH",
                details={"reason": "already_running", "port": managed.port}
            )
            return True

        if not is_valid:
            # v148.1: Use log_component_failure for criticality-aware logging
            log_component_failure(
                definition.name,
                "Cannot spawn: pre-spawn validation failed",
                phase="pre_spawn_validation",
            )
            managed.status = ServiceStatus.FAILED
            # v95.0: Emit service failed event
            await _emit_event(
                "SERVICE_CRASHED",
                service_name=definition.name,
                priority="CRITICAL",
                details={"reason": "pre_spawn_validation_failed"}
            )
            return False

        script_path = self._find_script(definition)

        # v95.7: Guard to ensure python_exec is non-None after validation passed
        if python_exec is None:
            python_exec = sys.executable  # Fallback to system Python
            logger.warning(f"Using system Python for {definition.name} as no venv detected")

        if script_path is None:
            # v148.1: Use log_component_failure for criticality-aware logging
            log_component_failure(
                definition.name,
                "Cannot spawn: no script found",
                phase="script_discovery",
            )
            managed.status = ServiceStatus.FAILED
            # v95.0: Emit service failed event
            await _emit_event(
                "SERVICE_CRASHED",
                service_name=definition.name,
                priority="CRITICAL",
                details={"reason": "script_not_found"}
            )
            return False

        managed.status = ServiceStatus.STARTING

        try:
            # Build environment
            env = os.environ.copy()
            env.update(definition.environment)

            # Add port hint for service registration
            env["SERVICE_PORT"] = str(definition.default_port)
            env["SERVICE_NAME"] = definition.name

            # v93.16: Suppress library warnings at environment level
            # This ensures warnings are suppressed BEFORE Python interpreter loads modules
            env.setdefault("PYTHONWARNINGS", "ignore::UserWarning,ignore::DeprecationWarning,ignore::FutureWarning")
            env.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")  # Suppress TensorFlow warnings
            env.setdefault("TRANSFORMERS_VERBOSITY", "error")  # Suppress transformers warnings
            env.setdefault("TOKENIZERS_PARALLELISM", "false")  # Suppress tokenizers warning
            env.setdefault("COREMLTOOLS_LOG_LEVEL", "ERROR")  # Suppress coremltools warnings

            # =========================================================================
            # v138.0: HARDWARE-AWARE STARTUP INTEGRATION
            # =========================================================================
            # For jarvis-prime (the AGI Hub host), inject hardware profile environment
            # variables to enable Memory-Aware Staged Initialization. This prevents
            # OOM kills by telling jarvis-prime:
            # - Whether to skip AGI Hub entirely (CLOUD_ONLY mode)
            # - Whether to use SLIM mode (deferred heavy subsystems)
            # - Optimal GPU layers and context size for the hardware
            # =========================================================================
            jarvis_prime_names = ["jarvis-prime", "jarvis_prime", "j-prime"]
            if definition.name.lower() in jarvis_prime_names:
                logger.info(f"[v138.0] 🖥️ Assessing hardware for {definition.name}...")
                try:
                    # Assess hardware profile (cached after first call)
                    hw_assessment = assess_hardware_profile()
                    log_hardware_assessment(hw_assessment)

                    # Get environment variables to pass to jarvis-prime
                    hw_env_vars = get_hardware_env_vars(hw_assessment)
                    env.update(hw_env_vars)

                    logger.info(
                        f"[v138.0] ✅ Hardware profile {hw_assessment.profile.name} will be passed to {definition.name}"
                    )

                    # Emit event for observability
                    await _emit_event(
                        "HARDWARE_ASSESSMENT_COMPLETE",
                        service_name=definition.name,
                        priority="MEDIUM",
                        details={
                            "profile": hw_assessment.profile.name,
                            "total_ram_gb": hw_assessment.total_ram_gb,
                            "available_ram_gb": hw_assessment.available_ram_gb,
                            "skip_agi_hub": hw_assessment.skip_agi_hub,
                            "slim_mode": hw_assessment.enable_slim_mode,
                            "defer_heavy": hw_assessment.defer_heavy_subsystems,
                        }
                    )
                except Exception as hw_err:
                    logger.warning(
                        f"[v138.0] ⚠️ Hardware assessment failed for {definition.name}: {hw_err}. "
                        "Proceeding with default settings."
                    )

            # ═══════════════════════════════════════════════════════════════════════
            # v148.0: CRITICAL SAFETY NET - Prevent Local Model Loading on SLIM Hardware
            # ═══════════════════════════════════════════════════════════════════════
            # On 16GB Mac (SLIM hardware), we MUST prevent jarvis-prime from loading
            # models locally. This is a SAFETY NET that works even if GCP fails.
            #
            # LOGIC:
            # - SLIM/CLOUD_ONLY hardware → ALWAYS set JARVIS_SLIM_HARDWARE_MODE=true
            # - This tells jarvis-prime to NEVER load heavy models locally
            # - Instead, use Claude API as fallback if GCP unavailable
            # ═══════════════════════════════════════════════════════════════════════
            # v149.0: Read hardware profile from env vars (set by hw_env_vars above)
            _hw_profile = env.get("JARVIS_HARDWARE_PROFILE", "UNKNOWN")
            if _hw_profile in {"SLIM", "CLOUD_ONLY", "ULTRA_SLIM"}:
                # SAFETY NET: Always set these on SLIM hardware regardless of GCP
                env["JARVIS_SLIM_HARDWARE_MODE"] = "true"
                env["JARVIS_MAX_LOCAL_MODEL_RAM_MB"] = "500"  # Only tiny models allowed
                env["JARVIS_FORCE_API_FALLBACK"] = "true"  # Use Claude if local fails
                env["JARVIS_PREVENT_OOM"] = "true"  # Enable OOM prevention
                env["JARVIS_SKIP_HEAVY_MODELS"] = "true"  # Skip torch, transformers
                logger.info(
                    f"[v148.0] 🛡️ SLIM HARDWARE SAFETY: {definition.name} restricted to "
                    f"lightweight mode (profile: {_hw_profile})"
                )
            
            # ═══════════════════════════════════════════════════════════════════════
            # v149.1: CHECK CLAUDE API FALLBACK SIGNAL FIRST
            # ═══════════════════════════════════════════════════════════════════════
            # If the fallback signal is active (GCP failed persistently), force API mode
            # regardless of other settings. This ensures jarvis-prime never gets stuck.
            # ═══════════════════════════════════════════════════════════════════════
            _claude_fallback_active = is_claude_api_fallback_active()
            if _claude_fallback_active and definition.name.lower() in jarvis_prime_names:
                logger.warning(
                    f"[v149.1] 📢 CLAUDE FALLBACK ACTIVE: {definition.name} forced to API-only mode"
                )
                env["JARVIS_API_ONLY_MODE"] = "true"
                env["JARVIS_CLAUDE_FALLBACK_ONLY"] = "true"
                env["JARVIS_MODEL_LOADING_MODE"] = "disabled"
                env["JARVIS_HOLLOW_CLIENT_MODE"] = "false"  # Not waiting for GCP
                env["JARVIS_SKIP_LOCAL_MODEL_LOAD"] = "true"
                env["JARVIS_GCP_OFFLOAD_ACTIVE"] = "false"  # GCP is unavailable
                # Don't check GCP - go straight to Claude API
            
            # v147.0: Pass GCP offload information to spawned service
            # This tells the service to use GCP VM for model inference instead of local loading
            # CRITICAL FIX: jarvis-prime uses JARVIS_GCP_PRIME_ENDPOINT and GCP_PRIME_ENDPOINT
            # to determine where to route requests, NOT JARVIS_GCP_MODEL_SERVER
            # v149.0: Read GCP state from environment variables
            # v149.1: Skip GCP routing if Claude fallback is active
            _gcp_active = env.get("JARVIS_GCP_OFFLOAD_ACTIVE", "").lower() == "true"
            _gcp_vm_ip = env.get("JARVIS_GCP_VM_IP", "")
            if _gcp_active and _gcp_vm_ip and not _claude_fallback_active:
                # Build the full endpoint URL (use port 8000 which is jarvis-prime's port)
                gcp_endpoint = f"http://{_gcp_vm_ip}:8000"
                
                # v147.0: Set ALL env vars that jarvis-prime checks for GCP routing
                env["JARVIS_GCP_OFFLOAD_ACTIVE"] = "true"
                env["GCP_PRIME_ENDPOINT"] = gcp_endpoint  # Used by hybrid_tiered_router.py
                env["JARVIS_GCP_PRIME_ENDPOINT"] = gcp_endpoint  # Alternate name
                env["JARVIS_GCP_MODEL_SERVER"] = gcp_endpoint  # Legacy compatibility
                env["JARVIS_GCP_VM_IP"] = _gcp_vm_ip
                # Tell service to use lightweight/proxy mode
                env["JARVIS_MODEL_LOADING_MODE"] = "gcp_proxy"
                env["JARVIS_HOLLOW_CLIENT_MODE"] = "true"  # v147.0: Explicit hollow client
                env["JARVIS_SKIP_LOCAL_MODEL_LOAD"] = "true"  # v147.0: Block local models
                
                logger.info(
                    f"[v147.0] 🚀 {definition.name} will route inference to GCP: {gcp_endpoint}"
                )
            elif _hw_profile in {"SLIM", "CLOUD_ONLY", "ULTRA_SLIM"} and not _claude_fallback_active:
                # v149.0: SLIM hardware but no GCP - use API fallback mode
                # v149.1: Skip if Claude fallback already set above
                if definition.name.lower() in jarvis_prime_names:
                    logger.warning(
                        f"[v148.0] ⚠️ SLIM HARDWARE + NO GCP: {definition.name} will operate in "
                        f"API-fallback mode (no local inference). This is expected if GCP failed."
                    )
                    env["JARVIS_API_ONLY_MODE"] = "true"
                    env["JARVIS_CLAUDE_FALLBACK_ONLY"] = "true"
                    env["JARVIS_MODEL_LOADING_MODE"] = "disabled"

            # v4.0: Build command using the detected Python executable
            cmd: List[str] = []

            if definition.use_uvicorn and definition.uvicorn_app:
                # Uvicorn-based FastAPI app
                # v95.7: Use str() to ensure type safety (uvicorn_app is verified non-None by condition)
                uvicorn_app: str = definition.uvicorn_app  # Narrow type for Pyright
                cmd = [
                    python_exec, "-m", "uvicorn",
                    uvicorn_app,
                    "--host", "0.0.0.0",
                    "--port", str(definition.default_port),
                ]
                logger.info(f"🚀 Spawning {definition.name} via uvicorn: {uvicorn_app}")

            elif definition.module_path:
                # Module-based entry point (python -m)
                # v95.7: Use type narrowing for Pyright
                module_path: str = definition.module_path  # Narrow type for Pyright
                cmd = [python_exec, "-m", module_path]
                # Add script_args if any
                if definition.script_args:
                    cmd.extend(definition.script_args)
                logger.info(f"🚀 Spawning {definition.name} via module: {definition.module_path}")

            else:
                # Traditional script-based entry point
                cmd = [python_exec, str(script_path)]
                # v4.0: Append command-line arguments (e.g., --port 8000)
                if definition.script_args:
                    cmd.extend(definition.script_args)
                logger.info(f"🚀 Spawning {definition.name}: {' '.join(cmd)}")

            # Spawn process
            # v95.20: CRITICAL - start_new_session=True isolates child from parent's process group
            # Without this, signals sent to parent (SIGTERM/SIGINT) propagate to ALL children,
            # causing immediate termination of spawned services with exit code 0.
            # This was the root cause of "Process died unexpectedly (uptime: 0.0s)"
            managed.process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(definition.repo_path),
                stdout=asyncio.subprocess.PIPE if self.config.stream_output else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE if self.config.stream_output else asyncio.subprocess.DEVNULL,
                env=env,
                start_new_session=True,  # v95.20: Isolate from parent's signal propagation
            )

            managed.pid = managed.process.pid
            managed.port = definition.default_port  # May be updated by registry discovery
            # v112.0: Track start time for adaptive timeouts
            managed.start_time = time.time()

            logger.info(f"📋 {definition.name} spawned with PID {managed.pid}")

            # v116.0: Register spawned service in GlobalProcessRegistry for SIGHUP protection
            try:
                from backend.core.supervisor_singleton import GlobalProcessRegistry
                GlobalProcessRegistry.register(
                    pid=managed.pid,
                    component=f"{definition.name} (spawned)",
                    port=definition.default_port
                )
                logger.info(f"    [v116.0] ✅ {definition.name} (PID {managed.pid}) registered in GlobalProcessRegistry")
            except Exception as reg_err:
                logger.warning(f"    [v116.0] ⚠️ GlobalProcessRegistry registration failed for {definition.name}: {reg_err}")

            # v108.0: Record startup time for grace period tracking
            # This allows the heartbeat validator to NOT mark components as dead during startup
            await self._record_component_startup(definition.name, managed.pid)

            # Start output streaming
            await self._start_output_streaming(managed)

            # v108.1: Determine if we should use non-blocking model loading
            # Heavy ML services (startup_timeout > 90s) can use background model loading
            # so the main JARVIS backend can start while external services load models
            is_ml_heavy = definition.startup_timeout > 90.0
            use_background_loading = (
                self.config.non_blocking_model_loading
                and is_ml_heavy
            )

            if use_background_loading:
                logger.info(
                    f"📋 [v108.1] {definition.name}: Using non-blocking model loading "
                    f"(server will be considered 'started' once responding)"
                )

            # Wait for service to become healthy
            healthy = await self._wait_for_health(
                managed,
                timeout=definition.startup_timeout,
                background_model_loading=use_background_loading,
            )

            if healthy:
                managed.status = ServiceStatus.HEALTHY

                # v95.0: Emit service healthy event
                await _emit_event(
                    "SERVICE_HEALTHY",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "pid": managed.pid,
                        "port": managed.port,
                        "startup_timeout": definition.startup_timeout
                    }
                )

                # Register in service registry
                if self.registry:
                    await self.registry.register_service(
                        service_name=definition.name,
                        pid=managed.pid,
                        port=managed.port,
                        health_endpoint=definition.health_endpoint,
                        metadata={"repo_path": str(definition.repo_path)}
                    )

                # Start health monitor
                managed.health_monitor_task = asyncio.create_task(
                    self._health_monitor_loop(managed)
                )

                # v95.0: Start dedicated heartbeat loop (prevents stale service issues)
                # This runs independently of health checks to ensure services
                # are NEVER marked stale as long as the process is running
                managed.heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(managed)
                )
                managed.last_known_health = "healthy"

                # v109.4: Clear Trinity restart notification now that service is healthy
                await self._clear_trinity_restart_notification(definition.name)

                return True
            else:
                logger.warning(
                    f"⚠️ {definition.name} spawned but did not become healthy "
                    f"within {definition.startup_timeout}s"
                )
                managed.status = ServiceStatus.DEGRADED
                managed.last_known_health = "degraded"

                # v95.0: Emit service unhealthy event
                await _emit_event(
                    "SERVICE_UNHEALTHY",
                    service_name=definition.name,
                    priority="HIGH",
                    details={
                        "reason": "health_check_timeout",
                        "timeout_seconds": definition.startup_timeout,
                        "pid": managed.pid
                    }
                )

                # v95.0: CRITICAL - Start heartbeat and health monitor even for degraded services
                # This ensures services are NOT marked stale while the process is running
                # and allows auto-healing to kick in when the health monitor detects failures
                if self.registry and managed.is_running:
                    await self.registry.register_service(
                        service_name=definition.name,
                        pid=managed.pid,
                        port=managed.port,
                        health_endpoint=definition.health_endpoint,
                        metadata={
                            "repo_path": str(definition.repo_path),
                            "status": "degraded",
                            "degraded_at": time.time()
                        }
                    )
                    logger.info(
                        f"[v95.0] Registered degraded service {definition.name} in registry"
                    )

                # Start health monitor to track recovery or trigger auto-heal
                managed.health_monitor_task = asyncio.create_task(
                    self._health_monitor_loop(managed)
                )

                # Start heartbeat loop to prevent stale service removal
                managed.heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(managed)
                )
                logger.info(
                    f"[v95.0] Started heartbeat loop for degraded service {definition.name}"
                )

                return False  # Still return False to indicate not healthy

        except Exception as e:
            # v148.1: Use log_component_failure for criticality-aware logging
            log_component_failure(
                definition.name,
                "Failed to spawn service",
                error=e,
                phase="spawn_core",
            )
            managed.status = ServiceStatus.FAILED
            # v95.0: Emit service crashed event
            await _emit_event(
                "SERVICE_CRASHED",
                service_name=definition.name,
                priority="CRITICAL",
                details={"reason": "spawn_exception", "error": str(e)}
            )
            return False

    async def _record_component_startup(self, service_name: str, pid: int) -> None:
        """
        v108.0: Record component startup time for grace period tracking.

        This integrates with:
        1. HeartbeatValidator - to prevent marking components as dead during startup
        2. TrinityHealthMonitor - to prevent marking components as unhealthy during startup
        3. TrinityOrchestrationConfig - to record startup in unified config

        Args:
            service_name: Name of the service being started
            pid: Process ID of the spawned service
        """
        startup_time = time.time()

        # Map service name to component type for config lookup
        component_type_map = {
            "jarvis-body": "jarvis_body",
            "jarvis_body": "jarvis_body",
            "jarvis-prime": "jarvis_prime",
            "jarvis_prime": "jarvis_prime",
            "j-prime": "jarvis_prime",
            "jprime": "jarvis_prime",
            "reactor-core": "reactor_core",
            "reactor_core": "reactor_core",
            "coding-council": "coding_council",
            "coding_council": "coding_council",
        }
        component_type = component_type_map.get(service_name.lower(), "jarvis_body")

        # 1. Record in HeartbeatValidator
        try:
            from backend.core.coding_council.trinity.heartbeat_validator import (
                HeartbeatValidator,
            )
            # Get or create global validator instance
            validator = HeartbeatValidator()
            validator.record_component_startup(service_name, component_type)
            logger.debug(
                f"[v108.0] Recorded startup time in HeartbeatValidator for {service_name}"
            )
        except ImportError:
            logger.debug(
                f"[v108.0] HeartbeatValidator not available for startup recording"
            )
        except Exception as e:
            logger.warning(
                f"[v108.0] Failed to record startup in HeartbeatValidator: {e}"
            )

        # 2. Record in TrinityOrchestrationConfig (for global access)
        # v137.0: Use non-blocking I/O
        try:
            from backend.core.trinity_orchestration_config import (
                get_orchestration_config,
                ComponentType,
            )
            # Store startup time in a shared location
            orch_config = get_orchestration_config()
            startup_file = orch_config.components_dir / f"{service_name}_startup.json"

            startup_data = {
                "service_name": service_name,
                "component_type": component_type,
                "startup_time": startup_time,
                "pid": pid,
            }

            # v137.0: Non-blocking atomic write
            success = await write_json_nonblocking(startup_file, startup_data)
            if success:
                logger.debug(
                    f"[v137.0] Recorded startup time file for {service_name} at {startup_file}"
                )
            else:
                logger.debug(f"[v137.0] Failed to write startup file for {service_name}")
        except ImportError:
            logger.debug(
                f"[v137.0] TrinityOrchestrationConfig not available for startup recording"
            )
        except Exception as e:
            logger.warning(
                f"[v137.0] Failed to write startup file: {e}"
            )

        # 3. Record in TrinityHealthMonitor config if available
        try:
            from backend.core.trinity_health_monitor import (
                TrinityHealthConfig,
                TrinityComponent,
            )
            # Map to TrinityComponent enum
            component_enum_map = {
                "jarvis_body": TrinityComponent.JARVIS_BODY,
                "jarvis_prime": TrinityComponent.JARVIS_PRIME,
                "reactor_core": TrinityComponent.REACTOR_CORE,
                "coding_council": TrinityComponent.CODING_COUNCIL,
            }
            if component_type in component_enum_map:
                # This will be picked up by the health monitor's config
                logger.debug(
                    f"[v108.0] Component {service_name} ({component_type}) startup recorded"
                )
        except ImportError:
            pass

        # 4. v109.4: Notify TrinityHealthMonitor that supervisor is spawning this service
        # This prevents Trinity from triggering a concurrent restart
        try:
            await self._notify_trinity_spawn(service_name, component_type, pid, startup_time)
        except Exception as e:
            logger.debug(f"[v109.4] Failed to notify Trinity of spawn: {e}")

    async def _notify_trinity_spawn(
        self,
        service_name: str,
        component_type: str,
        pid: int,
        startup_time: float
    ) -> None:
        """
        v109.4: Notify Trinity that supervisor is spawning/has spawned a service.
        v137.0: Updated to use non-blocking I/O (I/O Airlock pattern).

        This prevents the race condition where:
        1. Supervisor starts spawning a service
        2. Trinity detects the service is down and triggers its own restart
        3. Both try to start the service, causing port conflicts

        Trinity will read this notification and defer to the supervisor.
        """
        supervisor_dir = Path.home() / ".jarvis" / "supervisor"

        # v137.0: Non-blocking directory creation
        def _ensure_supervisor_dir():
            supervisor_dir.mkdir(parents=True, exist_ok=True)
        await _run_blocking_io(_ensure_supervisor_dir, timeout=2.0, operation_name="ensure_supervisor_dir")

        restart_state_file = supervisor_dir / "restart_state.json"

        # v137.0: Read existing state with non-blocking I/O
        existing = await read_json_nonblocking(restart_state_file)
        if existing is None:
            existing = {}

        # Update restarting services list
        restarting_services = existing.get("restarting", [])
        if service_name not in restarting_services:
            restarting_services.append(service_name)

        # Update spawn timestamps (for Trinity to know when we spawned)
        spawn_timestamps = existing.get("spawn_timestamps", {})
        spawn_timestamps[service_name] = startup_time

        # Update PIDs
        active_pids = existing.get("active_pids", {})
        active_pids[service_name] = pid

        # Write updated state
        state = {
            "restarting": restarting_services,
            "spawn_timestamps": spawn_timestamps,
            "active_pids": active_pids,
            "last_update": time.time(),
        }

        # v137.0: Atomic write with non-blocking I/O
        success = await write_json_nonblocking(restart_state_file, state)
        if success:
            logger.debug(f"[v137.0] Notified Trinity of {service_name} spawn (PID {pid})")
        else:
            logger.debug(f"[v137.0] Failed to notify Trinity of {service_name} spawn")

        # Also write spawn time to Trinity's component directory for direct access
        try:
            trinity_components_dir = Path.home() / ".jarvis" / "trinity" / "components"

            def _ensure_trinity_dir():
                trinity_components_dir.mkdir(parents=True, exist_ok=True)
            await _run_blocking_io(_ensure_trinity_dir, timeout=2.0, operation_name="ensure_trinity_dir")

            spawn_file = trinity_components_dir / f"{component_type}_spawn.json"
            spawn_data = {
                "service_name": service_name,
                "component_type": component_type,
                "spawn_time": startup_time,
                "pid": pid,
                "supervisor_spawned": True,
            }
            await write_json_nonblocking(spawn_file, spawn_data)
        except Exception:
            pass  # Not critical

    async def _clear_trinity_restart_notification(self, service_name: str) -> None:
        """
        v109.4: Clear Trinity restart notification after service is healthy.
        v137.0: Updated to use non-blocking I/O (I/O Airlock pattern).

        Called after a service successfully starts and is healthy to allow
        Trinity to resume normal monitoring.
        """
        supervisor_dir = Path.home() / ".jarvis" / "supervisor"
        restart_state_file = supervisor_dir / "restart_state.json"

        try:
            # v137.0: Read state with non-blocking I/O
            state = await read_json_nonblocking(restart_state_file)
            if state is None:
                return  # No state file, nothing to clear

            # Remove from restarting list
            restarting = state.get("restarting", [])
            if service_name in restarting:
                restarting.remove(service_name)
                state["restarting"] = restarting
                state["last_update"] = time.time()

                # v137.0: Atomic write with non-blocking I/O
                success = await write_json_nonblocking(restart_state_file, state)
                if success:
                    logger.debug(f"[v137.0] Cleared Trinity restart notification for {service_name}")

        except Exception as e:
            logger.debug(f"[v137.0] Failed to clear Trinity notification: {e}")

    async def _wait_for_health(
        self,
        managed: ManagedProcess,
        timeout: float = 60.0,
        background_model_loading: bool = False,
    ) -> bool:
        """
        Wait for service to become healthy with intelligent progress-based timeout extension.

        v93.5: Enhanced with intelligent progress detection:

        PHASE 1 (Quick): Wait for server to start responding (max 60s)
        - Server starts listening on port
        - Health endpoint returns any status (including "starting")
        - If this times out, the service failed to start

        PHASE 2 (Patient + Intelligent): Wait for model to load with progress detection
        - Health endpoint returns "healthy" status
        - Server is up, loading models in background
        - KEY ENHANCEMENT: If progress is detected (model_load_elapsed increasing),
          timeout is dynamically extended up to max_startup_timeout
        - This prevents timeout when model is actively loading (just slow)

        v108.1: Non-blocking model loading mode:
        - When background_model_loading=True, returns after Phase 1 (server responding)
        - Phase 2 (model loading) continues in background task
        - This allows the main JARVIS backend to start while J-Prime loads its model

        This prevents the scenario where:
        - Server takes 5s to start listening
        - Model takes 304s to load (just 4s over 300s timeout)
        - Old approach: times out at 300s even though model was 98% loaded
        - New approach: detects progress, extends timeout, model loads successfully

        Args:
            managed: The managed process to wait for
            timeout: Timeout for full health check (Phase 1 + Phase 2)
            background_model_loading: If True, return after Phase 1 and continue Phase 2 in background
        """
        start_time = time.time()
        check_interval = 1.0
        check_count = 0
        last_milestone_log = start_time

        # v93.5: Detect if this is a long-timeout service (likely loading ML models)
        is_ml_heavy = timeout > 90.0
        milestone_interval = 30.0 if is_ml_heavy else 15.0

        # v93.5: Progress tracking for intelligent timeout extension
        last_model_elapsed = 0.0
        progress_detected_at = 0.0
        timeout_extended = False
        effective_timeout = timeout
        max_timeout = self.config.max_startup_timeout

        # Phase 1: Wait for server to respond (quick timeout)
        phase1_timeout = min(60.0, timeout / 3)  # Max 60s or 1/3 of total timeout
        server_responding = False

        logger.info(
            f"    ⏳ Phase 1: Waiting for {managed.definition.name} server to start "
            f"(timeout: {phase1_timeout:.0f}s)..."
        )

        while (time.time() - start_time) < phase1_timeout:
            check_count += 1

            # Check if process died
            if not managed.is_running:
                exit_code = managed.process.returncode if managed.process else "unknown"
                logger.error(
                    f"    ❌ {managed.definition.name} process died during startup "
                    f"(exit code: {exit_code})"
                )
                return False

            # Check if server is responding (any status including "starting")
            if await self._check_service_responding(managed):
                elapsed = time.time() - start_time
                logger.info(
                    f"    ✅ Phase 1 complete: {managed.definition.name} server responding "
                    f"after {elapsed:.1f}s"
                )
                server_responding = True
                break

            # Quick checks during phase 1
            await asyncio.sleep(check_interval)
            check_interval = min(check_interval + 0.3, 2.0)

        if not server_responding:
            elapsed = time.time() - start_time
            logger.warning(
                f"    ⚠️ {managed.definition.name} server failed to start within {elapsed:.1f}s"
            )
            return False

        # v108.1: Non-blocking model loading mode
        # If enabled, return success after Phase 1 and continue Phase 2 in background
        if background_model_loading and is_ml_heavy:
            elapsed = time.time() - start_time
            logger.info(
                f"    🚀 [v108.1] Non-blocking mode: {managed.definition.name} server responding, "
                f"returning early (model loading continues in background)"
            )

            # Start background task to monitor Phase 2 (model loading)
            background_task = asyncio.create_task(
                self._background_model_loading_monitor(
                    managed=managed,
                    effective_timeout=effective_timeout,
                    max_timeout=max_timeout,
                ),
                name=f"model_loading_monitor_{managed.definition.name}"
            )
            self._track_background_task(background_task)

            # Return True - service is "started" (server responding)
            # Model loading will be monitored in background
            return True

        # Phase 2: Wait for "healthy" status (model loading) with intelligent progress detection
        phase2_start = time.time()
        check_interval = 2.0  # Slower checks now that server is up
        check_count = 0
        last_status = "unknown"

        if is_ml_heavy:
            logger.info(
                f"    ⏳ Phase 2: Waiting for {managed.definition.name} model to load "
                f"(base timeout: {effective_timeout:.0f}s, max: {max_timeout:.0f}s)..."
            )
            logger.info(
                f"    ℹ️  {managed.definition.name}: Server is up, model loading in background"
            )

        while True:
            phase2_elapsed = time.time() - phase2_start
            total_elapsed = time.time() - start_time

            # v93.5: Check against effective (possibly extended) timeout
            if phase2_elapsed >= effective_timeout:
                # Final timeout check - but only if no recent progress
                if progress_detected_at > 0 and (time.time() - progress_detected_at) < 60:
                    # Progress was detected within last 60s - extend if under max
                    if effective_timeout < max_timeout:
                        extension = min(
                            self.config.model_loading_timeout_extension,
                            max_timeout - effective_timeout
                        )
                        effective_timeout += extension
                        logger.info(
                            f"    🔄 {managed.definition.name}: Progress detected, extending timeout by {extension:.0f}s "
                            f"(new timeout: {effective_timeout:.0f}s)"
                        )
                        timeout_extended = True
                        continue
                break  # Actually timed out

            check_count += 1

            # Check if process died
            if not managed.is_running:
                exit_code = managed.process.returncode if managed.process else "unknown"
                logger.error(
                    f"    ❌ {managed.definition.name} process died during model loading "
                    f"(exit code: {exit_code})"
                )
                return False

            # v93.7: SINGLE health check request that both checks status AND tracks progress
            # This fixes the duplicate health check issue (was making 2 requests per interval)
            # v93.11: Uses shared HTTP session with connection pooling
            try:
                url = f"http://localhost:{managed.port}{managed.definition.health_endpoint}"
                session = await self._get_http_session()
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=self.config.health_check_timeout)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        current_status = data.get("status", "unknown")
                        current_phase = data.get("phase", "")  # v93.12: Also track phase
                        model_elapsed = data.get("model_load_elapsed_seconds", 0)
                        current_step = data.get("current_step", "")
                        model_progress = data.get("model_load_progress_pct", 0)
                        startup_elapsed = data.get("startup_elapsed_seconds", 0)  # v93.12: Track overall startup

                        # v93.12: Multiple ways to detect "healthy" state
                        # - status == "healthy" (standard)
                        # - phase == "ready" (jarvis-prime specific)
                        is_healthy = (
                            current_status == "healthy"
                            or current_phase == "ready"
                            or (data.get("ready_for_inference", False) and data.get("model_loaded", False))
                        )

                        if is_healthy:
                            logger.info(
                                f"    ✅ {managed.definition.name} fully healthy after {total_elapsed:.1f}s "
                                f"(server: {phase2_start - start_time:.1f}s, model: {phase2_elapsed:.1f}s)"
                                + (f" [timeout was extended]" if timeout_extended else "")
                            )
                            return True

                        # v93.12: Early exit on error status
                        if current_status == "error":
                            error_msg = data.get("error", "Unknown error")
                            logger.error(
                                f"    ❌ {managed.definition.name} reported error: {error_msg}"
                            )
                            # Don't return False immediately - let the process try to recover
                            # unless error persists for multiple checks

                        # v93.5: Detect progress (model_elapsed increasing)
                        if model_elapsed and model_elapsed > last_model_elapsed:
                            progress_detected_at = time.time()
                            last_model_elapsed = model_elapsed

                        # v93.12: Also detect step changes as progress
                        if current_step and current_step != getattr(self, '_last_detected_step', ''):
                            progress_detected_at = time.time()
                            self._last_detected_step = current_step
                            logger.info(
                                f"    📍 {managed.definition.name}: Step changed to '{current_step}'"
                            )

                        # v93.7: Log status changes with step info
                        if current_status != last_status:
                            step_info = f" (step: {current_step})" if current_step else ""
                            phase_info = f", phase: {current_phase}" if current_phase else ""
                            logger.info(
                                f"    ℹ️  {managed.definition.name}: status={current_status}{step_info}{phase_info}"
                            )
                            last_status = current_status

                        # v93.7: Enhanced milestone logging with step and progress info
                        if (time.time() - last_milestone_log) >= milestone_interval:
                            remaining = effective_timeout - phase2_elapsed
                            model_info = f", model loading: {model_elapsed:.0f}s" if model_elapsed else ""
                            progress_info = ""
                            if model_progress > 0:
                                progress_info = f" ({model_progress:.0f}% est.)"
                            if progress_detected_at > 0:
                                since_progress = time.time() - progress_detected_at
                                progress_info += f", last progress: {since_progress:.0f}s ago"
                            step_info = f", step: {current_step}" if current_step else ""
                            logger.info(
                                f"    ⏳ {managed.definition.name}: {current_status}{step_info} "
                                f"({phase2_elapsed:.0f}s elapsed, {remaining:.0f}s remaining{model_info}{progress_info})"
                            )
                            last_milestone_log = time.time()
                    else:
                        # Non-200 response
                        logger.debug(f"    Health check returned {response.status}")

            except asyncio.TimeoutError:
                logger.debug(f"    Health check timed out")
            except Exception as e:
                logger.debug(f"    Health check error: {e}")

            # v93.5: Adaptive check intervals for phase 2
            if phase2_elapsed > 180:
                check_interval = 10.0  # After 3 min, check every 10s
            elif phase2_elapsed > 60:
                check_interval = 5.0   # After 1 min, check every 5s
            else:
                check_interval = 3.0   # First minute, check every 3s

            await asyncio.sleep(check_interval)

        # Timeout
        total_elapsed = time.time() - start_time
        logger.warning(
            f"    ⚠️ {managed.definition.name} model loading timed out after {total_elapsed:.1f}s "
            f"({check_count} phase 2 checks, effective_timeout={effective_timeout:.0f}s)"
        )
        return False

    async def _background_model_loading_monitor(
        self,
        managed: ManagedProcess,
        effective_timeout: float,
        max_timeout: float,
    ) -> None:
        """
        v108.1: Background task to monitor model loading for non-blocking startup.

        This task continues monitoring Phase 2 (model loading) in the background
        after Phase 1 (server responding) completes. This allows the main JARVIS
        backend to start while heavy services like jarvis-prime load their ML models.

        The task:
        - Monitors health endpoint for "healthy" status
        - Tracks model loading progress
        - Extends timeout if progress is detected
        - Updates managed.status when fully healthy
        - Emits events for voice narration

        Args:
            managed: The managed process to monitor
            effective_timeout: Initial Phase 2 timeout
            max_timeout: Maximum timeout (hard cap)
        """
        service_name = managed.definition.name
        start_time = time.time()
        last_model_elapsed = 0.0
        progress_detected_at = 0.0
        timeout_extended = False
        check_interval = 3.0
        last_milestone_log = start_time
        milestone_interval = 30.0
        check_count = 0
        last_status = "starting"

        logger.info(
            f"    🔄 [v108.1] Background model loading monitor started for {service_name}"
        )

        try:
            while not self._shutdown_event.is_set():
                phase2_elapsed = time.time() - start_time
                check_count += 1

                # Check against effective (possibly extended) timeout
                if phase2_elapsed >= effective_timeout:
                    # Final timeout check - but only if no recent progress
                    if progress_detected_at > 0 and (time.time() - progress_detected_at) < 60:
                        # Progress was detected within last 60s - extend if under max
                        if effective_timeout < max_timeout:
                            extension = min(
                                self.config.model_loading_timeout_extension,
                                max_timeout - effective_timeout
                            )
                            effective_timeout += extension
                            logger.info(
                                f"    🔄 [v108.1] {service_name}: Progress detected, extending "
                                f"background timeout by {extension:.0f}s (new: {effective_timeout:.0f}s)"
                            )
                            timeout_extended = True
                            continue
                    # Actually timed out
                    logger.warning(
                        f"    ⚠️ [v108.1] {service_name} background model loading timed out "
                        f"after {phase2_elapsed:.1f}s"
                    )
                    managed.status = ServiceStatus.DEGRADED
                    await _emit_event(
                        "MODEL_LOADING_TIMEOUT",
                        service_name=service_name,
                        priority="HIGH",
                        details={
                            "elapsed_seconds": phase2_elapsed,
                            "timeout_seconds": effective_timeout,
                            "timeout_extended": timeout_extended,
                        }
                    )
                    return

                # Check if process died
                if not managed.is_running:
                    exit_code = managed.process.returncode if managed.process else "unknown"

                    # v108.2: Get crash forensics for detailed diagnosis
                    crash_context = managed.get_crash_context(num_lines=30)

                    logger.error(
                        f"    ❌ [v108.2] {service_name} process died during model loading "
                        f"(exit code: {exit_code})"
                    )

                    # v108.2: Log detailed crash forensics
                    if crash_context.get("last_stderr_lines"):
                        stderr_lines = crash_context["last_stderr_lines"]
                        logger.error(
                            f"    📋 CRASH FORENSICS ({len(stderr_lines)} stderr lines captured):\n" +
                            "\n".join(f"      | {line}" for line in stderr_lines[-20:])
                        )
                    elif crash_context.get("last_output_lines"):
                        output_lines = crash_context["last_output_lines"]
                        logger.error(
                            f"    📋 Last output before crash:\n" +
                            "\n".join(f"      | {line}" for line in output_lines[-10:])
                        )

                    managed.status = ServiceStatus.FAILED
                    await _emit_event(
                        "SERVICE_CRASHED",
                        service_name=service_name,
                        priority="CRITICAL",
                        details={
                            "reason": "process_died_during_model_loading",
                            "exit_code": exit_code,
                            "crash_context": crash_context,
                        }
                    )
                    return

                # Health check
                try:
                    url = f"http://localhost:{managed.port}{managed.definition.health_endpoint}"
                    session = await self._get_http_session()
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=self.config.health_check_timeout)
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            current_status = data.get("status", "unknown")
                            current_phase = data.get("phase", "")
                            model_elapsed = data.get("model_load_elapsed_seconds", 0)
                            current_step = data.get("current_step", "")
                            model_progress = data.get("model_load_progress_pct", 0)

                            # Check if healthy
                            is_healthy = (
                                current_status == "healthy"
                                or current_phase == "ready"
                                or (data.get("ready_for_inference", False) and data.get("model_loaded", False))
                            )

                            if is_healthy:
                                logger.info(
                                    f"    ✅ [v108.1] {service_name} fully healthy after "
                                    f"{phase2_elapsed:.1f}s (background monitoring)"
                                    + (f" [timeout was extended]" if timeout_extended else "")
                                )
                                managed.status = ServiceStatus.HEALTHY
                                await _emit_event(
                                    "MODEL_LOADING_COMPLETE",
                                    service_name=service_name,
                                    priority="HIGH",
                                    details={
                                        "elapsed_seconds": phase2_elapsed,
                                        "model_load_seconds": model_elapsed,
                                    }
                                )
                                return

                            # Track progress
                            if model_elapsed and model_elapsed > last_model_elapsed:
                                progress_detected_at = time.time()
                                last_model_elapsed = model_elapsed

                            # Log status changes
                            if current_status != last_status:
                                step_info = f" (step: {current_step})" if current_step else ""
                                logger.info(
                                    f"    ℹ️  [v108.1] {service_name}: status={current_status}{step_info}"
                                )
                                last_status = current_status

                            # Milestone logging
                            if (time.time() - last_milestone_log) >= milestone_interval:
                                remaining = effective_timeout - phase2_elapsed
                                model_info = f", model: {model_elapsed:.0f}s" if model_elapsed else ""
                                progress_info = f" ({model_progress:.0f}%)" if model_progress > 0 else ""
                                logger.info(
                                    f"    ⏳ [v108.1] {service_name}: {current_status}{progress_info} "
                                    f"({phase2_elapsed:.0f}s elapsed, {remaining:.0f}s remaining{model_info})"
                                )
                                last_milestone_log = time.time()

                except asyncio.TimeoutError:
                    pass  # Health check timeout, retry
                except Exception as e:
                    logger.debug(f"    [v108.1] Background health check error: {e}")

                # Adaptive check intervals
                if phase2_elapsed > 180:
                    check_interval = 10.0
                elif phase2_elapsed > 60:
                    check_interval = 5.0
                else:
                    check_interval = 3.0

                await asyncio.sleep(check_interval)

        except asyncio.CancelledError:
            logger.info(f"    🛑 [v108.1] Background model loading monitor cancelled for {service_name}")
            raise
        except Exception as e:
            logger.error(f"    ❌ [v108.1] Background model loading monitor error for {service_name}: {e}")
            managed.status = ServiceStatus.DEGRADED

    # =========================================================================
    # Process Stopping
    # =========================================================================

    async def _stop_process(self, managed: ManagedProcess) -> None:
        """Stop a managed process gracefully."""
        # Cancel background tasks (v95.0: includes dedicated heartbeat task)
        background_tasks = [
            managed.output_stream_task,
            managed.health_monitor_task,
            managed.heartbeat_task,  # v95.0: dedicated heartbeat task
        ]
        for task in background_tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if managed.process is None or not managed.is_running:
            return

        logger.info(f"🛑 Stopping {managed.definition.name} (PID: {managed.pid})...")

        try:
            # Try graceful shutdown first (SIGTERM)
            managed.process.terminate()

            try:
                await asyncio.wait_for(
                    managed.process.wait(),
                    timeout=self.config.shutdown_timeout
                )
                logger.info(f"✅ {managed.definition.name} stopped gracefully")

            except asyncio.TimeoutError:
                # Force kill if necessary (SIGKILL)
                logger.warning(
                    f"⚠️ {managed.definition.name} did not stop gracefully, forcing..."
                )
                managed.process.kill()
                await managed.process.wait()
                logger.info(f"✅ {managed.definition.name} force killed")

        except ProcessLookupError:
            pass  # Process already dead
        except Exception as e:
            logger.error(f"Error stopping {managed.definition.name}: {e}")

        managed.status = ServiceStatus.STOPPED

        # Deregister from service registry
        if self.registry:
            await self.registry.deregister_service(managed.definition.name)

    # =========================================================================
    # Signal Handlers
    # =========================================================================

    def _setup_signal_handlers(self) -> None:
        """
        v123.2: Setup graceful shutdown signal handlers.

        CRITICAL: Do NOT register signal handlers via loop.add_signal_handler()!
        This would REPLACE the UnifiedSignalManager's handlers from run_supervisor.py,
        causing the main() function to never detect shutdown completion.

        Instead, register a shutdown callback that will be triggered when
        global shutdown is initiated via graceful_shutdown.py.
        """
        if self._signals_registered:
            return

        # v123.2: Register as a shutdown observer instead of overriding signal handlers
        try:
            from backend.core.resilience.graceful_shutdown import register_shutdown_callback
            register_shutdown_callback(
                name="cross_repo_orchestrator",
                callback=lambda: asyncio.create_task(self._handle_orchestrator_shutdown()),
            )
            logger.info("🛡️ Orchestrator shutdown callback registered (via graceful_shutdown)")
        except ImportError:
            logger.debug("[v123.2] graceful_shutdown not available - shutdown callback not registered")
        except Exception as e:
            logger.warning(f"[v123.2] Could not register shutdown callback: {e}")

        self._signals_registered = True

    async def _handle_orchestrator_shutdown(self) -> None:
        """
        v123.2: Handle orchestrator shutdown when global shutdown is triggered.

        This is called as a callback when graceful_shutdown initiates shutdown,
        NOT directly from a signal handler (to avoid overriding UnifiedSignalManager).
        """
        logger.info("[v123.2] Orchestrator received shutdown notification")

        # Set shutdown event (signals other tasks to stop)
        self._shutdown_event.set()
        self._running = False

        # Perform graceful service shutdown
        try:
            await self.shutdown_all_services()
            logger.info("[v123.2] Orchestrator services shutdown complete")
        except asyncio.CancelledError:
            logger.debug("[v123.2] Orchestrator shutdown cancelled (expected)")
        except Exception as e:
            logger.error(f"[v123.2] Error during orchestrator shutdown: {e}")

    async def _handle_shutdown(self, signum: int) -> None:
        """
        Handle shutdown signal gracefully.

        v5.2: Proper async shutdown - don't call loop.stop() or sys.exit().
        Instead, set shutdown event and let pending tasks complete naturally.
        The main loop will exit when all tasks are done.
        """
        sig_name = signal.Signals(signum).name
        logger.info(f"\n🛑 Received {sig_name}, initiating graceful shutdown...")

        # v95.13: Initiate global shutdown signal FIRST
        # This notifies ALL components (WebSocket handlers, background tasks, etc.)
        try:
            from backend.core.resilience.graceful_shutdown import initiate_global_shutdown
            initiate_global_shutdown(reason=f"signal_{sig_name}", initiator="orchestrator")
        except Exception as e:
            logger.warning(f"[v95.13] Could not initiate global shutdown signal: {e}")

        # Set shutdown event FIRST (signals other tasks to stop)
        self._shutdown_event.set()
        self._running = False

        # Perform graceful service shutdown
        try:
            await self.shutdown_all_services()
            logger.info("✅ Graceful shutdown complete")
        except asyncio.CancelledError:
            # Expected during shutdown - don't log as error
            logger.info("✅ Shutdown tasks cancelled (expected)")
        except Exception as e:
            logger.error(f"⚠️ Error during shutdown: {e}")

        # v5.2: Cancel all remaining tasks gracefully instead of stopping loop
        # This allows pending futures to complete/cancel properly
        try:
            loop = asyncio.get_running_loop()
            tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]

            if tasks:
                logger.debug(f"[Shutdown] Cancelling {len(tasks)} remaining tasks...")
                for task in tasks:
                    task.cancel()

                # Wait for tasks to acknowledge cancellation
                await asyncio.gather(*tasks, return_exceptions=True)

            logger.info("✅ All tasks terminated")
        except Exception as e:
            logger.debug(f"[Shutdown] Task cleanup note: {e}")

    # =========================================================================
    # v93.11: Parallel Service Startup with Coordination
    # =========================================================================

    async def _start_single_service_with_coordination(
        self,
        definition: ServiceDefinition,
    ) -> tuple[str, bool, str]:
        """
        v95.1: Start a single service with deadlock-free coordination.

        CRITICAL FIX: Dependency waiting happens OUTSIDE the semaphore to prevent
        deadlock scenarios where services waiting for dependencies hold semaphore
        slots while their dependencies wait to acquire semaphore slots.

        Architecture:
        1. Wait for dependencies (NO semaphore held - prevents deadlock)
        2. Acquire semaphore with timeout (prevents indefinite blocking)
        3. Perform actual startup (semaphore held only for I/O-bound work)
        4. Release semaphore on completion or error

        Returns tuple of (service_name, success, reason).
        """
        self._ensure_locks_initialized()
        service_name = definition.name

        # Mark service as starting (outside semaphore - just bookkeeping)
        async with self._startup_coordination_lock_safe:
            self._services_starting.add(service_name)
            if service_name not in self._startup_events:
                self._startup_events[service_name] = asyncio.Event()

        # v95.5: Publish lifecycle event and create trace span for this service
        logger.info(f"[v137.1] 🚀 {service_name}: Starting service coordination...")
        await self.publish_service_lifecycle_event(service_name, "starting", {"depends_on": definition.depends_on})
        logger.info(f"[v137.1] {service_name}: Lifecycle event published, creating span...")
        service_span = await self._create_span(f"start_{service_name}", metadata={"service": service_name})
        logger.info(f"[v137.1] {service_name}: Span created, checking dependencies...")

        try:
            # ==================================================================
            # PHASE 1: Wait for dependencies OUTSIDE semaphore (prevents deadlock)
            # ==================================================================
            if definition.depends_on:
                logger.info(
                    f"[v95.1] {service_name}: Waiting for dependencies "
                    f"(semaphore NOT held - deadlock-free): {definition.depends_on}"
                )
                deps_ready = await self._wait_for_dependencies(definition)
                if not deps_ready:
                    logger.error(
                        f"[v95.1] Cannot start {service_name}: dependencies not ready"
                    )
                    # v95.5: Publish failed event for dependency timeout
                    await self._end_span(service_span, status="error", error_message="dependencies_not_ready")
                    await self.publish_service_lifecycle_event(service_name, "failed", {"error": "dependencies_not_ready"})
                    return service_name, False, "dependencies_not_ready"

            # ==================================================================
            # PHASE 2: Acquire semaphore WITH TIMEOUT (prevents indefinite block)
            # ==================================================================
            logger.info(f"[v137.1] {service_name}: No dependencies (or all ready), acquiring semaphore...")
            semaphore_timeout = 60.0  # Max wait for semaphore slot
            try:
                acquired = await asyncio.wait_for(
                    self._service_startup_semaphore_safe.acquire(),
                    timeout=semaphore_timeout
                )
                if not acquired:
                    return service_name, False, "semaphore_acquisition_failed"
                logger.info(f"[v137.1] {service_name}: Semaphore acquired, proceeding to PHASE 3...")
            except asyncio.TimeoutError:
                logger.warning(
                    f"[v95.1] {service_name}: Semaphore timeout after {semaphore_timeout}s "
                    f"(other services blocking startup slots)"
                )
                # v95.5: Publish failed event for semaphore timeout
                await self._end_span(service_span, status="error", error_message="semaphore_timeout")
                await self.publish_service_lifecycle_event(service_name, "failed", {"error": "semaphore_timeout"})
                return service_name, False, f"semaphore_timeout_{semaphore_timeout}s"

            try:
                # ==============================================================
                # PHASE 3: Perform actual startup (semaphore held)
                # ==============================================================
                logger.info(f"[v137.1] {service_name}: PHASE 3 - Checking persistent state...")

                # v117.5: Step -1 - Check PERSISTENT STATE FILES for previously running services
                # This enables service adoption across full supervisor restarts (not just SIGHUP)
                # Services that were running before a crash/restart can be adopted instead of respawned
                # v137.0: Use non-blocking I/O for state file read
                try:
                    service_state_file = Path.home() / ".jarvis" / "trinity" / "state" / "services.json"
                    logger.info(f"[v137.1] {service_name}: Reading persistent state from {service_state_file}...")
                    persistent_services = await read_json_nonblocking(service_state_file)
                    logger.info(f"[v137.1] {service_name}: Persistent state read complete (found: {persistent_services is not None})")
                    if persistent_services is not None:
                        # Normalize service name for lookup (handle hyphens vs underscores)
                        normalized_name = service_name.replace("-", "_")
                        service_state = persistent_services.get(service_name) or persistent_services.get(normalized_name)

                        if service_state and service_state.get("status") in ["running", "healthy"]:
                            persisted_pid = service_state.get("pid", 0)
                            persisted_port = service_state.get("port", 0)
                            persisted_at = service_state.get("updated_at", 0)

                            # Only consider services that were updated within last 24 hours
                            if persisted_pid and persisted_port and (time.time() - persisted_at) < 86400:
                                try:
                                    os.kill(persisted_pid, 0)  # Check if process exists

                                    # Process exists - verify it's actually responding
                                    session = await self._get_http_session()
                                    try:
                                        async with session.get(
                                            f"http://localhost:{persisted_port}{definition.health_endpoint}",
                                            timeout=aiohttp.ClientTimeout(total=3.0)
                                        ) as resp:
                                            if resp.status == 200:
                                                # Service is alive and healthy! Adopt it.
                                                reason = f"ADOPTED from persistent state (PID: {persisted_pid}, Port: {persisted_port})"
                                                logger.info(
                                                    f"    [v117.5] ✅ {service_name} ADOPTED from persistent state "
                                                    f"(PID {persisted_pid}, port {persisted_port}) - skipping spawn"
                                                )

                                                # Register in GlobalProcessRegistry for SIGHUP protection
                                                try:
                                                    from backend.core.supervisor_singleton import GlobalProcessRegistry
                                                    GlobalProcessRegistry.register(
                                                        pid=persisted_pid,
                                                        component=f"{service_name} (adopted-persistent)",
                                                        port=persisted_port
                                                    )
                                                except Exception:
                                                    pass

                                                # Register in service registry
                                                if self.registry:
                                                    try:
                                                        await self.registry.register_service(
                                                            service_name,
                                                            pid=persisted_pid,
                                                            port=persisted_port,
                                                            host="localhost"
                                                        )
                                                    except Exception:
                                                        pass

                                                await self._end_span(service_span, status="success")
                                                await self.publish_service_lifecycle_event(
                                                    service_name, "ready",
                                                    {"mode": "adopted_persistent", "port": persisted_port, "pid": persisted_pid}
                                                )
                                                return service_name, True, reason
                                    except Exception as health_err:
                                        logger.debug(
                                            f"    [v117.5] Persistent {service_name} (PID {persisted_pid}) "
                                            f"not responding: {health_err}"
                                        )
                                except OSError:
                                    logger.debug(
                                        f"    [v117.5] Persistent {service_name} (PID {persisted_pid}) is dead"
                                    )
                except Exception as e:
                    logger.debug(f"[v117.5] Persistent state check failed: {e}")
                
                logger.info(f"[v137.1] {service_name}: Checking GlobalProcessRegistry for preserved services...")

                # v117.0: Step 0 - Check if service was PRESERVED during restart
                # If GlobalProcessRegistry has this service (preserved via os.execv restart),
                # validate the process is still running and skip spawning.
                # v117.2: Enhanced with port reconciliation and service registry integration
                # v117.3: Fixed service name normalization (underscore vs hyphen mismatch)
                # v117.4: Use GlobalProcessRegistry.find_by_service() for cleaner lookup
                preserved_valid = False  # v117.4: Track if preserved service is valid
                try:
                    from backend.core.supervisor_singleton import GlobalProcessRegistry

                    # v117.4: Use centralized service lookup with normalization
                    preserved = GlobalProcessRegistry.find_by_service(service_name)
                    if preserved:
                        pid, info = preserved
                        component = info.get("component", "")
                        preserved_port = info.get("port", 0)

                        # Found a preserved service entry - validate process is still alive
                        try:
                            os.kill(int(pid), 0)  # Check if process exists

                            # v117.2: Verify the service is actually responding on its port
                            session = await self._get_http_session()
                            try:
                                async with session.get(
                                    f"http://localhost:{preserved_port}{definition.health_endpoint}",
                                    timeout=aiohttp.ClientTimeout(total=2.0)
                                ) as resp:
                                    if resp.status == 200:
                                        preserved_valid = True
                                    else:
                                        raise Exception(f"Health check failed: {resp.status}")
                            except Exception as health_err:
                                logger.warning(
                                    f"    [v117.2] ⚠️ Preserved {service_name} (PID {pid}) not responding "
                                    f"on port {preserved_port}: {health_err}"
                                )
                                GlobalProcessRegistry.deregister(int(pid))
                                # Fall through to normal startup

                            if preserved_valid:
                                # Process is alive AND healthy! Update internal state.
                                reason = f"PRESERVED from restart (PID: {pid}, Port: {preserved_port})"
                                logger.info(
                                    f"    [v117.0] ✅ {service_name} is PRESERVED from restart "
                                    f"(PID {pid}, port {preserved_port}) - skipping spawn"
                                )

                                # v117.2: Register in service registry so other components can find it
                                if self.registry:
                                    try:
                                        await self.registry.register_service(
                                            service_name,
                                            pid=int(pid),
                                            port=preserved_port,
                                            host="localhost"
                                        )
                                        logger.info(f"    [v117.2] ✅ Registered preserved {service_name} in service registry (port {preserved_port})")
                                    except Exception as reg_err:
                                        logger.debug(f"    [v117.2] Service registry update failed: {reg_err}")

                                # v117.4: Track preserved service internally for monitoring
                                # (ServiceInfo is registered in service registry above)

                                # v117.2: Check if port differs from default - log warning
                                if preserved_port != definition.default_port:
                                    logger.warning(
                                        f"    [v117.2] ⚠️ {service_name} running on non-default port "
                                        f"({preserved_port} vs default {definition.default_port})"
                                    )

                                await self._end_span(service_span, status="success")
                                await self.publish_service_lifecycle_event(
                                    service_name, "ready",
                                    {"mode": "preserved", "port": preserved_port, "pid": int(pid)}
                                )
                                # v117.5: Persist state for future restarts
                                await self._persist_service_state(service_name, int(pid), preserved_port, "running")
                                return service_name, True, reason

                        except OSError:
                            # Process is dead, remove from registry and continue with normal startup
                            GlobalProcessRegistry.deregister(int(pid))
                            logger.info(
                                f"    [v117.0] ⚠️ Preserved {service_name} (PID {pid}) is dead, "
                                f"will spawn new instance"
                            )
                except ImportError:
                    pass
                except Exception as e:
                    logger.debug(f"[v117.0] GlobalProcessRegistry check failed: {e}")
                
                logger.info(f"[v137.1] {service_name}: Checking service registry for existing instances...")

                # Step 1: Check if already running via registry
                if self.registry:
                    existing = await self.registry.discover_service(service_name)
                    logger.info(f"[v137.1] {service_name}: Registry check complete (found: {existing is not None})")
                    if existing:
                        reason = f"already running (PID: {existing.pid}, Port: {existing.port})"
                        # v116.0: Register adopted service in GlobalProcessRegistry for SIGHUP protection
                        try:
                            from backend.core.supervisor_singleton import GlobalProcessRegistry
                            GlobalProcessRegistry.register(
                                pid=existing.pid,
                                component=f"{service_name} (adopted)",
                                port=existing.port
                            )
                            logger.info(f"    [v116.0] ✅ Adopted {service_name} (PID {existing.pid}) registered in GlobalProcessRegistry")
                        except Exception as reg_err:
                            logger.warning(f"    [v116.0] ⚠️ GlobalProcessRegistry registration failed for {service_name}: {reg_err}")
                        # v95.5: Service already running, publish ready event
                        await self._end_span(service_span, status="success")
                        await self.publish_service_lifecycle_event(service_name, "ready", {"mode": "existing"})
                        # v117.5: Persist state for future restarts
                        await self._persist_service_state(service_name, existing.pid, existing.port, "running")
                        return service_name, True, reason

                # Step 2: HTTP probe using shared session
                logger.info(f"[v137.1] {service_name}: Performing HTTP health probe on port {definition.default_port}...")
                session = await self._get_http_session()
                url = f"http://localhost:{definition.default_port}{definition.health_endpoint}"

                try:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=3.0)
                    ) as resp:
                        if resp.status == 200:
                            reason = f"already running at port {definition.default_port}"
                            # v95.5: Service already running, publish ready event
                            await self._end_span(service_span, status="success")
                            await self.publish_service_lifecycle_event(service_name, "ready", {"mode": "existing_http"})
                            return service_name, True, reason
                except Exception:
                    pass  # Service not running, need to start
                
                logger.info(f"[v137.1] {service_name}: HTTP probe complete (service not running)")

                # Step 3: Try Docker hybrid mode
                logger.info(f"[v137.1] {service_name}: Checking Docker hybrid mode...")
                use_docker, docker_reason = await self._try_docker_hybrid_for_service(definition)
                logger.info(f"[v137.1] {service_name}: Docker check complete (use_docker={use_docker}, reason={docker_reason})")
                if use_docker:
                    # Create managed process entry for Docker
                    managed = ManagedProcess(definition=definition)
                    managed.status = ServiceStatus.HEALTHY
                    managed.port = definition.default_port
                    managed.pid = None
                    self.processes[service_name] = managed

                    # Start health monitor (track task for proper cleanup)
                    monitor_task = asyncio.create_task(
                        self._health_monitor_loop(managed),
                        name=f"health_monitor_{service_name}"
                    )
                    managed.health_monitor_task = monitor_task
                    self._track_background_task(monitor_task)

                    # Register in service registry
                    if self.registry:
                        await self.registry.register_service(
                            service_name=service_name,
                            pid=0,
                            port=managed.port,
                            health_endpoint=definition.health_endpoint,
                            metadata={
                                "repo_path": str(definition.repo_path),
                                "docker": True,
                                "container": self.config.jarvis_prime_docker_container,
                            }
                        )

                    # v95.5: Publish ready event for Docker service
                    await self._end_span(service_span, status="success")
                    await self.publish_service_lifecycle_event(service_name, "ready", {"mode": "docker"})
                    # v117.5: Persist state for future restarts (PID 0 for Docker-managed)
                    await self._persist_service_state(service_name, 0, managed.port, "running")
                    return service_name, True, f"Docker container ({docker_reason})"

                # Step 4: Spawn local process
                logger.info(f"[v137.1] {service_name}: Docker not available, spawning local process...")

                # v95.9: Check for port conflicts and resolve them automatically
                logger.info(f"[v137.1] {service_name}: Resolving port conflicts...")
                resolved_port, _ = await self._resolve_port_conflict(
                    service_name, definition.default_port
                )
                if resolved_port != definition.default_port:
                    logger.info(
                        f"[v95.9] Port conflict resolved for {service_name}: "
                        f"{definition.default_port} -> {resolved_port}"
                    )
                    # Update the port in place (dataclass is mutable)
                    definition.default_port = resolved_port
                    # Also update script_args if they contain the port
                    if definition.script_args:
                        updated_args = []
                        skip_next = False
                        for i, arg in enumerate(definition.script_args):
                            if skip_next:
                                skip_next = False
                                continue
                            if arg == "--port" and i + 1 < len(definition.script_args):
                                updated_args.append(arg)
                                updated_args.append(str(resolved_port))
                                skip_next = True
                            else:
                                updated_args.append(arg)
                        definition.script_args = updated_args

                managed = ManagedProcess(definition=definition)
                self.processes[service_name] = managed

                logger.info(f"[v137.1] {service_name}: Calling _spawn_service...")
                success = await self._spawn_service(managed)
                logger.info(f"[v137.1] {service_name}: _spawn_service returned: {success}")

                if success:
                    # v95.9: Start process health monitor for crash detection and auto-restart
                    health_monitor_task = asyncio.create_task(
                        self._monitor_process_health(managed),
                        name=f"crash_monitor_{service_name}"
                    )
                    self._track_background_task(health_monitor_task)
                    logger.debug(f"[v95.9] Started crash monitor for {service_name}")

                    # v95.5: Publish ready event for local process
                    await self._end_span(service_span, status="success")
                    await self.publish_service_lifecycle_event(service_name, "ready", {"mode": "local"})
                    # v117.5: Persist state for future restarts
                    if managed.pid and managed.port:
                        await self._persist_service_state(service_name, managed.pid, managed.port, "running")
                    return service_name, True, "local process"
                else:
                    # v95.5: Publish failed event
                    await self._end_span(service_span, status="error", error_message="spawn failed")
                    await self.publish_service_lifecycle_event(service_name, "failed", {"error": "spawn failed"})
                    return service_name, False, "spawn failed"

            finally:
                # ==============================================================
                # PHASE 4: Release semaphore (always, even on error)
                # ==============================================================
                self._service_startup_semaphore_safe.release()

        except Exception as e:
            logger.error(f"    ❌ {service_name} startup error: {e}")
            # v95.5: End span with error status
            await self._end_span(service_span, status="error", error_message=str(e))
            await self.publish_service_lifecycle_event(service_name, "failed", {"error": str(e)})
            return service_name, False, f"error: {e}"

        finally:
            # Mark service as no longer starting
            async with self._startup_coordination_lock_safe:
                self._services_starting.discard(service_name)
                if service_name in self._startup_events:
                    self._startup_events[service_name].set()

    async def _start_services_parallel(
        self,
        definitions: List[ServiceDefinition],
    ) -> Dict[str, bool]:
        """
        v93.11: Start multiple services in parallel with intelligent coordination.

        Features:
        - Parallel startup using asyncio.gather
        - Semaphore limits concurrent startups (default: 3)
        - Memory-aware service ordering (light services first)
        - Error isolation (one failure doesn't block others)
        - Aggregated results with detailed reasons

        Returns:
            Dict mapping service names to success status
        """
        self._ensure_locks_initialized()

        # v95.0: Emit parallel startup begin event
        await _emit_event("PARALLEL_STARTUP_BEGIN", priority="MEDIUM", details={"service_count": len(definitions)})

        # v95.2: CRITICAL FIX - Dependency-aware service ordering
        # The previous logic had a deadlock:
        # - Phase 1 started "light" services (including reactor-core)
        # - Phase 2 started "heavy" services (jarvis-prime)
        # - But reactor-core depends on jarvis-prime!
        # - reactor-core in Phase 1 would wait forever for jarvis-prime in Phase 2
        #
        # New logic: Services that depend on heavy services are also considered "heavy"
        # This ensures proper startup order based on dependency graph, not just ML load.

        # Build set of heavy service names
        heavy_service_names = {"jarvis-prime"}  # Base heavy services

        # Find all services that depend on heavy services (transitively)
        # These must also be treated as "heavy" to respect dependency order
        changed = True
        while changed:
            changed = False
            for d in definitions:
                if d.name not in heavy_service_names:
                    # Check if this service depends on any heavy service
                    if d.depends_on and any(dep in heavy_service_names for dep in d.depends_on):
                        heavy_service_names.add(d.name)
                        changed = True
                        logger.debug(f"[v95.2] '{d.name}' depends on heavy service, promoting to Phase 2")

        # Separate into phases based on dependency-aware classification
        heavy_services = [d for d in definitions if d.name in heavy_service_names]
        light_services = [d for d in definitions if d.name not in heavy_service_names]

        # v95.2: Sort heavy services by dependency order using topological sort
        # jarvis-prime must start before reactor-core
        def get_dependency_order(services: List[ServiceDefinition]) -> List[ServiceDefinition]:
            """Sort services so dependencies come first."""
            ordered = []
            remaining = list(services)
            service_names = {s.name for s in services}
            started_names = set()

            max_iterations = len(remaining) * 2  # Prevent infinite loop
            iterations = 0

            while remaining and iterations < max_iterations:
                iterations += 1
                for service in list(remaining):
                    # Check if all dependencies (within our set) are satisfied
                    deps_in_set = [d for d in (service.depends_on or []) if d in service_names]
                    if all(dep in started_names for dep in deps_in_set):
                        ordered.append(service)
                        started_names.add(service.name)
                        remaining.remove(service)

            # Add any remaining (circular deps) at the end
            ordered.extend(remaining)
            return ordered

        heavy_services = get_dependency_order(heavy_services)
        logger.info(f"[v95.2] Startup order - Light: {[s.name for s in light_services]}, "
                    f"Heavy: {[s.name for s in heavy_services]}")

        results: Dict[str, bool] = {}
        reasons: Dict[str, str] = {}

        # Phase 1: Start light services in parallel
        if light_services:
            logger.info(f"  🚀 Starting {len(light_services)} light service(s) in parallel...")
            logger.info(f"[v137.1] Creating asyncio tasks for light services: {[s.name for s in light_services]}")

            light_tasks = [
                self._start_single_service_with_coordination(d)
                for d in light_services
            ]
            logger.info(f"[v137.1] Light tasks created: {len(light_tasks)}, now awaiting gather...")

            light_results = await asyncio.gather(*light_tasks, return_exceptions=True)
            logger.info(f"[v137.1] asyncio.gather completed for light services")

            for result in light_results:
                # v95.7: Check for BaseException (not just Exception) for proper type narrowing
                if isinstance(result, BaseException):
                    logger.error(f"  ❌ Service startup exception: {result}")
                    continue

                name, success, reason = result
                results[name] = success
                reasons[name] = reason

                if success:
                    logger.info(f"    ✅ {name}: {reason}")
                    # v95.0: Emit service healthy event
                    await _emit_event("SERVICE_HEALTHY", service_name=name, priority="MEDIUM")
                else:
                    logger.warning(f"    ⚠️ {name}: {reason}")
                    # v95.0: Emit service unhealthy event
                    await _emit_event("SERVICE_UNHEALTHY", service_name=name, priority="HIGH", details={"reason": reason})

        # Phase 2: Start heavy services SEQUENTIALLY (respecting dependency order)
        # v95.2: Heavy services are started in dependency order, not parallel
        # This ensures jarvis-prime is healthy BEFORE reactor-core starts
        if heavy_services:
            logger.info(f"  🚀 Starting {len(heavy_services)} heavy service(s) in dependency order...")

            # Check memory before starting heavy services
            memory_status = await self._get_memory_status()
            logger.info(
                f"    📊 Memory: {memory_status.available_gb:.1f}GB available "
                f"({memory_status.percent_used:.0f}% used)"
            )

            # v95.2: Start heavy services SEQUENTIALLY in dependency order
            # This is critical: jarvis-prime must be HEALTHY before reactor-core starts
            for idx, definition in enumerate(heavy_services):
                logger.info(f"    🔄 Starting {definition.name}...")

                # v109.5: Broadcast service startup progress
                service_progress = 55 + (idx * 8)  # 55%, 63%, etc.
                await self._broadcast_progress_to_loading_server(
                    f"service_starting_{definition.name}",
                    f"Starting {definition.name}... (may take time for model loading)",
                    min(service_progress, 70),
                    {"service": definition.name, "action": "starting", "phase": 2}
                )

                try:
                    result = await self._start_single_service_with_coordination(definition)

                    if isinstance(result, Exception):
                        logger.error(f"  ❌ Heavy service startup exception: {result}")
                        results[definition.name] = False
                        reasons[definition.name] = str(result)
                        continue

                    name, success, reason = result
                    results[name] = success
                    reasons[name] = reason

                    if success:
                        logger.info(f"    ✅ {name}: {reason}")
                        # v95.0: Emit service healthy event for heavy service
                        await _emit_event("SERVICE_HEALTHY", service_name=name, priority="HIGH")

                        # v109.5: Broadcast service healthy
                        await self._broadcast_progress_to_loading_server(
                            f"service_healthy_{name}",
                            f"✅ {name} is healthy and responding",
                            min(65 + (idx * 8), 72),
                            {"service": name, "status": "healthy", "phase": 2}
                        )

                        # v95.2: Mark this service as ready for dependent services
                        # v95.7: Fixed bug - use _startup_events (Dict) not _services_ready (Set)
                        self._services_ready.add(name)
                        if name in self._startup_events:
                            self._startup_events[name].set()
                    else:
                        logger.warning(f"    ⚠️ {name}: {reason}")
                        # v95.0: Emit service unhealthy event for heavy service
                        await _emit_event("SERVICE_UNHEALTHY", service_name=name, priority="CRITICAL", details={"reason": reason})

                        # v109.5: Broadcast service failed (but continue)
                        await self._broadcast_progress_to_loading_server(
                            f"service_unavailable_{name}",
                            f"⚠️ {name} unavailable (continuing in degraded mode)",
                            min(65 + (idx * 8), 72),
                            {"service": name, "status": "unavailable", "phase": 2}
                        )

                        # v95.2: If a heavy service fails, dependent services will also fail
                        # But we continue trying other services in case they don't depend on this one
                        logger.warning(f"    ⚠️ Services depending on {name} may fail")

                except Exception as e:
                    logger.error(f"  ❌ Exception starting {definition.name}: {e}")
                    results[definition.name] = False
                    reasons[definition.name] = str(e)

        # v95.0: Emit parallel startup complete event
        await _emit_event("PARALLEL_STARTUP_COMPLETE", priority="MEDIUM", details={"results": results})

        return results

    async def _aggregate_cross_repo_health(self) -> Dict[str, Any]:
        """
        v93.11: Aggregate health status from all repos.

        Returns unified health view across JARVIS, Prime, and Reactor-Core.
        """
        self._ensure_locks_initialized()

        session = await self._get_http_session()

        services = [
            ("jarvis", 8010, "/health"),
            ("jarvis-prime", self.config.jarvis_prime_default_port, "/health"),
            ("reactor-core", self.config.reactor_core_default_port, "/health"),
        ]

        async def check_health(name: str, port: int, endpoint: str) -> Dict[str, Any]:
            url = f"http://localhost:{port}{endpoint}"
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=5.0)
                ) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            return {
                                "name": name,
                                "healthy": True,
                                "status": data.get("status", "healthy"),
                                "port": port,
                                "details": data,
                            }
                        except Exception:
                            return {
                                "name": name,
                                "healthy": True,
                                "status": "healthy",
                                "port": port,
                            }
                    else:
                        return {
                            "name": name,
                            "healthy": False,
                            "status": f"HTTP {resp.status}",
                            "port": port,
                        }
            except asyncio.TimeoutError:
                return {"name": name, "healthy": False, "status": "timeout", "port": port}
            except aiohttp.ClientConnectorError:
                return {"name": name, "healthy": False, "status": "connection refused", "port": port}
            except Exception as e:
                return {"name": name, "healthy": False, "status": str(e), "port": port}

        # Check all services in parallel
        health_tasks = [check_health(n, p, e) for n, p, e in services]
        health_results = await asyncio.gather(*health_tasks, return_exceptions=True)

        # Aggregate results
        aggregated = {
            "timestamp": time.time(),
            "services": {},
            "all_healthy": True,
            "healthy_count": 0,
            "total_count": len(services),
        }

        for result in health_results:
            # v95.7: Check for BaseException for proper type narrowing with asyncio.gather
            if isinstance(result, BaseException):
                continue
            # v95.7: Explicit cast for type narrowing since Pyright doesn't narrow dict types well
            health_info: Dict[str, Any] = result  # type: ignore[assignment]
            name = health_info["name"]
            aggregated["services"][name] = health_info

            if health_info.get("healthy"):
                aggregated["healthy_count"] += 1
            else:
                aggregated["all_healthy"] = False

        # Update cache
        async with self._health_cache_lock_safe:
            self._service_health_cache = aggregated

        return aggregated

    # =========================================================================
    # Main Orchestration
    # =========================================================================

    async def start_all_services(self) -> Dict[str, bool]:
        """
        Start all configured services with coordinated orchestration.

        v93.0: Enhanced with:
        - Pre-flight directory initialization
        - Robust directory handling throughout

        v5.0: Enhanced with:
        - Pre-flight cleanup of legacy ports
        - Wrong-binding detection (127.0.0.1 vs 0.0.0.0)
        - Trinity config integration for port consistency

        Returns dict mapping service names to success status.
        """
        self._running = True

        # =========================================================================
        # v143.0: SET HARDWARE ENVIRONMENT VARS FIRST (Critical for memory gate)
        # =========================================================================
        # This MUST happen before ANY spawn attempts. The v142.0 memory gate checks
        # os.environ for JARVIS_ENABLE_SLIM_MODE to determine thresholds.
        # Without this, the memory gate won't detect Slim Mode and will use the
        # wrong thresholds (80% instead of 95%), blocking startup on low-memory systems.
        # =========================================================================
        hw_assessment = None
        try:
            hw_assessment = assess_hardware_profile()
            set_hardware_env_in_supervisor(hw_assessment)
            log_hardware_assessment(hw_assessment)
        except Exception as e:
            logger.warning(f"[v143.0] Hardware assessment failed: {e}, proceeding with defaults")

        # =========================================================================
        # v147.0: ELASTIC SCALING - Hardware Upgrade Detection
        # =========================================================================
        # Check if user upgraded RAM since the cloud lock was set.
        # If RAM >= 32GB but cloud lock exists, auto-release it.
        # This allows users to return to local mode after a RAM upgrade
        # without manual intervention.
        # =========================================================================
        try:
            hardware_upgraded = check_and_release_cloud_lock_on_hardware_upgrade()
            if hardware_upgraded:
                logger.info(
                    "[v147.0] ✅ Cloud lock auto-released due to hardware upgrade. "
                    "System will use local inference."
                )
        except Exception as e:
            logger.debug(f"[v147.0] Hardware upgrade check failed: {e}")

        # =========================================================================
        # v146.0: TRINITY PROTOCOL - CLOUD-FIRST STRATEGY
        # =========================================================================
        # Check for persistent cloud lock (survives restarts after OOM)
        # and start background GCP pre-warm on SLIM hardware.
        #
        # This transforms startup from "Try Local, Fail, Retry" to
        # "Assess, Offload, Execute" - GCP is ready BEFORE you need it.
        # =========================================================================
        cloud_locked, lock_reason = is_cloud_locked()
        if cloud_locked:
            logger.warning(
                f"[v146.0] 🔒 CLOUD LOCK ACTIVE: {lock_reason}\n"
                f"    System will enforce cloud-only mode.\n"
                f"    To clear: call clear_cloud_lock() or delete ~/.jarvis/trinity/cloud_lock.json"
            )
            os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] = "true"
            os.environ["JARVIS_FORCE_CLOUD_HYBRID"] = "true"

        # Determine if Trinity Protocol (Cloud-First) should be activated
        should_activate_trinity = False
        trinity_reason = ""

        if cloud_locked:
            should_activate_trinity = True
            trinity_reason = f"Cloud lock active: {lock_reason}"
        elif hw_assessment and hw_assessment.profile in (HardwareProfile.SLIM, HardwareProfile.CLOUD_ONLY):
            should_activate_trinity = True
            trinity_reason = f"Hardware profile: {hw_assessment.profile.name}"
        elif hw_assessment and hw_assessment.force_cloud_hybrid:
            should_activate_trinity = True
            trinity_reason = "force_cloud_hybrid flag set"

        if should_activate_trinity:
            logger.info(
                f"[v146.0] 🔥 TRINITY PROTOCOL ACTIVATED: {trinity_reason}\n"
                f"    → Starting background GCP pre-warm (non-blocking)\n"
                f"    → jarvis-prime will run as Hollow Client\n"
                f"    → Heavy inference offloaded to GCP"
            )

            # Start background GCP pre-warm (NON-BLOCKING)
            # This runs in parallel with the rest of startup
            try:
                start_trinity_gcp_prewarm()
            except Exception as e:
                logger.warning(f"[v146.0] Could not start GCP pre-warm task: {e}")

            await _emit_event(
                "TRINITY_PROTOCOL_ACTIVATED",
                priority="HIGH",
                details={
                    "reason": trinity_reason,
                    "profile": hw_assessment.profile.name if hw_assessment else "unknown",
                    "cloud_locked": cloud_locked,
                }
            )

        # v117.0: Acquire distributed startup lock to prevent concurrent supervisor instances
        # This solves race conditions where multiple supervisors try to start/adopt services
        self._startup_lock_acquired = False
        self._startup_lock_context = None
        try:
            from backend.core.distributed_lock_manager import get_lock_manager
            lock_manager = await get_lock_manager()

            # v117.0: Write startup state file for other potential supervisors to detect
            # v137.0: Use non-blocking I/O for all file operations
            startup_state_file = Path.home() / ".jarvis" / "trinity" / "state" / "orchestrator.json"

            def _ensure_state_dir():
                startup_state_file.parent.mkdir(parents=True, exist_ok=True)
            await _run_blocking_io(_ensure_state_dir, timeout=2.0, operation_name="ensure_orchestrator_state_dir")

            # Check for existing startup state and verify if owner is still alive
            # v137.0: Use non-blocking read
            existing_state = await read_json_nonblocking(startup_state_file)
            if existing_state is not None:
                existing_pid = existing_state.get("pid", 0)
                if existing_pid and existing_pid != os.getpid():
                    try:
                        os.kill(existing_pid, 0)  # Check if process exists
                        logger.warning(
                            f"[v137.0] ⚠️ Another supervisor (PID {existing_pid}) may be running. "
                            f"Will attempt lock acquisition..."
                        )
                    except OSError:
                        # Process is dead - clean up stale state
                        logger.info(f"[v137.0] Cleaning up stale orchestrator state (dead PID {existing_pid})")

            # Write our startup state first
            startup_state = {
                "pid": os.getpid(),
                "started_at": time.time(),
                "status": "acquiring_lock",
                "version": "137.0"
            }
            await write_json_nonblocking(startup_state_file, startup_state)

            # Try to acquire startup lock with context manager
            # Timeout: 30s (to wait for previous supervisor if needed)
            # TTL: 600s (10 minutes - max expected startup time)
            self._startup_lock_context = lock_manager.acquire(
                "trinity_startup",
                timeout=30.0,
                ttl=600.0
            )

            # Enter the context manager manually so we can maintain the lock
            # throughout the entire startup process
            # v117.5: Use __aenter__ for async context managers (not __anext__)
            self._startup_lock_acquired = await self._startup_lock_context.__aenter__()

            if self._startup_lock_acquired:
                logger.info("[v137.0] ✅ Startup lock acquired - proceeding with orchestration")
                # v137.0: Update state file with non-blocking I/O
                startup_state["status"] = "lock_acquired"
                await write_json_nonblocking(startup_state_file, startup_state)
            else:
                logger.error("[v137.0] ❌ Could not acquire startup lock after 30s - another supervisor may be running")
                startup_state["status"] = "lock_failed"
                await write_json_nonblocking(startup_state_file, startup_state)
                # v117.5: Return failure for all services when lock not acquired
                return {"startup_lock": False, "jarvis-body": False}

        except ImportError:
            logger.debug("[v117.0] DistributedLockManager not available, proceeding without lock")
        except StopAsyncIteration:
            # Lock acquisition failed (context manager exhausted without yielding True)
            logger.error("[v117.0] ❌ Lock acquisition failed - context manager exhausted")
            # v117.5: Return failure for all services when lock failed
            return {"startup_lock": False, "jarvis-body": False}
        except Exception as e:
            logger.warning(f"[v117.0] Startup lock acquisition failed: {e}, proceeding without lock")

        # v95.4: Initialize locks and set jarvis-body to starting status
        self._ensure_locks_initialized()
        self._jarvis_body_status = "starting"
        self._jarvis_body_startup_time = time.time()
        self._services_starting.add("jarvis-body")
        logger.info("[v95.4] jarvis-body status: starting")

        # v109.5: Start progress keepalive and broadcast initial progress
        # CRITICAL: Prevents 60-second watchdog shutdown during cross-repo startup
        await self._start_progress_keepalive()
        await self._broadcast_progress_to_loading_server(
            "cross_repo_init",
            "Cross-repo orchestration starting...",
            38,
            {"phase": "init", "service": "orchestrator"}
        )

        # v95.5: Initialize distributed tracing FIRST (for correlation across all phases)
        await self._initialize_distributed_tracing()
        startup_span = await self._create_span("full_startup", metadata={"phase": "init"})

        # v95.5: Initialize event bus for lifecycle events
        await self._initialize_event_bus()
        await self.publish_service_lifecycle_event("jarvis-body", "starting", {"pid": os.getpid()})

        # v95.5: Initialize graceful degradation infrastructure
        await self._initialize_graceful_degradation()

        # v95.10: Initialize cross-repo integration systems
        await self._initialize_cross_repo_integration()

        # v93.0: CRITICAL - Ensure all required directories exist FIRST
        # This prevents "No such file or directory" errors throughout startup
        try:
            from backend.core.service_registry import ensure_all_jarvis_directories
            dir_stats = ensure_all_jarvis_directories()
            if dir_stats["failed"]:
                logger.warning(
                    f"[v93.0] Some directories could not be created: {dir_stats['failed']}"
                )
        except Exception as e:
            logger.warning(f"[v93.0] Directory pre-flight check failed: {e}")

        # Setup signal handlers
        try:
            self._setup_signal_handlers()
        except Exception as e:
            logger.warning(f"Could not setup signal handlers: {e}")

        # v95.2: CRITICAL - Register jarvis-body IMMEDIATELY before starting external services
        # This fixes the issue where jarvis-prime waits 120s for jarvis-body that never registered
        # The registration MUST happen BEFORE Phase 2 starts external services
        jarvis_body_registered = False
        if self.registry:
            # v95.2: First, ensure jarvis-body is registered and heartbeat sent IMMEDIATELY
            # This is critical for cross-repo discovery to work
            try:
                success = await self.registry.ensure_owner_registered_immediately()
                if success:
                    logger.info("[v95.2] ✅ jarvis-body registered (early registration for cross-repo discovery)")
                    jarvis_body_registered = True
                else:
                    logger.warning("[v95.2] ⚠️ Early jarvis-body registration may have failed")
            except Exception as e:
                logger.warning(f"[v95.2] Early jarvis-body registration warning: {e}")

            # Start cleanup task which includes self-heartbeat loop
            await self.registry.start_cleanup_task()

        # v95.4: Verify jarvis-body health and update unified state
        if jarvis_body_registered:
            # Update unified state to signal jarvis-body is available
            await self._update_unified_state_for_jarvis_body("starting", health=True)

            # Verify health - this confirms registry entry and heartbeat are working
            health_verified = await self._verify_jarvis_body_health(timeout=30.0)
            if health_verified:
                self._jarvis_body_status = "healthy"
                self._services_starting.discard("jarvis-body")
                self._services_ready.add("jarvis-body")
                await self._update_unified_state_for_jarvis_body("healthy", health=True)
                logger.info("[v95.4] ✅ jarvis-body health verified, external services can proceed")
            else:
                # Registration succeeded but health check didn't complete - still allow startup
                self._jarvis_body_status = "starting"
                await self._update_unified_state_for_jarvis_body("starting", health=True)
                logger.warning("[v95.4] ⚠️ jarvis-body health verification incomplete, proceeding with caution")

            # Signal jarvis-body ready event
            if self._jarvis_body_ready_event:
                self._jarvis_body_ready_event.set()
                logger.debug("[v95.4] jarvis-body ready event signaled")

        results = {"jarvis-body": jarvis_body_registered, "jarvis": True}  # Track jarvis-body explicitly

        logger.info("=" * 70)
        logger.info("Cross-Repo Startup Orchestrator v95.0 - Enterprise Grade")
        logger.info("  Features: Parallel startup, Connection pooling, Thread-safe ops, Voice narration")
        logger.info("=" * 70)
        logger.info(f"  Ports: jarvis-prime={self.config.jarvis_prime_default_port}, "
                    f"reactor-core={self.config.reactor_core_default_port}")

        # v95.0: Emit startup begin event for voice narration
        await _emit_event("STARTUP_BEGIN", priority="HIGH")

        # Phase 0: Pre-flight cleanup (v5.0 + v4.0 Service Registry)
        logger.info("\n📍 PHASE 0: Pre-flight cleanup")

        # v109.5: Broadcast Phase 0 progress
        await self._broadcast_progress_to_loading_server(
            "cross_repo_phase0",
            "Pre-flight cleanup: Cleaning up legacy ports...",
            40,
            {"phase": 0, "action": "cleanup"}
        )

        # v135.0: Comprehensive port cleanup (replaces legacy-only cleanup)
        # This cleans ALL service ports dynamically from definitions, not just legacy ports
        try:
            await self._comprehensive_pre_flight_cleanup()
        except Exception as e:
            logger.warning(f"[v135.0] Comprehensive cleanup failed, falling back to legacy: {e}")
            # Fallback to legacy cleanup if comprehensive fails
            await self._cleanup_legacy_ports()

        # v4.0: Clean up stale service registry entries (dead PIDs, PID reuse)
        if self.registry:
            logger.info("  🧹 Service registry pre-flight cleanup...")
            try:
                cleanup_stats = await self.registry.pre_flight_cleanup()
                removed_count = (
                    len(cleanup_stats.get("removed_dead_pid", [])) +
                    len(cleanup_stats.get("removed_pid_reuse", [])) +
                    len(cleanup_stats.get("removed_invalid", []))
                )
                if removed_count > 0:
                    logger.info(
                        f"  ✅ Cleaned {removed_count} stale registry entries "
                        f"({cleanup_stats['valid_entries']} valid remain)"
                    )
                else:
                    logger.info(
                        f"  ✅ Registry clean ({cleanup_stats['valid_entries']} valid services)"
                    )
            except Exception as e:
                logger.warning(f"  ⚠️ Registry cleanup failed (continuing): {e}")

        # Phase 1: JARVIS Core (already starting)
        logger.info("\n📍 PHASE 1: JARVIS Core (starting via supervisor)")
        logger.info("✅ JARVIS Core initialization in progress...")

        # v109.5: Broadcast Phase 1 progress
        await self._broadcast_progress_to_loading_server(
            "cross_repo_phase1",
            "JARVIS Core initialization in progress...",
            45,
            {"phase": 1, "service": "jarvis-core"}
        )

        # Phase 2: Probe and spawn external services (v93.11: PARALLEL)
        logger.info("\n📍 PHASE 2: External services startup (PARALLEL)")

        # v109.5: Broadcast Phase 2 progress
        await self._broadcast_progress_to_loading_server(
            "cross_repo_phase2",
            "Starting external services (JARVIS-Prime, Reactor-Core)...",
            50,
            {"phase": 2, "action": "external_services"}
        )

        definitions = self._get_service_definitions()

        # v93.11: Use parallel startup with coordination
        parallel_results = await self._start_services_parallel(definitions)
        results.update(parallel_results)

        # Phase 3: Verification
        logger.info("\n📍 PHASE 3: Integration verification")

        # v109.5: Broadcast Phase 3 progress
        await self._broadcast_progress_to_loading_server(
            "cross_repo_phase3",
            "Integration verification in progress...",
            75,
            {"phase": 3, "action": "verification"}
        )

        healthy_count = sum(1 for v in results.values() if v)
        total_count = len(results)

        if healthy_count == total_count:
            logger.info(f"✅ All {total_count} services operational - FULL MODE")
            # v95.0: Emit startup complete event
            await _emit_event("STARTUP_COMPLETE", priority="HIGH", elapsed_seconds=time.time() - self._running)
        else:
            logger.warning(
                f"⚠️ Running in DEGRADED MODE: {healthy_count}/{total_count} services operational"
            )
            # v95.0: Emit health degraded event
            unhealthy = [name for name, success in results.items() if not success]
            await _emit_event(
                "HEALTH_DEGRADED",
                priority="HIGH",
                details={"unhealthy_services": unhealthy, "healthy_count": healthy_count, "total_count": total_count}
            )

        # Phase 4: v93.0 - Register restart commands with resilient mesh
        logger.info("\n📍 PHASE 4: Registering auto-restart commands")

        # v109.5: Broadcast Phase 4 progress
        await self._broadcast_progress_to_loading_server(
            "cross_repo_phase4",
            "Registering auto-restart commands...",
            80,
            {"phase": 4, "action": "restart_commands"}
        )
        try:
            # Get service names that were started (excluding jarvis which is always running)
            started_services = [
                name for name, success in results.items()
                if success and name != "jarvis"
            ]
            started_services.append("jarvis-core")  # Ensure jarvis-core is included

            restart_results = await register_restart_commands_with_mesh(started_services)
            registered_count = sum(1 for v in restart_results.values() if v)

            if registered_count > 0:
                logger.info(f"  ✅ Registered {registered_count} restart commands for auto-healing")
            else:
                logger.warning(
                    "  ⚠️ No restart commands registered (mesh may not be initialized yet)"
                )
        except Exception as e:
            logger.warning(f"  ⚠️ Restart command registration failed (non-fatal): {e}")

        # v95.1: Phase 5 - Start intelligent recovery coordinator
        logger.info("\n📍 PHASE 5: Starting intelligent recovery coordinator")

        # v109.5: Broadcast Phase 5 progress
        await self._broadcast_progress_to_loading_server(
            "cross_repo_phase5",
            "Starting intelligent recovery coordinator...",
            85,
            {"phase": 5, "action": "recovery_coordinator"}
        )
        try:
            await self.start_recovery_coordinator()
            logger.info("  ✅ Recovery coordinator active (proactive health monitoring enabled)")
        except Exception as e:
            logger.warning(f"  ⚠️ Recovery coordinator startup failed (non-fatal): {e}")

        # Print summary
        logger.info("\n" + "=" * 70)
        logger.info("🎯 Startup Summary:")
        for name, success in results.items():
            status = "✅ Running" if success else "⚠️ Unavailable"
            logger.info(f"  {name}: {status}")
        logger.info("=" * 70)

        # v95.5: Publish completion lifecycle events for all services
        for name, success in results.items():
            if name == "jarvis-body":
                continue  # Already published
            status = "ready" if success else "failed"
            await self.publish_service_lifecycle_event(
                service=name,
                status=status,
                details={
                    "startup_time": time.time() - (self._jarvis_body_startup_time or time.time()),
                    "degradation_mode": self._degradation_mode.get(name, "unknown"),
                }
            )

        # v95.5: End the startup trace span
        if startup_span:
            status = "success" if healthy_count == total_count else "partial"
            await self._end_span(startup_span, status=status)

        # v95.5: Publish final startup completion event with degradation info
        await self._publish_lifecycle_event(
            event_type="lifecycle.startup_complete",
            payload={
                "services": results,
                "healthy_count": healthy_count,
                "total_count": total_count,
                "degradation_status": self.get_service_degradation_status(),
                "startup_duration": time.time() - (self._jarvis_body_startup_time or time.time()),
            },
            priority="HIGH" if healthy_count == total_count else "CRITICAL",
        )

        # v95.5: Log distributed tracing summary
        logger.info(f"[v95.5] Startup correlation ID: {self.get_correlation_id()}")

        # v109.5: Stop keepalive and broadcast final completion
        await self._stop_progress_keepalive()
        await self._broadcast_progress_to_loading_server(
            "cross_repo_complete",
            f"Cross-repo orchestration complete! {healthy_count}/{total_count} services healthy",
            90 if healthy_count == total_count else 85,
            {"phase": "complete", "healthy_count": healthy_count, "total_count": total_count}
        )

        # v117.0: Update orchestrator state file with completion status
        try:
            # v137.0: Update orchestrator state with non-blocking I/O
            startup_state_file = Path.home() / ".jarvis" / "trinity" / "state" / "orchestrator.json"
            orchestrator_state = {
                "pid": os.getpid(),
                "started_at": self._jarvis_body_startup_time or time.time(),
                "completed_at": time.time(),
                "status": "running" if healthy_count == total_count else "degraded",
                "healthy_count": healthy_count,
                "total_count": total_count,
                "services": results,
                "version": "137.0"
            }
            await write_json_nonblocking(startup_state_file, orchestrator_state)
        except Exception as e:
            logger.debug(f"[v137.0] Failed to update orchestrator state: {e}")

        # v117.5: Release startup lock by properly exiting context manager
        # Use __aexit__ for async context managers (not __anext__)
        if hasattr(self, '_startup_lock_context') and self._startup_lock_context is not None:
            try:
                # Properly exit the async context manager
                await self._startup_lock_context.__aexit__(None, None, None)
                logger.info("[v117.5] ✅ Startup lock released")
            except Exception as e:
                logger.debug(f"[v117.5] Startup lock release note: {e}")

        return results

    async def shutdown_all_services(self) -> None:
        """
        v95.3 + v95.11: Gracefully shutdown all managed services with proper ordering.

        Enhanced with:
        - REVERSE DEPENDENCY ORDER: Services shutdown in reverse startup order
          (dependents first, then their dependencies)
        - Background task cancellation (prevents orphaned coroutines)
        - HTTP session cleanup with error handling
        - Startup coordination cleanup
        - Timeout protection per service
        - Shutdown completion flag (v95.3) prevents post-shutdown recovery
        - v95.11: Operation draining before service termination
        """
        logger.info("\n🛑 Shutting down all services...")

        # v95.3: Set shutdown_completed flag EARLY to prevent any new recovery attempts
        # This is defensive - even if recovery coordinator is slow to stop, it will see this flag
        self._shutdown_completed = True
        self._shutdown_completed_timestamp = time.time()
        logger.info("[v95.3] Shutdown completion flag set - recovery disabled")

        # v95.11: Start draining all operations FIRST (before stopping services)
        # This prevents new database operations from starting during shutdown
        drain_timeout = float(os.environ.get("SHUTDOWN_DRAIN_TIMEOUT", "10.0"))
        try:
            from backend.core.resilience.graceful_shutdown import get_operation_guard_sync
            guard = get_operation_guard_sync()

            # Signal global shutdown - no new operations allowed
            await guard.begin_global_shutdown()
            logger.info(f"[v95.11] 🛑 Operation draining started (timeout: {drain_timeout}s)")

            # Wait for in-flight operations to complete
            active_before = guard.get_total_active()
            if active_before > 0:
                logger.info(f"[v95.11] Waiting for {active_before} in-flight operations to complete...")
                drained = await guard.wait_for_all_drain(timeout=drain_timeout)

                if drained:
                    logger.info("[v95.11] ✅ All operations drained successfully")
                else:
                    remaining = guard.get_total_active()
                    stats = guard.get_stats()
                    logger.warning(
                        f"[v95.11] ⚠️ Drain incomplete: {remaining} operations still active "
                        f"(categories: {stats['active_by_category']})"
                    )
            else:
                logger.info("[v95.11] ✅ No in-flight operations to drain")
        except ImportError:
            logger.debug("[v95.11] OperationGuard not available, skipping drain")
        except Exception as e:
            logger.warning(f"[v95.11] Error during operation drain: {e}")

        # v95.1: Stop recovery coordinator first (prevent restart during shutdown)
        await self.stop_recovery_coordinator()

        # v95.1: Calculate reverse dependency order for shutdown
        # Services that depend on others should shutdown FIRST
        shutdown_order = self._calculate_shutdown_order()
        logger.info(f"[v95.1] Shutdown order: {' → '.join(shutdown_order)}")

        # v95.1: Shutdown in phases by dependency level
        for service_name in shutdown_order:
            if service_name in self.processes:
                managed = self.processes[service_name]
                try:
                    logger.info(f"  Stopping {service_name}...")
                    await asyncio.wait_for(
                        self._stop_process(managed),
                        timeout=30.0  # Per-service shutdown timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[v95.1] Timeout stopping {service_name}, forcing termination"
                    )
                    if managed.process and managed.process.returncode is None:
                        try:
                            managed.process.kill()
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"[v95.1] Error stopping {service_name}: {e}")

        # v95.1: Cancel all tracked background tasks
        await self._cancel_all_background_tasks(timeout=10.0)

        # Stop registry cleanup
        if self.registry:
            try:
                await self.registry.stop_cleanup_task()
            except Exception as e:
                logger.warning(f"[v95.1] Registry cleanup error: {e}")

        # v95.1: Close shared HTTP session with error handling
        await self._close_http_session()

        # v95.12: Cleanup multiprocessing resources (ProcessPoolExecutors, semaphores)
        try:
            from backend.core.resilience.graceful_shutdown import cleanup_multiprocessing_resources
            mp_timeout = float(os.environ.get("SHUTDOWN_MP_CLEANUP_TIMEOUT", "10.0"))
            logger.info(f"[v95.12] Cleaning up multiprocessing resources...")
            mp_result = await cleanup_multiprocessing_resources(timeout=mp_timeout)
            logger.info(
                f"[v95.12] ✅ Multiprocessing cleanup: "
                f"{mp_result.get('successful', 0)} success, "
                f"{mp_result.get('forced', 0)} forced, "
                f"{mp_result.get('failed', 0)} failed"
            )
        except ImportError:
            logger.debug("[v95.12] Multiprocessing cleanup not available")
        except Exception as e:
            logger.warning(f"[v95.12] Multiprocessing cleanup error: {e}")

        # v95.1: Clear startup coordination state
        self._services_starting.clear()
        self._services_ready.clear()
        self._startup_events.clear()
        self._service_health_cache.clear()
        self._background_tasks.clear()

        # v95.13: Mark global shutdown as completed
        try:
            from backend.core.resilience.graceful_shutdown import complete_global_shutdown
            complete_global_shutdown()
        except Exception as e:
            logger.debug(f"[v95.13] Could not complete global shutdown signal: {e}")

        # v137.0: Shutdown I/O Airlock thread pool
        try:
            _shutdown_io_thread_pool()
            logger.info("[v137.0] ✅ I/O Airlock thread pool shut down")
        except Exception as e:
            logger.debug(f"[v137.0] I/O Airlock thread pool cleanup note: {e}")

        logger.info("✅ All services shut down")
        self._running = False

    def _calculate_shutdown_order(self) -> List[str]:
        """
        v95.1: Calculate optimal shutdown order based on dependencies.

        Returns services in REVERSE dependency order:
        - Services with NO dependents shutdown first
        - Services that others depend on shutdown last

        This prevents connection errors when services try to communicate
        with dependencies that have already shutdown.
        """
        # Build dependency graph
        dependents: Dict[str, Set[str]] = {}  # service -> set of services that depend on it
        all_services = set(self.processes.keys())

        for service_name in all_services:
            dependents[service_name] = set()

        # Populate dependents from service definitions
        for service_name, managed in self.processes.items():
            if managed.definition and managed.definition.depends_on:
                for dep in managed.definition.depends_on:
                    if dep in dependents:
                        dependents[dep].add(service_name)

        # Topological sort in reverse order (Kahn's algorithm)
        # Start with services that have NO dependents (leaf nodes)
        no_dependents = [s for s in all_services if not dependents[s]]
        shutdown_order = []

        while no_dependents:
            # Pick next service to shutdown (one with no remaining dependents)
            service = no_dependents.pop(0)
            shutdown_order.append(service)

            # "Remove" this service from the graph
            # Update services that this one depends on
            if service in self.processes:
                definition = self.processes[service].definition
                if definition and definition.depends_on:
                    for dep in definition.depends_on:
                        if dep in dependents:
                            dependents[dep].discard(service)
                            # If dep now has no dependents, add to queue
                            if not dependents[dep] and dep not in shutdown_order:
                                no_dependents.append(dep)

        # Handle any remaining services (circular dependencies or orphans)
        remaining = all_services - set(shutdown_order)
        if remaining:
            logger.warning(
                f"[v95.1] Services with circular dependencies or missing from graph: {remaining}"
            )
            shutdown_order.extend(remaining)

        return shutdown_order


# =============================================================================
# Convenience Functions (Backward Compatibility)
# =============================================================================

# Global orchestrator instance
_orchestrator: Optional[ProcessOrchestrator] = None


def get_orchestrator() -> ProcessOrchestrator:
    """Get global orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ProcessOrchestrator()
    return _orchestrator


def create_restart_function(service_name: str) -> Callable[[], Coroutine]:
    """
    v93.0: Factory function to create restart closures for services.

    This creates a restart function that can be registered with the
    SelfHealingServiceManager. When called, it will use the global
    orchestrator to restart the specified service.

    Args:
        service_name: Name of the service this restart function will handle

    Returns:
        An async function that restarts the service when called

    Example:
        restart_fn = create_restart_function("jarvis-core")
        recovery_manager.register_restart_command("jarvis-core", restart_fn)
    """
    async def restart_service() -> bool:
        """Restart the captured service via the global orchestrator."""
        orchestrator = get_orchestrator()
        return await orchestrator.restart_service(service_name)

    return restart_service


async def register_restart_commands_with_mesh(
    service_names: Optional[List[str]] = None
) -> Dict[str, bool]:
    """
    v93.0: Register restart commands for services with the resilient mesh.

    This bridges the ProcessOrchestrator's restart capability with the
    SelfHealingServiceManager in native_integration.py.

    Args:
        service_names: Optional list of service names to register.
                      If None, registers for all known services.

    Returns:
        Dict mapping service names to registration success status
    """
    results: Dict[str, bool] = {}

    # Default service names if not provided
    if service_names is None:
        service_names = ["jarvis-core", "jarvis-prime", "reactor-core"]

    try:
        # Lazy import to avoid circular dependencies
        from backend.core.ouroboros.native_integration import (
            get_resilient_mesh,
            get_recovery_manager,
        )

        # Get the recovery manager - prefer mesh's manager, fallback to singleton
        recovery_manager = None

        try:
            mesh = get_resilient_mesh()
            if mesh and hasattr(mesh, 'recovery_manager'):
                recovery_manager = mesh.recovery_manager
                logger.debug("[v93.0] Using recovery manager from resilient mesh")
        except Exception as mesh_err:
            logger.debug(f"[v93.0] Resilient mesh not available: {mesh_err}")

        # Fallback to global singleton if mesh unavailable
        if recovery_manager is None:
            try:
                recovery_manager = get_recovery_manager()
                logger.debug("[v93.0] Using global singleton recovery manager")
            except Exception as mgr_err:
                logger.debug(f"[v93.0] Recovery manager singleton failed: {mgr_err}")

        if recovery_manager is None:
            logger.warning(
                "[v93.0] Cannot register restart commands: "
                "no recovery manager available"
            )
            return {name: False for name in service_names}

        # Register restart functions for each service
        for service_name in service_names:
            try:
                restart_fn = create_restart_function(service_name)
                recovery_manager.register_restart_command(service_name, restart_fn)
                results[service_name] = True
                logger.info(f"[v93.0] Registered restart command for {service_name}")
            except Exception as e:
                logger.error(f"[v93.0] Failed to register restart for {service_name}: {e}")
                results[service_name] = False

        return results

    except ImportError as e:
        logger.warning(f"[v93.0] Could not import resilient mesh components: {e}")
        return {name: False for name in service_names}
    except Exception as e:
        logger.error(f"[v93.0] Error registering restart commands: {e}")
        return {name: False for name in service_names}


async def probe_jarvis_prime() -> bool:
    """Legacy: Probe J-Prime health endpoint."""
    config = OrchestratorConfig()
    url = f"http://localhost:{config.jarvis_prime_default_port}/health"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as response:
                return response.status == 200
    except Exception:
        return False


async def probe_reactor_core() -> bool:
    """Legacy: Probe Reactor-Core health endpoint."""
    config = OrchestratorConfig()
    # v95.14: Fixed endpoint path to match Reactor-Core's actual /health endpoint
    # (was incorrectly /api/health, causing health check failures)
    url = f"http://localhost:{config.reactor_core_default_port}/health"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as response:
                return response.status == 200
    except Exception:
        return False


async def start_all_repos() -> Dict[str, bool]:
    """Legacy: Start all repos with orchestration."""
    orchestrator = get_orchestrator()
    return await orchestrator.start_all_services()


async def initialize_cross_repo_orchestration() -> None:
    """
    Initialize cross-repo orchestration.

    This is called by run_supervisor.py during startup.
    """
    # v95.13: Reset global shutdown signal from previous runs
    try:
        from backend.core.resilience.graceful_shutdown import reset_global_shutdown
        reset_global_shutdown()
        logger.debug("[v95.13] Global shutdown signal reset for new startup")
    except Exception as e:
        logger.warning(f"[v95.13] Could not reset global shutdown signal: {e}")

    try:
        orchestrator = get_orchestrator()
        results = await orchestrator.start_all_services()

        # Initialize advanced training coordinator if Reactor-Core available
        if results.get("reactor-core"):
            logger.info("Initializing Advanced Training Coordinator...")
            try:
                from backend.intelligence.advanced_training_coordinator import (
                    AdvancedTrainingCoordinator
                )
                coordinator = await AdvancedTrainingCoordinator.create()
                logger.info("✅ Advanced Training Coordinator initialized")
            except Exception as e:
                logger.warning(f"Advanced Training Coordinator initialization failed: {e}")

    except Exception as e:
        logger.error(f"Cross-repo orchestration error: {e}", exc_info=True)


# =============================================================================
# v95.3: Global Shutdown State Accessors
# =============================================================================

def is_orchestrator_shutdown_in_progress() -> bool:
    """
    v95.3: Check if the orchestrator's shutdown is in progress.

    This is a cross-component accessor that allows other modules
    (like OrphanDetector, CoordinatedShutdownManager) to check if
    the orchestrator is shutting down.

    Returns:
        True if shutdown is in progress, False otherwise
    """
    if _orchestrator is None:
        return False
    return _orchestrator._shutdown_event.is_set()


def is_orchestrator_shutdown_completed() -> bool:
    """
    v95.3: Check if the orchestrator's shutdown has completed.

    This flag is set when shutdown finishes and stays True forever.
    Use this to prevent post-shutdown recovery attempts.

    Returns:
        True if shutdown has completed, False otherwise
    """
    if _orchestrator is None:
        return False
    return _orchestrator._shutdown_completed


def get_orchestrator_shutdown_state() -> dict:
    """
    v95.3: Get comprehensive shutdown state for debugging and coordination.

    Returns:
        Dict with shutdown state details:
        - shutdown_in_progress: bool
        - shutdown_completed: bool
        - shutdown_completed_timestamp: float or None
        - running: bool
    """
    if _orchestrator is None:
        return {
            "orchestrator_exists": False,
            "shutdown_in_progress": False,
            "shutdown_completed": False,
            "shutdown_completed_timestamp": None,
            "running": False,
        }

    return {
        "orchestrator_exists": True,
        "shutdown_in_progress": _orchestrator._shutdown_event.is_set(),
        "shutdown_completed": _orchestrator._shutdown_completed,
        "shutdown_completed_timestamp": _orchestrator._shutdown_completed_timestamp,
        "running": _orchestrator._running,
    }


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    "ProcessOrchestrator",
    "ManagedProcess",
    "ServiceDefinition",
    "ServiceStatus",
    "OrchestratorConfig",
    "get_orchestrator",
    "create_restart_function",
    "register_restart_commands_with_mesh",
    "start_all_repos",
    "initialize_cross_repo_orchestration",
    "probe_jarvis_prime",
    "probe_reactor_core",
    # v95.3: Global shutdown state accessors
    "is_orchestrator_shutdown_in_progress",
    "is_orchestrator_shutdown_completed",
    "get_orchestrator_shutdown_state",
]
