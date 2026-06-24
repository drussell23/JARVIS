<!--
  THE TRINITY MANIFESTO
  Canonical architectural truth for the JARVIS Trinity AGI OS.
  Author: Derek J. Russell — RSI/AGI Researcher & Trinity Architect.
  This document is the source of truth for the Body/Mind/Nerves boundaries,
  the RSI Flywheel, and the absolute safety laws. When code and this document
  disagree, one of them is a bug — reconcile, do not assume.
-->

# The Trinity Manifesto

**The canonical architecture of a self-healing, self-improving, sovereign AGI organism.**

> An autonomous organism that develops *itself* across three repositories — generating its own improvements, training its own intelligence, and guarded by safety laws that are *structurally* impossible to disable. This document is the architectural truth: the boundaries, the mechanics, and the absolute laws.

---

## 0. The One-Paragraph Truth

JARVIS is a tri-partite organism. The **Body** (JARVIS) runs the autonomous development loop. The **Mind** (J-Prime) is a self-hosted LLM that generates code. The **Nerves** (Reactor-Core) train the Mind on the Body's own experience. An external provider (**DoubleWord**) does the cheap day-to-day generation; the Trinity exists so the organism *survives and improves itself* when that external dependency fails. The organism can author changes into all three repositories — but the one capability that could let it rewrite its own Mind unwatched is made **structurally impossible** by construction. Every autonomous power is **default-OFF, fail-CLOSED, and cost-bounded.**

---

## 1. Two Axes — Do Not Conflate Them

The single most common architectural confusion is mixing the **organism axis** (which repo) with the **provider axis** (who generates code). They are orthogonal.

### Axis A — The Trinity (the organism's body parts)

| Role | Repository | Owns |
|------|-----------|------|
| **Body** | `JARVIS` (this repo) | The autonomous development loop (Ouroboros), macOS integration, the 102K-line unified supervisor, all governance + safety. **Where O+V lives and runs.** |
| **Mind** | `jarvis-prime` | Self-hosted LLM inference — 11 GGUF specialist models, code → `Qwen2.5-Coder-7B`. OpenAI-compatible API. **Generates code, sovereign and offline.** |
| **Nerves** | `reactor-core` | Training / fine-tuning / experience-collection / model-deployment (SFT, DPO, LoRA, GGUF quantize). **Makes the Mind better over time.** |

### Axis B — The Provider Chain (who generates, when O+V needs code)

| Tier | Provider | Cost | Role |
|------|----------|------|------|
| 0 | **DoubleWord 397B** | $0.10/$0.40/M | **PRIMARY** — cheapest, does ~all day-to-day generation |
| 1 | Claude (Anthropic) | $3/$15/M | Fallback |
| 2 | **J-Prime** (the Mind) | VM-cost only | **Last-resort, self-hosted sovereign generator** |

**The relationship that matters:** DoubleWord is the cheap external workhorse. J-Prime is *sovereignty insurance* — it exists so a total DoubleWord collapse cannot kill the organism. And every DoubleWord generation produces *experience* that trains the J-Prime Mind. **DW does the work; the Trinity owns the sovereignty.**

---

## 2. The Ouroboros Loop (the Body's heartbeat)

The Body runs an 11-phase governance FSM that turns a detected need into a verified, committed change:

```
CLASSIFY → ROUTE → [CONTEXT_EXPANSION] → [PLAN] → GENERATE → VALIDATE → GATE → [APPROVE] → APPLY → VERIFY → COMPLETE
```

- **16 autonomous sensors** feed the intake (TestFailure, OpportunityMiner, RuntimeHealth, …).
- A **4-tier risk ladder** governs every change: `SAFE_AUTO` / `NOTIFY_APPLY` / `APPROVAL_REQUIRED` / `BLOCKED`.
- The **Iron Gate** hard-enforces exploration-first (2+ tool calls before any patch) and ASCII-strictness.
- **L2 self-repair** closes the loop when validation fails.

This is the engine. Everything below is what makes it *sovereign* and *self-improving*.

---

