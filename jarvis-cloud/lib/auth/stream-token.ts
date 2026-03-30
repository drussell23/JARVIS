import { randomUUID } from "crypto";
import { getRedis } from "../redis/client";

const STREAM_TOKEN_TTL = 300;
const KEY_PREFIX = "ssetok:";

export async function issueStreamToken(deviceId: string): Promise<string> {
  const redis = getRedis();
  const token = randomUUID();
  await redis.set(`${KEY_PREFIX}${token}`, deviceId, { ex: STREAM_TOKEN_TTL });
  return token;
}

export async function validateStreamToken(
  token: string,
  deviceId: string,
): Promise<boolean> {
  const redis = getRedis();
  const stored = await redis.get(`${KEY_PREFIX}${token}`);
  if (stored !== deviceId) return false;
  await redis.del(`${KEY_PREFIX}${token}`);
  return true;
}
