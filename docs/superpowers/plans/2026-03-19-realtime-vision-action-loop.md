# Real-Time Vision Action Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a real-time vision action loop where JARVIS streams video at 30 FPS, detects screen changes, analyzes frames through tiered vision (scene graph → J-Prime GPU → Claude fallback), and executes UI actions with safety gates and verification — replacing Claude Computer Use's screenshot model.

**Architecture:** 11-state machine on JARVIS Body with PRECHECK safety gate. Frame pipeline: SCK stream → bounded queue → motion detect → scene graph (L1) → J-Prime vision (L2). Unified Knowledge Fabric with 3 partitions (scene/semantic/trinity). Reactor Core learns from action outcomes.

**Tech Stack:** Python 3.11+, asyncio, ScreenCaptureKit (via native C++ extension), NetworkX (scene graph), pyautogui (actions), aiohttp (J-Prime calls), SQLite (knowledge persistence), pytest

**Spec:** `docs/superpowers/specs/2026-03-19-realtime-vision-action-loop-design.md`

**Repos:** JARVIS-AI-Agent (Tasks 1-8), jarvis-prime (Tasks 9-10), reactor-core (Task 11)

---

## Phase A: Foundation (Tasks 1-4) — Frame Pipeline + State Machine

These tasks build the core vision loop on JARVIS. Testable independently with mock frames.

---

### Task 1: Pipeline State Enum + Transitions (JARVIS)

**Files:**
- Create: `backend/vision/realtime/__init__.py`
- Create: `backend/vision/realtime/states.py`
- Test: `tests/vision/realtime/test_states.py`

- [ ] **Step 1: Create package**

```bash
cd ~/Documents/repos/JARVIS-AI-Agent
mkdir -p backend/vision/realtime tests/vision/realtime
touch backend/vision/realtime/__init__.py tests/vision/__init__.py tests/vision/realtime/__init__.py
```

- [ ] **Step 2: Write failing tests**

```python
"""Tests for vision action loop state machine."""
import pytest
from backend.vision.realtime.states import (
    VisionState, VisionEvent, TransitionError,
    VisionStateMachine,
)


class TestLegalTransitions:
    def test_idle_to_watching(self):
        sm = VisionStateMachine()
        sm.transition(VisionEvent.START)
        assert sm.state == VisionState.WATCHING

    def test_watching_to_change_detected(self):
        sm = VisionStateMachine()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        assert sm.state == VisionState.CHANGE_DETECTED

    def test_precheck_is_only_path_to_acting(self):
        sm = VisionStateMachine()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        sm.transition(VisionEvent.ACTION_REQUESTED)
        sm.transition(VisionEvent.BURST_COMPLETE)
        # Must go through PRECHECK
        assert sm.state == VisionState.PRECHECK
        sm.transition(VisionEvent.ALL_GUARDS_PASS)
        assert sm.state == VisionState.ACTING


class TestIllegalTransitions:
    def test_cannot_skip_precheck(self):
        sm = VisionStateMachine()
        sm.transition(VisionEvent.START)
        # Cannot go from WATCHING directly to ACTING
        with pytest.raises(TransitionError):
            sm.transition(VisionEvent.ACTION_DISPATCHED)

    def test_cannot_transition_from_failed_except_ack(self):
        sm = VisionStateMachine()
        sm._state = VisionState.FAILED
        with pytest.raises(TransitionError):
            sm.transition(VisionEvent.START)
        # But user_acknowledges works
        sm.transition(VisionEvent.USER_ACKNOWLEDGES)
        assert sm.state == VisionState.WATCHING


class TestDegradedPath:
    def test_analyzing_to_degraded(self):
        sm = VisionStateMachine()
        sm._state = VisionState.ANALYZING
        sm.transition(VisionEvent.VISION_UNAVAILABLE)
        assert sm.state == VisionState.DEGRADED

    def test_degraded_to_recovering(self):
        sm = VisionStateMachine()
        sm._state = VisionState.DEGRADED
        sm.transition(VisionEvent.HEALTH_CHECK_PASS)
        assert sm.state == VisionState.RECOVERING

    def test_recovering_needs_3_healthy(self):
        sm = VisionStateMachine()
        sm._state = VisionState.RECOVERING
        sm.transition(VisionEvent.N_CONSECUTIVE_HEALTHY)
        assert sm.state == VisionState.WATCHING


class TestRetryBounds:
    def test_retry_targeting_increments(self):
        sm = VisionStateMachine()
        sm._state = VisionState.RETRY_TARGETING
        sm._retry_count = 0
        sm.transition(VisionEvent.RETRY)
        assert sm.state == VisionState.ACTION_TARGETING
        assert sm._retry_count == 1

    def test_retry_exceeded_goes_to_failed(self):
        sm = VisionStateMachine()
        sm._state = VisionState.RETRY_TARGETING
        sm._retry_count = 3  # max K=2 retries, so 3 means exceeded
        sm.transition(VisionEvent.RETRY_EXCEEDED)
        assert sm.state == VisionState.FAILED


class TestTelemetry:
    def test_transition_emits_record(self):
        sm = VisionStateMachine()
        records = []
        sm.on_transition = lambda r: records.append(r)
        sm.transition(VisionEvent.START)
        assert len(records) == 1
        assert records[0]["from"] == "IDLE"
        assert records[0]["to"] == "WATCHING"
```

