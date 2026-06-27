---
title: Project Move 8 Proactive Curiosity Loop
modules: [backend/core/ouroboros/governance/proactive_curiosity_reader.py, backend/core/ouroboros/governance/proactive_curiosity_loop_graduation_contract.py]
status: historical
source: project_move_8_proactive_curiosity_loop.md
---

Move 8 (PRD §29.7 / §35) closed end-to-end across 3 slices same-day. **117 new regression tests** + 220/220 across the full Move 7 + Move 8 + Wave 3 + closure spine.

**Why:** M9 (CLOSED 2026-05-04) wired three producers (GENERATE logprob entropy / VERIFY prophecy error / CoherenceAuditor RECURRENCE_DRIFT) into `CuriosityCollector` but no consumer translated curiosity scores into actionable exploration intents. The §35 entry framed Move 8 as "auto-spawn exploration ops on high-curiosity regions without operator nudge" — that's exactly what the 3 slices close.

**How to apply:** mirror this scoping pattern when investigating "is the broader Move X already done?" — the answer for Move 8 was "M9 substrate exists, Move 8 = consumer wire-up + auto-spawn." The right shape was 3 slices (not 5) because the async observer already lives in `ProactiveExplorationSensor._poll_loop` — extending that loop with a 3rd signal source preserves the existing cadence, dedup, exception-isolation, and SensorGovernor contract; no parallel sensor.

**Architecture**:
```
M9 producers (GENERATE/VERIFY/CoherenceAuditor)
   → CuriosityCollector (state)
   → Slice 1 rank_curious_clusters() (filter + rank + cooldown)
   → Slice 2 ProactiveExplorationSensor._emit_curiosity_signals (envelope synth + router.ingest)
   → Slice 3 is_ready_for_graduation (gate Slice 1 master flag flip)
```

**Slice 1 — `proactive_curiosity_reader.py`** (~770 LOC pure substrate):
- 5-value `CuriosityRankingDecision` closed enum (SURFACED / BELOW_FLOOR / COLD_START / DECAY_SUPPRESSED / COOLDOWN)
- Frozen `CuriosityRanking` (§33.5 versioned with symmetric `to_dict`/`from_dict` defensive parse)
- Pure-function `rank_curious_clusters()` composing `CuriosityCollector.snapshot_all`; integrates cooldown + cold-start exclusion + decay-reason exclusion + magnitude-floor filtering + top-K cap
- In-process cooldown ledger (cross-call dedup, env: `JARVIS_PROACTIVE_CURIOSITY_COOLDOWN_S` default 4h)
- Master flag `JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED` default-FALSE per §33.1 graduation contract pattern
- 4 AST pins auto-discovered (master-flag default-FALSE + authority asymmetry + decision taxonomy 5-values + composes-M9-substrate forbidding direct `compute_curiosity` calls)
- 4 FlagRegistry seeds
- Output ordering: SURFACED rows in rank order (1..K), then non-SURFACED in input order; demoted-by-K rows dropped silently (truthful decision distribution preserved)

**Slice 2 — ProactiveExplorationSensor wire-up**:
- `_emit_curiosity_signals()` method composes Slice 1 reader as 3rd signal source in `scan_once` (alongside LearningConsolidator failure-rules + codebase_character cluster-coverage)
- Lazy-imported via try/except (Decision X — substrate may be absent in rollback paths)
- Posture-aware suppression at HARDEN; fail-open on posture-probe glitch
- Reader raises → empty list returned (parent scan_once already exception-isolated; defense-in-depth)
- One bad envelope build does NOT poison other rankings
- Envelope evidence shape: `category=curiosity_driven` + `cluster_id` + `magnitude` + `confidence_m9` + `dominant_source` + `samples_count` + `rank` + `sensor=ProactiveExplorationSensor`
- SSE event_type=`curiosity_intent_emitted` via `ide_observability_stream` (best-effort)
- firing_telemetry counters `curiosity_driven_envelope_emit` + per-source
- Composition-order regression pin: curiosity emission MUST follow `_emit_cluster_coverage_signals` in scan_once

**Slice 3 — `proactive_curiosity_loop_graduation_contract.py`** (~600 LOC pure substrate):
- §33.1 graduation-contract harness mirroring `cross_op_semantic_budget_graduation_contract` and `phase10_graduation_contract` canonical shape exactly
- 5-value `CuriosityGraduationVerdict` closed enum (READY_FOR_GRADUATION / INSUFFICIENT_EMISSIONS / EXCESSIVE_THROTTLES / ALREADY_GRADUATED / DISABLED)
- Frozen `CuriosityGraduationReport` with `to_dict()` projection
- 3-gate `is_ready_for_graduation` predicate first-match-wins:
  - Gate 1: Slice 1 master flag already flipped → ALREADY_GRADUATED (no-op, NOT an error)
  - Gate 2: ≥ `required_emissions` (default 12 — 3× across each of 4 postures + headroom; clamped [3, 1000])
  - Gate 3: ≤ `max_governor_throttles` (default 0 — "the loop integrates cleanly"; clamped [0, 100])
- Harness master flag `JARVIS_PROACTIVE_CURIOSITY_GRADUATION_CONTRACT_ENABLED` default-TRUE per §33.1 separation-of-concerns (operator-binding default-FALSE lives on Slice 1's flag; harness is queryable)
- 3 AST pins (authority asymmetry + verdict taxonomy 5-values + composes-substrate via Slice 1's flag-helper — single source of truth, AST-precise `os.environ.get(literal)` detection avoiding diagnostic-string false positives)
- 3 FlagRegistry seeds
- Pattern compliance test proves §33.1 canonical shape parity with Move 7 Slice 5

**Tests**:
- Slice 1: 58 (closed taxonomy + §33.1 master-flag asymmetric semantics + env-knob clamping + verdict ladder all 5 paths + top-K cap + tie-break + cooldown + defensive paths + AST pins + FlagRegistry seeds + public API stability)
- Slice 2: 17 (structural call-site pin + composition-order pin + master-flag gate + decision gating + posture suppression + fail-open paths + envelope shape contract)
- Slice 3: 42 (closed taxonomy + harness master default-TRUE + env-knob clamping + 5-verdict ladder all paths + first-match-wins semantics + defensive paths + AST pins + §33.1 pattern compliance + FlagRegistry seeds)

**Pattern catalog reuse**: 5 of 5 §33 meta-patterns invoked across Move 8 (graduation contract / authority asymmetry / versioned-artifact / closed-taxonomy / posture-aware substrate).

**Operator-paced next**: 3+ clean evidence-ladder cycles → contract reports READY_FOR_GRADUATION → flip Slice 1's master flag default-TRUE.
