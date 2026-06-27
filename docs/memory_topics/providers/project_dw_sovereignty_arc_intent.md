---
title: Project Dw Sovereignty Arc Intent
modules: []
status: historical
source: project_dw_sovereignty_arc_intent.md
---

The load-bearing intent of the Slice 235→241 arc (operator restated it 3×, so hold it): **make DoubleWord (DW) work as the reliable, resilient PRIMARY provider for the Ouroboros+Venom (O+V) autonomous loop, so O+V completes real autonomous work (GOAL → state=applied) without ever needing Claude.**

Two forcing functions, both pointing at DW-primary:
1. **Claude is economically dead** — out of credits (BadRequestError 400 credit-too-low). It's not an available lane.
2. **Claude is more expensive than DW even when funded** ($3/$15 per M vs DW $0.10/$0.40). So DW is the *preferred* primary on cost, not a tolerated fallback.

DW has genuinely rougher transport characteristics (stateless OpenAI-compat /v1/chat/completions SSE — no resumable offset; slower TTFT; occasional real stream ruptures), so much of the arc HARDENS the pipeline to compensate: bounded diffs (235/236), convergence under deadline (237), cascade-respects-economic-breaker (238), test-sharding decouple (239/240).

**Recurring sub-theme (the subtler half): we kept blaming DW for OUR OWN problems.** Slice 185 found internal Python bugs (NameError) mislabeled as `live_transport`. Slice 241/T1 found the same for `tool_loop_deadline_exceeded` (our Venom tool-loop budget exhaustion) → falsely classified LIVE_TRANSPORT → falsely degraded DW health + severed its lane + fed the dead-Claude cascade. So "make DW work for O+V" is partly **honestly separating "DW is genuinely flaky" from "we falsely accused DW"** — a correct label lets DW's real reliability show through (fix = the `is FailureSource.LIVE_TRANSPORT` consumers ignore a correctly-labeled GENERATION_TIMEOUT).

The capstone everything chases — "a heavy multi-file GOAL reaches `state=applied`" — IS literally "O+V completed a real change on DW alone, no Claude." See [[project_slice235_validation_sixth_layer.md]] for the per-layer status (6-9 + 240 + 241T1 all merged; T2 = graduate the RT-vs-batch hedge, pending). The honest open question at each soak: did exhaustion/terminal_quota/false-sever drop, and did a heavy GOAL apply — and if not, what's the single binding constraint + how many independent layers remain.
