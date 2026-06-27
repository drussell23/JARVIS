---
title: Ticket A1 — idle_timeout hijacked by provider retry storm (SHIPPED)
modules: [scripts/ouroboros_battle_test.py]
status: historical
source: project_followup_idle_timeout_retry_hijack.md
---

# Ticket A1 — idle_timeout hijacked by provider retry storm (SHIPPED)

**Status:** Ticket A was split 2026-04-23 per operator binding after the initial implementation landed.

- **A1 (this doc): SHIPPED** as commit `6e87dea643`. Guard 2 (`--max-wall-seconds` CLI flag + wall-clock watchdog) is the hard unblocker for the S2 failure mode. Session-level physics first (§3).
- **A2 (`project_followup_provider_retry_ceiling.md`): DEFERRED.** Per-op `ProviderRetryExhausted` + `infra_transport` failure class. Defense-in-depth; fairness (no single op can eat the whole wall budget). Ships after Ticket B + C or after #7 FINAL, whichever comes first.

The sections below describe the full original Ticket A scope. "Guard 1 / authoritative fix" = A2 (deferred); "Guard 2 / belt-and-suspenders" = A1 (shipped). The bug was session-level termination, which Guard 2 solves on its own; Guard 1 only improves per-op fairness inside that bounded window.

## Observed behavior

Battle-test session `bt-2026-04-23-070317` (#7 GENERATE S2 graduation attempt) entered a Claude API retry loop at 00:58:27 with stack `APITimeoutError → ConnectTimeout → ConnectTimeout → TimeoutError → CancelledError: deadline exceeded`. The `ClaudeProvider` resilience wrapper (`providers.py _stream_with_resilience`) caught the timeout and backed off 2.0s, retrying the stream. Retry attempts continued generating internal heartbeats + log lines, which reset the harness's idle-activity counter. As a result:

- Session configured with `--idle-timeout 600` never hit the 10-minute quiescence threshold.
- Operator-invoked TaskStop at wall-clock +67 min was the only way to end the session.
- `summary.json` was never written — see Ticket B (partial-summary-on-interrupt).
- Budget cap also wasn't a safety net because retries don't spend $ (they fail before provider returns billable content).

## Root-cause analysis

Two independent lifecycle invariants failed together:

1. **§3 deterministic lifecycle violation.** The organism is supposed to remain master of its own liveness. Provider retry loops live inside the harness's async context, so any "activity" they generate (log lines, heartbeats, internal `call_soon` events) counts toward "not idle" even when semantically the op has made zero forward progress. External flakiness should not be able to prevent the harness from shutting down.

2. **No wall-clock ceiling.** The battle-test harness accepts `--idle-timeout` (gap-based) and budget cap ($-based) but has no `--max-wall-seconds`. When idle-gap is defeated by a chatty retry storm AND budget is defeated by the storm-being-unbilled, there is no third watchdog.

## Proposed fix (spec, not implementation)

Two complementary guards. Either alone closes the bug; both together match §3 belt-and-suspenders.

### Guard 1 — Retry-budget advances idle clock (authoritative fix)

Retry loops in `providers.py _stream_with_resilience` and `candidate_generator.py` `_call_fallback` should be opaque to the idle-activity counter. Options:

- **(preferred)** Carry a distinct `provider_retry_in_flight` flag in the harness's activity tracker. Retry heartbeats are marked "retry activity, not forward progress" and the idle counter keeps ticking.
- **(alternative)** Per-op retry budget (e.g. max 3 attempts × 60s each = 180s) that, once exhausted, raises `ProviderRetryExhausted` and the op terminates with `failure_class=infra_transport`. Prevents unbounded retry storms at the op level.

Both options are compatible. Preferred implementation: per-op retry budget + retry-activity tagging on the harness idle tracker.

### Guard 2 — `--max-wall-seconds` cap (separate knob)

Add a third CLI flag to `scripts/ouroboros_battle_test.py`:

```
--max-wall-seconds SEC   Hard wall-clock cap on total session duration
                         (default: unset / unlimited; graduation runs MUST set this)
```

When the wall-clock ceiling is hit:

1. Harness invokes the existing graceful shutdown path (same as `--idle-timeout`).
2. Any in-flight ops receive `CancelledError`.
3. `summary.json` is written via the existing `atexit` + signal-handler fallback (see Ticket B — currently broken on external-process kill).
4. Session exit code distinguishes `wall_clock_cap` from `idle_timeout` / `budget_exhausted`.

Graduation runbook update: every graduation soak launches with `--max-wall-seconds 2400` (40 min) or similar, chosen to exceed the normal 20–25 min happy path by 60% but terminate retry-storm purgatory well short of 67+ min.

## Blast radius

Zero runtime semantics for the happy path. Impact limited to retry-storm and ultra-long-session edge cases. The guard is defensive — it only activates when liveness is already pathological.

## Test plan

- Unit test: mock provider that raises `APITimeoutError` on every attempt, assert the harness exits on wall-clock cap within ±5s of the configured threshold (not on idle_timeout).
- Unit test: mock provider that raises transient errors for N retries then succeeds, assert normal op completion (guard does NOT fire).
- Integration: run a graduation-pattern soak with `--max-wall-seconds 120` and a deliberately broken provider; assert clean shutdown + summary.json + `stop_reason=wall_clock_cap` at t≈120s.

## Relation to graduation work

This ticket is a **hard unblocker** for #7 GENERATE graduation. Per operator binding 2026-04-23: "Do not start S3 while the client is still in known-bad API weather unless you add an explicit short wall-clock cap for graduation runs." Shipping Ticket A means:

- Future graduation soaks terminate deterministically regardless of provider weather.
- `session_outcome=wall_clock_cap` becomes a valid clean-stop for harness-class sessions (extend the existing footnote).
- Graduation cadence isn't held hostage to Anthropic's incident timeline.

## Not in this ticket

- Anthropic API reliability improvements (external, not our code).
- DW batch reliability (separate infra-tracking in `project_async_shutdown_race_triage.md`).
- Aggressive retry reduction (may harm happy-path resilience; out of scope).
