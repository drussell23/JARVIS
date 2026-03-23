# VisionCortex Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire Ferrari Engine, MemoryAwareScreenAnalyzer, MultiSpaceMonitor, VisionActionLoop, and Ghost Hands into a unified real-time screen awareness system with adaptive perception throttle.

**Architecture:** A single new coordinator (`VisionCortex`) reads the latest frame from Ferrari Engine via a non-destructive `latest_frame` property, injects it into MemoryAwareScreenAnalyzer for Phase 1/2 analysis, dispatches events to voice narration / TelemetryBus / scene graph, and adapts perception frequency based on screen activity rate.

**Tech Stack:** Python 3.9, asyncio, PIL/numpy, existing JARVIS subsystems (FramePipeline, MemoryAwareScreenAnalyzer, MultiSpaceMonitor, VisionRouter, KnowledgeFabric, TelemetryBus, safe_say)

**Spec:** `docs/superpowers/specs/2026-03-23-vision-cortex-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `backend/vision/realtime/vision_cortex.py` | CREATE | Coordinator: lifecycle, perception loop, adaptive throttle, callback dispatch |
| `backend/vision/realtime/frame_pipeline.py` | MODIFY | Add `_latest_frame` + `latest_frame` property (non-destructive read) |
| `backend/vision/continuous_screen_analyzer.py` | MODIFY | Add `injected_image` param to `_phase1_capture_and_detect`, add `inject_frame()` wrapper |
| `backend/vision/realtime/vision_action_loop.py` | MODIFY | Expose `frame_pipeline` and `knowledge_fabric` properties |
| `unified_supervisor.py` | MODIFY | Start VisionCortex at Zone 6.5 after VisionActionLoop |
| `tests/vision/realtime/test_vision_cortex.py` | CREATE | Unit tests for VisionCortex |
| `tests/vision/realtime/test_frame_pipeline_latest.py` | CREATE | Test latest_frame property + contention scenario |
| `tests/vision/test_analyzer_inject_frame.py` | CREATE | Test inject_frame path through Phase 1/2 |

---

### Task 1: FramePipeline `latest_frame` Property

**Files:**
- Modify: `backend/vision/realtime/frame_pipeline.py:225-235` (\_\_init\_\_), `321-346` (\_enqueue\_frame)
- Create: `tests/vision/realtime/test_frame_pipeline_latest.py`

- [ ] **Step 1: Write failing test for latest_frame property**

```python
# tests/vision/realtime/test_frame_pipeline_latest.py
import asyncio
import numpy as np
import pytest
from backend.vision.realtime.frame_pipeline import FramePipeline, FrameData


@pytest.fixture
def pipeline():
    return FramePipeline(use_sck=False, motion_detect=False)


def _make_frame(n: int) -> FrameData:
    return FrameData(
        data=np.zeros((100, 100, 3), dtype=np.uint8),
        width=100, height=100,
        timestamp=float(n), frame_number=n,
    )


def test_latest_frame_is_none_initially(pipeline):
    assert pipeline.latest_frame is None


def test_latest_frame_updates_on_enqueue(pipeline):
    frame = _make_frame(1)
    pipeline._enqueue_frame(frame)
    assert pipeline.latest_frame is frame
    assert pipeline.latest_frame.frame_number == 1


def test_latest_frame_is_most_recent(pipeline):
    pipeline._enqueue_frame(_make_frame(1))
    pipeline._enqueue_frame(_make_frame(2))
    pipeline._enqueue_frame(_make_frame(3))
    assert pipeline.latest_frame.frame_number == 3


@pytest.mark.asyncio
async def test_latest_frame_survives_queue_drain(pipeline):
    """latest_frame persists even after get_frame() drains the queue."""
    frame = _make_frame(42)
    pipeline._enqueue_frame(frame)

    got = await pipeline.get_frame(timeout_s=0.1)
    assert got is frame
    # Queue is empty now, but latest_frame still available
    assert pipeline.latest_frame is frame


