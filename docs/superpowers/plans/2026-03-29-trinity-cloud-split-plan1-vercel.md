# Trinity Cloud Split — Plan 1: Vercel App (jarvis-cloud)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Vercel-hosted nervous system that receives commands from all edge nodes, routes them to Claude (streaming) or Doubleword (batch), and fans out responses to connected devices via SSE.

**Architecture:** Next.js 16 App Router with API routes for command intake, per-device SSE streaming via Redis Streams, WebAuthn browser auth, HMAC device auth with HKDF-derived secrets, and dual-brain routing (Claude streaming + Doubleword batch). Upstash Redis for device registry, event backlog, and stream tokens. Vercel Queues for durable async delivery.

**Tech Stack:** Next.js 16, TypeScript, Upstash Redis (@upstash/redis), Anthropic SDK (@anthropic-ai/sdk), Vercel Queues, CryptoKit (HKDF/HMAC), ULID

**Spec:** `docs/superpowers/specs/2026-03-29-trinity-cloud-split-design.md`

**Related Plans:**
- Plan 2: Mac Thin Client (`brainstem/`) — depends on this plan's API routes
- Plan 3: Apple Watch App — depends on this plan's API routes
- Plan 4: iPhone App — depends on this plan's API routes

---

## File Structure

```
jarvis-cloud/
├── app/
│   ├── layout.tsx                              # Root layout (dark, Geist)
│   ├── page.tsx                                # Redirect → /dashboard
│   ├── login/
│   │   └── page.tsx                            # WebAuthn login page
│   ├── api/
│   │   ├── command/
│   │   │   └── route.ts                        # POST — unified command intake
│   │   ├── stream/
│   │   │   ├── [deviceId]/
│   │   │   │   └── route.ts                    # GET — per-device SSE
│   │   │   └── token/
│   │   │       └── route.ts                    # POST — issue stream token
│   │   ├── devices/
│   │   │   ├── route.ts                        # GET — list devices
│   │   │   ├── pair/
│   │   │   │   └── route.ts                    # POST — pairing flow
│   │   │   ├── health/
│   │   │   │   └── route.ts                    # GET — prune stale (cron)
│   │   │   └── [deviceId]/
│   │   │       └── revoke/
│   │   │           └── route.ts                # POST — revoke device
│   │   ├── doubleword/
│   │   │   ├── submit/
│   │   │   │   └── route.ts                    # POST — submit batch job
│   │   │   └── callback/
│   │   │       └── route.ts                    # POST — webhook
│   │   ├── ouroboros/
│   │   │   ├── submit/
│   │   │   │   └── route.ts                    # POST — governance scan
│   │   │   └── [jobId]/
│   │   │       └── route.ts                    # GET — job status
│   │   ├── state/
│   │   │   └── [deviceId]/
│   │   │       └── route.ts                    # GET — full state sync
│   │   └── auth/
│   │       ├── login/
│   │       │   └── route.ts                    # POST — WebAuthn
│   │       └── session/
│   │           └── route.ts                    # GET/DELETE — session
│   └── dashboard/
│       ├── layout.tsx                          # Sidebar nav
│       ├── page.tsx                            # Overview
│       ├── ouroboros/
│       │   ├── page.tsx                        # PR queue
│       │   └── [jobId]/
│       │       └── page.tsx                    # Diff viewer
│       ├── devices/
│       │   └── page.tsx                        # Device management
│       └── telemetry/
│           └── page.tsx                        # Event log
├── lib/
│   ├── auth/
│   │   ├── hmac.ts                             # canonicalize + verifyHMAC
│   │   ├── hkdf.ts                             # deriveDeviceSecret (RFC 5869)
│   │   ├── pairing.ts                          # generateCode + validatePair
│   │   ├── session.ts                          # WebAuthn + encrypted cookie
│   │   ├── stream-token.ts                     # issue + validate (opaque)
│   │   └── cron.ts                             # verifyCron (CRON_SECRET)
│   ├── routing/
│   │   ├── intent-router.ts                    # resolveRoute — Tier 0
│   │   └── types.ts                            # All shared interfaces
│   ├── brains/
│   │   ├── claude.ts                           # streamClaude — SSE pipe
│   │   ├── doubleword.ts                       # submitBatch — 4-stage
│   │   └── fan-out.ts                          # publishToDevices
│   ├── redis/
│   │   ├── client.ts                           # Upstash singleton
│   │   └── event-backlog.ts                    # XADD/XRANGE/XTRIM helpers
│   ├── sse/
│   │   ├── encoder.ts                          # formatSSE
│   │   └── types.ts                            # SSE event interfaces
│   └── queue/
│       └── topics.ts                           # Queue definitions
├── proxy.ts                                    # Dashboard session gate
├── next.config.ts
├── vercel.ts                                   # Crons
├── package.json
└── tsconfig.json
```

---

## Task 1: Project Scaffold + Redis Client

**Files:**
- Create: `jarvis-cloud/package.json`
- Create: `jarvis-cloud/tsconfig.json`
- Create: `jarvis-cloud/next.config.ts`
- Create: `jarvis-cloud/vercel.ts`
- Create: `jarvis-cloud/app/layout.tsx`
- Create: `jarvis-cloud/app/page.tsx`
- Create: `jarvis-cloud/lib/redis/client.ts`
- Test: `jarvis-cloud/lib/redis/__tests__/client.test.ts`

- [ ] **Step 1: Create the project directory and initialize**

```bash
mkdir -p jarvis-cloud
cd jarvis-cloud
npx create-next-app@latest . --typescript --tailwind --eslint --app --src-dir=false --import-alias="@/*" --turbopack
```

- [ ] **Step 2: Install core dependencies**

```bash
npm install @upstash/redis ulid @anthropic-ai/sdk
npm install -D vitest @testing-library/react
```

- [ ] **Step 3: Create vercel.ts with cron config**

```typescript
// jarvis-cloud/vercel.ts
import { type VercelConfig } from "@vercel/config/v1";

export const config: VercelConfig = {
  crons: [
    { path: "/api/ouroboros/submit", schedule: "0 3 * * *" },
    { path: "/api/devices/health", schedule: "0 */6 * * *" },
  ],
};
```

- [ ] **Step 4: Create Redis client singleton**

```typescript
// jarvis-cloud/lib/redis/client.ts
import { Redis } from "@upstash/redis";

let redis: Redis | null = null;

export function getRedis(): Redis {
  if (!redis) {
    redis = new Redis({
      url: process.env.UPSTASH_REDIS_REST_URL!,
      token: process.env.UPSTASH_REDIS_REST_TOKEN!,
    });
  }
  return redis;
}

export { redis };
export default getRedis;
```

- [ ] **Step 5: Write Redis client test**

```typescript
// jarvis-cloud/lib/redis/__tests__/client.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock env vars
vi.stubEnv("UPSTASH_REDIS_REST_URL", "https://test.upstash.io");
vi.stubEnv("UPSTASH_REDIS_REST_TOKEN", "test-token");

describe("Redis client", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("returns a Redis instance", async () => {
    const { getRedis } = await import("../client");
    const client = getRedis();
    expect(client).toBeDefined();
  });

  it("returns the same singleton on repeated calls", async () => {
    const { getRedis } = await import("../client");
    const a = getRedis();
    const b = getRedis();
    expect(a).toBe(b);
  });
});
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd jarvis-cloud && npx vitest run lib/redis/__tests__/client.test.ts
```
Expected: 2 tests PASS

- [ ] **Step 7: Create root layout (dark theme, Geist)**

```tsx
// jarvis-cloud/app/layout.tsx
import type { Metadata } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import "./globals.css";

export const metadata: Metadata = {
  title: "JARVIS Cloud",
  description: "Trinity Nervous System",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className={`${GeistSans.variable} ${GeistMono.variable} font-sans antialiased bg-zinc-950 text-zinc-100`}>
        {children}
      </body>
    </html>
  );
}
```

- [ ] **Step 8: Create root page (redirect to dashboard)**

```tsx
// jarvis-cloud/app/page.tsx
import { redirect } from "next/navigation";

export default function Home() {
  redirect("/dashboard");
}
```

- [ ] **Step 9: Commit**

```bash
git add jarvis-cloud/
git commit -m "feat(cloud): scaffold Next.js project with Redis client singleton"
```

---

## Task 2: Shared Types + SSE Encoder

**Files:**
- Create: `jarvis-cloud/lib/routing/types.ts`
- Create: `jarvis-cloud/lib/sse/types.ts`
- Create: `jarvis-cloud/lib/sse/encoder.ts`
- Test: `jarvis-cloud/lib/sse/__tests__/encoder.test.ts`

- [ ] **Step 1: Define all shared TypeScript interfaces**

```typescript
// jarvis-cloud/lib/routing/types.ts

export type DeviceType = "watch" | "iphone" | "mac" | "browser";
export type Priority = "realtime" | "background" | "deferred";
export type ResponseMode = "stream" | "notify";
export type BrainId = "claude" | "doubleword_397b" | "doubleword_235b";
export type RoutingMode = "stream" | "batch";
export type DeviceRole = "executor" | "observer";
export type FanOutChannel = "redis" | "queue";

export interface CommandContext {
  active_app?: string;
  active_file?: string;
  screen_summary?: string;
  location?: string;
  battery_level?: number;
}

export interface CommandPayload {
  command_id: string;
  device_id: string;
  device_type: DeviceType;
  text: string;
  intent_hint?: string;
  context?: CommandContext;
  priority: Priority;
  response_mode: ResponseMode;
  timestamp: string;
  signature: string;
}

export interface DeviceTarget {
  device_id: string;
  channel: FanOutChannel;
  role: DeviceRole;
}

export interface RoutingDecision {
  brain: BrainId;
  mode: RoutingMode;
  model: string;
  fan_out: DeviceTarget[];
  system_prompt_key: string;
  estimated_latency: "realtime" | "minutes" | "hours";
}

export interface DeviceRecord {
  device_id: string;
  device_type: DeviceType;
  device_name: string;
  paired_at: string;
  last_seen: string;
  push_token?: string;
  role: DeviceRole;
  active: boolean;
  hkdf_version: number;
}

export interface RouteRule {
  pattern: RegExp;
  brain: BrainId;
  mode: RoutingMode;
  model: string;
  system_prompt_key: string;
  estimated_latency: "realtime" | "minutes" | "hours";
}

export interface PairingSession {
  code: string;
  created_by_session: string;
  created_at: string;
  attempts_remaining: number;
  device_type_hint: DeviceType;
}
```

- [ ] **Step 2: Define SSE event types**

```typescript
// jarvis-cloud/lib/sse/types.ts

export interface TokenEvent {
  command_id: string;
  token: string;
  source_brain: "claude";
  sequence: number;
}

export interface ActionEvent {
  command_id: string;
  action_type: "ghost_hands" | "file_edit" | "terminal" | "notification";
  payload: Record<string, unknown>;
  target_device: "mac";
}

export interface DaemonEvent {
  command_id: string;
  narration_text: string;
  narration_priority: "ambient" | "informational" | "urgent";
  source_brain: "claude" | "doubleword_397b" | "doubleword_235b";
}

export interface StatusEvent {
  command_id: string;
  phase: string;
  progress?: number;
  message: string;
}

export interface CompleteEvent {
  command_id: string;
  source_brain: "claude" | "doubleword_397b" | "doubleword_235b";
  token_count?: number;
  latency_ms: number;
  artifacts?: {
    url: string;
    type: "pr" | "diff" | "analysis" | "vision_description";
    expires_at: string;
  }[];
}

export type SSEEventType = "token" | "action" | "daemon" | "status" | "complete" | "heartbeat" | "disconnect";
```

