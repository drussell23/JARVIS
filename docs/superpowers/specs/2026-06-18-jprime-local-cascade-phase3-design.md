# J-Prime Local Activation & Cooperative Multi-Provider Cascade — Phase 3 ADD

> **Architecture Design Document.** Phase 3 (Local Plumbing & Lifecycle Armor) of the
> three-phase J-Prime Activation matrix. Phases 1 (Topological Complexity Gate) and 2
> (FSM Utility-Maximization Upgrade) are **deferred to follow-on ADDs** per the approved
> Phase-3-first sequencing.

- **Status:** Approved scope — pending spec review before implementation plan
- **Date:** 2026-06-18
- **Author:** Derek J. Russell (with Claude Opus 4.8)
- **Branch:** `feat/jprime-local-cascade-phase3`
- **Master kill-switch:** `JARVIS_LOCAL_PRIME_ENABLED` (default `false` — OFF is byte-identical to today)

---

## 1. Problem

Every recent battle-test soak died the same way: `terminal_quota` (DoubleWord out of
quota) **and** `credit balance too low` (Claude out of credits) → `all_providers_exhausted`
→ the Ouroboros loop hard-stops. With both remote tiers down there is **no third leg**, so
the organism cannot generate, cannot reach L2 convergence, and learns nothing (the
Pre-Flight Critic accumulated 0 samples across a full 20-minute soak for exactly this
reason).

