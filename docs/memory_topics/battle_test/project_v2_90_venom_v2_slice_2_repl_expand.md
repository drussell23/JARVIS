---
title: Project V2 90 Venom V2 Slice 2 Repl Expand
modules: [tests/governance/test_tool_permissions_repl.py, backend/core/ouroboros/governance/tool_permissions_repl.py, backend/core/ouroboros/battle_test/serpent_flow.py, backend/core/ouroboros/governance/permission_decision_archive.py, backend/core/ouroboros/governance/decisions_repl.py, tests/governance/test_venom_v2_slice_1_tool_permission.py, tests/governance/test_section_31_u2_slice4_repl_observability.py, tests/governance/test_section_37_tier2_10_replay_repl.py, tests/governance/test_slice_5b_e_repls.py, tests/governance/test_repl_dispatch_registry.py, backend/core/ouroboros/battle_test/repl_dispatch_registry.py]
status: historical
source: project_v2_90_venom_v2_slice_2_repl_expand.md
---

May 10 2026: Slice 2 of the Venom V2 observability arc. Builds on v2.89 substrate (`permission_decision_archive.py`).

**Slice scope** — operator-facing surfaces (REPL verb + /expand integration). Surfaces 3-5 (SSE event / IDE GET endpoint / FlagRegistry seed) deferred to follow-on slices.

**Architectural composition**:

1. **REPL verb auto-discovery via §33.3 naming-cage**: new file `tool_permissions_repl.py` (~370 LOC) mirrors the canonical `decisions_repl.py` pattern exactly. Filename basename `tool_permissions_repl` → verb `tool_permissions` → registered automatically by `repl_dispatch_registry.prime_registry()` — ZERO edits to `serpent_flow.py`'s dispatch ladder. Verified end-to-end: registry now lists 49 verbs (was 48 pre-slice); `try_dispatch('/tool_permissions help')` returns matched=True+ok=True.

2. **Subcommand vocabulary** mirrors `decisions_repl` pattern:
   - `/tool_permissions` — alias for `/tool_permissions recent`
   - `/tool_permissions recent [N]` — most-recent N decisions (default 20, max 200)
   - `/tool_permissions tool <name>` — exact-match filter on tool_name
   - `/tool_permissions op <op_id>` — exact-match filter on op_id
   - `/tool_permissions stats` — archive snapshot (capacity / size / utilization)
   - `/tool_permissions help` — usage (always available; bypasses master gate)
   - Unknown subcommands → friendly error pointing at help

3. **Master-flag composition** (no parallel flag): local `_master_enabled()` defers to canonical `permission_archive_enabled()` from substrate. AST-pinned. When master is off, every subcommand except `help` returns disabled-notice pointing at `JARVIS_PERMISSION_ARCHIVE_ENABLED` env var. Help bypasses gate per discoverability invariant.

