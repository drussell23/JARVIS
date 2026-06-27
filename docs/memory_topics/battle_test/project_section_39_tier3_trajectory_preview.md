---
title: Project Section 39 Tier3 Trajectory Preview
modules: [backend/core/ouroboros/governance/forecast_repl.py]
status: historical
source: project_section_39_tier3_trajectory_preview.md
---

May 8 2026: §39 Tier-3 closed end-to-end same-day. (#11 capability gap proposals already shipped earlier via §38.11-E composition — actual scope ~10h vs ~14h estimate.)

**Surface #4 — Op trajectory predictor** `governance/op_trajectory_predictor.py` (~770 LOC):
- Closed 4-value `TrajectoryConfidence` enum (HIGH 🎯 / MEDIUM 🎲 / LOW ❓ / UNKNOWN ⋯)
- Bytes-pinned `_CONFIDENCE_THRESHOLDS = ((0.70, HIGH), (0.40, MEDIUM), (0.0, LOW))` table
- First-match-wins `_score_to_confidence(score, sufficient_samples=)` with sample-size override (insufficient → UNKNOWN)
- Frozen §33.5 `TrajectoryPrediction` artifact (op_id + confidence + score + similar_op_count + similar_op_kind + median + p90 + ETA + elapsed)
- Predictor composes canonical `op_block_buffer.blocks_by_state(COMMITTED)` for sample + `find_by_op_id` for active op — ZERO parallel duration ledger
- Confidence score = `(sample_size_score × variance_tightness) ** 0.5` via stdlib `statistics`
- Pure NumPy-free p90 percentile via linear interpolation
- 5 AST pins: master_default_false / authority_asymmetry / confidence_taxonomy_4_values / composes_canonical_op_block_buffer / **confidence_thresholds_canonical** (bytes-pin 0.70/0.40)

**Surface #19 — Risk-aware command preview** `governance/risk_command_preview.py` (~660 LOC):
- Closed 4-value `PreviewVerdict` enum (SAFE ✓ / NOTIFY ⚠ / APPROVAL 🔒 / BLOCKED ✗)
- Bytes-pinned `_FLOOR_TO_VERDICT` map (canonical risk-tier-floor names)
- Frozen §33.5 `CommandPreview` artifact (route + reason + floor + verdict + governor_emergency + cost + duration + diagnostic)
- Frozen `_PreviewContext` synthetic dataclass duck-typed for canonical `UrgencyRouter.classify(ctx)` — exposes only the 7 read-fields (no clone of OperationContext)
- Previewer composes canonical (1) `UrgencyRouter().classify` (2) `risk_tier_floor.recommended_floor` (3) `sensor_governor.is_emergency_brake` (4) bytes-pinned per-route cost/duration tables drawn from CLAUDE.md canonical numbers
- 5 AST pins: master_default_false / authority_asymmetry (forbids orchestrator+candidate_generator; allows risk_tier_floor as canonical read source) / verdict_taxonomy_4_values / composes_canonical_urgency_router / composes_canonical_risk_tier_floor

**Combined `/forecast` REPL** `forecast_repl.py` (§33.3 auto-discovered): 4 subcommands (trajectory <op-id> [kind] / command <urgency> <source> <complexity> [files...] [--cross-repo] / status / help). Single REPL covers both predictive surfaces — sister surfaces under one verb.

**Two new SSE events**: `EVENT_TYPE_TRAJECTORY_PREDICTED` + `EVENT_TYPE_COMMAND_PREVIEW_RENDERED` registered in canonical `_VALID_EVENT_TYPES` frozenset.

**Sub-flag granularity**: trajectory master `JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED` default-FALSE per §33.1 + 2 tunables (min_samples / history_limit); preview master `JARVIS_RISK_COMMAND_PREVIEW_ENABLED` default-FALSE.

**Regression**: 87 new tests + **775/775 cumulative** across §38.11 (A-F) + §39 Tier-1 + Tier-2 + Tier-3 + canonical sources.

**§38.11.5a.5 single-canonical-name discipline honored**: trajectory predictor reuses canonical OpBlockBuffer + duration_s; preview reuses canonical UrgencyRouter + 5-value ProviderRoute + risk_tier_floor + sensor_governor — ZERO parallel route logic, ZERO parallel timing ledger, ZERO parallel risk-tier inference. The `_PreviewContext` synthetic dataclass is the only NEW substrate-level type and exposes only the duck-typed read-fields the canonical classifier needs.

**§33 patterns invoked**: §33.1 graduation contract / §33.3 naming-cage REPL / §33.5 versioned artifact (TrajectoryPrediction + CommandPreview).

**§39.5 sequencing status**: Tier 3 ✅ SHIPPED. Now 11 substrate modules + 10 §33.3 REPLs + 11 SSE event types + 48+ AST pins across §38.11 + §39 (Tier 1+2+3).

**NEXT**: Tier-4 introspective — #10 operator's-eye session story + #18 memory crystallization timeline (#9 self-narration already shipped via §38.11-D composition) — actual ~9h after composition wins. OR autonomous self-development arc (Vector #11 monotonic clock ~2h, Vector #9+#10 ~2d, M10 ArchitectureProposer ~7-10d).
