import Anthropic from "@anthropic-ai/sdk";
import { formatSSE } from "../sse/encoder";
import { publishToDevices } from "./fan-out";
import type { CommandPayload, RoutingDecision } from "../routing/types";

const anthropic = new Anthropic();

const SYSTEM_PROMPTS: Record<string, string> = {
  jarvis: `You are JARVIS, Derek's AI assistant. You are concise, technical, and proactive. You have access to execute actions on Derek's Mac (Ghost Hands clicks, file edits, terminal commands) via structured action events. When a command requires local execution, emit action events in your response. Be direct and efficient.`,
  analysis: `You are JARVIS in deep analysis mode. Provide thorough, structured analysis of code, architecture, and systems. Be detailed and systematic.`,
  codegen: `You are JARVIS in code generation mode. Generate production-quality, well-tested code. Follow existing patterns and conventions.`,
  ouroboros: `You are JARVIS Ouroboros governance engine. Analyze codebases for improvements, security issues, and optimization opportunities. Propose concrete changes as diffs.`,
  vision: `You are JARVIS vision system. Describe what you see on screen accurately and concisely. Identify UI elements, text, and layout.`,
  default: `You are JARVIS, a helpful AI assistant.`,
};

export function getSystemPrompt(key: string): string {
  return SYSTEM_PROMPTS[key] ?? SYSTEM_PROMPTS.default;
}

export function buildMessages(payload: CommandPayload): Anthropic.MessageParam[] {
  let content = payload.text;
  if (payload.context) {
    const ctx = payload.context;
    const parts: string[] = [];
    if (ctx.active_app) parts.push(`Active app: ${ctx.active_app}`);
    if (ctx.active_file) parts.push(`Active file: ${ctx.active_file}`);
    if (ctx.screen_summary) parts.push(`Screen: ${ctx.screen_summary}`);
    if (ctx.location) parts.push(`Location: ${ctx.location}`);
    if (parts.length > 0) {
      content = `[Context: ${parts.join(", ")}]\n\n${content}`;
    }
  }
  return [{ role: "user", content }];
}

export function streamClaude(
  payload: CommandPayload,
  decision: RoutingDecision,
): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      const startTime = Date.now();
      let sequence = 0;
      try {
        const response = anthropic.messages.stream({
          model: decision.model,
          max_tokens: 4096,
          system: getSystemPrompt(decision.system_prompt_key),
          messages: buildMessages(payload),
        });

        for await (const event of response) {
          if (
            event.type === "content_block_delta" &&
            event.delta.type === "text_delta"
          ) {
            const token = event.delta.text;
            sequence++;
            controller.enqueue(
              encoder.encode(
                formatSSE("token", {
                  command_id: payload.command_id,
                  token,
                  source_brain: "claude",
                  sequence,
                }),
              ),
            );
            // Fan-out tokens to ALL devices (including sender).
            // The SSE stream is the canonical delivery channel.
            await publishToDevices(decision.fan_out, {
              event: "token",
              data: {
                command_id: payload.command_id,
                token,
                source_brain: "claude",
                sequence,
              },
            });
          }
        }

        const finalMsg = await response.finalMessage();
        const complete = {
          command_id: payload.command_id,
          source_brain: "claude" as const,
          token_count: finalMsg.usage.input_tokens + finalMsg.usage.output_tokens,
          latency_ms: Date.now() - startTime,
        };
        controller.enqueue(encoder.encode(formatSSE("complete", complete)));
        await publishToDevices(decision.fan_out, {
          event: "complete",
          data: complete,
        });
      } catch (err) {
        const errorEvent = {
          command_id: payload.command_id,
          narration_text: `Command failed: ${err instanceof Error ? err.message : "unknown error"}`,
          narration_priority: "urgent" as const,
          source_brain: "claude" as const,
        };
        controller.enqueue(encoder.encode(formatSSE("daemon", errorEvent)));
        await publishToDevices(decision.fan_out, {
          event: "daemon",
          data: errorEvent,
        });
      } finally {
        controller.close();
      }
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Command-ID": payload.command_id,
    },
  });
}
