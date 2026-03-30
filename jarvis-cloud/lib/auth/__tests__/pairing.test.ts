import { describe, it, expect, vi, beforeEach } from "vitest";

const mockRedis = {
  set: vi.fn().mockResolvedValue("OK"),
  get: vi.fn(),
  del: vi.fn().mockResolvedValue(1),
};

vi.mock("../../redis/client", () => ({
  getRedis: () => mockRedis,
}));

vi.stubEnv("JARVIS_MASTER_SECRET", "test-master-secret-at-least-32-bytes-long!!");

describe("pairing", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("generatePairingCode produces 8-char alphanumeric code", async () => {
    const { generatePairingCode } = await import("../pairing");
    const code = await generatePairingCode("session-1", "watch");
    expect(code).toMatch(/^[A-Z0-9]{8}$/);
    expect(mockRedis.set).toHaveBeenCalledWith(
      expect.stringMatching(/^pairing:/),
      expect.any(String),
      { ex: 300 },
    );
  });

  it("validatePairingCode succeeds with correct code", async () => {
    mockRedis.get.mockResolvedValue(JSON.stringify({
      code: "ABCD1234",
      created_by_session: "session-1",
      created_at: new Date().toISOString(),
      attempts_remaining: 3,
      device_type_hint: "watch",
    }));
    const { validatePairingCode } = await import("../pairing");
    const result = await validatePairingCode("ABCD1234", "device-new");
    expect(result.success).toBe(true);
    expect(result.device_secret).toBeTruthy();
    expect(result.device_secret).toHaveLength(64);
  });

  it("validatePairingCode fails with wrong code", async () => {
    mockRedis.get.mockResolvedValue(null);
    const { validatePairingCode } = await import("../pairing");
    const result = await validatePairingCode("WRONG123", "device-new");
    expect(result.success).toBe(false);
  });
});
