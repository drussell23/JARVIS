---
title: Reverse Russian Doll — Pass B Design Draft (2026-04-26)
modules: [backend/core/ouroboros/governance/multi_repo/registry.py, backend/core/ouroboros/governance/orchestrator.py, backend/core/ouroboros/governance/phase_runner.py, backend/core/ouroboros/governance/semantic_firewall.py, backend/core/ouroboros/governance/semantic_guardian.py, backend/core/ouroboros/governance/scoped_tool_backend.py, backend/core/ouroboros/governance/risk_tier_floor.py, backend/core/ouroboros/governance/change_engine.py, backend/core/ouroboros/governance/meta/ast_phase_runner_validator.py, backend/core/ouroboros/governance/phase_runners/gate_runner.py, backend/core/ouroboros/governance/meta/meta_phase_runner.py]
status: merged
source: project_reverse_russian_doll_pass_b.md
---

# Reverse Russian Doll — Pass B Design Draft (2026-04-26)

## 0. Status

**STRUCTURALLY COMPLETE 2026-04-26 — all 6 slices shipped (Slices 1+2+2b+3+4+5+6.1+6.2+6.3); 438/438 Pass B regression suite green; defaults still false pending per-slice graduation.**

Slice-by-slice landings:
- Slice 1: PR #22298 (manifest + 9 Body-only entries).
- Slice 2: PR #22320 (`ORDER_2_GOVERNANCE` risk class + classifier + risk-floor application).
- Slice 2b: PR #22329 (gate_runner.py wiring).
- Slice 3: PR #22347 (570 LOC AST-shape validator + 6 rules + 56 tests).
- Slice 4: PR #22375 (544 LOC shadow-replay primitive + 61 tests).
- Slice 5: PR #22396 (411 LOC MetaPhaseRunner + 33 tests; DEFERRED candidate exec to Slice 6).
- Slice 6.1: PR #22475 (sandboxed replay executor — RESOLVES the deferred exec; 47 tests).
- Slice 6.2: PR #22517 (review queue + locked-true `amendment_requires_operator()` cage invariant; 59 tests).
- Slice 6.3: PR (this) — `/order2 {pending,show,amend,reject,history,help}` REPL dispatcher; 51 tests; **THE only caller in O+V that passes `operator_authorized=True` to the replay executor**.

Pass B is now ready for graduation cadence (per-slice 3-clean-session arc, defaults stay false during soak per W1 + W2(5) policy). Pass C (adaptive Anti-Venom — `project_reverse_russian_doll_pass_c.md`) is now structurally unblocked; remains a draft until operator authorization.

## 1. Problem statement (recap from Pass A)

Today, O+V can autonomously rewrite the body (Order 1: JARVIS application code, tests, sensors, tooling). It **cannot** safely rewrite its own cognitive substrate — the orchestrator FSM, the immune-system gates (Iron Gate sequence, `semantic_firewall.py`, `semantic_guardian.py`, `scoped_tool_backend.py`), the risk-tier ladder, the change engine. This is by design — there's no Iron Gate equivalent for FSM/gate mutations themselves (Pass A Gap 2.2).

Pass B builds that cage. Two gaps must be closed *jointly*:

- **Gap 2.1 — pipeline FSM not yet pluggable.** In motion: W2(5) PhaseRunner extraction is shipped behind per-slice flags, all defaulting `false`. Once Slice 5b graduates, the FSM is a pluggable composition of `PhaseRunner` subclasses against a frozen ABC (`phase_runner.py:91`). At that point, in principle, O+V could *generate* a new runner.
- **Gap 2.2 — no amendment protocol for governance code.** Without 2.2, closing 2.1 would be net-negative: a pluggable FSM with no guard against silent self-soft-corruption is a worse cage than a hand-written one.

Pass B's thesis: **2.1 is the door, 2.2 is the lock. They ship together.**

## 2. Scope

