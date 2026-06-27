---
title: Project Gap 6 Slice4 Graduation
modules: [tests/governance/test_ide_observability.py, tests/governance/test_ide_observability_stream.py]
status: merged
source: project_gap_6_slice4_graduation.md
---

## What shipped (2026-04-20) — CLOSES GAP #6

**Flag flips**:
- `JARVIS_IDE_OBSERVABILITY_ENABLED` — default `false` → **`true`**
- `JARVIS_IDE_STREAM_ENABLED` — default `false` → **`true`**

**Tests**: 90/90 Python tests green across both regression spines (41 obs + 49 stream, 18 new graduation pins).
**Extension tests**: 35/35 TypeScript tests still green.

## Slice 4 content

### 1. Server-side graduation

- `ide_observability_enabled()` and `stream_enabled()` default values flipped + docstrings rewritten to name the graduation date (2026-04-20) and identify `=false` as the runtime kill switch.
- Zero changes to the structural caps: loopback assert, rate limit, CORS allowlist, bounded broker (8×64×512), heartbeat cadence, authority-module import ban.

### 2. Graduation pins (18 new tests)

**10 pins on `test_ide_observability.py`**:
- `test_slice4_graduation_default_is_true` — anchor.
- `test_slice4_graduation_explicit_false_is_full_revert` — kill switch verified across all three handlers with schema_version + reason_code assertions.
- `test_slice4_graduation_authority_invariant_preserved` — grep re-run for 7 forbidden imports.
- `test_slice4_graduation_loopback_assert_still_strict` — accept/reject matrix.
- `test_slice4_graduation_rate_limit_still_enforced` — sliding-window cap.
- `test_slice4_graduation_cors_still_no_wildcard`.
- `test_slice4_graduation_malformed_op_id_still_400`.
- `test_slice4_graduation_docstring_references_graduation` — bit-rot guard.
- `test_slice4_graduation_full_revert_matrix` — single test covering on/unset/off.
- `test_slice4_graduation_cursor_compat_shape` — `vscode-webview://` regex still in allowlist.

**8 pins on `test_ide_observability_stream.py`**:
- `test_slice4_stream_graduation_default_is_true` — anchor.
- `test_slice4_stream_graduation_explicit_false_is_kill_switch` — 403 + publish_task_event no-op.
- `test_slice4_stream_graduation_authority_invariant_preserved` — grep re-run.
- `test_slice4_stream_graduation_bounds_unchanged` — 8×64×512 caps.
- `test_slice4_stream_graduation_unidirectional_transport` — MagicMock proves only `add_get` is called (no POST/PUT/DELETE/PATCH).
- `test_slice4_stream_graduation_docstring_references_graduation` — bit-rot guard.
- `test_slice4_stream_graduation_full_revert_matrix`.
- `test_slice4_stream_graduation_mounts_beside_slice1` — both enable-helpers agree under graduated defaults.

### 3. Two pre-existing tests renamed for post-graduation accuracy

- `test_ide_observability_disabled_by_default` → `test_ide_observability_default_post_graduation_is_true`
- `test_stream_disabled_by_default` → `test_stream_default_post_graduation_is_true`
- `test_health/tasks_list/task_detail_returns_403_when_disabled` → `..._when_explicitly_disabled` (now sets `=false` explicitly instead of relying on the old deny-by-default)
- `test_publish_task_event_silent_no_op_when_disabled` → `..._when_explicitly_disabled`

### 4. Cursor compat (zero code change)

- `package.json` version bumped to `0.2.0`, description updated to mention Cursor, keywords list added (`cursor`, `vscode`, `sse`, `readonly`, etc.).
- The existing CORS pattern `^vscode-webview://[a-z0-9-]+$` already matches Cursor's webview scheme (Cursor is a VS Code fork using the same extension host).
- No TypeScript changes required. Same `.vsix` installs in Cursor.

### 5. Sublime Text — DEFERRED

Different ecosystem (Python plugin SDK, no rich webview). Bundling it here would double scope and conflate graduation with a new client. Captured as a future slice.

## Authority invariants (survived graduation)

- No new imports of orchestrator / policy / iron_gate / risk_tier_floor / semantic_guardian / semantic_firewall / tool_executor in either module.
- Loopback assert still rejects `0.0.0.0`/`::`/wildcard.
- Rate limiter still sliding-window 120/min (obs) + 10/min (stream).
- CORS allowlist still no `*`, no `Access-Control-Allow-Credentials`.
- Stream route still registered as `add_get` only (MagicMock-pinned).
- Schema stamp `"1.0"` still on every response.
- Op_id regex still `^[A-Za-z0-9_\-]{1,128}$`.
- Bounded caps unchanged (`_max_subscribers=8`, `_queue_maxsize=64`, `_history_maxlen=512`).

## Full-revert matrix

Single env var per surface. `=false` → disabled (403 on every route, silent no-op on publish hooks). Unset → graduated default (enabled). `=true` → explicit enabled (same as unset).

## Closes Gap #6

Server-side observability surface complete. Future additive work:
- Slice 5 candidate: Sublime Text plugin (Python SDK, ~400 lines).
- Slice 6 candidate: JetBrains plugin (Kotlin/IntelliJ SDK, heavyweight).
- Slice 7 candidate: live-fire session end-to-end against a running battle-test to capture the full GET + SSE + Tree View + webview flow.
