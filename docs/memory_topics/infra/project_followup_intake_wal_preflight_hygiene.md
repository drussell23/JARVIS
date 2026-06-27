---
title: Project Followup Intake Wal Preflight Hygiene
modules: [scripts/ouroboros_battle_test.py]
status: historical
source: project_followup_intake_wal_preflight_hygiene.md
---

## Why this exists

F1 Slice 4 cadence S4 (`bt-2026-04-25-083724`):
- BacklogSensor scanned successfully, F2/F3 hint markers fired
- BacklogSensor called `router.ingest(envelope)` for the seed
- **Router returned non-"enqueued"** (silently — no log line fires on dedup result)
- Seed never entered the pipeline → 0 ops dispatched in 20 minutes
- `.jarvis/intake_wal.jsonl` was 1.3MB, last modified during S3's shutdown
- WAL contained the seed's `task_id: wave3-item6-forced-reach-multifile-seed` signature from prior sessions

Documented as "intake WAL cross-session coalescing (state cleanup)" in Session O memory. Has bitten ~5 prior cadences silently — operators noticed only when sessions failed to produce expected markers.

## Operator binding 2026-04-25

> "let's execute these steps 1-4 now so that we can resolve these root causes and super beef it up!"

Steps 1-4 (immediate cleanup) executed. **Step 5 (add WAL-clear to canonical pre-flight) tracked HERE as a separate follow-up** to prevent silent contamination on every future cadence.

## Scope

**Where**: `scripts/ouroboros_battle_test.py` — the harness's pre-flight section (the same place that already does `pgrep` zombie scan + lock cleanup).

**What to add**:
1. Detect `.jarvis/intake_wal.jsonl` size > some threshold (e.g., 100KB)
2. If detected: backup with timestamped filename, then truncate
3. Log: `[BattleTest] Pre-flight: cleared stale intake WAL (size=X bytes, backup=Y)`
4. Optional: clear stale `intake_router*.lock` files older than N hours

**Master switch**: `JARVIS_BATTLE_PRE_FLIGHT_WAL_CLEAR=true` (default `true` — this is hygiene that should always run for graduation cadences). Operators can disable for forensic preservation if needed.

## Non-goals

- Don't change the WAL format itself
- Don't change `UnifiedIntakeRouter`'s dedup logic (it's correct — same-session dedup IS desired)
- Don't auto-clear during normal Ouroboros operation (only at battle-test harness boot)
- Don't delete the WAL — backup first, then truncate

## Why we don't fix this in `UnifiedIntakeRouter`

The router's WAL-based dedup is INTENTIONAL for same-session deduplication (prevents storms). The bug is purely cross-session: when a battle-test session ends and the next one starts, the WAL persists. The right fix is at the harness boundary (pre-flight cleanup), not at the router (which has correct semantics).

## Slices (when authorized)

### Slice 1 — primitive: `_pre_flight_wal_clear()` helper
- Add to `scripts/ouroboros_battle_test.py`
- Defaults: threshold=100KB, master flag default-on
- Tests: tmp WAL > threshold → backed up + truncated; tmp WAL < threshold → untouched

### Slice 2 — wire into harness pre-flight + graduation cadence
- Call from existing pre-flight (alongside zombie scan + lock cleanup)
- Update canonical recipe in `memory/project_wave3_item6_graduation_matrix.md`
- Update operator runbook

### Slice 3 — graduation: 2 clean cadences without manual WAL intervention

## Status

- **Identified**: 2026-04-25, F1 Slice 4 S4 forensics
- **Tracked**: this doc
- **Workaround executed**: WAL backed up + cleared manually 2026-04-25 (one-shot for S4b launch)
- **Implementation**: NOT authorized; awaiting explicit operator green light

## Related stale state observed in S4 forensics

These are out-of-scope for this follow-up but were caught during cleanup:
- `.jarvis/intake_router 2.lock` (Apr 23 19:11) — orphaned space-in-name lock
- `.jarvis/intake_router 3.lock` (Apr 23 20:06) — orphaned space-in-name lock
- `.jarvis/posture_history 2.jsonl` — orphaned space-in-name posture file
- `.jarvis/backlog.json.pre-s6-harness.bak` — old backup, may be stale

Pattern: macOS Finder-style "filename N.ext" duplicates. Probably a hand-edit copy that wasn't cleaned up. Not blocking but should be reaped on a separate hygiene pass.
