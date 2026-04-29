---
name: Priority 1 Confidence-Aware Execution — A-Level RSI Critical Path
description: 5-slice scope doc for §26.5.1 Confidence-Aware Execution (Probabilistic Posture); first concrete arc on the post-Phase-12 critical path; no hardcoding, leverages existing graduated primitives
type: project
---

Priority 1 Confidence-Aware Execution — scope doc for the first concrete arc on the post-Phase-12 A-Level RSI Critical Path (PRD §26.5.1).

**Why first:** highest unlock-to-effort ratio. Provider-side data (logprobs / top-k) is already on the wire and discarded. Capturing it turns the §25 verification loop from *passive* (records what happened) to *active* (intervenes mid-stream). Property Oracle claims pass/fail post-APPLY; confidence intervenes during GENERATE — the missing real-time signal. Single biggest CC-delta-closing primitive.

**How to apply:** treat as the load-bearing arc gating Priority 2 (Causality DAG) and Priority 3 (Adaptive Anti-Venom). Each slice gets its own master flag default false; per-slice 3-clean-session graduation cadence; AST-pinned authority invariants; full-revert matrix. No hardcoding — every threshold lives in FlagRegistry, posture-relevant, AdaptationLedger-tunable within Pass C's monotonic-tightening invariant once unblocked.

**Cost contract preservation (load-bearing throughout all 5 slices):** confidence-aware route routing (Slice 4) MUST NOT be able to escalate a BG/SPEC route to Claude regardless of confidence value. The §26.6 structural reinforcements (AST invariant + runtime assertion + Property Oracle claim) are PREREQUISITES for Slice 4 — Slice 4 cannot ship until §26.6 ships.

---

## Slice 1 — Logprob capture primitive

**Goal:** capture per-token logprobs from both providers into a structural artifact.

**Files extended:**
- `backend/core/ouroboros/governance/providers.py` — Claude stream parser captures `top_logprobs` from `content_block_delta` events (Anthropic API exposes this via `extra_headers={"anthropic-beta": "..."}` if needed; native if available).
- `backend/core/ouroboros/governance/doubleword_provider.py` — DW SSE stream captures OpenAI-compat `logprobs` field.
- `backend/core/ouroboros/governance/phase_capture.py` — extend record schema with optional `confidence_trace: List[float]` field (top-1 logprob per token).

**New module:** none. Leverage existing primitives.

**Master flag:** `JARVIS_CONFIDENCE_CAPTURE_ENABLED` default false. Asymmetric env semantics (empty/whitespace = default; explicit false hot-reverts).

**Authority invariants (AST-pinned):**
- Capture path is read-only on stream events; never modifies the model output.
- `ctx.confidence_trace` is append-only during GENERATE; immutable after.
- No logprob value influences any control flow in Slice 1 — pure capture.

**Tests:** ~25-30 deterministic + provider-mock integration tests covering: top-k captured when available, missing-logprobs gracefully degrade to None, oversized traces truncated at K-window cap (env-tunable, default 4096), schema round-trip through `phase_capture` Merkle hash, AST-pinned no-mutation invariant, master-off byte-for-byte preservation.

**Graduation criterion:** 3 clean soak sessions with `JARVIS_CONFIDENCE_CAPTURE_ENABLED=true` showing non-empty `confidence_trace` on every GENERATE call.

**Hot-revert:** single env knob → providers stop capturing → `confidence_trace` stays empty → downstream slices observe missing-data path.

---

## Slice 2 — Rolling-window confidence monitor + circuit-breaker

**Goal:** compute rolling top-1/top-2 margin and abort GENERATE on confidence collapse mid-stream.