The architecture already *names* a third tier — **J-Prime (Tier 2, "GCP self-hosted, when
available")** — and the provider chain already contains the machinery to use it. It simply
has **no model behind the endpoint**. This ADD activates that tier as a **native local
inference engine on the M1 host**, giving O+V a free, no-quota, always-available fallback.

### Goal

A **resource-aware local provider** that:
1. Catches the `all_providers_exhausted` fall — the loop never hard-stops on remote
   quota/credit exhaustion again.
2. Costs **$0 per call** (no quota, no credits) and runs entirely on the M1.
3. Governs its **own memory lifecycle** so it can never OOM-crash the host or starve the
   running O+V stack.

### Non-Goals (stated plainly to prevent scope drift)

- **NOT quality parity with DW 397B.** A quantized 3B is *availability armor + a
  trivial-op handler + a DW-output repair surface*, not a replacement code generator for
  hard, multi-file work. The cascade keeps DW/Claude as the primary brains for complex ops.
- **NOT a new prompt/compaction subsystem.** We reuse `context_compaction.py` (Gap #8).
- **NOT Phases 1 & 2** (topological pre-routing gate; FSM utility-max upgrade) — follow-on.

---

## 2. Existing Infrastructure We Reuse (Anti-Duplication Ledger)

The recurring lesson of this codebase: the substrate is usually already built. Phase 3 adds
the *minimum new code* and wires it into proven seams.

| Existing component | File | What we reuse |
|---|---|---|
| `PrimeProvider` | `providers.py:5063` | The CandidateProvider adapter (`provider_name="gcp-jprime"`, `generate(context, deadline, repair_context)`) — unchanged. We inject a **local** client into it. |
| `schema_capability` → `full_content` auto-disable | `providers.py:5160-5175` | Already forces `full_content` (not 2b.1-diff) for `<=14B` models. The 3B's "can't emit verbatim diffs" limitation is **already handled** — no new prompt splitter. |
| `FailbackStateMachine` | `candidate_generator.py:1-130` | `PRIMARY_READY -> PRIMARY_DEGRADED -> QUEUE_ONLY`, hysteresis, `required_probes` failback, **explicit `terminal_quota` cascade**. Docstring already says *"fallback provider (local model)"*. We feed it a real local provider + a latency-breaker signal. |
| `MemoryPressureGate` | `memory_pressure_gate.py:415` | `probe()`, `pressure()`, `level_for_free_pct()`, `PressureLevel{OK,WARN,HIGH,CRITICAL}`. Drives the eviction valve. |
| Live context auto-compaction | `context_compaction.py` (Gap #8) | Handles the 3B's small context window. No new distiller. |
| `PrimeProviderState` singleton hoist | `_governance_state.py` | The state root `PrimeProvider` already routes through; the local client lands on it with no migration. |

**New code is confined to:** one new module (`local_inference_director.py`) + a
`LocalPrimeClient` adapter + a thin latency-breaker hook + env wiring. No FSM rewrite.

---

## 3. Architecture Overview

```
                          incoming GENERATE / L2 repair op
                                       |
                                       v
                        +------------------------------+
                        |   FailbackStateMachine       |   (existing, candidate_generator.py)
                        |   primary: DW / Claude        |
                        +------------------------------+
                          | terminal_quota / schema_break / exhaustion
                          v   (cascade down — context passed natively)
                        +------------------------------+
                        |   PrimeProvider (gcp-jprime) |   (existing, providers.py)
                        |   schema_cap -> full_content  |
                        +---------------+--------------+
                                        | injected client
                                        v
                  +-----------------------------------------------+
                  |          LocalInferenceDirector (NEW)         |
                  |  - LocalPrimeClient (aiohttp pooled, :11434)  |
                  |  - warm-standby keep-alive                    |
                  |  - latency circuit breaker -> PRIMARY_DEGRADED|
                  |  - MemoryPressureGate eviction valve          |
                  +-----------------------+-----------------------+
                                          | HTTP (OpenAI-compat /v1/chat/completions)
                                          v
                  +-----------------------------------------------+
                  |   Native Ollama service (host, Metal GPU)     |
                  |   model: qwen2.5-coder:3b (q4)                |
                  +-----------------------------------------------+
```

**Why native (not Docker):** Docker Desktop on Apple Silicon runs Linux containers in a VM
with **no Metal GPU passthrough** — Ollama-in-Docker would run CPU-only (many times slower),
destroying the sub-second warm-standby goal. Ollama therefore runs **natively** on the M1
(brew/launchd-managed for restart-on-crash "permanence"). A containerized O+V stack, if ever
used, reaches it via `host.docker.internal:11434`. Docker-for-the-engine is only valid on a
Linux box with a real GPU.

---

## 4. Components

### 4.1 `LocalPrimeClient` — Asynchronous Connection-Pooled Abstraction

**File:** `backend/core/ouroboros/governance/local_inference_director.py` (new)

A `PrimeClient`-compatible client (`async generate(...)`) that talks to Ollama's
OpenAI-compatible endpoint, so the **existing `PrimeProvider` consumes it unchanged**.

- **Persistent pooled session:** one module-singleton `aiohttp.ClientSession` with a bounded
  `TCPConnector` (`limit`, `limit_per_host`, `keepalive_timeout`), targeting
  `http://127.0.0.1:11434` (env `JARVIS_LOCAL_MODEL_BASE_URL`). Connection-keep-alive headers
  eliminate per-call socket-setup latency across multi-iteration L2 repair passes. The session
  is created lazily and closed on `stop()`/kill-switch with **zero hanging file descriptors**
  (verified by test).
- **Warm-standby keep-alive:** every request sends Ollama's `keep_alive` field set from
  `JARVIS_LOCAL_MODEL_KEEP_ALIVE_SECONDS` (default e.g. `300`). Weights stay resident in M1
  unified memory during active development/repair bursts (sub-second subsequent latencies) and
  drop automatically when the loop goes idle.
- **Structured Prompt Discipline for the local 3B** (the correctly-targeted version of
  parameter #4): all injected context, constraints, and L2 instructions are rendered inside
  **rigid bounded XML/Markdown tags** (e.g. `<task>`, `<constraints>`, `<files>`,
  `<output_format>`), never loose natural-language steering. Small models align far more
  predictably to delimited structure. This reuses the existing prompt-assembly helpers and the
  `schema_capability -> full_content` path; it does **not** introduce a second prompt builder.

### 4.2 Deterministic Latency Circuit-Breaker

- A watchdog inside the local generate path measures **time-to-first-token** and total
  generation time against `JARVIS_LOCAL_INFERENCE_TIMEOUT_MS`. On breach it **trips a localized
  breaker** and surfaces a typed degradation signal.
- The signal transitions J-Prime to **`PRIMARY_DEGRADED`** in the *existing*
  `FailbackStateMachine`, which **cascades the active operation context upstream** to the
  remote tiers (or fails soft) **without tearing down the L2 repair sandbox session** — the
  FSM already passes context on cascade; we only feed it the local-latency input.
- Breaker recovery uses the FSM's existing `required_probes` hysteresis (no new recovery loop).

### 4.3 Ironclad Synchronous Memory-Eviction Valve

Wired to live `MemoryPressureGate.probe()` telemetry. Deterministic, first-match policy:

| PressureLevel | Action |
|---|---|
| `OK` / `WARN` | Normal operation. |
| `HIGH` | Restrict local inference concurrency to **a single in-flight request** (semaphore = 1). No new local generations admitted until pressure drops. |
| `CRITICAL` | **Un-bypassable atomic teardown:** (1) dispatch the eviction call to Ollama (`keep_alive: 0`, forcing immediate model unload from unified memory); (2) run a **dual-stage `gc.collect()`** sweep (two passes to reclaim cyclic references); (3) inject an execution **yield to the host** (`await asyncio.sleep(0)`) so macOS reclaims RAM before thrashing. Local generation is refused until pressure recovers; the op cascades up. |

The `gc.collect()` passes are synchronous and bounded; they run on the eviction path only
(never the hot generation path). This is the honest, buildable form of "adaptive cache
contraction" — we control the engine's residency via its API + Python GC, we do **not** write
Metal/unified-memory code (Ollama owns Metal natively).

### 4.4 Model & Memory Budget (16GB M1 reality)

- **Model:** `qwen2.5-coder:3b` quantized q4 (~2.5GB resident).
- The O+V stack already runs at HIGH memory pressure (~16-19% free in soaks). A 3B q4 fits
  alongside it; the eviction valve guarantees it yields RAM before the host thrashes.
- A 7B (~4.5GB) was rejected for this host (too tight under HIGH/CRITICAL); a 1.5B is the
  documented fallback model if 3B proves unstable (env-swappable, no code change).

---

## 5. Operator's Embedded Engineering Parameters — Disposition

| # | Requested | Disposition |
|---|---|---|
| 1 | aiohttp pooled session, port 11434, aggressive keep-alive headers | **Embedded** (§4.1). |
| 2 | Latency circuit-breaker -> `PRIMARY_DEGRADED`, cascade context upstream | **Embedded** (§4.2), reusing the existing FSM. |
| 3 | CRITICAL -> `keep_alive:0` eviction + dual-stage `gc.collect()` + host yield | **Embedded** (§4.3). |
| 4 | "Fable 5 prompt alignment" via rigid XML/Markdown tags | **Embedded with honest correction** (§4.1): the local engine is **Qwen-3B, not Fable 5**; the *structured-prompt-discipline principle* is correct and is applied to the 3B. |

**Out of scope / cut (approved):** runtime adaptive quantization (not a real live operation —
q4 is baked at pull time); a new async token-distillation/context governor (reuse
`context_compaction.py`); live 3B<->1.5B mid-op hot-swap handshake (deferred — on CRITICAL we
evict + cascade up, which the FSM already does cleanly).

---

## 6. Configuration (all env-driven, no hardcoding)

| Env flag | Default | Purpose |
|---|---|---|
| `JARVIS_LOCAL_PRIME_ENABLED` | `false` | **Master kill-switch.** OFF = byte-identical legacy (PrimeProvider receives no local client; chain behaves exactly as today). |
| `JARVIS_LOCAL_MODEL_BASE_URL` | `http://127.0.0.1:11434` | Native Ollama endpoint. |
| `JARVIS_LOCAL_MODEL_NAME` | `qwen2.5-coder:3b` | Model tag (swap to `:1.5b` with no code change). |
| `JARVIS_LOCAL_MODEL_KEEP_ALIVE_SECONDS` | `300` | Warm-standby residency window. |
| `JARVIS_LOCAL_INFERENCE_TIMEOUT_MS` | env-tuned | Latency breaker threshold (time-to-first-token + total). |
| `JARVIS_LOCAL_MODEL_MAX_CONCURRENCY` | `2` | Normal concurrency; forced to `1` at HIGH, `0` at CRITICAL. |
| `JARVIS_LOCAL_POOL_LIMIT` | `8` | aiohttp connector pool size. |

---

## 7. Failure Modes & Fallback Semantics

| Failure | Behavior |
|---|---|
| Kill-switch OFF | No local client injected; chain is exactly today's. |
| Ollama not running / unreachable | Client construction/health-check fails soft; PrimeProvider behaves as "primary unavailable"; FSM never routes to it. **No hard error.** |
| Local latency spike | Breaker -> `PRIMARY_DEGRADED` -> cascade up; no state disruption. |
| CRITICAL memory | Eviction valve unloads model + GC + yield; op cascades up; host protected. |
| Local malformed output | Reuses existing schema validation; failure cascades up like any provider's. |
| Process kill mid-generation | Pooled session closed on teardown; **zero hanging FDs** (test-asserted). |

**Invariant:** the local provider can only ever *add* a fallback path. It cannot block, slow,
or alter the existing remote path when healthy, and OFF is a perfect no-op.

---

## 8. Testing & Verification Harness

- **Unit (mocked Ollama):** pooled-session lifecycle (create/reuse/close, no FD leak);
  keep-alive field propagation; structured-prompt rendering; latency breaker trips at threshold
  and sets `PRIMARY_DEGRADED`; eviction valve fires `keep_alive:0` + double `gc.collect()` +
  yield at CRITICAL; concurrency clamps at HIGH.
- **Integration (FSM):** DW `terminal_quota` -> cascade to local -> local generates ->
  result returns; local latency spike -> cascade back up -> remote handles.
- **Kill-switch parity:** with `JARVIS_LOCAL_PRIME_ENABLED=false`, the provider chain is
  byte-identical to baseline (golden-path assertion) and **no Ollama call is ever made**.
- **Live smoke (manual, optional):** native Ollama + `qwen2.5-coder:3b` pulled; a trivial op
  generates locally end-to-end at sub-second warm latency.

---

## 9. Operational Setup (native, M1)

```bash
brew install ollama            # or the official installer
brew services start ollama     # launchd-managed: auto-start + restart-on-crash ("permanence")
ollama pull qwen2.5-coder:3b   # ~2.5GB q4
# then: export JARVIS_LOCAL_PRIME_ENABLED=true  (host-local .env, never committed)
```

`brew services` (launchd) gives the POSIX-managed lifecycle that replaces the Docker-for-engine
mistake: native Metal access + auto-restart.

---

## 10. Rollout & Sequencing

1. **Phase 3 (this ADD):** local plumbing + breaker + memory valve, gated OFF by default.
   Provable in isolation: flip the flag, kill remote quota, watch the loop survive on local.
2. **Phase 1 (follow-on ADD):** topological complexity pre-routing gate (Oracle
   `SqliteLazyGraphBackend` blast-radius + token density) — route *trivial* ops to local to
   *preserve* remote quota proactively, not just on exhaustion.
3. **Phase 2 (follow-on ADD):** upgrade `FailbackStateMachine` into a multi-variable
   utility-maximization matrix (latency + billing flags + memory profile).

---

## 11. Risks & Open Questions

- **3B code quality is low.** Mitigation: it is fallback/trivial-op armor by design; complex
  ops still cascade to DW/Claude. Acceptable for the availability goal.
- **Memory coexistence on 16GB is genuinely tight.** Mitigation: the eviction valve + the 1.5B
  env-swap escape hatch. If the valve fires too often in practice, the honest answer is a
  separate box, not a bigger M1 model.
- **`gc.collect()` cost.** Bounded, eviction-path-only, off the hot path.
- **Open:** exact `JARVIS_LOCAL_INFERENCE_TIMEOUT_MS` value — to be tuned from the first live
  smoke run, not guessed.

---

## 12. Definition of Done (Phase 3)

- `local_inference_director.py` + `LocalPrimeClient` implemented; `PrimeProvider` wired to
  accept it under the kill-switch.
- Latency breaker integrated into `FailbackStateMachine`; eviction valve integrated with
  `MemoryPressureGate`.
- Full test suite green incl. kill-switch byte-identical parity + zero-FD teardown.
- OFF by default; documented operational setup; no hardcoded model names or endpoints.