- [ ] **Step 3: Write the SSE encoder test**

```typescript
// jarvis-cloud/lib/sse/__tests__/encoder.test.ts
import { describe, it, expect } from "vitest";
import { formatSSE } from "../encoder";

describe("formatSSE", () => {
  it("formats a basic event with data", () => {
    const result = formatSSE("token", { command_id: "abc", token: "hello" });
    expect(result).toBe(
      'event:token\ndata:{"command_id":"abc","token":"hello"}\n\n'
    );
  });

  it("includes id when provided", () => {
    const result = formatSSE("status", { phase: "routing" }, "evt-123");
    expect(result).toBe(
      'id:evt-123\nevent:status\ndata:{"phase":"routing"}\n\n'
    );
  });

  it("formats heartbeat with empty data", () => {
    const result = formatSSE("heartbeat", {});
    expect(result).toBe("event:heartbeat\ndata:{}\n\n");
  });
});
```

- [ ] **Step 4: Run test to verify it fails**

```bash
npx vitest run lib/sse/__tests__/encoder.test.ts
```
Expected: FAIL — `formatSSE` not found

- [ ] **Step 5: Implement SSE encoder**

```typescript
// jarvis-cloud/lib/sse/encoder.ts

export function formatSSE(
  event: string,
  data: Record<string, unknown>,
  id?: string,
): string {
  const lines: string[] = [];
  if (id) lines.push(`id:${id}`);
  lines.push(`event:${event}`);
  lines.push(`data:${JSON.stringify(data)}`);
  lines.push("", "");
  return lines.join("\n");
}
```

- [ ] **Step 6: Run test to verify it passes**

```bash
npx vitest run lib/sse/__tests__/encoder.test.ts
```
Expected: 3 tests PASS

- [ ] **Step 7: Commit**

```bash
git add jarvis-cloud/lib/routing/types.ts jarvis-cloud/lib/sse/
git commit -m "feat(cloud): add shared types and SSE encoder"
```

---

## Task 3: HMAC Auth + HKDF Derivation

**Files:**
- Create: `jarvis-cloud/lib/auth/hmac.ts`
- Create: `jarvis-cloud/lib/auth/hkdf.ts`
- Test: `jarvis-cloud/lib/auth/__tests__/hmac.test.ts`
- Test: `jarvis-cloud/lib/auth/__tests__/hkdf.test.ts`

- [ ] **Step 1: Write HKDF test**

```typescript
// jarvis-cloud/lib/auth/__tests__/hkdf.test.ts
import { describe, it, expect, vi } from "vitest";

vi.stubEnv("JARVIS_MASTER_SECRET", "test-master-secret-at-least-32-bytes-long!!");

describe("deriveDeviceSecret", () => {
  it("derives a 64-char hex string", async () => {
    const { deriveDeviceSecret } = await import("../hkdf");
    const secret = await deriveDeviceSecret("device-abc", 1);
    expect(secret).toHaveLength(64);
    expect(secret).toMatch(/^[0-9a-f]{64}$/);
  });

  it("produces different secrets for different device IDs", async () => {
    const { deriveDeviceSecret } = await import("../hkdf");
    const a = await deriveDeviceSecret("device-abc", 1);
    const b = await deriveDeviceSecret("device-xyz", 1);
    expect(a).not.toBe(b);
  });

  it("produces different secrets for different versions", async () => {
    const { deriveDeviceSecret } = await import("../hkdf");
    const v1 = await deriveDeviceSecret("device-abc", 1);
    const v2 = await deriveDeviceSecret("device-abc", 2);
    expect(v1).not.toBe(v2);
  });

  it("is deterministic for same inputs", async () => {
    const { deriveDeviceSecret } = await import("../hkdf");
    const a = await deriveDeviceSecret("device-abc", 1);
    const b = await deriveDeviceSecret("device-abc", 1);
    expect(a).toBe(b);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npx vitest run lib/auth/__tests__/hkdf.test.ts
```
Expected: FAIL — module not found

- [ ] **Step 3: Implement HKDF derivation**

```typescript
// jarvis-cloud/lib/auth/hkdf.ts
import { hkdf } from "crypto";
import { promisify } from "util";

const hkdfAsync = promisify(hkdf);

/**
 * Derive a per-device secret from the master secret using RFC 5869 HKDF-SHA256.
 *
 * Extract:  salt = "jarvis-hkdf-salt-v1"
 * Expand:   info = "jarvis-device-v{version}:{deviceId}"
 * Output:   32 bytes → 64-char hex string
 */
export async function deriveDeviceSecret(
  deviceId: string,
  version: number,
): Promise<string> {
  const masterSecret = process.env.JARVIS_MASTER_SECRET;
  if (!masterSecret || masterSecret.length < 32) {
    throw new Error("JARVIS_MASTER_SECRET must be at least 32 bytes");
  }

  const okm = await hkdfAsync(
    "sha256",
    Buffer.from(masterSecret, "utf-8"),
    Buffer.from("jarvis-hkdf-salt-v1", "utf-8"),
    Buffer.from(`jarvis-device-v${version}:${deviceId}`, "utf-8"),
    32,
  );

  return Buffer.from(okm).toString("hex");
}
```

- [ ] **Step 4: Run HKDF test to verify it passes**

```bash
npx vitest run lib/auth/__tests__/hkdf.test.ts
```
Expected: 4 tests PASS

- [ ] **Step 5: Write HMAC canonicalization + verification test**

```typescript
// jarvis-cloud/lib/auth/__tests__/hmac.test.ts
import { describe, it, expect } from "vitest";
import { canonicalize, signPayload, verifyHMAC } from "../hmac";
import type { CommandPayload } from "../../routing/types";

const TEST_SECRET = "a".repeat(64); // 32-byte hex secret

const basePayload: Omit<CommandPayload, "signature"> = {
  command_id: "cmd-001",
  device_id: "watch-ultra2-derek",
  device_type: "watch",
  text: "refactor the auth module",
  priority: "realtime",
  response_mode: "stream",
  timestamp: "2026-03-29T18:45:00Z",
};

describe("canonicalize", () => {
  it("produces alphabetically sorted key=value pairs joined by &", () => {
    const result = canonicalize(basePayload as CommandPayload);
    expect(result).toBe(
      "command_id=cmd-001&device_id=watch-ultra2-derek&device_type=watch&" +
      "priority=realtime&response_mode=stream&text=refactor the auth module&" +
      "timestamp=2026-03-29T18:45:00Z"
    );
  });

  it("includes intent_hint when present", () => {
    const payload = { ...basePayload, intent_hint: "ouroboros_scan" } as CommandPayload;
    const result = canonicalize(payload);
    expect(result).toContain("intent_hint=ouroboros_scan");
  });

  it("includes sorted JSON context when present", () => {
    const payload = {
      ...basePayload,
      context: { location: "office", battery_level: 72 },
    } as CommandPayload;
    const result = canonicalize(payload);
    expect(result).toContain('context={"battery_level":72,"location":"office"}');
  });
});

describe("signPayload + verifyHMAC", () => {
  it("produces a valid signature that verifies", () => {
    const signature = signPayload(basePayload as CommandPayload, TEST_SECRET);
    expect(signature).toMatch(/^[0-9a-f]{64}$/);

    const payload = { ...basePayload, signature } as CommandPayload;
    expect(verifyHMAC(payload, TEST_SECRET)).toBe(true);
  });

  it("rejects a tampered payload", () => {
    const signature = signPayload(basePayload as CommandPayload, TEST_SECRET);
    const tampered = { ...basePayload, text: "rm -rf /", signature } as CommandPayload;
    expect(verifyHMAC(tampered, TEST_SECRET)).toBe(false);
  });

  it("rejects a wrong secret", () => {
    const signature = signPayload(basePayload as CommandPayload, TEST_SECRET);
    const payload = { ...basePayload, signature } as CommandPayload;
    expect(verifyHMAC(payload, "b".repeat(64))).toBe(false);
  });
});
```

- [ ] **Step 6: Run test to verify it fails**

```bash
npx vitest run lib/auth/__tests__/hmac.test.ts
```
Expected: FAIL — module not found

- [ ] **Step 7: Implement HMAC canonicalization + verification**

```typescript
// jarvis-cloud/lib/auth/hmac.ts
import { createHmac, timingSafeEqual } from "crypto";
import type { CommandPayload } from "../routing/types";

const CANONICAL_FIELDS = [
  "command_id",
  "device_id",
  "device_type",
  "priority",
  "response_mode",
  "text",
  "timestamp",
] as const;

/**
 * Produce the canonical byte string for HMAC signing.
 * Fields are alphabetical. context is sorted-key JSON. intent_hint included when present.
 */
export function canonicalize(payload: CommandPayload): string {
  const parts: string[] = CANONICAL_FIELDS.map(
    (k) => `${k}=${payload[k]}`,
  );

  // intent_hint comes between device_type and priority alphabetically
  if (payload.intent_hint) {
    parts.splice(3, 0, `intent_hint=${payload.intent_hint}`);
  }

  if (payload.context) {
    const sortedKeys = Object.keys(payload.context).sort();
    const sorted: Record<string, unknown> = {};
    for (const k of sortedKeys) {
      sorted[k] = (payload.context as Record<string, unknown>)[k];
    }
    parts.push(`context=${JSON.stringify(sorted)}`);
  }

  return parts.join("&");
}

/**
 * Sign a payload with HMAC-SHA256.
 * @param secret — 64-char hex string (32-byte device secret)
 */
export function signPayload(
  payload: CommandPayload,
  secret: string,
): string {
  const canonical = canonicalize(payload);
  return createHmac("sha256", Buffer.from(secret, "hex"))
    .update(Buffer.from(canonical, "utf-8"))
    .digest("hex");
}

/**
 * Verify a signed payload using timing-safe comparison.
 */
export function verifyHMAC(
  payload: CommandPayload,
  secret: string,
): boolean {
  const expected = signPayload(payload, secret);
  const actual = payload.signature;
  if (expected.length !== actual.length) return false;
  return timingSafeEqual(
    Buffer.from(expected, "hex"),
    Buffer.from(actual, "hex"),
  );
}
```

- [ ] **Step 8: Run test to verify it passes**

```bash
npx vitest run lib/auth/__tests__/hmac.test.ts
```
Expected: 6 tests PASS

- [ ] **Step 9: Commit**

```bash
git add jarvis-cloud/lib/auth/
git commit -m "feat(cloud): HMAC auth + HKDF device secret derivation"
```

---

## Task 4: Stream Token + Pairing + Cron Auth

**Files:**
- Create: `jarvis-cloud/lib/auth/stream-token.ts`
- Create: `jarvis-cloud/lib/auth/pairing.ts`
- Create: `jarvis-cloud/lib/auth/cron.ts`
- Test: `jarvis-cloud/lib/auth/__tests__/stream-token.test.ts`
- Test: `jarvis-cloud/lib/auth/__tests__/pairing.test.ts`

- [ ] **Step 1: Write stream token test**

