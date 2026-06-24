# Sovereign Cross-Repo Mutator — Design Spec (Multi-Repo Blast Radius Quarantine)

> **Arc.** The final frontier of sovereignty: let O+V autonomously generate → apply → PR mutations into **J-Prime (Mind)** and **Reactor-Core (Nerves)**, not just JARVIS (Body) — so the organism can heal and upgrade its entire nervous system. This is the **single highest-risk capability** in the architecture (it can rewrite its own Mind + training Nerves), so it ships behind a **Multi-Repo Blast Radius Quarantine**: AST-traced cross-repo context + an isolated Trinity integration sandbox + a Critical-Elevation operator lock.
> **Date:** 2026-06-23. Branch `worktree-sovereign-rsi-flywheel`.
> **HONEST FRAMING — this is ~90% WIRING, not a from-scratch build.** The deep dive found the Cross-Repo Scope Promoter already **implemented + gated default-OFF**, on a tested substrate. The spec documents how to compose existing pieces + add the three named guardrails; it does NOT redesign the cross-repo plumbing.

---

## 0. Reality grounding (reuse map from the deep dive)
**Already built + tested (reuse, do NOT rebuild):**
- `cross_repo_scope_promoter.py` — `JARVIS_CROSS_REPO_PROMOTER_ENABLED` (default OFF); on a boundary-crossing dependency cone it forces `RiskTier.APPROVAL_REQUIRED` (immutable Orange) — multi-repo mutations NEVER auto-apply. 11 tests.
- **Oracle** (`oracle.py`) — `_repos={jarvis,prime,reactor}`; `compute_blast_radius(node_id, max_depth)`, `get_dependents()`/`get_dependencies()`/`get_callers()` returning `NodeID{repo,file,name,type}` — **cross-repo AST graph already exists.**
- `multi_repo/` — `RepoRegistry.from_env` (resolves all 3 from env), `context_builder.build` (cross-repo context, token-budgeted), `blast_radius.analyze` (regex-only today), `repo_pipeline`, `repo_signature`.
- `saga_apply_strategy.py` — topological multi-repo apply + **compensating rollback** (all-or-nothing). `saga/cross_repo_verifier.py` — 3-tier verify (structure/compilation/integration).
- `risk_tier_floor.py` — strictest-wins 4-tier floor composition (`recommended_floor`). `orange_pr_reviewer.py` — async PR creation + loop-continuation human review. `adaptation/graduation_ledger.py` — unique-session success counter + `is_eligible` policy gate (the auto-commit graduation precedent). `auto_commit_graduation_gate.py` — git-log-signature scan pattern.
- `trinity_integrator.py` — `TrinityUnifiedOrchestrator` (Body+Mind+Nerves startup/health/crash-recovery). `docker-compose.{soak,prod,c2}.yml` reference the prime/reactor repo paths.

**The 3 real gaps (= this spec's build):** (G1) the Oracle's AST blast-radius isn't pulled into the *generation context window* (context_builder uses regex companions, not the Oracle's traced dependents); (G2) no ephemeral all-3-repo Docker integration test wired as a merge gate; (G3) no governance state that hard-halts the *first N* cross-repo PRs for operator approval regardless of test status.

## 1. Goals / Non-Goals
**Goals.** (G1) Before a cross-repo PR generates, **force the Oracle-traced cross-repo dependents into context** so the model sees the blast radius. (G2) Gate cross-repo merges on an **isolated Trinity integration sandbox** (all 3 repos in an ephemeral Docker network); handshake failure → `[SOVEREIGN YIELD: CROSS-REPO FRACTURE]`. (G3) A **CRITICAL_ELEVATION** governance state: the first `N` (default 5) cross-repo PRs **hard-halt for explicit operator approval regardless of test/CI status**, then graduate. (G4) Reuse-first, gated default-OFF, fail-soft/fail-CLOSED (cross-repo defaults to MORE restrictive), no hardcoding.
**Non-Goals.** Not rebuilding the promoter/saga/verifier/oracle/risk-tier/orange-PR/ledger. Not auto-merge of cross-repo PRs ever-by-default (even post-graduation stays Orange-reviewed — see §6). Not removing the Body-only default (this is an opt-in elevation). Not multi-repo for SAFE_AUTO/Green ops.

## 2. Architecture (the quarantine pipeline)
```
O+V op targets a symbol whose dependency cone (Oracle) crosses jarvis↔prime↔reactor
   │  master gate JARVIS_CROSS_REPO_MUTATION_ENABLED (default OFF) → else Body-only (today)
   ▼
[G1] AST Blast-Radius Context  ── Oracle.compute_blast_radius → pull EVERY downstream dependent
   │   (across repos) into the generation context window (token-budgeted) → model sees the radius
   ▼
GENERATE (cross-repo candidate) → forced RiskTier.APPROVAL_REQUIRED (existing promoter)
   ▼
SagaApply (existing, topological + compensating rollback) → CrossRepoVerifier (existing 3-tier)
   ▼
[G2] Isolated Trinity Sandbox  ── ephemeral Docker network (all 3 repos) → integration handshake
   │   PASS → continue;  FAIL → [SOVEREIGN YIELD: CROSS-REPO FRACTURE] → abort+rollback (op never lost)
   ▼
[G3] CRITICAL_ELEVATION  ── OrangePRReviewer creates the PR, but HARD-HALT at merge:
   │   first N cross-repo PRs require EXPLICIT operator approval regardless of CI status
   │   (GraduationLedger counts clean operator-approved merges)
   ▼  after N clean merges → graduate → normal cross-repo Orange flow (still PR-reviewed, no hard-halt)
```

