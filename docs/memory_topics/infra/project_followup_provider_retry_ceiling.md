---
title: Ticket A2 — per-op provider retry ceiling (DEFERRED)
modules: [backend/core/ouroboros/governance/providers.py, backend/core/ouroboros/governance/candidate_generator.py]
status: historical
source: project_followup_provider_retry_ceiling.md
---

# Ticket A2 — per-op provider retry ceiling (DEFERRED)

**Parent:** Ticket A (split 2026-04-23). **Sibling (shipped):** Ticket A1 (`project_followup_idle_timeout_retry_hijack.md`, commit `6e87dea643`).

**Priority:** Not on critical path for #7 restart. Ships after Ticket B (partial-summary-on-interrupt) + Ticket C (native `--headless` flag), or after #7 FINAL, whichever lands first. Defense-in-depth layer on top of A1's session-level cap.

## Problem A1 already solves

`--max-wall-seconds` (A1) gives a hard session-level ceiling. A retry storm in any op cannot keep the session alive past the configured cap. That is sufficient to restart the #7 graduation cadence deterministically. Closed the critical path.

## Problem A2 addresses (fairness, not termination)

Within a bounded session, a single stuck op can still consume the *whole* wall-clock budget by sitting in its own retry loop. The session still terminates at T+`max_wall_seconds_s`, but everything else on the queue never got a fair turn. In a 40-minute cap, one op eating 35 minutes of retry before the cap fires starves 15+ other ops that could have walked the pipeline.

Per-op retry ceiling says: any single provider call is capped at N attempts OR T wall-seconds, whichever comes first. Exceeded → `ProviderRetryExhausted` raised with `failure_class=infra_transport`, FSM transitions the op to POSTMORTEM (tagged `infra_waiver`), next op dequeues, session continues.

## Proposed fix (spec, not implementation)

### Touch points

- `backend/core/ouroboros/governance/providers.py::_call_with_backoff` (lines 4682–4847)
- `backend/core/ouroboros/governance/candidate_generator.py::_call_fallback` (lines 2399–2592)

### New exception type

```python
class ProviderRetryExhausted(RuntimeError):
    """Raised when a provider's retry budget is exhausted on a single op.

    Carries enough context for the orchestrator's POSTMORTEM classifier
    to tag the op with ``failure_class=infra_transport`` and `infra_waiver`
    the underlying transport error. Does NOT block subsequent ops — the
    op-level retry budget is independent from the session wall-clock cap
    (Ticket A1 / commit 6e87dea643).
    """
    def __init__(
        self,
        label: str,
        attempts: int,
        wall_s: float,
        last_exc: BaseException,
    ) -> None:
        super().__init__(
            f"Provider {label!r} retry budget exhausted after "
            f"{attempts} attempts / {wall_s:.1f}s; last={type(last_exc).__name__}"
        )
        self.label = label
        self.attempts = attempts
        self.wall_s = wall_s
        self.last_exc = last_exc
        self.failure_class = "infra_transport"
```

### Env knobs (per-op, not global)

```
JARVIS_PROVIDER_RETRY_MAX_ATTEMPTS   int  default 3   hard attempt ceiling
JARVIS_PROVIDER_RETRY_MAX_WALL_S     float default 120.0  wall-clock budget
```

First-trip wins — whichever exhausts first raises `ProviderRetryExhausted`.

### Orchestrator handling

POSTMORTEM phase already has a `failure_class` field in the ledger. When a `ProviderRetryExhausted` bubbles up from GENERATE / VALIDATE provider calls, the exception's `failure_class="infra_transport"` + structured fields (`label`, `attempts`, `wall_s`) get stamped on the postmortem row. No runner-attributed flag — these are external-weather waivers, same semantic class as A1's `infra_waiver: anthropic_transport` handling.

### Tests

- Unit: mock provider that always raises `APITimeoutError` → `ProviderRetryExhausted` after N attempts, not after the wall-clock deadline.
- Unit: mock provider that always raises but very slowly → `ProviderRetryExhausted` on wall-clock budget before attempt ceiling.
- Unit: happy path — provider succeeds on attempt 2/3 → no exception, normal candidate returned.
- Integration: op hits `ProviderRetryExhausted` → POSTMORTEM with `failure_class=infra_transport`, next op dequeues and makes progress (proves FSM isn't blocked).

## Blast radius

Touches provider hot paths — requires care:

- `_call_with_backoff` is called by multiple provider classes (Claude, DW, Prime fallback). Must keep backoff semantics + telemetry intact.
- `_call_fallback` in `candidate_generator.py` already has deadline-aware budget tracking. Integration must not double-count.
- FSM POSTMORTEM handling must correctly recognize the new exception class without breaking existing failure classifications.

Smaller diff is preferable. Consider landing the exception type + one touch point (providers.py) first, then adding candidate_generator.py as a follow-up if the first landing is clean.

## Relation to graduation work

- **NOT required** for #7 GENERATE graduation restart — A1 already guarantees deterministic termination.
- **Does improve** graduation-session throughput: more ops reach GENERATE / VALIDATE / GATE / SLICE4B per session because one stuck op can't monopolize the wall-clock budget. This may help observe Iron Gate signals firing live rather than always being blocked by upstream retry storms.

## Sequencing

Per operator binding 2026-04-23:

> Ticket B → Ticket C, then rerun #7 S2′ + S3 with --max-wall-seconds and the new shutdown semantics. Guard 1 (A2) ships after B/C or after #7 FINAL — whichever comes first, but not on the critical path to reopening the cadence.

So the order is: B → C → (rerun #7 with A1 alone) → A2 afterward.

## Not in this ticket

- Changing retry backoff curve (keep current exponential + jitter).
- Per-provider max-attempts differences (use a single global env knob).
- Changing `_CLAUDE_MIN_RETRY_CYCLE_S` or `_CLAUDE_BACKOFF_BUDGET_FRACTION` defaults (those are load-shaping knobs, not fairness guards).
