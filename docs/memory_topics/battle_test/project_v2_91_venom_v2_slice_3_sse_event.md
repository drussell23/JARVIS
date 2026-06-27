---
title: Project V2 91 Venom V2 Slice 3 Sse Event
modules: [tests/governance/test_permission_decision_sse.py, backend/core/ouroboros/governance/ide_observability_stream.py, backend/core/ouroboros/governance/permission_decision_archive.py, tests/governance/test_ide_observability_stream.py, tests/governance/test_section_37_tier1_2_posture_health_wiring.py, tests/governance/test_permission_decision_archive.py, tests/governance/test_tool_permissions_repl.py]
status: merged
source: project_v2_91_venom_v2_slice_3_sse_event.md
---

May 10 2026: Slice 3 of the Venom V2 observability arc. Forward-additive on v2.89 substrate + v2.90 REPL.

**Slice scope** — SSE producer-bridge for the canonical IDE event stream. Surfaces 4-6 (IDE GET endpoint / FlagRegistry seed / PRD §37 Tier 2 #6 row flip) deferred to follow-on slices.

**Architectural composition**:

1. **Event-type registration** in `ide_observability_stream.py` (the canonical broker substrate from Gap #6 Slice 2 + v2.84 sibling pattern):
   - New `EVENT_TYPE_PERMISSION_DECISION_RECORDED = "permission_decision_recorded"` constant defined alongside existing event-type constants
   - Added to the `_VALID_EVENT_TYPES` frozenset — load-bearing because the broker's pre-publish validation rejects any event type NOT in this set (mirrors the v2.84 fix where POSTURE_OBSERVER_DEGRADED was defined locally but missing from the frozenset → silent publish-rejection)
   - Comment block cites Slice 3 + PRD v2.91 + master-flag interaction

2. **Producer-bridge §33.2** in `permission_decision_archive.maybe_record_decision`:
   - After `archive.record()` returns a non-None record, attempt to publish the canonical `record.to_dict()` projection via the existing `publish_task_event(event_type, op_id, payload)` bridge
   - Composes the existing canonical bridge — ZERO parallel publish path, ZERO direct broker-coupling
   - **Positional invariant** (AST-pinned): the publish call MUST come AFTER `archive.record()` — load-bearing because publishing before would emit a stale ref / phantom record
   - **Defensive try/except** (AST-pinned): broker exceptions MUST NOT propagate into the policy substrate; the policy path is fail-silent by contract

3. **Dual-master-flag composition** (no parallel knob):
   - Archive master flag `JARVIS_PERMISSION_ARCHIVE_ENABLED` (default-FALSE per §33.1) gates `record()` first → archive-disabled means no record + no publish
   - Stream master flag `JARVIS_IDE_STREAM_ENABLED` (graduated default-TRUE 2026-04-20 Gap #6 Slice 2) gates `publish_task_event` second → stream-disabled means record happens but publish is no-op
   - Both flags compose deterministically: `record_attempted = archive_enabled`, `publish_attempted = (archive_enabled AND record_succeeded)`, `publish_observed = (publish_attempted AND stream_enabled)`

4. **Payload schema** — composes the canonical `DecisionRecord.to_dict()` projection:
   - `ref` (`p-N`)
   - `op_id` / `tool_name` / `decision_value` (allow/deny/ask/defer)
   - `decision` (full `AggregatePermissionDecision.to_dict()` projection — schema_version `tool_permission.1`, detail, deny_callbacks, ask_callbacks, total_callbacks)
   - `inserted_at` / `schema_version` (`permission_decision_archive.v1`)
   - IDE consumers can correlate the event to a `/expand p-N` retrieval via the canonical ref

**Tests**: 11 regression tests in `tests/governance/test_permission_decision_sse.py`:
- 2 event-type registration pins (canonical literal value + presence in `_VALID_EVENT_TYPES` frozenset)
- 4 producer-bridge contract tests (publish fires when both gates ON / silenced when stream OFF / silenced when archive OFF / payload carries canonical projection with deny + detail + schema_version)
- 1 best-effort contract test (broker exception via mocked `publish_task_event.side_effect` MUST NOT propagate; archive write still succeeds)
- **4 AST pins**:
  - `EVENT_TYPE_PERMISSION_DECISION_RECORDED` constant present at module scope with canonical literal
  - constant appears in the `_VALID_EVENT_TYPES` frozenset literal (drift here is the same silent-drop failure mode v2.84 fixed)
  - `maybe_record_decision` invokes `publish_task_event` AFTER `archive.record()` (positional invariant)
  - publish call wrapped in try/except (best-effort contract enforced structurally)

**Broader regression**: 137 governance tests green across 5 files (`test_ide_observability_stream.py` + `test_section_37_tier1_2_posture_health_wiring.py` + `test_permission_decision_archive.py` Slice 1 + `test_tool_permissions_repl.py` Slice 2 + `test_permission_decision_sse.py` Slice 3). **Zero regression** on the canonical broker substrate or on v2.84's POSTURE_OBSERVER_DEGRADED path (which is the canonical pattern v2.91 follows).

**Operator binding 2026-05-10 satisfied verbatim**:
- Solved root problem directly — IDE consumers need real-time decision events; the producer-bridge fires once per archived decision
- No workarounds — composed canonical `publish_task_event` bridge + canonical `_VALID_EVENT_TYPES` frozenset (mirrors v2.84 POSTURE_OBSERVER_DEGRADED canonical pattern)
- No shortcuts — 11 tests + 4 AST pins (positional invariant pinned via byte-equal index comparison)
- Composes existing canonical paths: `ide_observability_stream.publish_task_event` (zero parallel publish), `_VALID_EVENT_TYPES` frozenset (zero parallel registration), `DecisionRecord.to_dict()` projection (zero parallel payload schema)
- No hardcoding — event-type literal exposed as public constant; payload schema deferred to canonical projection
- No duplication — bridge is a single fire-after-record call; no SSE-specific state machinery; no parallel master flag

**Files**:
- `backend/core/ouroboros/governance/ide_observability_stream.py` (+18 LOC: constant declaration + comment block + frozenset entry)
- `backend/core/ouroboros/governance/permission_decision_archive.py` (+22 LOC inside existing `maybe_record_decision` body: defensive try/except wrapping `publish_task_event` call after the archive write succeeds)
- `tests/governance/test_permission_decision_sse.py` (NEW, 11 tests + 4 AST pins)

**Cumulative Venom V2 arc state** (3 slices shipped tonight):
- Slice 1 (v2.89) — substrate ring + producer-bridge + tool_executor seam wiring (31 tests)
- Slice 2 (v2.90) — REPL verb auto-discovered + /expand p-N 5th cross-substrate prefix (31 tests)
- Slice 3 (v2.91) — SSE event registration + producer-bridge to broker (11 tests)
- Total: 73 new regression tests + 16 AST pins across the 3 slices; 137 broader regression green

**Surfaces still deferred**:
- Slice 4: IDE GET `/observability/tool-permissions` (~1.5h, composes `IDEObservabilityRouter` pattern from v2.84's `/observability/posture/health`)
- Slice 5: FlagRegistry seed for `JARVIS_PERMISSION_ARCHIVE_ENABLED` + `JARVIS_PERMISSION_ARCHIVE_SIZE` (~30min)
- Slice 6: PRD §37 Tier 2 #6 row update + §3.6.3 row 🟡 → ✅ (~30min)

Each is forward-compatible with what's shipped. Each can land independently when operator chooses.

**Why this matters for RSI**: the SSE event makes Venom permission decisions **observable in real time** — when O+V's proactive curiosity loop starts firing tool calls at scale (e.g., the post-arc soak's "code_gen::.py import_error" intent), operators see every ALLOW/DENY/ASK/DEFER event in the IDE stream as it lands. Without this, the policy boundary between proactive ops and operator authority is invisible at runtime. The combination of v2.89's ring (history) + v2.90's REPL (operator query) + v2.91's SSE (real-time push) gives the full observability stack — exactly the §8 absolute-observability mandate from the manifesto.
