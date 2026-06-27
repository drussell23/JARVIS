---
title: Project Subagent Freeform Closure
modules: [backend/foo.py]
status: historical
source: project_subagent_freeform_closure.md
---

May 2, 2026: Free-Form Subagent Delegation 2-slice arc closed same-day. Lifts the Subagent delegation row in the CC parity table from ⚠️ structured-only (EXPLORE/REVIEW/PLAN/GENERAL only via orchestrator-driven slots) to ✅ free-form (model-driven via `dispatch_subagent(type="general", goal="<arbitrary>")`).

**The structural footgun this arc closed:**
Pre-Slice-1, widening the Venom tool's `subagent_type` enum to include GENERAL would have caused every model-driven dispatch to silently fail with `MalformedGeneralInput` at the `AgenticGeneralSubagent` boundary. The cause: `SubagentRequest.from_args()` did not synthesize the `general_invocation` / `plan_target` / `review_target_candidate` per-type fields the executors required. Audit identified this BEFORE the cosmetic enum widening; arc shipped the synthesizer FIRST then the enum exposure.

**Two slices shipped:**

1. **Slice 1 — Dynamic linkage + per-type synthesis** (`subagent_contracts.py`, `tool_executor.py`, commit `16f7114eba`):
   - 3 dynamic linkage helpers in `subagent_contracts.py`: `subagent_type_enabled()` (per-type kill switch), `policy_allowed_subagent_types()` (frozenset, authoritative), `tool_schema_subagent_types()` (sorted tuple, manifest enum source). All derive from `SubagentType` enum filtered through per-type flags.
   - 3 per-type invocation synthesizers: `_synthesize_general_invocation` / `_synthesize_plan_target` / `_synthesize_review_target`. Defaulting is TRANSPARENT — the Semantic Firewall §5 owns rejection at dispatch time. Conservative defaults derived from canonical sources (`firewall.readonly_tool_whitelist()` for tool list — never hardcoded).
   - `SubagentRequest.from_args()` extended to accept `parent_op_risk_tier` / `parent_op_description` / `parent_primary_repo` kwargs and dispatch to the appropriate synthesizer per-type.
   - `tool_executor.py` manifest's hardcoded `enum: ["explore"]` replaced with call to `_dynamic_subagent_type_enum()` helper at module load. `GoverningToolPolicy._READ_ONLY_SUBAGENT_TYPES` hardcoded frozenset replaced with call to `policy_allowed_subagent_types()` at check site (line 2296).
   - `_run_dispatch_subagent` threads `parent_op_risk_tier` from `policy_ctx` into `from_args` (defense-in-depth: model cannot fake higher tier in args — synthesizer ignores any model-supplied `parent_op_risk_tier`).
   - Manifest schema extended with 4 boundary-field args (`operation_scope`, `max_mutations`, `allowed_tools`, `invocation_reason`) — all optional with conservative defaults.
   - 56 tests covering closed-taxonomy invariants, mathematical schema↔policy lock, synthesizer matrix, defaulting transparency, parent_tier defense.

2. **Slice 2 — Graduation + AST pins** (commit `db55c12897`):
   - 4 per-type kill switch flags registered via dynamic discovery (162 total flags, was 158): all default-true post Phase B graduation.
   - 5 dynamic-linkage AST-pin invariants registered via dynamic discovery (81 total invariants, was 76). Pins enforce: 3 dynamic helpers + 3 synthesizers + from_args invokes synthesizers + manifest enum uses dynamic helper + policy uses dynamic helper. AST walk on the manifest enum scopes specifically to the `dispatch_subagent` ToolManifest dict (avoids false positives on unrelated tools like `delegate_to_agent`).
   - End-to-end Venom-path test: `_run_dispatch_subagent` with `subagent_type="general"` reaches `AsyncProcessToolBackend → mock SubagentOrchestrator.dispatch()` with populated `general_invocation`. Asserts: `result.status is SUCCESS` (not EXEC_ERROR with MalformedGeneralInput); request carries goal/operation_scope/max_mutations/invocation_reason/parent_op_risk_tier/allowed_tools; default tools include `read_file` + `search_code` from canonical `readonly_tool_whitelist()`.
   - 10 graduation tests; hot-revert proof (single env flip reverts BOTH schema + policy together).

**Architectural reuse spine — single source of truth pattern:**
- `SubagentType` enum (`subagent_contracts.py:372`) is THE load-bearing source. Both Venom tool's manifest enum AND `GoverningToolPolicy` frozenset derive from it via two helpers, which in turn filter through per-type kill switches (`JARVIS_SUBAGENT_<TYPE>_ENABLED`).
- Mathematically locked: `set(tool_schema_subagent_types()) == policy_allowed_subagent_types()` at all times, verified by parametrized regression test sweeping the full enable/disable matrix.
- AST-pinned by Slice 2: drift on either site fails before merge.
- Synthesizer defaults derive from canonical accessors (`firewall.readonly_tool_whitelist()`) — operators editing the firewall whitelist propagate to free-form GENERAL with zero code changes.

**Sweep results:** 109/109 combined sweep across subagent_dynamic_linkage + subagent_graduation + subagent_dispatch_graduated + autonomy/subagent_types + canonical "all 81 invariants validate clean against main".

**Where O+V stands now:** Subagent delegation row flipped ⚠️ → ✅. 5 ⚠️ rows remain in the parity table:
- ⚠️ Plan replan-on-falsify (one-shot; needs evidence-of-falsification trigger)
- ⚠️ Skills/slash commands ecosystem (limited; no plugin manifest)
- ⚠️ /compact, /clear UX (auto-compaction exists; no operator surface)
- ⚠️ Time-travel debugging UI (Causality DAG + Replay substrate ready; no IDE consumer)
- ⚠️ NOTIFY_APPLY 5s diff preview (now superseded by InlinePromptGate but the table row predates it)

All 5 are surface/UI/incremental — none require fundamental architectural primitives.

**Why "free-form" reuses `dispatch_subagent` instead of a separate `task` tool:** A separate `task` tool would duplicate the existing dispatch substrate (Semantic Firewall, ScopedToolBackend, worktree isolation, SubagentOrchestrator). Naming a second tool wouldn't change semantics — `dispatch_subagent(type="general", goal="...")` IS the free-form Task call by construction once the synthesizer bridges the Venom args into `general_invocation`. The "free-form" property comes from the `goal` field already being unstructured prose; the type-widening + synthesizer just exposes that.

**How to apply (operator-facing):** Model writes `dispatch_subagent(type="general", goal="<arbitrary task description>", operation_scope=["backend/foo.py"], invocation_reason="<one-line>")`. Synthesizer fills missing boundary fields conservatively (read-only scope from `target_files` if `operation_scope` empty, `max_mutations=0`, `allowed_tools` from canonical read-only whitelist, `invocation_reason` from `goal[:200]`). Semantic Firewall §5 enforces at dispatch time — model receives actionable structured rejection if any boundary is insufficient (e.g., SAFE_AUTO parent → "risk_tier" rejection).

**Commits:** `16f7114eba` (Slice 1 dynamic linkage + synthesis) → `db55c12897` (Slice 2 graduation + AST pins).
