# Real-Time Vision Action Loop — Design Spec

> **Status**: Approved
> **Date**: 2026-03-19
> **Scope**: JARVIS (Body) + J-Prime (Mind) + Reactor Core (Soul)
> **Parent**: Unified Thinking Pipeline (Mind/Body/Soul Trinity architecture)
> **Depends on**: Step 0+1+2 complete (protocol, brain selector, reasoning graph, MindClient)

---

## 1. Goal

Ship a real-time vision action loop where JARVIS streams video at 30+ FPS via ScreenCaptureKit, detects screen changes via motion detection, analyzes frames through a tiered vision pipeline (local scene graph → J-Prime GPU → Claude fallback), and executes UI actions (click, type, scroll) with pre-action safety gates and post-action verification.

This replaces Claude Computer Use's screenshot-wait-respond-wait model (~0.2 FPS, $0.01/action) with a continuous streaming model (~30 FPS, $0/action for local path).

---

## 2. Operating Mode: Hybrid (C)

- Stream always-on (Body-side capture), analysis is adaptive
- No significant change: skip model call (idle cost near zero)
- Change detected: sample and analyze 1 frame
- Action requested: burst-analyze 3 frames and fuse results
- Confidence-aware escalation: low fused confidence or bbox jitter → +2 extra frames → if still low → request approval, never click blindly

---

## 3. Pipeline State Machine

### 3.1 States

```
IDLE              — stream not started
WATCHING          — stream active, no changes detected
CHANGE_DETECTED   — motion detector fired (debounced)
ANALYZING         — single frame sent to vision, updating scene graph
ACTION_TARGETING  — burst 3 frames, fuse results for target coords
PRECHECK          — 5-guard safety wall before execution
ACTING            — executing click/type/scroll
VERIFYING         — post-action frame captured, checking postconditions
RETRY_TARGETING   — verification failed, re-targeting (bounded: max K)
DEGRADED          — J-Prime vision unavailable, local-only + approval required
RECOVERING        — health restored, hysteresis before returning to normal
FAILED            — max retries exceeded or safety violation, awaiting human
```

### 3.2 Transition Table

| From | Event | Guard | To | Side effects |
|------|-------|-------|----|-------------|
| IDLE | start() | — | WATCHING | Start SCK stream, motion detector |
| WATCHING | motion_detected | debounce_elapsed (100ms cooldown) | CHANGE_DETECTED | — |
| WATCHING | no_change | hysteresis_stable | WATCHING | Drop frame |
| CHANGE_DETECTED | sample_frame | frame_available | ANALYZING | Dequeue frame |
| CHANGE_DETECTED | no_frame_timeout | — | WATCHING | Log warning |
| ANALYZING | analysis_complete | no_action_requested | WATCHING | Update scene graph (L1) |
| ANALYZING | action_requested | has_target_goal | ACTION_TARGETING | Begin burst capture |
| ANALYZING | vision_unavailable | — | DEGRADED | Log, set degraded flag |
| ACTION_TARGETING | burst_complete | fused_confidence >= threshold | PRECHECK | — |
| ACTION_TARGETING | burst_complete | fused_confidence < threshold | ACTION_TARGETING | Escalate: +2 frames (max 1 escalation) |
| ACTION_TARGETING | escalation_exhausted | confidence still low | PRECHECK | PRECHECK will catch low confidence |
| ACTION_TARGETING | vision_unavailable | — | DEGRADED | — |
| PRECHECK | all_guards_pass | — | ACTING | Emit decision provenance |
| PRECHECK | freshness_fail | frame too old | ACTION_TARGETING | Re-target with fresh frame |
| PRECHECK | confidence_fail | below threshold | FAILED | Request user clarification |
| PRECHECK | risk_requires_approval | high-risk action | ACTING | After VoiceApprovalManager approves |
| PRECHECK | approval_denied | user said no | FAILED | Notify user |
| PRECHECK | idempotency_hit | action_id already committed | WATCHING | Skip, log |
| PRECHECK | intent_expired | >2s since user request | FAILED | Ask "scene changed, should I still...?" |
| ACTING | action_dispatched | — | VERIFYING | Capture verification frame |
| VERIFYING | postcondition_met | UI changed as expected | WATCHING | Update scene graph, emit success |
| VERIFYING | postcondition_fail | UI unchanged or wrong | RETRY_TARGETING | attempt_count++ |
| RETRY_TARGETING | retry_count < max_K | — | ACTION_TARGETING | Re-burst |
| RETRY_TARGETING | retry_count >= max_K | — | FAILED | Emit failure, notify user |
| DEGRADED | health_check_pass | — | RECOVERING | Start hysteresis counter |
| RECOVERING | N_consecutive_healthy (3) | — | WATCHING | Resume normal operation |
| RECOVERING | health_check_fail | — | DEGRADED | Reset counter |
| FAILED | user_acknowledges | — | WATCHING | Reset state |
| FAILED | stop() | — | IDLE | Clean shutdown |
| Any | stop() | — | IDLE | Cancel all tasks, drain queues |

