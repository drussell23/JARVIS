"""
Hardware Enforcer for JARVIS Hollow Client Mode
================================================

This module automatically enforces Hollow Client mode on machines with
insufficient RAM for local 70B models. It detects system RAM at import time
and sets JARVIS_HOLLOW_CLIENT=true if below the configurable threshold.

The macOS M1 16GB Mac cannot run local 70B models - it needs Hollow Client
mode where heavy inference is offloaded to GCP.

Design Principles:
- Enforce on import: Module-level call ensures enforcement before any code runs
- Idempotent: If JARVIS_HOLLOW_CLIENT is already "true", do nothing
- Configurable: RAM threshold adjustable via JARVIS_HOLLOW_RAM_THRESHOLD_GB
- Auditable: All enforcement decisions are logged with source context

Environment Variables:
----------------------
- JARVIS_HOLLOW_RAM_THRESHOLD_GB: RAM threshold in GB (default: 32.0)
    Purpose: Machines with RAM below this threshold are forced to Hollow Client mode
    Note: 70B models typically require 64GB+ for efficient inference

- JARVIS_HOLLOW_CLIENT: Set to "true" by this module when enforcement triggers
    Purpose: Signals to other modules that heavy inference should be offloaded

Usage:
    # Import automatically enforces based on RAM
    from backend.config.hardware_enforcer import enforce_hollow_client

    # Or explicitly call with source context
    enforce_hollow_client(source="startup_lock_context")

    # Check system RAM
    from backend.config.hardware_enforcer import get_system_ram_gb
    ram_gb = get_system_ram_gb()  # Returns float, e.g., 16.0
"""

from __future__ import annotations

import logging
import os

import psutil

from backend.utils.env_config import get_env_float

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

_DEFAULT_RAM_THRESHOLD_GB = 32.0
"""Default RAM threshold for Hollow Client enforcement (in GB)."""

_BYTES_PER_GB = 1024**3
"""Number of bytes per gigabyte."""


# =============================================================================
# RAM DETECTION
# =============================================================================


def get_system_ram_gb() -> float:
    """
    Get system RAM in gigabytes.

    Uses psutil.virtual_memory().total to detect system RAM and converts
    from bytes to GB.

    Returns:
        System RAM in GB as a float (e.g., 16.0 for 16GB)

    Note:
        When using in async context, wrap with asyncio.to_thread:
        ram_gb = await asyncio.to_thread(get_system_ram_gb)
    """
    total_bytes = psutil.virtual_memory().total
    return total_bytes / _BYTES_PER_GB


# =============================================================================
# HOLLOW CLIENT ENFORCEMENT
# =============================================================================


def enforce_hollow_client(source: str = "unknown") -> bool:
    """
    Enforce Hollow Client mode if system RAM is below threshold.

    This function:
    1. Checks if JARVIS_HOLLOW_CLIENT is already "true" (idempotent - returns True)
    2. Gets RAM threshold from JARVIS_HOLLOW_RAM_THRESHOLD_GB (default: 32.0GB)
    3. Compares system RAM against threshold
    4. If RAM < threshold: sets JARVIS_HOLLOW_CLIENT=true and logs
    5. If RAM >= threshold: logs at debug level, does not modify environment

    Args:
        source: Context string for logging (e.g., "module_import", "startup_lock_context")
                Helps with debugging where enforcement was triggered from.

    Returns:
        True if Hollow Client mode is enforced (or was already enforced)
        False if system has sufficient RAM for full mode

    Example:
        # Enforce from startup code
        if enforce_hollow_client(source="startup_lock_context"):
            logger.info("Running in Hollow Client mode - offloading to GCP")
    """
    # Idempotency check - if already set, return immediately
    if os.environ.get("JARVIS_HOLLOW_CLIENT") == "true":
        return True

    # Get configurable threshold
    threshold_gb = get_env_float(
        "JARVIS_HOLLOW_RAM_THRESHOLD_GB",
        _DEFAULT_RAM_THRESHOLD_GB,
        min_val=0.0,  # Allow zero (edge case testing)
    )

    # Get system RAM
    ram_gb = get_system_ram_gb()

    # Enforcement decision
    if ram_gb < threshold_gb:
        # Enforce Hollow Client mode
        os.environ["JARVIS_HOLLOW_CLIENT"] = "true"
        logger.info(
            f"[HardwareEnforcer] Hollow Client enforced (source: {source}): "
            f"{ram_gb:.1f}GB < {threshold_gb:.1f}GB threshold"
        )
        return True
    else:
        # Full mode available
        logger.debug(
            f"[HardwareEnforcer] Full mode available: "
            f"{ram_gb:.1f}GB >= {threshold_gb:.1f}GB"
        )
        return False


# =============================================================================
# MODULE-LEVEL ENFORCEMENT
# =============================================================================

# Enforce on import - ensures Hollow Client mode is set before any other code runs
# This is the primary enforcement point - unified_supervisor.py imports this module
# early, triggering automatic hardware detection and enforcement.
enforce_hollow_client(source="module_import")


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "get_system_ram_gb",
    "enforce_hollow_client",
]
