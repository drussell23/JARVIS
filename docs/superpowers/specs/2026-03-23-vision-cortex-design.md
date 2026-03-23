# VisionCortex: Adaptive Real-Time Screen Awareness

**Date:** 2026-03-23
**Status:** Approved
**Manifesto alignment:** §1 Unified Organism, §2 Progressive Awakening, §3 Async Tendrils, §4 Synthetic Soul, §6 Neuroplasticity, §7 Absolute Observability

## Problem

JARVIS has six vision organs that all work independently but are not connected:

1. **Ferrari Engine** (FramePipeline) — captures 30fps via ScreenCaptureKit, frames pile up in a queue with no consumer
2. **MemoryAwareScreenAnalyzer** — has continuous monitoring with Phase 1/2 analysis, but uses its own `screencapture` subprocess and is not started at boot
3. **MultiSpaceMonitor** — tracks apps across macOS Spaces with a 5s polling loop, but is not started at boot
4. **VisionActionLoop** — reactive perception-action-verify loop, only activates on voice commands
5. **GhostDisplayManager** — moves windows to virtual display for monitoring, wired into AGI OS coordinator
6. **VisionRouter** — 3-tier cascade (L1 cache → L2 LLaVA/GCP → L3 Claude Vision), only called on-demand

The result: JARVIS has a camera recording (Ferrari Engine) but no brain watching the feed. Screen awareness only exists when the user explicitly asks for it.

## Solution

A single new coordinator — `VisionCortex` — that acts as connective tissue between all existing organs. It does not duplicate logic; it wires what already exists into a unified nervous system.

## Architecture

```
unified_supervisor.py (Zone 6.5)
    │
    └── VisionCortex.awaken()
            │
            ├── Ferrari Engine (FramePipeline) ── 30fps SCK capture
            │       │
            │       └──→ frame_queue (bounded, drop-oldest)
            │               │
            ├── VisionCortex._perception_loop()
            │       │  reads latest frame at adaptive interval (1-8s)
            │       │  converts numpy RGB → PIL Image
            │       │
            │       └──→ MemoryAwareScreenAnalyzer.inject_frame()
            │               │
            │               ├── Phase 1 (FREE): fingerprint + app detection
            │               │       │
            │               │       └── if content changed:
            │               │               │
            │               │               └── Phase 2: VisionRouter
            │               │                   (L1 cache → L2 LLaVA → L3 Claude)
            │               │
            │               └── fires event callbacks → VisionCortex._on_screen_event()
            │                       │
            │                       ├── content_changed → update KnowledgeFabric L1 cache
            │                       ├── app_changed → voice + TelemetryBus + ConsciousnessBridge
            │                       ├── error_detected → proactive voice reaction
            │                       ├── notification_detected → voice narration (debounced)
            │                       ├── meeting_detected → voice narration
            │                       ├── security_concern → immediate alert
            │                       └── screen_captured → update throttle rate
            │
            ├── MultiSpaceMonitor._monitor_loop() (every 5s)
            │       │
            │       └── workspace events → VisionCortex._on_workspace_event()
            │               │
            │               ├── SPACE_SWITCHED → force immediate Phase 1
            │               ├── APP_LAUNCHED/CLOSED/MOVED → enrich analyzer context
            │               └── WORKFLOW_DETECTED → TelemetryBus + ConsciousnessBridge
            │
            └── VisionActionLoop (unchanged, reactive)
                    │
                    └── NOW enriched: L1 scene graph pre-populated
                        by continuous analysis → <5ms hits instead of 2-4s L2
```

## Adaptive Perception Throttle

Perception intensity adapts to environmental stimulus (Manifesto §6). The throttle computes a `content_change_rate` from Phase 1 fingerprint diffs over a 60-second sliding window, stored as a `collections.deque(maxlen=120)` of `(timestamp, changed: bool)` tuples — bounded memory regardless of activity level.

| Level | Rate threshold | Phase 1 interval | Phase 2 | Description |
|-------|---------------|-------------------|---------|-------------|
| IDLE | rate < 0.02 | 8s | disabled | Screen hasn't changed |
| LOW | 0.02 <= rate < 0.1 | 5s | on change only | User is reading |
| NORMAL | 0.1 <= rate < 0.5 | 3s | on change | User is working |
| HIGH | rate >= 0.5 | 1s | on every change | Rapid activity |