4. **Authority asymmetry preserved (AST-pinned)**: REPL imports `permission_decision_archive` ONLY (read-only consumer). Forbidden imports: `compute_permission_decision` / `evaluate_tool_permission` / `PermissionRegistry` / `ToolPermissionCallback` (the substrate's policy code). Verified by AST sweep test.

5. **Frozen `ToolPermissionsReplDispatchResult`** mirrors `DecisionsReplDispatchResult` shape: `ok: bool` + `text: str` + `matched: bool=True`. `matched=False` signals non-matching line for caller-side routing.

6. **`/expand p-N` cross-substrate integration** at `serpent_flow._handle_expand`:
   - Added `elif ref_or_op.startswith("p-"):` branch routing to new `_expand_permission_decision(ref)` method
   - New method composes canonical `permission_decision_archive.get_default_archive()` lookup (no parallel state)
   - Renders `⏺ Permission` header + tool/op/decision metadata + canonical `AggregatePermissionDecision.to_dict()` projection (detail / deny_callbacks / ask_callbacks / total_callbacks)
   - Reaches into projection via `proj.get(...)` duck-typing — preserves §33.5 frozen-artifact contract; future tool_permission schema bump (decision.2) flows through without edits
   - Updated `_print_expand_summary` to surface `p-N` refs in a new "permissions:" section parallel to "tool bodies:" — load-bearing for operator discoverability
   - Updated `_handle_expand` docstring to document the 5th branch (operator-help accuracy)

**Test results**:
- **31 new regression tests** in `tests/governance/test_tool_permissions_repl.py`:
  - Match contract (10 parametrized cases — exact verb / similar-but-different verb collision-avoidance with `/permissions`)
  - Master-flag gate (help bypasses / recent returns disabled-notice when master off)
  - Subcommand behaviours (recent empty / recent newest-first / recent respects limit / tool filter exact-match / tool missing arg / op filter exact-match / op missing arg / stats / unknown subcommand / bare invocation aliases)
  - **4 AST pins**: no-policy-imports / dispatcher-function-present (load-bearing naming-cage hook) / canonical-master-flag-composition / help-text-documents-canonical-flag-names
  - **4 cross-substrate AST pins** in serpent_flow integration: /expand-branches-p-prefix / _expand_permission_decision-method-present / _print_expand_summary-includes-perm-recent / _handle_expand-docstring-documents-p-prefix
- **62 own tests green** (Slice 1 substrate + Slice 2 surfaces)
- **314 broader regression green** — zero impact on `test_venom_v2_slice_1_tool_permission.py` / `test_section_31_u2_slice4_repl_observability.py` / `test_section_37_tier2_10_replay_repl.py` / `test_slice_5b_e_repls.py` / `test_repl_dispatch_registry.py`

**End-to-end verification under live registry**:
```python
from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
    try_dispatch, prime_registry, list_verbs,
)
prime_registry()
assert 'tool_permissions' in list_verbs()  # 49 total verbs
result = try_dispatch('/tool_permissions help')
assert result.matched and result.ok
```

**Operator binding 2026-05-10 satisfied verbatim**:
- Solved root problem directly — operator-facing query surface IS the load-bearing operator-visibility fact; auto-discovery + cross-substrate /expand integration delivers the surface
- No workarounds — used canonical `decisions_repl.py` template + canonical `_expand_*` method shape; zero parallel naming-cage / dispatch ladder
- No shortcuts — 31 new tests + 4 AST pins for load-bearing structural invariants + 4 cross-substrate AST pins
- Composes existing canonical paths: `decisions_repl.py` pattern (REPL verb shape), `repl_dispatch_registry.py` §33.3 auto-discovery (zero-edit), `_handle_expand` cross-substrate dispatcher (5th prefix), `_print_expand_summary` ref-listing (new section), canonical `permission_archive_enabled()` master flag (no parallel)
- No hardcoding — capacity surface via `snapshot()` snapshot; subcommand vocabulary documented in help text
- No duplication — REPL is a thin read-only consumer of substrate; zero policy logic; zero parallel taxonomy; zero parallel state

**Files**:
- `backend/core/ouroboros/governance/tool_permissions_repl.py` (NEW, ~370 LOC)
- `backend/core/ouroboros/battle_test/serpent_flow.py` (+93 LOC: docstring extended with p-N branch / `elif p-` branch in `_handle_expand` / new `_expand_permission_decision` method / `_print_expand_summary` extended with permissions section)
- `tests/governance/test_tool_permissions_repl.py` (NEW, 31 tests)

**Why this composition discipline matters for RSI**:
- Adding new substrates to the cross-substrate `/expand` family without touching the dispatcher's source code (zero edits to `_handle_expand`'s call sites or registration) is exactly the §33.3 naming-cage discipline that makes O+V's REPL composable as new arcs land.
- The 5-prefix family (`t-N`/`d-N`/`o-N`/`n-N`/`p-N`) keeps growing additively — every operator-visible substrate joins via the same 4 hooks (`elif startswith` / `_expand_*` method / summary listing / docstring update). This linear-additive shape is the load-bearing pre-requisite for §39 Tier 6 cross-repo expansion when J-Prime + Reactor-Core arrive.

**Surfaces still deferred**:
- Slice 3: SSE event type `permission_decision_recorded` in `_VALID_EVENT_TYPES` + producer hook from archive (~1h)
- Slice 4: IDE GET `/observability/tool-permissions` (~1.5h, composes `IDEObservabilityRouter` pattern)
- Slice 5: FlagRegistry seed for `JARVIS_PERMISSION_ARCHIVE_ENABLED` + `JARVIS_PERMISSION_ARCHIVE_SIZE` (~30min)
- Slice 6: PRD §37 Tier 2 #6 row update + §3.6.3 row 🟡 → ✅ (~30min)

These are forward-compatible follow-on slices each ~30min-1.5h. Each can ship independently when operator chooses to graduate.
