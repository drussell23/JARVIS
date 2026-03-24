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
    # Lifecycle
    # ------------------------------------------------------------------

    async def awaken(self) -> None:
        """Start all subsystems and the perception loop. Non-blocking."""
        if self._running:
            return
        await self._discover_subsystems()
        self._screen_dispatch = self._build_screen_dispatch()
        self._running = True
        self._start_perception_loop()
        await self._start_monitor()
        logger.info(
            "[VisionCortex] Awake (pipeline=%s, analyzer=%s, monitor=%s)",
            self._frame_pipeline is not None,
            self._analyzer is not None,
            self._monitor is not None,
        )

    async def shutdown(self) -> None:
        """Stop all subsystems. Idempotent."""
        if not self._running:
            return
        self._running = False
        if self._perception_task and not self._perception_task.done():
            self._perception_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._perception_task), timeout=2.0
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._perception_task = None
        if self._monitor:
            try:
                await self._monitor.stop_monitoring()
            except Exception as exc:
                logger.debug("[VisionCortex] Monitor stop error: %s", exc)
        if VisionCortex._instance is self:
            VisionCortex._instance = None
        logger.info("[VisionCortex] Shutdown complete")

    # ------------------------------------------------------------------
    # Subsystem discovery & wiring
    # ------------------------------------------------------------------

    async def _discover_subsystems(self) -> None:
        """Discover existing organs via singleton lookups. No hard imports.

        Subsystem discovery order:
        1. VisionActionLoop → frame pipeline + knowledge fabric
        2. VisionCommandHandler → real Phase 2 analysis (Claude Vision / LLaVA)
        3. MemoryAwareScreenAnalyzer → continuous screen monitoring
        """
        # 1. Frame pipeline from VisionActionLoop
        try:
            from backend.vision.realtime.vision_action_loop import VisionActionLoop
            val = VisionActionLoop.get_instance()
            if val is not None:
                self._frame_pipeline = val.frame_pipeline
                self._knowledge_fabric = val.knowledge_fabric
        except ImportError:
            pass

        # 2. Create analyzer with NullVisionHandler IMMEDIATELY (non-blocking).
        # The real VisionCommandHandler is heavy to construct (ML models, API
        # connections). Discovering it during boot would block the startup
        # sequence — violating §2 (Progressive Awakening).
        # Phase 2 starts with NullHandler; the real handler is discovered in
        # the background and hot-swapped when ready.
        try:
            from backend.vision.continuous_screen_analyzer import (
                MemoryAwareScreenAnalyzer,
            )
            self._analyzer = MemoryAwareScreenAnalyzer(_NullVisionHandler())
            self._wire_analyzer_callbacks()
            logger.info("[VisionCortex] Analyzer created (Phase 2 handler upgrading in background)")
        except ImportError as exc:
            logger.debug("[VisionCortex] Analyzer not available: %s", exc)

        # 3. Upgrade to real VisionCommandHandler in background (non-blocking)
        if self._analyzer is not None:
            asyncio.ensure_future(self._upgrade_vision_handler())

        # Fallback: if no Ferrari Engine, start analyzer's own monitoring loop
        if self._frame_pipeline is None and self._analyzer is not None:
            logger.info("[VisionCortex] No Ferrari Engine -- using screencapture fallback")
            await self._analyzer.start_monitoring()

    async def _upgrade_vision_handler(self) -> None:
        """Background task: discover and hot-swap the real VisionCommandHandler.

        Runs AFTER awaken() returns so boot is never blocked. When the real
        handler is ready, it replaces the NullVisionHandler on the analyzer,
        enabling Phase 2 analysis (Claude Vision / LLaVA).
        """
        try:
            # Run the heavy import/init in a thread to avoid blocking event loop
            def _get_handler():
                from backend.api.vision_command_handler import get_vision_command_handler
                return get_vision_command_handler()

            handler = await asyncio.to_thread(_get_handler)
            if handler is not None and self._analyzer is not None:
                self._analyzer.vision_handler = handler
                logger.info("[VisionCortex] Phase 2 handler upgraded to real VisionCommandHandler")
            else:
                logger.info("[VisionCortex] VisionCommandHandler not available — Phase 2 stays disabled")
        except ImportError:
            logger.debug("[VisionCortex] VisionCommandHandler not importable")
        except Exception as exc:
            logger.debug("[VisionCortex] Handler upgrade failed (non-fatal): %s", exc)

    def _wire_analyzer_callbacks(self) -> None:
        """Register VisionCortex as consumer of analyzer events.
        Stores strong refs to prevent _CallbackSet weakref GC.
        """
        if self._analyzer is None:
            return
        self._analyzer_callback_refs = []
        for event_type in ('content_changed', 'app_changed', 'error_detected',
                           'notification_detected', 'meeting_detected',
                           'security_concern', 'screen_captured'):
            handler = self._make_screen_handler(event_type)
            self._analyzer_callback_refs.append(handler)  # prevent GC
            self._analyzer.event_callbacks[event_type].add(handler)

    def _make_screen_handler(self, event_type: str):
        """Create a callback for a specific screen event type."""
        async def _handler(data):
            await self._on_screen_event(event_type, data)
        return _handler

    # ------------------------------------------------------------------
    # Perception loop
    # ------------------------------------------------------------------

    def _start_perception_loop(self) -> None:
        # Only start if we have Ferrari Engine
        if self._frame_pipeline is None:
            return
        self._perception_task = asyncio.ensure_future(
            self._perception_loop(),
        )

    async def _perception_loop(self) -> None:
        """Background loop: read latest frame, inject into analyzer, adapt."""
        while self._running:
            try:
                interval = self.perception_interval
                await asyncio.sleep(interval)
                await self._run_one_perception_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[VisionCortex] Perception cycle error: %s", exc)

    async def _run_one_perception_cycle(self) -> None:
        """Single perception cycle: read frame, inject, update throttle.
        Does NOT append to _change_history -- callbacks do that to avoid double-counting.
        """
        if self._frame_pipeline is None or self._analyzer is None:
            return
        frame = self._frame_pipeline.latest_frame
        if frame is None:
            return

        from PIL import Image
        pil_image = Image.fromarray(frame.data)
        await self._analyzer.inject_frame(pil_image, frame.timestamp)

    # ------------------------------------------------------------------
    # Monitor wiring
    # ------------------------------------------------------------------

    async def _start_monitor(self) -> None:
        try:
            from backend.vision.multi_space_monitor import (
                MultiSpaceMonitor, MonitorEventType,
            )
            self._monitor = MultiSpaceMonitor()
            for evt_type in (MonitorEventType.SPACE_SWITCHED,
                             MonitorEventType.APP_LAUNCHED,
                             MonitorEventType.APP_CLOSED,
                             MonitorEventType.APP_MOVED,
                             MonitorEventType.WORKFLOW_DETECTED):
                self._monitor.register_event_handler(
                    evt_type, self._on_workspace_event,
                )
            await self._monitor.start_monitoring()
        except ImportError as exc:
            logger.debug("[VisionCortex] MultiSpaceMonitor not available: %s", exc)
        except Exception as exc:
            logger.warning("[VisionCortex] Monitor start failed: %s", exc)

    # ------------------------------------------------------------------
    # Callback dispatchers -- registry-based, no if/elif chains (Manifesto ss5)
    # ------------------------------------------------------------------

    def _build_screen_dispatch(self) -> dict:
        """Build dispatch table for screen events. Called once in awaken()."""
        return {
            'content_changed': self._handle_content_changed,
            'screen_captured': self._handle_screen_captured,
            'error_detected': self._handle_narration,
            'security_concern': self._handle_narration,
            'app_changed': self._handle_narration,
            'notification_detected': self._handle_narration,
            'meeting_detected': self._handle_narration,
        }

    async def _on_screen_event(self, event_type: str, data: dict) -> None:
        """Dispatch screen events via registry. No if/elif chains."""
        handler = self._screen_dispatch.get(event_type)
        if handler:
            await handler(event_type, data)
        await self._emit_telemetry(f"screen.{event_type}@1.0.0", data)

    async def _handle_content_changed(self, event_type: str, data: dict) -> None:
        self._change_history.append((time.monotonic(), True))
        self._update_activity_level()
        await self._update_scene_graph(data)

    async def _handle_screen_captured(self, event_type: str, data: dict) -> None:
        self._change_history.append((time.monotonic(), False))
        self._update_activity_level()

    async def _handle_narration(self, event_type: str, data: dict) -> None:
        await self._narrate_event(event_type, data)

    async def _on_workspace_event(self, event) -> None:
        """Dispatch workspace events from MultiSpaceMonitor."""
        from backend.vision.multi_space_monitor import MonitorEventType
        if event.event_type == MonitorEventType.SPACE_SWITCHED:
            self._force_immediate_capture()
        await self._emit_telemetry(
            f"workspace.{event.event_type.value}@1.0.0",
            event.details,
        )

    def _force_immediate_capture(self) -> None:
        """Schedule an immediate perception cycle (space switched = new content)."""
        if self._running and self._perception_task is not None:
            asyncio.ensure_future(self._run_one_perception_cycle())

    async def _update_scene_graph(self, data: dict) -> None:
        """Feed analysis results into KnowledgeFabric L1 cache."""
        if self._knowledge_fabric is None:
            return
        try:
            self._knowledge_fabric.update_scene(data)
        except Exception as exc:
            logger.debug("[VisionCortex] Scene graph update error: %s", exc)

    async def _narrate_event(self, event_type: str, data: dict) -> None:
        if not _NARRATION_ENABLED:
            return
        await _safe_say(event_type, data)

    async def _emit_telemetry(self, schema: str, payload: dict) -> None:
        try:
            from backend.core.telemetry_bus import get_telemetry_bus, TelemetryEnvelope
            bus = get_telemetry_bus()
            if bus:
                envelope = TelemetryEnvelope.create(
                    event_schema=schema,
                    source="vision_cortex",
                    payload=payload or {},
                )
                bus.emit(envelope)
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("[VisionCortex] Telemetry emit error: %s", exc)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

async def _safe_say(event_type: str, data: dict) -> None:
    """Voice narration for vision events. Dynamically builds message from event data."""
    try:
        from backend.core.supervisor.unified_voice_orchestrator import safe_say
        app = data.get('app_name') or data.get('app') or ''
        detail = data.get('error_text') or data.get('description') or event_type.replace('_', ' ')
        msg = f"{detail} — {app}".strip(' —') if app else detail
        if msg:
            await safe_say(msg, source="vision_cortex", skip_dedup=False)
    except ImportError:
        pass
    except Exception:
        pass
