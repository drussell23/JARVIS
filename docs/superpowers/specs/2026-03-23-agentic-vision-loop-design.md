# Agentic Vision Loop: Multi-Turn See-Think-Act-Verify Chain

**Date:** 2026-03-23
**Status:** Approved
**Manifesto alignment:** §1 Unified Organism, §3 Async Tendrils, §5 Intelligence-Driven Routing, §6 Neuroplasticity, §7 Absolute Observability
**Depends on:** VisionCortex (2026-03-23-vision-cortex-design.md)

## Problem

`_dispatch_to_vision()` in RuntimeTaskOrchestrator is single-shot: open URL/app, one `execute_action()` call, one Ghost Hands correction attempt, done. Complex goals like "open WhatsApp, find Zach, send a message" require a **multi-turn reasoning chain** where J-Prime sees the screen each turn, decides what to do next, and the loop executes until the goal is achieved.

The infrastructure exists (Ferrari Engine captures frames, VisionActionLoop executes actions, Ghost Hands preserves focus, VisionRouter cascades L1→L2→L3) but there is no loop that repeatedly asks "what do I see? what should I do next?" until the goal is met.

## Solution

Replace `_dispatch_to_vision()` with an **agentic vision loop** — a bounded see→think→act→verify cycle driven by J-Prime. The loop code is dumb plumbing; J-Prime is the intelligence. J-Prime proposes; the loop enforces safety (max turns, stagnation detection, action type allowlist, coordinate bounds).

## Architecture

```
_dispatch_to_vision(goal, step)
    │
    ├── Phase 0: Open app/URL (same as today)
    │
    └── Phase 1: Agentic Loop
        │
        for turn in range(MAX_VISION_TURNS):
            │
            ├── STRATEGY CHECK: graduated plugin for this app?
            │   If found → use pre-built action sequence (skip THINK)
            │   If not → fall through to J-Prime
            │
            ├── SEE: frame = frame_pipeline.latest_frame
            │   (non-destructive read; frame safety: FramePipeline allocates
            │    new FrameData per capture — no in-place numpy mutation)
            │   JPEG compress, enforce max_bytes, compute sha256
            │
            ├── THINK: await mind_client.reason_vision_turn(
            │       request_id, session_id, goal, action_log,
            │       frame_jpeg_b64, frame_dims, allowed_actions)
            │   J-Prime returns: goal_achieved, next_action, reasoning, confidence
            │   Falls back to Claude Vision (L3) if J-Prime unavailable
            │
            ├── GATE: if goal_achieved → return success + stop_reason
            │
            ├── STAGNATION CHECK: if _is_stagnant(action_log, next_action)
            │   → early exit with partial result + StopReason.STAGNATION
            │
            ├── ACT: await vision_action_loop.execute_action(next_action)
            │
            ├── VERIFY (3-tier, cheapest first):
            │   Tier 1 (always): Executor truth — result.success + verifier status
            │   Tier 2 (on INCONCLUSIVE): Frame delta — wait 150ms, compare frames
            │   Tier 3 (on failure or confidence < τ): Surgical C — send before/after
            │       frames to J-Prime: "did this action succeed?"
            │
            ├── RECORD: append structured entry to action_log
            │
            └── EMIT: TelemetryBus vision.loop.turn@1.0.0

        Max turns exhausted → return partial + StopReason.MAX_TURNS
```

## Vision Loop V1 Contract

Versioned schema (`vision.loop.v1`) for communication between the loop and J-Prime. Strategy plugins and tests target this contract.

### Request (`/v1/vision/reason_turn`)

