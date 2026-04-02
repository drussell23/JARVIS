# Sub-project D: The Voice (DaemonNarrator Wiring)

**Date:** 2026-04-02
**Parent:** [Ouroboros HUD Pipeline Program](2026-04-02-ouroboros-hud-pipeline-program.md)
**Status:** Approved
**Depends on:** Sub-projects A, B, C

## Problem

The Ouroboros governance pipeline has a fully functional voice narration system (VoiceNarrator in CommProtocol, `safe_say` for TTS, `narrator_script.py` templates). But two gaps prevent Samantha from narrating the full lifecycle:

1. **GovernedLoopService has no `say_fn`** — its constructor (line 682 of `governed_loop_service.py`) has no `say_fn` parameter. Internal narrators (ReasoningNarrator, PreActionNarrator, EmergencyProtocolEngine) use `getattr(self, "_say_fn", None)` and always get None.

2. **Sub-project C gates are silent** — duplication blocks, similarity escalations, and verify regressions log warnings and emit ledger entries, but don't emit CommProtocol events that VoiceNarrator can hear. Also, `VoiceNarrator._map_phase` only branches on `outcome`, not `reason_code`, so new gate events would route to generic templates.

## Changes

### Phase 1: Wire `say_fn` into GovernedLoopService

**File:** `backend/core/ouroboros/governance/governed_loop_service.py` (line 682)

Add `say_fn: Optional[Callable[..., Coroutine[Any, Any, bool]]] = None` to `__init__`. Store as `self._say_fn = say_fn`. The existing `getattr(self, "_say_fn", None)` calls in `_build_components` then find it.

**Callers to update:**

| File | Location | Current | Change |
|------|----------|---------|--------|
| `unified_supervisor.py` | Search `GovernedLoopService(` (~line 85020, fallback path) | No `say_fn` | Add `say_fn=safe_say` (import from `unified_voice_orchestrator` with try/except) |
| `unified_supervisor.py` | Search `GovernedLoopService(` (~line 87273, handshake path) | No `say_fn` | Add `say_fn=safe_say` (same import) |
| `trigger_ignition.py` | Search `GovernedLoopService(` (~line 264) | No `say_fn` | Import `safe_say` from `unified_voice_orchestrator` (with try/except fallback to None), then pass `say_fn=safe_say`. Note: `safe_say` is NOT currently imported in this file — `_mock_safe_say` exists but is not the same function. |

Tests pass `None` (default) — no behavior change.

### Phase 2: Gate Event Emissions from Orchestrator

**File:** `backend/core/ouroboros/governance/orchestrator.py`

Add CommProtocol emissions at each Sub-project C gate firing point. Use existing `emit_decision` and `emit_postmortem` APIs — no new comm methods.

**1. Duplication blocked (VALIDATE):**
After the duplication checker returns a failure, the orchestrator's candidate validation loop emits:
```python
await self._stack.comm.emit_decision(
    op_id=ctx.op_id,
    outcome="blocked",
    reason_code="duplication",
)
```

**2. Similarity escalation (GATE):**
After `risk_tier` is set to `APPROVAL_REQUIRED` by the similarity gate, emit:
```python
await self._stack.comm.emit_decision(
    op_id=ctx.op_id,
    outcome="escalated",
    reason_code="similarity_escalation",
)
```

**3. Verify regression (VERIFY):**
Before advancing to POSTMORTEM, emit using existing `emit_postmortem` API with gate info encoded in `root_cause` and `failed_phase`:
```python
await self._stack.comm.emit_postmortem(
    op_id=ctx.op_id,
    root_cause=f"verify_regression: {_verify_error}",
    failed_phase="VERIFY",
)
```

Note: `emit_postmortem` has no `reason_code` parameter — only `root_cause`, `failed_phase`, `next_safe_action`. We encode the gate type as a stable prefix in `root_cause` so `_map_phase` can match it.

**Idempotency:** `VoiceNarrator` keys idempotency on `op_id:msg_type_name`. Only the first DECISION per op is narrated. If duplication fires, a later similarity check for the same op won't double-speak. POSTMORTEM is a different `MessageType` so verify regression always narrates separately.

**Ordering:** Emit the gate DECISION before any generic DECISION in the same op path. Since duplication fails VALIDATE (returns early), and similarity fires in GATE (before any "applied" DECISION), the gate event is always first.

### Phase 3: Extend VoiceNarrator Routing

**File:** `backend/core/ouroboros/governance/comms/voice_narrator.py` (line 159-174)

Extend `_map_phase` to check `reason_code` (for DECISION) and `root_cause` prefix (for POSTMORTEM) before falling through to generic routing:

```python
@staticmethod
def _map_phase(msg: CommMessage) -> str:
    if msg.msg_type == MessageType.INTENT:
        return "signal_detected"
    elif msg.msg_type == MessageType.POSTMORTEM:
        root_cause = msg.payload.get("root_cause", "")
        if root_cause.startswith("verify_regression"):
            return "verify_regression"
        return "postmortem"
    elif msg.msg_type == MessageType.DECISION:
        reason = msg.payload.get("reason_code", "")
        if reason == "duplication":
            return "duplication_blocked"
        if reason == "similarity_escalation":
            return "similarity_escalated"
        outcome = msg.payload.get("outcome", "")
        if outcome in ("applied", "validated"):
            return "applied"
        elif outcome == "blocked":
            return "approve"
        else:
            return "applied"
    return "signal_detected"
```

