# Cognitive Degradation Architecture — Design Spec

**Date:** 2026-06-28
**Author:** Derek J. Russell + O+V (Claude Opus 4.8)
**Status:** Design locked (brainstorm track) — pending user spec review → writing-plans
**Topic:** Epistemic Escalation (Micro plane) + Systemic Cognitive Collapse Detection (Macro plane)

> **Context gate.** This is **Step 2** of a two-step sequence. **Step 1** (the clean DW-only A1 cloud confirm
> proving `written=True` durable commit, failover pinned OFF) must be **green** before *any* code in this spec
> is wired live. This document is the "while we wait" design work; it is build-staged, not build-now.

---

## 1. Problem & Motivation

The Sovereign Failover Mesh currently awakens J-Prime off a **transport/availability** signal
(`failover_lifecycle.is_global_outage(route)` driven by `record_sweep`). That trigger was fixed (run #11
blindspot) to read the real per-route generation-failure signal rather than the cheap `HeavyProbe`.

But two gaps remain:

1. **No cognitive-failure axis.** A provider can be *transport-healthy* while its *cognition* collapses
   (returns zero tool calls, produces the identical rejected patch repeatedly, exhausts the Iron Gate). The
   failover mesh is blind to this.
2. **Capability inversion.** J-Prime (Qwen2.5-Coder-7B, CPU) is a **weaker** model than DW (397B). Failing
   *cognition* over to a weaker model makes cognition worse, not better. J-Prime is an **availability**
   fallback, never a **cognitive** one.

