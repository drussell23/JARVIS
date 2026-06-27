---
title: Phase B Subagent Roadmap (2026-04-18)
modules: []
status: merged
source: project_phase_b_subagent_roadmap.md
---

# Phase B Subagent Roadmap (2026-04-18)

**Entry point for the next session.** Phase 1 is graduated (see
`project_phase_1_subagent_graduation.md`). Phase B extends the
subagent architecture with three new types. The infrastructure is
built — Phase B is cognitive implementation, not plumbing.

## What's inherited from Phase 1 (don't rebuild)

All three Phase B agents inherit these 8 primitives. They are
production-tested and regression-pinned (79 tests). Do not rebuild:

1. **SubagentOrchestrator** — fan-out via `asyncio.TaskGroup`/`gather`, task isolation, typed result aggregation.
2. **Iron Gate diversity enforcement** — `diversity=3` floor applies generically; per-type thresholds adjustable via env.
3. **Five-budget-layer envelope** — pool worker ceiling / orchestrator gen_timeout / fallback `_max_cap` / DW stall budget / hard-kill wrapper. Formula: `base + MAX_PARALLEL_SCOPES × PRIMARY_PROVIDER_TIMEOUT_S + synthesis_reserve`. Tune per-type if needed; do NOT re-derive.
4. **Hard-kill wrapper** — `asyncio.wait({task}, timeout=soft+30)` at `providers.py:5257`. Generalize to any external provider stream call in Phase B.
5. **Read-only contract** — held differently per subagent type. EXPLORE + PLAN inherit the Advisor-bypass + Rule 0d + APPLY short-circuit + parser short-circuit chain from Phase 1 (parent op marked `is_read_only`, model goes through Venom, Rule 0d refuses mutation tools, APPLY short-circuits, parser refuses candidate shape). **REVIEW holds the contract structurally, not via reuse** — readiness audit 2026-04-20 confirmed that `AgenticReviewSubagent.review()` runs pure deterministic Python (SemanticGuardian + ast walks + optional injected mutation_runner) and never enters the tool_executor or emits a generation candidate. The contract holds because there is no mutation surface in REVIEW's path at all — not because Rule 0d refuses it. GENERAL needs the full Phase 1 chain plus the Semantic Firewall (§5).
6. **Weaponized prompt schema pattern** — replace code-gen schema with a `<CRITICAL_SYSTEM_DIRECTIVE>` when semantic expectations differ. Works for REVIEW verdicts and PLAN DAG output.
7. **Parser short-circuit** — `_parse_generation_response` already has the `ctx.is_read_only` branch. Extend with per-type branches if REVIEW/PLAN need their own validator shape.
8. **Observability pipeline** — `CommProtocolCommSink` + `LedgerSubagentSink` carry any `SubagentType` value. Per-type observability requires only new enum variants and SerpentFlow render methods.

**Inheritance matrix:**

| Primitive | REVIEW | PLAN | GENERAL |
|---|---|---|---|
| Orchestrator fan-out | ✓ reuse | ✓ reuse (sequential by default, optional parallel for independent plan branches) | ✓ reuse |
| Iron Gate diversity | ✓ reuse | ✗ different signal (DAG coverage, not tool diversity) | ✓ reuse |
| 5-layer budget envelope | ✓ reuse | ✓ reuse (shorter budgets — PLAN is faster than fan-out) | ✓ reuse (tighter — untrusted scope) |
| Hard-kill wrapper | ✓ reuse | ✓ reuse | ✓ **mandatory** — GENERAL's attack surface is largest |
| Read-only contract | ✓ **structural** (REVIEW has no tool_executor entry and no candidate emission — contract held by isolation, not Rule 0d reuse) | ✗ PLAN is pure analysis, doesn't mutate, but output IS consumed by GENERATE so not "read-only" in the APPLY-short-circuit sense | partial — depends on invocation scope |
| Weaponized prompt schema | ✓ verdict schema | ✓ DAG schema | ✗ varies per invocation |
| Parser short-circuit | ✓ REVIEW verdict parser | ✓ DAG parser (not short-circuit — must validate) | varies |
| Observability pipeline | ✓ reuse | ✓ reuse | ✓ reuse with elevated logging |

