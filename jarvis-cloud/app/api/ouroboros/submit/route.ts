import { verifyCron } from "@/lib/auth/cron";
import { getRedis } from "@/lib/redis/client";
import { submitBatch } from "@/lib/brains/doubleword";
import { publishToDevices } from "@/lib/brains/fan-out";
import type { CommandPayload, RoutingDecision } from "@/lib/routing/types";
import { randomUUID } from "crypto";

export async function POST(req: Request): Promise<Response> {
  const isCron = verifyCron(req);
  if (!isCron) {
    return new Response("Use /api/command with intent_hint=ouroboros_scan for manual submissions", { status: 400 });
  }

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

  const listRaw = await redis.get("devices:active_list");
  const deviceIds: string[] = listRaw ? (typeof listRaw === "string" ? JSON.parse(listRaw) : listRaw) : [];
  for (const id of deviceIds) {
    const raw = await redis.get(`device:${id}`);
    if (!raw) continue;
    const device = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (device.active) {
      decision.fan_out.push({ device_id: id, channel: "redis", role: device.device_type === "mac" ? "executor" : "observer" });
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