**Files extended:**
- `backend/core/ouroboros/governance/phase_runners/generate_runner.py` (Slice 5a's extracted runner) — wire confidence-monitor consultation into the per-token streaming loop.
- `backend/core/ouroboros/governance/comm_protocol.py` — emit `HEARTBEAT` payload with `confidence_margin` field on transitions.

**New module:** `backend/core/ouroboros/governance/verification/confidence_monitor.py`
- `ConfidenceMonitor` class with rolling K-token window (default 16, env-tunable).
- `evaluate(token_logprobs: Sequence[Tuple[float, ...]]) -> ConfidenceVerdict` — computes top-1/top-2 margin, returns `OK` / `APPROACHING_FLOOR` / `BELOW_FLOOR`.
- Pure stdlib; no dependencies; never raises (defensive).

**Master flag:** `JARVIS_CONFIDENCE_MONITOR_ENABLED` default false.

**Tunables (FlagRegistry-typed, posture-relevant):**
- `JARVIS_CONFIDENCE_FLOOR` default `0.05` — minimum acceptable top-1/top-2 margin. HARDEN posture nudges to 0.10; EXPLORE nudges to 0.02 (within bounds).
- `JARVIS_CONFIDENCE_WINDOW_K` default `16` — rolling window size in tokens.
- `JARVIS_CONFIDENCE_APPROACHING_FACTOR` default `1.5` — `APPROACHING_FLOOR` triggers at `floor × factor`.

**Circuit-breaker behavior:**
- `BELOW_FLOOR` → abort GENERATE round, write `confidence_collapse` artifact to ctx, route to GENERATE_RETRY with structured feedback (the partial output + window of low-confidence tokens).
- `APPROACHING_FLOOR` → emit `model.confidence_approaching` SSE (Slice 4); no abort.
- `OK` → no-op; stream continues.

**Authority invariants (AST-pinned):**
- ConfidenceMonitor is pure-data; no I/O, no mutation, no external dependencies.
- Circuit-breaker hook in `generate_runner.py` does NOT call any provider; only signals the runner's existing retry path.
- Failure mode under master-off: monitor short-circuits to OK on every call; all behavior preserved byte-for-byte.

**Tests:** ~35-40 deterministic tests covering: rolling-window math correctness (12 §-numbered cases), threshold transitions, posture-relevance application, master-off short-circuit, never-raises on malformed logprobs, circuit-breaker abort triggers GENERATE_RETRY with correct artifact shape, AST authority pins.

**Graduation criterion:** 3 clean soaks where ConfidenceMonitor evaluated ≥ 100 GENERATE rounds, ≥ 1 `BELOW_FLOOR` triggered with successful retry, no false-positive aborts on confidently-emitted ops.

**Hot-revert:** single env knob → monitor returns OK on every call → no aborts ever fire.

---

## Slice 3 — HypothesisProbe integration on confidence collapse

**Goal:** when ConfidenceMonitor aborts a GENERATE round, dispatch HypothesisProbe (§25 Priority C) with the partial output as evidence to determine whether to retry-with-different-strategy or escalate to NOTIFY_APPLY.

**Files extended:**
- `backend/core/ouroboros/governance/verification/hypothesis_probe.py` — extend with new probe type `confidence_collapse_diagnostic` (existing primitive's bounded contract preserved: depth ≤ 3, budget ≤ $0.05, wall ≤ 30s).
- `backend/core/ouroboros/governance/phase_runners/generate_runner.py` — on circuit-breaker abort, hand off to probe; consume `ProbeResult` to decide `RETRY_WITH_FEEDBACK` vs `ESCALATE_TO_OPERATOR`.
- `backend/core/ouroboros/governance/verification/hypothesis_consumers.py` — add `probe_confidence_collapse` consumer (mirrors `probe_trivial_op_assumption` from §25 Priority C).

**Master flag:** `JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED` default false.

**Probe hypothesis shape:**
- Claim: "the model's confidence collapse on op `<op_id>` indicates epistemic distress (not stylistic variation)"
- Evidence required: partial output, low-confidence token window, prior op outcomes for similar signal source
- Convergence: CONFIRMED (escalate to NOTIFY_APPLY) / REFUTED (retry with different sampling) / INCONCLUSIVE (retry with reduced thinking budget)

**Authority invariants (AST-pinned):**
- Probe is read-only by AST enforcement (already pinned in §25 Priority C).
- Probe budget is bounded: cannot exceed `$0.05/op`, depth ≤ 3, wall ≤ 30s.
- Failed probes recorded immutably at `.jarvis/failed_hypotheses.jsonl` (existing infrastructure from §25 Priority C) so adversarial retries cannot loop.

**Tests:** ~25-30 deterministic tests covering: probe dispatched on circuit-breaker abort, probe respects existing bounds, RETRY_WITH_FEEDBACK threads structured guidance back to GENERATE prompt, ESCALATE_TO_OPERATOR raises risk_tier to NOTIFY_APPLY, INCONCLUSIVE path reduces thinking budget on next round, AST-pinned read-only invariant.

**Graduation criterion:** 3 clean soaks with ≥ 5 confidence-collapse events, ≥ 50% RETRY_WITH_FEEDBACK convergence rate, zero infinite-curiosity loops (HypothesisProbe's existing depth/budget caps proven in production).

**Hot-revert:** single env knob → no probe dispatched → circuit-breaker abort falls back to default GENERATE_RETRY with empty feedback.

---

## Slice 4 — Observability + route-aware routing (cost-contract-preserving)

**Goal:** broadcast confidence-drop as a first-class SSE event class + extend `urgency_router.py` to use rolling confidence-trace history as a routing input.

**PREREQUISITE:** §26.6 Cost Contract Structural Reinforcement MUST ship before Slice 4. Slice 4 cannot weaken the cost contract; the §26.6 invariant + runtime assertion + Property Oracle claim are the structural guarantees that even confidence-driven route changes preserve "BG never cascades to Claude."

**Files extended:**
- `backend/core/ouroboros/governance/ide_observability_stream.py` — add 3 new SSE event types: `model.confidence_drop` (P1, abort fired), `model.confidence_approaching` (P2, near floor), `model.sustained_low_confidence` (P3, posture nudge candidate).
- `backend/core/ouroboros/governance/urgency_router.py` — consume `ctx.confidence_trace_history: deque[float]` (rolling N-op summary). Recurring low-confidence ops in BG → propose route demotion to SPECULATIVE (advisory; never auto-promotes to higher cost). Recurring high-confidence ops in COMPLEX → propose demotion to STANDARD (cost optimization).
- `backend/core/ouroboros/governance/postmortem_observability.py` — extend `/postmortems` REPL with `confidence-distribution` subcommand.

**Master flag:** `JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED` + `JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED` (both default false; independent).

**Cost contract enforcement (load-bearing):**
- Route routing proposals are ADVISORY ONLY — they emit `route_proposal` SSE events; they do NOT mutate `ctx.route` directly.
- `urgency_router.py` MUST NEVER propose escalation from BG/SPEC → STANDARD/COMPLEX/IMMEDIATE based on confidence alone. AST-pinned: `_propose_route_change()` body MUST contain `if proposed_route in BG_SPEC_ROUTES.complement and current_route in BG_SPEC_ROUTES: raise CostContractViolation(...)`.
- The §26.6 runtime structural assertion in `providers.py` provides defense-in-depth: even if the proposal somehow lands, dispatch refuses.

**Authority invariants (AST-pinned):**
- SSE events are loopback-only, rate-limited (existing IDEStreamRouter contract).
- Route proposals do not write to ctx.route; only emit advisory events.
- `urgency_router.py` cannot import any provider module (cost-contract isolation).

**Tests:** ~30-35 deterministic tests covering: 3 SSE event types fire on correct conditions, severity tiers correct, route-proposal logic for each (route → confidence) cell, **cost-contract assertions for BG-never-cascades** (5+ tests), AST-pinned invariants.

**Graduation criterion:** 3 clean soaks with ≥ 10 confidence-drop SSE events, route-proposal events emitted but NEVER auto-applied, cost-contract violation never observed (verified by §26.6 Property Oracle claim `cost.bg_op_used_claude_must_be_false` passing on 100% of BG/SPEC postmortems).

**Hot-revert:** two independent env knobs → SSE events drop / route proposals stop emitting.

---

## Slice 5 — Graduation flip + AST authority invariants

**Goal:** flip all 5 master flags from Slices 1–4 default false→true after per-slice graduation cadence; add shipped-code structural invariants that survive future patches.

**Master flags flipped (5 total):**
- `JARVIS_CONFIDENCE_CAPTURE_ENABLED` → true
- `JARVIS_CONFIDENCE_MONITOR_ENABLED` → true
- `JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED` → true
- `JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED` → true
- `JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED` → true (only after §26.6 ships)

**Files extended:**
- `meta/shipped_code_invariants.py` — add 4 new structural invariants:
  1. `confidence_capture_no_mutation` — Slice 1's read-only AST pin enforced at boot + APPLY.
  2. `confidence_monitor_pure_data` — Slice 2's no-I/O AST pin enforced.
  3. `confidence_probe_bounded` — Slice 3's depth/budget/wall caps AST-validated.
  4. `urgency_router_no_bg_escalation` — Slice 4's cost-contract AST pin (composes with §26.6 invariant).
- `flag_registry.py` — register all 5 confidence flags with category `verification`, posture-relevance `RELEVANT`, examples + descriptions per FlagRegistry pattern.
- Pre-graduation pin renames in all 5 owner suites per the embedded discipline (`test_master_flag_default_*_post_graduation`).

**Authority invariants (AST-pinned, full-revert matrix):**
- All 5 master-off paths byte-identical to pre-Slice-1 baseline.
- Cost contract held at 3 layers (§26.6) + Slice 4's AST pin = 4-layer defense-in-depth.
- AdaptationLedger (Pass C, when unblocked) can adjust confidence floor / window-K within FlagRegistry-declared bounds; cannot adjust master flags (locked-true per Pass B).

**Layered evidence target:** ~120-150 deterministic tests + 4 in-process live-fire smoke checks + ~20 graduation pins + 3 clean soak sessions.

**Hot-revert:** single env knob per slice → independent revert paths.

---

## Sequencing summary

| Slice | Depends on | Ship after | Ships in parallel with |
|---|---|---|---|
| 1 — Logprob capture | none | — | §26.6 cost contract reinforcement (independent arcs) |
| 2 — Confidence monitor + breaker | Slice 1 | Slice 1 graduates | — |
| 3 — HypothesisProbe integration | Slice 2 + §25 Priority C (DONE) | Slice 2 graduates | — |
| 4 — Observability + route routing | Slice 3 + **§26.6 (PREREQUISITE)** | Slice 3 graduates AND §26.6 ships | — |
| 5 — Graduation flip | Slices 1–4 graduated | Slice 4 graduates | — |

**Estimated wall-clock:** ~1 week with focused execution + soak windows.

---

## Anti-pattern checklist (reject if any present)

- [ ] Hardcoded confidence floor (must live in FlagRegistry, posture-relevant)
- [ ] Hardcoded window size (must be FlagRegistry-tunable)
- [ ] Hardcoded probe budget (must reuse §25 Priority C bounds)
- [ ] Synchronous blocking on probe dispatch (must use existing async infrastructure)
- [ ] Direct ctx.route mutation in route routing (must be advisory SSE only)
- [ ] BG/SPEC → STANDARD/COMPLEX/IMMEDIATE escalation path (cost contract violation; AST-pinned reject)
- [ ] Provider-module import in urgency_router.py (cost-contract isolation broken)
- [ ] Duplicating HypothesisProbe primitive (must extend §25 Priority C)
- [ ] New module that could live as extension of existing graduated primitive
- [ ] Test that asserts on internals not contract (Iron Gate § discipline)

---

## Reverse Russian Doll alignment

- **Outer shell expansion:** Confidence-aware execution lets O+V *act* on uncertainty rather than just record it. The shell genuinely expands — autonomous mid-stream intervention becomes possible.
- **Anti-Venom proportional scaling:** every confidence-driven decision is bounded — circuit-breaker is structural (Slice 2 AST-pinned), probe is bounded (Slice 3 reuses §25 Priority C bounds), route routing is advisory (Slice 4 AST-pinned), cost contract is enforced at 4 layers (§26.6 + Slice 4 AST pin).
- **Order-2 readiness:** the confidence floor is itself an Order-2 governance object (FlagRegistry-typed, posture-relevant, AdaptationLedger-tunable within bounds, locked-true amendment via Pass B Slice 1 when unblocked).
- **No hardcoding:** every threshold, window size, and probe bound lives in FlagRegistry; posture-relevance assigned; AdaptationLedger adjusts within Pass C's monotonic-tightening invariant when unblocked.
