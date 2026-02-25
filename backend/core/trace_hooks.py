"""Trace Lifecycle Hooks — thin adapter between supervisor and traceability.

Provides fire-and-forget functions that the supervisor calls at phase
boundaries.  Each function is a no-op if the traceability subsystem
has not been initialized (graceful degradation).

Usage in supervisor:
    from backend.core.trace_hooks import on_boot_start, on_phase_enter, on_phase_exit

    on_boot_start()
    on_phase_enter("resources", progress=35)
    ...
    on_phase_exit("resources", progress=52, success=True)
    ...
    on_boot_complete()
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _get_emitter():
    """Lazy import to avoid circular deps."""
    try:
        from backend.core.trace_bootstrap import get_lifecycle_emitter
        return get_lifecycle_emitter()
    except ImportError:
        return None


def on_boot_start(boot_id: str = "", metadata: Optional[Dict[str, Any]] = None) -> None:
    """Call once at the very start of _startup_impl()."""
    emitter = _get_emitter()
    if emitter is None:
        return
    try:
        meta = {"boot_id": boot_id} if boot_id else {}
        if metadata:
            meta.update(metadata)
        emitter.boot_start(metadata=meta or None)
    except Exception:
        logger.debug("Failed to emit boot_start", exc_info=True)


def on_boot_complete(duration_s: float = 0.0, metadata: Optional[Dict[str, Any]] = None) -> None:
    """Call when startup completes successfully."""
    emitter = _get_emitter()
    if emitter is None:
        return
    try:
        meta = {"duration_s": duration_s} if duration_s else {}
        if metadata:
            meta.update(metadata)
        emitter.boot_complete(metadata=meta or None)
    except Exception:
        logger.debug("Failed to emit boot_complete", exc_info=True)


def on_phase_enter(phase: str, progress: int = 0, metadata: Optional[Dict[str, Any]] = None) -> None:
    """Call when entering a startup phase."""
    emitter = _get_emitter()
    if emitter is None:
        return
    try:
        meta = {"progress_pct": progress}
        if metadata:
            meta.update(metadata)
        emitter.phase_enter(phase, metadata=meta)
    except Exception:
        logger.debug(f"Failed to emit phase_enter({phase})", exc_info=True)


def on_phase_exit(
    phase: str, progress: int = 0, success: bool = True,
    duration_s: float = 0.0,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Call when exiting a startup phase."""
    emitter = _get_emitter()
    if emitter is None:
        return
    try:
        meta = {"progress_pct": progress, "duration_s": duration_s}
        if metadata:
            meta.update(metadata)
        emitter.phase_exit(phase, success=success, metadata=meta)
    except Exception:
        logger.debug(f"Failed to emit phase_exit({phase})", exc_info=True)


def on_phase_fail(phase: str, error: str, evidence: Optional[Dict[str, Any]] = None) -> None:
    """Call when a phase fails."""
    emitter = _get_emitter()
    if emitter is None:
        return
    try:
        emitter.phase_fail(phase, error=error, evidence=evidence)
    except Exception:
        logger.debug(f"Failed to emit phase_fail({phase})", exc_info=True)


def on_boot_failed(
    error: str, phase: str = "", duration_s: float = 0.0,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Call when startup fails (exception or abort)."""
    emitter = _get_emitter()
    if emitter is None:
        return
    try:
        meta = {"phase": phase, "duration_s": duration_s}
        if metadata:
            meta.update(metadata)
        emitter.boot_failed(error=error, metadata=meta)
    except Exception:
        logger.debug("Failed to emit boot_failed", exc_info=True)


def on_shutdown(reason: str = "") -> None:
    """Call at the start of shutdown."""
    emitter = _get_emitter()
    if emitter is None:
        return
    try:
        emitter.shutdown_start(reason=reason)
    except Exception:
        logger.debug("Failed to emit shutdown_start", exc_info=True)


def on_recovery_start(component: str, reason: str, caused_by_event_id: Optional[str] = None) -> None:
    """Call when a recovery sequence begins."""
    emitter = _get_emitter()
    if emitter is None:
        return
    try:
        emitter.recovery_start(component, reason, caused_by_event_id=caused_by_event_id)
    except Exception:
        logger.debug(f"Failed to emit recovery_start({component})", exc_info=True)


def on_recovery_complete(component: str, outcome: str) -> None:
    """Call when a recovery sequence completes."""
    emitter = _get_emitter()
    if emitter is None:
        return
    try:
        emitter.recovery_complete(component, outcome)
    except Exception:
        logger.debug(f"Failed to emit recovery_complete({component})", exc_info=True)
