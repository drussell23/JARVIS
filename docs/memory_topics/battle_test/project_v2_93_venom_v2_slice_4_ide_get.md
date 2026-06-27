---
title: Project V2 93 Venom V2 Slice 4 Ide Get
modules: [tests/governance/test_ide_observability_tool_permissions.py, backend/core/ouroboros/governance/ide_observability.py, tests/governance/test_ide_observability.py, tests/governance/test_permission_decision_archive.py, tests/governance/test_tool_permissions_repl.py, tests/governance/test_permission_decision_sse.py]
status: merged
source: project_v2_93_venom_v2_slice_4_ide_get.md
---

May 10 2026: Slice 4 of the Venom V2 observability arc. Forward-additive on v2.89 (substrate) + v2.90 (REPL verb) + v2.91 (SSE event). Completes the full operator-visibility stack: ring (history) + REPL (operator query) + SSE (real-time push) + IDE GET (browseable surface).

**Slice scope** — IDE GET endpoints. Slice 5 (FlagRegistry seed) + Slice 6 (PRD §37 row flip) remain.

**Architectural composition**:

1. **Three new routes** registered on the canonical `IDEObservabilityRouter` in `register_routes()`:
   - `GET /observability/tool-permissions[?limit=N]` — most-recent N decisions (default 20, max 200)
   - `GET /observability/tool-permissions/by-tool/{tool_name}[?limit=N]` — exact-match filter on tool_name
   - `GET /observability/tool-permissions/{op_id}[?limit=N]` — exact-match filter on op_id (default 100)

2. **Route-order discipline (AST-pinned)** — the `/by-tool/{tool_name}` route MUST register BEFORE the generic `/{op_id}` route. aiohttp matches routes in registration order, and the generic op_id pattern would otherwise capture the literal string `by-tool` as an op_id value. AST pin in `test_route_order_specific_before_parameterized` asserts the source-code positional invariant via byte-index comparison.

