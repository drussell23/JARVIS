import { hkdf } from "crypto";
import { promisify } from "util";

const hkdfAsync = promisify(hkdf);

/**
 * Derive a per-device secret from the master secret using RFC 5869 HKDF-SHA256.
 *
 * Extract:  salt = "jarvis-hkdf-salt-v1"
 * Expand:   info = "jarvis-device-v{version}:{deviceId}"
 * Output:   32 bytes → 64-char hex string
 */
export async function deriveDeviceSecret(
  deviceId: string,
  version: number,
): Promise<string> {
  const masterSecret = process.env.JARVIS_MASTER_SECRET;
  if (!masterSecret || masterSecret.length < 32) {
    throw new Error("JARVIS_MASTER_SECRET must be at least 32 bytes");
  }

  const okm = await hkdfAsync(
    "sha256",
    Buffer.from(masterSecret, "utf-8"),
    Buffer.from("jarvis-hkdf-salt-v1", "utf-8"),
    Buffer.from(`jarvis-device-v${version}:${deviceId}`, "utf-8"),
    32,
  );

  return Buffer.from(okm).toString("hex");
}
