"""
Pressure-Aware Watchdog v1.0 — Phase 11 Hardening

System pressure detection + pressure-aware timeout gating.

Problem:
    All timeout mechanisms (heartbeat 30s, DLM keepalive 9s, DMS 60s,
    ProgressController 300s) fire independently during a single pressure
    event.  A 30-second filesystem stall triggers FATAL cascading failures
    across ALL levels because none check system state before escalating.

Solution:
    PressureOracle — singleton that samples system pressure at configurable
    intervals (default 5s).  Consumers call ``should_defer_destructive_action()``
    before any kill/restart/release decision.

    When pressure is detected:
    - Heartbeat threshold: scaled by pressure_multiplier (1.0-3.0)
    - DLM keepalive: max_failures extended
    - DMS: escalation deferred beyond "warn"
    - ProgressController: hard cap extended
    - Circuit breakers: recovery_timeout extended

Pressure Sources (sampled):
    - CPU utilization (psutil.cpu_percent)
    - Memory utilization (psutil.virtual_memory)
    - Event loop lag (scheduled callback latency)
    - GC pause detection (gc.callbacks for gen-2 collections)

Constraints:
    - Lightweight: cached pressure state, not per-check syscalls
    - Fail-open: if pressure detection fails, returns UNKNOWN → no deferral
    - Non-blocking: sync check from cached state
    - No new dependencies (psutil already in requirements)

Copyright (c) 2026 JARVIS AI. All rights reserved.
"""

import asyncio
import gc
import logging
import os
import threading
import time
from collections import deque
from enum import IntEnum
from typing import ClassVar, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("jarvis.pressure_watchdog")


# ---------------------------------------------------------------------------
# PressureLevel
# ---------------------------------------------------------------------------

class PressureLevel(IntEnum):
    """
    System pressure levels, ordered by severity.

    UNKNOWN is treated as NONE for gating decisions (fail-open).
    """
    NONE = 0
    LIGHT = 1       # CPU > 85% or memory > 80% or loop lag > 100ms
    MODERATE = 2    # CPU > 95% or memory > 90% or loop lag > 500ms
    SEVERE = 3      # Multiple indicators simultaneously elevated
    UNKNOWN = -1    # Detection failed — treated as NONE (fail-open)


# ---------------------------------------------------------------------------
# PressureSnapshot
# ---------------------------------------------------------------------------

class PressureSnapshot:
    """A single point-in-time pressure measurement."""

    __slots__ = (
        "timestamp_mono",
        "level",
        "cpu_percent",
        "memory_percent",
        "event_loop_lag_ms",
        "gc_pause_detected",
    )

    def __init__(
        self,
        timestamp_mono: float = 0.0,
        level: PressureLevel = PressureLevel.UNKNOWN,
        cpu_percent: float = 0.0,
        memory_percent: float = 0.0,
        event_loop_lag_ms: float = 0.0,
        gc_pause_detected: bool = False,
    ):
        self.timestamp_mono = timestamp_mono
        self.level = level
        self.cpu_percent = cpu_percent
        self.memory_percent = memory_percent
        self.event_loop_lag_ms = event_loop_lag_ms
        self.gc_pause_detected = gc_pause_detected

    def __repr__(self) -> str:
        return (
            f"PressureSnapshot(level={self.level.name}, "
            f"cpu={self.cpu_percent:.0f}%, mem={self.memory_percent:.0f}%, "
            f"loop_lag={self.event_loop_lag_ms:.0f}ms, "
            f"gc_pause={self.gc_pause_detected})"
        )


# ---------------------------------------------------------------------------
# Pressure level thresholds (configurable via env)
# ---------------------------------------------------------------------------

_CPU_LIGHT = float(os.environ.get("JARVIS_PRESSURE_CPU_LIGHT", "85"))
_CPU_MODERATE = float(os.environ.get("JARVIS_PRESSURE_CPU_MODERATE", "95"))
_MEM_LIGHT = float(os.environ.get("JARVIS_PRESSURE_MEM_LIGHT", "80"))
_MEM_MODERATE = float(os.environ.get("JARVIS_PRESSURE_MEM_MODERATE", "90"))
_LAG_LIGHT_MS = float(os.environ.get("JARVIS_PRESSURE_LAG_LIGHT_MS", "100"))
_LAG_MODERATE_MS = float(os.environ.get("JARVIS_PRESSURE_LAG_MODERATE_MS", "500"))

