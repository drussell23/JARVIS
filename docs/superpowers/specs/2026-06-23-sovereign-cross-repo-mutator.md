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
**Goals.** (G1) Before a cross-repo PR generates, **force the Oracle-traced cross-repo dependents into context** so the model sees the blast radius. (G2) Gate cross-repo merges on an **isolated, air-gapped Trinity integration sandbox** (all 3 repos in an ephemeral Docker network with an **egress sinkhole** — no live DW/Claude/GCP calls); handshake failure → `[SOVEREIGN YIELD: CROSS-REPO FRACTURE]`. (G3) An **Adaptive Trust Ledger** driving a **CRITICAL_ELEVATION** state: graduate the operator hard-halt only after a *streak* of consecutive **zero-rollback** cross-repo integrations whose cumulative **AST-complexity weight** clears a **dynamic** threshold (NO magic N); ANY rollback / FRACTURE resets the streak to zero. (G4) **Immutable Orange Protocol:** Mind (prime) + Nerves (reactor) mutations are **PERMANENTLY human-merged (Orange)** — a structural Sovereign Law that NO flag or graduation can disable; only the Body may ever reach silent auto-merge. (G5) Reuse-first, gated default-OFF, **fail-CLOSED** (cross-repo degrades to MORE restrictive), no hardcoding — *except* the immutable Orange floor (the one safety constant that MUST be hardcoded, by design).
**Non-Goals.** Not rebuilding the promoter/saga/verifier/oracle/risk-tier/orange-PR/ledger. **Never auto-merge of Mind/Nerves PRs — structurally impossible, by any flag, forever (§6 Immutable Orange Protocol).** Not removing the Body-only default (cross-repo is an opt-in elevation). Not multi-repo for SAFE_AUTO/Green ops.

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

## 4. Guardrail 2 — Isolated AIR-GAPPED Trinity Sandbox (Integration Gate)
`saga/trinity_integration_gate.py` (new), composed into the existing `cross_repo_verifier` AFTER structure/compilation pass.
- **Ephemeral, $0:** reuse the existing `docker-compose.*.yml` (which already reference the prime/reactor repo paths) to spin a **lightweight ephemeral Docker network** with all 3 repos *built from the candidate (mutated) tree*. Runs LOCALLY (dev machine) — **no persistent infra**; `docker compose down -v` teardown in a `finally` (the same dead-man discipline — never leave containers running). Bounded by `JARVIS_TRINITY_SANDBOX_TIMEOUT_S`.
- **AIR-GAP / EGRESS SINKHOLE (the operator constraint — never pollute live infra):** the compose network is declared **`internal: true`** (Docker — no route to the host/internet); the integration containers **cannot reach DoubleWord, Claude, or GCP**. A tiny **synthetic-mock egress responder** container (`trinity_sandbox_egress_mock`) is the ONLY reachable "external" endpoint — the providers' base URLs (DW/Claude/GCP metadata) are env-overridden inside the sandbox to point at it, returning deterministic synthetic responses. This proves the cross-repo AST/handshake logic with ZERO risk of live API calls / infra pollution / spend. Fail-CLOSED: if egress isolation cannot be asserted (the `internal` network or the mock isn't up, or a container can resolve a real provider host), the gate **refuses to run and treats it as a FRACTURE** — never run an integration test that could touch live infra.
- **The integration handshake:** reuse `trinity_integrator`'s health/communication path — assert Body↔Mind↔Nerves still talk *through the sinkhole*: jarvis can reach the mutated reactor/prime endpoints, the `model_ready`/Trinity-event handshake completes, health aggregator → all-green, all provider calls resolved to the mock. (A unit-test-green cross-repo PR that breaks the *handshake* is exactly the fracture this catches.)
- **On failure:** emit `[SOVEREIGN YIELD: CROSS-REPO FRACTURE]` (reuse `emit_sovereign_yield(reason="CROSS-REPO FRACTURE")`) → abort the saga → compensating rollback (existing) → the op is sealed/terminal, never half-applied. Gated `JARVIS_TRINITY_SANDBOX_GATE_ENABLED` (default true when the master flag is on). Fail-CLOSED: sandbox/Docker absent → treat as FRACTURE + escalate to operator.

## 5. Guardrail 3 — Adaptive Trust Ledger + CRITICAL_ELEVATION + Immutable Orange
`critical_elevation.py` + `cross_repo_trust_ledger.py` (new) + a new `risk_tier_floor` floor. **Build FIRST.**

### 5.1 CRITICAL_ELEVATION governance state
A new state ABOVE `approval_required`: a cross-repo PR is created via OrangePRReviewer, but the **merge is HARD-HALTED for explicit operator approval regardless of CI/test/sandbox status** — even an all-green PR will NOT auto-merge while elevated. Reuse `risk_tier_floor.recommended_floor` (add a cross-repo floor returning CRITICAL_ELEVATION when the op crosses repos AND the Trust Ledger says not-yet-graduated). Composes ON TOP of the existing governance-boundary + recursion-depth floors (strictest-wins).