## 3. The RSI Flywheel (how the organism gets smarter)

Recursive Self-Improvement, mechanically:

```
EVERY O+V generation (DoubleWord primary, OR J-Prime failover)
   │  produces an OUTCOME: passed VALIDATE/Iron-Gate/VERIFY  OR  failed + an epistemic-repair trajectory
   ▼
[4a] DPO Pair Synthesizer (Body, $0)  — the failed AST node = REJECTED, the repaired node = CHOSEN
   │     · EPISTEMIC PURITY GATE (absolute): only true COGNITIVE failures (pytest assertion, syntax,
   │       logical) become pairs; INFRA failures (DoubleWord timeout, 503, fsm_exhausted) are DROP-FILTERED
   │       — fails SAFE: unknown → drop. We never train on a vendor's infrastructure problems.
   │     · GOLDEN RATIO: a pair requires BOTH a failed AND a test-verified repaired state. No half-pairs.
   ▼  async, fire-and-forget → reactor-core's ingestor
[4b] Reactor-Core training (GPU, demand-gated, budget-capped)  — DPO/SFT fine-tunes Qwen2.5-Coder-7B
   ▼
[4c] Zero-downtime redeploy to J-Prime  — the Mind serves the improved model on the next failover
```

**The elegance:** the flywheel spins on *normal DoubleWord operation* — every repair the Body makes becomes a native preference pair that trains the sovereign Mind. The Body's hard-won lessons teach its own backup brain, for free.

---

## 4. The Sovereign Failover Lifecycle (surviving vendor collapse)

When DoubleWord collapses **and** Claude is unavailable, the Mind awakens:

```
DORMANT (golden-image snapshot, ~$0.50/mo)
   │  ProviderHealthGradient.is_global_outage(DW) == True   (a RATE, not a hardcoded N)
   │  AND  Cryo-Trigger:  R_forecast > C_coldstart · margin   (skip blips — don't wake for a flicker)
   ▼
AWAKEN J-Prime from the golden image  →  VRAM pre-warm  →  SERVING (route generation to the local Mind)
   │  cheap recovery-probe loop (observed, not predicted)
   ▼
DW sustainably healthy  →  [SOVEREIGN YIELD: UPSTREAM RECOVERED]  →  route back  →  delete-to-snapshot
```

- **Recovery Forecaster:** EWMA + within-outage velocity gradient + a **Data Poverty Override** (N<5 samples → LOW_CONFIDENCE → a safe fixed interval; never hallucinate a forecast on thin data).
- **Observed-gated authority (load-bearing law):** the forecast only *paces*; reality (an observed probe) *decides*. A wrong forecast is a bounded cost wobble, never a correctness break.
- **The Dead-Man's Switch:** the awakened node self-deletes via the metadata-SA-token Compute REST API if idle — decoupled, unbreakable, and **proven live (a node reaped itself at T+221s with zero orchestrator intervention).**

**Cost:** ~$1–3/month all-in vs. ~$100 always-on. The Mind is free insurance that costs pennies only during an actual outage.

---

## 5. The Cross-Repo Mutator + The Absolute Laws

The organism can author changes into its own Mind and Nerves — the highest-risk capability in the architecture. It is contained by a **Multi-Repo Blast Radius Quarantine**.

### 5.1 The Three Guardrails

- **G1 — Dynamic AST Blast-Radius:** before generating a cross-repo mutation, the Oracle traces *every* downstream dependent across all three repos and forces them into the generation prompt. The Mind cannot mutate the Nerves blind to what breaks in the Body.
- **G2 — The Air-Gapped Trinity Sandbox:** the candidate is validated by spinning all three repos in an ephemeral Docker network with `internal: true` — a **cryptographic sinkhole**. Provider URLs route to a synthetic mock; live DoubleWord/Claude/GCP calls are *physically impossible*. A broken cross-repo handshake → `[SOVEREIGN YIELD: CROSS-REPO FRACTURE]` → the saga rolls back all three repos. **Proven live: the air-gap is so real it blocks PyPI; the rollback restored both repos on a 32GB GCP node.**
- **G3 — The Adaptive Trust Ledger + CRITICAL_ELEVATION:** graduation from the operator hard-halt is *earned*, not counted — a consecutive zero-rollback streak whose AST-complexity-weighted trust clears a dynamic threshold; any rollback or FRACTURE resets it to zero. No magic number.

