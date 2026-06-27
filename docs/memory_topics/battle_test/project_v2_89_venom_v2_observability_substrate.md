---
title: Project V2 89 Venom V2 Observability Substrate
modules: [tests/governance/test_permission_decision_archive.py, backend/core/ouroboros/governance/permission_decision_archive.py, backend/core/ouroboros/governance/tool_executor.py, backend/core/ouroboros/governance/tool_permission.py, backend/core/ouroboros/governance/inline_permission_observability.py, backend/core/ouroboros/battle_test/tool_render_store.py, tests/governance/test_venom_v2_slice_1_tool_permission.py]
status: historical
source: project_v2_89_venom_v2_observability_substrate.md
---

May 10 2026: first forward-progress slice after cadence-arc closure (v2.86–v2.88) and post-arc empirical proof (`bt-2026-05-10-082106` outcome=clean cost=$0.13446 ops=17).

**Audit before building**: Explore agent verified 6 of 7 §37 Tier 2 catalog items already SHIPPED (`--rerun-from`/session search/op-graph/per-tool confidence/Operation Modes/Pattern C). Only genuine gap was Venom V2 — `tool_permission.py` substrate exists + composed by `tool_executor._maybe_evaluate_tool_permission` but no operator-facing query surface. Critical pre-build verification because the substrate is similar in name to the FULLY SHIPPED `inline_permission_observability.py` (different concern: mid-prompt human-in-the-loop, /observability/permissions/{prompts,grants}, default-TRUE) — collision-avoidance was load-bearing.

**Slice 1 scope** (substrate + wiring only; surfaces deferred):