### 5.2 The Adaptive Trust Ledger (rejects the magic N — streak + complexity-weighted, dynamic)
`cross_repo_trust_ledger.py` — graduation is NOT a fixed count. It is **earned trust**:
- **Consecutive-streak, zero-rollback:** the ledger tracks a *current streak* of cross-repo integrations that landed cleanly (operator-approved + merged + the sandbox PASSED + no compensating rollback). **ANY rollback OR `[SOVEREIGN YIELD: CROSS-REPO FRACTURE]` resets the streak (and the accumulated trust) to ZERO** — trust is consecutive, not cumulative-forever.
- **AST-complexity-weighted trust:** each clean merge adds `trust += complexity_weight(pr)`, where `complexity_weight` is derived from the Oracle blast-radius of the merged change (number of cross-repo dependents touched + AST node count of the diff + boundary-crossing depth) — reuse `Oracle.compute_blast_radius` + an AST node count (`ast.walk`) + `estimate_body_chars`. A trivial one-liner earns a little trust; a deep multi-dependent mutation earns a lot. **You cannot graduate by merging 100 trivial PRs.**
- **Dynamic threshold:** graduation requires `streak_trust >= adaptive_threshold`, where the threshold **scales with the complexity the system is attempting** — e.g. `adaptive_threshold = JARVIS_TRUST_BASE × max(observed_pr_complexity in the streak)`, so the bar rises with ambition: to be trusted with *complex* cross-repo mutations it must have *demonstrated* clean streaks at comparable complexity. All coefficients env (`JARVIS_TRUST_BASE`, weighting factors) — NO hardcoded count anywhere.
- Reuse the GraduationLedger's durable-JSONL + unique-PR-dedup + git-log-signature-scan machinery (the `auto_commit_graduation_gate` pattern) for the underlying record; the Trust Ledger is the streak/weight/threshold logic ON TOP. `trust_state(repo) -> {streak, trust, threshold, graduated: bool}`.

### 5.3 The Immutable Orange Protocol (the permanent ceiling — Sovereign Law)
**Mutations targeting `prime` (Mind) or `reactor` (Nerves) can NEVER reach auto-merge — structurally, by any flag, forever.**
- Graduation (5.2) for a prime/reactor target only lifts the CRITICAL_ELEVATION **hard-halt down to normal ORANGE** (OrangePRReviewer PR + async human review per Manifesto §7) — **never below `approval_required`.** The Trust Ledger unlocks "O+V autonomously writes + tests + sandbox-validates + packages the PR," but the **merge is permanently human**.
- Implemented as a **hardcoded immutable floor** in `risk_tier_floor` keyed on the target repo: `target_repo in {prime, reactor} → floor = approval_required`, evaluated with NO env override and NO graduation bypass (the ONE intentional hardcode — a safety constant). Only `jarvis` (Body) targets may ever graduate below Orange to silent auto-merge.
- **AST-pinned invariant + static test:** a regression proving that for prime/reactor targets the resolved tier is `>= approval_required` under EVERY combination of flags, graduation state, and trust level — un-disableable. "It can write its own brain; it can never merge its own brain."
- Fail-CLOSED everywhere: any ledger/floor error → stay in CRITICAL_ELEVATION (Body) / Orange (Mind+Nerves); never relax on an error.

## 6. Cross-cutting / Invariants
- **Highest-risk → most-gated:** master `JARVIS_CROSS_REPO_MUTATION_ENABLED` default **OFF** (Body-only today, byte-identical). Cross-repo is opt-in, forced-Orange, air-gap-sandbox-gated, and operator-locked via the Adaptive Trust Ledger.
- **Immutable Orange Protocol (Sovereign Law):** Mind (prime) + Nerves (reactor) targets are PERMANENTLY `>= approval_required` — a hardcoded floor no flag/graduation/trust-level can disable. Only Body (jarvis) can ever auto-merge. It can write its own brain; it can never merge its own brain.
- **Fail-CLOSED (not just fail-soft):** every guardrail degrades to MORE restrictive on error — context-trace fail → escalate floor; egress/sandbox not air-gapped or Docker absent → treat as FRACTURE; ledger fail → stay elevated/Orange. A cross-repo mutation can never become *less* gated through a failure.
- **Op-never-lost:** a FRACTURE/abort → compensating rollback (existing saga) → terminal, never half-applied across repos.
- **Reuse-first:** Oracle cross-repo graph (G1), docker-compose + trinity_integrator + cross_repo_verifier (G2), risk_tier_floor + OrangePRReviewer + GraduationLedger + auto_commit_graduation_gate (G3). New code = the 3 thin composition layers + the CRITICAL_ELEVATION state + the Adaptive Trust Ledger + the air-gap egress mock.
- **Cost:** ~$0. G1/G3 are pure logic. G2's sandbox is ephemeral local Docker on an `internal` (air-gapped) network with `down -v` teardown — no persistent infra, no live API calls (the sinkhole guarantees zero spend/pollution).
- **No hardcoding — ONE exception by design:** blast depth, token budget, sandbox timeout, trust coefficients all env. The single intentional hardcode is the **Immutable Orange floor** for prime/reactor (a safety constant that MUST NOT be env-disableable).