## 3. Guardrail 1 — Dynamic AST Dependency Tracing (Blast Radius → context)
`multi_repo/context_builder.py` + a new `cross_repo_blast_context.py`.
- **Reuse the Oracle's existing cross-repo graph** (`compute_blast_radius`/`get_dependents`) — do NOT extend the regex `blast_radius.analyze`. When an op's target symbol is resolved, call `Oracle.compute_blast_radius(node_id, max_depth=JARVIS_CROSS_REPO_BLAST_DEPTH)` to get `transitively_affected: Set[NodeID]` across all repos.
- **Force the dependents into context:** for a mutation in `reactor`, collect every `NodeID` in `jarvis`/`prime` that transitively depends on it, read those files (via `RepoRegistry.read_file`), and inject them into the generation context window with an explicit `## CROSS-REPO BLAST RADIUS (you are mutating <repo>; these <N> downstream symbols in <repos> depend on it — do NOT break their contract)` header. Token-budgeted (reuse context_builder's budget; `JARVIS_CROSS_REPO_BLAST_TOKEN_BUDGET`), nearest-dependents-first if over budget (and log the truncation — never silently drop). 
- The model is thus *forced to recognize* the blast radius before proposing the mutation. Fail-soft: Oracle/registry error → fall back to the existing single-repo context + escalate the risk floor (fail-CLOSED). Gated by the master flag.

## 4. Guardrail 2 — Isolated Trinity Sandbox (Integration Gate)
`saga/trinity_integration_gate.py` (new), composed into the existing `cross_repo_verifier` AFTER structure/compilation pass.
- **Ephemeral, $0-to-minimal:** reuse the existing `docker-compose.*.yml` (which already reference the prime/reactor repo paths) to spin a **lightweight ephemeral Docker network** with all 3 repos *built from the candidate (mutated) tree*. Runs LOCALLY (dev machine) or on a cheap ephemeral CI runner — **no persistent infra**; `docker compose down -v` teardown in a `finally` (the same dead-man discipline — never leave containers running). Bounded by `JARVIS_TRINITY_SANDBOX_TIMEOUT_S`.
- **The integration handshake:** reuse `trinity_integrator`'s health/communication path — assert Body↔Mind↔Nerves still talk: jarvis can reach the mutated reactor/prime endpoints, the `model_ready`/Trinity-event handshake completes, health aggregator → all-green. (A unit-test-green cross-repo PR that breaks the *handshake* is exactly the fracture this catches.)
- **On failure:** emit `[SOVEREIGN YIELD: CROSS-REPO FRACTURE]` (reuse `emit_sovereign_yield(reason="CROSS-REPO FRACTURE")`) → abort the saga → compensating rollback (existing) → the op is sealed/terminal, never half-applied. Gated `JARVIS_TRINITY_SANDBOX_GATE_ENABLED` (default true when the master flag is on). Fail-CLOSED: sandbox infra error (Docker absent) → treat as FRACTURE (do not pass a gate you couldn't run) + escalate to operator.

## 5. Guardrail 3 — CRITICAL_ELEVATION operator lock
`critical_elevation.py` (new) + a new `risk_tier_floor` floor + a `GraduationLedger` policy.
- **New governance state `CRITICAL_ELEVATION`** — sits ABOVE `approval_required` in intent (a cross-repo PR is created via OrangePRReviewer, but the **merge is hard-halted**): the first `JARVIS_CROSS_REPO_CRITICAL_PR_COUNT` (default **5**) cross-repo PRs require **explicit operator approval to merge, regardless of CI/test/sandbox status** (even an all-green PR will NOT auto-merge). Reuse `risk_tier_floor.recommended_floor` (add a cross-repo floor that returns CRITICAL_ELEVATION when the op crosses repos AND the graduation counter < N).
- **Counter via GraduationLedger** (reuse the auto-commit-graduation precedent): a new policy `JARVIS_CROSS_REPO_PR_GRADUATION` with `required_clean_sessions=N`; count **operator-approved + merged** cross-repo PRs (new `outcome_class="cross_repo_pr_merged"`, git-log-signature scan via the `auto_commit_graduation_gate` pattern). Unique-PR dedup.
- **Graduation:** after N clean operator-approved merges, `is_eligible` flips → the CRITICAL_ELEVATION hard-halt lifts → cross-repo ops fall to the **normal Orange flow** (still an OrangePRReviewer PR + async human review per Manifesto §7 — NOT silent auto-merge; "full autonomy" = the capability is unlocked + the hard-halt is gone, but cross-repo stays human-visible by default; a SEPARATE explicit flag would be required to ever auto-merge cross-repo, out of scope here).
- Fail-CLOSED: any counter/ledger error → stay in CRITICAL_ELEVATION (never relax on an error). The governance-boundary floor (governance/ files → approval_required) and recursion-depth floor remain — CRITICAL_ELEVATION composes ON TOP (strictest-wins).

## 6. Cross-cutting / Invariants
- **Highest-risk → most-gated:** master `JARVIS_CROSS_REPO_MUTATION_ENABLED` default **OFF** (Body-only today, byte-identical). Cross-repo is opt-in, forced-Orange, sandbox-gated, operator-locked for the first N, and NEVER auto-merged by default even post-graduation.
- **Fail-CLOSED (not just fail-soft):** every guardrail degrades to MORE restrictive on error — context-trace fail → escalate floor; sandbox infra fail → treat as FRACTURE; counter fail → stay CRITICAL_ELEVATION. A cross-repo mutation can never become *less* gated through a failure.
- **Op-never-lost:** a FRACTURE/abort → compensating rollback (existing saga) → terminal, never half-applied across repos.
- **Reuse-first:** Oracle cross-repo graph (G1), docker-compose + trinity_integrator + cross_repo_verifier (G2), risk_tier_floor + OrangePRReviewer + GraduationLedger + auto_commit_graduation_gate (G3). New code = the 3 thin composition layers + the CRITICAL_ELEVATION state.
- **Cost:** ~$0. G1/G3 are pure logic. G2's sandbox is ephemeral local/CI Docker with `down -v` teardown — no persistent infra. (If ever run on a cloud CI runner, it's a short ephemeral job, pennies.)
- **No hardcoding:** blast depth, token budget, sandbox timeout, critical-PR count all env.

## 7. Phasing / build order
1. **G3 first (the lock) — `critical_elevation.py` + the floor + the ledger policy.** The operator lock must exist BEFORE any cross-repo write is possible, so the first mutation is already hard-halted. Tests. (Master flag still OFF.)
2. **G1 — the AST blast-radius context layer.** Compose Oracle.compute_blast_radius into the cross-repo generation context. Tests.
3. **G2 — the Trinity integration sandbox gate.** Ephemeral Docker compose + handshake assert + FRACTURE yield + teardown. Tests (mock Docker; a real local sandbox run is operator-gated).
4. **Integration + final review** (Opus — this mutates the Mind + Nerves; the review must confirm fail-CLOSED + op-never-lost + master-OFF byte-identical + the hard-halt is un-bypassable by test-pass).
5. **First real cross-repo PR:** operator-gated, into a LOW-risk target first (e.g. a doc/test in reactor-core), through the full quarantine, to prove the pipeline before anything touches Mind/Nerves logic.

## 8. Tests
- **G1:** Oracle blast-radius dependents (cross-repo) forced into context with the header; token-budget truncation logged not silent; Oracle error → single-repo fallback + floor escalation (fail-closed).
- **G2:** sandbox spins all 3 (mocked), handshake-pass → gate passes; handshake-fail → `[SOVEREIGN YIELD: CROSS-REPO FRACTURE]` + rollback + teardown-in-finally; Docker-absent → treated as FRACTURE (fail-closed); timeout bounded.
- **G3:** first N cross-repo ops → CRITICAL_ELEVATION (hard-halt, no merge even with all-green CI); operator-approval path; counter increments only on approved+merged; graduation at N flips to normal Orange; counter error → stay CRITICAL_ELEVATION; governance-boundary + recursion floors still compose (strictest-wins).
- **Master OFF:** `JARVIS_CROSS_REPO_MUTATION_ENABLED=false` → Body-only, byte-identical (no context-trace, no sandbox, no elevation; cross-repo ops never created).
- **Cross-cutting (load-bearing):** a static proof that NO failure path makes a cross-repo mutation *less* gated (fail-closed) and that auto-merge of a cross-repo PR is unreachable without the (out-of-scope) separate flag.

## 9. Open decisions (operator review)
1. **Critical-PR count `N`:** default **5** (your spec). Higher (more proving) / lower?
2. **Post-graduation autonomy:** spec keeps cross-repo at **Orange (PR + human review) even after graduation** — "full autonomy" unlocks the capability + removes the hard-halt, but never silent auto-merge of Mind/Nerves changes (a separate explicit flag would be needed). Confirm this is the intended ceiling, or do you want true auto-merge after N?
3. **Sandbox venue:** local Docker (dev machine, $0) vs ephemeral cloud CI runner (pennies, but needs setup). *Spec assumes local-first.*
4. **First-target policy:** spec recommends the first real cross-repo PR target a LOW-risk file (doc/test) in reactor-core before any Mind/Nerves *logic*. Confirm.
