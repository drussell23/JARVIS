import { describe, it, expect } from "vitest";
import { formatSSE } from "../encoder";

describe("formatSSE", () => {
  it("formats a basic event with data", () => {
    const result = formatSSE("token", { command_id: "abc", token: "hello" });
    expect(result).toBe(
      'event:token\ndata:{"command_id":"abc","token":"hello"}\n\n'
    );
  });

  it("includes id when provided", () => {
    const result = formatSSE("status", { phase: "routing" }, "evt-123");
    expect(result).toBe(
      'id:evt-123\nevent:status\ndata:{"phase":"routing"}\n\n'
    );
  });

  it("formats heartbeat with empty data", () => {
    const result = formatSSE("heartbeat", {});
    expect(result).toBe("event:heartbeat\ndata:{}\n\n");
  });
});