```json
{
    "schema": "vision.loop.v1",
    "request_id": "req-a3f2b1c4",
    "session_id": "sess-uuid",
    "trace_id": "tr-12char",
    "goal": "message Zach on WhatsApp saying I'll be late",
    "turn_number": 3,
    "max_turns": 10,
    "allowed_action_types": ["click", "type", "scroll"],
    "strategy_hints": null,
    "action_log": [
        {
            "turn": 1,
            "action_type": "click",
            "target": "WhatsApp icon in dock",
            "text": null,
            "coords": null,
            "result": "success",
            "verify_tier": "executor",
            "observation": "WhatsApp window opened",
            "confidence": 0.94,
            "frame_hash": "a3f2..."
        },
        {
            "turn": 2,
            "action_type": "click",
            "target": "search bar at top",
            "text": null,
            "coords": [245, 52],
            "result": "success",
            "verify_tier": "frame_delta",
            "observation": "cursor blinking in search field",
            "confidence": 0.89,
            "frame_hash": "b7e1..."
        }
    ],
    "frame": {
        "data": "<base64 JPEG>",
        "content_type": "image/jpeg",
        "sha256": "c4a1...",
        "width": 2880,
        "height": 1800,
        "scale": 2.0,
        "captured_at_ms": 1711234567890
    }
}
```

### Response (continuation)

```json
{
    "schema": "vision.loop.v1",
    "goal_achieved": false,
    "next_action": {
        "action_id": "act-uuid",
        "action_type": "type",
        "target": "search field",
        "text": "Zach",
        "coords": null,
        "settle_ms": 300,
        "requires_verify": true
    },
    "reasoning": "Search field is focused with blinking cursor. Typing contact name.",
    "confidence": 0.91,
    "scene_summary": "WhatsApp desktop, search bar focused, recent chats below"
}
```

### Response (goal achieved)

```json
{
    "schema": "vision.loop.v1",
    "goal_achieved": true,
    "next_action": null,
    "stop_reason": "goal_satisfied",
    "reasoning": "Message sent. Blue check marks visible in Zach's chat.",
    "confidence": 0.95,
    "scene_summary": "WhatsApp chat with Zach, sent confirmation visible"
}
```

### Error response

HTTP 400 with:
```json
{
    "schema": "vision.loop.v1",
    "error_code": "INVALID_SCHEMA",
    "message": "Missing required field: goal"
}
```

Model refusal returns HTTP 200 with:
```json
{
    "schema": "vision.loop.v1",
    "goal_achieved": false,
    "next_action": null,
    "stop_reason": "model_refusal",
    "reasoning": "Cannot assist with this request."
}
```

### Field naming convention

`target` is the canonical field name everywhere — both `next_action` (response) and `action_log` entries (request). No aliases, no adapter normalization. When calling `VisionActionLoop.execute_action()`, the loop maps `target` to the `target_description` parameter.

## Enums

```python
class ActionOutcome(str, Enum):
    """Outcome of a single action within the agentic loop.
    Named ActionOutcome (not VisionActionResult) to avoid collision with
    the existing VisionActionResult dataclass in vision_action_loop.py.
    """
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"

class VerifyTier(str, Enum):
    NONE = "none"
    EXECUTOR = "executor"
    FRAME_DELTA = "frame_delta"
    MODEL_VERIFY = "model_verify"

class StopReason(str, Enum):
    GOAL_SATISFIED = "goal_satisfied"
    USER_CONFIRMATION = "user_visible_confirmation"
    BEST_EFFORT = "best_effort"
    STAGNATION = "stagnation"
    MAX_TURNS = "max_turns"
    MODEL_REFUSAL = "model_refusal"
    ERROR = "error"
```

## Verify Step (3-Tier)

After each ACT, the loop verifies the action succeeded:

| Tier | Trigger | Method | Cost |
|------|---------|--------|------|
| 1: Executor | Always | `result.success` + `result.verification_status` from VisionActionLoop | Free |
| 2: Frame delta | Tier 1 INCONCLUSIVE | Wait `settle_ms`, grab new frame, compute hash delta vs pre-action frame | Free (local) |
| 3: Surgical C | Failure OR confidence < `VISION_LOOP_VERIFY_CONFIDENCE_TAU` | Send before+after frames to J-Prime: "did this action succeed?" | 1 extra J-Prime call |

Tier 3 sends exactly **two** images (before/after), not the full action log with all frames. This is the "surgical C" escape hatch — targeted vision memory on ambiguity, not full conversation threading.

## Stagnation Detection

