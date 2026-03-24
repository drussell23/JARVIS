# Agentic Vision Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace single-shot `_dispatch_to_vision()` with a multi-turn see-think-act-verify loop driven by J-Prime, enabling complex goals like "open WhatsApp, find Zach, send a message."

**Architecture:** The RTO's `_dispatch_to_vision()` becomes a bounded loop. Each turn: read frame from Ferrari Engine -> ask J-Prime "what next?" via `vision.loop.v1` contract -> execute action via VisionActionLoop -> verify via 3-tier escalation. MindClient gets a new `reason_vision_turn()` method with L3 Claude fallback adapter.

**Tech Stack:** Python 3.9, asyncio, PIL/numpy, aiohttp, existing JARVIS subsystems (MindClient, VisionActionLoop, ActionVerifier, FramePipeline)

**Spec:** `docs/superpowers/specs/2026-03-23-agentic-vision-loop-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `backend/core/runtime_task_orchestrator.py` | MODIFY | Replace `_dispatch_to_vision` + `_attempt_ghost_hands_correction` with agentic loop |
| `backend/core/mind_client.py` | MODIFY | Add `reason_vision_turn()`, `_compress_frame_jpeg()`, `_claude_vision_fallback()` |
| `tests/core/test_agentic_vision_loop.py` | CREATE | Unit tests for the RTO agentic loop methods |
| `tests/core/test_mind_client_vision_turn.py` | CREATE | Unit tests for MindClient.reason_vision_turn |
| `tests/core/test_agentic_vision_loop_integration.py` | CREATE | Integration smoke test |

---

### Task 1: Enums + Stagnation Detection (RTO)

**Files:**
- Modify: `backend/core/runtime_task_orchestrator.py` (top of file for enums)
- Create: `tests/core/test_agentic_vision_loop.py`

- [ ] **Step 1: Write failing tests for enums and stagnation**

Create `tests/core/test_agentic_vision_loop.py` with:
- 3 tests verifying enum values (ActionOutcome, VerifyTier, StopReason)
- 4 tests for stagnation detection:
  - `test_stagnation_detects_repeated_successful_action`: 3 identical successful clicks -> stagnant
  - `test_stagnation_ignores_failed_repeats`: 3 identical failed clicks -> NOT stagnant (valid retry)
  - `test_stagnation_detects_frozen_frames`: 3 different actions but same frame_hash -> stagnant
  - `test_stagnation_no_false_positive_on_short_log`: 1 entry -> NOT stagnant

Stagnation tests use `RuntimeTaskOrchestrator._is_stagnant_static(log, proposed, stagnation_window=3, frame_stagnation=3)`.

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/core/test_agentic_vision_loop.py -x -v`
Expected: FAIL -- `ImportError: cannot import name 'ActionOutcome'`

- [ ] **Step 3: Add enums and stagnation to RTO**

Add `ActionOutcome`, `VerifyTier`, `StopReason` enums after existing imports. Add `_is_stagnant_static` as a `@staticmethod` on `RuntimeTaskOrchestrator`. Stagnation only counts entries where `result == "success"` (failed repeats are valid retry, not stagnation).

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/core/test_agentic_vision_loop.py -x -v`
Expected: all 7 PASS

- [ ] **Step 5: Commit**

```
git add backend/core/runtime_task_orchestrator.py tests/core/test_agentic_vision_loop.py
git commit -m "feat(rto): add ActionOutcome/VerifyTier/StopReason enums + stagnation detection"
```

---

### Task 2: MindClient `reason_vision_turn()` + Frame Compression

**Files:**
- Modify: `backend/core/mind_client.py`
- Create: `tests/core/test_mind_client_vision_turn.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/test_mind_client_vision_turn.py` with:
- `test_compress_frame_jpeg`: 2880x1800 image compresses under 500KB
- `test_compress_frame_jpeg_downscales_if_too_large`: tiny max_bytes forces downscale, verify width < original
- `test_reason_vision_turn_builds_v1_payload`: mock `_http_post`, verify payload has `schema: "vision.loop.v1"`, `goal`, returns parsed response
- `test_reason_vision_turn_validates_response`: malformed response (no goal_achieved) returns error-shaped dict with `stop_reason: "error"`, does not crash

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/core/test_mind_client_vision_turn.py -x -v`
Expected: FAIL -- `AttributeError: '_compress_frame_jpeg'`

