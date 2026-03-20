"""
Reasoning Activation Gate
=========================

7-state FSM that controls whether the reasoning chain accepts commands.
Uses capability-scoped gating: reasoning activates only when critical
dependencies (J-Prime + specific agents) are healthy.

Non-critical agents run independently and are not gated.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


CRITICAL_FOR_REASONING: Set[str] = {
    "jprime_lifecycle",
    "coordinator_agent",
    "predictive_planner",
    "proactive_detector",
}


class GateState(str, Enum):
    DISABLED = "DISABLED"
    WAITING_DEPS = "WAITING_DEPS"
    READY = "READY"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    TERMINAL = "TERMINAL"

    @property
    def accepts_commands(self) -> bool:
        return self in (GateState.ACTIVE, GateState.DEGRADED)


class DepStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class DepHealth:
    name: str
    status: DepStatus
    last_check: float = 0.0
    response_time_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class GateConfig:
    activation_dwell_s: float = 5.0
    min_state_dwell_s: float = 3.0
    degrade_threshold: int = 3
    block_threshold: int = 3
    recovery_threshold: int = 3
    max_block_duration_s: float = 300.0
    terminal_cooldown_s: float = 900.0
    dep_poll_interval_s: float = 10.0

    @classmethod
    def from_env(cls) -> GateConfig:
        return cls(
            activation_dwell_s=_env_float("REASONING_ACTIVATION_DWELL_S", 5.0),
            min_state_dwell_s=_env_float("REASONING_MIN_DWELL_S", 3.0),
            degrade_threshold=_env_int("REASONING_DEGRADE_THRESHOLD", 3),
            block_threshold=_env_int("REASONING_BLOCK_THRESHOLD", 3),
            recovery_threshold=_env_int("REASONING_RECOVERY_THRESHOLD", 3),
            max_block_duration_s=_env_float("REASONING_MAX_BLOCK_S", 300.0),
            terminal_cooldown_s=_env_float("REASONING_TERMINAL_COOLDOWN_S", 900.0),
            dep_poll_interval_s=_env_float("REASONING_DEP_POLL_S", 10.0),
        )


DEGRADED_OVERRIDES: Dict[str, float] = {
    "proactive_threshold_boost": 0.1,
    "auto_expand_threshold": 1.0,
    "expansion_timeout_factor": 0.5,
    "mind_request_timeout_factor": 0.5,
}