**In scope (Pass B):**
- The Order-2 manifest (`(repo, path-glob)` registry of governance-code paths)
- An Order-2 risk class above `BLOCKED`
- An AST-shape validator for new `PhaseRunner` subclasses
- A shadow pipeline + golden-replay corpus for regression-replay clearance
- The `MetaPhaseRunner` primitive (the component O+V uses to propose new runner subclasses)
- The manifest-amendment protocol (operator-only mutation path for the manifest itself)

**Out of scope (Pass C):**
- Adaptive Anti-Venom (Pass A Gap 2.3): SemanticGuardian patterns mined from POSTMORTEM corpus; Iron Gate floors auto-tightening; mutation budgets calibrated per-Order; risk-tier ladder learning new tiers.

**Out of scope (future, no Pass yet):**
- Cognitive primitive generation (Pass A Gap 2.4): O+V designing new subagent kinds. Pass B unlocks generated PhaseRunners only — not generated subagents.
- Cross-Trinity enforcement *logic* (Pass A Gap 2.5): the design supports Trinity-wide via manifest entries, but Pass B does not add J-Prime/Reactor patching authority. That requires the deferred separate document referenced in `OUROBOROS_VENOM_PRD.md:816`.

## 3. The Order-2 manifest

### 3.1 Schema (Trinity-extensible from day one)

```python
@dataclass(frozen=True)
class Order2ManifestEntry:
    repo: str           # RepoRegistry key: "jarvis" | "jarvis-prime" | "jarvis-reactor"
    path_glob: str      # POSIX glob relative to repo root
    rationale: str      # Why this path is governance-code (audit-readable)
    added: str          # ISO date the entry landed
    added_by: str       # "operator" | "<commit-sha>" — who authorized the entry

class Order2Manifest:
    entries: tuple[Order2ManifestEntry, ...]  # frozen at load time
```

The `(repo, path_glob)` tuple keys against the existing `RepoRegistry`
(`backend/core/ouroboros/governance/multi_repo/registry.py`). On Body-only
deploys, only the `jarvis` repo registers paths; future Mind/Soul integration
adds entries without schema change.

### 3.2 Initial Body-only entries (proposed)

```yaml
# .jarvis/order2_manifest.yaml — single source of truth
schema_version: 1
entries:
  # Pipeline FSM
  - repo: jarvis
    path_glob: backend/core/ouroboros/governance/orchestrator.py
    rationale: 11-phase governed loop; mutating this changes O+V's cognition
    added: 2026-04-26
    added_by: operator
  - repo: jarvis
    path_glob: backend/core/ouroboros/governance/phase_runner.py
    rationale: PhaseRunner ABC + PhaseResult contract — frozen interface
    added: 2026-04-26
    added_by: operator
  - repo: jarvis
    path_glob: backend/core/ouroboros/governance/phase_runners/*.py
    rationale: Concrete phase implementations — the cognition itself
    added: 2026-04-26
    added_by: operator

  # Immune system
  - repo: jarvis
    path_glob: backend/core/ouroboros/governance/semantic_firewall.py
    rationale: GENERAL cage (11 detectors, credential shapes, recursion ban)
    added: 2026-04-26
    added_by: operator
  - repo: jarvis
    path_glob: backend/core/ouroboros/governance/semantic_guardian.py
    rationale: Pre-APPLY pattern detector (10 patterns, raises friction)
    added: 2026-04-26
    added_by: operator
  - repo: jarvis
    path_glob: backend/core/ouroboros/governance/scoped_tool_backend.py
    rationale: Mutation cage (structural COUNT gate, hard-kill wrapper)
    added: 2026-04-26
    added_by: operator
  - repo: jarvis
    path_glob: backend/core/ouroboros/governance/risk_tier_floor.py
    rationale: 4-tier risk ladder + composing knobs — escalation authority
    added: 2026-04-26
    added_by: operator
  - repo: jarvis
    path_glob: backend/core/ouroboros/governance/change_engine.py
    rationale: APPLY phase write surface; AST/placeholder validation
    added: 2026-04-26
    added_by: operator

  # The manifest itself (self-reference)
  - repo: jarvis
    path_glob: .jarvis/order2_manifest.yaml
    rationale: This file. Amendment requires the §6 protocol.
    added: 2026-04-26
    added_by: operator
```

