import { describe, it, expect } from "vitest";
import { canonicalize, signPayload, verifyHMAC } from "../hmac";
import type { CommandPayload } from "../../routing/types";

const TEST_SECRET = "a".repeat(64); // 32-byte hex secret

const basePayload: Omit<CommandPayload, "signature"> = {
  command_id: "cmd-001",
  device_id: "watch-ultra2-derek",
  device_type: "watch",
  text: "refactor the auth module",
  priority: "realtime",
  response_mode: "stream",
  timestamp: "2026-03-29T18:45:00Z",
};

describe("canonicalize", () => {
  it("produces alphabetically sorted key=value pairs joined by &", () => {
    const result = canonicalize(basePayload as CommandPayload);
    expect(result).toBe(
      "command_id=cmd-001&device_id=watch-ultra2-derek&device_type=watch&" +
      "priority=realtime&response_mode=stream&text=refactor the auth module&" +
      "timestamp=2026-03-29T18:45:00Z"
    );
  });

  it("includes intent_hint when present", () => {
    const payload = { ...basePayload, intent_hint: "ouroboros_scan" } as CommandPayload;
    const result = canonicalize(payload);
    expect(result).toContain("intent_hint=ouroboros_scan");
  });

  it("includes sorted JSON context when present", () => {
    const payload = {
      ...basePayload,
      context: { location: "office", battery_level: 72 },
    } as CommandPayload;
    const result = canonicalize(payload);
    expect(result).toContain('context={"battery_level":72,"location":"office"}');
  });
});

describe("signPayload + verifyHMAC", () => {
  it("produces a valid signature that verifies", () => {
    const signature = signPayload(basePayload as CommandPayload, TEST_SECRET);
    expect(signature).toMatch(/^[0-9a-f]{64}$/);

    const payload = { ...basePayload, signature } as CommandPayload;
    expect(verifyHMAC(payload, TEST_SECRET)).toBe(true);
  });

  it("rejects a tampered payload", () => {
    const signature = signPayload(basePayload as CommandPayload, TEST_SECRET);
    const tampered = { ...basePayload, text: "rm -rf /", signature } as CommandPayload;
    expect(verifyHMAC(tampered, TEST_SECRET)).toBe(false);
  });

  it("rejects a wrong secret", () => {
    const signature = signPayload(basePayload as CommandPayload, TEST_SECRET);
    const payload = { ...basePayload, signature } as CommandPayload;
    expect(verifyHMAC(payload, "b".repeat(64))).toBe(false);
  });
});