### 3.3 Invariants

- No state can skip to a non-adjacent state
- Every transition emits telemetry
- PRECHECK is the ONLY path from ACTION_TARGETING to ACTING
- DEGRADED can be entered from ANALYZING, ACTION_TARGETING, or VERIFYING
- FAILED is terminal until user acknowledges or stop() is called
- action_id is assigned at ACTION_TARGETING entry and carried through to VERIFYING

---

## 4. PRECHECK Gate (Safety Wall)

Every action passes through 5 deterministic guards. All must pass.

### 4.1 Guards

| # | Guard | Check | Fail action |
|---|-------|-------|------------|
| 1 | **Freshness** | `target_frame_ts` within `freshness_ms` (default 500ms, per-app configurable) of action dispatch time | Re-target (back to ACTION_TARGETING) |
| 2 | **Confidence** | `fused_confidence >= threshold` (default 0.75, per-task-type configurable) | If escalation unused → escalate. If exhausted → FAILED + request clarification |
| 3 | **Risk class** | `action_type` checked against approval policy (same as ValidationNode high-risk table) | Route to VoiceApprovalManager. In DEGRADED mode: ALL actions require approval |
| 4 | **Idempotency** | `action_id` not in committed action set | Skip (already executed), return to WATCHING |
| 5 | **Intent expiry** | `intent_timestamp` within 2s of now. If scene changed significantly since intent, auto-expire | FAILED + ask "scene changed, should I still [action]?" |

### 4.2 Stale-Context Invariant

Hard rule: target coordinates must be derived from a frame whose timestamp is within `freshness_ms` of the moment the click/type/scroll is dispatched to pyautogui. If the frame is older, PRECHECK forces re-targeting. This is enforced in code, not advisory.

### 4.3 PRECHECK Result

```python
@dataclass
class PrecheckResult:
    passed: bool
    failed_guards: List[str]
    action_id: str
    frame_age_ms: float
    fused_confidence: float
    risk_class: str           # "safe" | "elevated" | "high_risk"
    approval_required: bool
    approval_source: Optional[str]  # "auto" | "voice" | "degraded_approval"
    decision_provenance: Dict  # full audit trail
```

---

## 5. Frame Pipeline Architecture

### 5.1 Capture Layer

- **SCK stream**: single owner `asyncio.Task`, 30 FPS default, Metal GPU zero-copy
- **Bounded queue**: `asyncio.Queue(maxsize=10)`, drop_oldest on overflow (explicit policy)
- **No orphan tasks**: all analysis tasks tracked via `asyncio.TaskGroup` or equivalent, cancellation propagates from parent

### 5.2 Motion Detection

- Perceptual hash diff between consecutive frames
- **Hysteresis**: scene-change threshold configurable via `VISION_MOTION_THRESHOLD` (default 0.05)
- **Debounce**: 100ms cooldown after change detected (configurable `VISION_MOTION_DEBOUNCE_MS`)
- **Animated UI protection**: if change rate exceeds `VISION_MAX_CHANGE_RATE_HZ` (default 5), treat as "animated" and sample at reduced rate (1 FPS) instead of flapping

### 5.3 Analysis Routing