- [ ] **Step 3: Run tests — verify FAIL**

- [ ] **Step 4: Implement states.py**

```python
"""
Vision Action Loop — State Machine with enforced legal transitions.

11 states, deterministic transition table. Every transition emits telemetry.
No state can skip to a non-adjacent state.

Spec: Section 3 of realtime-vision-action-loop-design.md
"""
from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("vision.realtime.states")


class VisionState(str, Enum):
    IDLE = "IDLE"
    WATCHING = "WATCHING"
    CHANGE_DETECTED = "CHANGE_DETECTED"
    ANALYZING = "ANALYZING"
    ACTION_TARGETING = "ACTION_TARGETING"
    PRECHECK = "PRECHECK"
    ACTING = "ACTING"
    VERIFYING = "VERIFYING"
    RETRY_TARGETING = "RETRY_TARGETING"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"


class VisionEvent(str, Enum):
    START = "START"
    STOP = "STOP"
    MOTION_DETECTED = "MOTION_DETECTED"
    NO_CHANGE = "NO_CHANGE"
    SAMPLE_FRAME = "SAMPLE_FRAME"
    NO_FRAME_TIMEOUT = "NO_FRAME_TIMEOUT"
    ANALYSIS_COMPLETE = "ANALYSIS_COMPLETE"
    ACTION_REQUESTED = "ACTION_REQUESTED"
    VISION_UNAVAILABLE = "VISION_UNAVAILABLE"
    BURST_COMPLETE = "BURST_COMPLETE"
    ESCALATION_EXHAUSTED = "ESCALATION_EXHAUSTED"
    ALL_GUARDS_PASS = "ALL_GUARDS_PASS"
    FRESHNESS_FAIL = "FRESHNESS_FAIL"
    CONFIDENCE_FAIL = "CONFIDENCE_FAIL"
    RISK_REQUIRES_APPROVAL = "RISK_REQUIRES_APPROVAL"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    IDEMPOTENCY_HIT = "IDEMPOTENCY_HIT"
    INTENT_EXPIRED = "INTENT_EXPIRED"
    ACTION_DISPATCHED = "ACTION_DISPATCHED"
    POSTCONDITION_MET = "POSTCONDITION_MET"
    POSTCONDITION_FAIL = "POSTCONDITION_FAIL"
    RETRY = "RETRY"
    RETRY_EXCEEDED = "RETRY_EXCEEDED"
    HEALTH_CHECK_PASS = "HEALTH_CHECK_PASS"
    HEALTH_CHECK_FAIL = "HEALTH_CHECK_FAIL"
    N_CONSECUTIVE_HEALTHY = "N_CONSECUTIVE_HEALTHY"
    USER_ACKNOWLEDGES = "USER_ACKNOWLEDGES"


class TransitionError(Exception):
    """Raised when an illegal state transition is attempted."""
    pass


# Transition table: (from_state, event) -> to_state
# Only legal transitions are in this table. Anything else raises TransitionError.
_TRANSITIONS: Dict[Tuple[VisionState, VisionEvent], VisionState] = {
    (VisionState.IDLE, VisionEvent.START): VisionState.WATCHING,
    (VisionState.WATCHING, VisionEvent.MOTION_DETECTED): VisionState.CHANGE_DETECTED,
    (VisionState.WATCHING, VisionEvent.NO_CHANGE): VisionState.WATCHING,
    (VisionState.WATCHING, VisionEvent.STOP): VisionState.IDLE,
    (VisionState.CHANGE_DETECTED, VisionEvent.SAMPLE_FRAME): VisionState.ANALYZING,
    (VisionState.CHANGE_DETECTED, VisionEvent.NO_FRAME_TIMEOUT): VisionState.WATCHING,
    (VisionState.ANALYZING, VisionEvent.ANALYSIS_COMPLETE): VisionState.WATCHING,
    (VisionState.ANALYZING, VisionEvent.ACTION_REQUESTED): VisionState.ACTION_TARGETING,
    (VisionState.ANALYZING, VisionEvent.VISION_UNAVAILABLE): VisionState.DEGRADED,
    (VisionState.ACTION_TARGETING, VisionEvent.BURST_COMPLETE): VisionState.PRECHECK,
    (VisionState.ACTION_TARGETING, VisionEvent.ESCALATION_EXHAUSTED): VisionState.PRECHECK,
    (VisionState.ACTION_TARGETING, VisionEvent.VISION_UNAVAILABLE): VisionState.DEGRADED,
    (VisionState.PRECHECK, VisionEvent.ALL_GUARDS_PASS): VisionState.ACTING,
    (VisionState.PRECHECK, VisionEvent.FRESHNESS_FAIL): VisionState.ACTION_TARGETING,
    (VisionState.PRECHECK, VisionEvent.CONFIDENCE_FAIL): VisionState.FAILED,
    (VisionState.PRECHECK, VisionEvent.RISK_REQUIRES_APPROVAL): VisionState.ACTING,  # after approval
    (VisionState.PRECHECK, VisionEvent.APPROVAL_DENIED): VisionState.FAILED,
    (VisionState.PRECHECK, VisionEvent.IDEMPOTENCY_HIT): VisionState.WATCHING,
    (VisionState.PRECHECK, VisionEvent.INTENT_EXPIRED): VisionState.FAILED,
    (VisionState.ACTING, VisionEvent.ACTION_DISPATCHED): VisionState.VERIFYING,
    (VisionState.VERIFYING, VisionEvent.POSTCONDITION_MET): VisionState.WATCHING,
    (VisionState.VERIFYING, VisionEvent.POSTCONDITION_FAIL): VisionState.RETRY_TARGETING,
    (VisionState.RETRY_TARGETING, VisionEvent.RETRY): VisionState.ACTION_TARGETING,
    (VisionState.RETRY_TARGETING, VisionEvent.RETRY_EXCEEDED): VisionState.FAILED,
    (VisionState.DEGRADED, VisionEvent.HEALTH_CHECK_PASS): VisionState.RECOVERING,
    (VisionState.DEGRADED, VisionEvent.STOP): VisionState.IDLE,
    (VisionState.RECOVERING, VisionEvent.N_CONSECUTIVE_HEALTHY): VisionState.WATCHING,
    (VisionState.RECOVERING, VisionEvent.HEALTH_CHECK_FAIL): VisionState.DEGRADED,
    (VisionState.FAILED, VisionEvent.USER_ACKNOWLEDGES): VisionState.WATCHING,
    (VisionState.FAILED, VisionEvent.STOP): VisionState.IDLE,
}
# Any state can STOP
for state in VisionState:
    if state not in (VisionState.IDLE,):
        key = (state, VisionEvent.STOP)
        if key not in _TRANSITIONS:
            _TRANSITIONS[key] = VisionState.IDLE


class VisionStateMachine:
    """Deterministic state machine with enforced legal transitions."""

    MAX_RETRIES = 2  # 3 total attempts (1 original + 2 retries)

    def __init__(self) -> None:
        self._state = VisionState.IDLE
        self._retry_count = 0
        self._action_id: Optional[str] = None
        self._committed_actions: set = set()
        self.on_transition: Optional[Callable[[Dict], None]] = None

    @property
    def state(self) -> VisionState:
        return self._state

    def transition(self, event: VisionEvent) -> VisionState:
        key = (self._state, event)
        if key not in _TRANSITIONS:
            raise TransitionError(
                f"Illegal transition: {self._state.value} + {event.value}"
            )

        from_state = self._state
        to_state = _TRANSITIONS[key]

        # Side effects
        if to_state == VisionState.ACTION_TARGETING and from_state == VisionState.RETRY_TARGETING:
            self._retry_count += 1
        if to_state == VisionState.ACTION_TARGETING and from_state != VisionState.RETRY_TARGETING:
            self._retry_count = 0
        if to_state == VisionState.WATCHING:
            self._retry_count = 0

        self._state = to_state

        # Emit telemetry
        if self.on_transition:
            self.on_transition({
                "from": from_state.value,
                "to": to_state.value,
                "event": event.value,
                "timestamp": time.time(),
                "retry_count": self._retry_count,
            })

        return to_state
```