1. New `permission_decision_archive.py` (~440 LOC) — mirrors `BoundedBodyStore` canonical ring pattern from `tool_render_store.py` (Gap #2 Slice 3). Closed `DecisionRecord` (frozen §33.5 with symmetric to_dict) + `ArchiveSnapshot` (capacity/size/next_seq/utilization). `BoundedDecisionArchive` class with thread-safe FIFO + drop-oldest eviction + monotonic `p-N` refs (counter never rewinds — load-bearing safety: a printed ref always resolves to the same decision OR None, never a different decision). `recent`/`by_tool`/`by_op` filtering APIs. `permission_archive_enabled()` master flag `JARVIS_PERMISSION_ARCHIVE_ENABLED` default-FALSE per §33.1 graduation contract. `JARVIS_PERMISSION_ARCHIVE_SIZE` env knob (default 50, bounds [1, 10000]). Singleton via `get_default_archive()` + `reset_default_archive_for_tests()`.

2. Producer-bridge §33.2 — `maybe_record_decision(*, op_id, tool_name, decision)` single-call composition wrapper. Master-flag-gated; NEVER raises into the policy path; defensive try/except swallows exceptions.

3. Wired at single seam in `tool_executor.py:_maybe_evaluate_tool_permission` (line 1218–1232) AFTER `evaluate_tool_permission` returns the decision — composes the existing wrapper rather than creating a parallel evaluation path. Defensive try/except so a fallback bug never breaks tool dispatch.

**Authority asymmetry preserved**:
- Archive module does NOT import `compute_permission_decision` / `PermissionRegistry` / `evaluate_tool_permission` / `ToolPermissionCallback` (AST-pinned)
- Archive composes `AggregatePermissionDecision.to_dict()` projection via duck-typing — preserves the §33.5 frozen-artifact contract without coupling
- Decision-value extracted via `getattr(decision.decision, 'value', ...)` — first-match-wins pattern, foreign shapes fall through to safe DEFER default

**Tests**: 31 regression tests in `tests/governance/test_permission_decision_archive.py`:
- Master-flag contract (default-False / canonical truthy alternation / no-op when off / producer-bridge no-op when off)
- Ring contract (p- prefix / drop-oldest eviction / monotonic refs never rewind / clear() doesn't reset counter)
- Filtering API (recent newest-first / by_tool exact match / by_op exact match)
- Projection contract (DecisionRecord.to_dict carries full inner projection / ArchiveSnapshot.to_dict shape)
- Fail-closed contract (invalid lookup returns None / malformed decision shape doesn't raise / producer-bridge swallows exceptions)
- Singleton lifecycle (same instance / reset drops singleton / capacity env var respected)
- 4 AST pins (p- prefix canonical literal / master-flag default-false structural / no-policy-imports authority asymmetry / record() first-statement-master-flag-shortcircuit)
- 1 tool-executor wiring pin (load-bearing seam: `_maybe_evaluate_tool_permission` MUST invoke `maybe_record_decision` AND import from canonical `permission_decision_archive` module)

**Regression**: 301 broader permission-related governance tests still green — zero impact on existing `test_venom_v2_slice_1_tool_permission.py` (the substrate that v2.89 hooks into).

**Operator binding 2026-05-10 satisfied verbatim**:
- Solved root problem directly — operator-visibility into permission decisions is the load-bearing fact; substrate+wiring composes at single seam
- No workarounds — used canonical `BoundedBodyStore` template instead of inventing a new ring shape
- No shortcuts — 31 tests + 4 AST pins for load-bearing structural invariants
- Composes existing canonical paths — `BoundedBodyStore` shape, `AggregatePermissionDecision.to_dict()` projection, `_maybe_evaluate_tool_permission` seam, `permission_archive_enabled()` master-flag pattern
- No hardcoding — capacity env-tunable, master flag env-gated, ref prefix exposed as public constant
- No duplication — verified pre-build that `inline_permission_observability.py` covers a different concern (mid-prompt human-in-the-loop), naming-collision avoided via `JARVIS_PERMISSION_ARCHIVE_*` (not `JARVIS_PERMISSION_*`) + `p-N` ref (not `perm-N`)

**Files**:
- `backend/core/ouroboros/governance/permission_decision_archive.py` (NEW, ~440 LOC)
- `backend/core/ouroboros/governance/tool_executor.py` (1 seam edit, +14 LOC for producer-bridge composition)
- `tests/governance/test_permission_decision_archive.py` (NEW, 31 tests)

**Surfaces deferred to follow-on Slices** (each independent, ~1-2h each):
- Slice 2: `/tool-permissions {recent|by-tool|by-op}` REPL verb (§33.3 naming-cage auto-discovery) + extend `/expand <ref>` dispatcher for `p-N` (5th prefix joining `t-N`/`d-N`/`o-N`/`n-N`)
- Slice 3: SSE event type `permission_decision_recorded` in `_VALID_EVENT_TYPES` + producer hook from archive
- Slice 4: IDE GET `/observability/tool-permissions` (loopback + rate-limited + CORS + schema-versioned, composing `IDEObservabilityRouter` pattern)
- Slice 5: FlagRegistry seed for new flags (1 master + 1 size env var)
- Slice 6: PRD §37 Tier 2 #6 row update + §3.6.3 row 🟡 → ✅

These surfaces are forward-compatible with Slice 1 substrate; each can ship as a tight ~1-2h commit when operator chooses to graduate the master flag.

**Why ship substrate-only first**: per CLAUDE.md "Don't add features beyond what the task requires" + operator binding "fully leverage existing files; avoid duplication". Slice 1 is a complete, parseable, testable closure on its own. Surfaces are additive read-side wrappers; building them all at once would be a 6h slice that's harder to review and review-revert. Operator can run `JARVIS_PERMISSION_ARCHIVE_ENABLED=true` + custom REPL invocation to query the ring directly today; the surfaces just polish the operator-UX.

**Why this matters for RSI**: the proactive curiosity loop fired during the post-arc soak (intent: "domain code_gen::.py fails 67%, address import_error proactively") but BACKGROUND topology-block prevented action. When the system DOES start acting on its own curiosity signals, `tool_permission.py` becomes the policy boundary between proactive ops and operator authority. Without observability into that boundary's decisions, the operator has no signal of what the system is choosing not to do. Slice 1 closes that signal.
