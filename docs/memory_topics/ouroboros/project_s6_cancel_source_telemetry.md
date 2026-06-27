---
title: Project S6 Cancel Source Telemetry
modules: []
status: historical
source: project_s6_cancel_source_telemetry.md
---

## TL;DR

**S6 CancelledError is NOT a transport flap.** Three distinct cancel classes observed in the seed op (`op-019dc1b1-4baa`):

| Time | Class | err_class | sem_wait_total_s | pre_sem_remaining_s | parent remaining_s | Cause |
|---|---|---|---|---|---|---|
| 16:20:06 (×3) | A — full budget timeout | `TimeoutError` | 120.0 / 120.43 | 220.0 | 0.0 | `_FALLBACK_MAX_TIMEOUT_S=120s` per-call ceiling fired (3 parallel attempts simultaneous) |
| 16:21:37 (×1) | B — ToolLoop round budget exhausted | `TimeoutError` | 90.43 | 0.0 | 0.0 | ToolLoop round had `budget=90.0s` (logged 16:20:06 BudgetPlan); fired wait_for at 90s |
| 16:21:49 (×3) | C — **external cooperative cancel** | `CancelledError` | 12.32 | 219.99 | 207.67 | **UNATTRIBUTED** — 3 parallel attempts cancelled at 12.3s with 207s budget remaining |

Class C is the one the operator flagged; it's not transport (Claude was healthy, no `hard_pool_signal` events on these calls; bytes_received non-zero on the round-1 case), not deadline (207s left), not provider error (no error response — just a cancel from outside the wait_for).

## Method (no code change yet)

1. Static log analysis on `bt-2026-04-24-225137/debug.log` (158 KB).
2. Cross-referenced `EXHAUSTION` events with `[ToolLoop] BudgetPlan` and `[CandidateGenerator] Fallback sem acquire` lines.
3. Searched for transport-class signals: `hard_pool_signal`, `RemoteProtocolError`, `ConnectTimeout`, `OSStatus -26276`. **No Claude-path transport errors.** GitHub `OSStatus -26276` errors observed at 16:12:53–16:12:56 are isolated to `gh` CLI (`api.github.com/graphql`), unrelated.
4. Walked `_call_with_backoff` / `[ClaudeProvider] stream terminated` log signature: `_elapsed=…s budget=…s` reflects the `timeout_s` parameter passed in. So `budget=90.0s` at 16:21:37 = `wait_for(stream, timeout=90)` — i.e., the ToolLoop's per-round budget propagated as the stream timeout.

## Three cancel classes — what they mean

### Class A: `_FALLBACK_MAX_TIMEOUT_S=120s` per-call ceiling (round 1 generate)

Three parallel Claude generate calls all ran to the full 120s before being cancelled with `TimeoutError`. `pre_sem_remaining_s=220.0` confirms parent had ample budget; the cap is per-call, not parent. This is **expected behavior** under the multi-file Iron Gate / parallel candidate generation — but means three full 120s Claude burns landed even with the seed having 220s parent budget.

**Implication**: 3 × 120s = 360s of theoretical worst-case GENERATE wall-time *per attempt*, even with healthy provider. This eats the BG worker pool ceiling.

### Class B: ToolLoop round budget (round 2)

Single attempt cancelled at 90.43s — matches the BudgetPlan `budget=90.0s` logged at 16:20:06. The ToolLoop's `_compute_budget` produces a per-round budget independent of parent deadline; when the round budget expires, all child wait_fors get cancelled.

**Implication**: The 90s round budget was lower than the upstream Class-A cap (120s) — so this round's Claude calls were cancelled BY ToolLoop at 90s rather than reaching their own 120s cap. Layered budgets are working as intended; this isn't a bug.

### Class C: External cooperative cancel @ 12.32s with 207s remaining

This is the unaccounted class. `pre_sem_remaining_s=219.99` and `remaining_s=207.67` mean:
- Sem acquired immediately (sem_wait ≈ 0).
- Claude call ran for ~12s.
- Cancelled despite ample parent deadline AND no Class A/B trigger.

**Hypotheses (need telemetry to disambiguate)**:

1. **Sibling-task cancellation in 3-way parallel** — `_call_fallback` may launch 3 parallel attempts (consistent with 3 simultaneous EXHAUSTION lines per moment). If they're inside a single `asyncio.gather(..., return_exceptions=False)` and one fails fast, the others get cancelled within the gather's cleanup. The 12s elapsed could be the sibling's failure-detection time.
2. **GENERATE_RETRY rapid bail** — the Orchestrator's "Generation attempt 2/2" log line suggests a retry harness above `_call_fallback`. If the retry harness has its own deadline (e.g., parent deadline minus accumulated backoff) AND the backoff math underflows, it may cancel mid-call.
3. **TopologyBlock force-route mid-flight** — at 16:21:37 the seed switched from `route=standard` to "IMMEDIATE route: Claude direct (skip DW)" mid-attempt. If TopologyBlock changes routing mid-call and reissues a fresh fallback acquire, the original might be cancelled to free the slot.

## Telemetry needed before S7

Minimal additive logging in `candidate_generator._call_fallback` and `tool_executor.run_tool_loop`:

1. Inside the `wait_for(self._fallback.generate(...), timeout=remaining)` exception handler in `_call_fallback`, log:
   ```
   [CandidateGenerator] cancel_attribution op=%s phase=%s
     handler=fallback_inner caller_task=%s parent_task=%s
     elapsed_s=%.2f remaining_at_cancel_s=%.2f source=%s
   ```
   Where `source` is determined by introspecting `asyncio.current_task().cancelled()` and walking the parent task chain via `asyncio.all_tasks()` looking for the cancelling task (best-effort; if attribution fails, log `source=unknown`).

2. Log the same in `ToolLoop._dispatch_round` exception handler so we know which level cancelled.

3. At every cancel site that decides to cancel a child, log `[CancelOrigin] cancelling child=%s reason=%s` (so we have BOTH ends of the cancel).

Estimated 30–60 LOC across 2 files; no behavior change. Pure observability. Tests: 3 unit tests covering (sibling-cancel, retry-harness-cancel, topology-reroute-cancel) using mock providers + `asyncio.gather`.

## TLS / OSStatus correlation result

- **Claude path: ZERO TLS errors.** All 7 Claude stream events in S6 either succeeded (end_turn) or hit Class A/B/C above. No `httpx.ConnectError`, no `RemoteProtocolError`, no SSL-related warnings.
- **GitHub path (`gh` CLI / GitHubIssueSensor): 3 OSStatus -26276 errors at 16:12:53–56.** Identical signature on all 3. Suggests host-level cert chain issue affecting `api.github.com`. Distinct from Claude path.
- **Recommendation per operator's optional cheap datapoint**: probe `gh auth status` and `curl -I https://api.anthropic.com` outside the harness to confirm Anthropic is reachable while GitHub is not (would explain OSStatus localization to GitHub).

## What this means for the bar

This investigation **rules out transport flap** as the S6 GENERATE failure cause. The cancellation is internal — either expected (Class A/B layered budgets) or attributable to one of the 3 hypotheses for Class C. Adding the cancel-attribution telemetry above turns Class C into a deterministic-classifiable event.

**Recommendation**: 
- **Don't burn S7 yet.** Land the telemetry patch first (3 small commits + tests), then S7 either reproduces Class C with attribution OR demonstrates the seed op completes GENERATE under the new bar.
- The 3 × 120s parallel-call cost (Class A) suggests the seed's multi-file candidate generation is naturally expensive. If headless completion contract (Path ii) accepts `[ParallelDispatch]` markers as the bar, GENERATE doesn't have to *succeed* — just emit a candidate that the post-GENERATE seam can route. Class A "TimeoutError on per-call cap" might still produce a partial candidate that triggers the seam.

## Operator decision points

1. Approve cancel-attribution telemetry patch (~60 LOC + 3 tests)?
2. Approve cheap connectivity probe outside harness (curl + gh)?
3. Pick headless completion contract path (i / ii / mixed) per `feedback_headless_completion_contract.md`?

Once 1+3 are settled, S7 is ready.