```
Frame arrives
    │
    ▼
Scene graph (L1): check if target element already known
    │
    ├─ HIT + fresh → use immediately (<5ms)
    │
    ├─ MISS or stale →
    │   ▼
    │   J-Prime LLaVA/Qwen-VL (L2): send frame for analysis (~200ms)
    │   │
    │   ├─ Available → analyze, update scene graph, return
    │   │
    │   └─ Unavailable →
    │       ▼
    │       Claude Vision (L3): paid fallback (~2s)
    │       │
    │       └─ Unavailable → DEGRADED mode
    │
    └─ Brain selector routes by task:
        scene_understanding → LLaVA 7B (fast)
        ui_element_detection → Qwen2-VL 7B (accurate)
        complex_ui_analysis → Claude Vision (fallback)
```

### 5.4 Structured Concurrency

```python
# Ownership model
class VisionActionLoop:
    _capture_task: asyncio.Task      # single owner, never orphaned
    _analysis_semaphore: Semaphore   # max 2 concurrent analyses
    _active_tasks: set               # tracked for cancellation

    async def shutdown(self):
        self._capture_task.cancel()
        for task in self._active_tasks:
            task.cancel()
        await asyncio.gather(*self._active_tasks, return_exceptions=True)
        # drain queue
        while not self._frame_queue.empty():
            self._frame_queue.get_nowait()
```

---

## 6. Unified Knowledge Fabric

### 6.1 Architecture

One logical graph API, three physical partitions:

```
┌─────────────────────────────────────────────────────┐
│        Unified Knowledge Fabric API                  │
│                                                     │
│  Global IDs: kg://scene/..., kg://semantic/...,     │
│              kg://trinity/...                        │
│                                                     │
│  query(entity_id) → routes to correct partition      │
│  query_nearest(embedding, scope) → fan-out + merge   │
│  write(node) → routes by ownership + freshness       │
└───────┬──────────────┬──────────────┬───────────────┘
        │              │              │
        ▼              ▼              ▼
  ┌───────────┐  ┌──────────────┐  ┌──────────────┐
  │  SCENE    │  │  SEMANTIC    │  │  TRINITY     │
  │           │  │              │  │              │
  │ JARVIS    │  │ J-Prime      │  │ All repos    │
  │ Body-local│  │ Mind-side    │  │ Durable sync │
  │           │  │              │  │              │
  │ TTL: 5s   │  │ TTL: 24h    │  │ TTL: durable │
  │ Hot path  │  │ Warm path   │  │ Cold path    │
  │           │  │              │  │              │
  │ NetworkX  │  │ ChromaDB +  │  │ SQLite +     │
  │ in-memory │  │ NetworkX    │  │ GCP sync     │
  └───────────┘  └──────────────┘  └──────────────┘
```

### 6.2 Entity Ownership Matrix

| Entity type | Owner partition | Freshness class | Example |
|------------|----------------|-----------------|---------|
| UI element position | `scene` | hot (5s TTL) | Submit button at (523, 187) |
| Window layout | `scene` | hot (5s TTL) | Safari frontmost, 1440x900 |
| Active app state | `scene` | hot (5s TTL) | URL bar shows linkedin.com |
| App-specific UI pattern | `semantic` | warm (24h TTL) | "Gmail compose always top-right" |
| Action success pattern | `semantic` | warm (24h TTL) | "LinkedIn message needs 2 clicks" |
| Vision model calibration | `semantic` | warm (24h TTL) | "LLaVA dropdown threshold: 0.90" |
| User preference | `trinity` | durable | "Derek prefers dark mode" |
| Action audit log | `trinity` | durable | Full provenance per action |
| Calibrated thresholds | `trinity` | durable | Per-app freshness, per-task confidence |

### 6.3 Query Precedence (Deterministic)

```
L1: scene partition (fresh, <5ms)
  → HIT + timestamp within TTL: use immediately
  → MISS or stale: fall through

L2: semantic partition (learned, ~50ms)
  → HIT: use learned pattern
  → MISS: fall through

L3: J-Prime vision (remote inference, ~200ms)
  → Send frame, get analysis
  → Update L1 scene + optionally L2 semantic

L4: trinity partition (historical, ~100ms)
  → Only for audit/context, not for action targeting
```

When L1 disagrees with L2: **L1 wins if fresh** (real-time observation beats learned pattern). L2 wins only when L1 has no data for the entity.