## 7. Phasing / build order
1. **G3 first (the lock) — `critical_elevation.py` + `cross_repo_trust_ledger.py` + the immutable-Orange floor.** The operator lock + the permanent Mind/Nerves ceiling must exist BEFORE any cross-repo write is possible, so the first mutation is already hard-halted AND prime/reactor are already un-auto-mergeable. Tests. (Master flag still OFF.)
2. **G1 — the AST blast-radius context layer.** Compose `Oracle.compute_blast_radius` into the cross-repo generation context. Tests.
3. **G2 — the air-gapped Trinity sandbox gate.** Ephemeral `internal`-network Docker compose + egress-mock sinkhole + handshake assert + FRACTURE yield + teardown. Tests (mock Docker; a real local sandbox run is operator-gated).
4. **Integration + final review** (Opus — this mutates the Mind + Nerves; the review must confirm fail-CLOSED + op-never-lost + master-OFF byte-identical + the Immutable Orange floor is un-bypassable + the Trust Ledger resets on rollback).
5. **First real cross-repo PR — UTILITY-GRADE target:** operator-gated, into a **utility-grade component in reactor-core** (e.g. a telemetry logging adapter or a metric struct) that has a **real, traceable dependency graph back to jarvis** — so we WATCH `Oracle.compute_blast_radius` actually pull the dependent Body files into the prompt. (NOT a doc/test — those prove nothing about AST dependency tracing.) Through the full quarantine: blast-radius context → air-gap sandbox → CRITICAL_ELEVATION operator approval.

## 8. Tests
- **G1:** Oracle blast-radius dependents (cross-repo) forced into context with the header; token-budget truncation logged not silent; Oracle error → single-repo fallback + floor escalation (fail-closed).
- **G2:** sandbox spins all 3 (mocked), handshake-pass → gate passes; handshake-fail → `[SOVEREIGN YIELD: CROSS-REPO FRACTURE]` + rollback + teardown-in-finally; Docker-absent → treated as FRACTURE (fail-closed); timeout bounded.
- **G3 — CRITICAL_ELEVATION + Trust Ledger:** a cross-repo op → CRITICAL_ELEVATION (hard-halt, no merge even with all-green CI); operator-approval path; **streak trust accumulates by AST-complexity weight; a rollback OR FRACTURE resets streak+trust to ZERO**; graduation only when `streak_trust >= adaptive_threshold` (NOT a fixed count — a static test proves 100 trivial merges do NOT graduate but a sufficient complexity-weighted streak does); the dynamic threshold scales with attempted complexity; ledger error → stay elevated.
- **G3 — Immutable Orange (load-bearing):** for prime/reactor targets the resolved tier is `>= approval_required` under EVERY flag/graduation/trust combination — un-disableable (parametrized over the env-flag matrix). Body may graduate below; Mind/Nerves never. AST-pinned (no env reads the immutable floor).
- **Master OFF:** `JARVIS_CROSS_REPO_MUTATION_ENABLED=false` → Body-only, byte-identical (no context-trace, no sandbox, no elevation; cross-repo ops never created).
- **G2 — Air-gap (load-bearing):** the sandbox network is `internal`; a test asserting a container cannot resolve/reach a real DW/Claude/GCP host (egress sinkhole), and that non-air-gapped → treated as FRACTURE (refuses to run).
- **Cross-cutting (load-bearing):** a static proof that NO failure path makes a cross-repo mutation *less* gated (fail-closed) and that **auto-merge of a Mind/Nerves PR is structurally unreachable by any flag** (the Immutable Orange Protocol).

## 9. Decisions (operator-authorized 2026-06-23)
1. **Graduation:** ✅ **Adaptive Trust Ledger** (rejects magic N) — consecutive zero-rollback streak, AST-complexity-weighted trust, dynamic threshold scaling with attempted complexity; rollback/FRACTURE → reset to zero. (§5.2)
2. **Post-graduation ceiling:** ✅ **Immutable Orange Protocol** — Mind (prime) + Nerves (reactor) PERMANENTLY human-merged, structurally un-disableable; only Body may auto-merge. (§5.3)
3. **Sandbox venue:** ✅ **Local ephemeral Docker, AIR-GAPPED** (`internal` network + egress sinkhole → no live DW/Claude/GCP). $0. (§4)
4. **First target:** ✅ **Utility-grade reactor-core component** (telemetry adapter / metric struct) with a real jarvis dependency graph — watch `compute_blast_radius` pull Body files. (§7.5)
