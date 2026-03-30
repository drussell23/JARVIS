import { describe, it, expect, vi, beforeEach } from "vitest";

const mockRedis = {
  xadd: vi.fn().mockResolvedValue("1234567890-0"),
  xtrim: vi.fn().mockResolvedValue(0),
};

vi.mock("../../redis/client", () => ({
  getRedis: () => mockRedis,
}));

describe("publishToDevices", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("publishes to Redis Stream for redis-channel targets", async () => {
    const { publishToDevices } = await import("../fan-out");
    await publishToDevices(
      [{ device_id: "mac-m1", channel: "redis", role: "executor" }],
      { event: "token", data: { command_id: "cmd-1", token: "hi" } },
    );
    expect(mockRedis.xadd).toHaveBeenCalledWith(
      "stream:events:mac-m1",
      "*",
      { payload: expect.any(String) },
    );
  });

  it("publishes to multiple targets in parallel", async () => {
    const { publishToDevices } = await import("../fan-out");
    await publishToDevices(
      [
        { device_id: "mac-m1", channel: "redis", role: "executor" },
        { device_id: "watch-ultra2", channel: "redis", role: "observer" },
      ],
      { event: "daemon", data: { narration_text: "hello" } },
    );
    expect(mockRedis.xadd).toHaveBeenCalledTimes(2);
  });
});