- [ ] **Step 3: Implement `_compress_frame_jpeg`**

Add to MindClient: JPEG compression with quality param, max_bytes enforcement via downscale loop (max 3 attempts, halve longest edge each time), returns dict with `data` (base64), `content_type`, `sha256`, `width`, `height`.

- [ ] **Step 4: Implement `reason_vision_turn`**

Add to MindClient:
- Builds vision.loop.v1 payload
- POST to `/v1/vision/reason_turn` via `asyncio.wait_for(asyncio.shield(...))` (shield protects aiohttp session on timeout)
- One idempotent retry on 502/504 with 2s backoff
- L3 fallback via `_claude_vision_fallback()` (stub for now -- returns error response, logs "not yet implemented")
- `_validate_vision_loop_response()`: checks `goal_achieved` present, enforces invariant (must have `goal_achieved: true` OR valid `next_action`, exception for `model_refusal`)
- On total failure: returns `{"goal_achieved": false, "stop_reason": "error"}`

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/core/test_mind_client_vision_turn.py -x -v`
Expected: all 4 PASS

- [ ] **Step 6: Commit**

```
git add backend/core/mind_client.py tests/core/test_mind_client_vision_turn.py
git commit -m "feat(mind_client): add reason_vision_turn() with frame compression and L3 stub"
```

---

### Task 3: Agentic Loop -- Replace `_dispatch_to_vision()`

**Files:**
- Modify: `backend/core/runtime_task_orchestrator.py` (lines 878-1073)
- Add tests: `tests/core/test_agentic_vision_loop.py`

- [ ] **Step 1: Write failing tests for the agentic loop**

Append to `tests/core/test_agentic_vision_loop.py`:
- `test_agentic_loop_achieves_goal_in_one_turn`: mock J-Prime says `goal_achieved: true` on first turn, verify `success: True`
- `test_agentic_loop_multi_turn`: J-Prime proposes click on turn 1, says achieved on turn 2, verify `execute_action` called once
- `test_agentic_loop_stagnation_exit`: J-Prime keeps proposing same click, verify early exit with `stop_reason: "stagnation"`
- `test_agentic_loop_degraded_no_val`: VisionActionLoop is None, verify graceful fallback
- `test_agentic_loop_model_refusal`: J-Prime returns `goal_achieved: false, next_action: null, stop_reason: "model_refusal"`, verify clean exit with `stop_reason: "model_refusal"` and `success: False`
- `test_agentic_loop_settle_ms_capped`: J-Prime returns `settle_ms: 999999`, verify loop caps it to `VISION_LOOP_MAX_SETTLE_MS` (mock asyncio.sleep and check the value passed)

Tests use `_make_mock_rto()` helper that creates an RTO with mocked VisionActionLoop (with `frame_pipeline.latest_frame` returning a numpy frame), mocked MindClient (with `_compress_frame_jpeg` returning test data), and `_prime = MagicMock()` for URL resolution.

Tests use `_make_mock_rto()` helper that creates an RTO with mocked VisionActionLoop (with frame_pipeline.latest_frame returning a numpy frame), mocked MindClient, and mocked dependencies.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/core/test_agentic_vision_loop.py -x -v -k "agentic_loop"`
Expected: FAIL -- current `_dispatch_to_vision` doesn't return `stop_reason`

- [ ] **Step 3: Replace `_dispatch_to_vision()` with agentic loop**

Grep for `async def _dispatch_to_vision` and `async def _attempt_ghost_hands_correction` in `backend/core/runtime_task_orchestrator.py` (do NOT rely on line numbers -- they shift after Task 1 edits). Replace BOTH methods with the agentic loop. Keep `_get_vision_action_loop()` unchanged.

