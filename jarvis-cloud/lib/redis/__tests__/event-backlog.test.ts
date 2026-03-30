import { describe, it, expect, vi, beforeEach } from "vitest";

const mockRedis = {
  xadd: vi.fn().mockResolvedValue("1234567890-0"),
  xrange: vi.fn().mockResolvedValue([]),
  xtrim: vi.fn().mockResolvedValue(0),
};

vi.mock("../client", () => ({
  getRedis: () => mockRedis,
}));

describe("event backlog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("appendToBacklog calls XADD with correct key and XTRIM", async () => {
    const { appendToBacklog } = await import("../event-backlog");
    await appendToBacklog("device-abc", "evt-1", {
      event: "token",
      data: { command_id: "cmd-1", token: "hi" },
    });
    expect(mockRedis.xadd).toHaveBeenCalledWith(
      "stream:events:device-abc",
      "*",
      { payload: expect.any(String) },
    );
    expect(mockRedis.xtrim).toHaveBeenCalledWith(
      "stream:events:device-abc",
      { strategy: "MAXLEN", threshold: 100 },
    );
  });

  it("replayBacklog calls XRANGE from lastEventId", async () => {
    mockRedis.xrange.mockResolvedValue({
      "1234567891-0": { payload: JSON.stringify({ event: "token", data: { token: "x" } }) },
    });
    const { replayBacklog } = await import("../event-backlog");
    const events = await replayBacklog("device-abc", "1234567890-0");
    expect(events).toHaveLength(1);
    expect(events[0].event).toBe("token");
    expect(mockRedis.xrange).toHaveBeenCalledWith(
      "stream:events:device-abc",
      "1234567890-1",
      "+",
      50,
    );
  });
});