@pytest.mark.asyncio
async def test_no_contention_with_get_frame(pipeline):
    """VisionCortex reads latest_frame while VisionActionLoop uses get_frame.
    get_frame must always succeed when frames are in the queue."""
    for i in range(5):
        pipeline._enqueue_frame(_make_frame(i))

    # Simulate VisionCortex reading latest_frame (non-destructive)
    latest = pipeline.latest_frame
    assert latest.frame_number == 4

    # Simulate VisionActionLoop draining queue (destructive)
    frames = []
    for _ in range(5):
        f = await pipeline.get_frame(timeout_s=0.1)
        assert f is not None
        frames.append(f)

    assert len(frames) == 5
    # latest_frame still accessible after drain
    assert pipeline.latest_frame.frame_number == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/vision/realtime/test_frame_pipeline_latest.py -x -v`
Expected: FAIL — `AttributeError: 'FramePipeline' object has no attribute 'latest_frame'`

- [ ] **Step 3: Implement latest_frame in FramePipeline**

In `backend/vision/realtime/frame_pipeline.py`, add to `__init__` (after line 243):
```python
        self._latest_frame: Optional[FrameData] = None
```

In `_enqueue_frame` (line 321), add as first line of method body:
```python
        self._latest_frame = frame
```

Add property after `is_running` (after line 290):
```python
    @property
    def latest_frame(self) -> Optional["FrameData"]:
        """Most recent frame — non-destructive read. Does not consume from queue."""
        return self._latest_frame
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/vision/realtime/test_frame_pipeline_latest.py -x -v`
Expected: all 5 PASS

- [ ] **Step 5: Run existing FramePipeline tests to verify no regression**

Run: `python3 -m pytest tests/vision/realtime/ -x -v`
Expected: all PASS (including the existing 14 VisionActionLoop tests)

- [ ] **Step 6: Commit**

```bash
git add backend/vision/realtime/frame_pipeline.py tests/vision/realtime/test_frame_pipeline_latest.py
git commit -m "feat(frame_pipeline): add latest_frame property for non-destructive reads"
```

---

### Task 2: VisionActionLoop Property Exposure

**Files:**
- Modify: `backend/vision/realtime/vision_action_loop.py:177-183` (Properties section)

- [ ] **Step 1: Write failing test**

```python
# Add to existing tests/vision/realtime/test_vision_action_loop.py
def test_frame_pipeline_property(vision_loop):
    """VisionCortex needs access to the frame pipeline."""
    assert vision_loop.frame_pipeline is not None


def test_knowledge_fabric_property(vision_loop):
    """VisionCortex needs access to the knowledge fabric for L1 cache updates."""
    assert vision_loop.knowledge_fabric is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/vision/realtime/test_vision_action_loop.py::test_frame_pipeline_property -x -v`
Expected: FAIL — `AttributeError`

- [ ] **Step 3: Add properties to VisionActionLoop**

In `backend/vision/realtime/vision_action_loop.py`, after the `state` property (line ~182):

```python
    @property
    def frame_pipeline(self) -> "FramePipeline":
        """The underlying frame capture pipeline. Used by VisionCortex for continuous awareness."""
        return self._frame_pipeline

    @property
    def knowledge_fabric(self) -> "KnowledgeFabric":
        """The scene graph fabric. Used by VisionCortex to populate L1 cache from continuous analysis."""
        return self._knowledge_fabric
```

- [ ] **Step 4: Run all VisionActionLoop tests**

Run: `python3 -m pytest tests/vision/realtime/test_vision_action_loop.py -x -v`
Expected: all 16 PASS (14 existing + 2 new)

- [ ] **Step 5: Commit**

```bash
git add backend/vision/realtime/vision_action_loop.py tests/vision/realtime/test_vision_action_loop.py
git commit -m "feat(vision_action_loop): expose frame_pipeline and knowledge_fabric properties"
```

---

### Task 3: MemoryAwareScreenAnalyzer `inject_frame()`

**Files:**
- Modify: `backend/vision/continuous_screen_analyzer.py:371-483` (\_phase1\_capture\_and\_detect)
- Create: `tests/vision/test_analyzer_inject_frame.py`

- [ ] **Step 1: Write failing test for inject_frame**

```python
# tests/vision/test_analyzer_inject_frame.py
import asyncio
import pytest
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image

from backend.vision.continuous_screen_analyzer import MemoryAwareScreenAnalyzer


@pytest.fixture
def mock_vision_handler():
    handler = MagicMock()
    handler.capture_screen = AsyncMock(return_value=None)
    handler.describe_screen = AsyncMock(return_value={"description": "test"})
    handler.analyze_screen = AsyncMock(return_value={"analysis": "test"})
    return handler


