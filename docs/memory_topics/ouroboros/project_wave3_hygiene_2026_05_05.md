---
title: Project Wave3 Hygiene 2026 05 05
modules: [tests/governance/test_wave3_hygiene_2026_05_05.py, backend/core/ouroboros/governance/agentic_general_subagent.py, backend/core/ouroboros/governance/exploration_fleet.py, backend/core/ouroboros/governance/mutation_tester.py, backend/core/ouroboros/governance/mutation_gate.py, backend/core/ouroboros/governance/unlimited_agents.py, backend/core/ouroboros/governance/scheduled_agents.py, backend/core/ouroboros/governance/observability/flag_change_emitter.py]
status: merged
source: project_wave3_hygiene_2026_05_05.md
---

**Status (2026-05-05)**: Wave 3 hygiene arc 4/6 items closed in single session. Items deferred: vector #10 AutoCommitter race (~1hr; needs lock-directory convention) + vector #8 ArtifactContract drift (multi-hour schema-versioning).

## Items closed

### Item 1 — Move 8 GENERAL LLM driver status conflict (PRD reconciliation only)

**Truth**: both CLAUDE.md and §28.6.3 are accurate at different layers:
- `agentic_general_subagent.py:39` describes the FALLBACK path returning `NOT_IMPLEMENTED_NEEDS_LLM_WIRING` when `JARVIS_GENERAL_LLM_DRIVER_ENABLED=false`
- `agentic_general_subagent.py:629-660+` describes the graduated factory wiring `general_driver.run_general_tool_loop` when the flag is true (default-true post 2026-04-20)

**§35 row updated** to reconcile; no code change.

### Item 2 — §3.6.2 vector #11 wall-clock → monotonic migration

**8 elapsed-time call sites migrated** across:
- `exploration_fleet.py:135, 232` (1 paired init+check)
- `mutation_tester.py:479, 521 (dur), 581, 612` (3 paired patterns)
- `mutation_gate.py:370, 390, 675` (2 paired patterns)
- `unlimited_agents.py:249, 329, 426, 487` (2 paired patterns)

**Cron scheduling at `scheduled_agents.py:432` deliberately retained wall-clock** — cron expressions are wall-clock by spec.

Per-file AST regression pin asserts ≥2 `time.monotonic()` calls per migrated file.

### Item 3 — §3.6.2 vector #9 FlagChangeEvent value masking

**`flag_change_emitter.py`** — added:
- `_SENSITIVE_NAME_TOKENS` FrozenSet (10 patterns: key/token/secret/password/passwd/pwd/credential/private/auth/session_id)
- `_is_sensitive_flag(flag_name)` — case-insensitive substring match
- `_mask_value(value)` — sha256[:8] + length token (e.g. `<MASKED:51c3ba65:len=10>`)
- `FlagChangeEvent.to_dict()` redacts both prev_value and next_value when sensitive
- New `value_masked: bool` field surfaces masking decision to consumers
- None values pass through unchanged so add/remove transitions stay distinguishable

Bytes-pinned token set so future drift fails the `test_sensitive_token_set_pinned` regression.

### Item 4 — §28.5.1 invariant_drift_store baseline write race

**`invariant_drift_store.write_baseline()`** — now wraps `_atomic_write` in `cross_process_jsonl.flock_critical_section` per §33.4 Per-Cluster flock'd JSONL Persistence pattern. Lazy-imported with fallback to in-process-lock-only when primitive unavailable (NEVER raises). POSIX-atomic rename preserved.

## Test spine

`tests/governance/test_wave3_hygiene_2026_05_05.py` — 24 tests covering:
- Per-file AST check that monotonic migration succeeded (4 files × parametrize)
- Sensitive flag masking (10 credential-shape names parametrized)
- Non-sensitive flag passthrough (5 names parametrized)
- None-value handling
- Bytes-pinned `_SENSITIVE_NAME_TOKENS` set
- Source-grep + AST check that `write_baseline` uses `flock_critical_section`
- AST check that `_atomic_write` still called inside the function
- Move 8 reconciliation evidence pins

## Items deferred

### Item 5 — Vector #10 AutoCommitter race (~1hr focused arc)

The race is a TOCTOU between `_intent_token_exists()` and `git commit` + `_store_intent_token()`. Two concurrent processes can both pass the dedup check before either commits, resulting in duplicate commits.

**Fix scope**: wrap the `commit()` critical section (intent_token check → git commit → store_intent_token) in a per-token flock keyed on `.jarvis/auto_commit_locks/<token>.lock`. Needs:
- New lock-directory convention
- Async-safe flock (the current `cross_process_jsonl.flock_critical_section` is sync; AutoCommitter uses asyncio.create_subprocess_exec)
- Tests proving the race window is closed

### Item 6 — Vector #8 ArtifactContract drift (multi-hour arc)

Multiple `*Artifact` dataclasses (`RollbackArtifact`, `SagaLedgerArtifact`, `WorkUnitLedgerArtifact`, etc.) without unified schema versioning. Cross-runner readers may see drift if any artifact's schema evolves.

**Fix scope**: introduce `ArtifactContract` base + per-artifact `schema_version` field + dual-reader pattern for backward-compat. Multi-hour scoping arc, not in-session work.

## PRD updates

- v2.24 → v2.25 with closure narrative
- §35 Open Strategic Moves Registry: 3 rows flipped to ✅ CLOSED; triage recommendation updated to reflect 4-of-6 progress
- 24 new tests integrated into the consolidation + Phase 10 regression sweep (263/263 + 24 = 287/287)

## Operator decision points

1. **AutoCommitter race fix** (~1hr) — completes the Wave 3 hygiene arc to 5/6
2. **ArtifactContract schema versioning** (multi-hour) — closes the last vector but bigger scope
3. **Phase 9 empirical cadence** — operator-paced soak runs (closes 🔴 vectors #6+#7)
4. **Move 7 Cross-op Semantic Budget scoping** — long-horizon arc

## Architectural significance

All 4 closures align with §33 reusable meta-patterns:
- Item 2 codifies "monotonic-for-elapsed" discipline (no §33 entry yet — could add as §33.5)
- Item 3 codifies "mask-for-credentials" discipline (no §33 entry yet — could add as §33.6)
- Item 4 directly applies §33.4 Per-Cluster flock'd JSONL Persistence
- Item 1 is documentation-only

The Wave 3 hygiene arc proves the §33 meta-pattern catalog already supports the next-level work — disciplines crystallize into substrate, then become reusable.
