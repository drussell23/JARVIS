---
title: Project Move 6 Scope
modules: [backend/core/ouroboros/governance/autonomy/subagent_scheduler.py, backend/core/ouroboros/governance/worktree_manager.py, orchestrator.py, backend/core/ouroboros/governance/candidate_generator.py, backend/core/ouroboros/governance/cost_contract_assertion.py, backend/core/ouroboros/governance/verification/generative_quorum.py, backend/core/ouroboros/governance/verification/ast_canonical.py, backend/core/ouroboros/governance/verification/generative_quorum_runner.py, backend/core/ouroboros/governance/verification/generative_quorum_gate.py, backend/core/ouroboros/governance/verification/generative_quorum_observability.py]
status: merged
source: project_move_6_scope.md
---

**Scope status: DRAFT** (post-Move-5-completion, awaiting kickoff
authorization). After commits `70bddb1a20` + `686a808c21` +
`ccee0c8c51` + `cd352719b9` + `636f06a05d` (Move 5 full arc) +
`7c0c160591` + `bed2b1182f` + `6265f86476` (§28 Tier 1 #1+#2+#3),
the empirical floor stands at **A−** with structural ceiling **A**.
Move 6 is the integrity upgrade per §28.6.2 — closes the two
remaining undefended Antivenom bypass vectors.

## Why Move 6 (architectural justification)

§28.5.2 v9 brutal review found **two undefended bypass vectors**:

| # | Vector | Defense status (verified) |
|---|--------|---------------------------|
| 4 | **Test-shape gaming** — model writes tests that pass without exercising the property (asserts on mock, no SUT exercise) | **ABSENT** — no AST pin can catch this; requires semantic understanding |
| 5 | **Quine-class hallucination** — generated code that includes its own validators / pins the patch's specific shape | **ABSENT** — registry is post-hoc; Quine-class gen with self-validating invariant is structurally accepted |

Move 9 (test-shape gaming static classifier) would catch the
*obvious* variants of #4 but cannot solve the general problem.
Move 6 — **Generative Quorum** — solves BOTH structurally in one
architectural move via independent-roll consensus.

**The mechanism:**

Run K=3 candidate generations in parallel from the same prompt
(different seeds/temperatures for true independence). Compute
AST-normalized signature for each candidate's diff. Accept only
candidates with semantic agreement:

  * **CONSENSUS** (all K agree) → accept best candidate, proceed
  * **MAJORITY_CONSENSUS** (K-1 agree, 1 outlier) → operator review
  * **DISAGREEMENT** (all distinct OR no quorum) → escalate via
    existing BLOCKED tier path

**Why this kills both vectors:**

  * **Quine-class** requires the model to synthesize a self-
    consistent validator. Three *independent* rolls are
    vanishingly unlikely to converge on the *same* hallucinated
    structure. Even if one roll generates a plausible-but-fake
    invariant, the other two probably won't generate the same
    fake. Disagreement → rejection.

  * **Test-shape gaming**: three independent rolls all generating
    the same vacuous test (asserts on mock without exercising
    SUT) is exponentially unlikely. Even partial agreement on
    the gaming pattern reveals it as a structural flaw, not a
    correctness check.

  * **Symbol-shape hallucination**: all three rolls invoking the
    same wrong contract is possible for single tokens, exponentially
    unlikely for multi-symbol patches. K=3 reduces hallucination
    surface area by approximately 1/K^N where N is symbol count.

## Existing infrastructure to leverage (NO duplication)

The substrate is mostly already shipped:

  * **L3 worktree isolation** (``subagent_scheduler.py`` +
    ``worktree_manager.py``) — parallel execution graphs use
    isolated git worktrees (COW via ``git worktree add -b``).
    Substrate for K-way parallelism without filesystem conflicts.
  * **BackgroundAgentPool** — 3 workers with PriorityQueue
    (already configurable via ``JARVIS_BG_POOL_SIZE``).
  * **Multi-file coordinated generation**
    (``orchestrator.py:9321-9471`` ``_apply_multi_file_candidate``)
    with batch-level rollback semantics.
  * **AST validator** (``meta/ast_phase_runner_validator.py``,
    Pass B Slice 3) — 6 structural rules for shipped code.
  * **Phase B subagents** (REVIEW + PLAN + GENERAL) — async,
    cage-protected. The cognitive primitives for K-way parallel
    work are in place.
  * **Candidate generator** (``candidate_generator.py``) —
    produces ONE candidate per op today; Quorum needs K candidates
    from same prompt with seed variation for true independence.
  * **Cost contract assertion** (``cost_contract_assertion.py``)
    — already enforced at provider dispatch boundary; Quorum
    inherits structurally.
  * **MutationGate / SemanticGuardian / RISK_CEILING /
    Iron Gate** — already pin mutation contracts. Quorum sits
    BEFORE these (post-GENERATE, pre-VALIDATE) to enrich the
    candidate set with consensus signal.

What Move 6 BUILDS:

  * **AST-normalized signature** — canonical form of "what
    changes does this candidate make?" that ignores formatting/
    whitespace/comments but captures structural mutations
    (function additions, class additions, type signatures,
    symbol references).
  * **Consensus detector** — pure compare function over K
    signatures; closed taxonomy of outcomes.
  * **K-way parallel runner** — orchestrates K candidate
    generations via existing infrastructure (subagent_scheduler
    or candidate_generator), gathers results, classifies
    consensus.
  * **Risk-tier gate** — Quorum fires only for APPROVAL_REQUIRED+
    tier ops (where K× cost is justified by stakes); SAFE_AUTO +
    NOTIFY_APPLY use single-candidate as today.

## The 5-slice arc

### Slice 1 — Quorum primitive (pure data + compute)

**New module**: ``verification/generative_quorum.py``

* Frozen dataclasses:
  - ``CandidateRoll(roll_id, candidate_diff, ast_signature, cost_estimate, generation_metadata)``
  - ``ConsensusVerdict(outcome, agreement_count, distinct_count, total_rolls, canonical_signature, accepted_roll_id, detail)``
* 5-value ``ConsensusOutcome`` closed enum (J.A.R.M.A.T.R.I.X.):
  - ``CONSENSUS`` — all K rolls agree on signature; accept any one
  - ``MAJORITY_CONSENSUS`` — K-1 of K agree; route to operator review
  - ``DISAGREEMENT`` — all K distinct OR no quorum; escalate
  - ``DISABLED`` — master flag off; no rolls executed
  - ``FAILED`` — defensive sentinel (runner exception)
* Pure compare function: ``compute_consensus(rolls, *, quorum, k)``
* AST signature stub (full canonicalization in Slice 2)
* Schema version ``GENERATIVE_QUORUM_SCHEMA_VERSION =
  "generative_quorum.1"``
* Master flag ``JARVIS_GENERATIVE_QUORUM_ENABLED`` default false
* Authority invariants AST-pinned: stdlib only (Slice 1 is
  pure-data primitive)

**Tests**: ~30 covering frozen-dataclass shape + serialization,
master-flag asymmetric env, ConsensusOutcome 5-value closed
taxonomy pin, consensus math (all-agree / K-1-agree / all-distinct
/ partial / empty), authority invariants AST-pinned.

### Slice 2 — AST-normalized signature

**Module**: ``verification/ast_canonical.py`` (new)

* ``compute_ast_signature(source_code: str) -> str`` — sha256
  hash of canonical AST dump
* Canonicalization rules:
  - Strip whitespace (handled by ``ast.parse``)
  - Strip comments (handled by ``ast.parse``)
  - Strip docstrings (option: env-tunable; default keep)
  - Normalize string + numeric literals to type tags
    (e.g., all string literals → ``<STR>``, all ints → ``<INT>``)
  - Preserve symbol names (function defs, class defs, attribute
    access — these are semantically load-bearing)
  - Preserve control flow structure (if/else/for/while/try)
  - Preserve type annotations
* Per-symbol signatures:
  - ``function_added``: name + arg signature + return type
  - ``class_added``: name + base class names + method names
  - ``import_added``: module path
* Defensive: ``ast.parse`` raises on syntax errors → returns
  ``""`` (sentinel; convergence detector treats empty as no-signal)
* Hash-stable across Python minor versions (use ``ast.dump`` with
  ``annotate_fields=True, include_attributes=False``)
* AST authority invariants pinned

**Tests**: ~25 covering identical-code-same-hash + whitespace-
invariant + comment-invariant + numeric-literal-normalization +
syntax-error-returns-empty + symbol-name-preserved + multi-file
coordinated diff stable + Python-version-stable.

### Slice 3 — K-way parallel runner

**Module**: ``verification/generative_quorum_runner.py`` (new)

* ``async run_generative_quorum(prompt, *, k, risk_tier,
  generator, agreement_threshold) -> ConsensusVerdict``
* Spawns K candidate-generation tasks in parallel via existing
  ``candidate_generator`` infrastructure (or ``subagent_scheduler``
  for L3 workers — design TBD per Slice 3 implementation).
* Each roll uses different ``seed`` parameter for true
  independence (provider-supported; falls back to temperature
  perturbation if seed not honored).
* Wall-clock cap (env-tunable): ``JARVIS_QUORUM_WALL_CLOCK_S``
  (default 120s, floor 30s, ceiling 300s — Quorum is more
  expensive than Move 5's probe loop).
* Per-roll timeout cap.
* Cost cap: total cost ≤ ``k × per-roll budget``. Provider
  refuses to escalate cost tier mid-roll.
* Cancellation safe: when CONSENSUS detected early, cancel
  pending rolls via ``Task.cancel`` + ``asyncio.gather
  (return_exceptions)`` (mirrors Move 5 Slice 3 pattern).
* Defensive everywhere; never raises.

**Tests**: ~35 covering happy-path consensus + early-stop
cancellation + diverging rolls → DISAGREEMENT + wall-clock
timeout + per-roll exception swallowed + risk-tier gating
+ cost-budget enforcement + null-generator safety + provider
seed propagation.

### Slice 4 — Risk-tier gate + orchestrator hook

**Module**: ``verification/generative_quorum_gate.py`` (new)

* ``should_invoke_quorum(risk_tier, current_route) -> bool``:
  - Returns False if ``current_route`` in
    ``COST_GATED_ROUTES = (BG_ROUTE, SPEC_ROUTE)`` —
    structural cost-contract preservation (§26.6)
  - Returns False if risk_tier in
    ``{SAFE_AUTO, NOTIFY_APPLY}`` — Quorum is for high-stakes
    ops only
  - Returns True if risk_tier in
    ``{APPROVAL_REQUIRED, BLOCKED}`` AND master flag on
* ``async invoke_quorum_for_op(prompt, ctx) ->
  GenerativeQuorumResult`` — orchestrator-facing entry point
* Mapping ConsensusOutcome → orchestrator action:
  - CONSENSUS → return accepted_roll's candidate (proceed)
  - MAJORITY_CONSENSUS → return majority candidate + raise
    risk_tier to NOTIFY_APPLY (operator sees it)
  - DISAGREEMENT → raise QuorumFailedConsensusError (escalates
    via existing BLOCKED tier path)
  - DISABLED / FAILED → fall through to existing single-candidate
    path (no behavior change)
* AST-pinned: gate consumes ``COST_GATED_ROUTES`` from
  ``cost_contract_assertion`` — refactor that bypasses cost
  guard caught structurally.

**Tests**: ~25 covering 4-tier × 2-route gate matrix + cost-gated
route refusal + master-off short-circuit + each
ConsensusOutcome → expected action mapping + defensive
fall-through on FAILED.

### Slice 5 — Graduation + operator surfaces

* **Master flag flips** (default false → true):
  - ``JARVIS_GENERATIVE_QUORUM_ENABLED``
  - ``JARVIS_QUORUM_GATE_ENABLED`` (sub-gate for the gate
    integration)
* **shipped_code_invariants AST pins** (mirror Move 4/5 Slice 5):
  - ``generative_quorum_no_mutation_tools`` — quorum module
    must not reference mutation tools in code
  - ``ast_canonical_pure_stdlib`` — signature module must be
    stdlib-only (no governance imports)
  - ``quorum_gate_consumes_cost_gated_routes`` — gate must
    reference ``COST_GATED_ROUTES`` constant from
    ``cost_contract_assertion`` (catches refactor that drops
    structural cost-contract guard)
  - ``quorum_cap_structure_pinned`` — K cap with floor + ceiling
* **FlagRegistry seeds**: 6+ FlagSpec entries (master + sub-gate
  + cap knobs)
* **Operator surfaces**:
  - ``/quorum`` REPL — recent / stats / `<op_id>` filter (mirrors
    /auto-action / /probe shape)
  - ``GET /observability/quorum[/config,/history,/stats]`` (4 routes)
  - SSE event ``EVENT_TYPE_QUORUM_OUTCOME`` published on every
    non-DISABLED quorum invocation
* **Comprehensive graduation pin suite** (~50 tests)

### Slice budget

| Slice | New module | Tests | LOC est |
|---|---|---|---|
| 1 — Quorum primitive | generative_quorum.py | ~30 | ~450 |
| 2 — AST signature | ast_canonical.py | ~25 | ~350 |
| 3 — K-way parallel runner | generative_quorum_runner.py | ~35 | ~550 |
| 4 — Risk-tier gate | generative_quorum_gate.py | ~25 | ~350 |
| 5 — Graduation + observability | + generative_quorum_observability.py | ~50 | ~500 |

**Total**: ~5 commits, ~165 tests, ~2,200 net new lines.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| **Cost overrun (K× generation cost)** | Risk-tier gate refuses to fire on SAFE_AUTO + NOTIFY_APPLY (most ops); APPROVAL_REQUIRED+ ops are high-stakes where 3× cost is justified. Default off until shadow-mode evidence shows operator approval. |
| **All K rolls produce same hallucination** (defeats independence) | Per-roll seed variation enforced at runner; falls back to temperature perturbation if provider doesn't honor seed. Empirical evidence in shadow mode reveals if hallucination convergence is real. |
| **Latency** (worst-case = max(roll_latency)) | Parallel execution; wall-clock cap; partial-quorum acceptance possible (e.g., 2-of-3 with timeout → MAJORITY_CONSENSUS). |
| **AST normalization too aggressive** (false positives — semantically different code hashes the same) | Conservative normalization (whitespace + comments + numeric/string literals to type tags only); preserve symbol names; preserve control flow. Empirical test corpus pins isomorphic-but-distinct code → distinct hashes. |
| **AST normalization too strict** (false negatives — semantically equivalent code hashes differently) | Use canonical AST dump (ast.dump + Python-version-stable). Whitespace doesn't change AST; comments don't either. |
| **Cost contract violation** (Quorum on BG/SPEC routes) | Gate explicitly refuses cost-gated routes via ``COST_GATED_ROUTES`` constant from cost_contract_assertion. AST-pinned by Slice 5 graduation. |
| **Quorum-failure cascade** (DISAGREEMENT escalates → backlog of unresolved ops) | DISAGREEMENT routes through existing BLOCKED tier path which has operator-review surfaces. No new escalation surface introduced. |
| **Backward-compat regression** (orchestrator hook breaks existing flow) | Slice 4 gate's master-off path is byte-for-byte equivalent to no-quorum behavior. AST-pin verifies. |

## Authority invariants (AST-pinned by Slice 5 graduation pins)

  * ``generative_quorum.py`` — stdlib only.
  * ``ast_canonical.py`` — stdlib only (no governance imports).
  * ``generative_quorum_runner.py`` — stdlib + Slice 1+2 +
    candidate_generator (existing) + cost_contract_assertion.
    NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / semantic_guardian / semantic_firewall
    / providers / doubleword_provider / urgency_router /
    auto_action_router / subagent_scheduler / tool_executor.
  * ``generative_quorum_gate.py`` — stdlib + Slice 1+2+3 +
    cost_contract_assertion.
  * ``generative_quorum_observability.py`` — stdlib + aiohttp +
    Slice 1+2+3+4.

  * No mutation tools referenced in code (AST walk verifies).
  * K cap with floor + ceiling (env-tunable).

## Knobs (Slice 5 graduation defaults)

  * ``JARVIS_GENERATIVE_QUORUM_ENABLED`` — master, **graduated true**
  * ``JARVIS_QUORUM_GATE_ENABLED`` — sub-gate for orchestrator
    hook, **graduated true**
  * ``JARVIS_QUORUM_K`` (default 3, floor 2, ceiling 5) —
    candidate count
  * ``JARVIS_QUORUM_AGREEMENT_THRESHOLD`` (default 2, floor 2 —
    majority quorum)
  * ``JARVIS_QUORUM_WALL_CLOCK_S`` (default 120, floor 30,
    ceiling 300)
  * ``JARVIS_QUORUM_PER_ROLL_TIMEOUT_S`` (default 60, floor 15,
    ceiling 180)
  * ``JARVIS_QUORUM_TIER_THRESHOLD`` (string default
    ``"approval_required"``; valid: safe_auto / notify_apply /
    approval_required / blocked)

## Cost contract preservation (PRD §26.6)

Quorum is K× generation cost within an op:

  * Each candidate goes through existing
    ``candidate_generator.py`` which already enforces
    ``cost_contract_assertion.assert_provider_route_compatible``
    at the dispatch boundary.
  * Quorum gate refuses to fire on BG/SPEC routes via the
    ``COST_GATED_ROUTES`` constant (AST-pinned in Slice 5).
  * Quorum gate ONLY fires for APPROVAL_REQUIRED+ tier — where
    K× cost is justified by op stakes.
  * Default disabled until Slice 5; operators graduate after
    observing shadow-mode evidence in
    ``.jarvis/quorum_history.jsonl`` (cross-process flock'd via
    Tier 1 #3's helper).

## Slice independence

Each slice is independently mergeable:

  * Slice 1 ships the primitive — Slice 2-5 not landed → no
    behavior change (primitive unused).
  * Slice 2 ships AST signature — used by Slice 1's primitive
    when called.
  * Slice 3 ships runner — callable by tests but not triggered
    from production until Slice 4.
  * Slice 4 wires the orchestrator hook but master flag default-
    false → no behavior change in production.
  * Slice 5 graduates — flag default-true unlocks Quorum in prod.

This matches Move 3 + Move 4 + Move 5 substrate-first cadence.

## What this Move does NOT prescribe

  * **No re-architecture of candidate_generator.py** — Quorum
    consumes existing single-candidate API K times in parallel.
  * **No mid-generation self-critique** — that's CC's Extended
    Thinking analog and remains a Move 7+ concern. Move 6
    operates on completed candidates, not in-flight reasoning.
  * **No ENFORCE-mode graduation for Move 3** — separate
    authorization gated on shadow-mode soak evidence.
  * **No replacement of SemanticGuardian/Iron Gate** — Quorum
    sits BEFORE these (post-GENERATE, pre-VALIDATE). Existing
    safety pins remain authoritative.

## Closure criterion

Move 6 closes when:

  * All 5 slices land (commits + regression tests green)
  * Master flag graduated default-true
  * shipped_code_invariants AST pins register and currently-hold
    (target: 27 total invariants post-Move-6)
  * Operator surfaces (4 GET routes + SSE) live
  * `memory/project_move_6_closure.md` written
  * MEMORY.md indexed
  * One end-to-end live verification: real APPROVAL_REQUIRED op
    triggers Quorum → consensus reached → accepted candidate
    proceeds through APPLY without operator escalation
