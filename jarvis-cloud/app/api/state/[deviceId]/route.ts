import { getRedis } from "@/lib/redis/client";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ deviceId: string }> },
): Promise<Response> {
  const { deviceId } = await params;
  const redis = getRedis();
  const [deviceListRaw, pendingEventsRaw] = await Promise.all([
    redis.get("devices:active_list"),
    redis.xrange(`stream:events:${deviceId}`, "-", "+", 50),
  ]);
  const deviceIds: string[] = deviceListRaw ? (typeof deviceListRaw === "string" ? JSON.parse(deviceListRaw) : deviceListRaw) : [];
  return Response.json({
    device_id: deviceId,
    active_devices: deviceIds.length,
    pending_events: (pendingEventsRaw ?? []).length,
    synced_at: new Date().toISOString(),
  });
}
