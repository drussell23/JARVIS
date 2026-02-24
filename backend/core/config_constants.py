"""
JARVIS Configuration Constants v1.0
====================================
Canonical source of truth for shared configuration values.
Each constant reads its env var ONCE at import time.

Usage:
    from backend.core.config_constants import BACKEND_PORT, FRONTEND_PORT

Rationale:
    Port 8010 alone appeared in 25+ files with 3 different env var names
    (BACKEND_PORT, JARVIS_API_PORT, JARVIS_PORT). Timeouts like 30.0 appeared
    in 100+ locations with no central constant. This module eliminates that
    drift by providing a single source of truth.

v270.3: Created as part of Phase 6 hardening.
"""

import os


def _env_int(key: str, *fallback_keys: str, default: int) -> int:
    """Read int from env var with fallback chain.

    Tries the primary key first, then each fallback key in order.
    Returns default if no key is set or all values are non-numeric.
    """
    for k in (key, *fallback_keys):
        val = os.environ.get(k)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    return default


def _env_float(key: str, default: float) -> float:
    """Read float from env var. Returns default if unset or non-numeric."""
    val = os.environ.get(key)
    if val is not None:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass
    return default


# =========================================================================
# PORTS
# =========================================================================
# Each port has a canonical env var name. Fallback keys handle legacy names
# that exist in older code — the fallback chain prevents breakage during
# progressive migration.

BACKEND_PORT: int = _env_int(
    "JARVIS_BACKEND_PORT", "BACKEND_PORT", "JARVIS_API_PORT", "JARVIS_PORT",
    default=8010,
)

FRONTEND_PORT: int = _env_int(
    "JARVIS_FRONTEND_PORT", "FRONTEND_PORT",
    default=3000,
)

LOADING_SERVER_PORT: int = _env_int(
    "JARVIS_LOADING_SERVER_PORT", "LOADING_SERVER_PORT",
    default=3001,
)

LOADING_SERVER_HTTP_PORT: int = _env_int(
    "JARVIS_LOADING_HTTP_PORT",
    default=8080,
)

WEBSOCKET_PORT: int = _env_int(
    "JARVIS_WEBSOCKET_PORT",
    default=8765,
)

JARVIS_PRIME_PORT: int = _env_int(
    "JARVIS_PRIME_PORT",
    default=8001,
)

INVINCIBLE_NODE_PORT: int = _env_int(
    "JARVIS_INVINCIBLE_NODE_PORT",
    default=8001,
)


# =========================================================================
# TIMEOUTS (seconds)
# =========================================================================
# Grouped by purpose. Each timeout has a clear semantic name.

# --- Shutdown ---
SHUTDOWN_TIMEOUT: float = _env_float("JARVIS_SHUTDOWN_TIMEOUT", default=30.0)
SERVICE_SHUTDOWN_TIMEOUT: float = _env_float("JARVIS_SERVICE_SHUTDOWN_TIMEOUT", default=30.0)
BACKGROUND_TASK_CLEANUP_TIMEOUT: float = _env_float("JARVIS_BACKGROUND_TASK_CLEANUP_TIMEOUT", default=10.0)

# --- Startup phases ---
CLEAN_SLATE_TIMEOUT: float = _env_float("JARVIS_CLEAN_SLATE_TIMEOUT", default=30.0)
LOADING_EXPERIENCE_TIMEOUT: float = _env_float("JARVIS_LOADING_EXPERIENCE_TIMEOUT", default=45.0)
RESOURCE_CHECK_TIMEOUT: float = _env_float("JARVIS_RESOURCE_CHECK_TIMEOUT", default=30.0)
BACKEND_STARTUP_TIMEOUT: float = _env_float("JARVIS_BACKEND_STARTUP_TIMEOUT", default=300.0)
TRINITY_TIMEOUT: float = _env_float("JARVIS_TRINITY_TIMEOUT", default=600.0)
ENTERPRISE_SERVICE_TIMEOUT: float = _env_float("JARVIS_ENTERPRISE_SERVICE_TIMEOUT", default=30.0)

# --- Health checks ---
HEALTH_CHECK_TIMEOUT: float = _env_float("JARVIS_HEALTH_CHECK_TIMEOUT", default=10.0)
HEALTH_CHECK_INTERVAL: float = _env_float("JARVIS_HEALTH_CHECK_INTERVAL", default=30.0)

# --- GCP ---
GCP_PROBE_TIMEOUT: float = _env_float("JARVIS_GCP_PROBE_TIMEOUT", default=15.0)
GCP_RECOVERY_TIMEOUT: float = _env_float("JARVIS_GCP_RECOVERY_TIMEOUT", default=450.0)

# --- Progress ---
PROGRESS_POLL_INTERVAL: float = _env_float("JARVIS_PROGRESS_POLL_INTERVAL", default=15.0)
PROGRESS_HEARTBEAT_INTERVAL: float = _env_float("JARVIS_PROGRESS_HEARTBEAT_INTERVAL", default=5.0)

# --- Phase hold ---
PHASE_HOLD_HARD_CAP: float = _env_float("JARVIS_PHASE_HOLD_HARD_CAP", default=300.0)


# =========================================================================
# MEMORY THRESHOLDS
# =========================================================================

SPAWN_ADMISSION_MIN_GB: float = _env_float("JARVIS_SPAWN_ADMISSION_MIN_GB", default=1.5)
PLANNED_ML_GB: float = _env_float("JARVIS_PLANNED_ML_GB", default=4.6)
MEMORY_PRESSURE_THRESHOLD: float = _env_float("JARVIS_MEMORY_PRESSURE_THRESHOLD", default=0.85)


# =========================================================================
# RETRY CONFIGURATION
# =========================================================================

DEFAULT_MAX_RETRIES: int = _env_int("JARVIS_DEFAULT_MAX_RETRIES", default=3)
DEFAULT_RETRY_DELAY: float = _env_float("JARVIS_DEFAULT_RETRY_DELAY", default=5.0)


# =========================================================================
# URL BASES (constructed from port constants)
# =========================================================================

BACKEND_URL: str = os.environ.get(
    "JARVIS_BACKEND_URL", f"http://localhost:{BACKEND_PORT}"
)
FRONTEND_URL: str = os.environ.get(
    "JARVIS_FRONTEND_URL", f"http://localhost:{FRONTEND_PORT}"
)
