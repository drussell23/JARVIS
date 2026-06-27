---
title: Project Move 7 Slice 1 2026 05 05
modules: [backend/core/ouroboros/governance/cross_op_semantic_budget.py, tests/governance/test_move_7_cross_op_semantic_budget.py]
status: historical
source: project_move_7_slice_1_2026_05_05.md
---

**Status (2026-05-05)**: Move 7 Slice 1 SHIPPED. 40 new tests + 367/367 across full sweep. Slices 2-5 deferred per Move 7's "long-horizon" framing.

## What landed

`backend/core/ouroboros/governance/cross_op_semantic_budget.py` (~580 LOC pure stdlib + math, substrate-only):

- **5-value `SemanticBudgetVerdict` closed enum** (`WITHIN_BUDGET` / `APPROACHING` / `EXCEEDED` / `INSUFFICIENT_DATA` / `DISABLED`)
- **Frozen `OpSemanticCentroid` artifact** adopting §33.5 Versioned-Artifact-Contract (`OP_SEMANTIC_CENTROID_SCHEMA_VERSION` + symmetric `to_dict` / `from_dict` defensive parse — None on malformed)
- **Frozen `SemanticBudgetReport`** with `to_dict()` projection (verdict + integrated_drift + threshold + approaching_band + window_size + per_op_deltas + diagnostics + elapsed_s + schema_version)
- **Pure-function `compute_semantic_budget(centroids, *, threshold, approaching_band_ratio, enabled_override) -> SemanticBudgetReport`** — integrates cosine-distance deltas across rolling window; verdict ladder branches: drift ≥ threshold → EXCEEDED; ≥ band (threshold × ratio) → APPROACHING; below → WITHIN_BUDGET. NO I/O, NO env reads inside the math (caller injects); NEVER raises.
- **Inline `cosine_distance(a, b)` pure-stdlib** — 8 lines, bytes-pinned for parity with `semantic_index._cosine`; substrate purity preserved (no cross-arc dependency on semantic_index full surface)
- **Master flag default-FALSE per §33.1** — `JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED` flips only after empirical Phase-9-style baseline; AST-pinned

## Architectural locks (§33 patterns reused)

- §33.1 Graduation Contract Pattern — master flag stays default-FALSE; AST pin enforces; synthetic test proves pin DOES fire on premature `return True` flip (AST-based not bytes-based to avoid self-matching false-positive)
- §33.5 Versioned-Artifact-Contract Pattern — `OpSemanticCentroid` carries schema_version + round-trip projection
- Authority asymmetry — substrate imports stdlib + math + meta.versioned_artifact ONLY (no orchestrator/iron_gate/policy/providers)

## 3 AST pins auto-discovered

1. `cross_op_semantic_budget_master_flag_stays_default_false` — operator binding (§33.1)
2. `cross_op_semantic_budget_authority_asymmetry` — substrate purity
3. `cross_op_semantic_budget_verdict_taxonomy_5_values` — closed-enum integrity

## 4 FlagRegistry seeds

- `JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED` (BOOL, default **False** — operator-pinned)
- `JARVIS_CROSS_OP_SEMANTIC_WINDOW_SIZE` (INT, default 50, clamped [2, 10000])
- `JARVIS_CROSS_OP_SEMANTIC_THRESHOLD` (FLOAT, default 0.30, clamped (0, 100])
- `JARVIS_CROSS_OP_SEMANTIC_APPROACHING_RATIO` (FLOAT, default 0.8, clamped [0.1, 1.0])

## Test spine

`tests/governance/test_move_7_cross_op_semantic_budget.py` — 40 tests:
- Closed-taxonomy 5-values
- Master flag asymmetric semantics (truthy/falsy/default-false)
- Cosine math 4-corner cases (identical/orthogonal/opposite/empty/zero-vector/unequal-lengths)
- Verdict ladder all 5 paths (DISABLED/INSUFFICIENT_DATA/WITHIN_BUDGET/APPROACHING/EXCEEDED)
- Integrated drift sums per-op deltas correctly
- OpSemanticCentroid §33.5 round-trip + defensive parse + filters non-numeric
- SemanticBudgetReport frozen + to_dict projection
- Defensive paths — malformed centroid in window skipped, None input handled
- Env knob clamping (3 knobs × 3 conditions)
- AST pins auto-registered + pass validation + premature-flip detection synthetic
- Authority asymmetry walk
- Public API stability (12 expected exports)

## Slices 2-5 deferred (per Move 7's "long-horizon" framing)

- **Slice 2** — centroid recorder at COMPLETE phase boundary + §33.4 flock'd JSONL ledger via `cross_process_jsonl.flock_append_line`; lazy producer-bridge per §33.2
- **Slice 3** — async observer (posture-aware cadence: HARDEN 1h / default 6h / MAINTAIN 24h) + SSE `semantic_budget_changed` event + `GET /observability/semantic-budget` route auto-mounted via §33.3 Slice 5b naming-cage
- **Slice 4** — `/semantic-budget` REPL verb auto-discovered via Slice 5b consolidation registry per §32.11 Slice 4
- **Slice 5** — graduation-contract harness gating master-flag flip on accumulated empirical evidence (per §33.1 pattern; Phase-9-style 30+ op evidence ladder)

## Architectural significance

Move 7 closes the SECOND axis of RSI drift mathematically:
- Move 4 (InvariantDriftAuditor): catches *architectural promise* drift (structural pin violations)
- Move 7 (Cross-op Semantic Budget): catches *semantic meaning* drift (codebase centroid rotation)

Together they bound drift in both axes — the foundation §29.4 line 3611 calls "the foundation for stable RSI." Substrate-level closure (Slice 1) is the precondition; empirical activation (Slice 5 master flag flip) waits on Phase 9's empirical baseline so the threshold knob is calibrated against this codebase's natural drift envelope, not arbitrary.

## What's next

Operator decision points:
1. Move 7 Slice 2 centroid recorder + JSONL ledger (~3-4 hours; substrate continuation)
2. Phase 9 empirical cadence (operator-paced; provides baseline for Move 7 graduation)
3. §28.5.1 4-phases-not-extracted hygiene arc
4. Move 8 Proactive Curiosity Loop scoping
