import { describe, it, expect } from "vitest";
import { buildMessages, getSystemPrompt } from "../claude";
import type { CommandPayload } from "../../routing/types";

describe("Claude helpers", () => {
  it("buildMessages creates a user message from command text", () => {
    const payload = {
      text: "refactor the auth module",
      context: { active_app: "VSCode", active_file: "/src/auth.ts" },
    } as CommandPayload;
    const messages = buildMessages(payload);
    expect(messages).toHaveLength(1);
    expect(messages[0].role).toBe("user");
    expect(messages[0].content).toContain("refactor the auth module");
    expect(messages[0].content).toContain("VSCode");
    expect(messages[0].content).toContain("/src/auth.ts");
  });

  it("getSystemPrompt returns JARVIS persona for 'jarvis' key", () => {
    const prompt = getSystemPrompt("jarvis");
    expect(prompt).toContain("JARVIS");
    expect(prompt.length).toBeGreaterThan(50);
  });

  it("getSystemPrompt returns analysis persona for 'analysis' key", () => {
    const prompt = getSystemPrompt("analysis");
    expect(prompt).toContain("analy");
  });
});
