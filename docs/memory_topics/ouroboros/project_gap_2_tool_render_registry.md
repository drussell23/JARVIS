---
title: Gap #2 — ToolRenderRegistry closure (2026-05-04)
modules: [backend/core/ouroboros/battle_test/tool_render_registry.py, backend/core/ouroboros/battle_test/tool_render_policy.py, backend/core/ouroboros/battle_test/tool_render_store.py, backend/core/ouroboros/battle_test/tool_render_view.py]
status: historical
source: project_gap_2_tool_render_registry.md
---

# Gap #2 — ToolRenderRegistry closure (2026-05-04)

5-slice arc closing the "tool result bodies expand unbounded" gap from
the SerpentFlow / LiveDashboard UX audit. Replaced two hardcoded
render paths (``serpent_flow.op_tool_call`` per-tool ``if/elif`` +
``tool_icons`` literal dict; ``ouroboros_tui.show_tool_call``
``8/20``-line caps + per-tool branching) with a descriptor-driven
adaptive substrate that respects posture × layout × env signals and
parks full bodies behind stable ``/expand t-N`` references.

## Slices

* **Slice 1** — `tool_render_registry.py` (~700 LOC): closed
  6-value `BodyShape` taxonomy; closed 4-value `ToolStatus`;
  frozen `ToolRenderDescriptor` + `RenderedToolResult`;
  declarative `_DESCRIPTORS: Mapping[str, ...]` covering all 18
  Venom tools (15 sync `_dispatch` + 3 async-native ToolManifests)
  + `_DEFAULT_DESCRIPTOR` for MCP-forwarded tools; pure
  `render()` function. 114 tests.

* **Slice 2** — `tool_render_policy.py` (~470 LOC): closed
  3-value `DensityLevel` (compact/balanced/verbose); closed
  3-value `LayoutKind` (flow/split/focus); declarative
  `_RESOLUTION_TABLE: Mapping[(Posture, LayoutKind), DensityLevel]`
  covering all 12 cells; 3-step precedence
  (explicit_override → env → table) with documented fallbacks;
  `PostureProvider` / `LayoutModeProvider` `@runtime_checkable`
  Protocols for DI; lazy-import `Default*Provider` classes
  preserving the Slice 5 DI cage. 66 tests.

* **Slice 3** — `tool_render_store.py` (~280 LOC): thread-safe
  `BoundedBodyStore` with drop-oldest FIFO eviction; monotonic
  `t-N` ref allocation that NEVER reuses (frozen safety contract
  for the eventual `/expand` REPL verb); env-driven capacity
  via `JARVIS_TOOL_RENDER_STORE_SIZE` (default 50, clamped
  [1, 10_000]); `StoreSnapshot` projection for observability;
  defensive coercion on every input. 32 tests.

* **Slice 4** — `tool_render_view.py` (~520 LOC): the **only**
  Rich-importing layer. Frozen `ComposedToolRender` output;
  `compose()` orchestrator (descriptor lookup + density resolve
  + body park + bounded render + Rich markup composition);
  per-`BodyShape` markup wrappers (diff colors, log dim, multi-
  line text); defensive `[`/`]` escaping; master-flag-gated
  `compose_if_enabled()` shim. Surgically wired into both
  `serpent_flow.op_tool_call` (line 1632) and
  `ouroboros_tui.show_tool_call` (line 163) at the top — legacy
  paths preserved verbatim below the guard for byte-identical
  fallback. 47 tests including `Console(record=True)`
  end-to-end markup capture.

* **Slice 5** — Graduation: master flag
  `JARVIS_TOOL_RENDER_REGISTRY_ENABLED` default-flipped to
  `true`; module-owned `register_flags(registry) -> 3` (auto-
  discovered via `_FLAG_PROVIDER_PACKAGES`); module-owned
  `register_shipped_invariants() -> 3 ShippedCodeInvariant`
  pinning (1) view public surface, (2) descriptor completeness
  for all 18 Venom tools, (3) policy DI cage forbidding
  top-level `posture_observer` / `posture_store` /
  `posture_health` imports. 16 graduation tests including
  AST-pin synthetic-positive coverage (each pin proven to fire
  on a deliberately-broken synthetic source).

## Numbers

* **275 / 275 green** across the 5-slice arc on first integrated run
* **0 regressions** — full `tests/battle_test/` sweep showed only
  4 pre-existing failures (verified by git-stash regression check)
* ~1,970 LOC substrate + ~2,200 LOC tests
* 3 FlagSpec seeds; 3 ShippedCodeInvariant pins; 1 memory file

## Architectural properties

* **No hardcoded `if tool_name ==`** chains downstream of the registry
* **No hardcoded line caps** (`8/20/200`) — replaced by
  `DensityPolicy.max_body_lines`
* **No top-level dependency** on stateful posture/layout surfaces
  in `tool_render_policy.py` — DI cage AST-pinned
* **No silent ref reuse** — `BoundedBodyStore` counter is monotonic
  forever, so an operator's printed `t-12` either resolves to the
  same body (if resident) or `None` (if evicted), never a different
  body
* **Layered design** — Slices 1-3 are renderer-agnostic (no Rich
  import); Slice 4 is the only Rich surface; runtime test pin at
  the substrate proves the layering

## Why-nots (deliberately deferred)

* `/expand <ref>` REPL verb itself — belongs to **Gap #4** (diff
  preview persistence) since it shares the "retrieve persistent
  artifact by ref" substrate. Slice 3 lays the foundation; the
  user-facing verb is one wiring change away.
* Async — substrate is purely synchronous. Tool result bodies are
  already capped upstream by `tool_executor`, so the render path
  is bounded; adding async would be ceremony not value.
* Time-based TTL on the store — capacity-only eviction via FIFO;
  session lifetime is the natural TTL.
