---
title: Project Vision Sensor Verify Arc
modules: [tests/governance/test_attachment_export_ban.py, backend/vision/frame_server.py, backend/core/ouroboros/governance/visual_verify.py, backend/core/ouroboros/governance/risk_tier_floor.py, backend/vision/vision_reflex.py, backend/core/ouroboros/governance/vision_repl.py]
status: historical
source: project_vision_sensor_verify_arc.md
---

Apr 18, 2026 — second capability picked off the CC-parity feature list (after Phase 1/B subagent graduation).

**Decision:** Ambitious track chosen over attachments-first. VisionSensor (proactive sensing via Ferrari frame stream) + Visual VERIFY (post-APPLY UI regression catch) ship as a 4-slice graduation arc, not on-demand multi-modal chat.

**Why:** Literal `/bug --screenshot` REPL parity is low-value versus continuous sensing + automatic visual verification. Manifesto boundary between deterministic execution and agentic discovery gets exercised in production via sensing, not chat. Derek explicitly accepted the larger design surface (FP budget, cost envelope, "what counts as a bug screen") in exchange for proactive value.

**How to apply:**
- Spec is single source of truth: `docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md` (status Approved, D1–D5 decisions folded in).
- Implementation plan: `docs/superpowers/plans/2026-04-18-vision-sensor-verify.md` (22 tasks, 4 slices).
- Each slice = 3 consecutive clean sessions before default-flip (same discipline as Phase 1→B subagent arc).
- Slice 2 graduation does a **dual flip**: Tier 2 VLM enabled AND chain cap 1→3 together (one earns the other).
- Slice 4 (model-assisted VERIFY) has an auto-demotion guardrail: post-flip session with FP rate ≥50% on `regressed` verdicts auto-reverts the default. No opt-in-forever end state.

**Structural invariants to enforce in any future vision work:**
- **I7 substrate export-ban.** `ctx.attachments` consumable only by VisionSensor and `visual_verify.py`. Enforced by `tests/governance/test_attachment_export_ban.py` (CI greps for unauthorized reads) + `providers._serialize_attachments(ctx, provider_kind, purpose)` with `purpose ∈ {sensor_classify, visual_verify}` gate. Any new consumer needs its own spec review + 3-session arc.
- **I8 no capture authority.** VisionSensor is a read-only Ferrari consumer. `VisionCortex` is the canonical owner. Sensor must fail closed (degraded telemetry, zero signals) when Ferrari absent — must never invoke `_ensure_frame_server()`, spawn `frame_server.py`, or touch Quartz/SCK/AVFoundation. Module-level static check in regression spine.
- **Vision signals never SAFE_AUTO.** Hardcoded floor `NOTIFY_APPLY` in `risk_tier_floor.py` for `SignalSource.VISION_SENSOR`, env tunable upward only.
- **No IMMEDIATE route for vision signals.** Too high FP risk; voice/test-failure signals remain the only IMMEDIATE triggers.

**Ferrari mechanical detail (resolved in this arc's deep dive):** `backend/vision/frame_server.py` = Ferrari Engine, Tier 2 in 4-tier capture cascade (~50ms vs 200ms screencapture), spawned per-window in parallel for God Mode 60fps multi-space OCR race-condition detection. "Retina" = poetic metaphor in `vision_reflex.py:296` for `VisionReflexCompiler._reflexes` dict (hot-swap cache for 397B-synthesized fast-path reflex code).

**Implementation status (2026-04-18):**
Tasks 1-21 **COMPLETE**. 799 tests green across 21 Vision-arc test files. Every module shipped, every synthetic integration smoke-tested end-to-end. Breakdown:
- Tasks 1-7: Substrate (Attachment + export-ban + SignalSource + FORBIDDEN_APP + plan ui_affected + risk_tier_floor + provider serialization with purpose-gate).
- Tasks 8-13: VisionSensor (Tier 0/1 deterministic + retention + threat model T1-T7 + FP/chain/cost policy + boot wiring).
- Tasks 14/16/18/20: Slice 1/2/3/4 pre-flights (synthetic integration) + operator graduation checklists under `docs/operations/vision-sensor-slice-{1,2,3,4}-graduation.md`.
- Tasks 15, 17, 19: Tier 2 VLM classifier + Visual VERIFY deterministic phase + model-assisted advisory with auto-demotion guardrail.
- Task 21: `/vision status|resume|boost` REPL handlers + dashboard status line renderer + `[vision-origin]` tag helper in `vision_repl.py`.
- Task 22: Final regression sweep (this task) — Vision arc fully green; 45 unrelated pre-existing failures in intake/saga/self_dev/graduation_orchestrator confirmed on clean main (predate the arc).

**Wiring handoffs still pending (flagged in each task's summary):**
- Orchestrator FSM: add `VISUAL_VERIFY` phase to `OperationPhase` + transitions + post-VERIFY call to `run_if_triggered` + advisory dispatch + session-end `check_and_apply_auto_demotion`.
- SerpentFlow: register `/vision {status,resume,boost}` + `/verify-{confirm,undemote}` slash handlers; prefix `vision_origin_tag()` in Update blocks.
- LiveDashboard: call `format_vision_status_line(sensor)` in per-tick render loop.
- IntakeLayerService: construct real VLM adapter (lean_loop-backed) and pass as `vlm_fn` at sensor construction — without it, Tier 2 stays stubbed.

**Graduation arcs (3-session each — operational, not in scope for autonomous execution):**
- Slice 1: `JARVIS_VISION_SENSOR_ENABLED` default flip (force failures + FP discipline + privacy session).
- Slice 2: dual flip `TIER2_ENABLED=true` + `CHAIN_MAX=3` (VLM fires + zero chain hits + ≤$0.50/session).
- Slice 3: `VISION_VERIFY_ENABLED` flip (≥3 UI ops reach VERIFY + zero false reject + ≥1 regression TestRunner missed).
- Slice 4: `MODEL_ASSISTED_ENABLED` flip PLUS mandatory auto-demotion stress test (post-flip forced-bad session must demote and `/verify-undemote` must re-arm).

**Next action:** Ship the orchestrator FSM wiring (small scoped commit covering phase enum + transitions + VERIFY handler hook + advisory dispatch), run Slice 1 pre-flight against the real harness, then start the real 3-session arc for Slice 1. Full arc graduation is ~12 sessions of human time (4 slices × 3 sessions each + 1 auto-demotion stress test).
