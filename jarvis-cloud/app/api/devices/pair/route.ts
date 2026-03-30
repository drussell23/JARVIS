import { getRedis } from "@/lib/redis/client";
import { validatePairingCode } from "@/lib/auth/pairing";
import type { DeviceRecord, DeviceType } from "@/lib/routing/types";

export async function POST(req: Request): Promise<Response> {
  const redis = getRedis();
  const body = await req.json();
  const { pairing_code, device_id, device_type, device_name, push_token } = body as {
    pairing_code: string; device_id: string; device_type: DeviceType; device_name: string; push_token?: string;
  };

  if (!pairing_code || !device_id || !device_type || !device_name) {
    return new Response("Missing required fields", { status: 400 });
  }

  const result = await validatePairingCode(pairing_code, device_id);
  if (!result.success || !result.device_secret) {
    return new Response("Invalid or expired pairing code", { status: 401 });
  }

  const device: DeviceRecord = {
    device_id, device_type, device_name,
    paired_at: new Date().toISOString(),
    last_seen: new Date().toISOString(),
    push_token,
    role: device_type === "mac" ? "executor" : "observer",
    active: true,
    hkdf_version: 1,
  };

  await redis.set(`device:${device_id}`, JSON.stringify(device));

  const listRaw = await redis.get("devices:active_list");
  const list: string[] = listRaw ? (typeof listRaw === "string" ? JSON.parse(listRaw) : listRaw) : [];
  if (!list.includes(device_id)) {
    list.push(device_id);
    await redis.set("devices:active_list", JSON.stringify(list));
  }

  const baseUrl = process.env.VERCEL_URL ? `https://${process.env.VERCEL_URL}` : "http://localhost:3000";
  return Response.json({
    device_secret: result.device_secret,
    stream_endpoint: `${baseUrl}/api/stream/${device_id}`,
    command_endpoint: `${baseUrl}/api/command`,
  });
}
