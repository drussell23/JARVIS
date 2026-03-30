import { randomBytes } from "crypto";
import { getRedis } from "../redis/client";
import { deriveDeviceSecret } from "./hkdf";
import type { DeviceType, PairingSession } from "../routing/types";

const PAIRING_TTL = 300;
const MAX_ATTEMPTS = 3;
const KEY_PREFIX = "pairing:";

export async function generatePairingCode(
  sessionId: string,
  deviceTypeHint: DeviceType,
): Promise<string> {
  const redis = getRedis();
  const code = randomBytes(4).toString("hex").toUpperCase().slice(0, 8);

  const session: PairingSession = {
    code,
    created_by_session: sessionId,
    created_at: new Date().toISOString(),
    attempts_remaining: MAX_ATTEMPTS,
    device_type_hint: deviceTypeHint,
  };

  await redis.set(`${KEY_PREFIX}${code}`, JSON.stringify(session), {
    ex: PAIRING_TTL,
  });

  return code;
}

export async function validatePairingCode(
  code: string,
  deviceId: string,
): Promise<{ success: boolean; device_secret?: string }> {
  const redis = getRedis();
  const raw = await redis.get(`${KEY_PREFIX}${code}`);
  if (!raw) return { success: false };

  const session: PairingSession =
    typeof raw === "string" ? JSON.parse(raw) : (raw as PairingSession);

  if (session.attempts_remaining <= 0) {
    await redis.del(`${KEY_PREFIX}${code}`);
    return { success: false };
  }

  const deviceSecret = await deriveDeviceSecret(deviceId, 1);
  await redis.del(`${KEY_PREFIX}${code}`);

  return { success: true, device_secret: deviceSecret };
}