- [ ] **Step 5: Run tests — verify ALL PASS**

- [ ] **Step 6: Commit**

```bash
git add backend/vision/realtime/ tests/vision/
git commit -m "feat(vision): add real-time vision state machine with enforced transitions

11 states, deterministic transition table. PRECHECK is the only path
to ACTING. Bounded retries (max 2). Degraded/Recovering with hysteresis.
Every transition emits telemetry record."
```

---

### Task 2: PRECHECK Gate (JARVIS)

**Files:**
- Create: `backend/vision/realtime/precheck_gate.py`
- Test: `tests/vision/realtime/test_precheck.py`

- [ ] **Step 1: Write failing tests**

Tests needed:
- `test_fresh_frame_passes`: frame_age_ms=100, freshness_ms=500 → passes
- `test_stale_frame_fails`: frame_age_ms=600, freshness_ms=500 → FRESHNESS_FAIL
- `test_high_confidence_passes`: fused_confidence=0.85, threshold=0.75 → passes
- `test_low_confidence_fails`: fused_confidence=0.60 → CONFIDENCE_FAIL
- `test_high_risk_requires_approval`: action_type="email_compose" → RISK_REQUIRES_APPROVAL
- `test_safe_action_no_approval`: action_type="click" → passes
- `test_idempotency_catches_duplicate`: action_id already committed → IDEMPOTENCY_HIT
- `test_intent_expired`: intent_timestamp 3s ago → INTENT_EXPIRED
- `test_intent_fresh`: intent_timestamp 1s ago → passes
- `test_all_guards_pass_returns_passed_true`: all 5 guards OK → PrecheckResult(passed=True)
- `test_internal_error_fail_closed`: inject exception → passed=False, PRECHECK_INTERNAL_ERROR
- `test_degraded_mode_all_actions_need_approval`: degraded=True → RISK_REQUIRES_APPROVAL always

