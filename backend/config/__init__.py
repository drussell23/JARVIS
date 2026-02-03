"""
Configuration package for the JARVIS backend application.
==========================================================

This package provides configuration management for the backend application,
including settings for database connections, API configurations, environment
variables, and other application-wide parameters.

The configuration system supports multiple environments (development, testing,
production) and provides a centralized way to manage application settings.

Key Modules:
- startup_timeouts: Centralized timeout configuration for startup/shutdown operations

Usage:
    # Import startup timeouts
    from backend.config import StartupTimeouts, get_timeouts

    # Access the singleton
    timeouts = get_timeouts()
    timeout_val = timeouts.backend_health_timeout

    # Or create a fresh instance
    my_timeouts = StartupTimeouts()
"""

# Startup timeout configuration - centralized timeouts for all operations
from backend.config.startup_timeouts import (
    StartupTimeouts,
    get_timeouts,
    reset_timeouts,
)


__all__ = [
    # Startup timeouts
    "StartupTimeouts",
    "get_timeouts",
    "reset_timeouts",
]
