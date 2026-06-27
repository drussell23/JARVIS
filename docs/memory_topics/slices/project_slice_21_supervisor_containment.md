---
title: What it does
modules: []
status: historical
source: project_slice_21_supervisor_containment.md
---

PR #59076 squash-merged 2026-05-26 at `35ed93802e`. Branch `ouroboros/slice-21-pipeline-supervisor-containment`. Closes runner-contract violation surfaced by v16 soak (`bt-2026-05-26-220930`).

# What it does

`phase_runner.py:103-104` mandates: *"Never raise into the dispatcher path — catch exceptions, emit telemetry, and return PhaseResult(status='fail', ...)."* The site at `generate_runner.py:2195` raised raw RuntimeError when `generation is None` after retry loop exhaustion — violated the contract.

**Fix**: replace raise with `ctx.advance(POSTMORTEM, terminal_reason_code='generation_exhausted_unrepairable')` + `return PhaseResult(next_phase=None, status='fail', reason='generation_exhausted_unrepairable', artifacts={'generation_exhaustion': True, 'supervisor_containment_slice': '21'})`.

The dispatcher at `phase_dispatcher.py:1041` already handles `next_phase is None` cleanly: logs terminal, fires `_fire_terminal_postmortem` hook, returns ctx to orchestrator's BG worker loop. **No orchestrator.py changes needed** — the dispatcher is the layer that handles structured terminal failures.

# v16 forensic

The orchestrator was ALREADY resilient to the raise (downstream handler caught it, BG workers cleanly unregistered + picked up next ops — observable in debug.log). Slice 21 just makes the failure STRUCTURED rather than exception-based:
- No traceback noise in debug.log (clean WARN + structured PhaseResult)
- Terminal POSTMORTEM phase with terminal_reason_code recorded
- Dispatcher's postmortem hook fires for structured learning

# Verification

6 tests (2 AST pins + 4 spine). AST pin walks the `if generation is None:` branch and asserts NO raise inside + Return PhaseResult present. Dispatcher pin walks the terminal-exit branch and asserts no double-raise. **57/57 regression** across Slices 18c→21.

# v16 soak postmortem (separate, ORTHOGONAL issue)

v16 ran 22 minutes before manual termination. **`cost_breakdown: {'doubleword': $0.0159}`** — DW was called but minimally. Per-op cost summary showed `spent=$0.0000` repeatedly — meaning **the provider cascade exhausted BEFORE most DW calls were made**. That's an UPSTREAM config issue (likely DW trusted_seed admission gates rejecting some routes, or fleet topology misroute) ORTHOGONAL to Slice 21.

Slice 21 fixes the SYMPTOM (ugly RuntimeError noise). The ROOT CAUSE (why provider cascade exhausts with zero spend) is a separate diagnostic slice.

**With Slice 21 landed**: the failure mode is now clean, so the v17 soak (if relaunched) will produce structured postmortem signals instead of traceback noise, making the upstream cascade diagnostic significantly easier.

# Stop conditions before v17 detonation

The v16 session was wedged on cascade-exhausts-with-zero-spend BEFORE the healing matrix even got exercised. Slice 21 doesn't change this — it just makes the failure mode clean. A v17 detonation WITHOUT first diagnosing the upstream cascade issue would burn another hour producing the same artifact. **Next step recommendation**: diagnose why DW topology + trusted_seed admission isn't admitting models for the SWE-Bench-Pro op route BEFORE relaunching.

Related: [[project_slice_20bc_healing_rotation]] (the healing matrix that v16 was meant to exercise but didn't reach), [[project_slice_20a_self_fallback]] (predecessor), [[feedback_no_preresult_euphoria]] (Slice 21 is methodology validation; v16/v17 capability proof still open).
