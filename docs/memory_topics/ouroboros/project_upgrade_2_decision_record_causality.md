---
title: Project Upgrade 2 Decision Record Causality
modules: [scripts/replay_determinism.py, backend/core/ouroboros/governance/verification/dag_navigation.py, backend/core/ouroboros/governance/flag_registry_seed.py, tests/governance/test_upgrade_2_graduation.py, backend/core/ouroboros/governance/ide_observability_stream.py, backend/core/ouroboros/governance/decisions_observability.py, backend/core/ouroboros/governance/decisions_repl.py, scripts/ouroboros_battle_test.py, backend/core/ouroboros/governance/determinism/decisions_reader.py, backend/core/ouroboros/governance/determinism/decision_runtime.py]
status: merged
source: project_upgrade_2_decision_record_causality.md
---

**Status (2026-05-04)**: **CLOSED** — All 5 slices graduated default-TRUE same-day. **124/124 tests green** (22 DecisionKind + 32 replay determinism + 39 observability/REPL + 8 SSE + 18 graduation + 5 falsy-revert).

## Substrate audit (CRITICAL FINDING — most of Upgrade 2 is already shipped)

**Already live** (Phase 1 Slice 1.4 + Priority 2 Slices 1-6 graduated default-true):
- `DecisionRecord` frozen primitive (10 base fields + lineage + per-worker ordinals) at `determinism/decision_runtime.py:257`
- `DecisionRuntime` class — per-session JSONL ledger with RECORD/REPLAY/VERIFY/PASSTHROUGH modes, asyncio.Lock + threading.RLock + flock'd via `_file_lock.flock_exclusive`
- `decide()` API + `VerifyResult` + `DecisionMismatchError`
- `capture_phase_decision()` async wrapper at `determinism/phase_capture.py:251`
- 4 phase boundaries already instrumented: route_runner / gate_runner / plan_runner / complete_runner
- `CausalityDAG.build_dag(session_id)` + `subgraph()` at `verification/causality_dag.py`
- `dag_navigation.py` with REPL `dispatch_dag_command()`
- `GET /observability/dag/{session_id}[/{record_id}]` HTTP routes already live
- Master flag `JARVIS_DETERMINISM_LEDGER_ENABLED` graduated default-true (Phase 1 Slice 1.5)

## Slice 5 (DONE) — Graduation

- Master flag `JARVIS_DETERMINISM_REPLAY_ENABLED` flipped default false → **true** (asymmetric env semantics — explicit `false`/`0`/`no`/`off` for instant revert)
- 4 FlagRegistry seeds in `flag_registry_seed.py`: master + DECISIONS_READER_DEFAULT_LIMIT (100) + DECISIONS_READER_MAX_RECORDS (10000) + DECISIONS_READER_MAX_SESSIONS (1000)
- 4 AST shipped-code-invariants pins in `meta/shipped_code_invariants.py`:
  - `replay_determinism_master_default_true` — bytes-pin "Graduated default 2026-05-04 (Slice 5)" marker
  - `decision_kind_closed_enum_intact` — all 12 DecisionKind enum members must remain (catches silent removal of decision-site kind)
  - `decisions_observability_read_only` — observability layer has no `DecisionRuntime(` / `.record(` / `_persist_history` mutation tokens
  - `replay_lazy_imports_sse_publisher` — replay's broker import must be lazy (no top-level `from ide_observability_stream`)
- Pre-existing slice 1-4 tests migrated `delenv` → `setenv("...", "false")` for default-true semantics
- Graduation regression file `test_upgrade_2_graduation.py` (18 tests) — pins flag flip + REPL auto-discovery + 4 seeds + 4 AST pins + SSE vocabulary + launcher presence + module-spine health

## Slice 4 (DONE) — decision_drift_detected SSE

