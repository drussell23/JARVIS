---
title: Project W2 4 Curiosity Closure
modules: [backend/core/ouroboros/governance/curiosity_engine.py, backend/core/ouroboros/governance/tool_executor.py, backend/core/ouroboros/governance/phase_runners/generate_runner.py, backend/core/ouroboros/governance/ide_observability_stream.py, backend/core/ouroboros/governance/ide_observability.py, tests/governance/test_curiosity_engine_slice1.py, tests/governance/test_curiosity_tool_policy_slice2.py, tests/governance/test_curiosity_sse_ide_get_slice3.py, tests/governance/test_w2_4_graduation_pins_slice4.py, scripts/livefire_w2_4_curiosity.py]
status: historical
source: project_w2_4_curiosity_closure.md
---

## Status: CLOSED 2026-04-25

W2(4) shipped end-to-end in a single day across 4 PRs:

| Slice | PR    | Merge SHA      | What                                    |
|-------|-------|----------------|------------------------------------------|
| 1     | #19373 | `c6af78f845`  | CuriosityBudget primitive + ContextVar + JSONL ledger + 4 env knobs |
| 2     | #19410 | `c7c42dbb8d`  | Rule 14 widening at SAFE_AUTO + GENERATE-phase budget binding |
| 3     | #19435 | `0226a6884e`  | SSE bridge (`bridge_curiosity_to_sse`) + IDE GET `/observability/curiosity{,/<question_id>}` |
| 4     | #19492 | `9d92de280f`  | GRADUATION — master flag default flipped false→true; 23 pins + runbook + live-fire |

Final main SHA at closure: **`9d92de280f`**

## What it enables

Pre-W2(4): `ask_human` (Venom tool) gated by NOTIFY_APPLY+ risk tier — model could only ask clarifying questions on Yellow/Orange ops.

Post-W2(4) graduation: model can also ask clarifying questions on Green (SAFE_AUTO) ops during exploratory work, gated by **all four**:
1. Master flag on (`JARVIS_CURIOSITY_ENABLED=true`, default)
2. CuriosityBudget bound to ambient ContextVar (set by GENERATE runner)
3. Posture in allowlist (default `EXPLORE,CONSOLIDATE`; HARDEN/MAINTAIN excluded)
4. Per-session quota + per-question cost cap not exhausted (3 questions × $0.05)

## Authority pins (graduation contract — pinned every commit)

- **Master-off → byte-for-byte pre-W2(4)** — single env knob `JARVIS_CURIOSITY_ENABLED=false` force-disables every sub-flag (mirrors W3(7) cancel master-off composition). 8 hot-revert tests.
- **BLOCKED tier still rejects ask_human** regardless of master state — pinned in `test_rule_14_blocked_tier_rejected_even_post_graduation`. The "no gate softening" invariant.
- **All 5 sub-flag readers gate on `if not curiosity_enabled():` first** — structural enforcement of master-off composition. Pinned via grep in `test_pin_master_off_composition_all_subflag_readers`.
- **SSE event vocab additive only** — Slice 3 added 41st event (`curiosity_question_emitted`), W3(7) Slice 7 count pin updated 40→41.
- **Schema `curiosity.1` frozen** — wire-format API for ledger consumers.
- **DenyReason vocab stable** — 5 values: `master_off / posture_disallowed / questions_exhausted / cost_exceeded / invalid_question`.

## Hot-revert recipe

```bash
export JARVIS_CURIOSITY_ENABLED=false
```

That single flip force-disables every sub-flag. No code revert, no service restart beyond env reload. Documented in `docs/operations/curiosity-graduation.md`.

## Files of record

- `backend/core/ouroboros/governance/curiosity_engine.py` (~445 lines) — primitive, env knobs, ledger, ContextVar, SSE bridge
- `backend/core/ouroboros/governance/tool_executor.py` Rule 14 (~line 2350) — SAFE_AUTO widening
- `backend/core/ouroboros/governance/phase_runners/generate_runner.py` (~line 139) — per-op budget binding
- `backend/core/ouroboros/governance/ide_observability_stream.py` — `EVENT_TYPE_CURIOSITY_QUESTION_EMITTED`
- `backend/core/ouroboros/governance/ide_observability.py` — `_handle_curiosity_list` + `_handle_curiosity_detail`
- `tests/governance/test_curiosity_engine_slice1.py` (24 tests)
- `tests/governance/test_curiosity_tool_policy_slice2.py` (13 tests)
- `tests/governance/test_curiosity_sse_ide_get_slice3.py` (17 tests)
- `tests/governance/test_w2_4_graduation_pins_slice4.py` (23 graduation pins, autouse contextvar reset fixture)
- `docs/operations/curiosity-graduation.md` (operator runbook)
- `scripts/livefire_w2_4_curiosity.py` (formal in-process live-fire, 20 checks, no API key required)

## Cross-links

- `project_w2_4_curiosity_scope.md` — original scope doc with operator decision points
- `project_phase_b_subagent_roadmap.md` — sibling Wave 2 work
- `project_wave3_item7_mid_op_cancel_scope.md` — pattern reference (W3(7) graduation pattern was the model)