```python
def _is_stagnant(action_log: list, proposed_action) -> bool:
    # Check 1: identical SUCCESSFUL action proposed N times (VISION_LOOP_STAGNATION_WINDOW, default 3)
    # Only count entries where result == "success" — repeating a failed action is valid retry.
    window = _STAGNATION_WINDOW
    recent = action_log[-window:] if len(action_log) >= window else action_log
    matches = sum(1 for e in recent
                  if e["action_type"] == proposed_action.action_type
                  and e.get("target") == proposed_action.target
                  and e.get("text") == getattr(proposed_action, "text", None)
                  and e.get("result") == "success")
    if matches >= window:
        return True

    # Check 2: frame unchanged for N turns (VISION_LOOP_FRAME_STAGNATION, default 3)
    if len(action_log) >= _FRAME_STAGNATION:
        recent_hashes = [e.get("frame_hash") for e in action_log[-_FRAME_STAGNATION:]]
        if len(set(h for h in recent_hashes if h)) == 1:
            return True

    return False
```

## MindClient: reason_vision_turn()

New method on the existing MindClient class.

```python
async def reason_vision_turn(
    self,
    request_id: str,
    session_id: str,
    goal: str,
    action_log: list,
    frame_jpeg_b64: str,
    frame_dims: dict,
    allowed_action_types: list,
    strategy_hints: Optional[dict] = None,
) -> dict:
    """POST /v1/vision/reason_turn — ask J-Prime what to do next.

    Returns validated vision.loop.v1 response dict.
    Falls back to Claude Vision (L3) if J-Prime unavailable.
    """
```

**Frame preparation**:
- JPEG compression at `VISION_LOOP_JPEG_QUALITY` (default 85)
- If size exceeds `VISION_LOOP_MAX_FRAME_BYTES` (default 500000), downscale longest edge by 50% and recompress
- Compute sha256 of compressed bytes for dedup and provenance

**L3 fallback adapter**:
- Converts vision.loop.v1 request to Claude multimodal message (system prompt + user message with inline image)
- Uses tool_use / structured output to constrain Claude's response to the v1 schema
- Validates and normalizes response before returning to the loop
- The loop never knows which backend answered — Manifesto §5

**Timeout**: `VISION_LOOP_THINK_TIMEOUT_S` (default 12s). Uses `asyncio.wait_for(asyncio.shield(http_call), timeout)` — shield prevents cancellation of the underlying HTTP request on timeout, allowing the aiohttp session to close gracefully. The loop moves on; the connection pool stays clean.

## Strategy Plugin Interface (Ouroboros Graduation)

The loop checks for graduated strategies before calling J-Prime:

```python
async def _find_strategy(self, goal: str, app_context: str) -> Optional[List[PlannedAction]]:
    """Check AgentRegistry for a graduated navigation strategy.
    Returns None today — Ouroboros fills this as strategies graduate.
    """
    return None  # stub — future: AgentRegistry.find_strategy(goal, app_context)
```

**Graduation flow** (future, not built now):
1. Loop completes successfully 3 times for similar goals (same app pattern)
2. GraduationTracker detects pattern in action_logs
3. GraduationOrchestrator synthesizes a strategy class
4. Strategy registers in AgentRegistry
5. Next time, loop finds strategy → skips J-Prime → executes pre-built plan
6. Each pre-built step still goes through ACT → VERIFY (safety preserved)

**Strategy contract** (stable interface for future plugins):
```python
class NavigationStrategy:
    app_pattern: str  # discoverable by app name
    async def plan(self, goal: str, context: dict) -> List[PlannedAction]
```

## File Changes

### Modified files (2)

**`backend/core/runtime_task_orchestrator.py`** (~120 lines replacing ~90 lines)
- Replace `_dispatch_to_vision()` with agentic loop
- Add `_ask_jprime_next_action()` — builds v1 payload, calls MindClient
- Add `_is_stagnant()` — stagnation detection
- Add `_verify_action()` — 3-tier verify dispatch
- Add `_build_action_log_entry()` — structured entry builder
- Add `_find_strategy()` — stub returning None (future: Ouroboros)
- Add enums: `ActionOutcome`, `VerifyTier`, `StopReason`
- Remove `_attempt_ghost_hands_correction()` — subsumed by the loop (Ghost Hands correction IS a loop turn: J-Prime proposes correction, loop executes via Ghost Hands)

