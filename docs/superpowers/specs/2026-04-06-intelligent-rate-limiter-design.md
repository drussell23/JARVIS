# Intelligent Rate Limiter — Design Spec

**Date:** 2026-04-06  
**Status:** Approved  
**Author:** Derek J. Russell + Claude  

---

## Purpose

A standalone `RateLimitService` that sits between the Ouroboros pipeline and API providers (Doubleword 397B, Claude). Enforces per-endpoint token buckets, runs a 3-layer predictive throttle (EWMA + linear regression + variance spike detection), manages per-endpoint circuit breakers with event-driven state propagation, and pushes proactive backpressure signals to providers. All fully async, zero blocking.

---

## What This Is

- Provider-agnostic rate limiting engine with provider-specific config profiles
- Standalone service wired during boot (not embedded in providers)
- Per-endpoint granularity (upload vs poll vs generate — separate state per endpoint)
- Bidirectional: providers pull (acquire gate) AND the rate limiter pushes (backpressure events)
- 3-layer predictive throttle using stdlib math (no ML dependencies)
- Latency history persisted across restarts (warm-start predictions)
- Circuit breaker and token bucket reset fresh per session

## What This Is NOT

- Not a routing layer (BrainSelector/CandidateGenerator decide routing — the rate limiter only constrains)
- Not ML-based (v1 uses deterministic statistics; ML interface defined for future v2)
- Not provider-specific code (one engine, multiple profiles)

---

## Architecture

```
Provider (Doubleword/Claude)
    |
    |  acquire() — async gate
    |  <-- throttle_signal (backpressure push)
    |
RateLimitService (singleton, wired at boot)
    |
    +-- EndpointState (per provider:endpoint pair)
    |       +-- TokenBucket (async, store-backed)
    |       +-- CircuitBreaker (event-driven, 3 states)
    |       +-- PredictiveThrottle (EWMA + regression + variance)
    |       +-- LatencyRing (circular buffer of recent timings)
    |
    +-- RateLimitStore (abstract)
    |       +-- MemoryRateLimitStore (asyncio.Lock, v1)
    |       +-- [RedisRateLimitStore] (future v2)
    |
    +-- BackpressureBus (asyncio.Event per endpoint)
    |
    +-- LatencyPersistence (JSON, warm-start on boot)
```

---

## Components

### 1. RateLimitStore (abstract) + MemoryRateLimitStore

Abstract base class with async methods: `get_state(endpoint_key)`, `update_state(endpoint_key, tokens, last_refill)`.

`MemoryRateLimitStore` uses `asyncio.Lock()` per endpoint key. Dict-backed. Interface ready for Redis swap without changing any consumer code.

No synchronous blocking code anywhere. All mutations protected by async locks.

### 2. TokenBucket

Per-endpoint async token bucket.

- `async acquire(tokens=1) -> float`: If tokens available, consume and return 0.0 (no wait). If empty, calculate exact sleep duration until next refill via `asyncio.sleep()` — no spin loops, no polling, no `while True` busy-wait.
- Refill rate is dynamically adjusted by the PredictiveThrottle via a `throttle_multiplier` in (0.0, 1.0]. 1.0 = full speed. 0.2 = 80% throttled.
- Capacity and base refill rate come from ProviderProfile config.
- Refill is computed lazily on each `acquire()` call — elapsed time since last refill * refill_rate = tokens added. No background refill task.

### 3. ProviderProfile (config)

Frozen dataclass per provider:

```python
@dataclass(frozen=True)
class EndpointConfig:
    name: str                    # e.g. "files_upload", "batches_poll"
    rpm: int                     # requests per minute
    tpm: int = 0                 # tokens per minute (0 = no token limit)
    burst: int = 1               # max burst above steady state
    timeout_s: float = 30.0      # per-request timeout
    retry_after_default_s: float = 5.0  # default backoff when no header

@dataclass(frozen=True)
class ProviderProfile:
    provider_name: str
    endpoints: Dict[str, EndpointConfig]
```

Default profiles:

**Doubleword:**
- `files_upload`: 30 RPM, burst 2, timeout 30s
- `batches_create`: 30 RPM, burst 2, timeout 30s
- `batches_poll`: 60 RPM, burst 5, timeout 15s
- `batches_retrieve`: 60 RPM, burst 5, timeout 30s

**Claude:**
- `messages`: 60 RPM, 100K TPM, burst 3, timeout 60s

