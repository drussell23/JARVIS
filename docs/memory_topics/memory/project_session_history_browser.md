---
title: Project Session History Browser
modules: [scripts/livefire_session_browser.py, backend/core/ouroboros/governance/session_record.py, backend/core/ouroboros/governance/session_browser.py, tests/governance/test_session_graduation.py]
status: historical
source: project_session_history_browser.md
---

Session History Browser — CLOSED 2026-04-21 (5-slice arc).

**What shipped:**
- Slice 1: `session_record.py` — frozen `SessionRecord` + `parse_session_dir()` fail-closed parser (summary.json + debug.log head). 36 tests.
- Slices 2+3+4: `session_browser.py` — `SessionIndex` (mtime-cached scan + multi-predicate filter + listener hooks), `BookmarkStore` (JSON-backed at `<root>/session_bookmarks.json`, NEVER touches session dirs), `SessionBrowser` (glue), `dispatch_session_command()` REPL (`/session list|show|recent|bookmark|unbookmark|bookmarks|replay|rescan|help` with `--ok/--bad/--has-replay/--parse-error/--limit N/--prefix=`). 74 tests.
- Slice 5: `test_session_graduation.py` (7 pins: authority invariant grep, §1 read-only pin, schema-version pins, docstring bit-rot, determinism) + `scripts/livefire_session_browser.py` (10 scenarios, 44 checks).

**Schema versions pinned:** `SESSION_RECORD_SCHEMA_VERSION = "session_record.v1"`, `SESSION_BROWSER_SCHEMA_VERSION = "session_browser.v1"`.

**§1 invariant (grep-enforced):** arc modules import none of `orchestrator`/`policy_engine`/`iron_gate`/`risk_tier_floor`/`semantic_guardian`/`tool_executor`/`candidate_generator`/`change_engine`. Browser NEVER writes into session dirs — bookmarks live at a separate root.

**Why:** Operator has session replay HTML but no way to navigate across runs. Closes CC-parity gap "no /session list."

**How to apply:** Purely additive, default-on, read-only. No env flag needed — arc is operator-facing only (no authority implications). Future extensions (remote session mirroring, diff across sessions) continue under Slice-N pattern with authority invariant preserved.

**Landmines resolved:**
- Singleton deadlock: `_singleton_lock` must be `threading.RLock()` (not `Lock()`) — `get_default_session_browser()` calls `get_default_session_index()` + `get_default_bookmark_store()` which re-acquire the lock.
- ISO-second sort ambiguity: bookmarks added within same second get undefined order — tests assert set-equality, not strict order.
- `replay_html_path()` + `/session replay` must rescan before lookup — tests/REPL that create sessions fresh won't see them otherwise.

**Commits:** `d498b0a8c3` (Slices 2+3+4), `56a591565c` (RLock fix), `aa6c32d2c5` (Slice 5 + live-fire).

**Test tally:** 117 arc tests + 44 live-fire checks green. 10/10 scenarios PASS.