All thresholds sourced from env vars:
- `VISION_CORTEX_IDLE_RATE` (default `0.02`)
- `VISION_CORTEX_LOW_RATE` (default `0.1`)
- `VISION_CORTEX_HIGH_RATE` (default `0.5`)
- `VISION_CORTEX_IDLE_INTERVAL` (default `8.0`)
- `VISION_CORTEX_LOW_INTERVAL` (default `5.0`)
- `VISION_CORTEX_NORMAL_INTERVAL` (default `3.0`)
- `VISION_CORTEX_HIGH_INTERVAL` (default `1.0`)

**Memory pressure override:** If process RSS exceeds `VISION_MEMORY_LIMIT_MB` (default 1500), the throttle forces IDLE regardless of activity. Note: this is the same env var used by MemoryAwareScreenAnalyzer for its own memory gating — both read a single process RSS value, which is correct since they share one process.

**Phase 2 gating:** Even in HIGH mode, Phase 2 only fires when the content fingerprint actually changed (existing `content_similarity_threshold=0.92`).

## Frame Bridge: Ferrari Engine → MemoryAwareScreenAnalyzer

### Frame access: last-frame cache (no queue contention)

VisionCortex does NOT call `get_frame()` on FramePipeline's queue. `get_frame()` is a destructive dequeue — if VisionCortex consumed a frame, VisionActionLoop's `execute_action()` could miss it during pre-action or verification captures, causing INCONCLUSIVE verifications.

Instead, FramePipeline exposes a **`latest_frame`** property — a non-destructive read of the most recently enqueued frame. FramePipeline stores `self._latest_frame: Optional[FrameData]` on every `_enqueue_frame()` call (~5 lines added). VisionCortex reads this property; VisionActionLoop keeps using `get_frame()` from the queue. Zero contention.

```python
# In FramePipeline (new, ~5 lines):
@property
def latest_frame(self) -> Optional[FrameData]:
    """Non-destructive read of the most recent frame. Thread-safe."""
    return self._latest_frame
```

### Perception loop

```python
async def _perception_loop(self):
    while self._running:
        interval = self._compute_interval()
        await asyncio.sleep(interval)

        # Non-destructive read — does NOT consume from queue
        frame = self._frame_pipeline.latest_frame
        if frame is None:
            continue

        # Convert numpy RGB → PIL Image for analyzer
        pil_image = Image.fromarray(frame.data)

        # Inject into analyzer (replaces its internal screencapture)
        await self._analyzer.inject_frame(pil_image, frame.timestamp)
```

**Fallback:** If Ferrari Engine is unavailable (no SCK, no screen recording permission), VisionCortex starts MemoryAwareScreenAnalyzer with its own `_monitoring_loop()` using `screencapture` subprocess.

**No queue contention:** VisionCortex reads `latest_frame` (non-destructive). VisionActionLoop reads `get_frame()` (destructive dequeue). They never compete for the same frame.

## Callback Wiring

VisionCortex registers itself as a callback consumer on both MemoryAwareScreenAnalyzer and MultiSpaceMonitor. It dispatches events to the appropriate subsystems without if/elif chains — each subsystem registers its own interest.

### Screen events (from MemoryAwareScreenAnalyzer)

| Event | Action |
|-------|--------|
| `content_changed` | Update KnowledgeFabric scene graph (feeds L1 cache) |
| `app_changed` | Update ConsciousnessBridge context, voice narration, TelemetryBus |
| `error_detected` | Proactive voice: "I see an error on your screen", TelemetryBus |
| `notification_detected` | Voice narration (debounced via existing dedup) |
| `meeting_detected` | Voice narration |
| `security_concern` | Immediate voice alert + security audit log |
| `screen_captured` | Update adaptive throttle rate calculation |

### Workspace events (from MultiSpaceMonitor)

VisionCortex registers handlers via `monitor.register_event_handler(event_type, handler)` for each `MonitorEventType` it cares about. Handlers receive a single `MonitorEvent` dataclass argument (not a tuple):

