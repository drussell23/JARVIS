---
title: Project Move 5 Scope
modules: []
status: merged
source: project_move_5_scope.md
---

**Scope status: DRAFT** (post-Tier-1-completion, awaiting kickoff
authorization). After commits `7c0c160591` (Tier 1 #1) +
`bed2b1182f` (Tier 1 #2) + `6265f86476` (Tier 1 #3) the
operational fragility floor is lifted to A−. Move 5 is the next
strategic move per §28.6.2.

## Why Move 5 (architectural justification)

§28.3 v9 brutal review found the cognitive gap explicitly:

  * `tool_executor.py:200-1000` audit confirmed sequential
    request→response→request only. **No inner reasoning between
    tool rounds. CC's Extended Thinking has no analog.**
  * `hypothesis_consumers.py:341-429` `probe_confidence_collapse`
    is REACTIVE — only fires AFTER a confidence collapse triggers
    it from the provider side.
  * 3-action `ConfidenceCollapseAction` enum offers only
    `RETRY_WITH_FEEDBACK` / `ESCALATE_TO_OPERATOR` / `INCONCLUSIVE`.
    No autonomous "I'm uncertain, let me probe the codebase to
    disambiguate" outcome.
  * `ask_human` is the only escape valve when ambiguity bites,
    which violates the "proactive autonomous opposite of CC"
    operator-binding mandate.

Move 5 is the **temporal analog of Move 4**:

  * Move 4 — InvariantDriftAuditor — detects drift between
    *architectural promises* across cross-op temporal boundaries.
  * Move 5 — Autonomous Probe Loop — detects ambiguity *within
    a single op* and resolves it autonomously instead of escalating.

Together they bound uncertainty in both axes:

  * Move 4: "Did our architectural promises change unexpectedly?"
    (cross-op temporal drift)
  * Move 5: "Are we sure about this op's premise?" (intra-op
    epistemic uncertainty)

## Existing infrastructure to leverage (NO duplication)

Audit confirms the substrate is mostly already shipped:

  * `adaptation/hypothesis_probe.py` (Phase 7.6) — bounded probe
    primitive. 9-value `ProbeVerdict` enum (CONFIRMED / REFUTED /
    4× INCONCLUSIVE_* / 3× SKIPPED_*). `EvidenceProber` Protocol.
    `_NullEvidenceProber` default. K=5 calls cap, 30s timeout,
    monotonic clock, sha256 diminishing-returns. **Three
    independent termination guarantees structurally enforced.**
    Master flag `JARVIS_HYPOTHESIS_PROBE_ENABLED` (default false).
  * `verification/hypothesis_consumers.py:100-127` —
    `ConfidenceCollapseAction` 3-value enum + `probe_confidence_
    collapse` async function (line 341). Master-flag-gated.
    Returns `ConfidenceCollapseDecision` frozen dataclass.
  * `verification/confidence_monitor.py` — `ConfidenceMonitor`
    with `evaluate()` returning OK / APPROACHING_FLOOR /
    BELOW_FLOOR. Per-GENERATE-round instance.
  * `verification/confidence_observability.py` — three publishers
    + `_record_verdict_for_auto_action_router` bridge (Move 3
    integration).
  * `auto_action_router._VerdictRingBuffer` — Move 3 verdict
    history surface.
  * `cross_process_jsonl.py` (Tier 1 #3) — flock helpers for
    any new ledger.

What Move 5 BUILDS is the **bridge**: connect Phase 7.6's bounded
probe primitive + confidence_monitor verdicts → 4th outcome that
auto-resolves ambiguity. **Zero new probe machinery. One new
bridge module. Two existing modules touched additively.**

## The 5-slice arc

### Slice 1 — Probe Bridge primitive

**New module**: `verification/confidence_probe_bridge.py`

* Frozen dataclasses:
  - `ProbeQuestion(question, resolution_method, max_tool_rounds)`
  - `ProbeAnswer(question, answer_text, evidence_fingerprint, tool_rounds_used)`
  - `ConvergenceVerdict(outcome, agreement_count, disagreement_count, canonical_answers, detail)`
* 5-value `ProbeOutcome` enum (J.A.R.M.A.T.R.I.X. closed taxonomy):
  - `CONVERGED` — K-1 of K probes agree on canonical answer →
    confidence elevated, op proceeds
  - `DIVERGED` — K-1 of K disagree → ESCALATE_TO_OPERATOR
  - `EXHAUSTED` — budget hit before convergence/divergence
  - `DISABLED` — master flag off
  - `FAILED` — defensive sentinel (probe runner raised)
* Pure decision functions: `compute_convergence(answers, threshold)`
* Schema version `CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION =
  "confidence_probe_bridge.1"`
* Master flag `JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED` default
  false until Slice 5 graduation
* Authority invariants (AST-pinned by companion tests):
  - Imports stdlib + adaptation.hypothesis_probe (primitive) +
    verification.confidence_monitor (Verdict enum) ONLY
  - NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / semantic_firewall / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor
  - Never raises out of any public method

**Tests**: ~30 covering frozen-dataclass shape + serialization,
master-flag asymmetric env semantics, ProbeOutcome 5-value
closed taxonomy pin, convergence math (K-1 agree / K-1 disagree
/ exact tie / single answer), ConvergenceVerdict round-trip,
authority invariants AST-pinned.

### Slice 2 — Probe Question Generator + Read-only EvidenceProber

**Two additions**:

**A. Generator** in `confidence_probe_bridge.py`:
* `generate_probes(ambiguity_context, *, max_questions=3)` → tuple
  of `ProbeQuestion`
* Default mode: deterministic templates ($0 cost) — questions
  derived from `ConfidenceMonitor.snapshot()` + ambiguity
  context (e.g., "what type is `<symbol>` in `<file>`?",
  "where is `<symbol>` defined?")
* Optional mode: small-LLM (env-tunable
  `JARVIS_CONFIDENCE_PROBE_GENERATOR_MODE = templates|llm`,
  default `templates`)
* AST-pinned: generator produces ProbeQuestion only; no side
  effects, no mutation tools, no shell

**B. Prober**: `verification/readonly_evidence_prober.py`
* Implements Phase 7.6 `EvidenceProber` Protocol
* Read-only tool allowlist (frozenset, AST-pinned):
  `{read_file, search_code, get_callers, glob_files, list_dir,
   list_symbols, git_blame, git_log, git_diff}`
* Each probe round: model call with read-only tools → answer
  text + tool_round count + evidence_fingerprint (sha256 of
  normalized answer)
* Cost cap inherited from Phase 7.6 (`MAX_CALLS_PER_PROBE = 5`,
  env-tunable)
* AST-pinned: prober imports CANNOT include any mutation tool
  (`edit_file`, `write_file`, `bash`, `delete_file`, `run_tests`)

**Tests**: ~25 covering template generator (deterministic,
$0 cost, bounded count), read-only prober (allowlist enforcement,
fingerprint stability, defensive on null/raising tool backend),
authority invariants AST-pinned (no mutation tools imported).

### Slice 3 — Convergence Loop + Confidence Elevation Bridge

**Wire**:

* `run_probe_loop(monitor, ambiguity_context, *, prober=None,
  budget=None)` → `ConvergenceVerdict`
* Async (mirrors Move 4 observer pattern). Default prober is
  `_NullEvidenceProber` (zero cost) so misconfigured callers
  cannot accidentally hit a paid API.
* Convergence detector — uses sha256 over canonical-form answer
  signatures (mirrors Move 4 drift signature ring discipline)
* On `CONVERGED` → `ConfidenceMonitor.reset_window()` (clear
  rolling margin so monitor returns OK on next observation) +
  emit telemetry
* On `DIVERGED` / `EXHAUSTED` → return verdict; caller (Slice 4)
  routes to ESCALATE_TO_OPERATOR
* Hard caps — env-tunable (mirror Phase 7.6 + Move 4 patterns):
  - `JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS` (default 3, floor 2,
    ceiling 5)
  - `JARVIS_CONFIDENCE_PROBE_WALL_CLOCK_S` (default 30, floor 5,
    ceiling 120)
  - `JARVIS_CONFIDENCE_PROBE_COST_FACTOR` (default 1.0 — probe
    cost ≤ 1× current op cost-tier)
  - `JARVIS_CONFIDENCE_PROBE_CONVERGENCE_QUORUM` (default 2,
    floor 2 — need K-1 agreement)

**Tests**: ~30 covering happy-path convergence, divergence,
exhausted budget, disabled flag, prober raise → FAILED, monotonic
clock (immune to wall-clock changes), per-question rounds capped,
defensive on malformed answers.

### Slice 4 — Wire into ConfidenceCollapseAction.PROBE_ENVIRONMENT

**Modify** `verification/hypothesis_consumers.py`:

* Add 4th value to `ConfidenceCollapseAction`:
  `PROBE_ENVIRONMENT = "probe_environment"`
* Update `probe_confidence_collapse` decision tree:
  - APPROACHING_FLOOR + bridge_enabled → return PROBE_ENVIRONMENT
    (defer to bridge for actual probe execution)
  - APPROACHING_FLOOR + bridge_disabled → existing
    RETRY_WITH_FEEDBACK (default-off revert path)
  - BELOW_FLOOR unchanged → ESCALATE_TO_OPERATOR
  - Other branches unchanged
* New caller-bridge path `execute_probe_environment(decision,
  monitor, ambiguity_context)`:
  - Calls `confidence_probe_bridge.run_probe_loop`
  - On CONVERGED → returns OK (op proceeds)
  - On DIVERGED → returns ESCALATE_TO_OPERATOR
  - On EXHAUSTED → returns RETRY_WITH_FEEDBACK (one retry, then
    escalate if still APPROACHING)
  - On FAILED / DISABLED → returns existing safe default
* **Backward-compat**: master flag default-off preserves existing
  3-action behavior. Existing consumers (Move 3 verdict ring
  buffer, observability publishers) tolerate the new enum value
  because they treat it as a string passthrough — no code path
  rejects unknown action types.

**Tests**: ~25 covering 4th-value-presence pin, decision tree
under all 4 outcomes, default-off backward-compat (3-action
behavior preserved exactly), Move 3 verdict ring buffer
tolerates PROBE_ENVIRONMENT (passthrough), defensive on bridge
exception, end-to-end integration (collapse → bridge → converged
→ monitor reset).

### Slice 5 — Graduation + Operator Surfaces

* **Master flag flips** (default false → true, asymmetric env
  semantics):
  - `JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED`
  - `JARVIS_HYPOTHESIS_PROBE_ENABLED` (Phase 7.6 graduation —
    safe to graduate now since Move 5 is its first live consumer)
  - `JARVIS_READONLY_EVIDENCE_PROBER_ENABLED`
* **shipped_code_invariants AST pins** (mirrors Move 4 Slice 5
  pattern):
  - `confidence_probe_bridge_no_mutation_tools` — bridge module
    must not import any tool from `_MUTATION_TOOLS` set
  - `readonly_evidence_prober_allowlist_pinned` — prober's
    `_READONLY_TOOL_ALLOWLIST` constant must contain only the
    7-tool whitelisted set; no additions without operator review
  - `confidence_probe_cap_structure_pinned` — env knobs for
    K-questions, wall-clock, cost-factor must be enforced via
    `min`/`max` clamps in the source (catches refactor that
    silently loosens caps)
* **FlagRegistry seeds**: 6 new FlagSpec entries (3 master flags
  + 3 cap knobs) with posture-relevance for /help posture filter
* **Operator surfaces**:
  - `/probe` REPL — recent / stats / `<op_id>` filter (mirrors
    /auto-action shape)
  - `GET /observability/probe[/stats,/history,/baseline]` (4
    routes mirroring Move 4 invariant-drift observability)
  - SSE event `EVENT_TYPE_PROBE_OUTCOME` published on every
    non-DISABLED probe loop completion
* **Comprehensive graduation pin suite**:
  - 3 master flag default-true tests + asymmetric env matrix (3
    flags × 9 values)
  - 3 cap-knob defaults + floors + ceilings + garbage tolerance
  - 6 FlagSpec presence + posture_relevance + source_file
  - 3 shipped_code_invariants pins registered + currently-hold
  - 4 GET routes mountable + 503-when-disabled + 200-when-enabled
  - SSE event published on probe outcomes (capturing fake broker)
  - End-to-end: confidence_monitor APPROACHING_FLOOR →
    PROBE_ENVIRONMENT → bridge → converged → monitor reset →
    op proceeds

**Tests**: ~50

### Slice budget

| Slice | Module | Tests | LOC est |
|---|---|---|---|
| 1 — Bridge primitive | confidence_probe_bridge.py | ~30 | ~400 |
| 2 — Generator + Prober | + readonly_evidence_prober.py | ~25 | ~350 |
| 3 — Convergence loop | (extends Slice 1 module) | ~30 | ~250 |
| 4 — ConfidenceCollapseAction wire | (modifies hypothesis_consumers.py) | ~25 | ~150 |
| 5 — Graduation + operator surfaces | + confidence_probe_observability.py | ~50 | ~500 |

**Total**: ~5 commits, ~160 tests, ~1,650 net new lines, full
Move 5 closure.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Cost overrun (LLM-based prober runs more rounds than expected) | Phase 7.6's 3-independent-termination-guarantees structurally enforced; cost-factor knob + Phase 7.6's MAX_CALLS_PER_PROBE compose multiplicatively |
| Probe loops cycle indefinitely on hard ambiguity | Phase 7.6's monotonic-clock wall-clock cap + diminishing-returns sha256 fingerprint; combined with new K-questions cap |
| Backward-compat regression (consumers don't tolerate 4th enum) | All consumers treat `ConfidenceCollapseAction` as string passthrough (verified: auto_action_router uses `verdict.value`, observability publishers use `_safe_str`). New value is additive |
| Read-only contract violation (prober mutates state) | AST-pinned `_READONLY_TOOL_ALLOWLIST` frozenset + AST authority test rejecting any import from `_MUTATION_TOOLS` set |
| Quine-class probe (model fabricates self-consistent answers across K probes) | Move 6 (Generative Quorum) is the structural defense; Move 5's convergence detector uses canonicalized answer signatures so identical fabrications agree (caught at Move 6 layer) |
| Anthropic-routed ops still have ZERO confidence signal | This is a pre-existing constraint; Move 5 only fires on DW-routed ops where logprobs exist. Anthropic-routed ops continue to use existing 3-action behavior. Documented limitation. |

## Authority invariants (AST-pinned by Slice 5 graduation pins)

* `confidence_probe_bridge.py` — stdlib + adaptation.hypothesis_probe
  + verification.confidence_monitor ONLY. NO orchestrator /
  phase_runners / candidate_generator / iron_gate / change_engine
  / policy / semantic_guardian / semantic_firewall / providers /
  doubleword_provider / urgency_router / auto_action_router /
  subagent_scheduler / tool_executor.
* `readonly_evidence_prober.py` — read-only tool allowlist
  enforced at module scope (frozenset constant, AST-walked by
  graduation test).
* No mutation tools (`edit_file` / `write_file` / `bash` /
  `delete_file` / `run_tests`) imported anywhere in the Move 5
  module set.

## Knobs (Slice 5 graduation defaults)

* `JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED` — master, **graduated true**
* `JARVIS_HYPOTHESIS_PROBE_ENABLED` — Phase 7.6 master,
  **graduated true** (Move 5 is its first live consumer)
* `JARVIS_READONLY_EVIDENCE_PROBER_ENABLED` — sub-gate,
  **graduated true**
* `JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS` (default 3, floor 2,
  ceiling 5)
* `JARVIS_CONFIDENCE_PROBE_WALL_CLOCK_S` (default 30, floor 5,
  ceiling 120)
* `JARVIS_CONFIDENCE_PROBE_COST_FACTOR` (default 1.0 — probe
  cost ≤ 1× current op cost-tier)
* `JARVIS_CONFIDENCE_PROBE_CONVERGENCE_QUORUM` (default 2, floor 2)
* `JARVIS_CONFIDENCE_PROBE_GENERATOR_MODE` (templates | llm,
  default templates — $0 cost)

## Cost contract preservation (PRD §26.6)

Probe execution is a sub-op operation. Cost contract structurally
preserved by inheriting Phase 7.6's existing protections:

* Probe runner consumes `ConfidenceMonitor` snapshots + tool
  outputs only; never reaches the provider dispatch boundary
  directly.
* Read-only EvidenceProber's tool calls go through Venom's
  existing `tool_executor.py` which already enforces
  `cost_contract_assertion.assert_provider_route_compatible` at
  the dispatch boundary.
* Probe budget caps prevent cost amplification: K=3 probes ×
  K=5 calls/probe × current op cost-tier × 1.0 cost-factor
  = ≤ 15× single-call cost in worst case (rare convergence
  failure path); typical case 1-2 probes.

## Slice independence

Each slice is independently mergeable:

* Slice 1 ships the primitive — Slice 2-5 not landed → no
  behavior change (primitive unused).
* Slice 2 ships generator + prober — still unused without bridge
  wire-up.
* Slice 3 ships convergence loop — callable by tests but not
  triggered from production until Slice 4.
* Slice 4 wires the 4th outcome but master flag default-false →
  no behavior change in production.
* Slice 5 graduates — flag default-true unlocks the loop in prod.

This matches Move 3 + Move 4 substrate-first cadence exactly.

## What this Move does NOT prescribe

* **No mid-generation self-critique** — that's CC's Extended
  Thinking analog and remains a Move 6+ concern. Move 5
  resolves ambiguity *between* GENERATE rounds, not *within*
  a streaming generation.
* **No Generative Quorum** — Move 6 is the K-way parallel
  candidate consensus; Move 5 only does K-way sequential probes.
* **No new ledger** — probe outcomes flow through the existing
  Move 3 auto_action_router verdict ring buffer (passthrough).
* **No new SSE vocabulary beyond `EVENT_TYPE_PROBE_OUTCOME`** —
  reuses `EVENT_TYPE_MODEL_CONFIDENCE_DROP` /
  `EVENT_TYPE_MODEL_CONFIDENCE_APPROACHING` /
  `EVENT_TYPE_MODEL_SUSTAINED_LOW_CONFIDENCE` (Tier 1 #1 wired).

## Closure criterion

Move 5 closes when:

  * All 5 slices land (commits + regression tests green)
  * 3 master flags graduated default-true
  * shipped_code_invariants AST pins register and currently-hold
  * Operator surfaces (/probe REPL + 4 GET routes + SSE) live
  * `memory/project_move_5_closure.md` written
  * MEMORY.md indexed
  * One end-to-end live verification: real DW-routed op triggers
    APPROACHING_FLOOR → bridge → converged → monitor reset → op
    proceeds without operator escalation
