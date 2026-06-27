---
title: Project Bg Spec Sealed
modules: []
status: historical
source: project_bg_spec_sealed.md
---

As of 2026-04-14 (commit 349d557b87), the BACKGROUND and SPECULATIVE
provider routes are SEALED. `doubleword_topology.routes.background.dw_allowed`
and `.speculative.dw_allowed` are `false`, and both entries carry
`block_mode: skip_and_queue` so `candidate_generator._generate_dispatch`
raises `background_dw_blocked_by_topology` / `speculative_deferred:blocked_by_topology`
instead of cascading to Claude. Gemma 4 31B is still wired into
`callers.semantic_triage` and `callers.ouroboros_plan` — that scope is
explicitly kept.

**Why:** Alignment test `bt-2026-04-14-182446` drove a right-sized
~2,836-token BG prompt envelope (vs. ~11K pre-patch) and still produced
**0/13 Gemma BG successes** — 12× 180s SSE stream stalls, 1× schema_invalid.
Root cause isolated to provider-side SSE stream stalling on the DW
endpoint, not prompt size. Derek's directive (verbatim): "Routing
continuous background daemons to Claude violates the fundamental unit
economics of scalable autonomy." Claude is ~$0.03/op vs BG's target
~$0.002/op — ~15× delta, unsustainable on a continuous background loop.

**How to apply:** Don't re-enable BG/SPEC `dw_allowed=true` until a
viable, cost-effective inference endpoint (not Gemma-on-DW, not Claude)
is actually secured — not just promised. When adding a new sensor, do
NOT route it through BACKGROUND or SPECULATIVE expecting work to flow;
the orchestrator will graceful-accept the skip and the op will just
queue dormant. If you need a sensor's work to actually execute today,
classify it into STANDARD (Midbrain — DW 397B + Claude fallback) or a
Prefrontal Cortex route, never BG/SPEC. Any PR that flips these flags
should link back to the new endpoint's production acceptance test, not
just a hypothesis that "the model is better now."
