---
title: Ticket #4 Slice 1 — BackgroundMonitor primitive (2026-04-20)
modules: [backend/core/ouroboros/governance/background_monitor.py]
status: historical
source: project_ticket_4_background_monitor.md
---

# Ticket #4 Slice 1 — BackgroundMonitor primitive (2026-04-20)

Closes the observability gap between Claude Code (streams stdout events
live) and O+V (polls blocking subprocess.run with timeout). Pure
infrastructure substrate — later slices add Venom Monitor tool +
TestRunner migration on top.

## File

`backend/core/ouroboros/governance/background_monitor.py` (300 lines)

## Public API

- `MonitorEvent` frozen dataclass: `kind`, `op_id`, `ts_mono`, `data`,
  `sequence`, `exit_code`, `truncated`, `line_terminator`. Kind
  constants: `KIND_STDOUT`, `KIND_STDERR`, `KIND_EXITED`, `KIND_ERROR`.
- `BackgroundMonitor(cmd, *, op_id, cwd, env, ring_capacity=1024,
  queue_capacity=2048, terminate_grace_s=2.0, event_bus=None,
  bus_topic_prefix="background_monitor")` — async context manager.
  `events()` yields MonitorEvents until subprocess exits.
  `ring_snapshot()` returns immutable tuple. `exit_code` / `pid`
  properties.

## Design decisions

- Argv-only spawn (asyncio subprocess execve variant). No shell
  interpretation, no injection surface. Pinned by test 21.
- Line-granular events via readline(). LimitOverrunError (64KB cap)
  caught, reader emits truncated=True event with partial buffer.
- Shared sequence counter across stdout + stderr (monotonic, lock-
  protected). Exited event has the highest sequence.
- Backpressure via blocking put() — slow consumer stalls subprocess
  stdio, not data loss.
- Bounded ring buffer via deque(maxlen=N). FIFO eviction. Tuple
  snapshot.
- Graceful shutdown: SIGTERM → grace (default 2s) → SIGKILL.
  exit_code populated BEFORE cancelling reader tasks (prevents
  None on _await_exit mid-cancel; pinned by test 13 after a live
  fix in __aexit__).
- Non-UTF8 safety: decode with errors="replace".
- Optional bus: event_bus=None → pure local-observer. When provided,
  publishes to `{prefix}.{op_id}.{kind}` with persist=False. Bus
  exceptions caught + DEBUG-logged, never kill the monitor.

## Ordering guarantee

KIND_EXITED is ALWAYS the LAST event. `_await_exit` waits for
stdout/stderr readers to drain via asyncio.gather BEFORE reaping the
process + emitting the terminal event. Consumers can trust the ring
buffer is complete at KIND_EXITED.

## Authority posture

Advisory / observability only. Slice 1 ships NO tool surface, NO
TestRunner migration. Later slices wire specific consumers.

## Regression spine — 21 tests green

- Spawn + teardown (3)
- Event shape (3)
- Stream mixing (2 — critical ordering pin)
- Ring buffer (2 — bounded FIFO + immutable snapshot)
- Non-UTF8 safety (1)
- Graceful shutdown (2 — early-exit + SIGTERM escalation)
- Event bus (3 — publish / no-bus / bus-exception caught)
- Concurrent monitors (1 — no cross-contamination)
- Input validation (3 — ring/queue capacity + grace < 0)
- Security pin (1 — argv not shell string)

## Next slices

- Slice 2 — Venom Monitor tool (read-only, not in _MUTATION_TOOLS).
  Signature: `monitor(cmd, pattern=None, timeout=30.0)` returning
  ring snapshot + early-exit flag. Policy-gated + risk-tier-aware.
- Slice 3 — TestRunner migration behind
  JARVIS_TEST_RUNNER_STREAMING_ENABLED. Per-test-line feedback into
  the governance loop; early-exit on first failure pattern.
- Slice 4 — Graduation: flip streaming default true after 3 clean
  sessions + CC-parity live-fire proof.