### 6.4 Cross-Partition Links

Instead of copying nodes between partitions, use references:

```
kg://scene/button/submit-001
  ├─ position: (523, 187)
  ├─ confidence: 0.92
  ├─ observed_at: 2026-03-19T18:30:00.123Z
  └─ linked_to: kg://semantic/pattern/gmail-submit-location
                    └─ historical_position: (520, 190) ± 5px
                    └─ success_rate: 0.97
                    └─ linked_to: kg://trinity/audit/submit-clicks-gmail
```

### 6.5 Advanced Requirements

- **Source of truth**: exactly one owner per entity type (see ownership matrix)
- **Conflict resolution**: L1 fresh > L2 recent > L3 remote > L4 historical
- **Idempotent merge**: replay/sync retries produce same result (dedup by entity_id + version)
- **Schema versioning**: fabric API version checked at boot (same pattern as protocol negotiation)
- **Backpressure isolation**: scene updates never blocked by semantic sync or trinity cloud writes
- **Security labels**: `local_only` (scene graph positions) vs `sync_allowed` (learned patterns)
- **Observability**: partition hit ratios, stale reads, merge conflicts, sync lag (all emitted as metrics)

---

## 7. Verification Postconditions

| Action type | Success postcondition | Check method |
|------------|----------------------|-------------|
| click | Target element state changed (button depressed, menu opened, page navigated) | Frame diff at click coords + scene graph update |
| type | Text appears in expected field | OCR on target region or scene graph text node |
| scroll | Content shifted in scroll direction | Frame diff shows content movement |
| navigate | URL or page title changed | Scene graph APPLICATION node updated |

Partial completion detection: if type action produces partial text (3 of 5 chars), verification reports `partial_success` with details.

**Bounded retries**: max 2 retries per action (3 total attempts). After 3: FAILED, emit telemetry, notify user via voice.

---

## 8. Reactor Core Integration (Soul)

### 8.1 New Experience Types

```python
# Added to EXPERIENCE_EVENT_TYPES in trinity_experience_receiver.py
"vision_action_outcome",        # per-action success/failure + full provenance
"scene_graph_accuracy",         # L1 cache hit rate, position accuracy
"vision_model_performance",     # per-model accuracy, latency, confidence calibration
"knowledge_fabric_health",      # partition hit ratios, sync lag, merge conflicts
```

### 8.2 What Reactor Core Learns

| Signal | Training output | Feeds back to |
|--------|----------------|---------------|
| vision_action_outcome | Confidence threshold calibration per task_type | PRECHECK gate thresholds |
| vision_action_outcome | Vision model routing weights | Brain selector vision_models section |
| scene_graph_accuracy | Per-app TTL tuning | Scene partition TTL config |
| vision_model_performance | Model accuracy per UI type | Vision router model preference |
| knowledge_fabric_health | Partition sizing, sync frequency | Fabric config |

### 8.3 Feedback Loop Timing

**Real-time (per action)**: Body emits `vision_action_outcome` → Reactor receives immediately.

**Batch (every 1000 actions or 24h)**: Reactor trains `vision_calibrator` → produces:
- `adjusted_thresholds.json` (confidence thresholds per task_type)
- `model_routing_weights.json` (which vision model for which UI type)
- `app_freshness_policy.json` (per-app TTL settings)

**Next boot or hot-reload**: Body + Mind read calibrated values from trinity partition → PRECHECK gate and vision router use learned thresholds.

### 8.4 Trinity Partition Data Flow

| Direction | Data | Example |
|-----------|------|---------|
| Body → Trinity → Reactor | Action audit trail | "Clicked (523,187) at 18:30, verified success" |
| Reactor → Trinity → Mind | Calibrated model weights | "Qwen-VL 3x better for forms → prefer" |
| Reactor → Trinity → Body | Per-app freshness policy | "Gmail TTL: 10s, LinkedIn TTL: 2s" |
| Mind → Trinity → Reactor | Reasoning trace | "PlanningNode decomposed into 4 vision steps" |

---

## 9. Decision Provenance

Every action emits a full audit record:

```python
@dataclass
class VisionActionRecord:
    # Identity
    action_id: str
    plan_id: str
    step_id: str

    # Frames
    targeting_frame_ids: List[str]
    targeting_frame_timestamps: List[float]
    verification_frame_id: Optional[str]

    # Decision
    target_element: str
    target_coords: Tuple[int, int]
    fused_confidence: float
    confidence_sources: Dict[str, float]  # {scene_L1: 0.9, jprime_L2: 0.85}

    # Guards
    precheck_passed: bool
    failed_guards: List[str]
    frame_age_ms: float
    risk_class: str
    approval_source: Optional[str]

    # Execution
    action_type: str
    backend_used: str  # "scene_graph_L1" | "jprime_llava" | "jprime_qwen_vl" | "claude_vision"
    action_latency_ms: float
    verification_result: str  # "success" | "fail" | "partial" | "inconclusive"
    retry_count: int

    # Knowledge
    kg_partition_hits: Dict[str, bool]
    scene_graph_updated: bool
    semantic_pattern_created: bool
```

---

## 10. Cost and Performance Guardrails

### 10.1 Idle Cost

Target: near-zero analysis calls when screen is static. Motion detector + hysteresis ensure no frames sent to vision models when nothing changes.

### 10.2 Burst Limits

- Max frames per burst: 3 (+ 2 escalation = 5 absolute max)
- Max vision API calls per minute: 30 (configurable `VISION_MAX_CALLS_PER_MIN`)
- Max concurrent analysis tasks: 2 (semaphore-bounded)

### 10.3 Latency Targets

| Path | Target p95 | Measured by |
|------|-----------|-------------|
| L1 scene graph hit | <5ms | query to result |
| L2 J-Prime LLaVA | <300ms | frame send to coords returned |
| L3 Claude Vision fallback | <3s | frame send to coords returned |
| Full action cycle (target + precheck + act + verify) | <2s for L1/L2 path | intent to verification complete |

### 10.4 Metrics Emitted

```
vision_analysis_calls_per_sec     — should be near 0 when idle
vision_action_latency_p95_ms      — full cycle time
vision_target_confidence_mean     — calibration signal
vision_misclick_rate              — actions where verification failed
vision_stale_frame_reject_rate    — PRECHECK freshness guard fires
vision_motion_detect_rate_hz      — how often screen changes
vision_l1_cache_hit_rate          — scene graph usefulness
vision_l2_call_rate               — J-Prime GPU utilization
vision_degraded_mode_seconds      — time spent in degraded
```

---

## 11. Fallback Tiers

| Level | Condition | Behavior |
|-------|-----------|----------|
| **Level 0** | J-Prime vision healthy | Full pipeline: L1 scene → L2 J-Prime → act |
| **Level 1** | J-Prime unavailable, Claude available | L1 scene → L3 Claude Vision → act (paid) |
| **Level 2** | Both unavailable | L1 scene graph only. ALL actions require voice approval. No remote vision analysis |

Transition uses same hysteresis as MindClient (3 consecutive failures → degrade, 3 consecutive successes → recover).

---

## 12. Edge Cases

| Case | Handling |
|------|---------|
| Animated UI (perpetual change) | `VISION_MAX_CHANGE_RATE_HZ` throttle → sample at 1 FPS instead of flapping |
| Button moves between target and click (layout reflow) | Freshness guard catches stale coords → re-target |
| Popup occludes target after targeting | Verification fails (target not clicked) → RETRY_TARGETING with new frame |
| Multi-monitor/space switch | Scene graph invalidated on space change → full re-analysis |
| Remote vision returns different coord space | Normalize all coords to primary display resolution at capture time |
| Clipboard/type partial completion | Verification detects partial text → report `partial_success` |
| Late remote inference overwrites newer target | Sequence numbers on vision responses → reject if older than current |
| Network jitter during L2 call | Timeout (default 5s) → fall through to L3 or DEGRADED |

---

## 13. File Structure

### JARVIS (Body) — new files