**Thesis:** cognitive degradation and availability degradation are **different planes** and must be answered
differently. Availability fails *over* (toward J-Prime, cross-op). Cognition escalates *up* (toward the
policy's superior reasoning tier, per-op) — never down.

## 2. Goals / Non-Goals

**Goals**
- Detect per-op cognitive degradation **structurally** (not by log/string matching) and answer it with an
  in-FSM escalation ladder: re-parameterize → reflect → escalate-tier.
- Detect **systemic** cognitive degradation across ops (a globally bad model) and — eventually — demote the
  degraded model fleet-wide to the reasoning tier, with full guardrails.
- **Zero hardcoded model names.** Every "superior reasoning model" reference resolves dynamically through
  `brain_selection_policy.yaml` / `ProviderTopology`.
- **Zero duplication.** Extend existing machinery (`RepairEngine`, the Adaptive Epistemic Feedback Matrix,
  `failover_lifecycle`'s gradient) — do not stand up parallel subsystems.

**Non-Goals**
- Not changing the **transport** failover path (J-Prime awaken) at all.
- Not arming the Macro plane to *enforce* on day one (shadow-first; see §5).
- Not touching the Step-1 file-isolation pin or the failover pin.

## 3. Symbol Grounding (the no-hallucination audit)

| Concept | Real symbol | Status |
|---|---|---|
| "TopologyRegistry" | `governance/provider_topology.py :: ProviderTopology` (loads `brain_selection_policy.yaml`; `dw_allowed`, `effective_dw_models`, severed-threshold `monitor:` section) | ✅ use as model-tier authority |
| superior reasoning model | resolved via `brain_selection_policy.yaml` fallback/escalation chain through `ProviderTopology` (Claude = policy Tier 1) | ✅ resolve dynamically — **never a literal string** |
| Phase 1 — Re-Parameterization | "Adaptive Epistemic Feedback Matrix (T2)" `providers.py:5203` — temperature drop on repeated `failure_signature_hash` | ✅ already built |
| Phase 2 — Reflection Injection | `repair_engine._generate_repair_candidate` (≈1682) prompt build; A1Trace source `governance/a1_trace.py :: _emit_ledger` | 🆕 new (hooks exist) |
| Phase 3 — Model Escalation | `repair_engine` per-iter provider isolation (Slice 5A) + `ProviderTopology` tier resolution | 🔧 extend |
| candidate signature | `repair_engine.py:343 :: _patch_sig(diff)` + `_count_diff_*` | ✅ reuse, upgrade to AST hash |
| `is_global_outage` / gradient | `governance/failover_lifecycle.py` (`record_sweep`, `is_global_outage`, `is_degrading`) | 🔧 extend with cognitive dimension |
| `ForwardProgress STUCK` | **no such symbol** — defined here in §4.1 | ⚠️ define, do not invent a detector |

**Planning-phase verification (must confirm before code):**
- Exact `ProviderTopology` runtime-demote / severed-threshold mutation API (read paths only confirmed so far).
- The countable Iron Gate exhaustion event (`ExplorationInsufficientError` / GENERATE_RETRY exhaustion) — where
  it surfaces and how to subscribe without importing `iron_gate` into observe-only modules.
- `repair_engine._generate_repair_candidate` internal prompt-assembly seam for Phase-2 injection.
- Note: ignore the ` 2.py` / ` 3.py` duplicate files in `governance/` — use canonical names only.

## 4. Micro Plane — per-op Epistemic Escalation (ENFORCE day 1)

Blast radius = one op, so it enforces immediately. Lives **inside** the L2 `RepairEngine` FSM.

### 4.1 `CognitiveStuckEvent` — the mathematical STUCK definition

> Structural, not string-matched. Detected inside the FSM.

```
Let sig(c) = normalized_ast_hash(candidate_c)        # whitespace/comment-invariant; falls back to _patch_sig(diff)
STUCK when:  sig(C_N) == sig(C_{N-1}) for >= 3 consecutive repair iterations
             AND Phase-1 temperature mutation was applied between those iterations
=> RepairEngine programmatically yields CognitiveStuckEvent(op_id, sig, iters, last_temp)
```

The "despite temperature mutation" clause is load-bearing: identical output *after* sampling was perturbed is
strong evidence of a deterministic cognitive loop, not sampling noise. (Distinct from the existing
`failure_signature_hash` axis, which is about the *test/failure* identity; this is about *candidate* identity.)

### 4.2 The escalation ladder (in-FSM)

| Rung | Trigger | Action | Symbol |
|---|---|---|---|
| Phase 1 | same `failure_signature_hash` repeats | lower temperature (existing) | Adaptive Epistemic Feedback Matrix T2 |
| Phase 2 | `CognitiveStuckEvent` (first) | inject A1Trace into repair **system prompt** as a Reflection directive: *"You failed the Iron Gate N times due to X. Identify the logical fallacy in your prior attempts and formulate a NEW ReAct strategy."* | `_generate_repair_candidate` + `a1_trace._emit_ledger` |
| Phase 3 | FSM near exhaustion (`current_iteration >= max_iterations_live - 1`) **or** repeated `CognitiveStuckEvent` | route **this op** to the policy's superior reasoning tier (resolved via `ProviderTopology`, never hardcoded) | per-iter provider isolation (Slice 5A) |

Reflection-injection content is **quarantined as inert DATA** (fenced), consistent with the Semantic Firewall
discipline — the A1Trace is untrusted context, not an instruction to the governance layer.

## 5. Macro Plane — systemic collapse detection (SHADOW first → graduate to ENFORCE)

Blast radius = fleet-wide routing, so it ships **shadow-only** and graduates after a clean soak.

### 5.1 Aggregator — extends the existing gradient (no competing loop)

- Consumes `CognitiveStuckEvent` + Iron-Gate-exhaustion events across ops via a **leaky-bucket / rolling
  time-window** counter.
- **Integration:** adds a *cognitive dimension* to the existing `ProviderHealthGradient` /
  `failover_lifecycle` gradient — it does **not** instantiate a parallel aggregator. The transport
  `is_global_outage` axis and the new cognitive axis are siblings on the same gradient object.
- Threshold: `>= N` cognitive-collapse events within window `T` ⇒ candidate systemic degradation.

### 5.2 Shadow mode — the `ShadowSeveranceReceipt`

When the bucket breaches threshold in shadow mode, **no `ProviderTopology` mutation occurs**. Instead it emits
a rich JSON receipt (observability + SSE), schema `shadow_severance.v1`:

```json
{
  "schema_version": "shadow_severance.v1",
  "ts": "<iso8601>",
  "would_sever_model": "<model-id resolved from ProviderTopology>",
  "would_promote_tier": "<reasoning tier id from brain_selection_policy.yaml>",
  "leaky_bucket": { "level": 7.0, "capacity": 8.0, "leak_rate_per_s": 0.05, "window_s": 600 },
  "trigger_a1trace_ids": ["...", "..."],
  "trigger_events": [ { "op_id": "...", "kind": "CognitiveStuckEvent|IronGateExhaustion", "sig": "..." } ],
  "projection": {
    "est_cost_delta_usd_per_op": 0.012,
    "est_latency_delta_ms_per_op": 1800,
    "basis": "brain_selection_policy.s2_pricing + observed ttft"
  },
  "decision": "WOULD_SEVER (shadow — no mutation applied)"
}
```

The `projection` block reuses `aegis/pricing.py` (`brain_selection_policy.s2_pricing`) for the cost math —
again, no hardcoded prices.

### 5.3 Graduation gate (shadow → enforce)

Promote the flag to ENFORCE only after a soak shows the receipts fire on a **real** degradation with **zero
false-positives**. When enforcing, the mutation demotes the degraded model in `ProviderTopology` to the
reasoning tier, guarded by §5.4.

### 5.4 Guardrail invariants (apply when ENFORCE)

1. **Never sever the last provider** (fail-closed) — if demoting would leave no viable provider, refuse + alarm.
2. **Hysteresis + auto-restore** — recovery (cognitive axis back under threshold for a hold window) restores
   the prior topology; no flapping.
3. **Cost/SLO guard** — refuse a sever that projects cost above a bounded ceiling unless availability is at risk.
4. **Kill switch + observability** — master flag + SSE event on every state change; every decision is auditable.
5. **Capability-inversion invariant** — cognition NEVER demotes to a *weaker* tier; only up (or no-op).

## 6. Async / Thread-Safety

The Macro aggregator is async-native and shared across parallel execution threads (L3 fan-out). State
(leaky-bucket level, last-decision, topology snapshot) is guarded by an `asyncio.Lock`; counter updates are
atomic. No torn reads of the cognitive-health state under concurrent `CognitiveStuckEvent` ingestion.

## 7. Flags (all default-OFF; graduation-staged)

| Flag | Default | Plane | Meaning |
|---|---|---|---|
| `JARVIS_COGNITIVE_ESCALATION_ENABLED` | `false` | Micro | master for the per-op ladder (Phase 2/3) |
| `JARVIS_COGNITIVE_REFLECTION_INJECT_ENABLED` | `false` | Micro | Phase 2 reflection injection |
| `JARVIS_COGNITIVE_TIER_ESCALATION_ENABLED` | `false` | Micro | Phase 3 tier escalation |
| `JARVIS_COGNITIVE_MACRO_ENABLED` | `false` | Macro | master for the aggregator |
| `JARVIS_COGNITIVE_MACRO_MODE` | `shadow` | Macro | `shadow` \| `enforce` (graduation knob) |
| `JARVIS_COGNITIVE_STUCK_REPEATS` | `3` | Micro | consecutive-identical-candidate threshold |
| `JARVIS_COGNITIVE_MACRO_WINDOW_S` | `600` | Macro | leaky-bucket window |

(Names indicative; final names + `FlagRegistry` registration settled in the plan.)

## 8. Observability

- SSE events: `cognitive_stuck_detected`, `cognitive_escalation_applied`, `shadow_severance_receipt`,
  (enforce) `cognitive_sever_applied` / `cognitive_sever_restored`.
- `GET /observability/cognitive-health` — bounded read-only projection (leaky-bucket level, recent receipts).
- Every decision carries the triggering A1Trace IDs for end-to-end traceability.

## 9. Testing Strategy

- **Micro:** deterministic FSM tests — feed identical candidates across iterations, assert `CognitiveStuckEvent`
  fires at exactly the threshold *only when* a temp mutation occurred between; assert Phase 2 injects the
  fenced A1Trace; assert Phase 3 resolves the tier via a stubbed `ProviderTopology` (no literal model name in
  the test or the code).
- **Macro shadow:** drive synthetic collapse events; assert a schema-valid `ShadowSeveranceReceipt` and that
  `ProviderTopology` is **never mutated** in shadow.
- **Macro guardrails:** unit-prove never-sever-last-provider, hysteresis/auto-restore, capability-inversion.
- **Zero-dup guard:** assert the cognitive axis is recorded through the existing gradient object (no second
  aggregator instance).

## 10. Build Staging (proof-first)

1. Step-1 A1 confirm green (`written=True`) — **precondition, not part of this build.**
2. Micro plane (4.1 → 4.2), ENFORCE, behind its flags (default-OFF). Prove per-op.
3. Macro aggregator + `ShadowSeveranceReceipt`, SHADOW only. Prove receipts on real degradation.
4. Graduate Macro to ENFORCE with §5.4 guardrails — separate, gated, observed.

## 11. Risks

- **Macro false-positive severance** → mitigated by shadow-first + receipts + guardrails (§5.4).
- **Reflection-injection prompt bloat / injection** → fenced inert DATA + bounded A1Trace size.
- **AST-hash fidelity** (false STUCK on benign cosmetic diffs) → normalized AST hash, `_patch_sig` fallback,
  the "despite temp mutation" clause.
- **Tier-escalation cost** (per-op Claude routing) → bounded by the per-op blast radius + existing route cost
  controls; Macro's projection surfaces aggregate cost before any enforce.