Profiles are env-overridable via `OUROBOROS_RATELIMIT_{PROVIDER}_{ENDPOINT}_RPM` etc.

### 4. CircuitBreaker (per-endpoint, event-driven)

Three states: `CLOSED` (healthy) -> `OPEN` (tripped) -> `HALF_OPEN` (probing).

**State machine:**
- `CLOSED`: Requests flow. On failure, increment consecutive failure counter. On success, reset counter. If counter >= `failure_threshold` (default 3): transition to OPEN.
- `OPEN`: All requests immediately raise `CircuitBreakerOpen(endpoint, retry_after_s)`. An `asyncio.Task` waits `recovery_timeout_s` (default 30s) then transitions to HALF_OPEN.
- `HALF_OPEN`: Allow exactly one probe request. If success -> CLOSED (reset counter). If failure -> OPEN (restart recovery timer).

**Event-driven propagation:** State changes set an `asyncio.Event` that wakes all waiters immediately. No polling. The BackpressureBus subscribes to breaker events and propagates to providers.

**Failure classification:**
- Counts as failure: HTTP 429, 500, 502, 503, `TimeoutError`, `ConnectionError`, `ConnectionTimeoutError`
- Does NOT count: HTTP 400, 401, 403, 404 (client errors — our bug, not provider instability)
- Special handling: HTTP 429 with `Retry-After` header — parse and use as recovery timeout instead of default

### 5. PredictiveThrottle (3-layer, zero dependencies)

Input: `LatencyRing` — circular buffer of the last 50 request latencies per endpoint.

**Layer 1 — EWMA (trend detection):**
- `ewma = alpha * latest_latency + (1 - alpha) * prev_ewma` (alpha=0.3)
- Baseline: median of first 10 latencies after boot
- If EWMA > 2x baseline: set throttle_multiplier to 0.6 (40% reduction)
- If EWMA > 3x baseline: set throttle_multiplier to 0.3 (70% reduction)

**Layer 2 — Linear regression (trajectory projection):**
- Fit slope `m` over the last 20 latencies using least-squares (same formula as ConvergenceTracker)
- Project: `time_to_timeout = (timeout_threshold - current_ewma) / m` (if m > 0)
- If projected breach < 30 seconds: set throttle_multiplier to 0.4 (60% reduction)
- If projected breach < 10 seconds: set throttle_multiplier to 0.2 (80% reduction)

**Layer 3 — Variance spike detector (cliff detection):**
- Compute variance of last 5 latencies vs variance of last 20 latencies
- If short_variance / long_variance > 5.0: **emergency throttle** — set throttle_multiplier to 0.2 immediately
- This catches the nonlinear cliff (200ms -> 2000ms) within 1-2 requests, before EWMA or regression react

**Output:** The final `throttle_multiplier` is the minimum of all three layers' outputs. This ensures the most conservative layer wins. Applied to the token bucket's effective refill rate: `effective_rate = base_rate * throttle_multiplier`.

All computation uses `math` and `statistics` stdlib. No ML imports. Pure deterministic skeleton.

### 6. BackpressureBus (proactive push)

Each endpoint has:
- `throttle_event: asyncio.Event` — set when throttle_multiplier changes
- `throttle_multiplier: float` — current multiplier (read by providers)
- `breaker_event: asyncio.Event` — set when circuit breaker state changes

When the PredictiveThrottle computes a new multiplier:
1. Updates the token bucket's effective refill rate
2. Sets `throttle_event` — wakes any provider coroutine awaiting it
3. Logs the change for observability

Providers can optionally subscribe: `await bus.wait_for_throttle_change(provider, endpoint)`. This is the bidirectional channel — the rate limiter pushes "slow down" without the provider asking.

When a circuit breaker trips:
1. Sets `breaker_event`
2. Emits to CommProtocol transport stack (battle test CLI shows it)

### 7. LatencyPersistence

**On shutdown:** Write per-endpoint latency history to `~/.jarvis/ouroboros/evolution/rate_limit_latency.json`:
```json
{
  "doubleword:batches_poll": {
    "latencies": [0.2, 0.3, 0.15, ...],
    "baseline_ewma": 0.22,
    "last_updated": 1712441234.5
  }
}
```

**On boot:** Load and seed each `LatencyRing` with historical data. The predictor starts warm — it knows typical latencies from previous sessions.

**NOT persisted:** Circuit breaker state, token bucket levels. These reset fresh per session — stale failure state from a previous session is dangerous (the provider may have recovered).