The new `_dispatch_to_vision`:
- Phase 0: Open app/URL (same as before -- resolve via J-Prime, open via subprocess)
- Phase 1: Agentic loop `for turn in range(MAX_VISION_TURNS)`:
  - SEE: `pipeline.latest_frame` (non-destructive read)
  - Compress frame via `mind._compress_frame_jpeg()`
  - THINK: `await mind.reason_vision_turn(...)` with vision.loop.v1 schema
  - GATE: if `goal_achieved` -> return success
  - Handle `model_refusal` and `error` stop reasons
  - STAGNATION CHECK: `_is_stagnant_static()` before acting
  - ACT: `await val.execute_action()` with timeout
  - VERIFY: Tier 1 (executor truth from VisionActionLoop's built-in verification)
  - Settle: `await asyncio.sleep(settle_ms / 1000)` capped at MAX_SETTLE_MS
  - RECORD: structured action_log entry
  - EMIT: logger.info per turn
- Catch `asyncio.CancelledError` and re-raise (BaseException, not Exception)
- Max turns exhausted: return partial result with `StopReason.MAX_TURNS`

Add `_get_mind_client()` helper (import-safe singleton lookup, same pattern as `_get_vision_action_loop`).

Add `_find_strategy()` stub returning `None` (future: Ouroboros graduation fills this via AgentRegistry semantic lookup, NOT regex matching -- Manifesto ss5):
```python
    async def _find_strategy(self, goal: str, app_context: str):
        """Check AgentRegistry for graduated navigation strategy. Returns None today."""
        return None
```

Add `_build_action_log_entry()` helper that constructs a dict matching the vision.loop.v1 action_log schema (turn, action_type, target, text, coords, result, verify_tier, observation, confidence, frame_hash). Used by the RECORD step to ensure consistent log entries.

Add `_verify_action()` method implementing 3-tier verify escalation:
- Tier 1 (always): Check `execute_action()` result's `success` and `verification_status`
- Tier 2 (on INCONCLUSIVE): Delegate to `ActionVerifier.verify_click/type/scroll()` with pre/post frames from FramePipeline
- Tier 3 (on failure or confidence < `VISION_LOOP_VERIFY_CONFIDENCE_TAU`): Call `mind.reason_vision_turn()` with `mode: "verify"` and before/after frames
- Returns `(ActionOutcome, VerifyTier, observation_str)`

DELETE `_attempt_ghost_hands_correction` entirely (subsumed by the loop).

**Note:** L3 Claude Vision fallback (`_claude_vision_fallback`) is intentionally stubbed in this plan iteration. The loop works with J-Prime (L2) immediately and returns an error response when both L2 and L3 are unavailable. The full Claude adapter (Anthropic SDK multimodal + tool_use constraint) is a separate task.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/core/test_agentic_vision_loop.py -x -v`
Expected: all 13 PASS (7 from Task 1 + 6 new)

- [ ] **Step 5: Run existing tests for regression**

Run: `python3 -m pytest tests/vision/realtime/ -x -q`
Expected: all PASS (VisionActionLoop, VisionCortex tests unchanged)

- [ ] **Step 6: Commit**

```
git add backend/core/runtime_task_orchestrator.py tests/core/test_agentic_vision_loop.py
git commit -m "feat(rto): replace single-shot _dispatch_to_vision with agentic vision loop"
```

---

### Task 4: Integration Smoke Test

**Files:**
- Create: `tests/core/test_agentic_vision_loop_integration.py`

- [ ] **Step 1: Write integration test**

Create `tests/core/test_agentic_vision_loop_integration.py` with:
- `test_full_loop_with_mocked_mind_and_real_val`: Creates a real VisionActionLoop (use_sck=False), injects a frame into its pipeline, mocks MindClient to return `goal_achieved: true`, calls `_dispatch_to_vision`, verifies success. Uses VisionActionLoop singleton (clean up in finally block).

- [ ] **Step 2: Run integration test**

Run: `python3 -m pytest tests/core/test_agentic_vision_loop_integration.py -x -v`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `python3 -m pytest tests/vision/realtime/ tests/core/test_agentic_vision_loop*.py tests/core/test_mind_client_vision_turn.py -x -q`
Expected: all PASS

- [ ] **Step 4: Commit**

```
git add tests/core/test_agentic_vision_loop_integration.py
git commit -m "test(rto): integration smoke test for agentic vision loop"
```
