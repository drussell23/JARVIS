---
title: Asyncio Audit Phase 1 — CLOSED 2026-05-03
modules: [backend/core/ouroboros/battle_test/harness.py]
status: historical
source: project_asyncio_audit_phase1.md
---

# Asyncio Audit Phase 1 — CLOSED 2026-05-03

## What & why

Audit of `ensure_future`/`create_task` spawn sites across O+V codebase
returned 182 sites OUTSIDE candidate_generator (which Defect #4 already
covered). Per-callsite retrofit at that scale is impractical and brittle.

Structural fix: install a single asyncio loop-level exception handler at
harness boot (`harness.py:run()`). Active for the entire session
lifetime, before anything spawns and after everything completes.

## What it does

- Replaces asyncio's default handler ("Unhandled exception in event loop:")
- Replaces prompt_toolkit's handler ("Press ENTER to continue...")
- Routes every leaked exception through `logging.getLogger("asyncio.leak")`
- Classifies via the `_EXPECTED_BACKGROUND_EXC_PATTERNS` tuple from
  candidate_generator (single source of truth — no duplication):
  - `CancelledError` → DEBUG
  - matches expected pattern → DEBUG
  - everything else → WARNING with full traceback

## Files touched

- `backend/core/ouroboros/battle_test/harness.py` — handler installed in
  `run()` before any sub-system boots. Reuses `_EXPECTED_BACKGROUND_EXC_PATTERNS`
  via lazy import.

## Validation surface

The new soak-v6 V6-B criterion asserts `WARNING [asyncio leak]` count == 0
in debug.log. Any WARNING entry surfaces a leaked exception class that
bypasses every per-callsite swallower — concrete audit follow-up target.

## What this doesn't fix

- The 182 spawn sites still lack per-callsite `add_done_callback`
  consumers. The loop handler catches them but a clean architecture
  would also have local consumers (DEBUG-classified, silent on expected).
  Phase 2 of the audit (deferred) would categorize the 182 sites by risk
  and retrofit the high-risk ones (provider/orchestrator/cancellation).
- The set_exception_handler=False parameter on `prompt_async` is still
  needed in SerpentREPL._loop so prompt_toolkit doesn't override the
  harness handler each prompt cycle.

## Reuse contract honored

- Single source of truth for "expected leaked exceptions"
  (`_EXPECTED_BACKGROUND_EXC_PATTERNS`)
- No new exception class, no new logger module, no new config knobs
- Per-callsite Defect #4 callbacks remain valuable + functional
- Mirrors existing `loop.set_exception_handler` idiom (same as
  asyncpg's _testbase pattern)
