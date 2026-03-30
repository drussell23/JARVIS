import { createHmac, timingSafeEqual } from "crypto";
import type { CommandPayload } from "../routing/types";

const CANONICAL_FIELDS = [
  "command_id",
  "device_id",
  "device_type",
  "priority",
  "response_mode",
  "text",
  "timestamp",
] as const;

/**
 * Produce the canonical byte string for HMAC signing.
 * Fields are alphabetical. context is sorted-key JSON. intent_hint included when present.
 */
export function canonicalize(payload: CommandPayload): string {
  const parts: string[] = CANONICAL_FIELDS.map(
    (k) => `${k}=${payload[k]}`,
  );

  // intent_hint comes between device_type and priority alphabetically
  if (payload.intent_hint) {
    parts.splice(3, 0, `intent_hint=${payload.intent_hint}`);
  }

  if (payload.context) {
    const sortedKeys = Object.keys(payload.context).sort();
    const sorted: Record<string, unknown> = {};
    for (const k of sortedKeys) {
      sorted[k] = (payload.context as Record<string, unknown>)[k];
    }
    parts.push(`context=${JSON.stringify(sorted)}`);
  }

  return parts.join("&");
}

/**
 * Sign a payload with HMAC-SHA256.
 * @param secret — 64-char hex string (32-byte device secret)
 */
export function signPayload(
  payload: CommandPayload,
  secret: string,
): string {
  const canonical = canonicalize(payload);
  return createHmac("sha256", Buffer.from(secret, "hex"))
    .update(Buffer.from(canonical, "utf-8"))
    .digest("hex");
}

/**
 * Verify a signed payload using timing-safe comparison.
 */
export function verifyHMAC(
  payload: CommandPayload,
  secret: string,
): boolean {
  const expected = signPayload(payload, secret);
  const actual = payload.signature;
  if (expected.length !== actual.length) return false;
  return timingSafeEqual(
    Buffer.from(expected, "hex"),
    Buffer.from(actual, "hex"),
  );
}