### 3.3 Trinity-readiness

Adding J-Prime to the cage later is one entry per file, e.g.:

```yaml
- repo: jarvis-prime
  path_glob: app/handlers/generate.py
  rationale: J-Prime generation handler — Mind cognition surface
  added: <future>
  added_by: <future-operator-decision>
```

No schema change, no logic change. The `RepoRegistry` lookup already resolves
`jarvis-prime` to the configured filesystem path via `JARVIS_PRIME_REPO_PATH`.

### 3.4 Authority invariant

The manifest is **read** by:
- The Order-2 risk classifier (§4) at GATE phase
- The MetaPhaseRunner AST validator (§6) at GENERATE phase

The manifest is **written** only by the §7 amendment protocol — never directly
by O+V, never by APPLY, never by AutoCommitter. AST-grep-pinned: any future
import of `Order2Manifest` outside `risk_tier_floor.py`, `meta_phase_runner.py`,
and the manifest amendment path is a CI failure.

## 4. Order-2 risk class

### 4.1 The new tier

Today's ladder (`backend/core/ouroboros/governance/risk_tier_floor.py`):

```
SAFE_AUTO < NOTIFY_APPLY < APPROVAL_REQUIRED < BLOCKED
```

Pass B adds:

```
SAFE_AUTO < NOTIFY_APPLY < APPROVAL_REQUIRED < BLOCKED < ORDER_2_GOVERNANCE
```

`ORDER_2_GOVERNANCE` is **strictly above** `BLOCKED`. `BLOCKED` says "this op
will not run autonomously, but a human can override at the REPL." `ORDER_2_GOVERNANCE`
says "this op cannot run **even with operator REPL approval** — it requires
the §7 amendment protocol." Three properties:

1. Auto-apply forbidden at every nominal tier including `SAFE_AUTO`.
2. REPL `approve <op-id>` does **not** clear an `ORDER_2_GOVERNANCE` op.
3. The op is routed to a dedicated `order2_review` queue with its own SLO
   (operator-driven; no autonomous timeout escalation).

### 4.2 Classifier integration

The classifier hook lives at GATE phase, in `phase_runners/gate_runner.py` (W2(5)
Slice 4a.2 extracted location). Pseudocode:

```python
def classify_order2(candidate, manifest: Order2Manifest) -> bool:
    """Return True if any candidate file path matches a manifest entry."""
    for change in candidate.iter_changes():  # multi-file aware
        for entry in manifest.entries:
            if change.repo == entry.repo and fnmatch(change.path, entry.path_glob):
                return True
    return False
```

If `classify_order2(...)` returns `True`, GATE forces `risk_tier = ORDER_2_GOVERNANCE`
**after** the existing risk-tier-floor composition (so a `JARVIS_PARANOIA_MODE=1`
override cannot accidentally lower an Order-2 op below itself).

## 5. AST-shape validator for new PhaseRunner subclasses

### 5.1 The validation contract

Any candidate file that introduces a new `PhaseRunner` subclass must pass:

1. **ABC conformance.** Class inherits from `PhaseRunner` (`phase_runner.py:91`).
2. **`phase` attribute.** Class sets a `phase: OperationPhase` class attribute
   that resolves to a known phase enum value.
3. **`run` signature.** Implements `async def run(self, ctx: OperationContext) -> PhaseResult`.
   Coroutine, single ctx arg, return type `PhaseResult` (frozen dataclass per `phase_runner.py:50`).
4. **No mutation of input ctx.** Body never assigns to `ctx.<attr>`. Required:
   produces new ctx via `ctx.advance(...)` (`phase_runner.py:101`).
5. **No raise into dispatcher.** Top-level try/except wraps `run` body; uncaught
   exceptions are converted to `PhaseResult(status="fail", reason=...)` before
   return (`phase_runner.py:103-104`).
