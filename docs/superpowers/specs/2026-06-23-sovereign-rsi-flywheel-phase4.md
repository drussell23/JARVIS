# Sovereign RSI Flywheel — Phase 4 Design Spec (costed)

> **Arc.** Close the Recursive Self-Improvement loop: O+V's own generation experience → fine-tune the self-hosted `Qwen2.5-Coder-7B` (the J-Prime failover Mind) → redeploy → a more capable last-resort generator. Built on the Trinity: Body (JARVIS/O+V, this repo) emits experience → Nerves (Reactor-Core, `~/Documents/repos/reactor-core`) trains → Mind (J-Prime, `~/Documents/repos/jarvis-prime`) serves the improved model.
> **Date:** 2026-06-23. Branch `worktree-sovereign-rsi-flywheel`.
> **PRIMARY CONSTRAINT (non-negotiable, operator-emphasized): the GCP bill must stay EXTREMELY LOW.** Every design choice below is subordinate to this. The cost model (§5) is the load-bearing section; if a feature can't be made near-free or hard-capped, it is cut or deferred.

---

## 0. Reality grounding (from the reactor-core deep dive)
Reactor-Core is **~85% ready** — real, production-grade: `RawInteraction` ingestion + `is_trainable()`; SFT (HF `SFTTrainer`); a complete **DPO pair generator** (`training/dpo_pair_generator.py`); LoRA/QLoRA; **GGUF quantization** (Q4_K_M); a **DeploymentGate** (40+ tests); deploy-to-J-Prime (copy GGUF + `current.gguf` symlink + `model_ready` Trinity event). The `telemetry_ingestor` already tags DW→Claude escalations as DPO signal.
**The three real gaps Phase 4 closes:** (4a) the Body emits no *code-generation-outcome* experience yet — only generic telemetry; (4b) reactor-core has **NO GPU-VM provisioning/lifecycle** — it assumes a node exists, so a training run left bare would bill an unmanaged GPU (the cost red-flag); (4c) the J-Prime hot-swap listener is unverified.
**Reuse-first:** the epistemic-repair trajectory + `ast_symbol_scoper.isolate_symbols` + the egress interceptor's `estimate_body_chars`/compression (4a); `failover_deadman.py` + `gcp_vm_manager` Spot/STANDARD + the metadata-SA-token self-delete + `IntelligentGCPOptimizer` budget (4b); reactor-core's `_deploy_to_jprime` + Ollama's native model API (4c). NO duplication of training/quant/deploy — those are Reactor's.

## 1. Goals / Non-Goals
**Goals.** (G1) Collect O+V generation experience as **token-dense DPO pairs** at **$0** (4a). (G2) Run fine-tunes on an **ephemeral, Spot, self-deleting** GPU node — never an orphaned A100 (4b). (G3) **Zero-downtime** model reload into J-Prime (4c). (G4) **Hard, autonomous cost guardrails**: data-volume + demand trigger, Spot-first minimum-viable GPU, dead-man self-delete, monthly budget ceiling. (G5) Reuse-first, gated, fail-soft, OFF byte-identical, no hardcoding.
**Non-Goals.** Not continuous/online training (cost). Not training a model bigger than 7B (cost + the failover host is CPU-class). Not rebuilding Reactor's trainer/quantizer/deployer. Not RLHF/world-models (Reactor's aspirational tier). The fine-tune targets the *failover* generator only; DW/Claude remain primary.

## 2. The flywheel (data-flow)
```
EVERY O+V generation (DW primary OR J-Prime failover)
   │  has an outcome: passed VALIDATE/Iron-Gate/VERIFY  OR  failed + epistemic-repair trajectory
   ▼
[4a] DPO Pair Synthesizer (Body, $0)  ── failed AST node = REJECTED, repaired AST node = CHOSEN,
   │                                      imports/boilerplate stripped, token-dense
   ▼  async fire-and-forget (the Phase-1 Trinity bridge) → GCS dataset (.jsonl, pennies)
   ▼
[4b] Ephemeral GPU Hypervisor (cost-gated)  ── trigger: enough NEW pairs AND model-underperforming
   │     → bid minimum-viable Spot GPU (L4 first) → reactor-core SFT/DPO → GGUF Q4_K_M
   │     → push weights to GCS → SELF-DELETE (dead-man) the instant quant completes
   ▼
[4c] Zero-Downtime Neural Reload (J-Prime)  ── Ollama hot-swap: load new, drain old, no dropped DAG req
   ▼  improved Qwen2.5-Coder-7B serving on the next failover
```
The flywheel spins on **normal DW operation** (every op yields a pair), but the GPU **only spins on demand** — see §5.

