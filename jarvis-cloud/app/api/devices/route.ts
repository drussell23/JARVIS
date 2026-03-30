import { getRedis } from "@/lib/redis/client";
import type { DeviceRecord } from "@/lib/routing/types";

export async function GET(): Promise<Response> {
  const redis = getRedis();
  const listRaw = await redis.get("devices:active_list");
  if (!listRaw) return Response.json({ devices: [] });
  const deviceIds: string[] = typeof listRaw === "string" ? JSON.parse(listRaw) : listRaw;
  const devices = await Promise.all(
    deviceIds.map(async (id) => {
      const raw = await redis.get(`device:${id}`);
      if (!raw) return null;
      return typeof raw === "string" ? JSON.parse(raw) : raw;
    }),
  );
  return Response.json({ devices: devices.filter(Boolean) as DeviceRecord[] });
}
