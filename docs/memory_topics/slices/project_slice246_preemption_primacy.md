---
title: Project Slice246 Preemption Primacy
modules: [tests/governance/test_slice246_preemption_primacy.py]
status: merged
source: project_slice246_preemption_primacy.md
---

**Slice 246 — Preemptive Interrupt Matrix & Human Override Protocol. MERGED PR #69489, main `8e6b271c`.** Closes the sovereignty hole [[project_slice245_resurrection_primacy]] opened: resurrected=-100 out-ranked voice_human=0 → a live human could be starved by an autonomous survivor. DW-sovereignty arc ([[project_dw_sovereignty_arc_intent]]).

**HONEST SCOPE — brief under-specified the PRIMARY fix (two parts):**

**Part 1 — Sovereign Human Tier (queue guarantee).** The stated vuln ("human queued behind background op") is a queue-ordering bug. Human-origin sources now rank BELOW resurrection → **Human > Resurrected > Normal**. Dynamic: `_sovereign_human_priority() = _resurrection_intake_priority() - _sovereign_primacy_margin()` (env `JARVIS_SOVEREIGN_PRIMACY_MARGIN` default 100), in BOTH intake `_compute_priority` (checked BEFORE the resurrected check) AND pool `submit()` (`_sovereign_pool_priority()`, checked before resurrected). Does NOT degrade resurrected (still beats all normal work). `SOVEREIGN_SOURCES = {voice_human, cli_emergency, human_override}` in leaf `intake/intent_envelope.py` (+ added cli_emergency/human_override to `_VALID_SOURCES`) so router+pool import without cycle.

**Part 2 — Cooperative Preemption (running-op backstop).** New `preemption.py`: `request_preemption/is_preemption_requested/clear_preemption/check_preemption/reset_preemptions` + `human_preemption_enabled()` (env `JARVIS_HUMAN_PREEMPTION_ENABLED` default-TRUE) + `OperationPreemptedError`. **Key design: OperationPreemptedError derives from BaseException** (like asyncio.CancelledError) so it slips PAST broad `except Exception` handlers in candidate_generator/orchestrator and reaches the pool worker's explicit catch (else it'd be converted to terminal failure). Kept SEPARATE from CancelToken (cancel→terminal POSTMORTEM; preempt→resumable re-ingest).
- Pool `submit()` = the SENTINEL: a sovereign-human submit fires `request_preemption` on `running_resurrected_op_ids()` (queue ordering can't preempt an already-RUNNING op). Gated + fail-soft.
- `tool_executor` round loop calls `check_preemption(op_id)` at the round boundary (the existing `per_round_observer` seam ~6484) → raises at coherent point. NO hard SIGTERM.
- Pool worker catches `OperationPreemptedError` BEFORE generic `except Exception` (mirrors the `_ParkRequested` non-terminal pattern) → status="preempted" + `resubmit_resurrected` (S245 reuse → micro-hibernation). Survivor re-enters VIP lane below the human, resumes from last durable phase.

**HONEST GRANULARITY (stated, not hidden):** yields at a ROUND boundary → the in-flight round restarts; completed phases in `OperationContext` survive (no catastrophic loss, NOT literally "zero compute" — same stateless-resume honesty as prior slices). Multi-worker pool + sovereign tier handle the common case; preemption is the worker-saturation backstop.

**Tests:** `tests/governance/test_slice246_preemption_primacy.py` 17 incl. Phase 4 (resurrected running → human emergency → sentinel fires → checkpoint raises → re-ingest → human dequeues before preserved survivor). **Updated 1 S245 test** whose invariant (`resurrected > voice_human`) is INTENTIONALLY overturned here → now asserts resurrected beats the top NON-sovereign source (test_failure). 162 green across pool/intake/hibernation/controller/242-246, zero regression. Pre-existing unrelated pgrep-pattern failure (battle_test script, Slice 125) proven independent via stash. Branched off fresh origin/main.

**Hierarchy now (both intake + pool, dynamic): Human (sovereign, ~-200) > Resurrected (~-100) > Normal (0..6 / route 1..7).**
