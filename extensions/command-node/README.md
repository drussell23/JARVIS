# JARVIS Sovereign Command Node (Phase 1 -- read-only)

A dynamic mission-control dashboard for the JARVIS Ouroboros governance
loop. **Phase 1 is READ-ONLY visualization** -- it consumes the existing
`/observability` SSE + GET surface and renders it. There is **no
write-path and no biometric** in this phase (the elevation "Authorize"
button is a disabled placeholder for Phase 2).

Stack: Next.js (App Router) + React + TypeScript + React Flow, with a
native-`fetch` SSE client mirrored from the `vscode-jarvis` extension's
proven parser + reconnect logic.

## What it shows

- **FSM ribbon** -- the 11-phase Ouroboros pipeline
  (`CLASSIFY -> ROUTE -> ... -> COMPLETE`), one live ribbon per active op,
  highlighting the current phase with provider / route / risk-tier
  badges (from `fsm_phase_changed`).
- **DAG canvas** -- the live execution graph, nodes colored by state
  (pending / running / applied / fractured / complete), fed by
  `dag_node_updated` + `task_*` events.
- **Blast-radius graph** -- the operator constraint, made visible. An
  interactive React Flow graph of
  `GET /observability/blast-radius/{op_id}`: the mutated symbol at the
  center, edges to every directly- and transitively-affected dependent,
  color-coded by repo (**Body/jarvis = blue, Mind/prime = amber,
  Nerves/reactor = violet**) so a cross-boundary blast is obvious. Click
  a node for a repo / file / symbol detail panel.
- **Elevation queue** -- read-only list of pending `CRITICAL_ELEVATION`
  PRs (from `cross_repo_elevation_pending`), each with a "view blast
  radius" action and a clearly-disabled "Authorize (Phase 2: biometric)"
  affordance.
- **Connection status + yield toasts** -- live SSE state and transient
  `sovereign_yield` alerts (FRACTURE / QUARANTINE / RECOVERED).

## Run

```bash
cd extensions/command-node
npm install
npm run dev          # http://localhost:3000
```

Point it at your running JARVIS observability backend (the
`EventChannelServer`) via env -- **there is no hardcoded localhost in
source**:

```bash
# .env.local
NEXT_PUBLIC_OBSERVABILITY_BASE=http://127.0.0.1:8765
```

If unset, the base defaults to
`http://127.0.0.1:${NEXT_PUBLIC_OBSERVABILITY_PORT:-8765}`.

### Tunables (all env, all optional)

| Env var | Default | Meaning |
| --- | --- | --- |
| `NEXT_PUBLIC_OBSERVABILITY_BASE` | `http://127.0.0.1:8765` | Backend base URL |
| `NEXT_PUBLIC_OBSERVABILITY_PORT` | `8765` | Used only to build the default base |
| `NEXT_PUBLIC_EVENT_BUFFER_CAP` | `500` | In-memory SSE ring-buffer size |
| `NEXT_PUBLIC_RECONNECT_MAX_BACKOFF_MS` | `15000` | Reconnect backoff ceiling |
| `NEXT_PUBLIC_POLL_INTERVAL_MS` | `5000` | Poll-fallback interval |
| `NEXT_PUBLIC_MAX_FAILURES_BEFORE_POLL` | `5` | Consecutive SSE failures before poll fallback |

## Test / typecheck

```bash
npm run test        # vitest (jsdom)
npm run typecheck   # tsc --noEmit (strict)
```

## Resilience

The SSE client (`hooks/useSovereignStream.ts` + `lib/stream.ts`) uses a
native-`fetch` `ReadableStream` reader (not the bare `EventSource`), so
it can send the `Last-Event-ID` header for server-side ring-buffer
replay on reconnect, with exponential backoff + full jitter. After
`MAX_FAILURES_BEFORE_POLL` consecutive failures it **degrades to a poll
fallback** (`GET /observability/tasks`) and automatically returns to SSE
when the stream recovers. The event buffer is bounded.

## Architecture / component tree

```
app/
  layout.tsx          # html shell + global CSS
  page.tsx            # 3-region mission-control layout, wires the hook
  globals.css
hooks/
  useSovereignStream.ts   # React SSE client (bounded buffer + poll fallback)
lib/
  stream.ts           # framework-agnostic SSE parser + reconnect (mirror of vscode)
  api.ts              # typed GET client (health/tasks/blast-radius)
  types.ts            # SSE discriminated union + GET response shapes
  config.ts           # env-driven config (no hardcoded localhost)
  projection.ts       # pure event-stream -> view-model transforms
  blastGraph.ts       # pure React Flow graph builder for the blast radius
  theme.ts            # repo color map (Body/Mind/Nerves) + DAG state colors
components/
  FSMStateStream.tsx  # 11-phase ribbons
  DAGCanvas.tsx       # live execution DAG (React Flow)
  BlastRadiusGraph.tsx# interactive blast-radius graph + detail panel
  ElevationQueue.tsx  # read-only elevation list (disabled authorize)
  ConnectionStatus.tsx
  YieldToasts.tsx
test/
  useSovereignStream.test.ts  # SSE parse + reconnect + Last-Event-ID + bounded buffer
  blastRadiusGraph.test.tsx   # repo-colored nodes + click -> detail
  projection.test.ts
```

## Scope (Phase 1)

Read-only. No POST/auth/biometric code anywhere. The dashboard is a pure
consumer of the observability surface; all governance authority stays in
the backend.