- [ ] **Step 2: Run tests — verify FAIL**

- [ ] **Step 3: Implement precheck_gate.py**

5 guards, fail-closed. Same high-risk task types as ValidationNode in Step 2. PrecheckResult dataclass. All thresholds configurable via env vars (`VISION_FRESHNESS_MS`, `VISION_CONFIDENCE_THRESHOLD`, `VISION_INTENT_EXPIRY_S`).

- [ ] **Step 4: Run tests — verify ALL PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(vision): add PRECHECK gate with 5 deterministic guards

Freshness, confidence, risk class, idempotency, intent expiry.
Fail-closed: internal error -> PRECHECK_INTERNAL_ERROR. All thresholds
env-var configurable. Degraded mode requires approval for all actions."
```

---

### Task 3: Confidence Fusion (JARVIS)

**Files:**
- Create: `backend/vision/realtime/fusion.py`
- Test: `tests/vision/realtime/test_fusion.py`

- [ ] **Step 1: Write failing tests**

Tests for `fuse_burst_results()`:
- `test_single_result_returns_as_is`: 1 hit → coords and confidence unchanged
- `test_three_agreeing_results`: 3 results within 10px → median coords, high confidence
- `test_outlier_rejection`: 2 results at (500,500), 1 at (100,100) → outlier rejected, median of 2
- `test_all_outliers_uses_best`: all disagree by >50px → highest confidence result, penalized
- `test_high_jitter_penalizes_confidence`: results spread >20px → confidence * 0.8
- `test_extreme_jitter_double_penalty`: results spread >50px → confidence * 0.8 * 0.6
- `test_no_hits_returns_zero`: all results status="not_found" → confidence=0.0
- `test_empty_input`: empty list → confidence=0.0, coords=None
- `test_deterministic`: same input twice → same output

- [ ] **Step 2-5: TDD cycle + commit**

```bash
git commit -m "feat(vision): add confidence fusion with median coords + outlier rejection