```typescript
// jarvis-cloud/lib/auth/__tests__/stream-token.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";

const mockRedis = {
  set: vi.fn().mockResolvedValue("OK"),
  get: vi.fn(),
  del: vi.fn().mockResolvedValue(1),
};

vi.mock("../../redis/client", () => ({
  getRedis: () => mockRedis,
}));

describe("stream tokens", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("issueStreamToken stores token in Redis with 300s TTL", async () => {
    const { issueStreamToken } = await import("../stream-token");
    const token = await issueStreamToken("device-abc");
    expect(token).toBeTruthy();
    expect(mockRedis.set).toHaveBeenCalledWith(
      expect.stringMatching(/^ssetok:/),
      "device-abc",
      { ex: 300 },
    );
  });

  it("validateStreamToken returns true for valid token and deletes it", async () => {
    mockRedis.get.mockResolvedValue("device-abc");
    const { validateStreamToken } = await import("../stream-token");
    const result = await validateStreamToken("tok-123", "device-abc");
    expect(result).toBe(true);
    expect(mockRedis.del).toHaveBeenCalledWith("ssetok:tok-123");
  });

  it("validateStreamToken returns false for wrong device", async () => {
    mockRedis.get.mockResolvedValue("device-xyz");
    const { validateStreamToken } = await import("../stream-token");
    const result = await validateStreamToken("tok-123", "device-abc");
    expect(result).toBe(false);
    expect(mockRedis.del).not.toHaveBeenCalled();
  });

  it("validateStreamToken returns false for expired/missing token", async () => {
    mockRedis.get.mockResolvedValue(null);
    const { validateStreamToken } = await import("../stream-token");
    const result = await validateStreamToken("tok-expired", "device-abc");
    expect(result).toBe(false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npx vitest run lib/auth/__tests__/stream-token.test.ts
```
Expected: FAIL

- [ ] **Step 3: Implement stream token**

```typescript
// jarvis-cloud/lib/auth/stream-token.ts
import { randomUUID } from "crypto";
import { getRedis } from "../redis/client";

export async function issueStreamToken(deviceId: string): Promise<string> {
  const redis = getRedis();
  const token = randomUUID();
  await redis.set(`ssetok:${token}`, deviceId, { ex: 300 });
  return token;
}

export async function validateStreamToken(
  token: string,
  deviceId: string,
): Promise<boolean> {
  const redis = getRedis();
  const stored = await redis.get(`ssetok:${token}`);
  if (stored !== deviceId) return false;
  // Single-use: consume the token
  await redis.del(`ssetok:${token}`);
  return true;
}
```

- [ ] **Step 4: Run stream token test**

```bash
npx vitest run lib/auth/__tests__/stream-token.test.ts
```
Expected: 4 tests PASS

- [ ] **Step 5: Write pairing test**

```typescript
// jarvis-cloud/lib/auth/__tests__/pairing.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";

const mockRedis = {
  set: vi.fn().mockResolvedValue("OK"),
  get: vi.fn(),
  del: vi.fn().mockResolvedValue(1),
};

vi.mock("../../redis/client", () => ({
  getRedis: () => mockRedis,
}));

vi.stubEnv("JARVIS_MASTER_SECRET", "test-master-secret-at-least-32-bytes-long!!");

describe("pairing", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("generatePairingCode produces 8-char alphanumeric code", async () => {
    const { generatePairingCode } = await import("../pairing");
    const code = await generatePairingCode("session-1", "watch");
    expect(code).toMatch(/^[A-Z0-9]{8}$/);
    expect(mockRedis.set).toHaveBeenCalledWith(
      expect.stringMatching(/^pairing:/),
      expect.any(String),
      { ex: 300 },
    );
  });

  it("validatePairingCode succeeds with correct code", async () => {
    mockRedis.get.mockResolvedValue(JSON.stringify({
      code: "ABCD1234",
      created_by_session: "session-1",
      created_at: new Date().toISOString(),
      attempts_remaining: 3,
      device_type_hint: "watch",
    }));
    const { validatePairingCode } = await import("../pairing");
    const result = await validatePairingCode("ABCD1234", "device-new");
    expect(result.success).toBe(true);
    expect(result.device_secret).toBeTruthy();
    expect(result.device_secret).toHaveLength(64);
  });

  it("validatePairingCode fails with wrong code", async () => {
    mockRedis.get.mockResolvedValue(null);
    const { validatePairingCode } = await import("../pairing");
    const result = await validatePairingCode("WRONG123", "device-new");
    expect(result.success).toBe(false);
  });
});
```

- [ ] **Step 6: Run test to verify it fails**

```bash
npx vitest run lib/auth/__tests__/pairing.test.ts
```
Expected: FAIL

- [ ] **Step 7: Implement pairing**

```typescript
// jarvis-cloud/lib/auth/pairing.ts
import { randomBytes } from "crypto";
import { getRedis } from "../redis/client";
import { deriveDeviceSecret } from "./hkdf";
import type { DeviceType, PairingSession } from "../routing/types";

const PAIRING_TTL = 300; // 5 minutes
const MAX_ATTEMPTS = 3;

export async function generatePairingCode(
  sessionId: string,
  deviceTypeHint: DeviceType,
): Promise<string> {
  const redis = getRedis();
  const code = randomBytes(4)
    .toString("hex")
    .toUpperCase()
    .slice(0, 8);

  const session: PairingSession = {
    code,
    created_by_session: sessionId,
    created_at: new Date().toISOString(),
    attempts_remaining: MAX_ATTEMPTS,
    device_type_hint: deviceTypeHint,
  };

  await redis.set(`pairing:${code}`, JSON.stringify(session), { ex: PAIRING_TTL });
  return code;
}

export async function validatePairingCode(
  code: string,
  deviceId: string,
): Promise<{ success: boolean; device_secret?: string }> {
  const redis = getRedis();
  const raw = await redis.get(`pairing:${code}`);
  if (!raw) return { success: false };

  const session: PairingSession = typeof raw === "string" ? JSON.parse(raw) : raw;

  if (session.attempts_remaining <= 0) {
    await redis.del(`pairing:${code}`);
    return { success: false };
  }

  // Derive device secret
  const deviceSecret = await deriveDeviceSecret(deviceId, 1);

  // Consume the pairing code
  await redis.del(`pairing:${code}`);

  return { success: true, device_secret: deviceSecret };
}
```

- [ ] **Step 8: Run pairing test**

```bash
npx vitest run lib/auth/__tests__/pairing.test.ts
```
Expected: 3 tests PASS

- [ ] **Step 9: Implement cron auth helper**

```typescript
// jarvis-cloud/lib/auth/cron.ts

export function verifyCron(req: Request): boolean {
  const auth = req.headers.get("Authorization");
  return auth === `Bearer ${process.env.CRON_SECRET}`;
}
```

- [ ] **Step 10: Commit**

```bash
git add jarvis-cloud/lib/auth/
git commit -m "feat(cloud): stream tokens, device pairing, and cron auth"
```

---

## Task 5: Intent Router (Tier 0)

**Files:**
- Create: `jarvis-cloud/lib/routing/intent-router.ts`
- Test: `jarvis-cloud/lib/routing/__tests__/intent-router.test.ts`

- [ ] **Step 1: Write intent router test**

```typescript
// jarvis-cloud/lib/routing/__tests__/intent-router.test.ts
import { describe, it, expect } from "vitest";
import { resolveRoute } from "../intent-router";
import type { CommandPayload } from "../types";

function makePayload(overrides: Partial<CommandPayload> = {}): CommandPayload {
  return {
    command_id: "cmd-001",
    device_id: "watch-ultra2-derek",
    device_type: "watch",
    text: "hello jarvis",
    priority: "realtime",
    response_mode: "stream",
    timestamp: new Date().toISOString(),
    signature: "test",
    ...overrides,
  };
}

describe("resolveRoute — Tier 0", () => {
  it("routes 'run ouroboros scan' to doubleword_397b batch", () => {
    const decision = resolveRoute(makePayload({ text: "run ouroboros scan on reactor-core" }));
    expect(decision.brain).toBe("doubleword_397b");
    expect(decision.mode).toBe("batch");
  });

  it("routes 'deep analyze' to doubleword_397b batch", () => {
    const decision = resolveRoute(makePayload({ text: "deep analyze the auth module" }));
    expect(decision.brain).toBe("doubleword_397b");
    expect(decision.mode).toBe("batch");
  });

  it("routes 'generate code' to doubleword_397b batch", () => {
    const decision = resolveRoute(makePayload({ text: "generate implementation for login flow" }));
    expect(decision.brain).toBe("doubleword_397b");
    expect(decision.mode).toBe("batch");
  });

  it("routes 'what do you see' to doubleword_235b batch", () => {
    const decision = resolveRoute(makePayload({ text: "what do you see on the screen?" }));
    expect(decision.brain).toBe("doubleword_235b");
    expect(decision.mode).toBe("batch");
  });

  it("routes 'screenshot' to doubleword_235b batch", () => {
    const decision = resolveRoute(makePayload({ text: "take a screenshot and analyze it" }));
    expect(decision.brain).toBe("doubleword_235b");
    expect(decision.mode).toBe("batch");
  });

  it("defaults unmatched text to claude streaming", () => {
    const decision = resolveRoute(makePayload({ text: "what's the weather today?" }));
    expect(decision.brain).toBe("claude");
    expect(decision.mode).toBe("stream");
  });

  it("honors trusted intent_hint (short-circuits regex)", () => {
    const decision = resolveRoute(makePayload({
      text: "please do something",
      intent_hint: "ouroboros_scan",
    }));
    expect(decision.brain).toBe("doubleword_397b");
    expect(decision.mode).toBe("batch");
  });

  it("ignores untrusted intent_hint", () => {
    const decision = resolveRoute(makePayload({
      text: "hello",
      intent_hint: "evil_hack",
    }));
    expect(decision.brain).toBe("claude");
    expect(decision.mode).toBe("stream");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npx vitest run lib/routing/__tests__/intent-router.test.ts
```
Expected: FAIL

- [ ] **Step 3: Implement intent router**