**`backend/core/mind_client.py`** (~140 lines added)
- Add `reason_vision_turn()` method (~60 lines: payload build, POST, validate)
- Add `_compress_frame_jpeg()` helper (~25 lines: JPEG quality, max_bytes, downscale, sha256)
- Add `_claude_vision_fallback()` helper (~55 lines: convert v1 request to Claude multimodal message, tool_use constraint for v1 schema output, validate and normalize response)

**Frame access path**: The loop obtains the FramePipeline via `VisionActionLoop.get_instance().frame_pipeline` (the `frame_pipeline` property was added in the VisionCortex implementation — Task 2). If VisionActionLoop is not running, the loop falls back to the existing single-shot behavior (open URL, return without vision verification).

**CancelledError handling**: The main `for turn in range(MAX_VISION_TURNS)` loop must catch `asyncio.CancelledError` separately and re-raise it. `CancelledError` is `BaseException` in Python 3.9+ and will NOT be caught by `except Exception`. The loop must not swallow it — task cancellation must propagate cleanly.

**Backend telemetry constants**: `"jprime_l2"`, `"claude_l3"`, `"strategy_cache"` (future). These are the only valid values for the `backend` field in telemetry payloads.

### Not modified

- VisionActionLoop — unchanged, loop calls `execute_action()` per turn
- VisionCortex — unchanged, continuous awareness runs independently
- FramePipeline — unchanged, loop reads `latest_frame`
- Ghost Hands / ActionExecutor — unchanged, used by ACT step
- ActionVerifier — unchanged, used by Tier 2 verify

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `VISION_LOOP_MAX_TURNS` | `10` | Hard cap on reasoning turns per goal |
| `VISION_LOOP_THINK_TIMEOUT_S` | `12.0` | J-Prime reasoning timeout per turn |
| `VISION_LOOP_JPEG_QUALITY` | `85` | Frame compression quality |
| `VISION_LOOP_MAX_FRAME_BYTES` | `500000` | Max frame payload, downscale if over |
| `VISION_LOOP_STAGNATION_WINDOW` | `3` | Identical successful actions before early exit |
| `VISION_LOOP_FRAME_STAGNATION` | `3` | Unchanged frames before early exit |
| `VISION_LOOP_VERIFY_CONFIDENCE_TAU` | `0.7` | Below this → Tier 3 surgical verify |
| `VISION_LOOP_DEFAULT_SETTLE_MS` | `200` | Default settle time when J-Prime omits it |
| `VISION_LOOP_MAX_SETTLE_MS` | `2000` | Cap on settle_ms to prevent stalling |

## Telemetry

Every turn emits `vision.loop.turn@1.0.0` to TelemetryBus:

```json
{
    "op_id": "req-a3f2b1c4",
    "session_id": "sess-uuid",
    "goal_hash": "sha256(goal)[:12]",
    "turn": 3,
    "goal_achieved": false,
    "action_type": "type",
    "verify_result": "success",
    "verify_tier": "executor",
    "confidence": 0.91,
    "reasoning_truncated": "Typing contact name...",
    "latency_ms": 2340,
    "backend": "jprime_l2"
}
```

Loop completion emits `vision.loop.completed@1.0.0`:

```json
{
    "op_id": "req-a3f2b1c4",
    "goal_hash": "sha256(goal)[:12]",
    "success": true,
    "stop_reason": "goal_satisfied",
    "total_turns": 6,
    "total_latency_ms": 14200,
    "backends_used": ["jprime_l2", "jprime_l2", "claude_l3"],
    "action_log_summary": "click→click→type→click→type→click"
}
```

## Testing Strategy

- Unit test `_is_stagnant()` with various action_log patterns
- Unit test `_verify_action()` 3-tier dispatch
- Unit test `_build_action_log_entry()` produces valid v1 schema entries
- Mock J-Prime: inject canned responses, verify loop terminates on `goal_achieved`
- Mock J-Prime: inject repeated same-action responses, verify stagnation detection
- Mock J-Prime: inject `model_refusal`, verify clean exit
- Integration: real VisionActionLoop + mocked J-Prime → verify full SEE→THINK→ACT→VERIFY cycle
- Test L3 fallback: J-Prime timeout → Claude adapter fires → valid v1 response
- Test frame compression: oversized frame → downscale → under max_bytes
- Existing RTO tests pass unchanged
