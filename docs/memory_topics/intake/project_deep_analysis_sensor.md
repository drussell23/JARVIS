---
title: Project Deep Analysis Sensor
modules: []
status: historical
source: project_deep_analysis_sensor.md
---

Derek wants O+V to have a DeepAnalysisSensor that goes far beyond current regex/metric scanners. The sensor should autonomously comprehend code intent, trace logic/data flow, and find subtle issues (race conditions, error swallowing, auth bypass, logic bugs) that no human commented on.

**Architecture confirmed (2026-04-11):**
- Phase 1 (Comprehend): DW 397B async — cost-efficient intent model building
- Phase 2 (Interrogate/Emit): Claude API with Venom tools — only fires if Phase 1 surfaces anomaly
- Maps to Manifesto §4 (Synthetic Soul) + §5 (Intelligence-Driven Routing)

**Full blueprint delivered 2026-04-12** — 9 sections covering trigger substrate (T1 idle / T2 commit-entropy / T3 blast-drift), class structure with dataclasses, DeepAnalysisLedger persistent memory, non-blocking execution (M1-M4), output contract, risks. See session 66cd0307 for the full text.

**Execution order (Derek, 2026-04-12):** B → A → D (tweaks folded) → C blocked.
1. HIBERNATION_MODE first (DeepAnalysis M4 depends on ProviderExhaustionWatcher).
2. Git merge of battle-test branch when ready.
3. Blueprint tweaks (below) folded before implementation.
4. DeepAnalysis starts ONLY after 3 objectively-clean battle tests. No override.

**Blueprint tweaks (fold before implementation):**
1. **Router API**: sensors call `router.ingest(envelope)` — confirmed at `unified_intake_router.py:282`. Blueprint's `_router.submit()` is wrong; align with OpportunityMinerSensor / make_envelope flow and pending_ack behavior.
2. **Trigger T1 predicate**: "no GENERATE in flight" must bind to one authoritative predicate. `_active_file_ops` is file-level, not phase-level. Define a single `is_governance_idle()` helper on the orchestrator that returns True iff (BG queue empty) AND (no op in {CLASSIFY..APPLY}) AND (last envelope landed > N seconds ago). T1 consumes only that.
3. **Phase 1 invocation path**: document that sensor-internal Phase 1 calls are a dedicated BG pool job (route=BACKGROUND, priority=5), NOT a normal intake envelope. Must thread through the cost governor so Phase 1 DW calls count against the budget — do not bypass it. Phase 2 Claude interrogation is invoked via the same BG pool with route=COMPLEX but strictly gated by Phase 1 anomaly.
4. **Oracle staleness in evidence contract**: Finding.evidence["topology_source"] must record {"oracle_edges": True, "inline_ast": True, "oracle_cached_at": ts} so VALIDATE/Iron Gate reviewers know topology edges are approximate (cached graph) while the target file's internal AST is fresh-parsed at analysis time.

**Clean battle test gate (objective, 3 consecutive required):**
- Zero `all_providers_exhausted` events in summary.json
- Zero sem-starved or budget-starved fallbacks in summary.json
- At least one full GENERATE → VALIDATE → GATE → APPLY → VERIFY per session (APPLY is the bar, not just VERIFY attempt)
- Stop reason is NOT cost_cap (means the budget had headroom)
- 3 sessions in a row satisfy all four. Anything that breaks the streak resets the counter.

**Why ON HOLD**: Progressive Readiness (Manifesto §2). Cannot install a massive cognitive analysis loop on infrastructure that still fatally dies on provider exhaustion (bt-2026-04-12-192619). Building DeepAnalysis on top of that stack adds DW/Claude load and widens failure modes.

**Prerequisites (order-locked):**
1. HIBERNATION_MODE merged (~400 LoC, 8-step order in prior blueprint). Solves `all_providers_exhausted` → fatal transition.
2. Hot-reload capability (`JARVIS_HOT_RELOAD_ENABLED`) — organism hot-swaps Python modules without restart.
3. 3 consecutive clean battle tests per the objective gate above.

**How to apply:** Do NOT begin DeepAnalysisSensor implementation until all three prerequisites are met. Design is frozen — execution is sequenced behind infrastructure stabilization. No override without explicit written directive from Derek.
