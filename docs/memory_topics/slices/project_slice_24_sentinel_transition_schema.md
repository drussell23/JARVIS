---
title: The bug
modules: []
status: historical
source: project_slice_24_sentinel_transition_schema.md
---

PR #59079 squash-merged 2026-05-26 at `2d96815523`. Branch `ouroboros/slice-24-sentinel-transition-schema`. Closes latent half-shipped Phase 12 Slice F/H bug surfaced by v18 (`bt-2026-05-26-233010`) when Slice 23's autonomous fleet walker activated for the first time.

# The bug

`report_failure` had been extended (Phase 12 Slice F) to accept structured-error kwargs: `status_code: Optional[int]`, `response_body: str`, `is_terminal: bool`. It stuffed them into `extra` dict + passed via `**extra` to `_emit_transition`. But `_emit_transition` signature + `TransitionRecord` dataclass + `to_json()` were NEVER updated to receive them. Every `report_failure(status_code=...)` raised `TypeError: TopologySentinel._emit_transition() got an unexpected keyword argument 'status_code'` — silently swallowed by bare except at line 1379.

**Dormant until Slice 23** because the sentinel walker rarely fired pre-Slice-23. v18 exposed it within 20 min.

# Consequences

1. Audit ledger missing structural fields on every terminal failure
2. Sentinel state machine never persisted `is_terminal` transitions (in-memory state worked because exception handling caught source directly; persistent state drift on restart)
3. SSE bridge observers missed every terminal-state alert

# Fix mechanism (additive schema completion)

- `TransitionRecord` gains 3 fields with defaults
- `_emit_transition` signature gains same 3 kwargs (defaults)
- `to_json()` serializes each ONLY when non-default (`response_body` truncated to 512 chars; `is_terminal=False` suppressed; `status_code=None` suppressed) — preserves byte-identical legacy records

# Discipline

- No new env knobs (incorrect behavior, not feature toggle)
- No new state (additive to existing dataclass)
- No parallel implementation (leverages `extra` dict report_failure was already building)
- Byte-identical legacy contract preserved for 7 other `_emit_transition` callers
- 2 AST pins prevent future schema regression

# Verification

8 tests (2 AST + 6 spine). 182/182 regression across slice arc + entire existing test_topology_sentinel.py (60 tests untouched) + Phase 10 graduation contract (32 tests preserved).

# v18 forensic that exposed this

v18 (bt-2026-05-26-233010) live debug.log within 20 min:
```
16:42:13 Qwen/Qwen3.5-4B FAILED (http_403, auth_terminal=true) — trying next
16:42:13 attempting model=moonshotai/Kimi-K2.6 (state=CLOSED)
16:46:50 Qwen/Qwen3.5-35B-A3B-FP8 FAILED — trying next
16:46:50 attempting model=Qwen/Qwen3.5-397B-A17B-FP8
```

This forensic also **fully validates Slice 23** — all 4 models being attempted in rotation, exactly as designed.

# v18 status when Slice 24 merged

PID 91694 ALIVE elapsed=25:49 cost=$0.0084 — soak continues running; Slice 24 affects v19+ (current session's dispatcher already catches the TypeError, so v18 keeps walking the fleet correctly at runtime; v19 will see correct persistence + audit ledger).

Related: [[project_slice_23_sentinel_activation]] (the slice that activated the walker and exposed this bug), [[project_slice_20bc_healing_rotation]] (downstream consumer of correct sentinel state), [[feedback_no_preresult_euphoria]] (v18 fleet walk = architectural validation, NOT capability win — RESOLVED still the bar).