3. **Dual master-flag gating** (no parallel knob):
   - `JARVIS_IDE_OBSERVABILITY_ENABLED` (graduated default-TRUE 2026-04-20 Gap #6 Slice 4) → 403 when off (port-scanner discipline)
   - `JARVIS_PERMISSION_ARCHIVE_ENABLED` (default-FALSE per §33.1) → 403 when off (archive owns its surface)
   - Composed via new `_permission_archive_master_enabled()` static helper that defers to canonical `permission_archive_enabled()` from substrate. Mirrors the `_posture_master_enabled()` pattern from v2.84.

4. **Three handler methods** mirror the canonical posture-health handler shape:
   - `_handle_tool_permissions_recent` — composes `get_default_archive().recent(limit)` + `snapshot().to_dict()`
   - `_handle_tool_permissions_by_tool` — composes `get_default_archive().by_tool(tool_name, limit)`
   - `_handle_tool_permissions_by_op` — composes `get_default_archive().by_op(op_id, limit)`
   - All three: 403 on either master-off, 429 on rate limit, 400 on missing path parameter, 503 on substrate import failure (graceful degradation), 200 + canonical projection on happy path

5. **Query-param parsing** — new `_parse_limit(request, default, ceiling)` static helper extracts `?limit=N` with bounds clamping (floor 1, env-tunable ceiling). Falls through to defaults on parse failure. NEVER raises.

6. **Payload schema** — composes the canonical `DecisionRecord.to_dict()` projection:
   ```json
   {
     "schema_version": "1.0",
     "count": 17,
     "snapshot": {"capacity": 50, "size": 17, "next_seq": 18, "utilization": 0.34, "schema_version": "permission_decision_archive.v1"},
     "records": [
       {"ref": "p-17", "op_id": "...", "tool_name": "read_file",
        "decision_value": "allow",
        "decision": { ... canonical AggregatePermissionDecision projection ... },
        "inserted_at": 12345.678, "schema_version": "permission_decision_archive.v1"},
       ...
     ]
   }
   ```
   - IDE consumers can correlate the event to a `/expand p-N` retrieval via the canonical ref
   - Inner `decision` field carries the canonical `tool_permission.1` schema_version — forward-compatible with future tool_permission schema bumps

7. **Surface field** in `/observability/health` extended with `tool-permissions` — IDE clients feature-detect against this comma-separated list.

8. **Authority asymmetry preserved (AST-pinned)** — the IDE module imports ONLY `get_default_archive` + `permission_archive_enabled` from the substrate. Does NOT import `BoundedDecisionArchive` (ring constructor), `DecisionRecord` (frozen dataclass), or `ArchiveSnapshot` (projection). Composes only the `.to_dict()` projection contract — preserves §33.5 frozen-artifact discipline + forward-compat with future schema bumps. AST walker test verifies via `ast.ImportFrom` inspection (not source-string grep — docstring mentions are fine).

**15 regression tests** in `tests/governance/test_ide_observability_tool_permissions.py`:
- Route registration (3 tests: all 3 paths registered / route-order specific-before-parameterized AST-pinned / surface field advertises tool-permissions)
- Gate enforcement (4 tests: 403 when IDE off / 403 when archive off / 400 on missing tool_name / 400 on missing op_id)
- Happy path (5 tests: empty archive returns 200 with empty records / recent returns newest-first / limit query param respected / limit clamped to max / by_tool exact match / by_op exact match)
- Authority asymmetry (2 tests: read-only contract — handlers do not mutate archive / does not import policy substrate types)
- 1 route-order positional AST pin (byte-index comparison in source — drift fails the build)

**Read-only contract** (load-bearing): the `test_handlers_do_not_mutate_archive` test records a baseline `snapshot().to_dict()`, hits all 3 routes, and asserts the archive's snapshot is byte-identical post-call. Drift here is the operator-visible signal that someone added a mutating side effect.

**Broader regression**: 129 tests green across `test_ide_observability.py` + `test_ide_observability_tool_permissions.py` + `test_permission_decision_archive.py` (Slice 1) + `test_tool_permissions_repl.py` (Slice 2) + `test_permission_decision_sse.py` (Slice 3). **Zero regression** on the canonical Gap #6 GET surface or on existing observability handlers.

**Files modified**:
- `backend/core/ouroboros/governance/ide_observability.py` (3 substantive blocks):
  - `register_routes` (~line 405): added 3 route entries with route-order positional invariant comment
  - `_handle_health` (~line 514): surface field extended with `tool-permissions`
  - new `_permission_archive_master_enabled()` + `_parse_limit()` helpers + 3 handler methods (~line 2956)
- `tests/governance/test_ide_observability_tool_permissions.py` (NEW, 15 regression tests + 2 AST pins)

**Cumulative Venom V2 arc state** (4 slices shipped):
- Slice 1 (v2.89) — substrate ring + producer-bridge + tool_executor seam (31 tests)
- Slice 2 (v2.90) — REPL verb auto-discovered + /expand p-N 5th cross-substrate prefix (31 tests)
- Slice 3 (v2.91) — SSE event registration + producer-bridge to broker (11 tests)
- Slice 4 (v2.93) — IDE GET endpoints + route-order discipline (15 tests)
- Total: 88 new regression tests + 19 AST pins across the 4 slices

**Operator binding 2026-05-10 satisfied verbatim**:
- Solved root problem directly — IDE consumers need browseable HTTP surface; routes compose existing canonical substrate + canonical IDEObservabilityRouter
- No workarounds — used canonical posture-health handler pattern from v2.84 (mirrors `_posture_master_enabled` static + `_handle_posture_health` shape exactly)
- No shortcuts — 15 tests + 2 AST pins; route-order positional invariant byte-pinned in source via `index()` comparison
- Composes existing canonical paths: `IDEObservabilityRouter` (single router class), `_json_response` (schema-versioned + CORS + Cache-Control: no-store), `_error_response` (port-scanner discipline 403/429), `_check_rate_limit` (sliding window quota), `ide_observability_enabled` (master flag), `get_default_archive` (substrate singleton), `DecisionRecord.to_dict` (canonical projection)
- No hardcoding — limit query param env-tunable (floor 1, ceiling 200); recent default 20, by-op default 100; all surfaceable as future env knobs if needed
- No duplication — IDE GET is a thin projection layer over the substrate ring; zero new state machinery; zero parallel taxonomy

**Slice 4 finishes the §8 absolute-observability stack for Venom permission decisions**:
- v2.89 ring: every decision archived with stable `p-N` ref
- v2.90 REPL: operator queries recent / by-tool / by-op via `/tool_permissions`
- v2.91 SSE: real-time push to IDE consumers via `permission_decision_recorded` event
- **v2.93 GET: browseable HTTP surface for VS Code extension + cron-fired audit scripts + Phase 9 ladder forensics**

The full observability triad (history + real-time + browseable) is now operational. Slices 5 (FlagRegistry seed) + 6 (PRD row flip) are housekeeping; the load-bearing operator-visibility work is COMPLETE.

**Why this matters for RSI**: when O+V's proactive curiosity loop scales (e.g., the post-arc soak's "code_gen::.py import_error" intent firing real ops, not just BG-route no-ops), every ALLOW/DENY/ASK/DEFER decision becomes browseable in real time via the IDE extension. Operators can answer "what is the system choosing not to do" without log archaeology. This is the load-bearing pre-requisite for unattended autonomous-development cycles where the operator reviews Venom V2 decisions asynchronously.
