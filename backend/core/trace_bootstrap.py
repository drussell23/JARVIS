"""TraceBootstrap — Centralized initialization of the traceability subsystem.

Provides singleton access to LifecycleEmitter, SpanRecorder, and
TraceEnvelopeFactory.  Call initialize() once at startup; all subsequent
get_*() calls return the same instances.

Environment Variables:
    JARVIS_TRACE_DIR            Trace output directory (default: ~/.jarvis/traces)
    JARVIS_BOOT_ID              Boot identifier
    JARVIS_RUNTIME_EPOCH_ID     Runtime epoch identifier
    JARVIS_NODE_ID              Node identifier (default: hostname)
    JARVIS_VERSION              Producer version (default: dev)
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from backend.core.trace_envelope import TraceEnvelopeFactory
    from backend.core.lifecycle_emitter import LifecycleEmitter
    from backend.core.span_recorder import SpanRecorder
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    TraceEnvelopeFactory = None  # type: ignore[assignment,misc]
    LifecycleEmitter = None  # type: ignore[assignment,misc]
    SpanRecorder = None  # type: ignore[assignment,misc]


_lock = threading.Lock()
_factory: Optional["TraceEnvelopeFactory"] = None
_emitter: Optional["LifecycleEmitter"] = None
_recorder: Optional["SpanRecorder"] = None
_initialized = False


def initialize(
    trace_dir: Optional[Path] = None,
    boot_id: Optional[str] = None,
    runtime_epoch_id: Optional[str] = None,
    node_id: Optional[str] = None,
    producer_version: Optional[str] = None,
) -> bool:
    """Initialize the traceability subsystem.  Idempotent — second call is a no-op.

    Returns True if initialization succeeded, False if trace modules unavailable.
    """
    global _factory, _emitter, _recorder, _initialized

    with _lock:
        if _initialized:
            return _factory is not None

        if not _AVAILABLE:
            logger.debug("Trace modules unavailable — traceability disabled")
            _initialized = True
            return False

        _trace_dir = trace_dir or Path(
            os.environ.get("JARVIS_TRACE_DIR", os.path.expanduser("~/.jarvis/traces"))
        )
        _trace_dir.mkdir(parents=True, exist_ok=True)

        _boot_id = boot_id or os.environ.get("JARVIS_BOOT_ID", uuid.uuid4().hex[:16])
        _epoch_id = runtime_epoch_id or os.environ.get(
            "JARVIS_RUNTIME_EPOCH_ID", uuid.uuid4().hex[:16]
        )
        _node_id = node_id or os.environ.get("JARVIS_NODE_ID", os.uname().nodename)
        _version = producer_version or os.environ.get("JARVIS_VERSION", "dev")

        _factory = TraceEnvelopeFactory(
            repo="jarvis",
            boot_id=_boot_id,
            runtime_epoch_id=_epoch_id,
            node_id=_node_id,
            producer_version=_version,
        )

        _emitter = LifecycleEmitter(
            trace_dir=_trace_dir,
            envelope_factory=_factory,
        )

        _recorder = SpanRecorder(
            trace_dir=_trace_dir,
            envelope_factory=_factory,
        )

        _initialized = True
        logger.info(
            f"Traceability initialized: boot_id={_boot_id}, "
            f"epoch={_epoch_id}, dir={_trace_dir}"
        )
        return True


def get_lifecycle_emitter() -> Optional["LifecycleEmitter"]:
    return _emitter


def get_span_recorder() -> Optional["SpanRecorder"]:
    return _recorder


def get_envelope_factory() -> Optional["TraceEnvelopeFactory"]:
    return _factory


def shutdown() -> None:
    """Flush and close the traceability subsystem."""
    if _emitter is not None:
        try:
            _emitter.close()
        except Exception:
            logger.debug("Error closing lifecycle emitter", exc_info=True)
    if _recorder is not None:
        try:
            _recorder.flush()
        except Exception:
            logger.debug("Error flushing span recorder", exc_info=True)


def _reset() -> None:
    """Reset all state. For testing only."""
    global _factory, _emitter, _recorder, _initialized
    with _lock:
        if _emitter is not None:
            try:
                _emitter.close()
            except Exception:
                pass
        _factory = None
        _emitter = None
        _recorder = None
        _initialized = False