```python
async def _on_workspace_event(self, event: MonitorEvent) -> None:
```

Registered event types:

| MonitorEventType | Action |
|------------------|--------|
| `SPACE_SWITCHED` | Force immediate Phase 1 capture (new space = new content) |
| `APP_LAUNCHED` / `APP_CLOSED` / `APP_MOVED` | Feed context to analyzer |
| `WORKFLOW_DETECTED` | TelemetryBus + ConsciousnessBridge record |

### Voice narration

Uses existing `safe_say()` with `source="vision_cortex"`. Controlled by `JARVIS_VISION_NARRATION_ENABLED` (default `true`). The existing speech gate and dedup prevent chatty output.

### TelemetryBus integration

Every event emits a `TelemetryEnvelope` to the existing bus:
- `screen.content_changed@1.0.0`
- `screen.app_changed@1.0.0`
- `screen.error_detected@1.0.0`
- `workspace.space_switched@1.0.0`

This feeds EliteDashboard ticker, LifecycleVoiceNarrator, and ConsciousnessBridge (Manifesto §7).

## Supervisor Integration (Zone 6.5)

VisionCortex starts immediately after VisionActionLoop, as a non-blocking background tendril.

```python
# Zone 6.5 — after VisionActionLoop start
if _get_env_bool("JARVIS_VISION_CORTEX_ENABLED",
                  _get_env_bool("JARVIS_VISION_LOOP_ENABLED", False)):
    try:
        from backend.vision.realtime.vision_cortex import VisionCortex
        self._vision_cortex = VisionCortex()
        VisionCortex.set_instance(self._vision_cortex)
        await asyncio.wait_for(
            self._vision_cortex.awaken(),
            timeout=_get_env_float("JARVIS_VISION_CORTEX_START_TIMEOUT", 10.0),
        )
    except asyncio.TimeoutError:
        logger.warning("[VisionCortex] Start timed out — continuing without")
        VisionCortex.set_instance(None)
        self._vision_cortex = None
    except ImportError as ie:
        logger.info("[VisionCortex] Not available: %s", ie)
        self._vision_cortex = None
    except Exception as exc:
        logger.warning("[VisionCortex] Start failed: %s — continuing without", exc)
        VisionCortex.set_instance(None)
        self._vision_cortex = None
```

### Progressive readiness (Manifesto §2)

1. VisionCortex starts with Phase 1 only (local, free)
2. When GCP J-Prime comes online (Zone 6.6+), VisionRouter's L2 tier activates automatically via circuit breaker
3. No restart needed — cognition awakens progressively

### Failure isolation

- If VisionCortex fails to start: VisionActionLoop still works (reactive mode)
- If MemoryAwareScreenAnalyzer crashes mid-loop: VisionCortex catches exception, logs it, continues with MultiSpaceMonitor
- If MultiSpaceMonitor crashes: continuous screen analysis continues without space context
- No single organ death kills the organism

## File Changes

### New file (1)

**`backend/vision/realtime/vision_cortex.py`** (~350-400 lines)

```
class VisionCortex:
    _instance: Optional[VisionCortex] = None

    # Singleton
    @classmethod get_instance() -> Optional[VisionCortex]
    @classmethod set_instance(instance) -> None

    # Lifecycle
    async def awaken() -> None
    async def shutdown() -> None

    # Perception loop (adaptive throttle)
    async def _perception_loop() -> None
    def _compute_interval() -> float
    def _compute_activity_rate() -> float
    def _update_activity_level() -> None

    # Callback dispatchers (no if/elif chains)
    async def _on_screen_event(event_type: str, data: dict) -> None
    async def _on_workspace_event(event: MonitorEvent) -> None

    # Frame bridge
    async def _inject_frame_to_analyzer(frame: FrameData) -> None

    # Scene graph bridge
    async def _update_scene_graph(analysis_result: dict) -> None

    # Properties
    @property activity_level -> str
    @property perception_interval -> float
    @property is_awake -> bool
```

### Modified files (4)