---

## Integration Points

### Boot
`RateLimitService` instantiated in the battle test harness (or unified supervisor) during boot. Injected into `GovernedLoopService` and made available to providers.

### Provider Calls
Before each API request:
```python
wait_s = await rate_limiter.acquire("doubleword", "batches_poll")
# wait_s is 0.0 if bucket had tokens, or the seconds waited
```

### Response Recording
After each response:
```python
rate_limiter.record("doubleword", "batches_poll", latency_s=0.45, status=200)
# or on failure:
rate_limiter.record("doubleword", "batches_poll", latency_s=2.1, status=502)
```

### Circuit Breaker
On `CircuitBreakerOpen`, the CandidateGenerator's failback FSM treats it the same as a provider timeout — falls through to next tier.

### CommProtocol
Circuit breaker state changes and emergency throttle events emit to the transport stack. The battle test CLI BattleDiffTransport shows:
```
  [BREAKER] doubleword:batches_poll OPEN (3 consecutive timeouts, recovery in 30s)
  [THROTTLE] doubleword:files_upload rate reduced to 20% (variance spike detected)
```

---

## File Structure

### New Files

| File | Responsibility |
|---|---|
| `backend/core/ouroboros/governance/rate_limiter.py` | RateLimitService, TokenBucket, CircuitBreaker, PredictiveThrottle, BackpressureBus, LatencyRing, MemoryRateLimitStore, ProviderProfile, EndpointConfig |
| `tests/test_ouroboros_governance/test_rate_limiter.py` | Full test suite |

Single implementation file — all components share EndpointState and LatencyRing. Splitting would create unnecessary cross-file coupling.

### Modified Files

| File | Change |
|---|---|
| `backend/core/ouroboros/governance/doubleword_provider.py` | Add `acquire()` before API calls, `record()` after responses |
| `backend/core/ouroboros/governance/providers.py` | Add `acquire()` / `record()` to ClaudeProvider |
| `backend/core/ouroboros/battle_test/harness.py` | Instantiate and inject RateLimitService during boot |

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `OUROBOROS_RATELIMIT_ENABLED` | `true` | Enable/disable rate limiting |
| `OUROBOROS_RATELIMIT_DOUBLEWORD_FILES_RPM` | `30` | Doubleword file upload RPM |
| `OUROBOROS_RATELIMIT_DOUBLEWORD_BATCHES_RPM` | `30` | Doubleword batch create RPM |
| `OUROBOROS_RATELIMIT_DOUBLEWORD_POLL_RPM` | `60` | Doubleword batch poll RPM |
| `OUROBOROS_RATELIMIT_CLAUDE_MESSAGES_RPM` | `60` | Claude messages RPM |
| `OUROBOROS_RATELIMIT_BREAKER_THRESHOLD` | `3` | Consecutive failures to trip breaker |
| `OUROBOROS_RATELIMIT_BREAKER_RECOVERY_S` | `30` | Seconds before HALF_OPEN probe |
| `OUROBOROS_RATELIMIT_EWMA_ALPHA` | `0.3` | EWMA smoothing factor |
| `OUROBOROS_RATELIMIT_VARIANCE_SPIKE_RATIO` | `5.0` | Variance ratio for cliff detection |

---

## Boundary Principle Compliance

- **Deterministic (skeleton):** Token bucket math, circuit breaker state machine, EWMA/regression/variance formulas, failure counting, latency persistence, backpressure event propagation
- **Agentic (nervous system):** The *decision* to route away from a throttled provider (made by BrainSelector/CandidateGenerator based on circuit breaker state, not by the rate limiter itself)

The rate limiter measures, constrains, and signals. It never decides where to route — that's the orchestrator's job.

---

## Success Criteria

1. `acquire()` never blocks the event loop (no synchronous sleep)
2. Token bucket correctly limits requests to configured RPM per endpoint
3. Circuit breaker trips after 3 consecutive failures on the same endpoint
4. Circuit breaker recovers via HALF_OPEN probe after recovery timeout
5. EWMA detects gradual latency increase and reduces refill rate
6. Linear regression projects timeout breach and pre-emptively throttles
7. Variance spike detector catches sudden cliffs (200ms -> 2000ms) within 2 requests
8. Backpressure events propagate to providers within one event loop cycle
9. Latency history survives restart and seeds warm predictions
10. Battle test runs for 10+ minutes without hitting provider rate limits or timeouts