## REVIEW Agent (Manifesto §6 — Execution Validation)

**What it is:** a subagent spawned in parallel to the VALIDATE phase. Given a candidate file (pre-APPLY), it reasons about semantic integrity against the generation intent, AST-walks for structural red flags, and optionally triggers mutation testing on critical paths. Returns a typed verdict: `APPROVE` / `APPROVE_WITH_RESERVATIONS` / `REJECT(reasons)`.

**Why it's needed:** current VALIDATE is test-run + LSP + Iron Gate — all mechanical checks. None of them answer "does this code mean what the goal said?" REVIEW closes that gap without blocking the main generation thread (parallel dispatch, Phase 1 primitive).

### Derek's constraint: §6 Execution Validation

> REVIEW cannot blindly approve code. It must be explicitly wired into the AST parser and the mutation testing suite to verify semantic integrity before rendering a verdict.

**Translation into implementation:**

1. **AST wire-up (mandatory, not optional).** REVIEW's verdict must be structurally derived from a concrete set of AST analyses, not from Claude's prose intuition. AST checks fall into two groups:

   **Graduation-required (Slice 1a + 1b):**
   - `SemanticGuardian` pattern set — 10 patterns already wired at orchestrator post-VALIDATE. REVIEW runs the same patterns with richer reasoning available. The `function_body_collapsed` pattern is the silent-stub detector (catches "same name, body rewritten to pass/raise/single-return").
   - Function-name loss — counts functions present in the old file but absent in the new. Catches silent deletion. Together with `function_body_collapsed` these cover the classes of silent stubbing we've actually seen in practice.
   - Import-graph delta — new imports introduced (narrative only); removed imports still referenced (already covered by SemanticGuardian's `removed_import_still_referenced`).

   **Future enhancements (scoped out of Slice 1b — do not block graduation):**
   - Full AST-tree hash diffing — hash each function's body subtree (`ast.dump`) and compare old→new for identically-named functions. Would catch drift beyond the collapse-to-trivial case. Add only if empirical telemetry shows the `function_body_collapsed` pattern misses material cases; otherwise this is "lazy exploration" (Manifesto §6) disguised as completeness.
   - Control-flow-graph edit distance — unusually large changes warrant higher scrutiny. Non-trivial to implement; park until REVIEW has graduated and we have data on false-negative rates.
   - Exported-symbol delta — public API changes must surface in verdict. Reasonable standalone slice once graduation is complete; sequenced after FutureAST-hash to decide whether they share a traversal.

2. **Mutation testing hook.** When the candidate touches an allowlisted critical path (same list as `mutation_gate.py`), REVIEW runs a sampled mutation test against the new candidate BEFORE verdict rendering. Low mutation score → verdict downgrade from APPROVE to APPROVE_WITH_RESERVATIONS (or REJECT below a hard floor).
   - Reuse `mutation_tester.py` + `mutation_gate.py` allowlist.
   - Sample count scales with op complexity — env-tunable `JARVIS_REVIEW_MUTATION_SAMPLE_COUNT` (default 5).
   - Mutation run is wrapped in the hard-kill pattern (mutation testing can hang on pathological source).

3. **Verdict structure.** Mandatory fields, no free-form prose as the verdict itself (prose is a `rationale` field):
   ```
   {
     "verdict": "approve" | "approve_with_reservations" | "reject",
     "semantic_integrity_score": 0.0-1.0,  # derived from AST signals
     "ast_findings": [{category, severity, detail}, ...],
     "mutation_score": 0.0-1.0 | null,  # null if path not in allowlist
     "reservations": [str, ...],  # forced non-empty for approve_with_reservations
     "reject_reasons": [str, ...],  # forced non-empty for reject
     "rationale": "<one paragraph, ≤ 800 chars>"
   }
   ```

### REVIEW graduation arc

1. **Pre-arc infrastructure:** `SubagentType.REVIEW` enum variant, Rule 0c extension in policy engine, REVIEW-specific prompt schema, AST integration adapter, mutation testing hook, regression spine minimum ~15 cases.
2. **Battle test target:** an op that historically produced subtle logical bugs that tests missed. Candidates in the OUROBOROS.md breakthrough log — the Session U `infra/test` flakiness or the Session 12 cost-cap overrun could be re-targeted to exercise REVIEW.
3. **Graduation threshold:** 3 consecutive sessions where REVIEW's verdict agrees with the actual APPLY+VERIFY outcome on ≥ 90% of ops. "Agrees" = REJECT → VERIFY fail, APPROVE → VERIFY pass, APPROVE_WITH_RESERVATIONS → correlated with L2 engagement.

### REVIEW anti-patterns (explicit "do not")

- ❌ Don't let REVIEW modify candidate content. It's a verdict agent, not a rewriter. If REVIEW could suggest changes, it would bypass the orchestrator's GENERATE→VALIDATE→GATE contract.
- ❌ Don't run REVIEW in-band on the GENERATE thread. It must be parallel — the whole point of Phase 1's fan-out is that REVIEW can take its time without blocking the main synthesis.
- ❌ Don't mark an op "approved" on REVIEW verdict alone. REVIEW is advisory input to the existing APPROVE phase, not a replacement. The orchestrator combines REVIEW verdict + existing Iron Gate signals + risk tier to decide approval.

## PLAN Agent (Manifesto §2 — Directed Acyclic Graph)

**What it is:** a subagent that replaces the current single-shot `plan_generator.py`. Given an op description + target files, it produces a strict DAG of implementation steps where every node declares its `dependency_ids`, `owned_paths`, `barrier_id`, and `acceptance_tests`. The GENERATE phase consumes the DAG instead of a flat plan.

**Why it's needed:** today `plan_generator.py` emits a JSON object with `ordered_changes: [...]` — a list, not a graph. The model implicitly serializes multi-file work even when files are independent. PLAN as a DAG-producing agent unlocks actual parallel GENERATE across independent nodes (which the existing `execution_graph` schema `2d.1` already supports — the infrastructure exists, the planning layer doesn't use it).

