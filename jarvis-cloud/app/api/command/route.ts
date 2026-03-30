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

  // 1. Replay protection
  const age = Math.abs(Date.now() - new Date(payload.timestamp).getTime()) / 1000;
  if (age > REPLAY_WINDOW_S) {
    return new Response("Timestamp expired", { status: 401 });
  }

  // 2. Device lookup
  const raw = await redis.get(`device:${payload.device_id}`);
  if (!raw) return new Response("Unknown device", { status: 401 });
  const device: DeviceRecord = typeof raw === "string" ? JSON.parse(raw) : raw;
  if (!device.active) return new Response("Device revoked", { status: 401 });

  // 3. HMAC verification
  const secret = await deriveDeviceSecret(payload.device_id, device.hkdf_version);
  if (!verifyHMAC(payload, secret)) {
    return new Response("Invalid signature", { status: 401 });
  }

  // 4. Route
  const decision = resolveRoute(payload);
  decision.fan_out = await buildFanOut(redis, payload.device_id);

  // 5. Update last_seen
  device.last_seen = new Date().toISOString();
  await redis.set(`device:${payload.device_id}`, JSON.stringify(device));

  // 6. Execute
  if (decision.mode === "stream") {
    const reserved = await redis.set(
      `cmd:${payload.command_id}`,
      JSON.stringify({ status: "in_flight", brain: decision.brain }),
      { nx: true, ex: 300 },
    );
    if (!reserved) return new Response("Command already in flight", { status: 409 });
    return streamClaude(payload, decision);
  } else {
    const cached = await redis.get(`cmd:${payload.command_id}`);
    if (cached) return Response.json(typeof cached === "string" ? JSON.parse(cached) : cached);

    const jobId = await submitBatch(payload, decision);
    await publishToDevices(decision.fan_out, {
      event: "status",
      data: { command_id: payload.command_id, phase: "queued", message: `Batch job ${jobId} submitted to ${decision.brain}` },
    });

    const result = { job_id: jobId, brain: decision.brain, status: "queued" };
    await redis.set(`cmd:${payload.command_id}`, JSON.stringify(result), { ex: 3600 });
    return Response.json(result);
  }
}

async function buildFanOut(redis: any, _excludeDeviceId: string) {
  // Include ALL devices in fan-out (including the sender).
  // The SSE stream is the canonical event delivery channel for every device.
  // The POST response also streams tokens to the requester, but the brainstem
  // discards that (returns {status: "streaming"}) and relies on SSE instead.
  const deviceListRaw = await redis.get("devices:active_list");
  if (!deviceListRaw) return [];
  const deviceIds: string[] = typeof deviceListRaw === "string" ? JSON.parse(deviceListRaw) : deviceListRaw;
  const targets = await Promise.all(
    deviceIds.map(async (id) => {
      const raw = await redis.get(`device:${id}`);
      if (!raw) return null;
      const device: DeviceRecord = typeof raw === "string" ? JSON.parse(raw) : raw;
      if (!device.active) return null;
      return { device_id: id, channel: "redis" as const, role: device.device_type === "mac" ? "executor" as const : "observer" as const };
    }),
  );
  return targets.filter(Boolean) as NonNullable<(typeof targets)[number]>[];
}
