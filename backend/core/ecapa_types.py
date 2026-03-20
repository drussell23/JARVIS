"""ECAPA lifecycle facade — shared types, config, and error hierarchy.

All other ECAPA facade modules import from here. No heavy dependencies are
required at module level so this file loads quickly even without numpy/torch
installed.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional


# ---------------------------------------------------------------------------
# State & Tier enums
# ---------------------------------------------------------------------------

class EcapaState(Enum):
    """Lifecycle states for the ECAPA speaker-embedding backend."""
    UNINITIALIZED = "uninitialized"
    PROBING = "probing"
    LOADING = "loading"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    RECOVERING = "recovering"


class EcapaTier(Enum):
    """Coarse capability tier exposed to callers."""
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


# Map every EcapaState to exactly one EcapaTier.
STATE_TO_TIER: Dict[EcapaState, EcapaTier] = {
    EcapaState.UNINITIALIZED: EcapaTier.UNAVAILABLE,
    EcapaState.PROBING:       EcapaTier.UNAVAILABLE,
    EcapaState.LOADING:       EcapaTier.UNAVAILABLE,
    EcapaState.READY:         EcapaTier.READY,
    EcapaState.DEGRADED:      EcapaTier.DEGRADED,
    EcapaState.UNAVAILABLE:   EcapaTier.UNAVAILABLE,
    EcapaState.RECOVERING:    EcapaTier.UNAVAILABLE,
}

# Sanity-check at import time that every state is covered.
assert set(STATE_TO_TIER) == set(EcapaState), "STATE_TO_TIER is missing states"


# ---------------------------------------------------------------------------
# Voice capability enum
# ---------------------------------------------------------------------------

class VoiceCapability(Enum):
    """Fine-grained capabilities that callers can gate on."""
    VOICE_UNLOCK        = "CAP_VOICE_UNLOCK"
    AUTH_COMMAND        = "CAP_AUTH_COMMAND"
    BASIC_COMMAND       = "CAP_BASIC_COMMAND"
    ENROLLMENT          = "CAP_ENROLLMENT"
    LEARNING_WRITE      = "CAP_LEARNING_WRITE"
    PROFILE_READ        = "CAP_PROFILE_READ"
    EXTRACT_EMBEDDING   = "CAP_EXTRACT_EMBEDDING"
    PASSWORD_FALLBACK   = "CAP_PASSWORD_FALLBACK"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    return int(val) if val is not None else default


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    return float(val) if val is not None else default


@dataclass
class EcapaFacadeConfig:
    """Runtime-tunable knobs for the ECAPA lifecycle facade.

    All fields can be overridden via environment variables so that nothing is
    hard-coded in source.
    """
    # Circuit-breaker thresholds
    failure_threshold: int = 3
    recovery_threshold: int = 3
    recovering_fail_threshold: int = 2

    # Timing
    transition_cooldown_s: float = 2.0
    reprobe_interval_s: float = 30.0
    reprobe_max_backoff_s: float = 300.0
    probe_timeout_s: float = 8.0
    local_load_timeout_s: float = 60.0

    # Budget
    reprobe_budget: int = 10
    max_concurrent_extractions: int = 4

    @classmethod
    def from_env(cls) -> "EcapaFacadeConfig":
        """Construct config reading from environment variables with safe defaults."""
        return cls(
            failure_threshold=_env_int("ECAPA_FAILURE_THRESHOLD", 3),
            recovery_threshold=_env_int("ECAPA_RECOVERY_THRESHOLD", 3),
            recovering_fail_threshold=_env_int("ECAPA_RECOVERING_FAIL_THRESHOLD", 2),
            transition_cooldown_s=_env_float("ECAPA_TRANSITION_COOLDOWN_S", 2.0),
            reprobe_interval_s=_env_float("ECAPA_REPROBE_INTERVAL_S", 30.0),
            reprobe_max_backoff_s=_env_float("ECAPA_REPROBE_MAX_BACKOFF_S", 300.0),
            probe_timeout_s=_env_float("ECAPA_PROBE_TIMEOUT_S", 8.0),
            local_load_timeout_s=_env_float("ECAPA_LOCAL_LOAD_TIMEOUT_S", 60.0),
            reprobe_budget=_env_int("ECAPA_REPROBE_BUDGET", 10),
            max_concurrent_extractions=_env_int("ECAPA_MAX_CONCURRENT_EXTRACTIONS", 4),
        )


# ---------------------------------------------------------------------------
# Result / check dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EmbeddingResult:
    """Return value from a speaker-embedding extraction call.

    ``embedding`` is typed as ``Optional[Any]`` to avoid a hard numpy import
    at module level; callers that need array operations should cast after
    checking ``success``.
    """
    embedding: Optional[Any]
    backend: str
    latency_ms: float
    from_cache: bool
    dimension: int
    error: Optional[str]

    @property
    def success(self) -> bool:
        return self.error is None and self.embedding is not None


@dataclass(frozen=True)
class CapabilityCheck:
    """Result of a per-capability admission check."""
    allowed: bool
    tier: EcapaTier
    reason_code: str
    constraints: FrozenSet[str] = field(default_factory=frozenset)
    fallback: Optional[str] = None
    root_cause_id: Optional[str] = None


@dataclass(frozen=True)
class EcapaStateEvent:
    """Immutable record emitted on every state transition."""
    event_id: str
    root_cause_id: str
    timestamp: float
    warning_code: str
    previous_state: EcapaState
    new_state: EcapaState
    tier: EcapaTier
    active_backend: str
    reason: str
    error_class: Optional[str] = None
    latency_ms: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make(
        cls,
        *,
        previous_state: EcapaState,
        new_state: EcapaState,
        active_backend: str,
        reason: str,
        warning_code: str = "ECAPA_STATE_CHANGE",
        error_class: Optional[str] = None,
        latency_ms: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "EcapaStateEvent":
        import time
        return cls(
            event_id=str(uuid.uuid4()),
            root_cause_id=str(uuid.uuid4()),
            timestamp=time.time(),
            warning_code=warning_code,
            previous_state=previous_state,
            new_state=new_state,
            tier=STATE_TO_TIER[new_state],
            active_backend=active_backend,
            reason=reason,
            error_class=error_class,
            latency_ms=latency_ms,
            metadata=metadata or {},
        )


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class EcapaError(Exception):
    """Base exception for all ECAPA facade errors."""


class EcapaUnavailableError(EcapaError):
    """Raised when the ECAPA backend is not available to serve a request."""


class EcapaOverloadError(EcapaError):
    """Raised when the ECAPA backend is at capacity.

    Attributes:
        retry_after_s: Suggested number of seconds to wait before retrying.
    """

    def __init__(self, retry_after_s: float = 1.0, message: Optional[str] = None) -> None:
        self.retry_after_s = retry_after_s
        msg = message or f"ECAPA backend overloaded; retry after {retry_after_s}s"
        super().__init__(msg)


class EcapaTimeoutError(EcapaError):
    """Raised when an ECAPA operation exceeds its deadline."""
