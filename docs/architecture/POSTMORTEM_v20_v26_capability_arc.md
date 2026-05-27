# Postmortem — v20 → v26 capability arc + Slice 33 blueprint

**Date:** 2026-05-27
**Author:** Derek J. Russell + O+V autonomous engineering
**Status:** v26 telemetry frozen; Slice 33 blueprint pending operator authorization
**Scope:** Hardware envelope — 16 GB M1, JARVIS-AI-Agent monolith (~29k Python files)

---

## TL;DR

Across 7 capability soaks (v20 → v26) and 16 architectural slices (Slice 19a → Slice 32) the cascade of upstream-coordination defects was peeled back one layer at a time. Each soak surfaced the next defect previously masked by the one above it. As of v26, **the bearer gate is structurally clean (Slice 31)** and **Oracle's parse + visitor walk no longer wedges the loop via GIL contention (Slice 32)** — but the **asyncio main thread is still being starved by a distinct downstream sink (graph writes + embedder + ToolLoop TTFT logic)** that pre-dates Slice 32 and was simply masked behind the older wedges.

**Capability bar (APPLY → VERIFY → RESOLVED): still unmet across all 7 soaks.** Methodology validated, capability not yet measured.

---

## The defect chain — fully traced as of v26

```
1. Aegis 401 missing_session_bearer            (Slice 31 — CLOSED, 2026-05-27 f1f62d89ab)
        │
        ▼
2. Oracle threadpool GIL contention             (Slice 32 — CLOSED, 2026-05-27 6274f76e37)
        │
        ▼
3. Residual asyncio main-loop starvation        (Slice 33 — pending)
   from on-loop work that is NOT the AST parse:
     a. NetworkX add_node / add_edge writes
        (~12k Oracle index calls in 32 min × N
         nodes/edges per file)
     b. ChromaDB embedder / vector embedding
        (Qwen3-Embedding-8B, olmOCR — invoked on
         oracle.py executor path)
     c. Anything else running synchronously
        between yield points
        │
        ▼
4. ToolLoop TTFT projection inflation           (Slice 33 — pending)
   per-round wall-clock measurement counts loop-
   starvation time, so projected_per_round
   becomes 5-12 s when actual provider TTFT is
   sub-second
        │
        ▼
5. ToolLoop defensive bail                      (Slice 33 — pending)
   "tool_loop_starved_below_min_ttft_floor" —
   guard math correct, projection input wrong
        │
        ▼
6. Fallback EXHAUSTION                          (downstream of #5)
   cause=fallback_failed, fallback_err_class=
   RuntimeError / TimeoutError — the EXHAUSTION
   we saw labeled as "provider failures" in
   v25 and earlier were actually loop-health
   failures all along
        │
        ▼
7. Slice 21 supervisor containment              (handled — structural shutdown)
   PhaseResult fail / generation_exhausted_
   unrepairable — no raw RuntimeError, advances
   to POSTMORTEM cleanly
        │
        ▼
0. APPLY / VERIFY / RESOLVED never reached      (capability bar UNMET)
```

The numbering on the right side (1-7) is the defect chain in execution order. Items 1 and 2 are now closed; items 3-5 are Slice 33's target; items 6-7 are downstream consequences that will resolve automatically when the upstream is fixed.

---

## v25 vs v26 — empirical comparison

| Metric | v25 (Slice 31 only) | v26 (Slice 31 + 32) | Δ |
|---|---|---|---|
| Duration before kill | 51 min then SIGKILL | 33 min, clean SIGTERM → SIGKILL needed (wedged loop) | – |
| Cost spent | $0.24 | $0.20 | trivial |
| `missing_session_bearer` events | 0 | **0** | ✓ Slice 31 holds |
| Slice 32 process dispatches | n/a | **11,999** in 32 min | new capability |
| ControlPlaneStarvation events | 113 in 51 min (~2.2/min) | 81 in 33 min (~2.45/min) | slightly worse |
| Peak stall duration | 54.5 s | **56.0 s** | slightly worse |
| `tool_loop_starved` bails | implicit (logged as RuntimeError) | **7 explicit** | now visible |
| `oracle_slow_call` WARN events | n/a | **1** (34.4 s parent / 110 ms worker) | new observability |
| EXHAUSTION events | 4 | 4 | same |
| Slice 21 structured terminals | 2 | 2 | same |
| APPLY events | 0 | 0 | capability bar unmet |
| VERIFY events | 0 | 0 | capability bar unmet |
| RESOLVED ops | 0 | 0 | capability bar unmet |

