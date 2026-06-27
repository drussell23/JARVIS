---
title: Project Gap 6 Slice1 Ide Observability
modules: [backend/core/ouroboros/governance/ide_observability.py, tests/governance/test_ide_observability.py]
status: historical
source: project_gap_6_slice1_ide_observability.md
---

## What shipped (2026-04-20)

**Module**: `backend/core/ouroboros/governance/ide_observability.py` (~290 lines)
**Tests**: `tests/governance/test_ide_observability.py` — 31 tests, all green
**Wiring**: `EventChannelServer.start()` mounts `IDEObservabilityRouter` beside existing `/webhook/*` when `JARVIS_IDE_OBSERVABILITY_ENABLED=true` AND `assert_loopback_only(self._host)` passes.

## Routes (all GET, all read-only)

- `GET /observability/health` — schema_version + enabled:true
- `GET /observability/tasks` — list of op_ids with boards registered
- `GET /observability/tasks/{op_id}` — bounded projection: `{task_id, state, title, body, sequence, cancel_reason}`

## Non-negotiables enforced

- **Deny-by-default**: `JARVIS_IDE_OBSERVABILITY_ENABLED` default `false`. Disabled → 403 (not 200 w/ enabled:false — port scanners see no signal).
- **Loopback-only bind**: `assert_loopback_only()` accepts only `{127.0.0.1, ::1, localhost}`; rejects `0.0.0.0`, `::`, `*`, empty. Called from EventChannelServer.start() before router mount.
- **Rate limiting**: `JARVIS_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN` default 120, sliding-window per client IP.
- **CORS**: regex allowlist via `JARVIS_IDE_OBSERVABILITY_CORS_ORIGINS` (default `^http://(127\.0\.0\.1|localhost)(:\d+)?$`), echoes matched origin only — no `*`, no `Access-Control-Allow-Credentials`.
- **Every response**: `schema_version: "1.0"` + `Cache-Control: no-store`.
- **Op_id regex**: `^[A-Za-z0-9_\-]{1,128}$` — malformed → 400.
- **Authority invariant**: `test_ide_observability_does_not_import_gate_modules` — grep-enforced; observability never imports orchestrator/policy/iron_gate/risk_tier/gate modules.

## Why this closes Gap #6 Slice 1

Manifesto §1 (Boundary Principle): observability surface answers *"what is the loop doing right now?"* — never *"what should the loop do next?"*. No Venom tool, no Iron Gate branch, no risk-tier escalation can be triggered via this surface. The router sees live TaskBoard state from `_BOARDS` registry (Gap #5 primitive) but cannot mutate it.

Next slices: Slice 2 (SSE/WebSocket stream), Slice 3 (VS Code extension), Slice 4 (JetBrains / graduation).
