---
title: Gap #6 — NarrativeChannel (proactive narrative) — CLOSED 2026-05-04
modules: []
status: historical
source: project_gap_6_narrative_channel.md
---

# Gap #6 — NarrativeChannel (proactive narrative) — CLOSED 2026-05-04

5-slice arc closing the operator-flagged "silent black box" gap: O+V
is **proactive** (sensors fire ops without operator input) but
previously didn't surface the model's natural-language reasoning the
way Claude Code does. The fix makes the model's voice present
throughout the op lifecycle.

## Slices

* **Slice 1** — `narrative_channel.py` (~430 LOC): substrate.
  Closed 6-value `NarrativeKind` taxonomy
  (INTENT/PLAN_PROSE/TOOL_PREAMBLE/THINKING/L2_REPAIR_PROSE/
  POSTMORTEM_PROSE), closed 3-value `FrameState` lifecycle
  (BUFFERING/COMMITTED/DISCARDED). Thread-safe FIFO ring with
  monotonic `n-N` refs (NEVER reused — mirrors `BoundedBodyStore` /
  `DiffArchive` / `OpBlockBuffer` safety contract). Composite-key
  active-frame index `(op_id, phase, kind) → ref` lets multiple
  parallel frames per op coexist (THINKING + PLAN_PROSE interleaved).
  Streaming API: ``start_frame`` → ``append_token`` → ``commit`` /
  ``discard`` plus ``emit_complete`` one-shot helper. 48 tests.

* **Slice 2** — `tool_preamble_synthesizer.py` (~280 LOC) +
  `intent_prompter.py` (~370 LOC): two complementary modules.
  Synthesizer ships a declarative descriptor table covering all
  18 Venom tools — each tool kind has a per-template formatter
  that produces a deterministic 1-sentence WHY from tool name +
  args (no LLM call, zero cost). Intent prompter wraps a brief
  async LLM call (DW Tier 0, 50-token cap, 5s timeout, hard-fail-
  silent) for the op_started "I'm going to do X" prose. Both
  modules are pure substrate — no console import. 60 tests.

* **Slice 3** — `narrative_renderer.py` (~340 LOC): the load-bearing
  visual-hierarchy enforcer. Per-kind dispatch table maps each
  `NarrativeKind` to an explicit `FrameStyle` (glyph + tint +
  italic). Constraint 1 (Visual Hierarchy): 💭 INTENT/PLAN_PROSE
  in `bright_blue italic`, 🗣 TOOL_PREAMBLE in `bright_black italic`,
  🤔 THINKING in `bright_black italic`, 🔧 L2_REPAIR in `yellow
  italic`, 💀 POSTMORTEM in `red italic` — all distinct from the
  cyan used by system actions. Constraint 3 (No Clutter): indent
  matches op-block ``  │  `` side rail; word-wrap respects indent;
  empty/discarded frames render nothing. 33 tests.

* **Slice 4** — REPL + integration. Wired `synthesize_preamble` into
  `op_tool_start` (Constraint 2: Tool Transparency — every tool
  call gets a 🗣 line). Wired `_maybe_fire_intent_prompt` into
  `op_started` as a fire-and-forget asyncio task (NEVER blocks
  op_started; intent renders into console after LLM returns).
  Extended unified `/expand <ref>` dispatcher with `n-N` prefix.
  New `/narrate {off|preambles|on|verbose}` REPL verb sets the env
  flags so operators can tune density without touching env vars.
  14 tests.

* **Slice 5** — Graduation. Master flags `JARVIS_NARRATIVE_INTENT_ENABLED`
  + `JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED` flipped default-TRUE.
  Module-owned `register_flags(registry) -> 5` (intent enabled +
  preamble enabled + buffer size + intent timeout + intent max
  tokens). `register_shipped_invariants() -> 4` AST pins:
    1. **NarrativeKind 6-value taxonomy frozen**
    2. **Renderer covers all 6 kinds explicitly** (Constraint 1)
    3. **op_tool_start invokes synthesize_preamble** (Constraint 2,
       BUG-FIX REGRESSION PIN)
    4. **op_started invokes _maybe_fire_intent_prompt**
  20 graduation tests including end-to-end production-seed-boot +
  AST-pin synthetic-positive coverage.

## Numbers

* **175 / 175 green** across the 5 slices on first integrated run
* ~1,420 LOC substrate + ~1,200 LOC tests
* 5 FlagSpec seeds; 4 ShippedCodeInvariant pins; 1 memory file

## Architectural properties

* **Zero new rendering surface** for tool preambles — extends the
  existing `op_tool_start` 🗣 path; synthesized preambles render
  identically to model-emitted ones.
* **Cost-bound intent prompt** — Tier 0 DW only (cheapest), 50-token
  output cap, 5s wall-clock timeout, hard-fail-silent. Per-op
  micro-spend ~$0.0002. NEVER blocks op_started (asyncio.create_task
  pattern).
* **Fallback-only synthesis** — `synthesize_preamble` returns
  model-emitted preamble verbatim when present; only synthesizes
  on absence. Operators can't tell which were synthesized — they
  render identically.
* **Visual hierarchy structurally enforced** — per-kind `FrameStyle`
  table, AST-pinned to require explicit entries for all 6 kinds.
  Cannot regress to "all 💭 the same color".
* **No clutter contract** — renderer indents match the op-block
  side rail; empty/discarded frames render nothing; word-wrap
  preserves continuation alignment. Side-effect of slack-filling
  these constraints into the renderer's pure-formatting design.
* **REPL density control** — `/narrate {off|preambles|on|verbose}`
  composes the per-flag env state. Operators tune verbosity in one
  verb instead of remembering 4 env vars.

## Master-flag rollback contract

Each subsystem has its own master flag, all default-true post-graduation.
Setting any to `false` returns byte-identical legacy behavior:

```
JARVIS_NARRATIVE_INTENT_ENABLED=false           → no intent prompt
JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED=false     → no synthesized 🗣 lines
```

Or use `/narrate off` in the REPL to disable both via one verb.

## Reused architectural assets

| Existing asset | Reuse |
|---|---|
| `BoundedBodyStore` / `DiffArchive` / `OpBlockBuffer` ring patterns | Same monotonic-ref + drop-oldest + active-index-pruning + replace-in-place semantics |
| `tool_render_registry`-style descriptor table | Synthesizer's per-tool template registry mirrors the `ToolRenderDescriptor` shape |
| `op_tool_start` 🗣 line | Synthesizer fallback piggybacks on the existing rendering path — no parallel renderer |
| `DoublewordProvider` (Tier 0) | Intent prompter uses it directly — no new HTTP client |
| `SerpentREPL` `_handle_*` pattern | `/narrate` follows the same convention |
| Unified `/expand <ref>` dispatcher (Gap #1+3+5) | Extended with `n-` prefix branch |
| `_FLAG_PROVIDER_PACKAGES` discovery | Module-owned `register_flags()` auto-discovered |

## Why-nots (deliberately deferred)

* **Provider-side streaming for PLAN_PROSE / THINKING tokens during
  GENERATE/PLAN/L2_REPAIR phases** — would require touching
  providers.py extensively. The substrate is ready (channel +
  streaming API), but wiring providers' delta callbacks through the
  channel is a focused follow-up. Today operators see intent
  (op_started) + tool preambles (every tool call) which already
  delivers most of the CC-equivalent feel.
* **Postmortem narrative streaming** — same as above; substrate
  ready, provider wiring deferred.
* **Fanout to SSE for IDE consumption** — narrative frames could
  fire as SSE events for VS Code extension consumption. Followup arc.
