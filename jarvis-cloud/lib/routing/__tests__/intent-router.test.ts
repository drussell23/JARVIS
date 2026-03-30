// jarvis-cloud/lib/routing/__tests__/intent-router.test.ts
import { describe, it, expect } from "vitest";
import { resolveRoute } from "../intent-router";
import type { CommandPayload } from "../types";

function makePayload(overrides: Partial<CommandPayload> = {}): CommandPayload {
  return {
    command_id: "cmd-001",
    device_id: "watch-ultra2-derek",
    device_type: "watch",
    text: "hello jarvis",
    priority: "realtime",
    response_mode: "stream",
    timestamp: new Date().toISOString(),
    signature: "test",
    ...overrides,
  };
}

describe("resolveRoute — Tier 0", () => {
  it("routes 'run ouroboros scan' to doubleword_397b batch", () => {
    const decision = resolveRoute(makePayload({ text: "run ouroboros scan on reactor-core" }));
    expect(decision.brain).toBe("doubleword_397b");
    expect(decision.mode).toBe("batch");
  });

  it("routes 'deep analyze' to doubleword_397b batch", () => {
    const decision = resolveRoute(makePayload({ text: "deep analyze the auth module" }));
    expect(decision.brain).toBe("doubleword_397b");
    expect(decision.mode).toBe("batch");
  });

  it("routes 'generate code' to doubleword_397b batch", () => {
    const decision = resolveRoute(makePayload({ text: "generate implementation for login flow" }));
    expect(decision.brain).toBe("doubleword_397b");
    expect(decision.mode).toBe("batch");
  });

  it("routes 'what do you see' to doubleword_235b batch", () => {
    const decision = resolveRoute(makePayload({ text: "what do you see on the screen?" }));
    expect(decision.brain).toBe("doubleword_235b");
    expect(decision.mode).toBe("batch");
  });

  it("routes 'screenshot' to doubleword_235b batch", () => {
    const decision = resolveRoute(makePayload({ text: "take a screenshot and analyze it" }));
    expect(decision.brain).toBe("doubleword_235b");
    expect(decision.mode).toBe("batch");
  });

  it("defaults unmatched text to claude streaming", () => {
    const decision = resolveRoute(makePayload({ text: "what's the weather today?" }));
    expect(decision.brain).toBe("claude");
    expect(decision.mode).toBe("stream");
  });

  it("honors trusted intent_hint (short-circuits regex)", () => {
    const decision = resolveRoute(makePayload({
      text: "please do something",
      intent_hint: "ouroboros_scan",
    }));
    expect(decision.brain).toBe("doubleword_397b");
    expect(decision.mode).toBe("batch");
  });

  it("ignores untrusted intent_hint", () => {
    const decision = resolveRoute(makePayload({
      text: "hello",
      intent_hint: "evil_hack",
    }));
    expect(decision.brain).toBe("claude");
    expect(decision.mode).toBe("stream");
  });
});
