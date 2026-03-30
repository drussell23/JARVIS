import { describe, it, expect, vi, beforeEach } from "vitest";

vi.stubEnv("UPSTASH_REDIS_REST_URL", "https://test.upstash.io");
vi.stubEnv("UPSTASH_REDIS_REST_TOKEN", "test-token");

describe("Redis client", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("returns a Redis instance", async () => {
    const { getRedis } = await import("../client");
    const client = getRedis();
    expect(client).toBeDefined();
  });

  it("returns the same singleton on repeated calls", async () => {
    const { getRedis } = await import("../client");
    const a = getRedis();
    const b = getRedis();
    expect(a).toBe(b);
  });
});