6. **No imports from the Order-2 manifest paths.** A new runner cannot
   `from .semantic_firewall import ...`, `from .change_engine import ...`,
   etc. — that would be Order-2 transitive authority creep. Allowed imports:
   `phase_runner` ABC, `op_context`, `subagent_contracts`, stdlib, third-party.

### 5.2 Implementation surface

`backend/core/ouroboros/governance/meta/ast_phase_runner_validator.py` (new file).
Pure AST walk via `ast.parse` on the candidate's `full_content` — zero runtime
introspection, zero LLM, deterministic verdict. Failure raises
`PhaseRunnerASTValidationError` with the rule that fired.

### 5.3 Where it fires

Inside the GATE phase (after Order-2 classification, before risk-tier finalization).
A candidate that touches `phase_runners/*.py` AND introduces a new class:
- Must pass §5 validator → if fail, op routes to `order2_review` queue with
  `reason=phase_runner_ast_invalid:<rule>`
- Must pass §6 shadow pipeline → if fail, op routes to `order2_review` with
  `reason=shadow_replay_regression:<details>`
- THEN still hits §4 ORDER_2_GOVERNANCE risk class → operator amendment protocol

Three independent gates compose. Each is structural (deterministic, replayable,
zero-LLM). The MetaPhaseRunner (§7) does not replace any of them — it fills
the gap between "model proposes a new runner" and "the gates evaluate it."

## 6. Shadow pipeline + golden-replay corpus

### 6.1 The thesis

A new `PhaseRunner` subclass is structurally well-formed (§5) but might still
break the FSM dynamically — mishandle a corner case, change behavior on retry,
silently drop telemetry. Replay-against-golden is the regression cage.

### 6.2 The corpus

`.jarvis/order2_replay_corpus/` — a curated set of completed Order-1 ops
(serialized `OperationContext` snapshots at each phase boundary). Schema:

```
.jarvis/order2_replay_corpus/
  manifest.yaml         # Index: op_id → snapshot path + tags
  ops/
    op-019d9368-654b/   # The Session U-W multi-file APPLY (battle-test landmark)
      classify.json
      route.json
      ...
      complete.json
    op-019d7d3e-.../    # The 2026-04-11 first-sustained-completion op
      ...
```

Initial corpus: ~20 ops drawn from the battle-test breakthrough log
(`docs/architecture/OUROBOROS.md` "Battle Test Breakthrough Log" section,
referenced in `CLAUDE.md`). Selection criteria:
- 5 happy-path single-file
- 5 multi-file (Session U-W class)
- 5 retry/L2 paths
- 3 ORANGE-tier (pre-APPLY block)
- 2 with semantic-firewall hits (mutation cage rejections)

### 6.3 Replay semantics

For a candidate runner replacing phase X:
1. For each corpus op, take the `ctx` snapshot at phase X-1 (pre-X input).
2. Run the candidate runner against that ctx.
3. Compare the produced `PhaseResult` against the recorded post-X `ctx` and the
   recorded `next_phase` / `status` / `reason`.
4. Comparison metric: byte-identical for `next_phase`, `status`, `reason`;
   structural-equality for `next_ctx` (whitelisted fields — phase log entries
   can differ in timestamp; risk_tier, op_id, candidate set must match).
5. Any divergence → `shadow_replay_regression` failure with the corpus op
   that failed and the diff.

### 6.4 Why this is parity, not test

Parity replay is what W2(5) already uses for inline-vs-runner tests. Pass B
extends the same discipline to runners-O+V-might-generate. The corpus *is* the
golden — a new runner that diverges from how the inline (or graduated W2(5))
phase ran on those exact ops is a regression by definition.

### 6.5 Corpus amendment protocol

Adding/removing/replacing corpus ops is itself an Order-2 manifest amendment
(the corpus is part of the cage). §7 protocol applies. This prevents O+V from
proposing a new runner *and* a smaller corpus that conveniently passes.

## 7. The MetaPhaseRunner primitive

### 7.1 What it does

The `MetaPhaseRunner` is the single component O+V uses to propose a new
`PhaseRunner` subclass. It composes §3 + §4 + §5 + §6:

1. Receives an op whose target file matches `phase_runners/*.py` (manifest hit).
2. Routes the op through the **standard 11-phase pipeline** (CLASSIFY → COMPLETE)
   using the existing PhaseRunners — the FSM cognizes about its own extension.
3. At GATE: §4 classifier flips `risk_tier = ORDER_2_GOVERNANCE`.
4. Pre-APPLY: §5 AST validator runs.
5. Pre-APPLY: §6 shadow replay runs against the corpus, with the candidate
   runner *substituted in* for the phase it claims to replace/extend.
6. If §5 + §6 pass: op **does not auto-apply**. It enters the `order2_review`
   queue with the full evidence bundle (AST verdict, replay diff, candidate
   AST tree, rationale).
7. Operator approves via §7 amendment protocol → APPLY runs → AutoCommitter
   commits with `Order-2-Authorized-By: <operator>` trailer.

### 7.2 What it doesn't do

- It does not generate runners autonomously. O+V proposes; operator authorizes.
- It does not bypass any existing gate. Iron Gate, SemanticGuardian, SemanticFirewall,
  exploration ledger — all run as normal. ORDER_2_GOVERNANCE is *additive*.
- It does not handle non-runner Order-2 patches (e.g. modifying `semantic_firewall.py`
  itself). Those still match the manifest and get ORDER_2_GOVERNANCE risk-class,
  but the §5 AST validator is PhaseRunner-specific. Other Order-2 governance code
  takes the manifest hit + operator amendment path with no AST validator —
  which is correct: there's no shape contract to validate against for a one-off
  governance file.

## 8. Manifest-amendment protocol

### 8.1 Authority

The Order-2 manifest is amendable **only by operator authorization**. Three
properties:

1. **No SAFE_AUTO ever.** Amendments are not eligible for any auto-apply tier.
2. **No O+V autonomous proposal-then-auto-merge.** O+V *can* propose an
   amendment (e.g. "this newly-created governance file should be in the
   manifest") — the proposal lands in the same `order2_review` queue.
3. **Operator-explicit acknowledgment.** The REPL approval surface for
   manifest amendments is a distinct command (`/order2 amend <op-id>`),
   not the generic `approve <op-id>`. This prevents reflex-approval.

### 8.2 The locked invariant

`JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR=true` is **not a
graduation candidate**. It ships locked-true. Flipping it is itself an
Order-2 governance change (it would lower the cage). The flag exists only
so the property is greppable and asserted in tests; flipping it false in
code review is rejected on sight.

## 9. Slice plan (drafted; gated on W2(5) Slice 5b)

| Slice | Deliverable | Env flag (default) | Graduation criteria |
|---|---|---|---|
| **1** | `Order2Manifest` schema + loader + `.jarvis/order2_manifest.yaml` Body-only initial entries + grep-pinned authority invariant. **No enforcement.** | `JARVIS_ORDER2_MANIFEST_LOADED` (false) | 1 clean session: manifest loads at boot, no behavior change |
| **2** | Order-2 risk class + integration with `risk_tier_floor.py` composition. GATE classifier hook in `phase_runners/gate_runner.py`. | `JARVIS_ORDER2_RISK_CLASS_ENABLED` (false) | 3 clean sessions where a manifest-matching op is correctly routed to `order2_review` instead of normal tier |
| **3** | AST-shape validator (`meta/ast_phase_runner_validator.py`) + 6-rule check + `PhaseRunnerASTValidationError` raising + GATE wiring. | `JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED` (false) | 3 clean sessions; validator never false-positives on existing graduated runners (regression spine: parity-test against all `phase_runners/*.py`) |
| **4** | `.jarvis/order2_replay_corpus/` initial 20 ops + replay harness + structural-equality diff. | `JARVIS_SHADOW_PIPELINE_ENABLED` (false) | 3 clean sessions; corpus replay passes byte-identically against current graduated runners |
| **5** | `MetaPhaseRunner` primitive composing §3+§4+§5+§6. End-to-end live-fire: O+V proposes a no-op runner extension; full pipeline runs; op lands in `order2_review`. | `JARVIS_META_PHASE_RUNNER_ENABLED` (false) | 1 live-fire success + 3 clean sessions; first operator-authorized amendment lands |
| **6** | Manifest-amendment protocol: `/order2 {pending,show,amend,reject,history}` REPL + `order2_review` queue surface + AutoCommitter trailer + corpus amendment recursion. | `JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR` (**locked true**) | Schema invariant, never flipped — graduation is "lands and stays locked" |

Per-slice discipline mirrors W2(5): each slice has a parity test, an authority
invariant, and a full-revert matrix. Defaults all `false` until per-slice
graduation. Pass B does **not** prescribe graduation dates; cadence determined
post-W2(5) Slice 5b.

## 10. Boundaries (what Pass B does NOT do)

1. **No adaptive Anti-Venom.** Gates remain static — same patterns, same
   thresholds, same operator-tunable knobs. Pass C scope.
2. **No subagent-kind generation.** O+V can propose new `PhaseRunner`
   subclasses (with §3-§8 cage); it cannot propose new subagent kinds
   (`agentic_*_subagent.py`). Future scope, no pass yet.
3. **No cross-Trinity logic change.** The manifest *supports* Trinity-wide
   entries (`repo: jarvis-prime`, `repo: jarvis-reactor`). The cross-repo
   patching authority itself is deferred per `OUROBOROS_VENOM_PRD.md:816`.
4. **No flag flips.** Every flag in §9 ships `false` (or locked-true). Pass B
   produces design + code surface for review; graduation cadence is a
   separate authorization, slice-by-slice, post-W2(5) Slice 5b.
5. **No new LLM calls in the cage.** AST validator is pure `ast.parse`. Shadow
   replay is structural diff. Order-2 classification is `fnmatch`. The cage
   is deterministic; only the *generation* (existing GENERATE phase) uses LLMs.

## 11. Dependencies + sequencing

**Hard prerequisites** (Pass B Slice 1 cannot start until):
1. W2(5) Slices 1–5a all graduated (defaults flipped `true`, inline FSM blocks
   removed from `orchestrator.py`).
2. W2(5) Slice 5b dispatcher cutover graduated.
3. Operator authorization to begin Pass B (separate from Pass B drafting).

**Soft prerequisites** (helpful but not blocking):
- W3(6) Slice 5 graduation (parallel L3 fan-out final stamp). Already deferred
  per `project_wave3_item6_graduation_matrix.md` ("officially closed
  architecturally 2026-04-25"). Not blocking Pass B.
- W3(7) graduation. Already complete per same memory.

**Pass C dependency on Pass B**:
Pass C (adaptive Anti-Venom, Gap 2.3) depends on Pass B Slice 6 existing — you
can't grow an adaptive immune system if amending the immune system itself
isn't a defined protocol. Pass C designs *how* gates learn; Pass B designs
*how their learning is itself authorized*.

## 12. Open design questions (deliberate, for Pass C handoff or operator decision)

1. **Per-Order mutation budget.** §3.4 of Pass A noted "Order-2 ops should
   get a much smaller mutation budget than Order-1 ops." Pass B does not
   change `ScopedToolBackend` budget defaults. Should Pass B Slice 2 add
   `JARVIS_ORDER2_MUTATION_BUDGET` (e.g. 1) on top of the standard cap?
   Pass C scope or Pass B follow-up?

2. **Corpus rotation policy.** §6.2 specifies an initial 20-op corpus.
   Should the corpus auto-include each successfully-amended Order-2 op as a
   new corpus entry (corpus grows monotonically), or stay frozen at 20 with
   manual rotation? Auto-grow gives stronger regression coverage; manual
   keeps the cage smaller and faster to replay. Operator preference.

3. **Shadow pipeline cost.** §6.3 replay against 20 ops is bounded compute
   (no LLM, just runner invocation + structural diff). But if corpus grows,
   runtime grows linearly. Worth a `JARVIS_SHADOW_PIPELINE_OP_LIMIT` so the
   replay can sample? Or always-full to never-flake? Bias toward always-full
   for the cage's first year; revisit if it becomes a wallclock bottleneck.

4. **Trinity activation criterion.** When does `(repo: jarvis-prime, ...)`
   become eligible to add to the manifest? Pass A §7 Q1 punted this; the
   answer is "when the deferred Trinity integration document lands." But
   Pass B Slice 1's manifest schema is Trinity-ready *now* — entries are
   just empty for Mind/Soul. Confirms decision but doesn't move it.

## 13. Interaction with existing memory

- **W2(5) memory entries** (`project_wave2_phaserunner_slice1.md` through
  `project_wave2_phaserunner_slice5a.md`): Pass B is downstream — the
  PhaseRunner ABC frozen in Slice 1 is Pass B's design surface.
- **`project_rsi_convergence.md`**: Wang's framework is orthogonal. Wang gives
  *why convergence is monotonic*; Pass B gives *what's allowed to mutate*.
  Both can coexist in the PRD.
- **`project_iron_gate_pushB.md`**: the four existing Iron Gate enforcements
  remain unchanged. Order-2 classification is *additive*, not a replacement.
- **`project_phase_b_subagent_roadmap.md`**: Phase B is cognitive *delegation*
  (Pass A finding). Pass B does not extend or modify Phase B subagent kinds.
  Future cognitive-primitive-generation work would build *on top of* both.

## 14. Vocabulary landing (post-Pass B, separate work item)

Per operator decision 2026-04-26, the Reverse Russian Doll Order axis lands
in `OUROBOROS_VENOM_PRD.md` as a new top-level section orthogonal to
Phase 1–6. Drafted content (suggested location: after §3 Architecture, before
§4 Phase 1 — placing Order vocabulary as the *frame* for what the phases
operate within):

> ### §3.5 The Reverse Russian Doll: Orders of Self-Reference
>
> *(complementary to §4–§9 Phase roadmap, orthogonal axis)*
>
> Where Phases describe **behavioral milestones** (Self-Reading → Self-Modeling),
> Orders describe **what O+V acts upon** — the layer of self-reference at
> which a given improvement operates.
>
> **Order 0 — Exoskeleton.** AI as compiled tool. Industry default. Ouroboros
> rejects this baseline by design.
>
> **Order 1 — Body (current state).** O+V as autonomic nervous system: 16
> sensors continuously scan the JARVIS Body; the 11-phase governed loop
> generates patches against application code, sensors, tooling, tests; the
> AutoCommitter graduates them into the persistent codebase. Manifesto §1
> (Unified Organism) and §6 (Threshold-Triggered Neuroplasticity) describe
> this Order.
>
> **Order 2 — Cognitive Substrate (horizon).** O+V turns inward and
> proposes modifications to its own cognitive architecture — orchestrator
> FSM, immune system gates, change engine, risk-tier ladder. **No Order-2
> change auto-applies. Ever.** All Order-2 candidates traverse:
> the Order-2 manifest classifier → a structural AST cage (PhaseRunner
> subclasses) → a regression-replay shadow pipeline → operator amendment
> protocol. The cage's authority cannot be lowered without itself being
> an Order-2 amendment. See `project_reverse_russian_doll_pass_b.md` for
> design.
>
> **Anti-Venom.** As O+V's outward (Order-1) reach grows, the inward (Order-2)
> cage must scale proportionally — patterns mined from POSTMORTEM corpus,
> exploration floors auto-tightening on category bypass, mutation budgets
> per Order. Adaptive Anti-Venom is Pass C scope.
>
> **Trinity scope.** The Order-2 manifest is `(repo, path-glob)`-keyed and
> Trinity-ready from day one. Initial deployment is Body-only; J-Prime and
> Reactor Core entries land when the deferred cross-Trinity integration
> document (referenced §15) lands.

Vocabulary-landing PR is a separate work item from Pass B Slices 1–6 and can
ship before W2(5) Slice 5b graduation (it's doctrine, not code).
