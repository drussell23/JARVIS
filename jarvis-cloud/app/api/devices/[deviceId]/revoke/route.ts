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
  const device: DeviceRecord = typeof raw === "string" ? JSON.parse(raw) : raw;
  device.active = false;
  await redis.set(`device:${deviceId}`, JSON.stringify(device));
  return Response.json({ revoked: true, device_id: deviceId });
}
