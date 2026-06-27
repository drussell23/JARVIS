---
title: Project Gap 6 Slice3 Vscode Extension
modules: [extensions/vscode-jarvis/src/extension.ts, config.ts, extensions/vscode-jarvis/src/logger.ts, types.ts, client.ts, stream.ts, extensions/vscode-jarvis/src/panel/renderers.ts, extensions/vscode-jarvis/src/panel/opDetailPanel.ts]
status: historical
source: project_gap_6_slice3_vscode_extension.md
---

## What shipped (2026-04-20)

**Path**: `extensions/vscode-jarvis/` — full VS Code extension scaffold, strict TypeScript, no heavy runtime deps.
**Tests**: 35/35 green under `node --test` (types 8 + client 7 + panel 9 + stream-parser 5 + stream-lifecycle 3 + stream-reconnect 3).
**Build**: `npm install && npm run compile` → `dist/extension.js`.

## Module layout

```
extensions/vscode-jarvis/
├── package.json + tsconfig.json + tsconfig.test.json + .vscodeignore + .gitignore
└── src/
    ├── extension.ts            — activate/deactivate wiring
    ├── config.ts               — typed settings accessors
    ├── logger.ts               — OutputChannel wrapper
    ├── api/
    │   ├── types.ts            — wire-type mirrors of server schema v1.0
    │   ├── client.ts           — fetch wrapper + ObservabilityError / SchemaMismatchError
    │   └── stream.ts           — SSE parser + StreamConsumer state machine
    ├── tree/opsProvider.ts     — TreeDataProvider with bounded LRU
    ├── panel/
    │   ├── renderers.ts        — pure HTML renderers (no vscode import)
    │   └── opDetailPanel.ts    — webview host
    └── status/statusBar.ts     — status bar item
```

## Key design decisions

- **SSE over WebSocket** — same rationale as Slice 2: unidirectional → no command surface.
- **Native `fetch` + `ReadableStream`** — zero SSE-polyfill deps. `TextDecoder` + `indexOf('\n\n')` parser.
- **AbortController everywhere** — every request cancelable. `signal.addEventListener('abort')` hooks `reader.cancel()` so `stop()` returns promptly even against an infinitely-open mocked stream.
- **Exponential backoff + full jitter** — `BASE_BACKOFF_MS=500`, capped at `reconnectMaxBackoffMs` (default 30s).
- **`Last-Event-ID` reconnect** — consumer tracks lastEventId per frame, header sent on reconnect; server-side replays via Slice 2's ring buffer.
- **`stream_lag` → hard refresh** — on lag control frame, `OpsTreeProvider.refresh()` re-fetches from GET endpoints.
- **Poll fallback** — `pollIntervalMs` tick re-fetches if status is error/reconnecting/disconnected.
- **Bounded LRU** — `maxOpsCached` (256 default) prevents unbounded memory in long sessions.
- **Schema validation** — every GET response + every SSE frame's `schema_version: "1.0"` check; mismatches surface as typed errors or silent frame drops (stream).
- **Strict TypeScript** — `strict: true` + `noImplicitAny` + `noImplicitOverride` + `noUnusedLocals` all on.

## Authority invariants

- Extension never issues a non-GET request to the agent. Mirror of server-side §1 Boundary.
- Op Detail webview: `enableScripts: false` + `localResourceRoots: []` + CSP meta `default-src 'none'; style-src 'unsafe-inline'` → zero XSS surface.
- All HTML rendering escapes `<>&"'` via `escapeHtml()`; tested via the `<script>alert(1)</script>` fixture.
- All input sanitized at boundaries: op_id regex `^[A-Za-z0-9_\-]{1,128}$` before any network call.

## Test harness

Node 18+ `node --test` on compiled JS under `dist-test/`. Test files intentionally split:
- `types.test.ts` — schema constants + discriminated-union helpers
- `client.test.ts` — HTTP paths, error taxonomy, schema mismatch
- `panel.test.ts` — pure HTML rendering, CSP, escaping
- `stream.parser.test.ts` — SSE wire format + chunk boundary + comment + schema filter
- `stream.lifecycle.test.ts` — state transitions, stop(), HTTP 403
- `stream.reconnect.test.ts` — backoff math, Last-Event-ID, op_id filter

Test gotcha (documented in `stream.reconnect.test.ts`): zero-time `sleepFn` must yield to the macrotask queue via `setImmediate` or `waitUntil`'s `setTimeout` starves. Without the yield, microtask chain is infinite.

## Extension commands + settings

Commands: `jarvisObservability.{connect,disconnect,refresh,showOp,showLog}`.
Settings: `jarvisObservability.{endpoint,enabled,autoReconnect,reconnectMaxBackoffMs,pollIntervalMs,opIdFilter,maxOpsCached}`.

## Why this closes Gap #6 Slice 3

Gap #6 originally called for VS Code + JetBrains IDE extensions. Slice 3 ships the VS Code one. Next slice (4) is either JetBrains (IntelliJ plugin) or graduation of the three env flags (`JARVIS_IDE_OBSERVABILITY_ENABLED`, `JARVIS_IDE_STREAM_ENABLED`) after a live-fire proof of the full stack.
