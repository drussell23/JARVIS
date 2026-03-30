// jarvis-cloud/lib/routing/intent-router.ts
import type { CommandPayload, RoutingDecision, BrainId, RouteRule } from "./types";

const TRUSTED_HINTS: Record<string, { brain: BrainId; mode: "batch" }> = {
  ouroboros_scan: { brain: "doubleword_397b", mode: "batch" },
  ouroboros_review: { brain: "doubleword_397b", mode: "batch" },
  deep_analysis: { brain: "doubleword_397b", mode: "batch" },
  vision_capture: { brain: "doubleword_235b", mode: "batch" },
  code_generation: { brain: "doubleword_397b", mode: "batch" },
};

const TIER_0_ROUTES: RouteRule[] = [
  { pattern: /^(run |start |execute )?ouroboros/i, brain: "doubleword_397b", mode: "batch", model: "Qwen/Qwen3.5-397B-A17B-FP8", system_prompt_key: "ouroboros", estimated_latency: "minutes" },
  { pattern: /^(deep )?(scan|analyze|audit)/i, brain: "doubleword_397b", mode: "batch", model: "Qwen/Qwen3.5-397B-A17B-FP8", system_prompt_key: "analysis", estimated_latency: "minutes" },
  { pattern: /^generate (code|implementation|PR)/i, brain: "doubleword_397b", mode: "batch", model: "Qwen/Qwen3.5-397B-A17B-FP8", system_prompt_key: "codegen", estimated_latency: "minutes" },
  { pattern: /^(what do you see|analyze screen|describe)/i, brain: "doubleword_235b", mode: "batch", model: "Qwen/Qwen3.5-235B-Vision", system_prompt_key: "vision", estimated_latency: "minutes" },
  { pattern: /screenshot|screen capture|visual/i, brain: "doubleword_235b", mode: "batch", model: "Qwen/Qwen3.5-235B-Vision", system_prompt_key: "vision", estimated_latency: "minutes" },
];

// Haiku for real-time conversation (fast, cheap: $0.25/$1.25 per 1M)
// Sonnet reserved for complex tasks that need deeper reasoning
const CLAUDE_HAIKU_DEFAULT: RoutingDecision = {
  brain: "claude",
  mode: "stream",
  model: "claude-haiku-4-5-20251001",
  fan_out: [],
  system_prompt_key: "jarvis",
  estimated_latency: "realtime",
};

// Sonnet for commands that need more reasoning but still real-time
const CLAUDE_SONNET_ROUTES: RouteRule[] = [
  { pattern: /^(explain|debug|review|refactor|fix|architect)/i, brain: "claude", mode: "stream", model: "claude-sonnet-4-6", system_prompt_key: "jarvis", estimated_latency: "realtime" },
  { pattern: /\b(why|how does|what causes|trade.?offs?)\b.*\?$/i, brain: "claude", mode: "stream", model: "claude-sonnet-4-6", system_prompt_key: "jarvis", estimated_latency: "realtime" },
];

export function resolveRoute(payload: CommandPayload): RoutingDecision {
  // Fast-path: trusted intent_hint skips regex
  if (payload.intent_hint && payload.intent_hint in TRUSTED_HINTS) {
    const hint = TRUSTED_HINTS[payload.intent_hint];
    const matchingRule = TIER_0_ROUTES.find(r => r.brain === hint.brain);
    return {
      brain: hint.brain,
      mode: hint.mode,
      model: matchingRule?.model ?? "Qwen/Qwen3.5-397B-A17B-FP8",
      fan_out: [],
      system_prompt_key: matchingRule?.system_prompt_key ?? "default",
      estimated_latency: matchingRule?.estimated_latency ?? "minutes",
    };
  }

  // Tier 0: regex matching
  for (const rule of TIER_0_ROUTES) {
    if (rule.pattern.test(payload.text)) {
      return {
        brain: rule.brain,
        mode: rule.mode,
        model: rule.model,
        fan_out: [],
        system_prompt_key: rule.system_prompt_key,
        estimated_latency: rule.estimated_latency,
      };
    }
  }

  // Tier 0.5: Sonnet for complex real-time tasks
  for (const rule of CLAUDE_SONNET_ROUTES) {
    if (rule.pattern.test(payload.text)) {
      return {
        brain: rule.brain,
        mode: rule.mode,
        model: rule.model,
        fan_out: [],
        system_prompt_key: rule.system_prompt_key,
        estimated_latency: rule.estimated_latency,
      };
    }
  }

  // VLA: screenshot present → upgrade to Sonnet with vision prompt for better visual reasoning.
  // Haiku can handle vision but Sonnet produces significantly better spatial understanding
  // and action recommendations. This fires regardless of command text.
  if (payload.context?.screenshot) {
    return {
      brain: "claude",
      mode: "stream",
      model: "claude-sonnet-4-6",
      fan_out: [],
      system_prompt_key: "vision",
      estimated_latency: "realtime",
    };
  }

  // Default: Haiku streaming (fast + cheap for conversation)
  return { ...CLAUDE_HAIKU_DEFAULT };
}
