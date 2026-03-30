# Trinity Cloud Split — Design Specification

> **Date:** 2026-03-29
> **Status:** Draft — Pending final review
> **Scope:** Decompose JARVIS from a local monolith into a cloud-native hybrid: Vercel (Nervous System) + Doubleword/Claude APIs (Dual Brain) + Mac Thin Client (Body) + Apple Watch/iPhone (Edge Sensors)

---

## Table of Contents

1. [Guiding Principles](#1-guiding-principles)
2. [System Topology](#2-system-topology)
3. [API Contracts](#3-api-contracts)
4. [Authentication Model](#4-authentication-model)
5. [Vercel App Router Structure](#5-vercel-app-router-structure)
6. [Mac Thin Client (Brainstem)](#6-mac-thin-client-brainstem)
7. [Apple Watch & iPhone Clients](#7-apple-watch--iphone-clients)
8. [Constraints & Non-Goals](#8-constraints--non-goals)

---

## 1. Guiding Principles

### The Boundary Mandate (from Symbiotic Manifesto v3)

Deterministic code is the skeleton — fast, reliable, testable. Agentic intelligence is the nervous system — adaptive, creative, resilient. Each operates strictly in its domain of absolute strength.

### Dual-Brain Routing

- **Reflex Arc (Claude API):** Real-time streaming for voice interactions, conversation, quick tasks. Sub-second first-token latency.
- **Deep Cortex (Doubleword Batch API):** Asynchronous heavy cognition — Ouroboros governance scans (397B), deep visual analysis (235B), massive code generation. Minutes-to-hours latency, acceptable for async work.

### Critical Constraint

**`unified_supervisor.py` is NEVER deleted or modified for this project.** It remains the fully-local, offline entry point and the 102K-line architectural reference. The cloud split creates a NEW, SEPARATE entry point (`brainstem.py`). Shared hardware modules in `backend/` must maintain backward compatibility with both entry points.

---

## 2. System Topology

```
┌─────────────────────────────────────────────────────────────────┐
│                    EDGE NODES (Senses)                          │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐   │
│  │ Watch Ultra 2 │  │ iPhone 15 PM │  │ M1 Mac (Brainstem) │   │
│  │ Action Button │  │ Mobile Cmd   │  │ brainstem.py        │   │
│  │ On-device STT │  │ On-device STT│  │ Ghost Hands, Audio  │   │
│  │ Haptic output │  │ Full UI      │  │ Screen, HUD overlay │   │
│  └──────┬───────┘  └──────┬───────┘  └─────────┬──────────┘   │
│         │                  │                     │              │
│         │    Uniform JSON Command Payload        │              │
│         └──────────────────┼─────────────────────┘              │
└────────────────────────────┼────────────────────────────────────┘
                             │
                    HTTPS + SSE (TLS 1.3)
                             │
┌────────────────────────────▼────────────────────────────────────┐
│              VERCEL (The Nervous System)                         │
│                                                                  │
│  POST /api/command      ← Uniform intake from all nodes          │
│  GET  /api/stream/:id   ← Per-device SSE stream                  │
│  POST /api/doubleword/callback  ← Batch completion webhook       │
│  GET  /api/dashboard/*  ← Browser dashboard APIs                 │
│  POST /api/ouroboros/submit     ← Governance task submit         │
│                                                                  │
│  ┌─────────────────┐    ┌──────────────────────┐                │
│  │  Intent Router   │    │  Device Registry      │                │
│  │  (Tier 0 determ) │    │  (auth + fan-out map) │                │
│  └────────┬────────┘    └──────────────────────┘                │
│           │                                                      │
│     ┌─────┴──────┐                                               │
│     │             │                                               │
│  Reflex Arc   Deep Cortex                                        │
│  (real-time)  (async batch)                                      │
│                                                                  │
│  ┌───────────┐  ┌────────────────┐  ┌───────────────────────┐  │
│  │  Upstash   │  │ Vercel Queues  │  │  Next.js Dashboard    │  │
│  │  Redis     │  │ (durable)      │  │  (Browser UI)         │  │
│  │  Streams   │  │                │  │  Ouroboros PR review   │  │
│  │  (fan-out) │  │  ouroboros.*   │  │  Telemetry graphs     │  │
│  └───────────┘  │  doubleword.*  │  │  Command history      │  │
│                  └────────────────┘  └───────────────────────┘  │
└────────────┬─────────────┬──────────────────────────────────────┘
             │             │
      ┌──────▼──────┐  ┌──▼──────────────────┐
      │  Claude API  │  │  Doubleword API     │
      │  (streaming) │  │  (batch 4-stage)    │
      │              │  │                     │
      │  Real-time   │  │  397B: code/reason  │
      │  conversation│  │  235B: vision       │
      │  voice cmds  │  │                     │
      │  quick tasks │  │  Ouroboros scans    │
      └─────────────┘  │  Deep analysis      │
                        └─────────────────────┘
```

### Fan-Out Infrastructure

- **Upstash Redis Streams** — real-time fan-out for voice interactions. Vercel Functions write events via `XADD`; per-device SSE handlers read via `XRANGE` with 100ms internal polling. ~100-200ms cross-device latency.
- **Vercel Queues** — durable delivery for Ouroboros/Doubleword results. At-least-once delivery ensures no lost events even if Mac is offline.
- **APNs Push** — notification delivery to Watch/iPhone when apps are backgrounded. Used for `response_mode: "notify"` and urgent daemon events.

### Latency Budget

| Path | Latency | Mechanism |
|---|---|---|
| Watch → Claude → Watch (requester) | Sub-second | Direct SSE from POST response |
| Watch → Claude → Mac (fan-out) | ~100-200ms | Redis Stream XRANGE poll |
| Doubleword → all devices | ~100-500ms | Queue + Redis Stream write |
| APNs push (offline devices) | 1-3s | Apple push delivery |

### v2 Migration Path

Replace Redis Streams polling with Ably/Pusher channels for true sub-10ms cross-device fan-out. `publishToDevices` changes target; no other code changes needed.

---

## 3. API Contracts

### 3.1 — Uniform Command Payload (All Edge Nodes → Vercel)

Every node sends `POST /api/command` with the identical shape:

```typescript
interface CommandPayload {
  command_id: string;          // UUIDv4, generated on-device
  device_id: string;           // Persistent per-device, stored in Keychain
  device_type: "watch" | "iphone" | "mac" | "browser";
  text: string;                // Transcribed command (from on-device STT)
  intent_hint?: string;        // Optional — trusted allowlist fast-path
  context?: {
    active_app?: string;       // Mac: frontmost app name
    active_file?: string;      // Mac: current file path in editor
    screen_summary?: string;   // Mac: HUD's last screen description
    location?: string;         // Watch: coarse location (home/office/mobile)
    battery_level?: number;    // Watch: affects response verbosity
  };
  priority: "realtime" | "background" | "deferred";
  response_mode: "stream" | "notify";
  timestamp: string;           // ISO 8601
  signature: string;           // HMAC-SHA256(canonical, device_secret)
}
```

### 3.2 — Intent Router (Tier 0 Deterministic)

```typescript
interface RoutingDecision {
  brain: "claude" | "doubleword_397b" | "doubleword_235b";
  mode: "stream" | "batch";
  model: string;
  fan_out: DeviceTarget[];
  system_prompt_key: string;
  estimated_latency: "realtime" | "minutes" | "hours";
}

interface DeviceTarget {
  device_id: string;
  channel: "redis" | "queue";
  role: "executor" | "observer";  // Resolved from DeviceRecord
}
```

**Tier 0 routing rules** (deterministic, no model call):

```typescript
const TIER_0_ROUTES: RouteRule[] = [
  // Deep Cortex — Doubleword 397B (batch)
  { pattern: /^(run |start |execute )?ouroboros/i,    brain: "doubleword_397b", mode: "batch" },
  { pattern: /^(deep )?(scan|analyze|audit)/i,        brain: "doubleword_397b", mode: "batch" },
  { pattern: /^generate (code|implementation|PR)/i,   brain: "doubleword_397b", mode: "batch" },
  // Deep Cortex — Doubleword 235B vision (batch)
  { pattern: /^(what do you see|analyze screen|describe)/i, brain: "doubleword_235b", mode: "batch" },
  { pattern: /screenshot|screen capture|visual/i,           brain: "doubleword_235b", mode: "batch" },
];
// No match → Claude streaming (default). No classification cost.
```

**`intent_hint` semantics:** Trusted only if it matches a server-side allowlist. Trusted hints short-circuit regex (no pattern matching). Untrusted hints are ignored. The allowlist is: `ouroboros_scan`, `ouroboros_review`, `deep_analysis`, `vision_capture`, `code_generation`.

**v1 scope:** Tier 0 deterministic + default Claude only. Tier 1 (model-based classifier for ambiguous intents) is a v2 milestone.

### 3.3 — SSE Stream Format (Vercel → All Clients)

`GET /api/stream/:deviceId` — each device holds one SSE connection:

```typescript
type SSEEvent =
  | { event: "token";     data: TokenEvent }
  | { event: "action";    data: ActionEvent }
  | { event: "daemon";    data: DaemonEvent }
  | { event: "status";    data: StatusEvent }
  | { event: "complete";  data: CompleteEvent }
  | { event: "heartbeat"; data: {} }

interface TokenEvent {
  command_id: string;
  token: string;
  source_brain: "claude";
  sequence: number;            // Monotonic ordering
}

interface ActionEvent {
  command_id: string;
  action_type: "ghost_hands" | "file_edit" | "terminal" | "notification";
  payload: Record<string, unknown>;
  target_device: "mac";        // Only Mac executes actions
}

interface DaemonEvent {
  command_id: string;
  narration_text: string;
  narration_priority: "ambient" | "informational" | "urgent";
  source_brain: "claude" | "doubleword_397b" | "doubleword_235b";
}

interface StatusEvent {
  command_id: string;
  phase: string;
  progress?: number;
  message: string;
}

interface CompleteEvent {
  command_id: string;
  source_brain: "claude" | "doubleword_397b" | "doubleword_235b";
  token_count?: number;        // From Anthropic usage, not delta count
  latency_ms: number;
  artifacts?: {
    url: string;               // Signed URL with TTL
    type: "pr" | "diff" | "analysis" | "vision_description";
    expires_at: string;        // ISO 8601, typically +24h
  }[];
}
```

**POST `/api/command` response type:** For the reflex path (Claude streaming), the response content-type is `text/event-stream`, NOT JSON. Clients must handle both `text/event-stream` (stream) and `application/json` (batch queued). The requesting device receives tokens directly from the POST response. Other devices receive the same tokens via their SSE stream (Redis Streams fan-out).

### 3.4 — Doubleword Batch Callback

```typescript
// POST /api/doubleword/callback (webhook)
interface DoublewordCallback {
  job_id: string;
  status: "completed" | "failed";
  model: string;
  result?: {
    text: string;
    artifacts?: {
      type: "pr" | "diff" | "analysis" | "vision_description";
      content: string;
    }[];
  };
  error?: string;
  metrics?: {                  // Optional — may be partial on failure
    input_tokens: number;
    output_tokens: number;
    processing_time_ms: number;
  };
  signature: string;           // HMAC from DOUBLEWORD_WEBHOOK_SECRET
}
```

### 3.5 — Notify Delivery Cascade

For `response_mode: "notify"` when the device may not have an active SSE connection:

1. **SSE push** (if device has active connection)
2. **APNs push notification** (Watch/iPhone — always sent for "notify")
   - `priority: "background"` for ambient, `"alert"` for urgent
   - `collapse_id: command_id` — collapses duplicate pushes
3. **Redis storage** (`pending:{deviceId}:{commandId}`, TTL 24h) — client fetches on SSE reconnect via `Last-Event-ID` replay

### 3.6 — Idempotency

`command_id` (UUIDv4) is the idempotency key.

- **Batch commands:** `redis.SET cmd:{id} NX EX 3600` after auth. Duplicate returns cached result (200).
- **Streaming commands:** `redis.SET cmd:{id} NX EX 300` with `status: "in_flight"` before streaming. Duplicate returns `409 Command already in flight`.
- Idempotency check runs AFTER auth verification (prevents unauthenticated key pollution).

### 3.7 — Replay Protection

- Clock skew tolerance: 30 seconds
- Replay window: 300 seconds (5 minutes)
- Timestamps beyond the window are rejected (401)
- Combined with idempotency: duplicate `command_id` within the window is a no-op

### 3.8 — HMAC Canonicalization

```typescript
// Canonical fields (alphabetical, excluding 'signature'):
const CANONICAL_FIELDS = [
  "command_id", "device_id", "device_type",
  "priority", "response_mode", "text", "timestamp",
] as const;

// If intent_hint is present, it is included (between device_type and priority):
// "intent_hint={value}"

// context is serialized as sorted-key JSON, no whitespace:
// "context={...}"

// All strings UTF-8 encoded before HMAC.
// Format: "key1=val1&key2=val2&..."
```

Both client (Swift/Python) and server (TypeScript) must produce identical canonical strings. Test vectors should be included in the implementation.

### 3.9 — Fan-Out Event IDs

Event IDs use **ULID** (Universally Unique Lexicographically Sortable Identifier) to prevent collisions and support ordering. Format: `{commandId}:{ulid}`.

---

## 4. Authentication Model

### 4.1 — Trust Hierarchy

```
JARVIS_MASTER_SECRET (Vercel env vars — never leaves Vercel)
    │
    └─ HKDF-SHA256 derivation per device
         │
    ┌────┴────┬──────────┬──────────┐
    Mac       Watch      iPhone     Browser
    Keychain  Keychain   Keychain   Passkey + session
```

Single-user system (Derek only). Per-device secret derivation. Master secret never stored on devices.

### 4.2 — HKDF Specification (RFC 5869)

```
Extract:
  salt = UTF-8("jarvis-hkdf-salt-v1")        // Fixed application salt
  IKM  = JARVIS_MASTER_SECRET                 // Raw bytes, min 32 bytes

Expand:
  info = UTF-8("jarvis-device-v{version}:{device_id}")
         e.g., "jarvis-device-v1:watch-ultra2-derek"
  L    = 32 bytes (256-bit output)

device_secret = hex(OKM)  // 64-char hex string, stored in Keychain
```

Device secrets are **never stored on Vercel** — derived on demand from master secret + device_id + version. If Redis is compromised, no secrets are leaked.

### 4.3 — Device Registration (One-Time Pairing)

1. Derek initiates pairing from the dashboard (browser, authenticated)
2. Vercel generates an 8-character alphanumeric pairing code, stores in Redis (TTL 5 min)
3. Derek enters the code on the new device
4. Device sends `POST /api/devices/pair` with code + device_id
5. Vercel validates, derives secret via HKDF, returns `device_secret` once over TLS
6. Device stores secret in Keychain. Never transmitted again.

**Pairing abuse controls:**
- Max 3 attempts per code (then code is burned)
- Max 5 codes per hour from dashboard
- 8-char alphanumeric (not 6-digit — increased entropy)
- 2s cooldown after failed attempt
- Code bound to the dashboard session that created it

### 4.4 — Request Authentication Flow

```
1. Replay protection (timestamp, no Redis needed)
2. Device lookup (Redis: device:{id} → DeviceRecord)
3. HKDF derive expected secret (never stored)
4. Verify HMAC-SHA256 (timing-safe comparison)
5. Idempotency check (AFTER auth — prevents key pollution)
```

### 4.5 — SSE Stream Authentication

`EventSource` / `URLSession` don't support custom headers on SSE. Solution: opaque stream tokens.

1. Device requests token via authenticated `POST /api/stream/token`
2. Vercel stores `ssetok:{token} → device_id` in Redis (TTL 300s)
3. Device connects to `GET /api/stream/{deviceId}?t={token}`
4. Vercel validates + **consumes** token (atomic delete — single-use)
5. Device requests new token every 4 minutes (before 5-min expiry)
6. Each device maintains **exactly one** SSE connection. New connection sends `disconnect` event to old one.

No JWT. No secret material in URLs/logs. Opaque tokens only.

### 4.6 — Browser Authentication

WebAuthn passkey (Touch ID / Face ID). No passwords, no OAuth.

- `POST /api/auth/login` — WebAuthn challenge/verify
- Session stored in AES-256-GCM encrypted HTTP-only, Secure, SameSite=Strict cookie (7-day TTL)
- Browser registers as a device (`device_type: "browser"`, `role: "observer"`) on login
- Browser uses SSE via `GET /api/stream/{browserDeviceId}` — same mechanism as all devices

### 4.7 — Device Revocation

Set `device.active = false` in Redis. Next request → 401. Next SSE token refresh → denied. No secret rotation needed.

### 4.8 — Master Secret Rotation

`DeviceRecord` includes `hkdf_version`. Rotation procedure:

1. Set `JARVIS_MASTER_SECRET_V2` in Vercel env (keep V1 active)
2. Vercel tries device's `hkdf_version` first
3. Dashboard shows "re-pair required" for each device
4. As each device re-pairs, record updates to v2
5. Once all devices on v2, remove V1

Emergency: set all devices `active=false`, rotate master, re-pair all.

### 4.9 — Security Matrix

| Threat | Mitigation |
|---|---|
| Stolen device | Revoke from dashboard. Per-device secrets — others unaffected |
| Replayed command | 5-min timestamp window + command_id idempotency |
| MITM | TLS 1.3 on all connections. HMAC prevents payload tampering |
| Redis breach | No secrets stored — HKDF derives on demand |
| Vercel env leak | Single secret to rotate. All device secrets change automatically |
| Forged intent_hint | Server-side allowlist validation |
| SSE eavesdropping | Single-use opaque token over TLS |
| Browser session hijack | HTTP-only, Secure, SameSite=Strict, AES-256-GCM encrypted cookie |
| Pairing brute force | 8-char code, max 3 attempts, session-bound, rate-limited |
| Doubleword webhook forgery | Separate HMAC via DOUBLEWORD_WEBHOOK_SECRET |
| Cron endpoint abuse | CRON_SECRET verification on all scheduled routes |

---

## 5. Vercel App Router Structure

### 5.1 — Project Layout

```
jarvis-cloud/
├── app/
│   ├── layout.tsx                          # Root: dark theme, Geist Sans/Mono
│   ├── page.tsx                            # Redirect → /dashboard
│   ├── login/
│   │   └── page.tsx                        # WebAuthn login
│   ├── api/
│   │   ├── command/
│   │   │   └── route.ts                    # POST — unified command intake
│   │   ├── stream/
│   │   │   ├── [deviceId]/
│   │   │   │   └── route.ts               # GET — per-device SSE stream
│   │   │   └── token/
│   │   │       └── route.ts               # POST — issue opaque stream token
│   │   ├── devices/
│   │   │   ├── route.ts                    # GET — list all devices
│   │   │   ├── pair/
│   │   │   │   └── route.ts               # POST — pairing flow
│   │   │   ├── health/
│   │   │   │   └── route.ts               # GET — prune stale (cron target)
│   │   │   └── [deviceId]/
│   │   │       └── revoke/
│   │   │           └── route.ts            # POST — revoke device
│   │   ├── doubleword/
│   │   │   ├── submit/
│   │   │   │   └── route.ts               # POST — submit batch job
│   │   │   └── callback/
│   │   │       └── route.ts               # POST — webhook from Doubleword
│   │   ├── ouroboros/
│   │   │   ├── submit/
│   │   │   │   └── route.ts               # POST — governance scan trigger
│   │   │   └── [jobId]/
│   │   │       └── route.ts               # GET — job status + results
│   │   ├── state/
│   │   │   └── [deviceId]/
│   │   │       └── route.ts               # GET — full state sync on reconnect
│   │   └── auth/
│   │       ├── login/
│   │       │   └── route.ts               # POST — WebAuthn challenge/verify
│   │       └── session/
│   │           └── route.ts               # GET — check / DELETE — logout
│   └── dashboard/
│       ├── layout.tsx                      # Sidebar nav, device status bar
│       ├── page.tsx                        # Overview: live command feed
│       ├── ouroboros/
│       │   ├── page.tsx                    # PR review queue
│       │   └── [jobId]/
│       │       └── page.tsx               # Diff viewer + approve/reject
│       ├── devices/
│       │   └── page.tsx                    # Device registry + pairing UI
│       └── telemetry/
│           └── page.tsx                    # Live event log + metrics
├── lib/
│   ├── auth/
│   │   ├── hmac.ts                         # canonicalize() + verifyHMAC()
│   │   ├── hkdf.ts                         # deriveDeviceSecret() — RFC 5869
│   │   ├── pairing.ts                      # generateCode() + validatePair()
│   │   ├── session.ts                      # WebAuthn + cookie session
│   │   ├── stream-token.ts                 # issueStreamToken() + validate()
│   │   └── cron.ts                         # verifyCron() — CRON_SECRET
│   ├── routing/
│   │   ├── intent-router.ts                # resolveRoute() — Tier 0
│   │   └── types.ts                        # All shared TypeScript interfaces
│   ├── brains/
│   │   ├── claude.ts                       # streamClaude() — SSE pipe
│   │   ├── doubleword.ts                   # submitBatch() — 4-stage
│   │   └── fan-out.ts                      # publishToDevices() — Redis Streams + Queue
│   ├── redis/
│   │   ├── client.ts                       # Upstash Redis singleton
│   │   └── event-backlog.ts                # Redis Stream helpers (XADD/XRANGE/XTRIM)
│   ├── sse/
│   │   ├── encoder.ts                      # formatSSE() — event: data:\n\n
│   │   └── types.ts                        # All SSE event interfaces
│   └── queue/
│       └── topics.ts                       # Queue topic definitions + handlers
├── proxy.ts                                # Next.js 16 — dashboard session gate only
├── next.config.ts
├── vercel.ts                               # Cron jobs config
├── package.json
└── tsconfig.json
```

### 5.2 — Redis Key Schema (Canonical)

All keys use JSON blobs via `redis.set`/`redis.get`. No `hset` anywhere.

```
device:{device_id}               → DeviceRecord (JSON)        TTL: none
cmd:{command_id}                 → CommandResult (JSON)        TTL: 3600s
jobmeta:{job_id}                 → JobMetadata (JSON)          TTL: 86400s
job:{job_id}                     → DoublewordCallback (JSON)   TTL: 86400s
ssetok:{token}                   → device_id (string)          TTL: 300s
pairing:{code}                   → PairingSession (JSON)       TTL: 300s
stream:events:{device_id}        → Redis Stream (XADD)         MAXLEN: 100
```

### 5.3 — SSE Handler (Redis Streams, Serverless-Compatible)

Per-device SSE handlers use `XRANGE` polling inside the Vercel Function (100ms interval). The client sees a clean SSE stream; the internal polling is invisible. Functions run up to 300s on Pro plan, then client reconnects with `Last-Event-ID`.

SSE resume is **best-effort within a 10-minute window**. Events older than 10 minutes or beyond the 100-event Redis Stream buffer are lost — client must request full state sync via `GET /api/state/{deviceId}`.

### 5.4 — Cron Jobs

```typescript
// vercel.ts
crons: [
  { path: "/api/ouroboros/submit", schedule: "0 3 * * *" },  // Nightly governance
  { path: "/api/devices/health",  schedule: "0 */6 * * *" }, // Prune stale devices
]
```

All cron targets verify `Authorization: Bearer {CRON_SECRET}` before processing.

### 5.5 — proxy.ts

Matches `/dashboard/:path*` only. Validates AES-256-GCM encrypted session cookie. API routes handle their own auth (HMAC/webhook/cron). Does not match `/api/*`.

---

## 6. Mac Thin Client (Brainstem)

### 6.1 — The Split

```
unified_supervisor.py (102K) → PRESERVED AS-IS (offline mode + reference)

brainstem.py (~2K new) → NEW entry point
  Imports from backend/:
    audio/audio_bus.py           — mic/speaker hardware
    voice/safe_say.py            — TTS via afplay
    voice/streaming_stt.py       — on-device STT
    ghost_hands/*                — AX APIs, yabai, clicks
    vision/realtime/frame_pipeline.py — screen capture (SHM)

  New modules:
    brainstem/sse_consumer.py    — Vercel SSE stream listener (~200 lines)
    brainstem/action_dispatcher.py — Event → local execution (~300 lines)
    brainstem/command_sender.py  — Mac → Vercel POST (~150 lines)
    brainstem/voice_intake.py    — STT → command_sender bridge
    brainstem/hud.py             — transparent overlay window
    brainstem/auth.py            — HMAC signing, token refresh
```

### 6.2 — Boot Sequence

```
T+0.0s  Load env vars, create DeviceAuth
T+0.1s  Create CommandSender
T+0.3s  Hardware init:
          ├─ AudioBus.start()        ~1.5s
          ├─ GhostHands.initialize() ~0.5s
          └─ HUD overlay create      ~0.3s
T+2.5s  Request stream token from Vercel
T+3.0s  Connect SSE stream
T+3.5s  Start voice intake
T+3.5s  "JARVIS Online" ✅

Total: ~3.5 seconds
```

No model loading, no Trinity phase, no 7-phase boot DAG, no native preload races.

### 6.3 — SSE Consumer

Connects to `GET /api/stream/{deviceId}?t={token}`. Handles:

- **Reconnection** with exponential backoff (1s → 2s → 4s → ... → 30s max)
- **Last-Event-ID** replay on reconnect
- **Token refresh** every 4 minutes (before 5-min expiry)
- **Event dispatch** to ActionDispatcher

Uses `aiohttp` for HTTP streaming. Parses SSE protocol (id/event/data fields, double-newline boundaries).

### 6.4 — Action Dispatcher

Routes SSE events to local hardware:

| Event | Handler |
|---|---|
| `token` | HUD overlay — display streaming text |
| `action` (ghost_hands) | Click, type, scroll via AXUIElement |
| `action` (file_edit) | Apply diff or write file locally |
| `action` (terminal) | Run shell command (with safety blocklist) |
| `action` (notification) | macOS system notification via osascript |
| `daemon` (ambient) | HUD text only, no voice |
| `daemon` (informational) | HUD + safe_say() voice |
| `daemon` (urgent) | HUD + safe_say() + system notification |
| `status` | HUD progress indicator |
| `complete` | HUD clear active command state |

### 6.5 — Command Sender

Signs and sends commands to `POST /api/command`. Gathers local context automatically (frontmost app, active file). Handles both response types:

- `text/event-stream` → tokens arrive on SSE consumer, POST response discarded to avoid duplicates
- `application/json` → batch job queued, return job_id

### 6.6 — Vision Bridge (60fps VLA Loop)

The real-time vision pipeline **stays entirely on the brainstem** and **bypasses Vercel**. Adding a Vercel hop to the vision loop would add latency for zero benefit — frames are captured locally and actions execute locally.

**Data flow:**
1. **FramePipeline** (60fps SHM capture via SCK) — local, hardware-bound
2. **L1 Scene Graph Cache** (KnowledgeFabric) — local in-memory, ~5ms, TTL 5s
3. **Doubleword VL-235B** (`/chat/completions` sync endpoint) — direct API call from Mac, ~1-3s
4. **Claude Vision API** (fallback) — direct API call from Mac, ~5-15s
5. **Ghost Hands** executes the action — local AX APIs

**On-demand activation:** The FramePipeline does NOT start at boot. It lazy-starts when:
- A `vision_task` action arrives via SSE from Vercel
- `JARVIS_VISION_LOOP_ENABLED=true` is set
- The user explicitly requests vision ("what do you see" → Vercel routes to batch Doubleword, but also triggers local capture for the response context)

**JarvisCU** (Computer Use orchestrator) stays local: planning via Claude Vision, per-step execution via the 3-layer cascade (Accessibility API → Doubleword VL-235B → Claude Vision), verification via dhash frame comparison.

**Only ad-hoc text commands go through Vercel:** "analyze this screenshot" → Vercel intent router → Doubleword 235B batch. The continuous VLA loop never touches Vercel.

### 6.7 — Eliminated from Mac

| Component | Reason |
|---|---|
| PrimeClient, PrimeRouter | Cognitive routing → Vercel |
| Model serving (UnifiedModelServing) | Inference → Claude/Doubleword APIs |
| Ouroboros governance pipeline | → Vercel + Doubleword |
| Trinity integration | → eliminated (cloud-native) |
| AGI OS, Neural Mesh | → Vercel API routes |
| DMS, Startup Watchdog | → eliminated (3.5s boot) |
| Progressive Readiness | → eliminated (instant boot) |
| PlatformMemoryMonitor | → eliminated (no local inference) |
| Native preload orchestration | → eliminated (no local ML imports) |
| 7-phase boot DAG | → 4-step linear boot |

---

## 7. Apple Watch & iPhone Clients

### 7.1 — Shared Swift Package (JARVISKit)

```
JARVISKit/Sources/JARVISKit/
├── Auth/
│   ├── DeviceAuth.swift         # HMAC signing (NOT HKDF — server derives)
│   ├── KeychainStore.swift       # Secure device_secret + device_id storage
│   └── StreamToken.swift         # Opaque token request/refresh
├── Networking/
│   ├── CommandSender.swift       # POST /api/command (signed)
│   ├── SSEClient.swift           # URLSession SSE consumer
│   └── APITypes.swift            # CommandPayload, SSE event types
├── Voice/
│   └── SpeechTranscriber.swift   # Apple Speech framework wrapper
└── Models/
    ├── JARVISEvent.swift          # Parsed SSE events
    └── DeviceConfig.swift         # Endpoints, device ID, type
```

**SSEClient notes:**
- Uses `URLSessionDataDelegate` for streaming
- Buffers on `\n\n`, parses `id:`/`event:`/`data:` fields
- Persists `Last-Event-ID` for reconnect replay
- Must call `finishTasksAndInvalidate()` on old session before creating new one
- Must implement `urlSession(_:task:didCompleteWithError:)` for disconnect detection
- All buffer/state mutation on single queue to prevent races

**Canonical HMAC in Swift:** Must produce identical canonical strings as TypeScript server. Include `intent_hint` in canonical when present. Test vectors shared across implementations.

### 7.2 — Watch Ultra 2

**Action Button integration:** Use **App Intents / Shortcuts** or **scene/user-activity** handler — NOT `applicationDidBecomeActive` (which fires for many reasons). The exact WatchKit entry point depends on the OS version and Action Button configuration.

**Voice flow:**
1. Action Button → haptic (`.click`)
2. Start on-device STT (Apple Speech framework)
3. Partial transcript → UI update
4. Final transcript → haptic (`.success`) → `POST /api/command`
5. Response tokens arrive on SSE stream → display on Watch
6. DaemonEvent → haptic based on priority

**Pairing:** Must handle "not paired" state gracefully (show pairing UI, not crash). Both `device_id` and `device_secret` stored in Keychain (not UserDefaults).

**Battery monitoring:** `WKInterfaceDevice.current().batteryLevel` requires `isBatteryMonitoringEnabled = true`.

**Location:** Requires Core Location permission (privacy string in Info.plist). Use reduced accuracy for coarse "home/office/mobile" mapping.

**watchOS SSE constraints:** Long-lived networking is limited. Align with WKApplication background modes and extended runtime sessions. Expect frequent disconnects (dock, low power mode). APNs is the reliable path for offline delivery.

### 7.3 — iPhone 15 Pro Max

**Tabs:** Command Center, Ouroboros (PR review queue + diff viewer), Devices, Settings.

**Push notifications:**
- `userNotificationCenter(_:willPresent:)` — foreground presentation
- `userNotificationCenter(_:didReceive:)` — user interaction (deep link)
- `application(_:didReceiveRemoteNotification:)` — silent/background push
- Requires Push Notification capability + correct APNs payload format
- Deep links: `jarvis://ouroboros/{jobId}`, `jarvis://daemon/urgent`

**Background:** `BGTaskScheduler` for periodic queue drain. APNs is the primary delivery path for async results when app is backgrounded.

### 7.4 — Device Interaction Matrix

```
              │ Send Cmd │ Recv Token │ Recv Action │ Recv Daemon │ Recv Push │
──────────────┼──────────┼────────────┼─────────────┼─────────────┼───────────│
Watch Ultra 2 │    ✅    │     ✅     │      ─      │   ✅ haptic │    ✅     │
iPhone 15 PM  │    ✅    │     ✅     │      ─      │   ✅ alert  │    ✅     │
Mac M1        │    ✅    │   ✅ HUD   │   ✅ exec   │   ✅ voice  │     ─     │
Browser       │    ✅    │   ✅ UI    │   ✅ read   │   ✅ toast  │     ─     │
```

Watch/iPhone: observe + command. Mac: execute. Browser: observe (read-only for actions — shows log entry, never executes).

### 7.5 — Pairing Flow

1. Dashboard → "Pair New Device" → Vercel generates 8-char alphanumeric code (TTL 5 min)
2. Derek enters code on device
3. Device sends `POST /api/devices/pair` with code + generated device_id
4. Vercel validates → HKDF derives secret → returns `device_secret` + endpoints over TLS
5. Device stores in Keychain
6. Device connects to SSE
7. Dashboard shows "Device paired ✅"

---

## 8. Constraints & Non-Goals

### Hard Constraints

- `unified_supervisor.py` is never deleted or modified
- Shared `backend/` modules must maintain backward compatibility with both entry points
- Single-user system (Derek only) — no multi-tenant auth
- On-device STT on Watch/iPhone — no cloud STT
- Doubleword is batch-only — no streaming from 397B/235B

### Non-Goals for v1

- Tier 1 model-based intent classification
- Sub-10ms cross-device fan-out (v2: Ably/Pusher)
- GCP VM integration (currently disabled)
- Multi-user support
- Watch standalone mode without cellular

### Dependencies

- **Vercel Pro plan** — 300s function timeout for SSE handlers
- **Upstash Redis** (via Vercel Marketplace) — Streams + key-value
- **Vercel Queues** — durable async delivery
- **Anthropic Claude API** — streaming inference
- **Doubleword API** — batch inference (397B + 235B)
- **Apple Developer Account** — Watch/iPhone distribution + APNs
- **Apple Speech Framework** — on-device STT

### New Repos / Projects

| Project | Language | Purpose |
|---|---|---|
| `jarvis-cloud` | TypeScript (Next.js) | Vercel app — nervous system |
| `jarvis-brainstem` | Python | Mac thin client — subdirectory of JARVIS-AI-Agent (`brainstem/`) to share `backend/` modules without duplication |
| `JARVISKit` | Swift | Shared Apple client package |
| `JARVISWatch` | Swift | watchOS app |
| `JARVISPhone` | Swift | iOS app |