# Pressure multipliers per level (timeout scaling)
_MULTIPLIER_MAP: Dict[PressureLevel, float] = {
    PressureLevel.NONE: 1.0,
    PressureLevel.LIGHT: 1.5,
    PressureLevel.MODERATE: 2.0,
    PressureLevel.SEVERE: 3.0,
    PressureLevel.UNKNOWN: 1.0,  # fail-open
}


# ---------------------------------------------------------------------------
# GC pause tracker (registered once globally)
# ---------------------------------------------------------------------------

_gc_pause_flag = False
_gc_pause_lock = threading.Lock()
_GC_PAUSE_THRESHOLD_S = float(os.environ.get("JARVIS_GC_PAUSE_THRESHOLD", "0.5"))
_gc_callback_registered = False


def _gc_callback(phase: str, info: Dict) -> None:
    """
    gc.callbacks hook that detects long gen-2 collections.

    Called by the GC runtime.  We only care about ``phase == "stop"``
    for generation >= 2.
    """
    global _gc_pause_flag
    if phase == "stop" and info.get("generation", 0) >= 2:
        duration = info.get("elapsed", 0.0)
        if duration >= _GC_PAUSE_THRESHOLD_S:
            with _gc_pause_lock:
                _gc_pause_flag = True
            logger.debug(
                "[PressureOracle] GC gen-%d pause detected: %.3fs",
                info.get("generation", -1),
                duration,
            )


def _ensure_gc_callback() -> None:
    """Register the GC callback exactly once."""
    global _gc_callback_registered
    if _gc_callback_registered:
        return
    try:
        if _gc_callback not in gc.callbacks:
            gc.callbacks.append(_gc_callback)
        _gc_callback_registered = True
    except Exception:
        pass


def _consume_gc_pause() -> bool:
    """Return and clear the GC pause flag."""
    global _gc_pause_flag
    with _gc_pause_lock:
        was_set = _gc_pause_flag
        _gc_pause_flag = False
    return was_set


# ---------------------------------------------------------------------------
# PressureOracle singleton
# ---------------------------------------------------------------------------

