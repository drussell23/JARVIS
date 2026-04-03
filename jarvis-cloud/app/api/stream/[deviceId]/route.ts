import { validateStreamToken } from "@/lib/auth/stream-token";
import { replayBacklog } from "@/lib/redis/event-backlog";
import { getRedis } from "@/lib/redis/client";
import { formatSSE } from "@/lib/sse/encoder";
import { accumulateHiveEvent } from "@/lib/hive/hive-state";

export const runtime = "nodejs";
export const maxDuration = 300;

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
      if (lastEventId) {
        try {
          const missed = await replayBacklog(deviceId, lastEventId);
          for (const event of missed) {
            controller.enqueue(encoder.encode(formatSSE(event.event, event.data, event.id)));
          }
        } catch { /* best-effort replay */ }
      }

      let cursor = lastEventId ?? "0";
      let heartbeatCounter = 0;
      const POLL_INTERVAL_MS = 100;
      const HEARTBEAT_EVERY = 150;

      while (!req.signal.aborted) {
        try {
          const exclusiveStart = cursor === "0" ? "0" : `${cursor.split("-")[0]}-${parseInt(cursor.split("-")[1] ?? "0", 10) + 1}`;
          const entries = await redis.xrange<Record<string, string>>(streamKey, exclusiveStart, "+", 50);
          const entryPairs = Object.entries(entries);

          for (const [id, fields] of entryPairs) {
            // Upstash REST auto-deserializes JSON — payload may be object or string
            const parsed = typeof fields.payload === "string"
              ? JSON.parse(fields.payload)
              : fields.payload;
            controller.enqueue(encoder.encode(formatSSE(parsed.event, parsed.data, id)));

            // Fire-and-forget: accumulate Hive-relevant events into Redis summary keys
            const hiveEventTypes = ["agent_log", "persona_reasoning", "thread_lifecycle", "cognitive_transition"];
            if (hiveEventTypes.includes(parsed.event)) {
              accumulateHiveEvent(parsed.event, parsed.data).catch(() => {});
            }

            cursor = id;
          }

          heartbeatCounter++;
          if (heartbeatCounter >= HEARTBEAT_EVERY) {
            controller.enqueue(encoder.encode(formatSSE("heartbeat", {})));
            heartbeatCounter = 0;
          }

          if (entryPairs.length === 0) {
            await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
          }
        } catch { break; }
      }
      controller.close();
    },
  });

  return new Response(stream, {
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive" },
  });
}