**`backend/vision/continuous_screen_analyzer.py`** (~40 lines added)
- Refactor `_phase1_capture_and_detect()` to accept optional `injected_image: Optional[Image.Image] = None` parameter
- When `injected_image` is provided, skip the `capture_screen()` call and use the injected image directly
- All subsequent logic (fingerprinting, `_quick_screen_analysis()` for focused app via NSWorkspace, event firing) runs identically
- Add thin public `inject_frame()` wrapper:
  ```python
  async def inject_frame(self, pil_image: Image.Image, timestamp: float) -> None:
      phase1 = await self._phase1_capture_and_detect(injected_image=pil_image)
      if phase1 and phase1.get('needs_full_analysis'):
          await self._phase2_analyze_if_memory_allows(phase1)
  ```

**`backend/vision/realtime/frame_pipeline.py`** (~8 lines added)
- Add `self._latest_frame: Optional[FrameData] = None` in `__init__`
- Set `self._latest_frame = frame` in `_enqueue_frame()` before queue insertion
- Add `@property latest_frame` — non-destructive read for VisionCortex

**`unified_supervisor.py`** (~30 lines added at Zone 6.5)
- After VisionActionLoop start, create and awaken VisionCortex
- Three-branch exception pattern (TimeoutError, ImportError, Exception) matching VisionActionLoop
- Failure paths clear singleton via `VisionCortex.set_instance(None)`

**`backend/vision/realtime/vision_action_loop.py`** (~6 lines added)
- Add `@property frame_pipeline` — exposes `_frame_pipeline` for VisionCortex to share the frame source
- Add `@property knowledge_fabric` — exposes `_knowledge_fabric` for VisionCortex to update the L1 scene graph

### Not modified

- VisionRouter — unchanged, VisionCortex calls it for Phase 2
- ActionExecutor / Ghost Hands — unchanged
- ActionVerifier — unchanged
- MultiSpaceMonitor — unchanged, VisionCortex just starts it and registers callbacks
- GhostDisplayManager — unchanged, already wired in AGI OS
- RuntimeTaskOrchestrator — unchanged, already routes to VisionActionLoop

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_VISION_CORTEX_ENABLED` | inherits `JARVIS_VISION_LOOP_ENABLED` | Feature gate |
| `JARVIS_VISION_CORTEX_START_TIMEOUT` | `10.0` | Boot timeout (seconds) |
| `JARVIS_VISION_NARRATION_ENABLED` | `true` | Voice reactions to screen events |
| `VISION_CORTEX_IDLE_RATE` | `0.02` | Changes/sec threshold for IDLE |
| `VISION_CORTEX_LOW_RATE` | `0.1` | Changes/sec threshold for LOW |
| `VISION_CORTEX_HIGH_RATE` | `0.5` | Changes/sec threshold for HIGH |
| `VISION_CORTEX_IDLE_INTERVAL` | `8.0` | Phase 1 interval in IDLE (seconds) |
| `VISION_CORTEX_LOW_INTERVAL` | `5.0` | Phase 1 interval in LOW (seconds) |
| `VISION_CORTEX_NORMAL_INTERVAL` | `3.0` | Phase 1 interval in NORMAL (seconds) |
| `VISION_CORTEX_HIGH_INTERVAL` | `1.0` | Phase 1 interval in HIGH (seconds) |
| `VISION_CORTEX_RATE_WINDOW_S` | `60.0` | Sliding window for rate calculation |

All existing `VISION_*` env vars from MemoryAwareScreenAnalyzer, FramePipeline, and VisionRouter remain unchanged and in effect.

## Testing Strategy

- Unit tests for VisionCortex in isolation (mock FramePipeline, mock analyzer)
- Test adaptive throttle: inject change events, verify interval adjusts
- Test frame bridge: inject numpy frame, verify analyzer receives PIL image
- Test callback wiring: fire screen events, verify voice/telemetry dispatched
- Test graceful degradation: start without Ferrari Engine, verify screencapture fallback
- Test failure isolation: crash analyzer mid-loop, verify cortex continues
- Test frame contention: VisionCortex reads `latest_frame` while VisionActionLoop calls `get_frame()` concurrently — verify VisionActionLoop always gets its frames for pre-action and verification captures
- Existing VisionActionLoop tests pass unchanged (14/14)