---

## 3. Phase 4a — Autonomous DPO Pair Synthesizer (Body, $0, build FIRST)
`backend/core/ouroboros/governance/dpo_synthesizer.py` (pure, fail-soft).
- **Source = the epistemic-repair trajectory** (we already generate it): a `(failed_candidate, epistemic_diff, repaired_candidate, test_outcome)` sequence. The synthesizer turns each *failed→passing* transition into a DPO pair. Also consumes DW→Claude escalations (Reactor already tags these).
- **AST-node isolation (the operator constraint — no raw payload dumps):** use `ast_symbol_scoper.isolate_symbols` to extract ONLY the symbol(s) that changed between rejected and chosen. `REJECTED` = the failed symbol body; `CHOSEN` = the repaired symbol body. Strip irrelevant global imports/boilerplate/unrelated symbols via an AST keep-list (the symbol + its directly-referenced names), reusing the egress interceptor's `estimate_body_chars` to keep each pair under a token-density cap (`JARVIS_DPO_PAIR_MAX_CHARS`, default ~2KB). A pair that can't be isolated cleanly is dropped (quality over quantity), logged — never a raw dump.
- **`DPOPair` shape** matches reactor-core's `dpo_pair_generator` export (`prompt, chosen, rejected, metadata{signature, task_type, source}`) so Reactor ingests it natively — NO new schema on the Nerves side.
- **Export:** async fire-and-forget on the existing Phase-1 Trinity bridge pattern (`emit_*` → `asyncio.create_task`, strong-ref, swallow-all) → append to a local bounded JSONL ring AND (gated) a GCS object. Body's live path NEVER blocks on it. Gated `JARVIS_DPO_SYNTHESIS_ENABLED` (default true; OFF → no-op).
- **Dedup + density:** content-hash dedup (a recurring same fix isn't re-emitted); only `is_trainable`-quality pairs (real test-verified chosen). Bounded ring (`JARVIS_DPO_DATASET_MAX`, default 5000).
- **Cost: $0** (in-process) + GCS storage: 5000 pairs × ~2KB ≈ 10MB ≈ **<$0.01/mo**.

## 4. Phase 4b — Ephemeral GPU Hypervisor + Dead-Man's Switch (cost-gated; the bill guardrail)
`backend/core/ouroboros/governance/rsi_training_hypervisor.py` — the cost-control wrapper Reactor lacks. Reuses the failover node's EXACT discipline.
- **Demand-aware trigger (no wasteful runs):** train ONLY when BOTH (a) `new_pairs_since_last_train >= JARVIS_RSI_MIN_NEW_PAIRS` (default 2000 — amortize the GPU cost over enough data) AND (b) a **demand signal**: recent J-Prime failover generations underperformed (failure-rate over a window) OR `JARVIS_RSI_FORCE_TRAIN`. **Rationale: J-Prime is rarely active (failover-only), so a fine-tune that isn't needed is pure cost — don't train an idle, adequate model.** This is the single biggest cost lever.
- **Minimum-viable Spot GPU bid:** a dynamic provisioner sizes the GPU to the dataset — `L4` (g2-standard-4, ~$0.20-0.30/hr Spot) for the default 7B-QLoRA on ≤ a few-thousand pairs; escalate to `A100` only if dataset/seq-len demands it (env thresholds, NOT hardcoded). **Spot-first** (`provisioning_model=SPOT`, `instance_termination_action=DELETE`) — reuse `gcp_vm_manager`'s Spot path. On-demand fallback only if Spot capacity is unavailable AND the budget allows.
- **Aggressive Dead-Man's Switch (the no-orphaned-A100 guarantee):** the node's startup-script (reuse `failover_deadman.build_deadman_startup_script` pattern) runs the reactor-core training as its workload, then **the instant GGUF quantization completes + weights are pushed to GCS, the node executes a metadata-SA-token Compute REST `DELETE` on itself** — no idle GPU-seconds. PLUS an idle-timeout dead-man (training wedged/crashed → self-delete after `JARVIS_RSI_NODE_IDLE_TIMEOUT_S`) AND a hard `max_run_duration` cap (training overruns → GCP auto-deletes). Three independent teardowns, exactly like the failover node.
- **Budget ceiling (hard):** reuse `IntelligentGCPOptimizer` `CostBudget` — a dedicated `JARVIS_RSI_MONTHLY_BUDGET_USD` (default **5.0** — i.e. ≤ $5/mo for ALL training) that BLOCKS a training launch if the month's RSI spend would exceed it. A launch is refused (logged, deferred) when the budget is exhausted — never silently overrun.
- **Observed-gated, fail-soft:** a provision/train/push failure → self-delete + retry-next-trigger; the existing J-Prime model stays in place (no broken deploy). Gated `JARVIS_RSI_TRAINING_ENABLED` (default **FALSE** — the GPU tier stays OFF until the operator flips it after reviewing accumulated data + this cost model). OFF → no GPU ever provisions.

## 5. COST MODEL (the load-bearing section)
| Component | Billing | Frequency | ~Cost |
|---|---|---|---|
| 4a data collection | in-process, $0 | every op | **$0** |
| 4a dataset storage (GCS) | ~10MB | continuous | **<$0.01/mo** |
| 4b GPU training burst (L4 Spot, 7B QLoRA, ~2k pairs) | ~$0.25/hr × 1-3hr | **demand-gated** | **~$0.30-0.90/run** |
| 4b (A100 Spot, only if escalated) | ~$1.20/hr × 2hr | rare | ~$2.40/run |
| 4b weights→GCS + model storage | ~2GB GGUF | per run | **~$0.05/mo** |
| 4c hot-swap | runs on existing J-Prime node | per deploy | **$0** |
| **Hard ceiling** | `JARVIS_RSI_MONTHLY_BUDGET_USD` | — | **≤ $5/mo, enforced** |

**Realistic steady-state: ~$0-2/mo.** Because (1) data collection is free; (2) the GPU is demand-gated — J-Prime is failover-only, so training is *infrequent* (maybe 0-2 runs/month, often zero if no failovers exercised the model); (3) L4-first + Spot + dead-man self-delete means each run is sub-dollar and leaves zero idle GPU; (4) the $5/mo hard cap is a backstop, not the expected spend. **Worst case is bounded at $5/mo by construction; expected is near-zero.** No persistent GPU, ever.

**Cost kill-switches (all default-safe):** `JARVIS_RSI_TRAINING_ENABLED=false` (GPU tier fully off — the default) · `JARVIS_RSI_MONTHLY_BUDGET_USD` (hard cap) · `JARVIS_RSI_MIN_NEW_PAIRS` (amortization floor) · demand-gate (don't train an adequate model) · Spot-first + dead-man + max_run_duration (no orphan).

## 6. Phase 4c — Zero-Downtime Neural Reload (J-Prime)
Triggered by reactor-core's existing `model_ready` Trinity event after deploy.
- **Ollama-native hot-swap (no hard reboot):** load the new GGUF as a new model tag, warm it (reuse the `LocalPrimeClient.warmup` 1-token gen — Phase-3 primitive), then atomically flip the `current` pointer the `LocalPrimeClient` resolves; the old model is unloaded from memory only AFTER in-flight requests drain. Incoming O+V DAG requests are served by old-or-new throughout — never dropped/erroring.
- **Drain-before-unload + rollback:** a request-counter gate (no unload while requests in flight); if the new model fails its warmup/DeploymentGate inference check, **keep the old model** (no flip) and log — a bad fine-tune never degrades the failover. Reuse the existing DeploymentGate verdict.
- **Spans the jarvis-prime repo** (the listener) — a cross-repo change; this spec defines the Body/Reactor contract + the J-Prime-side requirement.

## 7. Cross-cutting / Invariants
- **Bill-low-by-construction:** GPU tier default-OFF; demand+volume gated; Spot+dead-man+budget-cap; data/storage near-free. The expected bill is ~$0; the enforced ceiling is $5/mo.
- **Fail-soft + OFF byte-identical:** every phase gated; OFF = today's behavior (no synthesis, no GPU, no hot-swap). Any error → the existing model/path stays; the live O+V DAG is never blocked or degraded.
- **Reuse-first:** epistemic trajectory + isolate_symbols + egress compression (4a); failover_deadman + gcp_vm_manager + SA-self-delete + IntelligentGCPOptimizer (4b); reactor-core trainer/quant/deploy + Ollama API + warmup + DeploymentGate (4c). No duplication.
- **No hardcoding:** GPU tier, budget, triggers, density caps all env. **Observed-gated:** train on demand+evidence, deploy on DeploymentGate-pass, never on a forecast alone.
- **3-repo span (honest):** 4a = JARVIS (this repo); 4b = JARVIS provisioner wrapping reactor-core's training workload; 4c = jarvis-prime listener + the reactor-core deploy trigger. Build order isolates risk: 4a first ($0, self-contained here).

## 8. Phasing / build order
1. **4a (build FIRST, $0):** `dpo_synthesizer.py` + the bridge export + tests. Starts accumulating the dataset immediately from normal DW operation. NO GPU.
2. **4b (build after 4a has data; GPU tier stays default-OFF):** `rsi_training_hypervisor.py` (demand+volume trigger, Spot-L4 bid, dead-man self-delete, budget cap) + the reactor-core training-workload wiring + tests. Operator flips `JARVIS_RSI_TRAINING_ENABLED` only after reviewing accumulated data + a dry-run cost estimate.
3. **4c (build with/after 4b):** the J-Prime Ollama hot-swap listener (jarvis-prime repo) + drain/rollback + the Body-side reload trigger.
4. **First real fine-tune burst:** operator-gated, budget-capped, on accumulated data — measured against the gauntlet (does the fine-tuned 7B thrash less on TTL-LRU/Paxos?).

## 9. Tests (per phase)
- **4a:** AST isolation maps failed-symbol→rejected + repaired-symbol→chosen; imports/boilerplate stripped; density cap enforced; pair shape matches reactor's DPO schema; dedup; fire-and-forget non-blocking + fail-soft; OFF → no-op; $0 (no network in the hot path).
- **4b:** demand+volume trigger fires only when BOTH conditions met (not on data alone, not on an adequate model); L4 selected by default + A100 only above threshold; Spot-first + DELETE-on-preempt; dead-man self-delete script issues the SA-token REST DELETE on GGUF-complete; budget-cap BLOCKS launch when exhausted; max_run_duration set; OFF → no provision ever; fail-soft (provision/train fail → self-delete, existing model intact).
- **4c:** hot-swap loads new + drains old with zero dropped requests (concurrent-request test); failed-warmup/gate → keep old (rollback); OFF → no swap.
- **Cost guardrail (load-bearing):** a static test asserting the GPU tier cannot provision when `JARVIS_RSI_TRAINING_ENABLED=false`, cannot exceed the monthly budget, and every provision path sets Spot+DELETE+dead-man+max_run_duration (no un-capped launch reachable).

## 10. Open decisions (for operator review of the cost model)
1. **Monthly budget ceiling:** `JARVIS_RSI_MONTHLY_BUDGET_USD` default **5.0**. Lower? (Spec assumes $5/mo hard cap; expected spend ~$0-2.)
2. **GPU tier default:** L4-first (cheapest viable) vs always-A100 (faster, ~4-8× cost). *Spec assumes L4-first, escalate-by-dataset.*
3. **Train cadence trigger:** demand-gated (only when the failover model underperforms) vs pure-volume (every N pairs). *Spec assumes demand-AND-volume (cheapest — don't train an idle adequate model).*
4. **GPU tier activation:** keep `JARVIS_RSI_TRAINING_ENABLED` default-OFF (operator flips after reviewing data) — recommended given the bill sensitivity. *Spec assumes default-OFF.*