**Verdict:** Slice 31 + 32 closed their named defects (verified by absence of `missing_session_bearer` + presence of 11,999 `execution_mode=process` dispatches). The capability bar did not move because the starvation defect is structural and orthogonal to both closed slices.

The most diagnostic v26 signal was the lone `oracle_slow_call` WARN at 15:27:35:

```
parent_await_ms=34429.6   ← Oracle's _index_file await
worker_elapsed_ms=109.6   ← actual work in the spawn pool
source_bytes=117688       ← 117 KB Python source
```

The **34 second gap between parent-await and worker time** is the entire defect in one log line. The worker did 110 ms of real work; the parent task was descheduled for 34.3 s waiting for the asyncio loop to give it CPU back. Something else was eating the loop — not the spawn worker, not the parse, not GIL contention. The next-layer sink is **on the main asyncio thread, between yield points, doing synchronous CPU-bound or blocking I/O work that pre-dates Slice 32**.

---

## SIGTERM evidence — self-diagnosing teardown

The v26 SIGTERM took >50 s to even reach the `atexit` partial-summary handler, and the async shutdown coroutine never ran to completion (the harness's structured `_generate_report` path was never reached; the loop was wedged). SIGKILL was required. This is *itself* a diagnostic: **the wedged loop literally could not process its own shutdown signal.** When the operator-bound "un-killable background asset" survives signal handling but cannot service its own coroutines, the fix is structural off-loading, not signal-handler tuning.

---

## What the closed slices DID accomplish

Two confidence-building wins worth naming despite the capability bar not moving:

1. **Slice 31 (Aegis session-bearer)** — the v24 401 wedge that killed every DW outbound call would have prevented v26 from ever showing this next layer. **Without Slice 31, we would have spent the entire v26 burn on the same 401 cascade.** It was a necessary precondition for the present diagnostic clarity.

2. **Slice 32 (Oracle process-pool isolation)** — the parse + visitor walk now genuinely runs in a spawn process. The 11,999 `execution_mode=process` dispatches are empirical proof. The `oracle_slow_call` observability surface added in Slice 32 was the single instrument that proved the residual starvation is NOT in the parse layer.

Neither slice was a euphoria target; both are methodology slices that unblocked diagnosis of the actual capability blocker. The capability artifact (APPLY/VERIFY/RESOLVED) remains the only bar that earns résumé moves.

---

## Slice 33 blueprint — Unified On-Loop Sink Elimination

Per operator scope:

> Asynchronous Graph-Write Offloading + Isolated Embedder Processing + Dynamic ToolLoop Adaptation

These are three orthogonal arcs that compose into one slice family. Each can be sequenced independently, but the arc order matters because each later arc benefits from upstream loop health improvements.

### Arc A — Graph-Write Offloading (priority 1)

**Target:** `Oracle._index_file` lines 2128-2130 (post-Slice-32):

```python
self._file_hashes[cache_key] = result.content_hash
for node_data in result.nodes:
    self._graph.add_node(node_data)     # <-- on loop, pure-Python NetworkX dict mutations
for source, target, edge_data in result.edges:
    self._graph.add_edge(source, target, edge_data)  # <-- on loop, same
```

At 11,999 dispatches × N nodes/edges per file (the v26 `oracle_slow_call` was for a 117 KB file — likely thousands of nodes), this is millions of `dict.__setitem__` operations on the asyncio thread. NetworkX's `DiGraph.add_node`/`add_edge` are pure-Python and serial.

**Approach (proposed, NOT authorized):**
- **Option A1 — Batched async queue.** Push `(nodes, edges)` tuples onto an `asyncio.Queue`. Consumer task batches N writes and calls a single `await asyncio.to_thread(_bulk_graph_apply, ...)` (releases GIL during the threaded mutation). Composes with existing `cooperative_yield_every_n_async` from `event_loop_governance`.
- **Option A2 — Process-pool graph proxy.** Move the entire NetworkX graph into a spawn worker; main process holds only a thin RPC proxy. Heavier refactor; harder to query synchronously.
- **Option A3 — Replace NetworkX with a thread-safe in-memory graph (e.g. immutable copy-on-write trie).** Largest refactor, also touches every consumer of `self._graph`.

**Recommendation:** A1. Smallest diff, composes existing substrates, addresses the dominant on-loop work without touching graph query semantics.

### Arc B — Embedder Off-loading (priority 2)

**Target:** Oracle's ChromaDB embedder path (`SEMANTIC_EMBED_MODEL=all-MiniLM-L6-v2` by default; operator's Qwen3-Embedding-8B / olmOCR mentioned as the upgrade target). Currently wrapped in `loop.run_in_executor` (default ThreadPoolExecutor) at oracle.py:1388.