### Phase 4: Narration Templates

**File:** `backend/core/ouroboros/governance/comms/narrator_script.py`

Add three entries to `SCRIPTS` dict (matching existing flat string format):

```python
"duplication_blocked": (
    "I blocked a code change for {file}. "
    "The generated code duplicated existing logic."
),
"similarity_escalated": (
    "A code change for {file} has high overlap with existing code. "
    "Escalating for your review."
),
"verify_regression": (
    "I rolled back a change to {file}. "
    "Post-apply verification failed: {root_cause}."
),
```

Add corresponding entries to `_REQUIRED_KEYS`:
```python
"duplication_blocked":   ("file",),
"similarity_escalated":  ("file",),
"verify_regression":     ("file", "root_cause"),
```

**Context availability:**
- `{file}`: Set by VoiceNarrator from `msg.payload["target_files"][0]` (line 141-143). For DECISION events from gate firings, the orchestrator must include `target_files` in the context. But `emit_decision` does NOT have a `target_files` parameter today.
- **Fix:** For gate DECISION emissions, set `target_files` on the CommMessage payload by passing it through `diff_summary` (string field, already optional) as a JSON-encoded list, or extend `emit_decision` to accept an optional `target_files` parameter.
- **Chosen approach:** Extend `emit_decision` to accept `target_files: Optional[List[str]] = None` and include it in the payload when set. This is a backward-compatible addition.
- For POSTMORTEM, `root_cause` is already in the payload (line 318-319 of comm_protocol.py). `{file}` comes from `target_files` which is NOT in `emit_postmortem` today. Add optional `target_files: Optional[List[str]] = None` to `emit_postmortem` too.

## Testing Strategy

| Test | File | What it verifies |
|------|------|-----------------|
| `test_gls_accepts_say_fn` | `test_daemon_narrator_wiring.py` | GLS constructor stores say_fn |
| `test_gls_say_fn_default_none` | `test_daemon_narrator_wiring.py` | Default None for tests/headless |
| `test_map_phase_duplication` | `test_daemon_narrator_wiring.py` | reason_code=duplication → "duplication_blocked" |
| `test_map_phase_similarity` | `test_daemon_narrator_wiring.py` | reason_code=similarity_escalation → "similarity_escalated" |
| `test_map_phase_verify_regression` | `test_daemon_narrator_wiring.py` | root_cause prefix "verify_regression" → "verify_regression" |
| `test_map_phase_generic_decision` | `test_daemon_narrator_wiring.py` | Existing outcome routing unchanged |
| `test_narration_template_duplication` | `test_daemon_narrator_wiring.py` | Template renders with file context |
| `test_narration_template_verify` | `test_daemon_narrator_wiring.py` | Template renders with root_cause |
| `test_emit_decision_target_files` | `test_daemon_narrator_wiring.py` | Extended emit_decision includes target_files in payload |

## Files Created/Modified

| File | Action |
|------|--------|
| `backend/core/ouroboros/governance/governed_loop_service.py` | Modify (add say_fn param) |
| `backend/core/ouroboros/governance/comms/voice_narrator.py` | Modify (_map_phase extension) |
| `backend/core/ouroboros/governance/comms/narrator_script.py` | Modify (3 new templates + keys) |
| `backend/core/ouroboros/governance/comm_protocol.py` | Modify (target_files param on emit_decision + emit_postmortem) |
| `backend/core/ouroboros/governance/orchestrator.py` | Modify (3 gate event emissions) |
| `unified_supervisor.py` | Modify (pass say_fn to GLS) |
| `trigger_ignition.py` | Modify (pass say_fn to GLS) |
| `tests/governance/test_daemon_narrator_wiring.py` | Create |

## Implementation Notes

- **`root_cause` prefix stripping for speech:** The `verify_regression` template uses `{root_cause}` which contains the prefix `"verify_regression: ..."`. For speech quality, strip the prefix before formatting: `root_cause.replace("verify_regression: ", "", 1)` in the context before calling `format_narration`. This keeps full string in logs while Samantha says the human-readable part.
- **CommProtocol test follow-up:** Adding optional `target_files` to `emit_decision`/`emit_postmortem` is backward compatible for callers, but tests that mock these methods with strict arity (`assert_called_with`) may need updating. Grep for `emit_decision` and `emit_postmortem` mock assertions after implementation.
- **Line numbers are approximate:** Use symbol-based searches (`GovernedLoopService.__init__`, `GovernedLoopService(` in supervisor) rather than relying on exact line numbers which drift with edits.

## Out of Scope

- HUD overlay graphics for narration (future UX work)
- Custom per-gate narration voices (uses default Samantha)
- Real-time streaming narration during generation (HEARTBEAT events already work)
- Multi-DECISION idempotency relaxation (first DECISION per op wins — acceptable)
