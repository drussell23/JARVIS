---
title: Ticket #4 Slice 2 — Venom monitor tool (2026-04-20)
modules: [backend/core/ouroboros/governance/monitor_tool.py, backend/core/ouroboros/governance/tool_executor.py, tests/governance/test_monitor_tool.py, test_runner.py, backend/core/ouroboros/governance/background_monitor.py]
status: historical
source: project_ticket_4_monitor_tool.md
---

# Ticket #4 Slice 2 — Venom monitor tool (2026-04-20)

Closes Slice 2 of the CC-parity event-streaming arc. Slice 1 shipped the
BackgroundMonitor primitive (isolated, no Venom wiring). Slice 2 wires a
deny-by-default, binary-allowlisted Venom tool surface on top, with
tests that encode the constraints as invariants.

## Files

- `backend/core/ouroboros/governance/monitor_tool.py` (NEW, ~280 lines) —
  the handler module + env helpers + structural validators.
- `backend/core/ouroboros/governance/tool_executor.py` (MODIFIED) —
  manifest registration + async-native dispatch branch + Rule 16 in
  GoverningToolPolicy.evaluate.
- `tests/governance/test_monitor_tool.py` (NEW, 30 tests).

## Non-negotiables honored (from user authorization)

1. Policy-gated and risk-tier-aware — **deny-by-default**.
   `JARVIS_TOOL_MONITOR_ENABLED` defaults **false** (opposite of
   run_tests / bash which default true). Explicit opt-in required.
2. Read-only category. Manifest capabilities = `{"subprocess"}` NOT
   `{"subprocess","write"}`. NOT in _MUTATION_TOOLS. Under
   is_read_only scope the ScopedToolGate still allows it
   (observation is not mutation).
3. Argv-only spawn — inherits Slice 1 primitive's security pin. Tool
   never invokes a shell. Additional binary allowlist caps cmd[0]
   basename to an operator-curated set.
4. Tool registration + regression spine — 30 tests covering the
   deny/allow matrix, handler happy path + failure modes, manifest
   integrity, authority invariant, bus-failure-doesn't-break-monitor
   invariant (reused from Slice 1).
5. No TestRunner migration. No graduation flip. Slices 3+4 stay out
   of scope.

## Env knobs (all deny-by-default or conservative)

- `JARVIS_TOOL_MONITOR_ENABLED` — master switch, default **false**
- `JARVIS_TOOL_MONITOR_ALLOWED_BINARIES` — CSV basenames, default
  `"pytest,python,python3,node,npm,go,cargo,make,ruff,mypy,pyright"`
- `JARVIS_TOOL_MONITOR_TIMEOUT_S` — per-invocation ceiling, default `60.0`
- `JARVIS_TOOL_MONITOR_MAX_EVENTS` — ring + output cap, default `500`

## Policy Rule 16 (inserted in GoverningToolPolicy.evaluate)

First-match-wins gate, runs AFTER Rule 15 (delegate_to_agent):

1. `monitor_enabled()` false → DENY `tool.denied.monitor_disabled`
2. `classify_cmd(args["cmd"])` fails → DENY `tool.denied.monitor_bad_args`
3. `basename(cmd[0])` not in allowlist → DENY
   `tool.denied.monitor_binary_not_allowed`
4. Fall through → ALLOW (final line of evaluate())

## Handler contract (run_monitor_tool)

Returns `ToolResult` with JSON output:

    {
      "exit_code": int | null,
      "duration_s": float,
      "event_count": int,
      "events": [{"kind","data","ts_mono","sequence","exit_code"}, ...],
      "early_exit": bool,
      "early_exit_match": str,
      "timed_out": bool,
      "truncated": bool
    }

Effective timeout = min(model-requested, env ceiling, Venom deadline-remaining).
Never silently exceeds any of the three.

## Defense-in-depth

- Handler re-validates cmd shape via classify_cmd even though policy
  should have caught it. Direct-call tests bypassing policy cannot
  crash the handler.
- Malformed regex pattern → clean EXEC_ERROR, not an uncaught re.error.
- Binary-vanished-between-approval-and-spawn → FileNotFoundError
  caught, returned as EXEC_ERROR with a descriptive message.
- Any unexpected exception caught at the tool boundary and returned
  as EXEC_ERROR — the handler never raises past its return.
- Bus publish failure in the underlying primitive does NOT break the
  handler (Slice 1 invariant preserved; test 21 re-validates).

## Regression spine — 30 tests

Manifest + authority (3): registered with correct caps, not in
_MUTATION_TOOLS, allowed under read-only ScopedToolGate.

Policy gate deny/allow matrix (9):
- deny when master switch off
- deny when master switch explicitly "false"
- deny when binary not in allowlist
- deny bad_args non-list cmd
- deny bad_args empty cmd
- deny bad_args non-string element
- allow on happy path (enabled + allowlisted)
- allow on absolute-path invocation (basename match)
- deny with empty allowlist (runtime kill switch)

Handler behavior (8): happy path, early-exit on pattern, timeout
enforced, env ceiling caps requested timeout, binary-not-found clean
error, bad-regex clean error, missing-cmd clean error, max_events cap.

Observer invariant (1): completes without event_bus, reuses Slice 1
invariant.

Helper-function pins (9): classify_cmd × 4 variants,
extract_binary_basename × 1, monitor_allowed_binaries CSV parse × 1,
monitor_enabled default/true/case × 3.

## What Slice 3 will consume

TestRunner migration (`test_runner.py`) — replaces the blocking
`proc.communicate()` path with a BackgroundMonitor-backed streaming
path behind `JARVIS_TEST_RUNNER_STREAMING_ENABLED` (default false).
Per-test-line feedback into the governance loop; early-exit on first
failure pattern.

Slice 3 uses the **primitive** directly (from background_monitor.py),
NOT the Venom monitor-tool handler. Reasons:

- TestRunner is infra, not a model-facing tool. It doesn't need the
  deny/allow policy gate or the JSON-serialized result envelope.
- TestRunner can run at boot or in L2 repair without a PolicyContext.
- Keeps the tool surface's responsibility narrow: observability for
  the model, nothing else.

So Slice 3 imports `background_monitor.BackgroundMonitor`, not
`monitor_tool.run_monitor_tool`.

## What stays out of scope

- Graduation flip of `JARVIS_TOOL_MONITOR_ENABLED` default → true.
  Deferred to Slice 4 after Slice 3 proves the primitive holds under
  TestRunner load + Slice 2 has operator battle-test hours at
  opt-in level.
- SerpentFlow live-dashboard surface for monitor events. Orthogonal
  to Slices 3/4.
- TrinityEventBus wiring in the Venom tool-executor. Policy-layer
  doesn't carry a bus ref today; adding one is its own slice and
  would bleed into the subagent / orchestrator surface.
