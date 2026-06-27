---
title: Project Session History Browser Ext
modules: [scripts/livefire_session_browser_ext.py, backend/core/ouroboros/governance/session_diff.py, backend/core/ouroboros/governance/session_stream_bridge.py, backend/core/ouroboros/governance/session_browser.py, backend/core/ouroboros/governance/ide_observability.py, backend/core/ouroboros/governance/ide_observability_stream.py, tests/governance/test_session_diff.py]
status: merged
source: project_session_history_browser_ext.md
---

Session History Browser extension arc — CLOSED 2026-04-21 (5-slice arc).
Layers four operator-confidence features on top of the base Session
Browser arc that closed the same day.

**What shipped:**
- Slice 1: `session_diff.py` — pure `diff_records()` + frozen `SessionDiff` +
  `render_session_diff()`. Numeric deltas for 7 fields + regression
  classification (higher_is_better rule table for ops counts vs cost).
  Parse-error suppresses classification. Schema `session_diff.v1`. 17 tests.
- Slice 2: `Bookmark.pinned` field (backward-compat load),
  `BookmarkStore.pin/unpin/list_pinned/is_pinned/on_change`,
  `SessionBrowser.pin/unpin/is_pinned/list_pinned_with_records/diff`,
  REPL verbs `/session diff|pin|unpin|pinned` + `--pinned` flag, default-entry
  surfaces pinned block before recent block. 36 tests.
- Slice 3: `session_stream_bridge.py` with three bridges
  (`bridge_session_index_to_broker` / `bridge_bookmark_store_to_broker` /
  `bridge_session_browser_to_broker`). 6 new event types added to
  `_VALID_EVENT_TYPES` in `ide_observability_stream.py`:
  `session_added`, `session_rescan`, `session_bookmarked`,
  `session_unbookmarked`, `session_pinned`, `session_unpinned`.
  Session id stuffed into `op_id` slot so `?op_id=<session-id>` filter works.
  Authority-free (lives outside session_browser, mirrors plan-approval
  bridge pattern). Schema `session_stream_bridge.v1`. 16 tests.
- Slice 4: `GET /observability/sessions` + `GET /observability/sessions/<id>`
  routes on existing `IDEObservabilityRouter`. Filter query params:
  `ok` / `bookmarked` / `pinned` / `has_replay` / `parse_error` / `prefix` /
  `limit (1..1000)`. Projection extends `SessionRecord.project()` with
  `bookmarked` / `pinned` / `bookmark_note` / `bookmark_ts` overlay. Same
  deny/loopback/rate-limit/CORS discipline as tasks+plans routes. Health
  surface string updated: `"tasks,plans,sessions"`. 26 tests.
- Slice 5: `test_session_browser_ext_graduation.py` (17 pins) +
  `scripts/livefire_session_browser_ext.py` (10 scenarios, 41 checks).

**Schema versions pinned:** `session_diff.v1`, `session_stream_bridge.v1`.

**§1 invariant (grep-enforced):** `session_diff.py` + `session_stream_bridge.py`
import zero of orchestrator / policy_engine / iron_gate / risk_tier_floor /
semantic_guardian / tool_executor / candidate_generator / change_engine.

**Why:** Closes the UX gap from the first-pass Session History Browser —
operator cannot compare runs (diff), cannot promote a run above chronological
noise (pin), cannot see runs land without polling (SSE bridge), and cannot
integrate session history into IDE views (GET endpoint).

**How to apply:** Additive — existing arcs + env flags unchanged. New
operator verbs (`diff` / `pin` / `unpin` / `pinned`) default-on. New endpoint
gated by existing `JARVIS_IDE_OBSERVABILITY_ENABLED` (default `true`,
graduated 2026-04-20). SSE events piggyback on existing `JARVIS_IDE_STREAM_ENABLED`
(default `true`).

**Landmines resolved:**
- Health surface string was `"tasks"` even post-Plan-Approval (latent
  inconsistency). Updated to `"tasks,plans,sessions"` and the base test
  pin relaxed to substring `in` so future domains don't need to update
  this assertion.
- `Bookmark` dataclass is frozen — `pin()` must construct a fresh
  instance; cannot mutate existing.
- Legacy bookmark JSON (no `pinned` key) must deserialize cleanly:
  `_load()` uses `item.get("pinned", False)`.
- Session id regex includes `:` and `.` (timestamp format). `_SESSION_ID_RE`
  added to `ide_observability.py` because the existing `_OP_ID_RE`
  rejects them.
- Bridge is push-only — broker never mutates index. Pinned by scenario 9
  (authority grep) + scenario in `test_session_stream_bridge.py`.
- Rescan "new_or_updated" list clipped to 32 entries in SSE payload
  to keep frames bounded; `new_or_updated_overflow: true` flags truncation.

**Files shipped:**
- `backend/core/ouroboros/governance/session_diff.py` (new)
- `backend/core/ouroboros/governance/session_stream_bridge.py` (new)
- `backend/core/ouroboros/governance/session_browser.py` (extended — pin/unpin,
  on_change, diff, list_pinned_with_records, REPL verbs)
- `backend/core/ouroboros/governance/ide_observability.py` (extended —
  /observability/sessions{,/<id>} handlers, surface string)
- `backend/core/ouroboros/governance/ide_observability_stream.py` (6 new
  event type constants + admitted to `_VALID_EVENT_TYPES`)
- `tests/governance/test_session_diff.py`,
  `test_session_browser_pinned.py`, `test_session_stream_bridge.py`,
  `test_session_observability.py`, `test_session_browser_ext_graduation.py`
- `scripts/livefire_session_browser_ext.py`

**Test tally:** 112 arc-specific tests + 17 graduation pins = 129 tests
green; 41 live-fire checks across 10 scenarios. 319 tests green across
session arc + extension + touched modules (no regressions in
ide_observability_stream or base session_browser).
