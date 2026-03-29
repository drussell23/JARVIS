"""Environment-driven configuration dataclass for the Ouroboros Daemon (Zone 7.0).

All settings have sensible defaults and can be overridden via environment
variables.  The dataclass is frozen so that configuration is treated as an
immutable value after construction.

Usage::

    from backend.core.ouroboros.daemon_config import DaemonConfig

    cfg = DaemonConfig.from_env()          # reads os.environ
    cfg = DaemonConfig()                   # all defaults
    cfg = DaemonConfig(rem_max_agents=5)   # selective override
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TRUTHY = frozenset({"true", "1", "yes"})
_FALSY = frozenset({"false", "0", "no"})


def _parse_bool(env_name: str, raw: str) -> bool:
    """Parse a boolean environment variable using the documented rules.

    Raises
    ------
    ValueError
        If *raw* is not a recognised truthy or falsy string.
    """
    lower = raw.lower()
    if lower in _TRUTHY:
        return True
    if lower in _FALSY:
        return False
    raise ValueError(
        f"Environment variable {env_name!r} has unrecognised boolean value "
        f"{raw!r}. Expected one of: "
        f"{sorted(_TRUTHY | _FALSY)}"
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return _parse_bool(name, raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


# ---------------------------------------------------------------------------
# DaemonConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DaemonConfig:
    """Immutable configuration for the Ouroboros Daemon.

    All fields default to the values prescribed in the specification.  Use
    :meth:`from_env` to construct a config from environment variables.

    Env-var mapping
    ---------------
    daemon_enabled              → OUROBOROS_DAEMON_ENABLED
    vital_scan_timeout_s        → OUROBOROS_VITAL_SCAN_TIMEOUT_S
    spinal_timeout_s            → OUROBOROS_SPINAL_TIMEOUT_S
    rem_enabled                 → OUROBOROS_REM_ENABLED
    rem_cycle_timeout_s         → OUROBOROS_REM_CYCLE_TIMEOUT_S
    rem_epoch_timeout_s         → OUROBOROS_REM_EPOCH_TIMEOUT_S
    rem_max_agents              → OUROBOROS_REM_MAX_AGENTS
    rem_max_findings_per_epoch  → OUROBOROS_REM_MAX_FINDINGS
    rem_cooldown_s              → OUROBOROS_REM_COOLDOWN_S
    rem_idle_eligible_s         → OUROBOROS_REM_IDLE_ELIGIBLE_S
    exploration_model_enabled   → OUROBOROS_EXPLORATION_MODEL_ENABLED
    exploration_model_rpm       → OUROBOROS_EXPLORATION_MODEL_RPM
    """

    # General daemon
    daemon_enabled: bool = True
    vital_scan_timeout_s: float = 30.0
    spinal_timeout_s: float = 10.0

    # REM sleep cycle
    rem_enabled: bool = True
    rem_cycle_timeout_s: float = 300.0
    rem_epoch_timeout_s: float = 1800.0
    rem_max_agents: int = 30
    rem_max_findings_per_epoch: int = 10
    rem_cooldown_s: float = 3600.0
    rem_idle_eligible_s: float = 60.0

    # Exploration model (optional external LLM)
    exploration_model_enabled: bool = False
    exploration_model_rpm: int = 10

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "DaemonConfig":
        """Construct a :class:`DaemonConfig` from environment variables.

        Any variable that is not set falls back to the field default.
        """
        return cls(
            daemon_enabled=_env_bool("OUROBOROS_DAEMON_ENABLED", True),
            vital_scan_timeout_s=_env_float("OUROBOROS_VITAL_SCAN_TIMEOUT_S", 30.0),
            spinal_timeout_s=_env_float("OUROBOROS_SPINAL_TIMEOUT_S", 10.0),
            rem_enabled=_env_bool("OUROBOROS_REM_ENABLED", True),
            rem_cycle_timeout_s=_env_float("OUROBOROS_REM_CYCLE_TIMEOUT_S", 300.0),
            rem_epoch_timeout_s=_env_float("OUROBOROS_REM_EPOCH_TIMEOUT_S", 1800.0),
            rem_max_agents=_env_int("OUROBOROS_REM_MAX_AGENTS", 30),
            rem_max_findings_per_epoch=_env_int("OUROBOROS_REM_MAX_FINDINGS", 10),
            rem_cooldown_s=_env_float("OUROBOROS_REM_COOLDOWN_S", 3600.0),
            rem_idle_eligible_s=_env_float("OUROBOROS_REM_IDLE_ELIGIBLE_S", 60.0),
            exploration_model_enabled=_env_bool(
                "OUROBOROS_EXPLORATION_MODEL_ENABLED", False
            ),
            exploration_model_rpm=_env_int("OUROBOROS_EXPLORATION_MODEL_RPM", 10),
        )
