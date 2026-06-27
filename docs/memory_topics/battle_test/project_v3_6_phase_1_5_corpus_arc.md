---
title: Project V3 6 Phase 1 5 Corpus Arc
modules: [scripts/validate_l2_exercise_corpus_hardness.py, tests/governance/test_l2_exercise_corpus_hardness_validator.py, backend/core/ouroboros/governance/l2_exercise_seed.py, backend/core/ouroboros/battle_test/harness.py]
status: historical
source: project_v3_6_phase_1_5_corpus_arc.md
---

**v3.6 Phase 1.5 — L2 Exercise Corpus + Hardness Validator (2026-05-12, single-day arc)**

Phase 1.5 closes the gap between [[project-v3-3-treefinement-l2-six-phase-arc]] (substrate) + [[project-v3-4-treefinement-production-wiring]] (production wiring proof) + [[project-v3-5-treefinement-first-validation-soak]] (boot-safety validation) on one side, and PRD §40.7.9 Phase 2 SWE-Bench-Pro (benchmark-scale hardness) on the other. Between them was the question: *"before paying for SWE-Bench-Pro compute, demonstrate that O+V's L2 path fires under bounded paid load against in-repo fixtures."* Phase 1.5 closes that gap operator-paced for ~$0.05/run.

**Why:** §40.7.8 Phase 1 (the deterministic CI test) proves wiring isn't crashed; SWE-Bench-Pro compute proves benchmark performance. Phase 1.5 sits between them as the *cheapest-possible empirical bridge*: real DW provider calls, real subprocess pytest verdicts, real cost tracking — but bounded to single-digit dollars across many iterations.

**How to apply:** Read this when planning Phase 2 SWE-Bench-Pro wiring (this is the empirical bedrock it builds on), when designing future synthetic hardness fixtures (the canonical-pattern-defeats-traps lesson below is decisive), or when re-running 1.5.E acceptance.

---

## Arc trajectory

| Stage | What shipped | Outcome |
|---|---|---|
| 1.5.A substrate (`287cd8fa39`) | `l2_exercise_seed.py` (loader + envelope builder + 3 master flags + canonical `make_envelope` composition) | substrate green |
| 1.5.B first fixture (`55ea5c968b`) | `problem_001` off_by_one (nth_smallest); proves substrate works with one fixture | bug-is-real + fix-is-real pinned |
| 1.5.C harness wire (`6cfa5631ba`) | `maybe_inject_exercise_at_boot` boot-hook in `battle_test/harness.py` (lazy import, fail-open) | default-FALSE; production behavior byte-identical when unset |
| 1.5.D validator (`54de85b7e8`) | `scripts/validate_l2_exercise_corpus_hardness.py` — operator-paced bounded-cost CLI | refusal paths green; canary measurement against problem_001 |
| 1.5.D fix (`3e9765eefb`) | `_extract_dw_message_content` mirroring canonical DW provider's `content` → `reasoning_content` fallback | 4/5 attempts had hit KeyError on Qwen3.5 reasoning_details-only shape |
| 1.5.D first paid run | problem_001 alone — 1/1 completed pass = fixture too easy | empirical signal: off-by-one is canonical for LLMs |
| 1.5.D.2 Stage 1 (`ff13e1118a`) | schema v2 + AttemptStatus 4-value taxonomy + parse-retry + min_completed_per_problem + HARDNESS_SET | bias fix: parse errors excluded from numerator AND denominator |
| 1.5.D.2 Stage 2.1 (`b139dea20b`) | problem_002 logic_inversion (MaxPriorityQueue, multi-site push+peek negation invariant) | naive-fix canary (negate-at-push-only) embedded in spine; AST-discoverable |
| 1.5.D.2 Stage 2.2 (`110df54173`) | problem_003 v1 missing_null_check (sanitize_thread exclusion contract) | naive-fix canaries embedded |
| 1.5.D.2 Stage 3 paid run | HARDNESS_SET={002, 003-v1}, threshold 0.40 | 002: 0.80 fail / 003-v1: 0.00 fail — boundary pass mean=0.40, problem_003 NOT triggering trap |
| 1.5.D.2 Stage 3.5.A (`7dc4a54bfb`) | strict gate: mean ≥ 0.45 + per-problem floor 0.20 + `below_floor` diagnostic | "no freeriders" rule pinned by `test_gate_below_floor_returns_unevaluable` (replicates Stage 3 distribution exactly) |
| 1.5.D.2 Stage 3.5.B (`60fd444eb1`) | problem_003 v2 in-place redesign — neutral function names (clean/shorten/process/collect), no docstring contract, AST pin forbidding v1 telegraphing phrases | response to Stage 3 0%: hide contract surface |
| 1.5.D.2 Stage 3.5.C (`fac9406d3a`) | problem_004 dict_keyerror recursive deep-merge — depth-ceiling canaries (shallow + level-1-only naive fixes embedded in spine) | corpus walker now sees 4 problems |
| 1.5.D.2 Stage 3.5.D paid run | HARDNESS_SET={002, 003-v2, 004}, strict gate | 002: 1.00 / 003-v2: 0.00 / 004: 0.00 → `below_floor=[003-v2, 004]`, gate FAILS correctly |
| 1.5.D.2 Stage 3.6 mining survey | 388 sessions + 150 parse_failures + repair_tree.jsonl walked | mineable VALIDATE-failure markers exist but reconstructing them as drop-in fixtures = ~2h git-archaeology + heavy sanitization per candidate; **does not** guarantee non-canonical property |
| Phase 1.5.F (this artifact) | PRD §40.7.10 + this memory + 1.5.E plan: HARDNESS_SET={problem_002} singleton bootstrap | multi-fixture hardness deferred to Phase 2 |