class PressureOracle:
    """
    Singleton that samples system pressure and provides gating decisions.

    Consumers call ``should_defer_destructive_action()`` before any
    kill/restart/release decision.  The oracle returns whether the action
    should be deferred and a human-readable reason.

    Usage::

        oracle = PressureOracle.get_instance()
        await oracle.start_sampling()  # once at startup

        # Before a destructive action:
        defer, reason = oracle.should_defer_destructive_action("DMS.restart")
        if defer:
            logger.info("Deferring restart: %s", reason)
    """

    _instance: ClassVar[Optional["PressureOracle"]] = None
    _instance_lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(
        self,
        sample_interval: float = 5.0,
        history_maxlen: int = 60,
    ):
        self._sample_interval = float(
            os.environ.get("JARVIS_PRESSURE_SAMPLE_INTERVAL", str(sample_interval))
        )
        self._latest = PressureSnapshot()
        self._history: Deque[PressureSnapshot] = deque(maxlen=history_maxlen)
        self._sampling_task: Optional[asyncio.Task] = None
        self._running = False
        self._data_lock = threading.Lock()

        # Self-register as singleton if not already set
        with PressureOracle._instance_lock:
            if PressureOracle._instance is None:
                PressureOracle._instance = self

        _ensure_gc_callback()

    @classmethod
    def get_instance(cls) -> "PressureOracle":
        """Get or create the singleton. Thread-safe."""
        if cls._instance is not None:
            return cls._instance
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def get_instance_safe(cls) -> Optional["PressureOracle"]:
        """Return existing instance or None. Never creates."""
        return cls._instance

    # -- Public query API ----------------------------------------------------

    def current_pressure(self) -> PressureLevel:
        """Return cached pressure level. Thread-safe, O(1), never blocks."""
        with self._data_lock:
            return self._latest.level

    def current_snapshot(self) -> PressureSnapshot:
        """Return a copy of the latest snapshot."""
        with self._data_lock:
            # PressureSnapshot is mutable but we return the same object
            # since callers only read it.  Safe because next sample creates
            # a new object.
            return self._latest

    def should_defer_destructive_action(
        self,
        action_name: str = "",
        severity_threshold: PressureLevel = PressureLevel.MODERATE,
    ) -> Tuple[bool, str]:
        """
        Check if a destructive action should be deferred due to pressure.

        Args:
            action_name: Human-readable label for logging.
            severity_threshold: Minimum level to trigger deferral.
                Default MODERATE — light pressure does not defer.

        Returns:
            (should_defer, reason) — fail-open: (False, "") on any error.
        """
        try:
            with self._data_lock:
                snap = self._latest

            level = snap.level
            if level == PressureLevel.UNKNOWN:
                return False, ""  # fail-open

            if level >= severity_threshold:
                reason = (
                    f"System pressure {level.name} "
                    f"(cpu={snap.cpu_percent:.0f}%, "
                    f"mem={snap.memory_percent:.0f}%, "
                    f"loop_lag={snap.event_loop_lag_ms:.0f}ms, "
                    f"gc_pause={snap.gc_pause_detected})"
                )
                if action_name:
                    reason = f"[{action_name}] {reason}"
                return True, reason

            return False, ""
        except Exception:
            return False, ""  # fail-open

    def pressure_multiplier(self) -> float:
        """
        Return a timeout multiplier based on current pressure.

        NONE → 1.0, LIGHT → 1.5, MODERATE → 2.0, SEVERE → 3.0, UNKNOWN → 1.0

        Consumers multiply their timeout by this value during pressure.
        Fail-open: returns 1.0 on any error.
        """
        try:
            level = self.current_pressure()
            return _MULTIPLIER_MAP.get(level, 1.0)
        except Exception:
            return 1.0

    # -- Sampling lifecycle --------------------------------------------------

    async def start_sampling(self) -> None:
        """Start background pressure sampling loop."""
        if self._running:
            return
        self._running = True
        self._sampling_task = asyncio.ensure_future(self._sampling_loop())
        logger.info(
            "[PressureOracle] Started sampling (interval=%.1fs)",
            self._sample_interval,
        )

    def stop_sampling(self) -> None:
        """Stop background sampling."""
        self._running = False
        if self._sampling_task and not self._sampling_task.done():
            self._sampling_task.cancel()
        logger.debug("[PressureOracle] Stopped sampling")

    async def _sampling_loop(self) -> None:
        """Background loop that takes pressure samples."""
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                # Sample in thread executor to avoid blocking event loop
                snap = await loop.run_in_executor(None, self._take_sample)

                # Measure event loop lag by scheduling a callback
                lag_ms = await self._measure_event_loop_lag()
                snap.event_loop_lag_ms = lag_ms

                # Re-classify level with loop lag included
                snap.level = self._classify(snap)

                with self._data_lock:
                    self._latest = snap
                    self._history.append(snap)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("[PressureOracle] Sampling error: %s", exc)
                with self._data_lock:
                    self._latest = PressureSnapshot(
                        timestamp_mono=time.monotonic(),
                        level=PressureLevel.UNKNOWN,
                    )

            await asyncio.sleep(self._sample_interval)

    def _take_sample(self) -> PressureSnapshot:
        """
        Take a single pressure sample.  Runs in thread executor.

        Uses psutil if available, falls back to os.getloadavg().
        """
        now_mono = time.monotonic()
        cpu = 0.0
        mem = 0.0
        gc_pause = _consume_gc_pause()

        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0)  # non-blocking cached value
            mem = psutil.virtual_memory().percent
        except ImportError:
            # Fallback: use load average as CPU proxy
            try:
                load_1m = os.getloadavg()[0]
                cpu_count = os.cpu_count() or 1
                cpu = min(100.0, (load_1m / cpu_count) * 100)
            except (OSError, AttributeError):
                pass

        snap = PressureSnapshot(
            timestamp_mono=now_mono,
            level=PressureLevel.UNKNOWN,  # classified after loop lag
            cpu_percent=cpu,
            memory_percent=mem,
            event_loop_lag_ms=0.0,  # filled in by caller
            gc_pause_detected=gc_pause,
        )
        # Preliminary classification (without loop lag)
        snap.level = self._classify(snap)
        return snap

    async def _measure_event_loop_lag(self) -> float:
        """
        Measure event loop lag by scheduling a zero-delay callback.

        Returns lag in milliseconds.
        """
        try:
            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            scheduled_mono = time.monotonic()

            def _on_callback() -> None:
                if not future.done():
                    future.set_result(time.monotonic())

            loop.call_soon(_on_callback)
            # Wait at most 2 seconds for the callback
            actual_mono = await asyncio.wait_for(future, timeout=2.0)
            lag_ms = (actual_mono - scheduled_mono) * 1000.0
            return lag_ms
        except asyncio.TimeoutError:
            return 2000.0  # 2s timeout → definitely lagging
        except Exception:
            return 0.0  # fail-open

    @staticmethod
    def _classify(snap: PressureSnapshot) -> PressureLevel:
        """Classify a snapshot into a PressureLevel."""
        moderate_count = 0

        # CPU
        if snap.cpu_percent >= _CPU_MODERATE:
            moderate_count += 1
        elif snap.cpu_percent >= _CPU_LIGHT:
            pass  # light indicator

        # Memory
        if snap.memory_percent >= _MEM_MODERATE:
            moderate_count += 1
        elif snap.memory_percent >= _MEM_LIGHT:
            pass  # light indicator

        # Event loop lag
        if snap.event_loop_lag_ms >= _LAG_MODERATE_MS:
            moderate_count += 1
        elif snap.event_loop_lag_ms >= _LAG_LIGHT_MS:
            pass  # light indicator

        # GC pause
        if snap.gc_pause_detected:
            moderate_count += 1

        # SEVERE: multiple moderate indicators simultaneously
        if moderate_count >= 2:
            return PressureLevel.SEVERE

        # MODERATE: any single moderate indicator
        if moderate_count >= 1:
            return PressureLevel.MODERATE

        # LIGHT: any single light indicator
        if (
            snap.cpu_percent >= _CPU_LIGHT
            or snap.memory_percent >= _MEM_LIGHT
            or snap.event_loop_lag_ms >= _LAG_LIGHT_MS
        ):
            return PressureLevel.LIGHT

        return PressureLevel.NONE

    # -- History queries (diagnostic) ----------------------------------------

    def recent_pressure_levels(self, n: int = 12) -> List[PressureLevel]:
        """Return the last *n* pressure levels (newest first)."""
        with self._data_lock:
            return [s.level for s in reversed(list(self._history)[-n:])]

    def sustained_pressure(
        self, min_level: PressureLevel = PressureLevel.MODERATE, window_s: float = 30.0
    ) -> bool:
        """
        True if pressure has been >= *min_level* for the last *window_s* seconds.

        Used for diagnostic logging, not for gating.
        """
        try:
            now_mono = time.monotonic()
            with self._data_lock:
                for snap in reversed(self._history):
                    if now_mono - snap.timestamp_mono > window_s:
                        break
                    if snap.level < min_level:
                        return False
                return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Module-level convenience functions (fail-open)
# ---------------------------------------------------------------------------

def get_pressure_oracle() -> PressureOracle:
    """Get the singleton PressureOracle."""
    return PressureOracle.get_instance()


def should_defer_destructive_action(
    action_name: str = "",
    severity_threshold: PressureLevel = PressureLevel.MODERATE,
) -> Tuple[bool, str]:
    """
    Quick check: should we defer a destructive action?

    Fail-open: returns (False, "") on any error (including if oracle
    is not yet started).
    """
    try:
        oracle = PressureOracle.get_instance_safe()
        if oracle is None:
            return False, ""
        return oracle.should_defer_destructive_action(action_name, severity_threshold)
    except Exception:
        return False, ""


def pressure_multiplier() -> float:
    """
    Get current timeout multiplier.

    NONE → 1.0, LIGHT → 1.5, MODERATE → 2.0, SEVERE → 3.0

    Fail-open: returns 1.0 on any error.
    """
    try:
        oracle = PressureOracle.get_instance_safe()
        if oracle is None:
            return 1.0
        return oracle.pressure_multiplier()
    except Exception:
        return 1.0
