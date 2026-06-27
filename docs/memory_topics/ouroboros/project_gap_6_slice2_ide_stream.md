---
title: Project Gap 6 Slice2 Ide Stream
modules: [backend/core/ouroboros/governance/ide_observability_stream.py, tests/governance/test_ide_observability_stream.py]
status: historical
source: project_gap_6_slice2_ide_stream.md
---

## What shipped (2026-04-20)

**Module**: `backend/core/ouroboros/governance/ide_observability_stream.py` (~480 lines)
**Tests**: `tests/governance/test_ide_observability_stream.py` — 41 tests, all green
**Wiring**: `EventChannelServer.start()` mounts `IDEStreamRouter` beside Slice 1 router when `JARVIS_IDE_STREAM_ENABLED=true` AND loopback assert passes.
**Integration**: `task_tool.py` publishes on every successful state transition; `close_task_board()` publishes `board_closed`.

## SSE over WebSocket — locked choice

Unidirectional transport means no covert command surface. Authority enforced by protocol, not discipline alone. Same `EventChannelServer` app, same loopback/CORS discipline as Slice 1.

## Event vocabulary (10 types, frozen)

- Task transitions: `task_created` / `task_started` / `task_updated` / `task_completed` / `task_cancelled`
- Board lifecycle: `board_closed`
- Control frames (bypass op_id filter): `heartbeat` / `stream_lag` / `replay_start` / `replay_end`

## Broker mechanics (`StreamEventBroker`)

- **Deny-by-default**: `JARVIS_IDE_STREAM_ENABLED` default `false`
- **Caps** (env-tunable): 8 subscribers × 64-slot queue × 512-event history × 15s heartbeat × 10/min subscribe rate
- **Drop-oldest backpressure**: slow clients drop events; first drop per lag-window emits single `stream_lag` control frame (suppressed via `_lag_pending` guard)
- **Last-Event-ID replay**: ring-buffer history + `replay_start` marker with `known=false` when ack ID evicted
- **Op_id filter**: `?op_id=X` query scopes stream; control frames bypass
- **Singleton**: `get_default_broker()` + `reset_default_broker()` for tests
- **Monotonic event_ids**: 12-hex zero-padded sequence per process lifetime
- **Schema stamping**: every frame carries `schema_version: "1.0"`
- **SSE wire format**: `id: <eid>\nevent: <etype>\ndata: <json>\n\n`

## HTTP handler (`IDEStreamRouter`) status codes

- 403: `ide_stream.disabled` (flag off) — port scanners see no signal
- 400: `ide_stream.malformed_op_id` (regex `^[A-Za-z0-9_\-]{1,128}$`)
- 429: `ide_stream.rate_limited` (sliding-window per client IP)
- 503: `ide_stream.capacity` (`Retry-After: 30`)
- 200: `text/event-stream` with `Cache-Control: no-store` + `X-Accel-Buffering: no`

## task_tool integration (`_publish_stream_event`)

Best-effort, never raises. Silently no-ops when flag off. Called after every successful `board.{create,start,complete,cancel,update}`. `close_task_board` publishes `board_closed` with `reason` payload. TaskBoard itself remains untouched — authority lives in the tool handler layer.

## Authority invariants (grep-enforced by tests)

- Never imports: orchestrator / policy_engine / iron_gate / risk_tier_floor / semantic_guardian / semantic_firewall / tool_executor (gate modules)
- Unidirectional SSE = no client input channel = no command surface
- TaskBoard primitive unmodified (publish hooks live in task_tool, not task_board)
- Reuses Slice 1 `assert_loopback_only()` + `_cors_origin_patterns()` — one allowlist story

## Why this closes Gap #6 Slice 2

Gap #6 originally called for SSE/WebSocket event stream. Slice 2 delivers SSE with broker + replay + backpressure + heartbeat, wired to TaskBoard via best-effort hooks. Next slices: Slice 3 (VS Code extension consuming Slice 1 GET + Slice 2 stream), Slice 4 (JetBrains extension or graduation of both flags).