Median coordinates robust to outliers. 50px outlier threshold.
Jitter penalty: >20px = 20%, >50px = additional 40%. Deterministic."
```

---

### Task 4: Frame Pipeline + Motion Detector (JARVIS)

**Files:**
- Create: `backend/vision/realtime/frame_pipeline.py`
- Test: `tests/vision/realtime/test_frame_pipeline.py`

- [ ] **Step 1: Write failing tests**

Tests for `FramePipeline` and `MotionDetector`:
- `test_motion_detector_detects_change`: two different frames → changed=True
- `test_motion_detector_ignores_same_frame`: two identical frames → changed=False
- `test_debounce_suppresses_rapid_changes`: 10 changes in 50ms → only 1 reported
- `test_animated_ui_throttle`: change rate > max_hz → reduced to 1 FPS
- `test_bounded_queue_drops_oldest`: push 15 frames into queue(maxsize=10) → only 10 remain
- `test_pipeline_start_stop_lifecycle`: start → stop → no orphan tasks
- `test_pipeline_cancellation_propagates`: cancel parent → all child tasks cancelled

- [ ] **Step 2-5: TDD cycle + commit**

Key implementation: `MotionDetector` uses perceptual hash (dhash 8x8) with configurable threshold. `FramePipeline` wraps SCK `AsyncCaptureStream` with bounded asyncio.Queue and motion detection. Falls back to `screencapture` subprocess when SCK unavailable.

```bash
git commit -m "feat(vision): add frame pipeline with motion detection + bounded queue

SCK stream wrapper with dhash motion detection. Debounce (100ms),
animated UI throttle (max 5Hz). Bounded queue (10 frames, drop_oldest).
Structured concurrency: all tasks tracked, cancellation propagates."
```

---

## Phase B: Knowledge Fabric (Tasks 5-6)

### Task 5: Knowledge Fabric API + Scene Partition (JARVIS)

**Files:**
- Create: `backend/knowledge/__init__.py`
- Create: `backend/knowledge/schema.py`
- Create: `backend/knowledge/fabric.py`
- Create: `backend/knowledge/scene_partition.py`
- Create: `backend/knowledge/fabric_router.py`
- Test: `tests/knowledge/test_fabric.py`
- Test: `tests/knowledge/test_scene_partition.py`

- [ ] **Step 1: Write failing tests**

Tests for schema: global ID format (`kg://scene/...`, `kg://semantic/...`, `kg://trinity/...`), entity types, freshness classes.

Tests for scene partition: add element → query by ID → returns position. TTL expiry (5s). Update element position. Query nearest element to coords.

Tests for fabric router: write to scene entity → routes to scene partition. Read semantic entity → routes to semantic partition. L1 fresh beats L2.

- [ ] **Step 2-5: TDD cycle + commit**

Scene partition wraps `SemanticSceneGraph` (existing at `backend/vision/intelligence/semantic_scene_graph.py`) with TTL + global IDs. Fabric API provides unified `query()`, `write()`, `query_nearest()`.

```bash
git commit -m "feat(knowledge): add Unified Knowledge Fabric with scene partition

One API, three partition routing. Global IDs (kg://scene/...).
Scene partition: NetworkX in-memory, 5s TTL, <5ms query target.
Fabric router: deterministic read/write routing by entity type."
```

---

### Task 6: Vision Router (JARVIS)

**Files:**
- Create: `backend/vision/realtime/vision_router.py`
- Test: `tests/vision/realtime/test_vision_router.py`

- [ ] **Step 1: Write failing tests**

Tests for `VisionRouter`:
- `test_l1_cache_hit_skips_remote`: scene partition has element → returns in <5ms, no remote call
- `test_l1_miss_calls_l2`: scene partition empty → calls J-Prime LLaVA
- `test_l2_unavailable_calls_l3`: J-Prime down → calls Claude Vision
- `test_l3_unavailable_returns_degraded`: Claude also down → returns None, sets degraded
- `test_brain_selector_routes_by_task`: scene_understanding → LLaVA, ui_element_detection → Qwen-VL
- `test_updates_scene_graph_on_l2_result`: L2 returns element → scene partition updated

