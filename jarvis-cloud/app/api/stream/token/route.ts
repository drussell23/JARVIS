import { getRedis } from "@/lib/redis/client";
import { verifyHMAC } from "@/lib/auth/hmac";
import { deriveDeviceSecret } from "@/lib/auth/hkdf";
import { issueStreamToken } from "@/lib/auth/stream-token";
import type { DeviceRecord } from "@/lib/routing/types";

export async function POST(req: Request): Promise<Response> {
  const redis = getRedis();
  const body = await req.json();
  const { device_id, signature, timestamp } = body;

  const raw = await redis.get(`device:${device_id}`);
  if (!raw) return new Response("Unknown device", { status: 401 });
  const device: DeviceRecord = typeof raw === "string" ? JSON.parse(raw) : raw;
  if (!device.active) return new Response("Device revoked", { status: 401 });

  const secret = await deriveDeviceSecret(device_id, device.hkdf_version);
  const payload = { ...body, command_id: "stream-token", device_type: device.device_type, text: "stream-token-request", priority: "realtime", response_mode: "stream" };
  if (!verifyHMAC(payload, secret)) return new Response("Invalid signature", { status: 401 });

  const token = await issueStreamToken(device_id);
  const baseUrl = process.env.VERCEL_URL ? `https://${process.env.VERCEL_URL}` : "http://localhost:3000";
  return Response.json({ token, stream_url: `${baseUrl}/api/stream/${device_id}?t=${token}` });
}
