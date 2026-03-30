import { getRedis } from "@/lib/redis/client";
import { verifyCron } from "@/lib/auth/cron";
import type { DeviceRecord } from "@/lib/routing/types";

const STALE_THRESHOLD_MS = 7 * 24 * 60 * 60 * 1000;

export async function GET(req: Request): Promise<Response> {
  if (!verifyCron(req)) return new Response("Unauthorized", { status: 401 });
  const redis = getRedis();
  const listRaw = await redis.get("devices:active_list");
  if (!listRaw) return Response.json({ pruned: 0 });
  const deviceIds: string[] = typeof listRaw === "string" ? JSON.parse(listRaw) : listRaw;

  let pruned = 0;
  for (const id of deviceIds) {
    const raw = await redis.get(`device:${id}`);
    if (!raw) continue;
    const device: DeviceRecord = typeof raw === "string" ? JSON.parse(raw) : raw;
    const lastSeen = new Date(device.last_seen).getTime();
    if (Date.now() - lastSeen > STALE_THRESHOLD_MS) {
      device.active = false;
      await redis.set(`device:${id}`, JSON.stringify(device));
      pruned++;
    }
  }
  return Response.json({ pruned, checked: deviceIds.length });
}
