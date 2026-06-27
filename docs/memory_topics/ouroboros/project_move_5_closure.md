---
title: Project Move 5 Closure
modules: []
status: merged
source: project_move_5_closure.md
---

**Closed 2026-05-01.** Move 5 of the §27 v6 brutal-review autonomy
roadmap — closes the cognitive gap §28.3 v9 brutal review identified.

**Why:** O+V's Venom does multi-turn tools but had **no inner reasoning
between tool rounds** — CC's Extended Thinking has no analog. When
``confidence_monitor`` flagged ``APPROACHING_FLOOR``, the only
outcomes were ``RETRY_WITH_FEEDBACK`` / ``ESCALATE_TO_OPERATOR`` /
``INCONCLUSIVE``. ``ask_human`` was the only escape valve when
ambiguity bit — violated the "proactive autonomous opposite of CC"
operator binding.

**How to apply:** When confidence collapse triggers Slice 4's
``PROBE_ENVIRONMENT`` outcome, caller invokes
``execute_probe_environment(monitor, ambiguity_context)``:

  * Generates K=3 deterministic-template probes from context
  * Spawns parallel read-only tool calls via the canonical
    9-tool ``READONLY_TOOL_ALLOWLIST``
  * ``asyncio.as_completed`` — early-stop on convergence quorum
  * Maps verdict to base ConfidenceCollapseAction:
    - CONVERGED → reset monitor window + RETRY_WITH_FEEDBACK with
      canonical answer threaded as feedback
    - DIVERGED → ESCALATE_TO_OPERATOR
    - EXHAUSTED → INCONCLUSIVE (budget reduction × 0.5)
    - DISABLED/FAILED → safe legacy defaults

## What graduated (Slice 5)

Two master flags flipped false → true:

  * ``JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED``
  * ``JARVIS_READONLY_EVIDENCE_PROBER_ENABLED``

Asymmetric env semantics: empty/whitespace = unset = post-graduation
default; explicit ``0``/``false``/``no``/``off`` hot-reverts each
independently.

6 ``FlagSpec`` entries seeded in ``flag_registry_seed.SEED_SPECS``
(2 master flags with posture-relevance + 4 cap/mode knobs).

3 ``shipped_code_invariants`` AST pins:

  * ``confidence_probe_bridge_no_mutation_tools`` — bridge module
    AST walk verifies NO mutation tool name references in code
    (Name + Attribute nodes; docstring strings allowed).
  * ``readonly_evidence_prober_allowlist_pinned`` — every expected
    tool name appears as string literal; frozenset constant
    structurally present; no mutation tool names anywhere.
  * ``confidence_probe_cap_structure_pinned`` — cap helpers
    (max_questions / convergence_quorum /
    max_tool_rounds_per_question) present; min/max clamp pattern
    present; floor/ceiling constants pinned. Catches refactors
    that loosen caps below structural floor.

SSE event ``EVENT_TYPE_PROBE_OUTCOME = "confidence_probe_outcome"``
fires on every non-DISABLED probe loop completion via
``publish_probe_outcome`` (best-effort, lazy broker import,
debounceable). Wired into runner's terminal outcomes via
``_verdict_with_publish`` helper.

4 GET routes mounted via ``register_confidence_probe_routes``:

  * ``GET /observability/probe`` — flag state + cadence config
  * ``GET /observability/probe/config`` — env-knob snapshot
  * ``GET /observability/probe/allowlist`` — read-only 9-tool
    allowlist surfaced for operator audit
  * ``GET /observability/probe/stats`` — flag+cadence+SSE event

Each route master-flag-gated per request (live toggle without
re-mounting); 503 with master off; 200 with master on.

## The 5-slice arc