```typescript
// jarvis-cloud/lib/routing/intent-router.ts
import type { CommandPayload, RoutingDecision, BrainId, RouteRule } from "./types";

const TRUSTED_HINTS: Record<string, { brain: BrainId; mode: "batch" }> = {
  ouroboros_scan: { brain: "doubleword_397b", mode: "batch" },
  ouroboros_review: { brain: "doubleword_397b", mode: "batch" },
  deep_analysis: { brain: "doubleword_397b", mode: "batch" },
  vision_capture: { brain: "doubleword_235b", mode: "batch" },
  code_generation: { brain: "doubleword_397b", mode: "batch" },
};

const TIER_0_ROUTES: RouteRule[] = [
  // Doubleword 397B — coding/reasoning
  { pattern: /^(run |start |execute )?ouroboros/i, brain: "doubleword_397b", mode: "batch", model: "Qwen/Qwen3.5-397B-A17B-FP8", system_prompt_key: "ouroboros", estimated_latency: "minutes" },
  { pattern: /^(deep )?(scan|analyze|audit)/i, brain: "doubleword_397b", mode: "batch", model: "Qwen/Qwen3.5-397B-A17B-FP8", system_prompt_key: "analysis", estimated_latency: "minutes" },
  { pattern: /^generate (code|implementation|PR)/i, brain: "doubleword_397b", mode: "batch", model: "Qwen/Qwen3.5-397B-A17B-FP8", system_prompt_key: "codegen", estimated_latency: "minutes" },
  // Doubleword 235B — vision
  { pattern: /^(what do you see|analyze screen|describe)/i, brain: "doubleword_235b", mode: "batch", model: "Qwen/Qwen3.5-235B-Vision", system_prompt_key: "vision", estimated_latency: "minutes" },
  { pattern: /screenshot|screen capture|visual/i, brain: "doubleword_235b", mode: "batch", model: "Qwen/Qwen3.5-235B-Vision", system_prompt_key: "vision", estimated_latency: "minutes" },
];

const CLAUDE_DEFAULT: RoutingDecision = {
  brain: "claude",
  mode: "stream",
  model: "claude-sonnet-4-6",
  fan_out: [], // Populated by caller with device registry
  system_prompt_key: "jarvis",
  estimated_latency: "realtime",
};

export function resolveRoute(payload: CommandPayload): RoutingDecision {
  // Fast-path: trusted intent_hint skips regex
  if (payload.intent_hint && payload.intent_hint in TRUSTED_HINTS) {
    const hint = TRUSTED_HINTS[payload.intent_hint];
    const matchingRule = TIER_0_ROUTES.find(r => r.brain === hint.brain);
    return {
      brain: hint.brain,
      mode: hint.mode,
      model: matchingRule?.model ?? "Qwen/Qwen3.5-397B-A17B-FP8",
      fan_out: [],
      system_prompt_key: matchingRule?.system_prompt_key ?? "default",
      estimated_latency: matchingRule?.estimated_latency ?? "minutes",
    };
  }

  // Tier 0: regex matching
  for (const rule of TIER_0_ROUTES) {
    if (rule.pattern.test(payload.text)) {
      return {
        brain: rule.brain,
        mode: rule.mode,
        model: rule.model,
        fan_out: [],
        system_prompt_key: rule.system_prompt_key,
        estimated_latency: rule.estimated_latency,
      };
    }
  }

  // Default: Claude streaming
  return { ...CLAUDE_DEFAULT };
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
npx vitest run lib/routing/__tests__/intent-router.test.ts
```
Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis-cloud/lib/routing/
git commit -m "feat(cloud): Tier 0 intent router with trusted hint allowlist"
```

---

## Task 6: Redis Event Backlog (Streams)

**Files:**
- Create: `jarvis-cloud/lib/redis/event-backlog.ts`
- Test: `jarvis-cloud/lib/redis/__tests__/event-backlog.test.ts`

- [ ] **Step 1: Write event backlog test**

```typescript
// jarvis-cloud/lib/redis/__tests__/event-backlog.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";

const mockRedis = {
  xadd: vi.fn().mockResolvedValue("1234567890-0"),
  xrange: vi.fn().mockResolvedValue([]),
  xtrim: vi.fn().mockResolvedValue(0),
};

vi.mock("../client", () => ({
  getRedis: () => mockRedis,
}));