@pytest.fixture
def analyzer(mock_vision_handler):
    return MemoryAwareScreenAnalyzer(mock_vision_handler)


def test_inject_frame_method_exists(analyzer):
    assert hasattr(analyzer, 'inject_frame')
    assert asyncio.iscoroutinefunction(analyzer.inject_frame)


@pytest.mark.asyncio
async def test_inject_frame_skips_capture(analyzer, mock_vision_handler):
    """inject_frame must NOT call vision_handler.capture_screen."""
    img = Image.fromarray(np.zeros((100, 100, 3), dtype=np.uint8))
    await analyzer.inject_frame(img, 1234567890.0)
    mock_vision_handler.capture_screen.assert_not_called()


@pytest.mark.asyncio
async def test_inject_frame_runs_phase1_fingerprinting(analyzer):
    """inject_frame should produce a screen_captured event."""
    events_fired = []
    analyzer.event_callbacks['screen_captured'].add(
        lambda data: events_fired.append(data)
    )
    img = Image.fromarray(np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8))
    await analyzer.inject_frame(img, 1234567890.0)
    assert len(events_fired) >= 1


@pytest.mark.asyncio
async def test_inject_frame_detects_app_change(analyzer):
    """inject_frame should detect app changes via _quick_screen_analysis."""
    events_fired = []
    analyzer.event_callbacks['app_changed'].add(
        lambda data: events_fired.append(data)
    )
    # Set up a previous app state
    analyzer.current_screen_state['quick_app'] = 'Safari'

    img = Image.fromarray(np.zeros((100, 100, 3), dtype=np.uint8))
    with patch.object(analyzer, '_quick_screen_analysis', new_callable=AsyncMock,
                      return_value={'current_app': 'Terminal'}):
        await analyzer.inject_frame(img, 1234567890.0)

    assert any(e.get('app_name') == 'Terminal' for e in events_fired)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/vision/test_analyzer_inject_frame.py -x -v`
Expected: FAIL — `AttributeError: 'MemoryAwareScreenAnalyzer' object has no attribute 'inject_frame'`

- [ ] **Step 3: Implement inject_frame**

In `backend/vision/continuous_screen_analyzer.py`:

**3a.** Modify `_phase1_capture_and_detect` signature (line 371) to accept optional image:

```python
    async def _phase1_capture_and_detect(
        self, injected_image: Optional["Image.Image"] = None,
    ) -> Optional[Dict[str, Any]]:
```

**3b.** Also add optional `injected_timestamp` param:

```python
    async def _phase1_capture_and_detect(
        self,
        injected_image: Optional["Image.Image"] = None,
        injected_timestamp: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
```

**3c.** Replace the capture block (lines 377-393) with:

```python
        if injected_image is not None:
            screenshot = injected_image
        else:
            capture_result = await self.vision_handler.capture_screen()
            if capture_result is None:
                return None

            if hasattr(capture_result, 'success'):
                if not capture_result.success:
                    return None
            screenshot = capture_result

        current_time = injected_timestamp if injected_timestamp is not None else time.time()
```

And remove the existing `current_time = time.time()` line that was below the old capture block.

All subsequent code (fingerprinting, quick_analysis, event firing) remains identical — it only uses `screenshot` and `current_time`.

**3d.** Add public `inject_frame` method after `_phase1_capture_and_detect` (around line 484):

```python
    async def inject_frame(
        self, pil_image: "Image.Image", timestamp: float,
    ) -> None:
        """Accept an externally-captured frame (from Ferrari Engine via VisionCortex).

        Runs the same Phase 1 fingerprinting + Phase 2 analysis pipeline as the
        internal monitoring loop, but skips the screencapture subprocess.
        The timestamp from the original capture is preserved (no timing skew).
        """
        phase1 = await self._phase1_capture_and_detect(
            injected_image=pil_image,
            injected_timestamp=timestamp,
        )
        if phase1 and phase1.get('needs_full_analysis'):
            await self._phase2_analyze_if_memory_allows(phase1)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/vision/test_analyzer_inject_frame.py -x -v`
Expected: all 4 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/vision/continuous_screen_analyzer.py tests/vision/test_analyzer_inject_frame.py
git commit -m "feat(screen_analyzer): add inject_frame() for external frame injection from VisionCortex"
```

---

### Task 4: VisionCortex — Core Class + Adaptive Throttle

**Files:**
- Create: `backend/vision/realtime/vision_cortex.py`
- Create: `tests/vision/realtime/test_vision_cortex.py`

- [ ] **Step 1: Write failing tests for VisionCortex core**

```python
# tests/vision/realtime/test_vision_cortex.py
import asyncio
import pytest
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch

from backend.vision.realtime.vision_cortex import VisionCortex, ActivityLevel


@pytest.fixture(autouse=True)
def clear_singleton():
    """Ensure clean singleton state between tests."""
    VisionCortex.set_instance(None)
    yield
    VisionCortex.set_instance(None)


def test_singleton_initially_none():
    assert VisionCortex.get_instance() is None


def test_activity_level_default():
    cortex = VisionCortex()
    assert cortex.activity_level == ActivityLevel.NORMAL


def test_compute_interval_normal():
    cortex = VisionCortex()
    cortex._activity_level = ActivityLevel.NORMAL
    assert cortex.perception_interval == 3.0


def test_compute_interval_idle():
    cortex = VisionCortex()
    cortex._activity_level = ActivityLevel.IDLE
    assert cortex.perception_interval == 8.0


def test_compute_interval_high():
    cortex = VisionCortex()
    cortex._activity_level = ActivityLevel.HIGH
    assert cortex.perception_interval == 1.0


def test_compute_activity_rate_empty():
    cortex = VisionCortex()
    assert cortex._compute_activity_rate() == 0.0


def test_compute_activity_rate_with_changes():
    import time
    cortex = VisionCortex()
    now = time.monotonic()
    # Simulate 10 changes in 60 seconds
    for i in range(10):
        cortex._change_history.append((now - 60 + i * 6, True))
    for i in range(50):
        cortex._change_history.append((now - 60 + i * 1.2, False))
    rate = cortex._compute_activity_rate()
    # 10 changes / 60 seconds = 0.167
    assert 0.1 < rate < 0.3


def test_update_activity_level_from_rate():
    cortex = VisionCortex()
    # Zero rate → IDLE
    cortex._change_history.clear()
    cortex._update_activity_level()
    assert cortex._activity_level == ActivityLevel.IDLE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/vision/realtime/test_vision_cortex.py -x -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.vision.realtime.vision_cortex'`

- [ ] **Step 3: Create VisionCortex with core + throttle**

Create `backend/vision/realtime/vision_cortex.py`:

```python
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

        # Subsystem references — populated in awaken()
        self._frame_pipeline = None
        self._knowledge_fabric = None
        self._analyzer = None
        self._monitor = None

        # Strong refs for analyzer callbacks (prevent weakref GC)
        self._analyzer_callback_refs: list = []

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
    # Lifecycle — awaken / shutdown (implemented in Task 5)
    # ------------------------------------------------------------------

    async def awaken(self) -> None:
        raise NotImplementedError("Implemented in Task 5")

    async def shutdown(self) -> None:
        raise NotImplementedError("Implemented in Task 5")
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/vision/realtime/test_vision_cortex.py -x -v`
Expected: all 8 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/vision/realtime/vision_cortex.py tests/vision/realtime/test_vision_cortex.py
git commit -m "feat(vision_cortex): core class with adaptive throttle and singleton"
```

---

### Task 5: VisionCortex — Lifecycle (awaken/shutdown) + Perception Loop

**Files:**
- Modify: `backend/vision/realtime/vision_cortex.py`
- Add tests: `tests/vision/realtime/test_vision_cortex.py`

- [ ] **Step 1: Write failing tests for lifecycle + perception**

Append to `tests/vision/realtime/test_vision_cortex.py`:

```python
@pytest.mark.asyncio
async def test_awaken_and_shutdown():
    cortex = VisionCortex()
    # Mock subsystem discovery
    with patch.object(cortex, '_discover_subsystems', new_callable=AsyncMock):
        with patch.object(cortex, '_start_perception_loop'):
            with patch.object(cortex, '_start_monitor', new_callable=AsyncMock):
                await cortex.awaken()
                assert cortex.is_awake
                await cortex.shutdown()
                assert not cortex.is_awake


@pytest.mark.asyncio
async def test_awaken_clears_singleton_on_shutdown():
    cortex = VisionCortex()
    assert VisionCortex.get_instance() is cortex
    with patch.object(cortex, '_discover_subsystems', new_callable=AsyncMock):
        with patch.object(cortex, '_start_perception_loop'):
            with patch.object(cortex, '_start_monitor', new_callable=AsyncMock):
                await cortex.awaken()
    await cortex.shutdown()
    assert VisionCortex.get_instance() is None


@pytest.mark.asyncio
async def test_perception_loop_reads_latest_frame():
    """Verify perception loop uses latest_frame (non-destructive) not get_frame."""
    from backend.vision.realtime.frame_pipeline import FrameData
    cortex = VisionCortex()

    mock_frame = FrameData(
        data=np.zeros((100, 100, 3), dtype=np.uint8),
        width=100, height=100, timestamp=1.0, frame_number=1,
    )
    mock_pipeline = MagicMock()
    mock_pipeline.latest_frame = mock_frame
    cortex._frame_pipeline = mock_pipeline

    mock_analyzer = MagicMock()
    mock_analyzer.inject_frame = AsyncMock()
    cortex._analyzer = mock_analyzer
    cortex._running = True

    # Run one iteration of perception loop
    await cortex._run_one_perception_cycle()

    mock_analyzer.inject_frame.assert_called_once()
    # Verify it did NOT call get_frame
    mock_pipeline.get_frame.assert_not_called()
```

- [ ] **Step 2: Run to verify failures**

Run: `python3 -m pytest tests/vision/realtime/test_vision_cortex.py -x -v -k "awaken or perception"`
Expected: FAIL — `NotImplementedError`

- [ ] **Step 3: Implement awaken/shutdown/perception loop**

Replace the `awaken` and `shutdown` stubs and add the perception methods in `vision_cortex.py`:

```python
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

    async def _discover_subsystems(self) -> None:
        """Discover existing organs via singleton lookups. No hard imports."""
        try:
            from backend.vision.realtime.vision_action_loop import VisionActionLoop
            val = VisionActionLoop.get_instance()
            if val is not None:
                self._frame_pipeline = val.frame_pipeline
                self._knowledge_fabric = val.knowledge_fabric
        except ImportError:
            pass

        # Create analyzer — works with or without Ferrari Engine
        try:
            from backend.vision.continuous_screen_analyzer import (
                MemoryAwareScreenAnalyzer,
            )
            handler = _NullVisionHandler()
            self._analyzer = MemoryAwareScreenAnalyzer(handler)
            self._wire_analyzer_callbacks()
        except ImportError as exc:
            logger.debug("[VisionCortex] Analyzer not available: %s", exc)

        # Fallback: if no Ferrari Engine, start analyzer's own monitoring loop
        if self._frame_pipeline is None and self._analyzer is not None:
            logger.info("[VisionCortex] No Ferrari Engine — using screencapture fallback")
            await self._analyzer.start_monitoring()

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

    def _start_perception_loop(self) -> None:
        # Only start perception loop if we have Ferrari Engine
        # (otherwise analyzer runs its own screencapture loop via fallback)
        if self._frame_pipeline is None:
            return
        self._perception_task = asyncio.ensure_future(
            self._perception_loop(),
        )
        self._perception_task.set_name("vision_cortex.perception")

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

        Does NOT append to _change_history here — that is done by
        _on_screen_event callbacks (content_changed=True, screen_captured=False)
        to avoid double-counting.
        """
        if self._frame_pipeline is None or self._analyzer is None:
            return
        frame = self._frame_pipeline.latest_frame
        if frame is None:
            return

        from PIL import Image
        pil_image = Image.fromarray(frame.data)
        await self._analyzer.inject_frame(pil_image, frame.timestamp)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/vision/realtime/test_vision_cortex.py -x -v`
Expected: all 11 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/vision/realtime/vision_cortex.py tests/vision/realtime/test_vision_cortex.py
git commit -m "feat(vision_cortex): lifecycle (awaken/shutdown) + adaptive perception loop"
```

---

### Task 6: VisionCortex — Callback Dispatchers (Voice, Telemetry, Scene Graph)

**Files:**
- Modify: `backend/vision/realtime/vision_cortex.py`
- Add tests: `tests/vision/realtime/test_vision_cortex.py`

- [ ] **Step 1: Write failing tests for callbacks**

Append to test file:

```python
@pytest.mark.asyncio
async def test_on_screen_event_content_changed_updates_throttle():
    cortex = VisionCortex()
    cortex._screen_dispatch = cortex._build_screen_dispatch()
    cortex._change_history.clear()
    await cortex._on_screen_event('content_changed', {'app': 'Terminal'})
    # Should record a True change in history
    assert any(changed for _, changed in cortex._change_history)


@pytest.mark.asyncio
async def test_on_screen_event_error_detected_narrates():
    cortex = VisionCortex()
    cortex._screen_dispatch = cortex._build_screen_dispatch()
    with patch('backend.vision.realtime.vision_cortex._NARRATION_ENABLED', True):
        with patch('backend.vision.realtime.vision_cortex._safe_say', new_callable=AsyncMock) as mock_say:
            await cortex._on_screen_event('error_detected', {
                'app': 'Terminal', 'error_text': 'segfault'
            })
            mock_say.assert_called_once()


@pytest.mark.asyncio
async def test_on_workspace_event_space_switched():
    from backend.vision.multi_space_monitor import MonitorEvent, MonitorEventType
    from datetime import datetime
    cortex = VisionCortex()
    cortex._force_immediate_capture = MagicMock()
    event = MonitorEvent(
        event_type=MonitorEventType.SPACE_SWITCHED,
        timestamp=datetime.now(),
        space_id=2,
    )
    await cortex._on_workspace_event(event)
    cortex._force_immediate_capture.assert_called_once()
```

- [ ] **Step 2: Run to verify failures**

Run: `python3 -m pytest tests/vision/realtime/test_vision_cortex.py -x -v -k "on_screen or on_workspace"`
Expected: FAIL — missing methods

- [ ] **Step 3: Implement callback dispatchers**

Add to `vision_cortex.py`:

```python
    # ------------------------------------------------------------------
    # Callback dispatchers — registry-based, no if/elif chains (Manifesto ss5)
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
```

Add the `_safe_say` helper at module level:

```python
async def _safe_say(event_type: str, data: dict) -> None:
    """Voice narration for vision events. Dynamically builds message from event data."""
    try:
        from backend.core.supervisor.unified_voice_orchestrator import safe_say
        # Build message dynamically from event data — no hardcoded strings
        app = data.get('app_name') or data.get('app') or ''
        detail = data.get('error_text') or data.get('description') or event_type.replace('_', ' ')
        msg = f"{detail} — {app}".strip(' —') if app else detail
        if msg:
            await safe_say(msg, source="vision_cortex", skip_dedup=False)
    except ImportError:
        pass
    except Exception:
        pass
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/vision/realtime/test_vision_cortex.py -x -v`
Expected: all 14 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/vision/realtime/vision_cortex.py tests/vision/realtime/test_vision_cortex.py
git commit -m "feat(vision_cortex): callback dispatchers for voice, telemetry, and scene graph"
```

---

### Task 7: Supervisor Integration (Zone 6.5)

**Files:**
- Modify: `unified_supervisor.py:81916-81920`

- [ ] **Step 1: Read current state of Zone 6.5 to find exact insertion point**

Run: `grep -n "JARVIS_VISION_LOOP_ENABLED\|VisionActionLoop disabled\|vision_action_loop = None" unified_supervisor.py | tail -5`

- [ ] **Step 2: Add VisionCortex startup after VisionActionLoop**

After the block ending with `self._vision_action_loop = None` (the `else` branch for disabled vision loop), insert:

```python
            # v306.0 Zone 6.5b: VisionCortex — continuous screen awareness.
            # Wires Ferrari Engine → MemoryAwareScreenAnalyzer → MultiSpaceMonitor
            # into a unified perception system with adaptive throttle.
            # Non-blocking: failure degrades to reactive-only (VisionActionLoop).
            if _get_env_bool("JARVIS_VISION_CORTEX_ENABLED",
                             _get_env_bool("JARVIS_VISION_LOOP_ENABLED", False)):
                try:
                    from backend.vision.realtime.vision_cortex import VisionCortex
                    self._vision_cortex = VisionCortex()
                    VisionCortex.set_instance(self._vision_cortex)
                    _vc_timeout = _get_env_float("JARVIS_VISION_CORTEX_START_TIMEOUT", 10.0)
                    await asyncio.wait_for(
                        self._vision_cortex.awaken(),
                        timeout=_vc_timeout,
                    )
                    self.logger.info(
                        "[VisionCortex] Awake (activity=%s, interval=%.1fs)",
                        self._vision_cortex.activity_level.value,
                        self._vision_cortex.perception_interval,
                    )
                except asyncio.TimeoutError:
                    self.logger.warning(
                        "[VisionCortex] Start timed out — continuing without"
                    )
                    VisionCortex.set_instance(None)
                    self._vision_cortex = None
                except ImportError as ie:
                    self.logger.info(
                        "[VisionCortex] Not available: %s", ie
                    )
                    self._vision_cortex = None
                except Exception as exc:
                    self.logger.warning(
                        "[VisionCortex] Start failed: %s — continuing without", exc
                    )
                    VisionCortex.set_instance(None)
                    self._vision_cortex = None
            else:
                self._vision_cortex = None
```

- [ ] **Step 3: Run existing tests to verify no regression**

Run: `python3 -m pytest tests/vision/realtime/test_vision_action_loop.py -x -v`
Expected: 14 PASS (VisionActionLoop unchanged)

Run: `python3 -m pytest tests/vision/realtime/ -x -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add unified_supervisor.py
git commit -m "feat(supervisor): start VisionCortex at Zone 6.5b for continuous screen awareness"
```

---

### Task 8: Integration Test — Full Pipeline Smoke Test

**Files:**
- Create: `tests/vision/realtime/test_vision_cortex_integration.py`

- [ ] **Step 1: Write integration smoke test**

```python
# tests/vision/realtime/test_vision_cortex_integration.py
"""Integration smoke test: VisionCortex wires real subsystems together."""
import asyncio
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.vision.realtime.vision_cortex import VisionCortex, ActivityLevel
from backend.vision.realtime.frame_pipeline import FramePipeline, FrameData
from backend.vision.realtime.vision_action_loop import VisionActionLoop


@pytest.fixture(autouse=True)
def clean_singletons():
    VisionCortex.set_instance(None)
    VisionActionLoop.set_instance(None)
    yield
    VisionCortex.set_instance(None)
    VisionActionLoop.set_instance(None)


def _make_frame(n: int) -> FrameData:
    return FrameData(
        data=np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8),
        width=100, height=100,
        timestamp=float(n), frame_number=n,
    )


@pytest.mark.asyncio
async def test_cortex_discovers_vision_action_loop():
    """VisionCortex should find VisionActionLoop's frame pipeline."""
    val = VisionActionLoop(use_sck=False)
    assert VisionActionLoop.get_instance() is val

    cortex = VisionCortex()
    await cortex._discover_subsystems()

    assert cortex._frame_pipeline is val.frame_pipeline


@pytest.mark.asyncio
async def test_full_perception_cycle():
    """Frame flows: FramePipeline.latest_frame -> VisionCortex -> analyzer.inject_frame."""
    val = VisionActionLoop(use_sck=False)
    frame = _make_frame(1)
    val.frame_pipeline._latest_frame = frame  # Simulate captured frame

    cortex = VisionCortex()
    await cortex._discover_subsystems()

    if cortex._analyzer:
        with patch.object(cortex._analyzer, 'inject_frame', new_callable=AsyncMock) as mock_inject:
            await cortex._run_one_perception_cycle()
            mock_inject.assert_called_once()


@pytest.mark.asyncio
async def test_graceful_degradation_no_vision_loop():
    """VisionCortex starts even when VisionActionLoop is not running."""
    cortex = VisionCortex()
    await cortex._discover_subsystems()
    assert cortex._frame_pipeline is None
    # Perception cycle should be a no-op, not crash
    await cortex._run_one_perception_cycle()
```

- [ ] **Step 2: Run integration tests**

Run: `python3 -m pytest tests/vision/realtime/test_vision_cortex_integration.py -x -v`
Expected: all 3 PASS

- [ ] **Step 3: Run full test suite**

Run: `python3 -m pytest tests/vision/realtime/ -x -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add tests/vision/realtime/test_vision_cortex_integration.py
git commit -m "test(vision_cortex): integration smoke tests for full perception pipeline"
```