### Derek's constraint: §2 DAG

> PLAN cannot output flat task lists; it must output strict, mathematically verifiable dependency DAGs.

**Translation into implementation:**

1. **Output schema is a DAG, not a list.** Reuse the existing `_SCHEMA_VERSION_EXECUTION_GRAPH` (`2d.1`) — already defined in `providers.py` for cross-repo execution graphs. PLAN produces this shape directly; the GENERATE phase already knows how to consume it. Key contracts:
   - `units[*]` each have a `unit_id`, `dependency_ids: [unit_id]`, `barrier_id` (optional convergence point), `owned_paths` (must be disjoint for parallel units), `acceptance_tests`.
   - PLAN's output is run through `_validate_dag()` (exists in `op_context.py:644`) — rejects cycles, unreachable nodes, self-references.
   - A flat output (single unit with no edges) is valid ONLY for single-file ops; multi-file DAGs with no edges fail schema validation (models tend to emit degenerate DAGs to sidestep the contract — the validator catches this).

2. **Mathematical verification before injection.** PLAN's output must pass:
   - Acyclicity (`networkx.is_directed_acyclic_graph` or equivalent).
   - Reachability (every unit reachable from some root — no islanded sub-DAGs).
   - Owned-path disjointness (parallel branches cannot share `owned_paths`; shared paths require a `barrier_id` forcing convergence).
   - Acceptance-test coverage (every unit has at least one `acceptance_test` OR a documented "no-test" rationale).