- [ ] **Step 2-5: TDD cycle + commit**

VisionRouter queries L1 scene partition first. On miss, calls `MindClient.send_vision_frame()` (L2). On L2 failure, falls back to Claude Vision API (L3). Brain selector picks model via `brain_selection_policy.yaml` vision_models section.

```bash
git commit -m "feat(vision): add tiered VisionRouter (L1 scene → L2 J-Prime → L3 Claude)

Scene graph cache hit skips remote. Brain selector routes by vision task
type. Results update scene graph. Independent operational level from
MindClient reasoning."
```

---

## Phase C: Action Execution + Verification (Tasks 7-8)

### Task 7: Action Executor + Verifier (JARVIS)

**Files:**
- Create: `backend/vision/realtime/action_executor.py`
- Create: `backend/vision/realtime/verification.py`
- Test: `tests/vision/realtime/test_action_executor.py`
- Test: `tests/vision/realtime/test_verification.py`

- [ ] **Step 1: Write failing tests**

Action executor tests: click dispatches pyautogui.click(x, y). Type dispatches pyautogui.typewrite(text). Scroll dispatches pyautogui.scroll(amount). Action records action_id in committed set.

Verification tests: click success (frame diff at coords shows change). Click fail (no change). Type success (text appears). Partial completion detected. Max retries bounded.

- [ ] **Step 2-5: TDD cycle + commit**

```bash
git commit -m "feat(vision): add action executor + post-action verification

Click/type/scroll via pyautogui with action_id tracking. Verification
captures post-action frame, checks postconditions per action type.
Partial completion detection for type actions."
```

---

### Task 8: Vision Action Loop Orchestrator (JARVIS)

**Files:**
- Create: `backend/vision/realtime/vision_action_loop.py`
- Create: `backend/vision/realtime/metrics.py`
- Modify: `backend/api/unified_command_processor.py`
- Modify: `backend/core/mind_client.py`
- Test: `tests/vision/realtime/test_vision_action_loop.py`

- [ ] **Step 1: Write failing tests**

Integration tests for `VisionActionLoop`:
- `test_full_cycle_idle_to_verified`: mock frames → change detected → analyze → target → precheck → act → verify → watching
- `test_static_screen_no_analysis`: identical frames → stays in WATCHING, zero vision calls
- `test_degraded_mode_requires_approval`: mock J-Prime unavailable → DEGRADED → all actions need approval
- `test_stale_frame_retargets`: inject old frame → PRECHECK catches → re-targets
- `test_retry_bounded`: verification fails 3 times → FAILED
- `test_metrics_emitted`: action produces VisionActionRecord with all provenance fields

- [ ] **Step 2-5: TDD cycle**

VisionActionLoop orchestrates: FramePipeline + VisionStateMachine + VisionRouter + PrecheckGate + ActionExecutor + Verifier + KnowledgeFabric + Metrics.

Wire into command processor: `_execute_single_step()` routes `tool_required="computer_use"` or `"visual_browser"` to `VisionActionLoop.execute_action(goal, target_description)`.

Add `MindClient.send_vision_frame()` for L2 calls.

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(vision): add VisionActionLoop orchestrator — the real-time eye

Wires state machine + frame pipeline + vision router + precheck gate +
executor + verifier into one async loop. Emits VisionActionRecord for
full decision provenance. Wired into command processor for plan steps."
```

---

## Phase D: J-Prime Vision Endpoint (Tasks 9-10)

### Task 9: /v1/vision/analyze Endpoint (J-Prime)

**Files:**
- Create: `jarvis_prime/reasoning/vision_assist.py`
- Modify: `jarvis_prime/reasoning/endpoints.py`
- Modify: `jarvis_prime/server.py`
- Test: `tests/reasoning/test_vision_assist.py`

- [ ] **Step 1: Write failing tests**

Tests for `handle_vision_analyze()`:
- `test_find_element_returns_coords`: mock model returns element → coords + confidence
- `test_not_found_returns_status`: model finds nothing → status="not_found"
- `test_multiple_elements`: model finds 3 buttons → all returned sorted by confidence
- `test_model_failure_returns_error`: model unavailable → status="error"
- `test_coord_space_is_logical_pixels`: response.coord_space == "logical_pixels"

- [ ] **Step 2-5: TDD cycle + commit**

Uses `ModelProvider` (same DI pattern as reasoning graph). System prompt asks model to identify UI elements and return JSON with coords.

```bash
cd ~/Documents/repos/jarvis-prime
git commit -m "feat(vision): add POST /v1/vision/analyze endpoint