| Slice | Commit | Tests | Net |
|---|---|---|---|
| 1 — Bridge primitive | `70bddb1a20` | 64 | New module ~470 lines: 5-value ProbeOutcome enum (CONVERGED/DIVERGED/EXHAUSTED/DISABLED/FAILED) + frozen dataclasses + canonical_fingerprint sha256 dedup + compute_convergence pure decision function |
| 2 — Generator + Prober | `686a808c21` | 61 | Two new modules ~620 lines: AmbiguityContext + 4 template-set branches + READONLY_TOOL_ALLOWLIST 9-tool frozenset + QuestionResolver Protocol + ReadonlyToolBackend Protocol + _NullToolBackend safe default + ReadonlyEvidenceProber concrete impl |
| 3 — Async runner | `ccee0c8c51` | 26 | New module ~440 lines: parallel-with-early-stop via asyncio.as_completed; sync prober wrapped in asyncio.to_thread; cancellation via Task.cancel + asyncio.gather(return_exceptions); wall-clock cap with floor + ceiling; 6 outcome paths |
| 4 — PROBE_ENVIRONMENT wire-up | `cd352719b9` | 28 | 4th enum value + ConfidenceMonitor.reset_window() + new probe_environment_executor.py mapping ConvergenceVerdict → ConfidenceCollapseVerdict; backward-compat verified across 26 auto_action_router tests |
| 5 — Graduation | this commit | 56 | 2 master flag flips, 6 FlagRegistry seeds, 3 shipped_code_invariants AST pins, EVENT_TYPE_PROBE_OUTCOME SSE event + publisher, 4 GET routes via register_confidence_probe_routes, comprehensive graduation regression spine |

**Total: 5 commits, 235 new regression tests, ~3,400 net new lines.**

## Architecture overview

```
ConfidenceMonitor.evaluate() → APPROACHING_FLOOR
    ↓ (Slice 5b — production wire-up at providers.py call site)
classify → ConfidenceCollapseAction.PROBE_ENVIRONMENT
    ↓
execute_probe_environment(monitor, ambiguity_context)        [Slice 4]
    ├── run_probe_loop                                        [Slice 3]
    │   ├── generate_probes (deterministic templates)         [Slice 2]
    │   └── ReadonlyEvidenceProber.resolve (allowlist)        [Slice 2]
    └── compute_convergence (sha256 dedup)                    [Slice 1]
    ↓
ConvergenceVerdict
    ↓
ConfidenceCollapseVerdict mapping:
    CONVERGED  → reset monitor + RETRY("probe converged: <answer>")
    DIVERGED   → ESCALATE_TO_OPERATOR
    EXHAUSTED  → INCONCLUSIVE (budget × 0.5)
    DISABLED   → RETRY (safe default)
    FAILED     → INCONCLUSIVE
    ↓
SSE EVENT_TYPE_PROBE_OUTCOME (non-DISABLED only)              [Slice 5]
```

## Cost contract preservation (PRD §26.6, structurally inherited)

Probe execution is sub-op:

  * Probe runner consumes ``ConfidenceMonitor`` snapshots + tool
    outputs only; never reaches provider dispatch boundary.
  * Read-only EvidenceProber's tool calls go through Venom's
    existing ``tool_executor.py`` (when wired in Slice 5b
    follow-up) which already enforces ``cost_contract_assertion.
    assert_provider_route_compatible``.
  * Probe budget caps prevent cost amplification: K=3 probes ×
    K=5 calls/probe × 1.0× cost-factor = ≤ 15× single-call cost
    in worst case (rare convergence failure path); typical 1-2
    probes.
  * Escalation routes through existing
    ``ESCALATE_TO_OPERATOR`` (Move 3 path) which already enforces
    no-BG/SPEC-cascade.

## Authority invariants (AST-pinned)

Per-module forbidden-import lists progressively widen:

  * **confidence_probe_bridge.py** — stdlib ONLY (Slice 1 was
    pure-data primitive). Slice 5 graduation pins no
    mutation-tool name refs in code.
  * **confidence_probe_generator.py** — stdlib + Slice 1.
  * **readonly_evidence_prober.py** — stdlib + Slice 1.
    READONLY_TOOL_ALLOWLIST frozenset constant pinned by Slice 5
    AST validator.
  * **confidence_probe_runner.py** — stdlib + Slice 1+2 + (Slice
    5) ide_observability_stream (lazy import for SSE publisher).
  * **probe_environment_executor.py** — stdlib + Slice 1+2+3 +
    confidence_monitor + hypothesis_consumers.
  * **confidence_probe_observability.py** — stdlib + Slice 1+2+3.

All modules NEVER raise from public methods (defensive
everywhere).