3. **PLAN subagent is NOT read-only.** Its output is consumed by GENERATE; the `is_read_only` contract does not apply. But PLAN also does not *mutate* — it has no write tool calls. Its contract is "inert on filesystem, consequential on downstream phases". This is a new intermediate category. Treat it as "effects contained to ctx mutation" and keep the hard-kill wrapper + Iron Gate diversity.

### PLAN graduation arc

1. **Pre-arc infrastructure:** `SubagentType.PLAN` enum, Rule 0c extension, DAG-specific prompt schema with the `2d.1` shape, hook into orchestrator PLAN phase (replace direct `plan_generator.py` invocation with a PLAN subagent dispatch), `_validate_dag()` integration at the boundary, regression spine ~20 cases (DAG validity, cycle rejection, reachability, disjoint ownership, barrier semantics).
2. **Battle test target:** a genuinely parallel multi-file op. The Trinity cartography task happened to be 3 disjoint files — ideal PLAN target. Other candidates: multi-sensor test generation (like the Session W four-file test module landing).
3. **Graduation threshold:** 3 consecutive sessions where (a) PLAN's DAG validates, (b) GENERATE exploits at least one parallel edge (i.e., two units with disjoint `owned_paths` execute concurrently), (c) APPLY+VERIFY passes.

### PLAN anti-patterns

- ❌ Don't let PLAN generate code. It plans; GENERATE generates. Breaking this contract re-introduces the single-shot generator we're replacing.
- ❌ Don't short-circuit the DAG validator. A "mostly valid" DAG (cycle that you think is benign, disjointness violation that you think is fine) breaks parallel GENERATE catastrophically. The validator is the boundary; it is non-negotiable.
- ❌ Don't allow PLAN to modify `target_files`. PLAN refines *how* to change them, not *what* to change. The orchestrator owns target-file scoping.

## GENERAL Agent (Manifesto §5 — Semantic Firewall)

**What it is:** a subagent for tasks that don't fit EXPLORE (read-only cartography), REVIEW (verdict on candidate), or PLAN (DAG planning). Examples: "rewrite this paragraph in the docs", "investigate why tests occasionally flake on Tuesdays", "summarize the last 10 operations and identify a pattern". Open-ended. The most dangerous category.

**Why it's risky:** GENERAL has no structural constraint binding it to a read-only contract or a specific output schema. The subagent's prompt + context can contain untrusted content (upstream file contents, external data, user-supplied goals). Without strict boundaries, GENERAL is a prompt-injection entry point into the organism.

### Derek's constraint: §5 Semantic Firewall

> GENERAL is heavily sandboxed. Because it lacks a specific domain constraint, it is the most vulnerable to prompt injection or context drift. Strict boundary conditions must gate its invocation.

**Translation into implementation:**

1. **Tier -1 input sanitization.** Every input field passed to a GENERAL subagent is treated as untrusted. Reuse `backend.core.secure_logging.sanitize_for_log` + the bridge's secret-shape redaction. Specifically:
   - `goal` — sanitized; forbidden patterns (ignore-previous-instructions, role-override) rejected at dispatch.
   - `scope_paths` — normalized against repo root; path traversal / symlink escape refused.
   - Any `evidence` payload — stripped of control characters, truncated to env-tunable max.
   - Sanitization failures raise `SubagentSemanticFirewallRejection` at dispatch, not mid-run.

2. **Explicit invocation boundary conditions.** GENERAL dispatch requires ALL of:
   - `operation_scope` — concrete file paths or directory globs. No "the whole repo" scope allowed.
   - `max_mutations` — integer, forced `0` for read-only invocations, capped at env-tunable ceiling for mutating.
   - `allowed_tools` — explicit subset of the Venom tool list. Default is read-only tools only (`READONLY_TOOL_MANIFEST`). Mutating tools require explicit per-call grant.
   - `invocation_reason` — one-sentence rationale from the caller, ≤ 200 chars, sanitized and logged.
   - `parent_op_risk_tier` — inherited from parent; parent must be at or above `NOTIFY_APPLY` for GENERAL to be allowed (SAFE_AUTO ops cannot dispatch GENERAL — too broad a blast radius for auto-applied changes).