Accepts frame artifact_ref + target description, returns element coords
with confidence. Uses ModelProvider DI. Logical pixel coordinate space."
```

---

### Task 10: Semantic Partition (J-Prime)

**Files:**
- Create: `jarvis_prime/knowledge/__init__.py`
- Create: `jarvis_prime/knowledge/semantic_partition.py`
- Test: `tests/knowledge/test_semantic_partition.py`

- [ ] **Step 1-5: TDD cycle**

Wraps existing knowledge patterns. Stores learned UI patterns with 24h TTL. Vector search via ChromaDB (if available, graceful fallback to keyword match). Global IDs (`kg://semantic/...`).

```bash
cd ~/Documents/repos/jarvis-prime
git commit -m "feat(knowledge): add semantic partition for learned UI patterns

24h TTL, ChromaDB vector search with keyword fallback. Global IDs.
Stores app-specific UI patterns learned from vision action outcomes."
```

---

## Phase E: Reactor Core Learning (Task 11)

### Task 11: Vision Experience Types + Calibrator (Reactor Core)

**Files:**
- Modify: `reactor_core/integration/trinity_experience_receiver.py`
- Create: `reactor_core/training/vision_calibrator.py`
- Test: `tests/training/test_vision_calibrator.py`

- [ ] **Step 1-5: TDD cycle**

Register 4 new experience types: `vision_action_outcome`, `scene_graph_accuracy`, `vision_model_performance`, `knowledge_fabric_health`.

VisionCalibrator: receives `vision_action_outcome` events, tracks per-task-type confidence vs actual success rate, produces `adjusted_thresholds.json` when calibration drifts beyond threshold.

```bash
cd ~/Documents/repos/reactor-core
git commit -m "feat(training): add vision calibrator for confidence threshold learning

4 new experience types. VisionCalibrator tracks confidence vs success rate
per task_type. Produces adjusted_thresholds.json for PRECHECK gate calibration."
```

---

## Task Dependencies

```
Task 1 (states)
  ├─► Task 2 (precheck) ─────────────────┐
  ├─► Task 3 (fusion) ──────────────────┐ │
  └─► Task 4 (frame pipeline) ─────────┐│ │
                                        ││ │
Task 5 (knowledge fabric) ─────────────┐││ │
Task 6 (vision router) ◄──── Task 5 ──┘││ │
                                        ││ │
Task 7 (executor + verifier) ◄──────────┘│ │
                                         │ │
Task 8 (orchestrator) ◄── ALL above ─────┘ │
                                           │
Task 9 (J-Prime vision endpoint) ──────────┘
Task 10 (semantic partition) — independent

Task 11 (Reactor Core) — independent, can be done in parallel
```

---

## Smoke Test (after all tasks)

```bash
# 1. Verify J-Prime vision endpoint
curl -s -X POST http://136.113.252.164:8000/v1/vision/analyze \
  -H "Content-Type: application/json" \
  -d '{"request_id":"smoke","session_id":"s","trace_id":"t","frame":{"artifact_ref":"test","width":1440,"height":900,"scale_factor":2.0,"captured_at_ms":0,"display_id":0},"task":{"type":"find_element","target_description":"any button","action_intent":"click"}}' \
  | python3 -m json.tool

# 2. Verify vision action loop on JARVIS (with mock frames)
python3 -c "
import asyncio
from backend.vision.realtime.vision_action_loop import VisionActionLoop
loop = VisionActionLoop()
result = asyncio.run(loop.execute_action('click the submit button'))
print('Result:', result)
"

# 3. Verify knowledge fabric
python3 -c "
from backend.knowledge.fabric import KnowledgeFabric
kf = KnowledgeFabric()
kf.write('kg://scene/button/test-001', {'position': (100, 200), 'confidence': 0.9})
result = kf.query('kg://scene/button/test-001')
print('KG result:', result)
"
```