**Approach (proposed, NOT authorized):**
- **Compose AstCompileHelper pool** (same pattern as Slice 32) — register a new worker fn `_worker_embed_in_process(texts: list[str]) -> list[list[float]]` and a public coro `embed_for_oracle(...)`. The worker imports the embedder model lazily on first call (warm-up cost paid once per spawn worker).
- **Caveat:** embedder model size (Qwen3-Embedding-8B is ~8 GB) on a 16 GB M1 means a spawn worker holding the model is significant RSS. Default `JARVIS_AST_HELPER_POOL_MAX_WORKERS=1` keeps total at ~8 GB worker + ~3 GB parent — within envelope. Operators with more RAM can raise workers.
- **AST cage**: extend Slice 11 pin to admit `_worker_embed_in_process` as a 4th allowed worker.

**Recommendation:** Implement only if Arc A doesn't move the starvation needle. If Arc A drops the starvation rate by >50%, Arc B may be unnecessary in this cycle.

### Arc C — Dynamic ToolLoop TTFT Adaptation (priority 3)

**Target:** `tool_executor.py` `tool_loop_starved_below_min_ttft_floor` guard. Currently uses a static `min_ttft_floor=45.0s`, multiplied by `projected_per_round` (rolling avg of recent rounds) against `rounds_left × remaining_s`.

**Defect:** `projected_per_round` measures wall-clock per round, which includes any loop-starvation time. When the loop is healthy, projected drops to <1 s. When starved, it inflates to 5-12 s.

**Approach (proposed, NOT authorized):**
- **C1 — Separate wall-clock from provider TTFT.** Measure provider-only TTFT (`time.monotonic()` deltas around the `await provider.stream(...)` call, NOT around the full round). Projection should use provider TTFT, not wall-clock.
- **C2 — Adaptive floor from observed endpoint latency.** Replace static 45 s with a rolling p95 of recent provider TTFTs × safety factor (e.g. 2×). Operators can set hard ceiling via env (`JARVIS_TOOL_LOOP_MIN_TTFT_FLOOR_CAP_S`).
- **C3 — Decouple bail from loop health.** The bail decision currently treats loop-starved rounds as if the provider were slow. Separate concerns: if loop is starved (consult ControlPlaneWatchdog's recent lag history) AND projection inflated, surface a different telemetry channel ("loop_starved_not_provider_slow") and DON'T bail.

**Recommendation:** C1 + C2 as a single arc. C3 is more invasive and depends on Arcs A+B reducing starvation first.

### Architectural envelope for Slice 33

- All three arcs reuse `ast_compile_helper.py`'s spawn pool singleton — no new pools (operator binding from Slice 32 carries forward).
- All three arcs carry default-TRUE master flags with explicit-FALSE escape hatches (`JARVIS_ORACLE_GRAPH_WRITE_OFFLOAD_ENABLED`, `JARVIS_ORACLE_EMBEDDER_OFFLOAD_ENABLED`, `JARVIS_TOOL_LOOP_ADAPTIVE_TTFT_ENABLED`).
- AST pins enforce: no on-loop NetworkX mutations after Arc A; no on-loop embedder calls after Arc B; no static-45s reference outside legacy path after Arc C.
- Spine tests: heartbeat-during-graph-write (Arc A inverse of v25 wedge), heartbeat-during-embed (Arc B), adaptive-floor-recomputes-correctly (Arc C).

### v27 graduation bar (proposed)

After Slice 33 (any subset), v27 capability soak passes IFF:
- ControlPlaneStarvation events ≤ 10 over 30 min (vs v26's 81)
- Peak stall ≤ 5 s (vs v26's 56 s)
- ≥1 op reaches APPLY (the bar Slice 31+32+33 collectively unblock)
- Bonus: ≥1 RESOLVED (capability bar — what the v20→v26 arc is for)

If APPLY still doesn't fire after starvation drops, the remaining blocker is either (a) DW capability for these specific SWE prompts, or (b) something further downstream we haven't surfaced yet. Each soak peels one layer.

---

## Operator decision points (Slice 33 authorization)

1. **Arc sequence:** A only, A+B, A+B+C, or all three? (Recommendation: A first as a standalone slice, measure with v27, decide on B+C from v27 data.)
2. **AstCompileHelper pool worker count default** — should `JARVIS_AST_HELPER_POOL_MAX_WORKERS` be raised from 1 to 2 or 4 to accommodate Oracle + OpportunityMiner + (potentially) embedder concurrent load? Tradeoff: RAM headroom on 16 GB M1 vs throughput.
3. **v27 cost cap:** $5 / 3600s wall (same as v25/v26) or wider?

No code authorized for Slice 33 until operator confirms scope.
