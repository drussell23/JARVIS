"""
VisionCortex -- adaptive real-time screen awareness coordinator.

Wires Ferrari Engine (FramePipeline) -> MemoryAwareScreenAnalyzer -> MultiSpaceMonitor
into a unified perception system with adaptive throttle.

Manifesto alignment:
    ss1 Unified Organism: single coordinator, discoverable via singleton
    ss2 Progressive Awakening: Phase 1 local-first, Phase 2 when GCP arrives
    ss3 Async Tendrils: non-blocking perception loop
    ss6 Neuroplasticity: perception intensity adapts to activity rate
    ss7 Absolute Observability: all events -> TelemetryBus
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from enum import Enum
from typing import Optional

import psutil

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Environment-driven tunables -- zero hardcoding
# ---------------------------------------------------------------------------
_IDLE_RATE = float(os.environ.get("VISION_CORTEX_IDLE_RATE", "0.02"))
_LOW_RATE = float(os.environ.get("VISION_CORTEX_LOW_RATE", "0.1"))
_HIGH_RATE = float(os.environ.get("VISION_CORTEX_HIGH_RATE", "0.5"))
_IDLE_INTERVAL = float(os.environ.get("VISION_CORTEX_IDLE_INTERVAL", "8.0"))
_LOW_INTERVAL = float(os.environ.get("VISION_CORTEX_LOW_INTERVAL", "5.0"))
_NORMAL_INTERVAL = float(os.environ.get("VISION_CORTEX_NORMAL_INTERVAL", "3.0"))
_HIGH_INTERVAL = float(os.environ.get("VISION_CORTEX_HIGH_INTERVAL", "1.0"))
_RATE_WINDOW_S = float(os.environ.get("VISION_CORTEX_RATE_WINDOW_S", "60.0"))
_MEMORY_LIMIT_MB = int(os.environ.get("VISION_MEMORY_LIMIT_MB", "1500"))
_NARRATION_ENABLED = os.environ.get("JARVIS_VISION_NARRATION_ENABLED", "true").lower() == "true"
# Deque maxlen: 2 samples/s * window_s = headroom for HIGH mode (1s interval)
_HISTORY_MAXLEN = int(_RATE_WINDOW_S * 2)


class _NullVisionHandler:
    """Minimal handler for injected-frame-only mode (no capture needed).

    Satisfies MemoryAwareScreenAnalyzer's interface without importing test libs.
    """
    async def capture_screen(self):
        return None

    async def describe_screen(self, *a, **kw):
        return {}

    async def analyze_screen(self, *a, **kw):
        return {}


class ActivityLevel(str, Enum):
    IDLE = "idle"
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class VisionCortex:
    """Adaptive real-time screen awareness coordinator.

    Reads frames from Ferrari Engine via FramePipeline.latest_frame (non-destructive),
    injects them into MemoryAwareScreenAnalyzer for Phase 1/2 analysis, dispatches
    events to voice/telemetry/scene-graph, and adapts perception frequency.
    """

    _instance: Optional[VisionCortex] = None

    @classmethod
    def get_instance(cls) -> Optional[VisionCortex]:
        return cls._instance

    @classmethod
    def set_instance(cls, instance: Optional[VisionCortex]) -> None:
        cls._instance = instance

    def __init__(self) -> None:
        self._running = False
        self._perception_task: Optional[asyncio.Task] = None
        self._activity_level = ActivityLevel.NORMAL
        self._change_history: collections.deque = collections.deque(maxlen=_HISTORY_MAXLEN)

        # Subsystem references -- populated in awaken()
        self._frame_pipeline = None
        self._knowledge_fabric = None
        self._analyzer = None
        self._monitor = None

        # Strong refs for analyzer callbacks (prevent weakref GC)
        self._analyzer_callback_refs: list = []

        # Screen event dispatch table -- populated in awaken() via _build_screen_dispatch()
        self._screen_dispatch: dict = {}

        # Self-register singleton
        VisionCortex._instance = self

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def activity_level(self) -> ActivityLevel:
        return self._activity_level

    @property
    def perception_interval(self) -> float:
        return {
            ActivityLevel.IDLE: _IDLE_INTERVAL,
            ActivityLevel.LOW: _LOW_INTERVAL,
            ActivityLevel.NORMAL: _NORMAL_INTERVAL,
            ActivityLevel.HIGH: _HIGH_INTERVAL,
        }[self._activity_level]

    @property
    def is_awake(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Adaptive throttle
    # ------------------------------------------------------------------

    def _compute_activity_rate(self) -> float:
        """Changes per second over the sliding window."""
        now = time.monotonic()
        cutoff = now - _RATE_WINDOW_S
        changes = sum(1 for ts, changed in self._change_history
                      if changed and ts >= cutoff)
        return changes / _RATE_WINDOW_S if _RATE_WINDOW_S > 0 else 0.0

    def _is_memory_pressured(self) -> bool:
        """True if process RSS exceeds VISION_MEMORY_LIMIT_MB."""
        try:
            rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
            return rss_mb > _MEMORY_LIMIT_MB
        except Exception:
            return False

    def _update_activity_level(self) -> None:
        # Memory pressure override (spec requirement): force IDLE
        if self._is_memory_pressured():
            self._activity_level = ActivityLevel.IDLE
            return
        rate = self._compute_activity_rate()
        if rate >= _HIGH_RATE:
            self._activity_level = ActivityLevel.HIGH
        elif rate >= _LOW_RATE:
            self._activity_level = ActivityLevel.NORMAL
        elif rate >= _IDLE_RATE:
            self._activity_level = ActivityLevel.LOW
        else:
            self._activity_level = ActivityLevel.IDLE

    # ------------------------------------------------------------------
    # Lifecycle -- stubs for Task 5
    # ------------------------------------------------------------------

    async def awaken(self) -> None:
        raise NotImplementedError("Implemented in Task 5")

    async def shutdown(self) -> None:
        raise NotImplementedError("Implemented in Task 5")