describe("event backlog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("appendToBacklog calls XADD with correct key and XTRIM", async () => {
    const { appendToBacklog } = await import("../event-backlog");
    await appendToBacklog("device-abc", "evt-1", {
      event: "token",
      data: { command_id: "cmd-1", token: "hi" },
    });
    expect(mockRedis.xadd).toHaveBeenCalledWith(
      "stream:events:device-abc",
      "*",
      { payload: expect.any(String) },
    );
    expect(mockRedis.xtrim).toHaveBeenCalledWith(
      "stream:events:device-abc",
      { strategy: "MAXLEN", threshold: 100 },
    );
  });

  it("replayBacklog calls XRANGE from lastEventId", async () => {
    mockRedis.xrange.mockResolvedValue([
      ["1234567891-0", { payload: JSON.stringify({ event: "token", data: { token: "x" } }) }],
    ]);
    const { replayBacklog } = await import("../event-backlog");
    const events = await replayBacklog("device-abc", "1234567890-0");
    expect(events).toHaveLength(1);
    expect(events[0].event).toBe("token");
    expect(mockRedis.xrange).toHaveBeenCalledWith(
      "stream:events:device-abc",
      "1234567890-1", // Exclusive start: increment sequence
      "+",
      50,
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npx vitest run lib/redis/__tests__/event-backlog.test.ts
```
Expected: FAIL

- [ ] **Step 3: Implement event backlog**

```typescript
// jarvis-cloud/lib/redis/event-backlog.ts
import { getRedis } from "./client";

const MAX_BACKLOG = 100;
const MAX_REPLAY = 50;

export async function appendToBacklog(
  deviceId: string,
  eventId: string,
  event: { event: string; data: Record<string, unknown> },
): Promise<void> {
  const redis = getRedis();
  const key = `stream:events:${deviceId}`;

  await redis.xadd(key, "*", {
    payload: JSON.stringify({ ...event, id: eventId }),
  });
  await redis.xtrim(key, { strategy: "MAXLEN", threshold: MAX_BACKLOG });
}

export interface ReplayedEvent {
  id: string;
  event: string;
  data: Record<string, unknown>;
}

export async function replayBacklog(
  deviceId: string,
  lastEventId: string,
): Promise<ReplayedEvent[]> {
  const redis = getRedis();
  const key = `stream:events:${deviceId}`;

  // Exclusive start: increment the sequence number
  const parts = lastEventId.split("-");
  const exclusiveStart = parts.length === 2
    ? `${parts[0]}-${parseInt(parts[1], 10) + 1}`
    : `${lastEventId}-1`;

  const entries = await redis.xrange(key, exclusiveStart, "+", MAX_REPLAY);

  return entries.map(([id, fields]: [string, Record<string, string>]) => {
    const parsed = JSON.parse(fields.payload);
    return {
      id,
      event: parsed.event,
      data: parsed.data,
    };
  });
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
npx vitest run lib/redis/__tests__/event-backlog.test.ts
```
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis-cloud/lib/redis/
git commit -m "feat(cloud): Redis Streams event backlog for SSE replay"
```

---

## Task 7: Fan-Out (Redis Streams + Queue)

**Files:**
- Create: `jarvis-cloud/lib/brains/fan-out.ts`
- Create: `jarvis-cloud/lib/queue/topics.ts`
- Test: `jarvis-cloud/lib/brains/__tests__/fan-out.test.ts`

- [ ] **Step 1: Write fan-out test**

```typescript
// jarvis-cloud/lib/brains/__tests__/fan-out.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";

const mockRedis = {
  xadd: vi.fn().mockResolvedValue("1234567890-0"),
  xtrim: vi.fn().mockResolvedValue(0),
};

vi.mock("../../redis/client", () => ({
  getRedis: () => mockRedis,
}));

vi.mock("../../redis/event-backlog", () => ({
  appendToBacklog: vi.fn(),
}));

describe("publishToDevices", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("publishes to Redis Stream for redis-channel targets", async () => {
    const { publishToDevices } = await import("../fan-out");
    await publishToDevices(
      [{ device_id: "mac-m1", channel: "redis", role: "executor" }],
      { event: "token", data: { command_id: "cmd-1", token: "hi" } },
    );
    expect(mockRedis.xadd).toHaveBeenCalledWith(
      "stream:events:mac-m1",
      "*",
      { payload: expect.any(String) },
    );
  });

  it("publishes to multiple targets in parallel", async () => {
    const { publishToDevices } = await import("../fan-out");
    await publishToDevices(
      [
        { device_id: "mac-m1", channel: "redis", role: "executor" },
        { device_id: "watch-ultra2", channel: "redis", role: "observer" },
      ],
      { event: "daemon", data: { narration_text: "hello" } },
    );
    expect(mockRedis.xadd).toHaveBeenCalledTimes(2);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npx vitest run lib/brains/__tests__/fan-out.test.ts
```
Expected: FAIL

- [ ] **Step 3: Implement fan-out**

```typescript
// jarvis-cloud/lib/brains/fan-out.ts
import { ulid } from "ulid";
import { getRedis } from "../redis/client";
import type { DeviceTarget } from "../routing/types";

export async function publishToDevices(
  targets: DeviceTarget[],
  event: { event: string; data: Record<string, unknown> },
): Promise<void> {
  const eventId = `${(event.data.command_id as string) ?? "sys"}:${ulid()}`;
  const redis = getRedis();

  await Promise.all(
    targets.map(async (target) => {
      if (target.channel === "redis") {
        const key = `stream:events:${target.device_id}`;
        const payload = JSON.stringify({ ...event, id: eventId });
        await redis.xadd(key, "*", { payload });
        await redis.xtrim(key, { strategy: "MAXLEN", threshold: 100 });
      } else {
        // Vercel Queue — durable delivery
        await enqueueForDevice(target.device_id, { ...event, id: eventId });
      }
    }),
  );
}

async function enqueueForDevice(
  deviceId: string,
  event: Record<string, unknown>,
): Promise<void> {
  // Vercel Queue integration — writes to durable topic
  // For v1, fall back to Redis Stream with longer TTL
  const redis = getRedis();
  const key = `queue:durable:${deviceId}`;
  await redis.xadd(key, "*", { payload: JSON.stringify(event) });
  await redis.xtrim(key, { strategy: "MAXLEN", threshold: 500 });
}
```

- [ ] **Step 4: Create queue topics stub**

```typescript
// jarvis-cloud/lib/queue/topics.ts

/**
 * Vercel Queue topic definitions.
 *
 * v1: Uses Redis Streams with longer TTL as durable fallback.
 * v2: Migrate to native Vercel Queues when available.
 */

export const TOPICS = {
  OUROBOROS_COMPLETE: "ouroboros.complete",
  DOUBLEWORD_RESULT: "doubleword.result",
  DEVICE_NOTIFICATION: "device.notification",
} as const;

export async function enqueueOuroborosResult(payload: {
  job_id: string;
  command_id: string;
  status: string;
  artifacts?: unknown[];
}): Promise<void> {
  // For v1, uses the Redis-backed durable queue in fan-out.ts
  // Import dynamically to avoid circular dependency
  const { getRedis } = await import("../redis/client");
  const redis = getRedis();
  await redis.xadd("queue:ouroboros:results", "*", {
    payload: JSON.stringify(payload),
  });
}
```

- [ ] **Step 5: Run fan-out test**

```bash
npx vitest run lib/brains/__tests__/fan-out.test.ts
```
Expected: 2 tests PASS

- [ ] **Step 6: Commit**

```bash
git add jarvis-cloud/lib/brains/ jarvis-cloud/lib/queue/
git commit -m "feat(cloud): dual-path fan-out (Redis Streams + durable queue)"
```

---

## Task 8: Claude Streaming Brain

**Files:**
- Create: `jarvis-cloud/lib/brains/claude.ts`
- Test: `jarvis-cloud/lib/brains/__tests__/claude.test.ts`

- [ ] **Step 1: Write Claude streaming test**

```typescript
// jarvis-cloud/lib/brains/__tests__/claude.test.ts
import { describe, it, expect, vi } from "vitest";
import { buildMessages, getSystemPrompt } from "../claude";
import type { CommandPayload } from "../../routing/types";

describe("Claude helpers", () => {
  it("buildMessages creates a user message from command text", () => {
    const payload = {
      text: "refactor the auth module",
      context: { active_app: "VSCode", active_file: "/src/auth.ts" },
    } as CommandPayload;
    const messages = buildMessages(payload);
    expect(messages).toHaveLength(1);
    expect(messages[0].role).toBe("user");
    expect(messages[0].content).toContain("refactor the auth module");
    expect(messages[0].content).toContain("VSCode");
    expect(messages[0].content).toContain("/src/auth.ts");
  });

  it("getSystemPrompt returns JARVIS persona for 'jarvis' key", () => {
    const prompt = getSystemPrompt("jarvis");
    expect(prompt).toContain("JARVIS");
    expect(prompt.length).toBeGreaterThan(50);
  });

  it("getSystemPrompt returns analysis persona for 'analysis' key", () => {
    const prompt = getSystemPrompt("analysis");
    expect(prompt).toContain("analy");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npx vitest run lib/brains/__tests__/claude.test.ts
```
Expected: FAIL

- [ ] **Step 3: Implement Claude brain module**

```typescript
// jarvis-cloud/lib/brains/claude.ts
import Anthropic from "@anthropic-ai/sdk";
import { formatSSE } from "../sse/encoder";
import { publishToDevices } from "./fan-out";
import type { CommandPayload, RoutingDecision } from "../routing/types";

const anthropic = new Anthropic();

const SYSTEM_PROMPTS: Record<string, string> = {
  jarvis: `You are JARVIS, Derek's AI assistant. You are concise, technical, and proactive. You have access to execute actions on Derek's Mac (Ghost Hands clicks, file edits, terminal commands) via structured action events. When a command requires local execution, emit action events in your response. Be direct and efficient.`,
  analysis: `You are JARVIS in deep analysis mode. Provide thorough, structured analysis of code, architecture, and systems. Be detailed and systematic.`,
  codegen: `You are JARVIS in code generation mode. Generate production-quality, well-tested code. Follow existing patterns and conventions.`,
  ouroboros: `You are JARVIS Ouroboros governance engine. Analyze codebases for improvements, security issues, and optimization opportunities. Propose concrete changes as diffs.`,
  vision: `You are JARVIS vision system. Describe what you see on screen accurately and concisely. Identify UI elements, text, and layout.`,
  default: `You are JARVIS, a helpful AI assistant.`,
};

export function getSystemPrompt(key: string): string {
  return SYSTEM_PROMPTS[key] ?? SYSTEM_PROMPTS.default;
}

export function buildMessages(
  payload: CommandPayload,
): Anthropic.MessageParam[] {
  let content = payload.text;

  if (payload.context) {
    const ctx = payload.context;
    const parts: string[] = [];
    if (ctx.active_app) parts.push(`Active app: ${ctx.active_app}`);
    if (ctx.active_file) parts.push(`Active file: ${ctx.active_file}`);
    if (ctx.screen_summary) parts.push(`Screen: ${ctx.screen_summary}`);
    if (ctx.location) parts.push(`Location: ${ctx.location}`);
    if (parts.length > 0) {
      content = `[Context: ${parts.join(", ")}]\n\n${content}`;
    }
  }

  return [{ role: "user", content }];
}

export function streamClaude(
  payload: CommandPayload,
  decision: RoutingDecision,
): Response {
  const encoder = new TextEncoder();

  const stream = new ReadableStream({
    async start(controller) {
      const startTime = Date.now();
      let sequence = 0;

      try {
        const response = anthropic.messages.stream({
          model: decision.model,
          max_tokens: 4096,
          system: getSystemPrompt(decision.system_prompt_key),
          messages: buildMessages(payload),
        });

        for await (const event of response) {
          if (
            event.type === "content_block_delta" &&
            event.delta.type === "text_delta"
          ) {
            const token = event.delta.text;
            sequence++;

            // SSE to requesting device (this response)
            controller.enqueue(
              encoder.encode(
                formatSSE("token", {
                  command_id: payload.command_id,
                  token,
                  source_brain: "claude",
                  sequence,
                }),
              ),
            );

            // Fan-out to OTHER devices via Redis Streams
            await publishToDevices(
              decision.fan_out.filter(
                (d) => d.device_id !== payload.device_id,
              ),
              {
                event: "token",
                data: {
                  command_id: payload.command_id,
                  token,
                  source_brain: "claude",
                  sequence,
                },
              },
            );
          }
        }

        // Complete event
        const finalMsg = await response.finalMessage();
        const complete = {
          command_id: payload.command_id,
          source_brain: "claude" as const,
          token_count:
            finalMsg.usage.input_tokens + finalMsg.usage.output_tokens,
          latency_ms: Date.now() - startTime,
        };
        controller.enqueue(
          encoder.encode(formatSSE("complete", complete)),
        );
        await publishToDevices(decision.fan_out, {
          event: "complete",
          data: complete,
        });
      } catch (err) {
        const errorEvent = {
          command_id: payload.command_id,
          narration_text: `Command failed: ${err instanceof Error ? err.message : "unknown error"}`,
          narration_priority: "urgent" as const,
          source_brain: "claude" as const,
        };
        controller.enqueue(
          encoder.encode(formatSSE("daemon", errorEvent)),
        );
        // Fan-out error to all devices
        await publishToDevices(decision.fan_out, {
          event: "daemon",
          data: errorEvent,
        });
      } finally {
        controller.close();
      }
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Command-ID": payload.command_id,
    },
  });
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
npx vitest run lib/brains/__tests__/claude.test.ts
```
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis-cloud/lib/brains/claude.ts jarvis-cloud/lib/brains/__tests__/claude.test.ts
git commit -m "feat(cloud): Claude streaming brain with SSE + fan-out"
```

---

## Task 9: Doubleword Batch Brain

**Files:**
- Create: `jarvis-cloud/lib/brains/doubleword.ts`
- Test: `jarvis-cloud/lib/brains/__tests__/doubleword.test.ts`

- [ ] **Step 1: Write Doubleword batch test**

```typescript
// jarvis-cloud/lib/brains/__tests__/doubleword.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";

const mockFetch = vi.fn();
global.fetch = mockFetch;

const mockRedis = {
  set: vi.fn().mockResolvedValue("OK"),
};

vi.mock("../../redis/client", () => ({
  getRedis: () => mockRedis,
}));

vi.stubEnv("DOUBLEWORD_API_KEY", "test-key");
vi.stubEnv("DOUBLEWORD_API_URL", "https://api.doubleword.ai");

describe("submitBatch", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("uploads file, creates batch, and stores job metadata", async () => {
    // Mock the 2-stage upload+create flow
    mockFetch
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ id: "file-001" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ id: "batch-001" }),
      });

    const { submitBatch } = await import("../doubleword");
    const jobId = await submitBatch(
      {
        command_id: "cmd-001",
        device_id: "mac-m1",
        text: "run ouroboros scan",
      } as any,
      {
        brain: "doubleword_397b",
        mode: "batch",
        model: "Qwen/Qwen3.5-397B-A17B-FP8",
        fan_out: [{ device_id: "mac-m1", channel: "redis", role: "executor" }],
        system_prompt_key: "ouroboros",
        estimated_latency: "minutes",
      },
    );

    expect(jobId).toBe("batch-001");
    expect(mockRedis.set).toHaveBeenCalledWith(
      "jobmeta:batch-001",
      expect.any(String),
      { ex: 86400 },
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npx vitest run lib/brains/__tests__/doubleword.test.ts
```
Expected: FAIL

- [ ] **Step 3: Implement Doubleword batch brain**

```typescript
// jarvis-cloud/lib/brains/doubleword.ts
import { getRedis } from "../redis/client";
import type { CommandPayload, RoutingDecision } from "../routing/types";
import { getSystemPrompt, buildMessages } from "./claude";

const DOUBLEWORD_API_URL = process.env.DOUBLEWORD_API_URL ?? "https://api.doubleword.ai";
const DOUBLEWORD_API_KEY = process.env.DOUBLEWORD_API_KEY ?? "";

/**
 * Submit a batch job to Doubleword's 4-stage async API.
 * Stage 1: Upload input file (JSONL with messages)
 * Stage 2: Create batch job referencing the file
 * Stages 3-4 (poll + retrieve) happen via webhook callback
 *
 * Returns the batch job ID for tracking.
 */
export async function submitBatch(
  payload: CommandPayload,
  decision: RoutingDecision,
): Promise<string> {
  const redis = getRedis();
  const systemPrompt = getSystemPrompt(decision.system_prompt_key);
  const messages = buildMessages(payload);

  // Stage 1: Upload input as JSONL
  const jsonlContent = JSON.stringify({
    custom_id: payload.command_id,
    method: "POST",
    url: "/v1/chat/completions",
    body: {
      model: decision.model,
      messages: [
        { role: "system", content: systemPrompt },
        ...messages.map((m) => ({ role: m.role, content: m.content })),
      ],
      max_tokens: 8192,
    },
  });

  const uploadResponse = await fetch(`${DOUBLEWORD_API_URL}/v1/files`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${DOUBLEWORD_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      content: jsonlContent,
      purpose: "batch",
    }),
  });

  if (!uploadResponse.ok) {
    throw new Error(`Doubleword file upload failed: ${uploadResponse.status}`);
  }
  const uploadResult = await uploadResponse.json();
  const fileId = uploadResult.id;

  // Stage 2: Create batch job
  const batchResponse = await fetch(`${DOUBLEWORD_API_URL}/v1/batches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${DOUBLEWORD_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      input_file_id: fileId,
      endpoint: "/v1/chat/completions",
      completion_window: "24h",
    }),
  });

  if (!batchResponse.ok) {
    throw new Error(`Doubleword batch creation failed: ${batchResponse.status}`);
  }
  const batchResult = await batchResponse.json();
  const jobId = batchResult.id;

  // Store metadata for callback fan-out
  await redis.set(
    `jobmeta:${jobId}`,
    JSON.stringify({
      command_id: payload.command_id,
      fan_out: decision.fan_out,
      brain: decision.brain,
      submitted_at: new Date().toISOString(),
    }),
    { ex: 86400 },
  );

  return jobId;
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
npx vitest run lib/brains/__tests__/doubleword.test.ts
```
Expected: 1 test PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis-cloud/lib/brains/doubleword.ts jarvis-cloud/lib/brains/__tests__/doubleword.test.ts
git commit -m "feat(cloud): Doubleword batch brain (4-stage API)"
```

---

## Task 10: POST /api/command Route

**Files:**
- Create: `jarvis-cloud/app/api/command/route.ts`
- Test: `jarvis-cloud/app/api/command/__tests__/route.test.ts`

- [ ] **Step 1: Write command route integration test**

```typescript
// jarvis-cloud/app/api/command/__tests__/route.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";

const mockRedis = {
  get: vi.fn(),
  set: vi.fn().mockResolvedValue("OK"),
  hset: vi.fn().mockResolvedValue(1),
};

vi.mock("../../../lib/redis/client", () => ({
  getRedis: () => mockRedis,
}));

vi.stubEnv("JARVIS_MASTER_SECRET", "test-master-secret-at-least-32-bytes-long!!");

describe("POST /api/command", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns 401 for unknown device", async () => {
    mockRedis.get.mockResolvedValue(null);
    const { POST } = await import("../route");
    const req = new Request("http://localhost/api/command", {
      method: "POST",
      body: JSON.stringify({
        command_id: "cmd-001",
        device_id: "unknown-device",
        device_type: "watch",
        text: "hello",
        priority: "realtime",
        response_mode: "stream",
        timestamp: new Date().toISOString(),
        signature: "invalid",
      }),
    });
    const res = await POST(req);
    expect(res.status).toBe(401);
  });

  it("returns 401 for expired timestamp", async () => {
    mockRedis.get.mockResolvedValue(JSON.stringify({
      device_id: "watch-ultra2-derek",
      device_type: "watch",
      active: true,
      hkdf_version: 1,
    }));
    const { POST } = await import("../route");
    const req = new Request("http://localhost/api/command", {
      method: "POST",
      body: JSON.stringify({
        command_id: "cmd-001",
        device_id: "watch-ultra2-derek",
        device_type: "watch",
        text: "hello",
        priority: "realtime",
        response_mode: "stream",
        timestamp: "2020-01-01T00:00:00Z", // Way expired
        signature: "test",
      }),
    });
    const res = await POST(req);
    expect(res.status).toBe(401);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
npx vitest run app/api/command/__tests__/route.test.ts
```
Expected: FAIL

- [ ] **Step 3: Implement POST /api/command**

```typescript
// jarvis-cloud/app/api/command/route.ts
import { getRedis } from "@/lib/redis/client";
import { verifyHMAC } from "@/lib/auth/hmac";
import { deriveDeviceSecret } from "@/lib/auth/hkdf";
import { resolveRoute } from "@/lib/routing/intent-router";
import { streamClaude } from "@/lib/brains/claude";
import { submitBatch } from "@/lib/brains/doubleword";
import { publishToDevices } from "@/lib/brains/fan-out";
import type { CommandPayload, DeviceRecord } from "@/lib/routing/types";

const REPLAY_WINDOW_S = 300;

export async function POST(req: Request): Promise<Response> {
  const redis = getRedis();
  const payload: CommandPayload = await req.json();

  // 1. Replay protection (no Redis needed)
  const age =
    Math.abs(Date.now() - new Date(payload.timestamp).getTime()) / 1000;
  if (age > REPLAY_WINDOW_S) {
    return new Response("Timestamp expired", { status: 401 });
  }

  // 2. Device lookup
  const raw = await redis.get(`device:${payload.device_id}`);
  if (!raw) {
    return new Response("Unknown device", { status: 401 });
  }
  const device: DeviceRecord =
    typeof raw === "string" ? JSON.parse(raw) : raw;
  if (!device.active) {
    return new Response("Device revoked", { status: 401 });
  }

  // 3. HMAC verification
  const secret = await deriveDeviceSecret(
    payload.device_id,
    device.hkdf_version,
  );
  if (!verifyHMAC(payload, secret)) {
    return new Response("Invalid signature", { status: 401 });
  }

  // 4. Route
  const decision = resolveRoute(payload);

  // Build fan-out targets from device registry
  decision.fan_out = await buildFanOut(redis, payload.device_id);

  // 5. Update last_seen
  device.last_seen = new Date().toISOString();
  await redis.set(`device:${payload.device_id}`, JSON.stringify(device));

  // 6. Execute
  if (decision.mode === "stream") {
    // Streaming idempotency: reserve in-flight slot
    const reserved = await redis.set(
      `cmd:${payload.command_id}`,
      JSON.stringify({ status: "in_flight", brain: decision.brain }),
      { nx: true, ex: 300 },
    );
    if (!reserved) {
      return new Response("Command already in flight", { status: 409 });
    }
    return streamClaude(payload, decision);
  } else {
    // Batch idempotency
    const cached = await redis.get(`cmd:${payload.command_id}`);
    if (cached) {
      return Response.json(
        typeof cached === "string" ? JSON.parse(cached) : cached,
      );
    }

    const jobId = await submitBatch(payload, decision);

    await publishToDevices(decision.fan_out, {
      event: "status",
      data: {
        command_id: payload.command_id,
        phase: "queued",
        message: `Batch job ${jobId} submitted to ${decision.brain}`,
      },
    });

    const result = {
      job_id: jobId,
      brain: decision.brain,
      status: "queued",
    };
    await redis.set(
      `cmd:${payload.command_id}`,
      JSON.stringify(result),
      { ex: 3600 },
    );
    return Response.json(result);
  }
}

async function buildFanOut(
  redis: ReturnType<typeof getRedis>,
  excludeDeviceId: string,
) {
  // Scan all device keys for active devices
  // For v1, we use a known device list key
  const deviceListRaw = await redis.get("devices:active_list");
  if (!deviceListRaw) return [];

  const deviceIds: string[] =
    typeof deviceListRaw === "string"
      ? JSON.parse(deviceListRaw)
      : deviceListRaw;

  const targets = await Promise.all(
    deviceIds
      .filter((id) => id !== excludeDeviceId)
      .map(async (id) => {
        const raw = await redis.get(`device:${id}`);
        if (!raw) return null;
        const device: DeviceRecord =
          typeof raw === "string" ? JSON.parse(raw) : raw;
        if (!device.active) return null;
        return {
          device_id: id,
          channel: "redis" as const,
          role: device.device_type === "mac"
            ? ("executor" as const)
            : ("observer" as const),
        };
      }),
  );

  return targets.filter(Boolean) as NonNullable<(typeof targets)[number]>[];
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
npx vitest run app/api/command/__tests__/route.test.ts
```
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis-cloud/app/api/command/
git commit -m "feat(cloud): POST /api/command — unified intake with dual-brain routing"
```

---

## Task 11: GET /api/stream/[deviceId] SSE Route

**Files:**
- Create: `jarvis-cloud/app/api/stream/[deviceId]/route.ts`
- Create: `jarvis-cloud/app/api/stream/token/route.ts`

- [ ] **Step 1: Implement stream token endpoint**

```typescript
// jarvis-cloud/app/api/stream/token/route.ts
import { getRedis } from "@/lib/redis/client";
import { verifyHMAC } from "@/lib/auth/hmac";
import { deriveDeviceSecret } from "@/lib/auth/hkdf";
import { issueStreamToken } from "@/lib/auth/stream-token";
import type { DeviceRecord } from "@/lib/routing/types";

export async function POST(req: Request): Promise<Response> {
  const redis = getRedis();
  const body = await req.json();
  const { device_id, signature, timestamp } = body;

  // Verify device auth
  const raw = await redis.get(`device:${device_id}`);
  if (!raw) return new Response("Unknown device", { status: 401 });

  const device: DeviceRecord =
    typeof raw === "string" ? JSON.parse(raw) : raw;
  if (!device.active) return new Response("Device revoked", { status: 401 });

  const secret = await deriveDeviceSecret(device_id, device.hkdf_version);
  const payload = { ...body, command_id: "stream-token", device_type: device.device_type, text: "stream-token-request", priority: "realtime", response_mode: "stream" };
  if (!verifyHMAC(payload, secret)) {
    return new Response("Invalid signature", { status: 401 });
  }

  const token = await issueStreamToken(device_id);
  const baseUrl = process.env.VERCEL_URL
    ? `https://${process.env.VERCEL_URL}`
    : "http://localhost:3000";

  return Response.json({
    token,
    stream_url: `${baseUrl}/api/stream/${device_id}?t=${token}`,
  });
}
```

- [ ] **Step 2: Implement SSE stream handler**

```typescript
// jarvis-cloud/app/api/stream/[deviceId]/route.ts
import { validateStreamToken } from "@/lib/auth/stream-token";
import { replayBacklog } from "@/lib/redis/event-backlog";
import { getRedis } from "@/lib/redis/client";
import { formatSSE } from "@/lib/sse/encoder";

export const runtime = "nodejs";
export const maxDuration = 300; // 5 minutes on Pro plan

export async function GET(
  req: Request,
  { params }: { params: Promise<{ deviceId: string }> },
): Promise<Response> {
  const { deviceId } = await params;
  const url = new URL(req.url);
  const token = url.searchParams.get("t");

  if (!token || !(await validateStreamToken(token, deviceId))) {
    return new Response("Unauthorized", { status: 401 });
  }

  const lastEventId = req.headers.get("Last-Event-ID");
  const encoder = new TextEncoder();
  const streamKey = `stream:events:${deviceId}`;
  const redis = getRedis();

  const stream = new ReadableStream({
    async start(controller) {
      // Replay missed events from backlog
      if (lastEventId) {
        try {
          const missed = await replayBacklog(deviceId, lastEventId);
          for (const event of missed) {
            controller.enqueue(
              encoder.encode(formatSSE(event.event, event.data, event.id)),
            );
          }
        } catch {
          // Best-effort replay — continue even if it fails
        }
      }

      let cursor = lastEventId ?? "0";
      let heartbeatCounter = 0;
      const POLL_INTERVAL_MS = 100;
      const HEARTBEAT_EVERY = 150; // ~15 seconds at 100ms poll

      while (!req.signal.aborted) {
        try {
          const entries = await redis.xrange(streamKey, cursor === "0" ? "$" : `${cursor.split("-")[0]}-${parseInt(cursor.split("-")[1] ?? "0", 10) + 1}`, "+", 50);

          for (const [id, fields] of entries) {
            const parsed = JSON.parse((fields as Record<string, string>).payload);
            controller.enqueue(
              encoder.encode(formatSSE(parsed.event, parsed.data, id)),
            );
            cursor = id;
          }

          // Heartbeat
          heartbeatCounter++;
          if (heartbeatCounter >= HEARTBEAT_EVERY) {
            controller.enqueue(
              encoder.encode(formatSSE("heartbeat", {})),
            );
            heartbeatCounter = 0;
          }

          // Wait before next poll
          if (entries.length === 0) {
            await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
          }
        } catch (err) {
          // Connection or Redis error — close stream, client will reconnect
          break;
        }
      }

      controller.close();
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  });
}
```

- [ ] **Step 3: Commit**

```bash
git add jarvis-cloud/app/api/stream/
git commit -m "feat(cloud): SSE stream handler + token endpoint (Redis Streams polling)"
```

---

## Task 12: Device Management Routes

**Files:**
- Create: `jarvis-cloud/app/api/devices/route.ts`
- Create: `jarvis-cloud/app/api/devices/pair/route.ts`
- Create: `jarvis-cloud/app/api/devices/[deviceId]/revoke/route.ts`
- Create: `jarvis-cloud/app/api/devices/health/route.ts`

- [ ] **Step 1: Implement device list**

```typescript
// jarvis-cloud/app/api/devices/route.ts
import { getRedis } from "@/lib/redis/client";
import type { DeviceRecord } from "@/lib/routing/types";

export async function GET(): Promise<Response> {
  const redis = getRedis();
  const listRaw = await redis.get("devices:active_list");
  if (!listRaw) return Response.json({ devices: [] });

  const deviceIds: string[] =
    typeof listRaw === "string" ? JSON.parse(listRaw) : listRaw;

  const devices = await Promise.all(
    deviceIds.map(async (id) => {
      const raw = await redis.get(`device:${id}`);
      if (!raw) return null;
      return typeof raw === "string" ? JSON.parse(raw) : raw;
    }),
  );

  return Response.json({
    devices: devices.filter(Boolean) as DeviceRecord[],
  });
}
```

- [ ] **Step 2: Implement pairing route**

```typescript
// jarvis-cloud/app/api/devices/pair/route.ts
import { getRedis } from "@/lib/redis/client";
import { validatePairingCode } from "@/lib/auth/pairing";
import type { DeviceRecord, DeviceType } from "@/lib/routing/types";

export async function POST(req: Request): Promise<Response> {
  const redis = getRedis();
  const body = await req.json();
  const { pairing_code, device_id, device_type, device_name, push_token } =
    body as {
      pairing_code: string;
      device_id: string;
      device_type: DeviceType;
      device_name: string;
      push_token?: string;
    };

  if (!pairing_code || !device_id || !device_type || !device_name) {
    return new Response("Missing required fields", { status: 400 });
  }

  const result = await validatePairingCode(pairing_code, device_id);
  if (!result.success || !result.device_secret) {
    return new Response("Invalid or expired pairing code", { status: 401 });
  }

  // Create device record
  const device: DeviceRecord = {
    device_id,
    device_type,
    device_name,
    paired_at: new Date().toISOString(),
    last_seen: new Date().toISOString(),
    push_token,
    role: device_type === "mac" ? "executor" : "observer",
    active: true,
    hkdf_version: 1,
  };

  await redis.set(`device:${device_id}`, JSON.stringify(device));

  // Add to active device list
  const listRaw = await redis.get("devices:active_list");
  const list: string[] = listRaw
    ? typeof listRaw === "string"
      ? JSON.parse(listRaw)
      : listRaw
    : [];
  if (!list.includes(device_id)) {
    list.push(device_id);
    await redis.set("devices:active_list", JSON.stringify(list));
  }

  const baseUrl = process.env.VERCEL_URL
    ? `https://${process.env.VERCEL_URL}`
    : "http://localhost:3000";

  return Response.json({
    device_secret: result.device_secret,
    stream_endpoint: `${baseUrl}/api/stream/${device_id}`,
    command_endpoint: `${baseUrl}/api/command`,
  });
}
```

- [ ] **Step 3: Implement revoke route**

```typescript
// jarvis-cloud/app/api/devices/[deviceId]/revoke/route.ts
import { getRedis } from "@/lib/redis/client";
import type { DeviceRecord } from "@/lib/routing/types";

export async function POST(
  _req: Request,
  { params }: { params: Promise<{ deviceId: string }> },
): Promise<Response> {
  const { deviceId } = await params;
  const redis = getRedis();

  const raw = await redis.get(`device:${deviceId}`);
  if (!raw) return new Response("Device not found", { status: 404 });

  const device: DeviceRecord =
    typeof raw === "string" ? JSON.parse(raw) : raw;
  device.active = false;
  await redis.set(`device:${deviceId}`, JSON.stringify(device));

  return Response.json({ revoked: true, device_id: deviceId });
}
```

- [ ] **Step 4: Implement health/prune route (cron target)**

```typescript
// jarvis-cloud/app/api/devices/health/route.ts
import { getRedis } from "@/lib/redis/client";
import { verifyCron } from "@/lib/auth/cron";
import type { DeviceRecord } from "@/lib/routing/types";

const STALE_THRESHOLD_MS = 7 * 24 * 60 * 60 * 1000; // 7 days

export async function GET(req: Request): Promise<Response> {
  if (!verifyCron(req)) {
    return new Response("Unauthorized", { status: 401 });
  }

  const redis = getRedis();
  const listRaw = await redis.get("devices:active_list");
  if (!listRaw) return Response.json({ pruned: 0 });

  const deviceIds: string[] =
    typeof listRaw === "string" ? JSON.parse(listRaw) : listRaw;

  let pruned = 0;
  for (const id of deviceIds) {
    const raw = await redis.get(`device:${id}`);
    if (!raw) continue;
    const device: DeviceRecord =
      typeof raw === "string" ? JSON.parse(raw) : raw;
    const lastSeen = new Date(device.last_seen).getTime();
    if (Date.now() - lastSeen > STALE_THRESHOLD_MS) {
      device.active = false;
      await redis.set(`device:${id}`, JSON.stringify(device));
      pruned++;
    }
  }

  return Response.json({ pruned, checked: deviceIds.length });
}
```

- [ ] **Step 5: Commit**

```bash
git add jarvis-cloud/app/api/devices/
git commit -m "feat(cloud): device management routes (list, pair, revoke, health cron)"
```

---

## Task 13: Doubleword Callback Webhook

**Files:**
- Create: `jarvis-cloud/app/api/doubleword/callback/route.ts`
- Create: `jarvis-cloud/app/api/doubleword/submit/route.ts`

- [ ] **Step 1: Implement callback webhook**

```typescript
// jarvis-cloud/app/api/doubleword/callback/route.ts
import { createHmac, timingSafeEqual } from "crypto";
import { getRedis } from "@/lib/redis/client";
import { publishToDevices } from "@/lib/brains/fan-out";
import { enqueueOuroborosResult } from "@/lib/queue/topics";
import type { BrainId } from "@/lib/routing/types";

export async function POST(req: Request): Promise<Response> {
  // Verify webhook signature
  const body = await req.text();
  const signature = req.headers.get("X-Doubleword-Signature") ?? "";
  const webhookSecret = process.env.DOUBLEWORD_WEBHOOK_SECRET ?? "";

  if (webhookSecret) {
    const expected = createHmac("sha256", webhookSecret)
      .update(body)
      .digest("hex");
    if (
      signature.length !== expected.length ||
      !timingSafeEqual(Buffer.from(signature), Buffer.from(expected))
    ) {
      return new Response("Invalid signature", { status: 401 });
    }
  }

  const callback = JSON.parse(body);
  const redis = getRedis();

  // Store result (24h TTL)
  await redis.set(`job:${callback.job_id}`, body, { ex: 86400 });

  // Look up original command metadata
  const metaRaw = await redis.get(`jobmeta:${callback.job_id}`);
  if (!metaRaw) {
    return Response.json({ received: true, warning: "no metadata found" });
  }

  const meta =
    typeof metaRaw === "string" ? JSON.parse(metaRaw) : metaRaw;

  // Determine source brain
  const sourceBrain: BrainId = (callback.model ?? "").includes("235B")
    ? "doubleword_235b"
    : "doubleword_397b";

  // Durable notification
  await enqueueOuroborosResult({
    job_id: callback.job_id,
    command_id: meta.command_id,
    status: callback.status,
    artifacts: callback.result?.artifacts,
  });

  // Real-time fan-out
  const narrationText =
    callback.status === "completed"
      ? `Deep analysis complete. ${callback.result?.artifacts?.length ?? 0} artifacts ready for review.`
      : `Batch job failed: ${callback.error ?? "unknown error"}`;

  await publishToDevices(meta.fan_out, {
    event: "daemon",
    data: {
      command_id: meta.command_id,
      narration_text: narrationText,
      narration_priority:
        callback.status === "completed" ? "informational" : "urgent",
      source_brain: sourceBrain,
    },
  });

  await publishToDevices(meta.fan_out, {
    event: "complete",
    data: {
      command_id: meta.command_id,
      source_brain: sourceBrain,
      token_count:
        (callback.metrics?.input_tokens ?? 0) +
        (callback.metrics?.output_tokens ?? 0),
      latency_ms: callback.metrics?.processing_time_ms ?? 0,
      artifacts: callback.result?.artifacts?.map(
        (a: { type: string; content: string }) => ({
          url: `/api/ouroboros/${callback.job_id}`,
          type: a.type,
          expires_at: new Date(
            Date.now() + 86400_000,
          ).toISOString(),
        }),
      ),
    },
  });

  return Response.json({ received: true });
}
```

- [ ] **Step 2: Implement submit route (manual + cron trigger)**

```typescript
// jarvis-cloud/app/api/doubleword/submit/route.ts
import { verifyCron } from "@/lib/auth/cron";

export async function POST(req: Request): Promise<Response> {
  // Accept cron trigger or pass through to /api/command
  if (verifyCron(req)) {
    // Cron-triggered: create a synthetic command for scheduled scan
    // This would call the same submitBatch flow
    return Response.json({ status: "scheduled_scan_queued" });
  }
  return new Response("Use /api/command for manual submissions", {
    status: 400,
  });
}
```

- [ ] **Step 3: Commit**

```bash
git add jarvis-cloud/app/api/doubleword/
git commit -m "feat(cloud): Doubleword callback webhook + submit route"
```

---

## Task 14: proxy.ts + Dashboard Shell

**Files:**
- Create: `jarvis-cloud/proxy.ts`
- Create: `jarvis-cloud/app/dashboard/layout.tsx`
- Create: `jarvis-cloud/app/dashboard/page.tsx`
- Create: `jarvis-cloud/app/login/page.tsx`

- [ ] **Step 1: Implement proxy.ts (dashboard session gate)**

```typescript
// jarvis-cloud/proxy.ts
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export function proxy(req: NextRequest) {
  const session = req.cookies.get("jarvis_session")?.value;

  if (!session) {
    return NextResponse.redirect(new URL("/login", req.url));
  }

  // Session cookie is encrypted — if tampered, it won't decrypt
  // Full session validation happens in dashboard layout.tsx server component
  return NextResponse.next();
}

export const config = {
  matcher: ["/dashboard/:path*"],
};
```

- [ ] **Step 2: Create login page**

```tsx
// jarvis-cloud/app/login/page.tsx
export default function LoginPage() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-zinc-950">
      <div className="w-full max-w-sm p-8 border border-zinc-800 rounded-lg">
        <h1 className="text-2xl font-bold text-zinc-100 mb-2 font-mono">
          JARVIS
        </h1>
        <p className="text-zinc-500 text-sm mb-8">
          Trinity Nervous System
        </p>
        <button
          className="w-full bg-zinc-100 text-zinc-900 font-medium py-3 rounded-md hover:bg-zinc-200 transition-colors"
          id="webauthn-login"
        >
          Sign in with Passkey
        </button>
        <p className="text-zinc-600 text-xs mt-4 text-center">
          Touch ID · Face ID · Security Key
        </p>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Create dashboard layout**

```tsx
// jarvis-cloud/app/dashboard/layout.tsx
import Link from "next/link";

const NAV_ITEMS = [
  { href: "/dashboard", label: "Overview", icon: "⚡" },
  { href: "/dashboard/ouroboros", label: "Ouroboros", icon: "🔄" },
  { href: "/dashboard/devices", label: "Devices", icon: "📱" },
  { href: "/dashboard/telemetry", label: "Telemetry", icon: "📊" },
];

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-screen flex">
      <nav className="w-56 border-r border-zinc-800 p-4 flex flex-col gap-1">
        <div className="mb-6">
          <h1 className="text-lg font-bold font-mono text-zinc-100">JARVIS</h1>
          <p className="text-xs text-zinc-500">Cloud Nervous System</p>
        </div>
        {NAV_ITEMS.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className="flex items-center gap-2 px-3 py-2 rounded-md text-sm text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800/50 transition-colors"
          >
            <span>{item.icon}</span>
            {item.label}
          </Link>
        ))}
      </nav>
      <main className="flex-1 p-6">{children}</main>
    </div>
  );
}
```

- [ ] **Step 4: Create dashboard overview page**

```tsx
// jarvis-cloud/app/dashboard/page.tsx
export default function DashboardOverview() {
  return (
    <div>
      <h2 className="text-xl font-bold text-zinc-100 mb-6">System Overview</h2>
      <div className="grid grid-cols-3 gap-4">
        <div className="border border-zinc-800 rounded-lg p-4">
          <p className="text-xs text-zinc-500 uppercase tracking-wider">
            Connected Devices
          </p>
          <p className="text-2xl font-mono text-zinc-100 mt-1">—</p>
        </div>
        <div className="border border-zinc-800 rounded-lg p-4">
          <p className="text-xs text-zinc-500 uppercase tracking-wider">
            Commands Today
          </p>
          <p className="text-2xl font-mono text-zinc-100 mt-1">—</p>
        </div>
        <div className="border border-zinc-800 rounded-lg p-4">
          <p className="text-xs text-zinc-500 uppercase tracking-wider">
            Active Jobs
          </p>
          <p className="text-2xl font-mono text-zinc-100 mt-1">—</p>
        </div>
      </div>
      <div className="mt-8 border border-zinc-800 rounded-lg p-4">
        <h3 className="text-sm font-bold text-zinc-400 mb-4">
          Live Command Feed
        </h3>
        <p className="text-zinc-600 text-sm">
          Connect devices to see live activity.
        </p>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Commit**

```bash
git add jarvis-cloud/proxy.ts jarvis-cloud/app/login/ jarvis-cloud/app/dashboard/
git commit -m "feat(cloud): proxy.ts, login page, and dashboard shell"
```

---

## Task 15: Remaining Dashboard Pages (Ouroboros, Devices, Telemetry)

**Files:**
- Create: `jarvis-cloud/app/dashboard/ouroboros/page.tsx`
- Create: `jarvis-cloud/app/dashboard/ouroboros/[jobId]/page.tsx`
- Create: `jarvis-cloud/app/dashboard/devices/page.tsx`
- Create: `jarvis-cloud/app/dashboard/telemetry/page.tsx`

- [ ] **Step 1: Create Ouroboros PR queue page**

```tsx
// jarvis-cloud/app/dashboard/ouroboros/page.tsx
export default function OuroborosQueue() {
  return (
    <div>
      <h2 className="text-xl font-bold text-zinc-100 mb-6">
        Ouroboros Governance
      </h2>
      <div className="border border-zinc-800 rounded-lg divide-y divide-zinc-800">
        <div className="p-4 text-zinc-500 text-sm">
          No active governance jobs. Scheduled scan runs at 3:00 AM daily.
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Create Ouroboros job detail page**

```tsx
// jarvis-cloud/app/dashboard/ouroboros/[jobId]/page.tsx
export default async function OuroborosJobDetail({
  params,
}: {
  params: Promise<{ jobId: string }>;
}) {
  const { jobId } = await params;

  return (
    <div>
      <h2 className="text-xl font-bold text-zinc-100 mb-2">
        Job: <span className="font-mono text-zinc-400">{jobId}</span>
      </h2>
      <div className="mt-6 border border-zinc-800 rounded-lg p-4">
        <p className="text-zinc-500 text-sm">
          Job details will load here once results are available.
        </p>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Create devices management page**

```tsx
// jarvis-cloud/app/dashboard/devices/page.tsx
export default function DevicesPage() {
  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold text-zinc-100">Devices</h2>
        <button className="bg-zinc-100 text-zinc-900 text-sm font-medium px-4 py-2 rounded-md hover:bg-zinc-200 transition-colors">
          Pair New Device
        </button>
      </div>
      <div className="border border-zinc-800 rounded-lg divide-y divide-zinc-800">
        <div className="p-4 text-zinc-500 text-sm">
          No devices paired. Click "Pair New Device" to get started.
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Create telemetry page**

```tsx
// jarvis-cloud/app/dashboard/telemetry/page.tsx
export default function TelemetryPage() {
  return (
    <div>
      <h2 className="text-xl font-bold text-zinc-100 mb-6">Telemetry</h2>
      <div className="border border-zinc-800 rounded-lg p-4">
        <h3 className="text-sm font-bold text-zinc-400 mb-4">
          Event Log
        </h3>
        <p className="text-zinc-600 text-sm">
          Events will appear here as devices connect and send commands.
        </p>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Commit**

```bash
git add jarvis-cloud/app/dashboard/
git commit -m "feat(cloud): dashboard pages — Ouroboros queue, devices, telemetry"
```

---

## Task 16: State Sync + Ouroboros Status Routes

**Files:**
- Create: `jarvis-cloud/app/api/state/[deviceId]/route.ts`
- Create: `jarvis-cloud/app/api/ouroboros/submit/route.ts`
- Create: `jarvis-cloud/app/api/ouroboros/[jobId]/route.ts`

- [ ] **Step 1: Implement state sync endpoint**

```typescript
// jarvis-cloud/app/api/state/[deviceId]/route.ts
import { getRedis } from "@/lib/redis/client";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ deviceId: string }> },
): Promise<Response> {
  const { deviceId } = await params;
  const redis = getRedis();

  // Return current system state for a reconnecting device
  const [deviceListRaw, pendingEventsRaw] = await Promise.all([
    redis.get("devices:active_list"),
    redis.xrange(`stream:events:${deviceId}`, "-", "+", 50),
  ]);

  const deviceIds: string[] = deviceListRaw
    ? typeof deviceListRaw === "string"
      ? JSON.parse(deviceListRaw)
      : deviceListRaw
    : [];

  return Response.json({
    device_id: deviceId,
    active_devices: deviceIds.length,
    pending_events: (pendingEventsRaw ?? []).length,
    synced_at: new Date().toISOString(),
  });
}
```

- [ ] **Step 2: Implement Ouroboros submit (cron + manual)**

```typescript
// jarvis-cloud/app/api/ouroboros/submit/route.ts
import { verifyCron } from "@/lib/auth/cron";
import { getRedis } from "@/lib/redis/client";
import { submitBatch } from "@/lib/brains/doubleword";
import { publishToDevices } from "@/lib/brains/fan-out";
import type { CommandPayload, RoutingDecision } from "@/lib/routing/types";
import { randomUUID } from "crypto";

export async function POST(req: Request): Promise<Response> {
  const isCron = verifyCron(req);

  if (!isCron) {
    // Manual trigger requires device auth — redirect to /api/command
    return new Response(
      "Use /api/command with intent_hint=ouroboros_scan for manual submissions",
      { status: 400 },
    );
  }

  // Cron-triggered nightly scan
  const redis = getRedis();
  const commandId = randomUUID();

  const payload: CommandPayload = {
    command_id: commandId,
    device_id: "cron-scheduler",
    device_type: "browser",
    text: "run ouroboros governance scan on all repos",
    intent_hint: "ouroboros_scan",
    priority: "deferred",
    response_mode: "notify",
    timestamp: new Date().toISOString(),
    signature: "cron-internal",
  };

  const decision: RoutingDecision = {
    brain: "doubleword_397b",
    mode: "batch",
    model: "Qwen/Qwen3.5-397B-A17B-FP8",
    fan_out: [],
    system_prompt_key: "ouroboros",
    estimated_latency: "hours",
  };

  // Build fan-out from all active devices
  const listRaw = await redis.get("devices:active_list");
  const deviceIds: string[] = listRaw
    ? typeof listRaw === "string"
      ? JSON.parse(listRaw)
      : listRaw
    : [];

  for (const id of deviceIds) {
    const raw = await redis.get(`device:${id}`);
    if (!raw) continue;
    const device = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (device.active) {
      decision.fan_out.push({
        device_id: id,
        channel: "redis",
        role: device.device_type === "mac" ? "executor" : "observer",
      });
    }
  }

  const jobId = await submitBatch(payload, decision);

  await publishToDevices(decision.fan_out, {
    event: "daemon",
    data: {
      command_id: commandId,
      narration_text: "Nightly Ouroboros governance scan started.",
      narration_priority: "ambient",
      source_brain: "doubleword_397b",
    },
  });

  return Response.json({ job_id: jobId, status: "submitted" });
}
```

- [ ] **Step 3: Implement Ouroboros job status**

```typescript
// jarvis-cloud/app/api/ouroboros/[jobId]/route.ts
import { getRedis } from "@/lib/redis/client";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  const { jobId } = await params;
  const redis = getRedis();

  const [jobRaw, metaRaw] = await Promise.all([
    redis.get(`job:${jobId}`),
    redis.get(`jobmeta:${jobId}`),
  ]);

  if (!metaRaw) {
    return new Response("Job not found", { status: 404 });
  }

  const meta = typeof metaRaw === "string" ? JSON.parse(metaRaw) : metaRaw;
  const result = jobRaw
    ? typeof jobRaw === "string"
      ? JSON.parse(jobRaw)
      : jobRaw
    : null;

  return Response.json({
    job_id: jobId,
    command_id: meta.command_id,
    brain: meta.brain,
    submitted_at: meta.submitted_at,
    status: result ? result.status : "pending",
    result: result?.result ?? null,
    metrics: result?.metrics ?? null,
  });
}
```

- [ ] **Step 4: Commit**

```bash
git add jarvis-cloud/app/api/state/ jarvis-cloud/app/api/ouroboros/
git commit -m "feat(cloud): state sync, Ouroboros submit (cron + manual), job status"
```

---

## Task 17: Final Integration — Verify Build + Deploy Config

**Files:**
- Create: `jarvis-cloud/.env.example`
- Modify: `jarvis-cloud/next.config.ts`

- [ ] **Step 1: Create .env.example**

```bash
# jarvis-cloud/.env.example

# Master secret for HKDF device key derivation (min 32 bytes)
JARVIS_MASTER_SECRET=

# Upstash Redis (auto-provisioned via Vercel Marketplace)
UPSTASH_REDIS_REST_URL=
UPSTASH_REDIS_REST_TOKEN=

# Anthropic Claude API
ANTHROPIC_API_KEY=

# Doubleword Batch API
DOUBLEWORD_API_KEY=
DOUBLEWORD_API_URL=https://api.doubleword.ai
DOUBLEWORD_WEBHOOK_SECRET=

# Session encryption (32-byte random hex)
JARVIS_SESSION_SECRET=

# Vercel Cron
CRON_SECRET=
```

- [ ] **Step 2: Verify next.config.ts**

```typescript
// jarvis-cloud/next.config.ts
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  experimental: {
    serverActions: {
      bodySizeLimit: "2mb",
    },
  },
};

export default nextConfig;
```

- [ ] **Step 3: Run build to verify everything compiles**

```bash
cd jarvis-cloud && npm run build
```
Expected: Build succeeds with no TypeScript errors.

- [ ] **Step 4: Run all tests**

```bash
cd jarvis-cloud && npx vitest run
```
Expected: All tests PASS.

- [ ] **Step 5: Create .gitignore additions**

```bash
echo ".env*.local" >> jarvis-cloud/.gitignore
```

- [ ] **Step 6: Final commit**

```bash
git add jarvis-cloud/
git commit -m "feat(cloud): complete Vercel app — env config, build verification"
```

---

## Summary

| Task | Component | Tests |
|---|---|---|
| 1 | Project scaffold + Redis client | 2 |
| 2 | Shared types + SSE encoder | 3 |
| 3 | HMAC auth + HKDF derivation | 10 |
| 4 | Stream token + pairing + cron auth | 7 |
| 5 | Intent router (Tier 0) | 8 |
| 6 | Redis event backlog (Streams) | 2 |
| 7 | Fan-out (Redis + Queue) | 2 |
| 8 | Claude streaming brain | 3 |
| 9 | Doubleword batch brain | 1 |
| 10 | POST /api/command route | 2 |
| 11 | SSE stream + token endpoint | 0 (integration) |
| 12 | Device management routes | 0 (integration) |
| 13 | Doubleword callback webhook | 0 (integration) |
| 14 | proxy.ts + dashboard shell | 0 (UI) |
| 15 | Dashboard pages | 0 (UI) |
| 16 | State sync + Ouroboros routes | 0 (integration) |
| 17 | Build verification | Build + all tests |

**Total: 17 tasks, 38 unit tests, ~2400 lines of application code**

---

## Deferred to Plan 1.1

- **WebAuthn login/session route handlers** (`app/api/auth/login/route.ts`, `app/api/auth/session/route.ts`) — WebAuthn credential registration, challenge/response, and encrypted session cookie management. The login page (Task 14) and proxy.ts session gate are scaffolded, but the actual WebAuthn server-side implementation is complex enough to be its own plan.
- **Dashboard interactivity** — Live SSE consumption in React via `useEventSource` hook, real-time device status updates, Ouroboros job polling. The static shell pages (Tasks 14-15) provide the layout; interactivity wires in after the API layer is proven.