```
backend/vision/realtime/
  __init__.py
  vision_action_loop.py        # State machine + main loop orchestrator
  frame_pipeline.py            # SCK stream → bounded queue → motion detect
  precheck_gate.py             # 5-guard safety wall
  action_executor.py           # click/type/scroll via pyautogui/AppleScript
  verification.py              # Post-action postcondition checking
  vision_router.py             # L1 scene → L2 J-Prime → L3 Claude routing
  metrics.py                   # VisionActionRecord + telemetry emission

backend/knowledge/
  __init__.py
  fabric.py                    # Unified Knowledge Fabric API (one interface)
  scene_partition.py           # L1 hot cache (wraps SemanticSceneGraph)
  fabric_router.py             # Read/write routing by entity type + freshness
  schema.py                    # Global ID format, entity types, freshness classes
```

### J-Prime (Mind) — new files

```
jarvis_prime/reasoning/
  vision_assist.py             # POST /v1/vision/analyze endpoint
                               # Accepts frame, returns element coords + confidence

jarvis_prime/knowledge/
  __init__.py
  semantic_partition.py        # L2 learned patterns (wraps SharedKnowledgeGraph)
```

### Reactor Core (Soul) — new + modified files

```
reactor_core/integration/
  trinity_experience_receiver.py   # Add 4 new vision experience types

reactor_core/training/
  vision_calibrator.py             # NEW: trains confidence thresholds,
                                   # model routing weights, per-app freshness
                                   # from vision_action_outcome data
```

### Modified files across repos

```
JARVIS:
  backend/api/unified_command_processor.py     # Wire vision loop for plan steps
  backend/core/mind_client.py                  # Add send_vision_frame() method
  brain_selection_policy.yaml                  # Add vision_models section

J-Prime:
  jarvis_prime/reasoning/endpoints.py          # Add vision_analyze handler
  jarvis_prime/server.py                       # Register /v1/vision/analyze
  jarvis_prime/reasoning/unified_brain_selector.py  # Add vision model routing

Reactor Core:
  reactor_core/integration/trinity_experience_receiver.py  # 4 new types
```

---

## 14. Acceptance Criteria

### State Machine
- [ ] Static screen → no unnecessary vision calls (idle cost near zero)
- [ ] Rapid UI changes → no queue blowup (bounded queue + drop_oldest)
- [ ] Action on moving target → freshness guard catches stale coords
- [ ] Low-confidence target → escalation (+2 frames) then approval if still low
- [ ] Stale frame protection → PRECHECK rejects, forces re-target
- [ ] Cancel/restart consistency → all tasks cancelled cleanly, no orphans
- [ ] Animated UI → throttled to 1 FPS, no CHANGE_DETECTED flapping

### PRECHECK Gate
- [ ] All 5 guards enforced on every action path
- [ ] No path from ACTION_TARGETING to ACTING that bypasses PRECHECK
- [ ] Freshness violation → re-target (not fail)
- [ ] Low confidence → approval request (not blind click)
- [ ] High-risk action → voice approval required
- [ ] Already-committed action_id → skip silently
- [ ] Intent expired (>2s) → ask user, don't execute

### Knowledge Fabric
- [ ] Single API, three partitions respond correctly
- [ ] L1 hit returns in <5ms
- [ ] L2 hit returns in <50ms
- [ ] L1 fresh beats L2 learned (conflict resolution)
- [ ] Cross-partition links resolve correctly
- [ ] Scene graph TTL respected (entries expire)
- [ ] Backpressure: scene writes never blocked by semantic/trinity sync

### Verification
- [ ] Click verified by UI state change at coords
- [ ] Type verified by text appearance in field
- [ ] Scroll verified by content shift
- [ ] Partial completion detected and reported
- [ ] Max 2 retries, then FAILED

### Reactor Core
- [ ] 4 new experience types received and routed to training
- [ ] vision_calibrator produces threshold adjustments
- [ ] Adjusted thresholds written to trinity partition
- [ ] Body reads calibrated thresholds on next boot

### Performance
- [ ] L1+L2 action cycle <2s p95
- [ ] Idle analysis rate near zero
- [ ] Burst bounded at 5 frames max per action

---

## 15. Vision Analyze Endpoint Schema

`POST /v1/vision/analyze` on J-Prime accepts a frame and returns detected UI elements with coordinates and confidence.

### 15.1 Request

