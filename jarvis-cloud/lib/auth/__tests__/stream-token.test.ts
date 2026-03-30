import { describe, it, expect, vi, beforeEach } from "vitest";

const mockRedis = {
  set: vi.fn().mockResolvedValue("OK"),
  get: vi.fn(),
  del: vi.fn().mockResolvedValue(1),
};

vi.mock("../../redis/client", () => ({
  getRedis: () => mockRedis,
}));

describe("stream tokens", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("issueStreamToken stores token in Redis with 300s TTL", async () => {
    const { issueStreamToken } = await import("../stream-token");
    const token = await issueStreamToken("device-abc");
    expect(token).toBeTruthy();
    expect(mockRedis.set).toHaveBeenCalledWith(
      expect.stringMatching(/^ssetok:/),
      "device-abc",
      { ex: 300 },
    );
  });

  it("validateStreamToken returns true for valid token and deletes it", async () => {
    mockRedis.get.mockResolvedValue("device-abc");
    const { validateStreamToken } = await import("../stream-token");
    const result = await validateStreamToken("tok-123", "device-abc");
    expect(result).toBe(true);
    expect(mockRedis.del).toHaveBeenCalledWith("ssetok:tok-123");
  });

  it("validateStreamToken returns false for wrong device", async () => {
    mockRedis.get.mockResolvedValue("device-xyz");
    const { validateStreamToken } = await import("../stream-token");
    const result = await validateStreamToken("tok-123", "device-abc");
    expect(result).toBe(false);
    expect(mockRedis.del).not.toHaveBeenCalled();
  });

  it("validateStreamToken returns false for expired/missing token", async () => {
    mockRedis.get.mockResolvedValue(null);
    const { validateStreamToken } = await import("../stream-token");
    const result = await validateStreamToken("tok-expired", "device-abc");
    expect(result).toBe(false);
  });
});
