import { describe, it, expect, vi } from "vitest";

vi.stubEnv("JARVIS_MASTER_SECRET", "test-master-secret-at-least-32-bytes-long!!");

describe("deriveDeviceSecret", () => {
  it("derives a 64-char hex string", async () => {
    const { deriveDeviceSecret } = await import("../hkdf");
    const secret = await deriveDeviceSecret("device-abc", 1);
    expect(secret).toHaveLength(64);
    expect(secret).toMatch(/^[0-9a-f]{64}$/);
  });

  it("produces different secrets for different device IDs", async () => {
    const { deriveDeviceSecret } = await import("../hkdf");
    const a = await deriveDeviceSecret("device-abc", 1);
    const b = await deriveDeviceSecret("device-xyz", 1);
    expect(a).not.toBe(b);
  });

  it("produces different secrets for different versions", async () => {
    const { deriveDeviceSecret } = await import("../hkdf");
    const v1 = await deriveDeviceSecret("device-abc", 1);
    const v2 = await deriveDeviceSecret("device-abc", 2);
    expect(v1).not.toBe(v2);
  });

  it("is deterministic for same inputs", async () => {
    const { deriveDeviceSecret } = await import("../hkdf");
    const a = await deriveDeviceSecret("device-abc", 1);
    const b = await deriveDeviceSecret("device-abc", 1);
    expect(a).toBe(b);
  });
});
