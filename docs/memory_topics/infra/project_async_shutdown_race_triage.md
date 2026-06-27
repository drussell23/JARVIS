---
title: Async shutdown race + DW connection flake — root-cause triage
modules: [scripts/ouroboros_battle_test.py]
status: historical
source: project_async_shutdown_race_triage.md
---

# Async shutdown race + DW connection flake — root-cause triage

Surfaced during CLASSIFY flag #2 graduation session S3. Neither error ties to the CLASSIFY runner code path — operator accepted both as infra-noise waivers, but asked for root-cause investigation to stop cumulative tagging.

## Error #1 — asyncio shutdown race (`Event loop is closed`)

### Stack (13:52:26 in S3 debug.log)

```
concurrent.futures._base._invoke_callbacks
  -> asyncio.futures._call_set_state
  -> asyncio.base_events.call_soon_threadsafe
  -> _check_closed
  -> RuntimeError: Event loop is closed
```

### Root cause

`scripts/ouroboros_battle_test.py` closes the asyncio event loop without draining pending `run_in_executor` / `asyncio.to_thread` thread-pool tasks first:

```python
try:
    loop.run_until_complete(harness.run())
except KeyboardInterrupt:
    ...
finally:
    loop.close()   # thread-pool futures still in-flight
```

When `harness.run()` returns, the harness completed its graceful shutdown (Oracle cache saved, summary written, notebook generated). Background `asyncio.to_thread(...)` tasks spawned by long-lived subsystems (Oracle cache-save I/O, SessionReplay HTML write, NotebookGenerator) may still have thread-pool Futures whose completion callbacks have not fired yet. Those callbacks schedule `_set_state` on the destination asyncio loop via `call_soon_threadsafe`, which hits `_check_closed` on the just-closed loop.

Classic Python 3.9 asyncio shutdown hygiene miss. Canonical fix uses the 3.9+ methods `shutdown_asyncgens()` + `shutdown_default_executor()` before `close()`.

### Fix

Apply standard shutdown hygiene in the script's `finally` block:

```python
finally:
    try:
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:
        pass
    try:
        loop.run_until_complete(loop.shutdown_default_executor())
    except Exception:
        pass
    loop.close()
```

`shutdown_default_executor()` (Python 3.9+) waits for all pending thread-pool tasks to complete or be cancelled, allowing their callbacks to fire while the loop is still open. `shutdown_asyncgens()` drains any async generators. Both wrapped in try/except because some harness exit paths may have already nulled parts of the runtime state.

### Blast radius

Zero runtime semantics. Shutdown-only path. The only observable change: session debug.log no longer carries the `concurrent.futures` callback error line, and the session exits cleaner.

### Proof / non-regression

- Parity tests 248/248 still green (they never touch shutdown).
- Post-fix sessions should not emit the `Event loop is closed` traceback under normal completion.
- Under operator-initiated SIGINT, the graceful-shutdown path already caught this; the race only happened on natural idle-timeout completion.

## Error #2 — DoubleWord batch create connection failure

### Stack (13:40:30 in S3 debug.log)

```
aiohttp.connector._create_connection
  -> _create_proxy_connection
  -> _create_direct_connection
  -> _wrap_create_connection
  -> aiohappyeyeballs.start_connection
  -> _staggered.staggered_race
```

### Root cause

External: DoubleWord API endpoint was unreachable or slow-to-accept at that moment. `aiohappyeyeballs.staggered_race` could not establish a socket within its stagger budget. `DoublewordProvider.batch_create` already has retry + cooldown via the `ExhaustionWatcher` path, so the error is logged but does not propagate as a runtime failure — the provider falls back via the adaptive failback tier cascade.

### Fix

Not a code fix this turn. The existing `ExhaustionWatcher` handles this correctly. Options if the pattern becomes chronic:

1. Widen aiohappyeyeballs stagger budget — env knob if supported, or patch the `DoublewordProvider.__init__` aiohttp connector with a longer `timeout.connect`.
2. Circuit breaker at `DoublewordProvider` layer — tighten to pause DW attempts for N seconds after a connection failure.
3. Pattern-tracking ticket — if the same waiver appears in 2+ future graduation sessions, open a dedicated investigation.

### Decision

Keep on pattern-tracking list. No code change this turn. The log line stays as §8 observability signal of the transient.

## Commit plan

1. `scripts/ouroboros_battle_test.py` — add shutdown hygiene (Error #1 fix).
2. This triage doc committed alongside.
3. No changes to `DoublewordProvider` — kept on watch list.

## Follow-up

Once Error #1 fix lands, future soak sessions that STILL emit the callback race at shutdown should be investigated — likely a subsystem holding on to a thread-pool Future past the `shutdown_default_executor` boundary.