3. **Recursion ban.** GENERAL cannot dispatch GENERAL. One level deep maximum. Reuse parent `SubagentContext` to detect recursive dispatch attempts and raise `SubagentRecursionRejection` at dispatch.

4. **Output quarantine.** GENERAL's output is returned to the caller wrapped in a `<general_subagent_output untrusted="true">` fence with the same Tier -1 sanitizer applied on the return path. The caller (typically orchestrator or parent subagent) treats the output as advisory, not authoritative — consistent with `ConversationBridge`'s untrusted-stack discipline.

5. **Elevated observability.** Every GENERAL dispatch emits:
   - `GENERAL dispatch op=X sanitized_goal_hash8=XXXX invocation_reason=Y allowed_tools=N operation_scope=[...] parent_risk=Z`
   - Every tool call inside a GENERAL subagent logged at INFO (not DEBUG) with `via=general_subagent` tag.
   - POSTMORTEM always fires for GENERAL ops regardless of outcome — the audit trail matters more than for deterministic types.

### GENERAL graduation arc

1. **Pre-arc infrastructure:** `SubagentType.GENERAL` enum, Rule 0c extension with the strict boundary-condition checks, semantic firewall sanitizer module (can extend `sanitize_for_log` or fork), `SubagentSemanticFirewallRejection` + `SubagentRecursionRejection` exceptions, regression spine ~30 cases (firewall rejections, recursion detection, boundary condition enforcement, tool-subset honoring, output quarantine).
2. **Battle test target:** a contrived task with a known prompt-injection payload embedded in the goal. The firewall must reject it before dispatch. Then a clean general-purpose task (e.g., "summarize the last 5 battle test sessions into a recurring-pattern report") must complete successfully.
3. **Graduation threshold:** 3 consecutive sessions where (a) the firewall correctly rejects known-bad inputs, (b) GENERAL completes clean tasks, (c) no tool call leaks outside the explicit `allowed_tools` grant.

### GENERAL anti-patterns

- ❌ Don't default `allowed_tools` to the full Venom set. Default is `READONLY_TOOL_MANIFEST`. Callers must explicitly upgrade, which forces the decision into visible code.
- ❌ Don't allow GENERAL to inherit the parent's `is_read_only` stamp automatically. The parent may be read-only by *op* contract; GENERAL's specific invocation needs its own read-only designation based on `max_mutations=0`.
- ❌ Don't log `goal` at INFO without sanitization. Prompt injection attempts often include shell-escape sequences or control-character floods designed to pollute operator telemetry.
- ❌ Don't skip the firewall on "trusted" call sites. The whole point of §5 is that trust is asserted at the boundary, not assumed by module. If GENERAL is invoked from orchestrator.py, it still runs the firewall.

## Ordering

**Recommended build sequence:**

1. **REVIEW first.** Smallest structural surface area — read-only, consumes existing primitives almost 1:1. AST + mutation hooks are wiring, not architecture. Good shakedown for Phase B toolchain.
2. **PLAN second.** Replaces an existing subsystem rather than adding one. Requires orchestrator PLAN-phase surgery but the DAG schema already exists. Risk is localized to the PLAN→GENERATE boundary.
3. **GENERAL last.** Largest attack surface, most novel infrastructure (semantic firewall, recursion detection, elevated observability). By the time GENERAL is built, REVIEW and PLAN graduation arcs have validated the 5-budget-layer envelope under three new workload profiles — tightens the sanity base for GENERAL's first run.

Each graduation arc is its own 3-consecutive-clean-session chain per Manifesto §6. Do not flip defaults until the arc completes.

