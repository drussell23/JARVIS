import { describe, it, expect, vi, beforeEach } from "vitest";

const mockFetch = vi.fn();
global.fetch = mockFetch;

const mockRedis = {
  set: vi.fn().mockResolvedValue("OK"),
};

vi.mock("../../redis/client", () => ({
  getRedis: () => mockRedis,
}));

vi.stubEnv("DOUBLEWORD_API_KEY", "test-key");
vi.stubEnv("DOUBLEWORD_API_URL", "https://api.doubleword.ai");

describe("submitBatch", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("uploads file, creates batch, and stores job metadata", async () => {
    mockFetch
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ id: "file-001" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ id: "batch-001" }),
      });

    const { submitBatch } = await import("../doubleword");
    const jobId = await submitBatch(
      {
        command_id: "cmd-001",
        device_id: "mac-m1",
        text: "run ouroboros scan",
      } as any,
      {
        brain: "doubleword_397b",
        mode: "batch",
        model: "Qwen/Qwen3.5-397B-A17B-FP8",
        fan_out: [{ device_id: "mac-m1", channel: "redis", role: "executor" }],
        system_prompt_key: "ouroboros",
        estimated_latency: "minutes",
      },
    );

    expect(jobId).toBe("batch-001");
    expect(mockRedis.set).toHaveBeenCalledWith(
      "jobmeta:batch-001",
      expect.any(String),
      { ex: 86400 },
    );
  });
});