## Mutation boundary still locked

Move 5 ships *advisory* probe loops only. The probe runner only
reads (read_file / search_code / get_callers / glob_files /
list_dir / list_symbols / git_blame / git_log / git_diff) — never
mutates. AST-pinned by allowlist + no-mutation-name graduation
pins.

## Operator binding (J.A.R.M.A.T.R.I.X.)

Three closed-taxonomy enums shape every code path:

  * ``ProbeOutcome`` — 5 values (CONVERGED, DIVERGED, EXHAUSTED,
    DISABLED, FAILED) — Slice 1
  * ``ConfidenceCollapseAction`` — 4 values post-Slice-4 (added
    PROBE_ENVIRONMENT to existing 3); the 4th is transient/trigger
    state, never terminal — Slice 4
  * ``GeneratorMode`` — 2 values (TEMPLATES, LLM); LLM falls
    through to TEMPLATES with logged warning until post-graduation
    slice — Slice 2

Mirrors Move 3 ``AdvisoryActionType`` / Move 4 ``BootSnapshotOutcome``
discipline.

## Knobs (Slice 5 graduation)

  * ``JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED`` — master,
    **graduated true**
  * ``JARVIS_READONLY_EVIDENCE_PROBER_ENABLED`` — sub-gate,
    **graduated true**
  * ``JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS`` (default 3, floor 2,
    ceiling 5)
  * ``JARVIS_CONFIDENCE_PROBE_CONVERGENCE_QUORUM`` (default 2,
    floor 2)
  * ``JARVIS_CONFIDENCE_PROBE_MAX_TOOL_ROUNDS`` (default 5,
    floor 1, ceiling 10)
  * ``JARVIS_CONFIDENCE_PROBE_WALL_CLOCK_S`` (default 30, floor 5,
    ceiling 120)
  * ``JARVIS_CONFIDENCE_PROBE_GENERATOR_MODE`` (templates | llm,
    default templates)

## What remains (NOT in this arc)

  * **/probe REPL command** — operator workflow polish; the GET
    endpoints + SSE event surface cover the load-bearing use cases.
    REPL is Slice 5b follow-up if operators ask. Same deferral
    pattern as Move 4's /invariant REPL.
  * **Production wire-up at providers.py** — connecting probe
    execution to the actual collapse-error path requires (a)
    extracting AmbiguityContext from ctx + (b) calling
    execute_probe_environment from the provider's collapse handler.
    Discrete piece deferred to Slice 5b.
  * **event_channel.py boot wiring** — mounting the GET routes via
    EventChannel.start (mirrors Move 4 invariant_drift
    register_invariant_drift_routes wiring). Slice 5b.

## Net trajectory after Move 5

§28 v9 brutal review's biggest cognitive gap is now structurally
filled. O+V can autonomously resolve epistemic ambiguity without
``ask_human`` interrupt for the common case (CONVERGED probe set).
For genuine ambiguity (DIVERGED), the system gracefully escalates
through the existing operator-review path.

The combined Tier 1 #1+#2+#3 + Move 5 Slices 1-5 lifts the
empirical floor from B+ toward A−. Move 6 (Generative Quorum) is
the next strategic move per §28.6.2 — kills Quine-class +
symbol-shape hallucination via K-way parallel candidate consensus
using L3 worktrees + subagent_scheduler substrate.

## Sibling architecture (NOT duplicated)

  * Phase 7.6 ``adaptation/hypothesis_probe.py`` — bounded probe
    primitive with three independent termination guarantees.
    Move 5 does NOT consume this — independent pipeline. The
    scope doc was over-generous; Move 5 ships its own
    QuestionResolver Protocol for the question→answer use case
    (different from Phase 7.6's per-round-evidence-for-claim).

  * Move 3 ``auto_action_router`` — verification → action loop
    operationally. Move 5 plugs into it via the
    ``ConfidenceCollapseAction`` enum that auto_action_router
    already consumes (string passthrough verified backward-compat).

  * Move 4 ``InvariantDriftAuditor`` — verification → action loop
    temporally (cross-op drift). Move 5 is intra-op (epistemic
    uncertainty). Together they bound uncertainty in both axes.