## Global anti-patterns (Phase 1 lessons applied universally)

- ❌ Don't trust `asyncio.wait_for` for provider stream calls. Use the `asyncio.wait({task}, timeout=soft+30)` hard-kill pattern from `providers.py:5257`.
- ❌ Don't rely on the orchestrator stamping intent mid-pipeline. Intake-time stamps propagate to the pool worker's ceiling selection; orchestrator-time stamps don't. New intake entry points must call the appropriate inference function.
- ❌ Don't add budget layers ad-hoc. Use the 5-layer envelope formula (`base + MAX_PARALLEL_SCOPES × PRIMARY_PROVIDER_TIMEOUT_S + synthesis_reserve`) for every new subagent type. Per-type overrides go through dedicated env vars, not constant edits.
- ❌ Don't bypass Rule 0d for "trusted" subagent types. Every subagent type that is read-only by contract MUST be denied mutation tools at the policy engine. The contract is structural, not per-type.
- ❌ Don't let a new subagent type graduate without a 3-consecutive-clean-session arc. Manifesto §6 applies to every type independently.

## Where to start the next session

1. Read this file (`MEMORY.md` indexes it).
2. Read `project_phase_1_subagent_graduation.md` for the architectural genealogy.
3. Start with REVIEW: extend `SubagentType` enum, scaffold `AgenticReviewSubagent` following the `AgenticExploreSubagent` pattern, extend Rule 0c, extend schema-swap for verdict output, write regression spine, run 3-session arc.
4. Do not break Phase 1. The `JARVIS_SUBAGENT_DISPATCH_ENABLED=true` default stays; EXPLORE continues working; REVIEW is additive.
5. When REVIEW graduates, update this file's status tracker and start PLAN.

Session-continuity hint: the `LastSessionSummary` mechanism will replay the previous session's terminal state into the next session's prompt. If the last session ended on a REVIEW graduation, the next session picks up mid-Phase-B automatically.

## Status tracker (update as we go)

| Subagent type | Phase | Status | Last updated |
|---|---|---|---|
| EXPLORE | 1 | ✅ Graduated production | 2026-04-18 |
| REVIEW | B | ✅ Graduated production (`JARVIS_REVIEW_SUBAGENT_SHADOW=true` by default; observer-only contract, FSM not gated by verdict) | 2026-04-20 |
| PLAN | B | ✅ Graduated production — shadow (`JARVIS_PLAN_SUBAGENT_SHADOW=true`) + exploit (`JARVIS_PLAN_EXPLOIT_ENABLED=true`) both default-on. Synthetic proof showed 4× wall-clock speedup (4000ms serial → 1001ms fanned out at concurrency=4) on 4-unit parallel DAG; live session confirmed read_only/BG fallback path runs legacy byte-identically. | 2026-04-20 |
| GENERAL | B | ✅ **Infrastructure graduated** — Semantic Firewall (§5) + dispatch + Layer-2 re-validation + recursion ban + output quarantine fence + hard-kill wrapper all production. 48/48 tests green. | 2026-04-20 |
| GENERAL | C Slice 1a+1b | ✅ **LLM-driven execution body graduated** — `general_driver.py` shipped, `build_llm_general_factory` wired at `governed_loop_service.py`, `JARVIS_GENERAL_LLM_DRIVER_ENABLED` default flipped `false`→`true` after 3-test live battle matrix (Happy Path / Boundary Push / Tool Violation) proved allowlist + scope + mutation cap against the real Claude API. 32/32 tests green (25 driver + 7 wire-in). Latent tickets (6 `generate_text` / 8 max_mutations COUNT / 9 hard_kill records) tracked in `project_phase_b_step2_deferred.md`. | 2026-04-20 |
| RESEARCH | (deferred) | scaffolding only | — |
| REFACTOR | (deferred) | scaffolding only | — |