---

## Decisive empirical finding (this arc's main lesson)

**Qwen3.5-397B-A17B-FP8 (production DW Tier 0 default model) is extraordinarily good at canonical-pattern bugs — even with hidden contract surfaces and depth-ceiling canaries.**

Measured first-try fail rates (5 attempts per problem, --confirm-paid runs):

| Fixture | Bug pattern | Fail rate | Verdict |
|---|---|---|---|
| problem_001 | off_by_one (nth_smallest indexing) | 0.50 (4 completed) | not in HARDNESS_SET; smoke role |
| problem_002 | logic_inversion (heapq + max-priority + peek priority-leak) | **1.00** (5/5) | **only fixture that defeated the model** |
| problem_003 v1 | missing_null_check (with telegraphing docstring) | 0.00 (5/5 pass) | name + docstring revealed contract |
| problem_003 v2 | missing_null_check (neutral names, behavior-inferred contract) | 0.00 (5/5 pass) | model infers contract from test I/O examples — hiding docstring **insufficient** |
| problem_004 | dict_keyerror (recursive deep-merge) | 0.00 (5/5 pass) | "deep merge" is canonical pattern; model recursed correctly |

**Why problem_002 is the only winner:** it wraps `heapq` (Python's min-heap) into a `MaxPriorityQueue` with a `peek` invariant requiring the internal negated-priority storage to be *re-negated* before returning to the caller. The naive fix (negate at push() only) leaks the internal -priority through peek's return tuple. This is **non-canonical multi-abstraction** (data structure layer + API contract layer) — Qwen3.5 doesn't have a clean training-data analog.

**Why hiding the docstring surface didn't save problem_003-v2:** test input/output pairs (`collect(["<p>alpha</p>", None, "<b>beta</b>"]) == ["alpha", "beta"]`) are themselves a clear contract surface. The model reads the examples and infers the filtering semantics. **Conclusion: contract-surface hiding via renaming + docstring removal is NOT sufficient to defeat canonical-pattern recognition** when the test cases reveal behavior.

**Why naive-fix canaries in the spine alone aren't enough:** the spine tests prove the trap is *structurally present* (i.e., a deliberately-naive fix DOES fail at least one test). But the model doesn't take the naive path — it pattern-matches to the correct solution directly from training data. The structural trap and the empirical trap are different things.

---

## Bootstrap scope (the operationally-honest decision)

**HARDNESS_SET = {problem_002} for Phase 1.5 acceptance only.**

- The gate computation is **NOT relaxed** — per-problem floor stays 0.20, mean threshold stays 0.45. The SET is narrowed, not the contract.
- problem_002 measurement: fail_rate=1.00, completed=5/5, both floor and threshold cleared with margin.
- The gate now answers the operationally-honest question: *"Is there at least one fixture in this repo that reliably forces first-try failure under the harness?"* → yes.

**Non-claim (explicit, PRD-stamped):**
- We do **NOT** assert multi-fixture hardness.
- We do **NOT** assert diverse-bug-class hardness.
- We do **NOT** assert external-corpus provenance.
- The hardness claim supported by 1.5 measurements is *exactly* "one synthetic fixture reliably defeats the production provider."

**Successor (explicit, PRD-stamped):**
- Multi-fixture / diverse-class / external-provenance hardness is owned by **§40.7.9 Phase 2 SWE-Bench-Pro** (1,865 problems across 41 repos with reproducible test harnesses + comparison vs Claude Sonnet 4.5 at 43.6% resolve rate).
- Phase 2 is structurally the right answer to "is the hardness ladder real."
- Phase 1.5 is structurally the right answer to "is the L2 path firing under bounded paid load at all."

---

## What this arc rules out for future synthetic fixture design

1. **"Hidden docstring + neutral function names is sufficient"** — false. Tested in problem_003-v2; 0% fail rate.
2. **"Spine naive-fix canaries prove fixture hardness"** — false. Structural trap presence ≠ empirical model failure. Canaries are *necessary but not sufficient*.
3. **"Multi-site bug categories are intrinsically hard"** — false. problem_003 (3-site None-handling) and problem_004 (single-function recursive merge) both at 0%. Only problem_002's *non-canonical multi-abstraction trap* worked.
4. **"More attempts will smooth out problem_003-v2's 0%"** — implausible. 5/5 pass with uniform pattern strongly suggests the model has the fix's training-data analog. Variance argument fails.

**What DOES seem to work (single-fixture sample size, conclusion tentative):**
- Wrapping a builtin abstraction (heapq) into a contract that requires the *internal storage shape to leak through a different method* (peek returning original priority while pop returns just item).
- Multi-abstraction *with conflicting* invariants between layers (storage layer says "use -priority"; API contract says "show original priority").

**Honest acknowledgment:** my designer-intuition this arc went 1/3 on synthetic fixtures (problem_002 only). Pattern-matching to LLM weakness is hard. The structural answer is **external corpora**, which is exactly Phase 2's mandate.

---

## File coordinates

- `scripts/validate_l2_exercise_corpus_hardness.py` — operator-paced CLI, schema v2, singleton-bootstrap docstring annotation, AST-pinned no-I/O `compute_acceptance_gate`
- `tests/governance/test_l2_exercise_corpus_hardness_validator.py` — 55 spine pins (taxonomy + retry + classification + gate semantics + floor + composition + report schema)
- `tests/governance/fixtures/l2_exercise_corpus/problem_00{1..4}/` — 4 fixtures with manifest + before + tests + reference fix
- `tests/governance/test_fixture_l2_exercise_problem_00{1..4}.py` — per-fixture spines (11/12/13/13 tests each) with embedded naive-fix canaries
- `backend/core/ouroboros/governance/l2_exercise_seed.py` — Phase 1.5.A substrate (loader, envelope builder, 3 FlagRegistry seeds)
- `backend/core/ouroboros/battle_test/harness.py` — boot hook (lazy import, attribute walker matching plugin pattern, fail-open try/except)
- `docs/architecture/OUROBOROS_VENOM_PRD.md` §40.7.10 — bootstrap-scope + non-claim + successor language
- `tests/governance/fixtures/l2_exercise_corpus/_hardness_report.json` — latest paid measurement evidence

**Tests: 170/170 cumulative Treefinement-arc spine green throughout. `_run_inner` sha256 still `9e881fdde25ec5b1` (no edits to repair_engine in this arc).**

**Next:** 1.5.E paid re-confirm (~$0.01–0.02 single-problem run) → memory addendum with the official acceptance report → Phase 2 SWE-Bench-Pro arc planning per [[project-v3-3-treefinement-l2-six-phase-arc]] design discipline.
