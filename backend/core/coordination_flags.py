"""
Coordination Flags — Context-managed environment variable flags.

Replaces manual os.environ.pop() in 7+ exception paths with a single
async with block that guarantees cleanup on exit, error, or cancellation.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

logger = logging.getLogger(__name__)


@asynccontextmanager
async def coordination_flag(name: str, value: str = "true"):
    """
    Set an environment variable for cross-component coordination.
    Guaranteed cleanup via finally — no manual pop() in each exception path.

    Usage:
        async with coordination_flag("JARVIS_INVINCIBLE_NODE_BOOTING"):
            result = await boot_invincible_node()
        # Flag automatically cleared on success, error, timeout, or cancellation
    """
    os.environ[name] = value
    logger.debug("[CoordFlag] SET %s=%s", name, value)
    try:
        yield
    finally:
        os.environ.pop(name, None)
        logger.debug("[CoordFlag] CLEARED %s", name)


def is_flag_set(name: str) -> bool:
    """Check if a coordination flag is currently set."""
    return os.getenv(name, "").lower() in ("true", "1", "yes")
