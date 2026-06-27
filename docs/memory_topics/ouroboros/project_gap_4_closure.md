---
title: Gap #4 — Official Closure (2026-04-20)
modules: []
status: merged
source: project_gap_4_closure.md
---

# Gap #4 — Official Closure (2026-04-20)

**Status**: CLOSED (Reading A)
**Closing commit**: `405a808873` (Ticket #4 Slice 4 — graduation)
**Closure authorization**: explicit operator decision, 2026-04-20

## Gap text

> 4 Background task monitors (Monitor tool, event streams)
> CC streams stdout events. O+V polls.

## Reading A (the accepted closure)

The gap named three outcomes; Ticket #4 delivered and graduated all three:

1. **Monitor tool** → shipped Slice 2, graduated Slice 4.
   `JARVIS_TOOL_MONITOR_ENABLED` default `true`.
2. **Event streams (primitive + consumption)** → shipped Slice 1
   (`BackgroundMonitor` primitive) + Slice 3 (TestRunner streaming).
   `JARVIS_TEST_RUNNER_STREAMING_ENABLED` default `true`.
3. **Fix O+V vs CC mismatch on primary pain surface (long-running
   tests)** → TestRunner now streams per-test events via
   `[TestRunner] streaming ...` INFO + optional `event_callback`.

Structural safeguards preserved through graduation:
- Manifest capabilities unchanged (`{"subprocess"}`, NOT `"write"`)
- Binary allowlist still fires
- Argv-only spawn, no shell
- Infra (TestRunner) vs Venom (monitor tool) boundary pinned
- 3-module dependency-direction rule grep-enforced

Manifesto compliance:
- §1 satisfied: graduation changed opt-in friction, NOT authority
- §8 satisfied: high-signal subprocess paths visible via
  streaming + optional callback/bus patterns

Scorecard: **111/111 tests green** across the four slices + legacy.

## Reading B — OUT OF SCOPE for Gap #4

"Never poll/block anywhere subprocess exists" is NOT what Gap #4
named. Remaining `subprocess.run` / `proc.communicate()` sites
(Venom bash tool, battle-test harness, orchestrator git one-shots,
AutoCommitter push, etc.) are **backlog-only**, not regressions
against this gap's closure.

**Do NOT auto-open a sweep/audit ticket.** If a subprocess
observability pain appears in practice (e.g., operators report
bash-tool output latency), open a new ticket with explicit
authorization — do not retcon it onto Gap #4.

## Operational state post-closure

- Fresh install: model can call `monitor` (read-only,
  allowlisted); TestRunner streams test events by default.
- Kill switch: `JARVIS_TOOL_MONITOR_ENABLED=false` and/or
  `JARVIS_TEST_RUNNER_STREAMING_ENABLED=false` for targeted
  revert (flags independent, full-revert matrix pinned by
  `test_4h_full_revert_matrix`).
- Next: await explicit authorization before starting the next
  numbered gap/ticket. No proactive work.
