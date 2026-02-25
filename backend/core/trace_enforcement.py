"""Boundary Enforcement Middleware v1.0

Enforces TraceEnvelope presence at boundary crossings with staged rollout.
Three modes: STRICT (reject), CANARY (warn+proceed), PERMISSIVE (log+proceed).

Also provides:
- inject/extract trace headers for HTTP boundaries
- inject/extract trace env vars for subprocess boundaries
- ComplianceTracker for monitoring instrumentation coverage
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
from enum import Enum
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from backend.core.trace_envelope import TraceEnvelope
    _TRACE_ENVELOPE_AVAILABLE = True
except ImportError:
    _TRACE_ENVELOPE_AVAILABLE = False
    TraceEnvelope = None  # type: ignore[assignment,misc]

try:
    from backend.core.resilience.correlation_context import get_current_context
    _CORRELATION_AVAILABLE = True
except ImportError:
    _CORRELATION_AVAILABLE = False
    get_current_context = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Enforcement Mode
# ---------------------------------------------------------------------------

class EnforcementMode(str, Enum):
    STRICT = "strict"
    CANARY = "canary"
    PERMISSIVE = "permissive"


_enforcement_lock = threading.Lock()
_enforcement_mode: EnforcementMode = EnforcementMode.PERMISSIVE

_env_mode = os.environ.get("JARVIS_TRACE_ENFORCEMENT", "").lower()
if _env_mode in ("strict", "canary", "permissive"):
    _enforcement_mode = EnforcementMode(_env_mode)


def set_enforcement_mode(mode: EnforcementMode) -> None:
    global _enforcement_mode
    with _enforcement_lock:
        _enforcement_mode = mode


def get_enforcement_mode() -> EnforcementMode:
    with _enforcement_lock:
        return _enforcement_mode


# ---------------------------------------------------------------------------
# Violation counter
# ---------------------------------------------------------------------------

_violation_count: int = 0
_violation_lock = threading.Lock()


def get_violation_count() -> int:
    with _violation_lock:
        return _violation_count


def reset_violation_count() -> None:
    """Reset the violation counter (primarily for testing)."""
    global _violation_count
    with _violation_lock:
        _violation_count = 0


def _increment_violations() -> None:
    global _violation_count
    with _violation_lock:
        _violation_count += 1


# ---------------------------------------------------------------------------
# Enforcement error
# ---------------------------------------------------------------------------

class TraceEnforcementError(Exception):
    """Raised in STRICT mode when a boundary crossing lacks a valid TraceEnvelope."""
    pass


# ---------------------------------------------------------------------------
# enforce_trace decorator
# ---------------------------------------------------------------------------

def enforce_trace(
    boundary_type: str = "internal",
    classification: str = "standard",
) -> Callable:
    """Async decorator that checks for TraceEnvelope presence at boundary crossings.

    In STRICT mode, raises TraceEnforcementError if no envelope is found.
    In CANARY mode, logs a warning and increments the violation counter.
    In PERMISSIVE mode, logs at debug level and increments the counter.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            mode = get_enforcement_mode()
            has_envelope = False

            if _CORRELATION_AVAILABLE and get_current_context is not None:
                ctx = get_current_context()
                if ctx is not None:
                    envelope = getattr(ctx, "envelope", None)
                    if envelope is not None:
                        has_envelope = True

            if not has_envelope:
                msg = (
                    f"Trace enforcement violation: {fn.__qualname__} "
                    f"(boundary={boundary_type}, classification={classification}) "
                    f"called without TraceEnvelope in context"
                )
                if mode == EnforcementMode.STRICT:
                    _increment_violations()
                    raise TraceEnforcementError(msg)
                elif mode == EnforcementMode.CANARY:
                    _increment_violations()
                    logger.warning(msg)
                else:  # PERMISSIVE
                    _increment_violations()
                    logger.debug(msg)

            return await fn(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# HTTP header injection/extraction
# ---------------------------------------------------------------------------

def inject_trace_headers(headers: Dict[str, str], envelope: Any) -> None:
    """Inject TraceEnvelope fields into HTTP headers."""
    if envelope is None:
        return
    h = envelope.to_headers()
    headers.update(h)


def extract_trace_from_headers(headers: Dict[str, str]) -> Optional[Any]:
    """Extract a TraceEnvelope from HTTP headers."""
    if not _TRACE_ENVELOPE_AVAILABLE or TraceEnvelope is None:
        return None
    return TraceEnvelope.from_headers(headers)


# ---------------------------------------------------------------------------
# Env var injection/extraction (subprocess boundaries)
# ---------------------------------------------------------------------------

def inject_trace_env_var(env_dict: Dict[str, str], envelope: Any) -> None:
    """Inject TraceEnvelope as JSON env var (for subprocess boundaries)."""
    if envelope is None:
        return
    try:
        serialized = json.dumps(envelope.to_dict(), separators=(",", ":"))
        if len(serialized) <= 4096:
            env_dict["JARVIS_TRACE_ENVELOPE"] = serialized
    except Exception:
        logger.debug("Failed to inject trace envelope env var", exc_info=True)


def extract_trace_env_var() -> Optional[Any]:
    """Extract TraceEnvelope from JARVIS_TRACE_ENVELOPE env var."""
    if not _TRACE_ENVELOPE_AVAILABLE or TraceEnvelope is None:
        return None
    raw = os.environ.get("JARVIS_TRACE_ENVELOPE")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return TraceEnvelope.from_dict(data)
    except Exception:
        logger.debug("Failed to extract trace envelope from env var", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# ComplianceTracker
# ---------------------------------------------------------------------------

class ComplianceTracker:
    """Tracks registered vs instrumented boundaries for compliance scoring."""

    def __init__(self) -> None:
        self._boundaries: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def register_boundary(self, name: str, classification: str = "standard") -> None:
        """Register a boundary that should have trace enforcement."""
        with self._lock:
            self._boundaries[name] = {
                "classification": classification,
                "instrumented": False,
            }

    def mark_instrumented(self, name: str) -> None:
        """Mark a registered boundary as instrumented."""
        with self._lock:
            if name in self._boundaries:
                self._boundaries[name]["instrumented"] = True

    def get_score(self) -> Dict[str, Any]:
        """Compute compliance score."""
        with self._lock:
            boundaries = dict(self._boundaries)

        total = len(boundaries)
        instrumented = sum(1 for b in boundaries.values() if b["instrumented"])
        critical_total = sum(1 for b in boundaries.values() if b["classification"] == "critical")
        critical_instrumented = sum(
            1 for b in boundaries.values()
            if b["classification"] == "critical" and b["instrumented"]
        )

        return {
            "total_boundaries": total,
            "instrumented": instrumented,
            "score_overall": round((instrumented / total * 100) if total > 0 else 0.0, 1),
            "critical_total": critical_total,
            "critical_instrumented": critical_instrumented,
            "score_critical": round(
                (critical_instrumented / critical_total * 100) if critical_total > 0 else 0.0, 1
            ),
        }

    def ci_gate_passes(self, overall_threshold: float = 80.0) -> bool:
        """Check if compliance meets CI gate requirements.

        Gate passes when:
        - All critical boundaries are instrumented (100%)
        - Overall score >= overall_threshold (default 80%)
        """
        score = self.get_score()
        if score["critical_total"] > 0 and score["score_critical"] < 100.0:
            return False
        return score["score_overall"] >= overall_threshold

    def to_json(self) -> str:
        """Serialize compliance score to JSON string for CI output."""
        import json
        score = self.get_score()
        score["ci_gate_passes"] = self.ci_gate_passes()
        return json.dumps(score, indent=2)