```json
{
  "protocol_version": "1.0.0",
  "request_id": "uuid",
  "session_id": "uuid",
  "trace_id": "uuid",
  "frame": {
    "artifact_ref": "artifacts/frames/frame-001.jpg",
    "width": 1440,
    "height": 900,
    "scale_factor": 2.0,
    "captured_at_ms": 1710876543210,
    "display_id": 0
  },
  "task": {
    "type": "find_element",
    "target_description": "submit button",
    "target_context": "bottom-right of the form",
    "action_intent": "click"
  },
  "constraints": {
    "timeout_ms": 5000,
    "max_elements": 10
  }
}
```

Note: Frame binary is sent out-of-band via `artifact_ref` (shared filesystem or HTTP upload to `/v1/artifacts`). The protocol payload carries only the reference, not base64 blobs. For local-network J-Prime, `artifact_ref` points to a shared path. For remote, JARVIS uploads first then sends the ref.

### 15.2 Response

```json
{
  "request_id": "uuid",
  "status": "found | not_found | ambiguous | error",
  "elements": [
    {
      "element_id": "elem-001",
      "description": "Submit button",
      "coords": [523, 187],
      "bbox": [490, 170, 556, 204],
      "confidence": 0.92,
      "element_type": "button",
      "text_content": "Submit",
      "interactable": true
    }
  ],
  "scene_summary": "Gmail compose window with form fields and submit button",
  "model_used": "llava-v1.5-7b",
  "inference_latency_ms": 187,
  "coord_space": "logical_pixels"
}
```

### 15.3 Coordinate Space

All coordinates are in **logical pixels** (macOS points), not physical pixels. On Retina displays, `scale_factor=2.0` means physical pixels = logical * 2. The frame is captured at physical resolution but coordinates are normalized to logical space at capture time. pyautogui operates in logical pixels, so no conversion is needed at action time. The `scale_factor` is included for J-Prime to correctly interpret the frame resolution.

---

## 16. Confidence Fusion Algorithm

When burst-analyzing 3+ frames for a target element, results are fused into a single target coordinate and confidence score.

### 16.1 Algorithm

```python
def fuse_burst_results(results: List[VisionResult]) -> FusedTarget:
    """Fuse multiple frame analyses into one targeting decision.

    Strategy: median coordinates with outlier rejection, weighted mean confidence.
    """
    if not results:
        return FusedTarget(confidence=0.0, coords=None)

    # Filter to results that found the target
    hits = [r for r in results if r.status == "found" and r.confidence > 0.3]

    if not hits:
        return FusedTarget(confidence=0.0, coords=None)

    # Extract coordinate arrays
    xs = [r.coords[0] for r in hits]
    ys = [r.coords[1] for r in hits]
    confs = [r.confidence for r in hits]

    # Outlier rejection: if any coord is >50px from median, exclude it
    med_x, med_y = median(xs), median(ys)
    filtered = [
        (x, y, c) for x, y, c in zip(xs, ys, confs)
        if abs(x - med_x) <= 50 and abs(y - med_y) <= 50
    ]

    if not filtered:
        # All outliers — use highest confidence result
        best = max(hits, key=lambda r: r.confidence)
        return FusedTarget(
            coords=best.coords,
            confidence=best.confidence * 0.7,  # penalize for disagreement
            bbox_jitter=max(xs) - min(xs),
        )

    # Median coordinates (robust to outliers)
    fused_x = int(median([f[0] for f in filtered]))
    fused_y = int(median([f[1] for f in filtered]))

    # Weighted mean confidence (higher confidence frames count more)
    total_weight = sum(c for _, _, c in filtered)
    fused_conf = sum(c * c for _, _, c in filtered) / total_weight  # confidence-weighted

    # Bbox jitter: spread in coordinates (high jitter = low reliability)
    jitter = max(max(xs) - min(xs), max(ys) - min(ys))

    # Penalize confidence for high jitter
    if jitter > 20:
        fused_conf *= 0.8  # 20% penalty for >20px jitter
    if jitter > 50:
        fused_conf *= 0.6  # additional 40% penalty for >50px jitter

    return FusedTarget(
        coords=(fused_x, fused_y),
        confidence=min(fused_conf, 0.99),
        bbox_jitter=jitter,
        frames_used=len(filtered),
        frames_rejected=len(hits) - len(filtered),
    )
```

### 16.2 Jitter Threshold