### 5.2 ⚖️ THE IMMUTABLE ORANGE PROTOCOL (the Sovereign Law)

> **A mutation targeting the Mind (`jarvis-prime`) or the Nerves (`reactor-core`) can NEVER reach auto-merge. By any flag. By any graduation. By any trust level. Forever.**

This is the one intentional hardcode in the entire system — a `frozenset({"prime","reactor"})` floor, evaluated *first* (before any flag or ledger that could throw and skip it), read by *no* environment variable, un-disableable by construction. The organism can autonomously *write, test, sandbox-validate, and package* a PR against its own Mind — but the **merge is permanently human.**

*Proven under adversarial test: across every combination of flags, env overrides, graduation states, and forced exceptions, a prime/reactor target resolves to `approval_required` every single time.*

**It can write its own brain. It can never merge its own brain.**

### 5.3 The Cloud Operating Room (IaC Hypervisor)

The organism can project the entire Trinity into GCP — provision a 32GB node, beam all three repos in, run the surgery air-gapped, stream it to the operator, and **self-burn** (a quadruple dead-man: local delete + remote SA-token self-delete + Spot DELETE + max-run-duration). A full live cross-repo surgery cost **~$0.30 with zero orphaned instances.**

---

## 6. The Governing Principles (non-negotiable)

Every autonomous capability in this architecture obeys these laws:

1. **Default-OFF.** Every power ships gated; the safe state is inert and byte-identical to today. The operator arms; the system never assumes.
2. **Fail-CLOSED, not just fail-soft.** On any uncertainty or error, the system degrades to *more* restrictive — never less-gated. A cross-repo mutation can never become less-contained through a failure.
3. **Cost-bounded by construction.** Ephemeral, Spot-first, dead-man-burned, budget-capped. No persistent expensive compute; no orphaned instances; ever.
4. **Observed-gated authority.** Forecasts pace; reality decides. No autonomous action commits on a prediction alone.
5. **Op-never-lost.** Every failure path ends sealed, terminal, or retried — never silently dropped.
6. **No hardcoding — one exception by design.** Everything is env-driven, *except* the Immutable Orange floor: the single safety constant that MUST NOT be disableable.
7. **Reuse-first, evidence-gated.** Build on what exists; graduate on proof, not hope.

---

## 7. Map of the Sovereign Subsystems

| Subsystem | Module(s) | Law it enforces |
|-----------|-----------|-----------------|
| Provider Quarantine | `provider_quarantine.py` | Gradient-deduced outage → Cryo-DLQ (no immortal spin) |
| Failover Lifecycle | `failover_lifecycle.py`, `recovery_forecaster.py`, `failover_deadman.py` | Sovereign generation when DW collapses, cost-bounded |
| RSI Flywheel | `dpo_synthesizer.py` + reactor-core training | Self-improvement from epistemic-purity-gated experience |
| Cross-Repo Quarantine | `cross_repo_blast_context.py`, `trinity_integration_gate.py`, `critical_elevation.py`, `cross_repo_trust_ledger.py` | Blast-radius context · air-gapped sandbox · Immutable Orange |
| IaC Hypervisor | `scripts/sovereign_iac_hypervisor.py` | Cloud operating room with quadruple dead-man |

> **For the operator's command surface, the canonical CLI conventions, and the full subsystem catalog, see `CLAUDE.md`. For the product roadmap, see `docs/architecture/OUROBOROS_VENOM_PRD.md`. This Manifesto is the architectural *law*; those are the *practice*.**

---

*The Sovereign Architecture is complete. The organism heals itself, improves itself, and authors itself across its entire Trinity — and the one move that could let it rewrite its own mind unwatched is the one thing made impossible by construction.*
