"""
Shared Async System Metrics Service v258.0

Provides non-blocking access to system metrics (CPU, memory, disk) from a single
background daemon thread. Replaces 22+ scattered `psutil.cpu_percent(interval=0.1)`
calls that each block the event loop for 100ms.

Architecture:
- Single daemon thread refreshes metrics every JARVIS_CPU_METRICS_REFRESH_INTERVAL seconds
- GIL guarantees atomic reference assignment — readers always see a complete snapshot
  (old or new), never partially-constructed (R2-#10)
- Daemon thread exits with process — no shutdown race (R2-#2)
- Cross-namespace singleton via sys attribute to avoid dual-module aliasing

Usage (async context):
    from core.async_system_metrics import get_cpu_percent
    cpu = await get_cpu_percent()

Usage (sync/thread context):
    from core.async_system_metrics import get_cpu_percent_cached
    cpu = get_cpu_percent_cached()
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("jarvis.async_system_metrics")

# ---------------------------------------------------------------------------
# Configuration (env-var overridable)
# ---------------------------------------------------------------------------

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, ""))
    except (ValueError, TypeError):
        return default


_REFRESH_INTERVAL = _env_float("JARVIS_CPU_METRICS_REFRESH_INTERVAL", 2.0)
_MEASUREMENT_INTERVAL = _env_float("JARVIS_CPU_METRICS_MEASUREMENT_INTERVAL", 0.1)
_MAX_CACHE_AGE = _env_float("JARVIS_CPU_METRICS_MAX_CACHE_AGE", 5.0)

# Cross-namespace singleton attribute name
_SYS_ATTR = "_jarvis_metrics_service"


# ---------------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------------

@dataclass
class SystemMetricsSnapshot:
    """Immutable-ish snapshot of system metrics."""
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    memory_available_gb: float = 0.0
    disk_percent: float = 0.0
    disk_free_gb: float = 0.0
    timestamp: float = 0.0
    source: str = "init"  # "init" until first real measurement, then "fresh"


# ---------------------------------------------------------------------------
# Service implementation
# ---------------------------------------------------------------------------

class _AsyncSystemMetricsService:
    """Background daemon thread that periodically refreshes system metrics.

    Design decisions (R2 review):
    - Daemon thread instead of run_in_executor: avoids R2-#2 (executor thread
      outliving event loop during shutdown).
    - threading.Event for stop signal: clean cooperative shutdown.
    - GIL-atomic reference swap for snapshot: no locks needed for readers (R2-#10).
    """

    def __init__(self) -> None:
        self._snapshot = SystemMetricsSnapshot()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False

    # -- Properties ----------------------------------------------------------

    @property
    def snapshot(self) -> SystemMetricsSnapshot:
        return self._snapshot

    @property
    def cpu_percent(self) -> float:
        return self._snapshot.cpu_percent

    @property
    def memory_percent(self) -> float:
        return self._snapshot.memory_percent

    @property
    def memory_available_gb(self) -> float:
        return self._snapshot.memory_available_gb

    # -- Background thread ---------------------------------------------------

    def _refresh_loop_thread(self) -> None:
        """Daemon thread: periodically refreshes metrics. No event loop dependency."""
        import psutil  # Import inside thread to avoid module-level side effects

        while not self._stop_event.is_set():
            try:
                snap = SystemMetricsSnapshot(source="fresh")
                snap.cpu_percent = psutil.cpu_percent(interval=_MEASUREMENT_INTERVAL)

                mem = psutil.virtual_memory()
                snap.memory_percent = mem.percent
                snap.memory_available_gb = mem.available / (1024 ** 3)

                disk = psutil.disk_usage("/")
                snap.disk_percent = disk.percent
                snap.disk_free_gb = disk.free / (1024 ** 3)

                snap.timestamp = time.monotonic()

                # GIL guarantees atomic reference assignment — readers always see
                # a complete snapshot (old or new), never partially-constructed. (R2-#10)
                self._snapshot = snap
            except Exception:
                pass  # Non-fatal — stale data is better than crashing

            self._stop_event.wait(_REFRESH_INTERVAL)

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start the background metrics refresh thread."""
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._refresh_loop_thread,
            name="jarvis-metrics-refresh",
            daemon=True,  # R2-#2: daemon thread exits with process, no shutdown race
        )
        self._thread.start()
        logger.debug("Metrics refresh thread started (interval=%.1fs)", _REFRESH_INTERVAL)

        # Wait for first measurement (up to 0.5s) so callers don't get 0.0
        for _ in range(50):
            if self._snapshot.source == "fresh":
                break
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        """Stop the background metrics refresh thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._started = False
        logger.debug("Metrics refresh thread stopped")


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

def _get_service() -> _AsyncSystemMetricsService:
    """Get or create the cross-namespace singleton service."""
    existing = getattr(sys, _SYS_ATTR, None)
    if existing is not None:
        return existing

    svc = _AsyncSystemMetricsService()
    setattr(sys, _SYS_ATTR, svc)
    return svc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_cpu_percent() -> float:
    """Non-blocking CPU percentage. Returns cached value from background thread.

    Works before start_metrics_service() via psutil.cpu_percent(interval=None)
    fallback (R2-#4: lazy-start, non-blocking instant cached value).
    """
    svc = _get_service()
    if svc._started:
        snap = svc.snapshot
        age = time.monotonic() - snap.timestamp
        if age <= _MAX_CACHE_AGE:
            return snap.cpu_percent
        # R2-#4: Cache too old — fall through to psutil fallback

    # R2-#4: Lazy-start fallback: instant cached value (non-blocking)
    try:
        import psutil
        return psutil.cpu_percent(interval=None)
    except Exception:
        return 0.0


async def get_memory_percent() -> float:
    """Non-blocking memory percentage."""
    svc = _get_service()
    if svc._started:
        snap = svc.snapshot
        age = time.monotonic() - snap.timestamp
        if age <= _MAX_CACHE_AGE:
            return snap.memory_percent

    try:
        import psutil
        return psutil.virtual_memory().percent
    except Exception:
        return 0.0


async def get_snapshot() -> SystemMetricsSnapshot:
    """Get the full metrics snapshot."""
    svc = _get_service()
    return svc.snapshot


def get_cpu_percent_cached() -> float:
    """Sync version for monitoring threads. Always non-blocking.

    Returns the latest cached CPU percentage from the background thread.
    If the service isn't started, returns psutil.cpu_percent(interval=None).
    """
    svc = _get_service()
    if svc._started and svc.snapshot.source == "fresh":
        return svc.cpu_percent

    try:
        import psutil
        return psutil.cpu_percent(interval=None)
    except Exception:
        return 0.0


def get_memory_percent_cached() -> float:
    """Sync version for monitoring threads. Always non-blocking."""
    svc = _get_service()
    if svc._started and svc.snapshot.source == "fresh":
        return svc.memory_percent

    try:
        import psutil
        return psutil.virtual_memory().percent
    except Exception:
        return 0.0


async def start_metrics_service() -> None:
    """Start the shared metrics service. Safe to call multiple times."""
    svc = _get_service()
    await svc.start()


async def stop_metrics_service() -> None:
    """Stop the shared metrics service."""
    svc = _get_service()
    await svc.stop()
