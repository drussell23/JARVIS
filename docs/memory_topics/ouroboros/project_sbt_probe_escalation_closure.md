---
title: Project Sbt Probe Escalation Closure
modules: []
status: merged
source: project_sbt_probe_escalation_closure.md
---

May 2, 2026: SBT-Probe Escalation Bridge 3-slice arc closed same-day. Closes the deferred Priority #4 Slice 5b orchestrator hook end-to-end. Probe EXHAUSTED outcomes now escalate by default to a tree-shaped SBT analysis with multi-tool branch diversity before falling through to INCONCLUSIVE.

**Three slices shipped:**

1. **Slice 1 — Pure-stdlib decision primitive** (`sbt_escalation_bridge.py`, commit `33314519a1`): 5-value `EscalationDecision` closed taxonomy (ESCALATE / SKIP / BUDGET_EXHAUSTED / DISABLED / FAILED). Frozen `EscalationContext` + `EscalationVerdict` dataclasses. Total `compute_escalation_decision` mapping function. 5→3 `tree_verdict_to_collapse_action` mapping (TreeVerdict → ConfidenceCollapseAction string). Pure-stdlib at hot path with byte-parity tests against ProbeOutcome / TreeVerdict / ConfidenceCollapseAction enums (3 pin tests). Phase C `MonotonicTighteningVerdict.PASSED` stamping outcome-aware (ESCALATE → "passed"; others → empty). 66 tests.

2. **Slice 2 — Async wrapper + executor wire-up** (`sbt_escalation_runner.py`, commit `d345cac0f4`): `escalate_via_sbt()` async wrapper returning `Optional[ConfidenceCollapseVerdict]` (None = caller falls through; non-None = override). Composes Slice 1 primitive + existing `run_speculative_tree` runner. `asyncio.wait_for` defense-in-depth secondary timeout. Wires into `probe_environment_executor.execute_probe_environment()` — 30-line lazy-import block in EXHAUSTED branch calls wrapper before falling through to existing INCONCLUSIVE. Backward-compat by master flag default-FALSE through Slices 1-2. 21 tests including 4 executor integration tests proving (a) backward-compat unchanged, (b) escalation overrides INCONCLUSIVE when fires, (c) falls through cleanly when wrapper returns None, (d) defensively swallows wrapper crash + falls through.

3. **Slice 3 — Graduation + production prober adapter** (commits `2044f14f52` + `1429dbb39b`): Master flag flipped default false→true. NEW `sbt_branch_prober_adapter.py` — production `BranchProber` adapter wrapping Move 5's `ReadonlyEvidenceProber`. Each branch becomes one `ProbeQuestion` with a different `resolution_method` rotated deterministically across `READONLY_TOOL_ALLOWLIST` for branch diversity. Multi-tool agreement = strong signal; multi-tool divergence = genuine ambiguity. Singleton via `get_default_branch_prober()`. 3 SBT escalation flags + 7 SBT escalation AST-pin invariants registered via dynamic discovery (155 total flags / 68 total invariants post-Slice-3). 41 tests (24 adapter + 17 graduation including end-to-end through executor with production adapter wired).

**Architectural reuse spine — no duplication:**
- `READONLY_TOOL_ALLOWLIST` (Move 5): the canonical 9-tool frozenset. Adapter rotates over it. AST-pinned: adapter MUST reference this symbol (no parallel/duplicated allowlist).
- `ReadonlyEvidenceProber` (Move 5): the QuestionResolver that calls a ReadonlyToolBackend. Adapter wraps one branch call into one ProbeQuestion + resolve(). Singleton accessor reused.
- `BranchProber` Protocol (Priority #4): adapter implements `probe_branch(target, branch_id, depth, prior_evidence) -> Tuple[BranchEvidence, ...]`. Drop-in replacement for `_NullBranchProber`.
- Module-owned `register_flags(registry)` + `register_shipped_invariants()` contract (Priority #6 closure): all 3 modules expose them; discovered automatically. No edits to `flag_registry_seed.py` or `meta/shipped_code_invariants.py` required.

**Branch diversity pattern:**
- Same `branch_id` → same `resolution_method` (idempotent across retries via sha256 hash of branch_id mod allowlist size).
- Different `branch_ids` → spread across 9-tool allowlist (verified by rotation distribution test: 200 distinct ids yield ≥5 distinct methods).
- Branch evidence `content_hash = sha256(answer_text)` — semantically equivalent answers across branches converge on the same fingerprint → SBT CONVERGED → executor returns RETRY_WITH_FEEDBACK with tree fingerprint threading evidence into next GENERATE round.

**Sweep results:** 513/513 combined sweep across full SBT-Probe Escalation stack (Slices 1-3) + SBT (3 modules: speculative_branch + runner + comparator) + Move 5 (3 modules: confidence_probe_bridge + runner + executor) + Priority #1 Slice 5 graduation pin (canonical "all 68 invariants validate clean against main").

**Where O+V stands post InlinePromptGate + SBT-Probe Escalation:** A across the board structurally + cognitive interleaved-thinking loop now closes via SBT escalation when the single-probe is inconclusive. Multi-tool branch diversity widens the evidence search at every confidence-collapse point. Closes the §28.3 cognitive gap (autonomous ambiguity resolution) AND the deferred §28.5 tree-shape gap (interleaved-thinking + speculative branching + plan-replan).

**Why escalation-on-EXHAUSTED, not classifier:** The Founding Architect directive forbids hardcoding. A complexity classifier would require a magic threshold; an empirical "probe couldn't resolve it (EXHAUSTED) → try a wider tree (SBT)" is structural, not heuristic. The probe's EXHAUSTED verdict IS the complexity signal — measurement, not estimate.

**How to apply:** When `JARVIS_SBT_ESCALATION_ENABLED=true` (graduated default) and the Move 5 confidence probe loop returns `ProbeOutcome.EXHAUSTED`, the executor calls `escalate_via_sbt()` with the production `ReadonlyBranchProberAdapter` wired in. SBT spawns N parallel branches (default 3) each using a different read-only tool from `READONLY_TOOL_ALLOWLIST`. CONVERGED tree → `RETRY_WITH_FEEDBACK` with fingerprint; DIVERGED → `ESCALATE_TO_OPERATOR`; INCONCLUSIVE/TRUNCATED/FAILED → INCONCLUSIVE (mid-band collapse). Hot-revert via explicit `JARVIS_SBT_ESCALATION_ENABLED=false`.

**Commits:** `33314519a1` (Slice 1) → `d345cac0f4` (Slice 2) → `2044f14f52` + `1429dbb39b` (Slice 3 graduation, auto-commit hook split into two).