- `ide_observability_stream.py` extension: `EVENT_TYPE_DECISION_DRIFT_DETECTED = "decision_drift_detected"` + `publish_decision_drift_event()` helper. Single event covers all 4 actionable ReplayDriftKind values (NONE silent — chatter suppression). Bounded payload (256-char cap per field via to_dict() projection). Pattern matches publish_curiosity_event / publish_budget_action_event / publish_trajectory_drift_event.
- `replay_determinism.replay_session_consistency()` extended to publish one SSE per drift entry — best-effort, exception-isolated, lazy-imported (broker stays out of replay's import graph at module load). Replay's `exit_code` + `drift_entries` remain authoritative even when publish raises (verified by `test_exit_code_authoritative_when_publish_raises`).
- Test contract: clean records produce zero events (chatter suppression); 2 drifted records produce exactly 2 events; payload carries session_id + drift_kind + record_id + bounded expected/actual/detail + ts_unix; lazy-import discipline source-grep-pinned.

## Slice 3 (DONE) — /decisions REPL + GET /observability/decisions

- New `determinism/decisions_reader.py` (~370 LOC) — shared read primitives: `list_available_sessions()` (filesystem walk under ledger root, mtime-desc sort), `read_records_for_session(sid, limit, kind_filter)` (cross-process flock'd JSONL read with bounded limit + kind filter), `aggregate_kinds_for_session(sid)` (histogram), `recent_records_across_sessions(limit, kind_filter)` (cross-session aggregation). Reuses existing `_ledger_dir()` + `flock_critical_section`. Frozen result containers (`SessionListEntry` / `DecisionsQueryResult` / `KindAggregation`). Hard caps (`max_records_per_session=10000`, `max_sessions_listed=1000`, `_MAX_LEDGER_FILE_BYTES=100MB`). All env-tunable via `JARVIS_DECISIONS_READER_*` knobs.
- New `decisions_observability.py` (~280 LOC) — `GET /observability/decisions` overview (sessions list + recent records + cross-session kind histogram + DecisionKind vocabulary) + `GET /observability/decisions/session/{session_id}` detail (paginated per-session ledger + per-session histogram). 503/429/400/404 + Cache-Control: no-store. `?limit=N` + `?kind=K` query params.
- New `decisions_repl.py` (~410 LOC) — `/decisions {recent, session, kind, sessions, count, help}` 5-subcommand REPL with `register_verbs()` auto-discovery. Read-only — no mutation surface (Slice 5 AST-pinned).
- Master flag defers to existing `decision_runtime.ledger_enabled()` — no parallel flag (graduated default-true via Phase 1 Slice 1.5 already).
- All three modules: authority floor pinned (no orchestrator/iron_gate/providers imports), read-only contract pinned (no `DecisionRuntime(` / `.record(` / mutation tokens in source).

## Slice 2 (DONE) — Replay-determinism primitive + CLI launcher

- New `determinism/replay_determinism.py` (~480 LOC + 32 tests):
  - 5-value closed `ReplayDriftKind` enum: NONE / INPUT_HASH_MISMATCH / OUTPUT_REPR_NON_CANONICAL / SCHEMA_VERSION_DRIFT / PARSE_ERROR
  - Frozen `ReplayDriftReport` (record_index + record_id + expected + actual + detail; bounded 256-char projection)
  - Frozen `ReplaySummary` (records_total / records_verified / drift_entries / elapsed_s / exit_code / diagnostics; POSIX exit codes 0/1/2)
  - `replay_session_consistency(session_id)` — load + verify entry; uses `flock_critical_section` for cross-process tear-safe read; resolves ledger path via existing `session_replay._ledger_dir()` (zero duplication)
  - `_verify_record()` per-record verifier — schema-version check FIRST (so version drift surfaces as distinct kind not PARSE_ERROR), then `DecisionRecord.from_dict()` parse, then output_repr canonical-form check via `_canonical_serialize()` round-trip
  - `replay_cli_main()` argparse wrapper — `--session`, `--json`, `--allow-disabled` flags
- New `scripts/replay_determinism.py` thin launcher (~50 LOC) — adds repo-root to sys.path + delegates to `replay_cli_main()`. Mirrors `ouroboros_battle_test.py` thin-launcher pattern
- Master flag `JARVIS_DETERMINISM_REPLAY_ENABLED` default-FALSE (Slice 5 graduates default-TRUE)
- Authority floor pinned: zero coupling to orchestrator/iron_gate/providers/etc.
- Reuses `_canonical_serialize` + `DecisionRecord.from_dict` + `_ledger_dir()` + `flock_critical_section` — no parallel canonicalization or locking code

## Slice 1 (DONE) — `DecisionKind` closed enum

- New module `determinism/decision_kinds.py` (~75 LOC + 22 tests)
- 12-value closed taxonomy: ROUTE_SELECTION / GATE_PASS / GATE_FAIL / VALIDATOR_PASS / VALIDATOR_FAIL / RISK_ESCALATION / PROBE_TRIGGER / SBT_TRIGGER / AUTO_ACTION_PROPOSAL / APPROVAL_REQUEST / PHASE_TRANSITION / DISABLED
- `str` subclass — backward-compat with shipped freeform `kind=` strings preserved (verified by test that DecisionRecord written with `DecisionKind.X.value` is byte-identical to one with the raw string)
- Authority floor pinned: zero coupling to orchestrator/iron_gate/providers/etc.
- Site instrumentation deferred (existing 4 phase boundaries are sufficient for Slice 2's replay job; granular site hooks land as follow-up)

## What's actually still missing for Upgrade 2 closure

| Slice | Item | Effort |
|---|---|---|
| 2 | ✅ DONE — `replay_determinism.py` primitive + `scripts/replay_determinism.py` CLI launcher (54/54 tests green) |
| 3 | ✅ DONE — decisions_reader.py + decisions_observability.py + decisions_repl.py (93/93 tests green) |
| 4 | ✅ DONE — EVENT_TYPE_DECISION_DRIFT_DETECTED SSE wired into replay_session_consistency (101/101 tests green) |
| 5 | ✅ DONE — master flag flipped + 4 AST pins + 4 FlagRegistry seeds + graduation regression file + PRD §31.3 closure marker (124/124 green) |

**Total remaining effort**: ~3-4 days vs original §31.3 ~7-9 day estimate. The expensive infrastructure was already shipped via Phase 1 Slice 1.4 + Priority 2; Upgrade 2 is essentially the **graduation surface** for that substrate.

## Architectural locks (operator mandate)

1. **Zero duplication** — extend existing DecisionRuntime, do NOT parallel-implement
2. **Closed-enum DecisionKind** — AST-pinned at Slice 5 to enforce all NEW writes use enum value (legacy reads tolerated)
3. **No new flock primitive** — reuse `adaptation/_file_lock.flock_exclusive`
4. **Replay job offline** — runs from operator machine, NEVER blocks live FSM
5. **RSI safety gate** — patches touching `decision_runtime.py` from `decisions.jsonl` caller chain forced to APPROVAL_REQUIRED (PRD §31.3.4)
6. **Decision sites NEVER raise** — silent skip preserves existing DecisionRuntime contract
7. **Backward-compat byte-identity** — old freeform `kind` strings continue to read; new writes use enum.value
