import { createHmac, timingSafeEqual } from "crypto";
import { getRedis } from "@/lib/redis/client";
import { publishToDevices } from "@/lib/brains/fan-out";
import { enqueueOuroborosResult } from "@/lib/queue/topics";
import type { BrainId } from "@/lib/routing/types";

export async function POST(req: Request): Promise<Response> {
  const body = await req.text();
  const signature = req.headers.get("X-Doubleword-Signature") ?? "";
  const webhookSecret = process.env.DOUBLEWORD_WEBHOOK_SECRET ?? "";

  if (webhookSecret) {
    const expected = createHmac("sha256", webhookSecret).update(body).digest("hex");
    if (signature.length !== expected.length || !timingSafeEqual(Buffer.from(signature), Buffer.from(expected))) {
      return new Response("Invalid signature", { status: 401 });
    }
  }

  const callback = JSON.parse(body);
  const redis = getRedis();
  await redis.set(`job:${callback.job_id}`, body, { ex: 86400 });

  const metaRaw = await redis.get(`jobmeta:${callback.job_id}`);
  if (!metaRaw) return Response.json({ received: true, warning: "no metadata found" });
  const meta = typeof metaRaw === "string" ? JSON.parse(metaRaw) : metaRaw;

  const sourceBrain: BrainId = (callback.model ?? "").includes("235B") ? "doubleword_235b" : "doubleword_397b";

  await enqueueOuroborosResult({
    job_id: callback.job_id,
    command_id: meta.command_id,
    status: callback.status,
    artifacts: callback.result?.artifacts,
  });

  const narrationText = callback.status === "completed"
    ? `Deep analysis complete. ${callback.result?.artifacts?.length ?? 0} artifacts ready for review.`
    : `Batch job failed: ${callback.error ?? "unknown error"}`;

  await publishToDevices(meta.fan_out, {
    event: "daemon",
    data: {
      command_id: meta.command_id,
      narration_text: narrationText,
      narration_priority: callback.status === "completed" ? "informational" : "urgent",
      source_brain: sourceBrain,
    },
  });

  await publishToDevices(meta.fan_out, {
    event: "complete",
    data: {
      command_id: meta.command_id,
      source_brain: sourceBrain,
      token_count: (callback.metrics?.input_tokens ?? 0) + (callback.metrics?.output_tokens ?? 0),
      latency_ms: callback.metrics?.processing_time_ms ?? 0,
    },
  });

  return Response.json({ received: true });
}