If `bbox_jitter > VISION_JITTER_THRESHOLD_PX` (default 30), the fused confidence is penalized AND PRECHECK's confidence guard is more likely to fire, triggering escalation or approval.

---

## 17. Vision Operational Level Independence

The vision action loop maintains its **own operational level** independently from `MindClient`'s reasoning operational level. They are separate circuits:

| Component | Degradation trigger | Endpoint affected |
|-----------|-------------------|------------------|
| MindClient reasoning level | `/v1/reason` failures | Reasoning pipeline |
| Vision operational level | `/v1/vision/analyze` failures | Vision action loop |

Both use the same hysteresis pattern (3 failures → degrade, 3 successes → recover) but track state independently. Possible state combinations:

| Reasoning | Vision | Behavior |
|-----------|--------|----------|
| Level 0 | Level 0 | Full pipeline: Mind plans + Vision executes |
| Level 0 | Level 1 | Mind plans, Vision uses Claude fallback for targeting |
| Level 0 | Level 2 | Mind plans, but Vision actions need approval (scene graph only) |
| Level 1 | Level 0 | Degraded reasoning, but Vision executes normally |
| Level 2 | Level 2 | Reflex only, no vision actions |

---

## 18. Brain Selection Policy — Vision Models

Add to `brain_selection_policy.yaml`:

```yaml
vision_models:
  required:
    - brain_id: "llava_7b"
      provider: "gcp_prime"
      model_name: "llava-v1.5-7b"
      required_capabilities: ["scene_understanding", "element_detection"]
      routable: true
      allowed_task_classes: ["scene_understanding", "ui_element_detection"]
      cost_class: "free"
      latency_class: "low"
      compute_class: "gpu_l4"
      port: 8001

  optional:
    - brain_id: "qwen2_vl_7b"
      provider: "gcp_prime"
      model_name: "qwen2-vl-7b"
      required_capabilities: ["scene_understanding", "ui_element_detection", "complex_ui_analysis"]
      routable: true
      allowed_task_classes: ["ui_element_detection", "complex_ui_analysis"]
      cost_class: "free"
      latency_class: "medium"
      compute_class: "gpu_l4"
      port: 8002

  vision_task_routing:
    scene_understanding:
      primary: "llava_7b"
      fallback: "qwen2_vl_7b"
      latency_target_ms: 200
    ui_element_detection:
      primary: "qwen2_vl_7b"
      fallback: "llava_7b"
      latency_target_ms: 300
    complex_ui_analysis:
      primary: "qwen2_vl_7b"
      fallback: "claude_vision"
      latency_target_ms: 500
```

---

## 19. PRECHECK Internal Error Handling

If PRECHECK itself throws an exception (internal error in any of the 5 guards):

```python
# Same fail-closed pattern as ValidationNode
try:
    result = self._run_all_guards(state)
except Exception as exc:
    logger.error("[PRECHECK] Internal error (fail-closed): %s", exc)
    result = PrecheckResult(
        passed=False,
        failed_guards=["PRECHECK_INTERNAL_ERROR"],
        action_id=state.action_id,
        frame_age_ms=-1,
        fused_confidence=0.0,
        risk_class="high_risk",
        approval_required=True,
        decision_provenance={"error": str(exc), "fail_closed": True},
    )
```

PRECHECK internal errors transition to FAILED. The action is never executed when PRECHECK cannot evaluate. This matches the Step 2 spec's `VALIDATION_UNAVAILABLE` pattern.

---

## 20. Non-Goals (Future Work)

- Multi-monitor simultaneous streaming (single display for now)
- Video recording/playback of action sessions
- Training custom UI detection models from Reactor Core data
- Gesture recognition (pinch, swipe) — keyboard/mouse only
- Audio-visual fusion (combining what JARVIS hears + sees)
- Real-time OCR streaming (OCR is per-frame on demand, not continuous)

- Multi-monitor simultaneous streaming (single display for now)
- Video recording/playback of action sessions
- Training custom UI detection models from Reactor Core data
- Gesture recognition (pinch, swipe) — keyboard/mouse only
- Audio-visual fusion (combining what JARVIS hears + sees)
- Real-time OCR streaming (OCR is per-frame on demand, not continuous)
