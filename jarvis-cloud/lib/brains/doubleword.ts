import { getRedis } from "../redis/client";
import type { CommandPayload, RoutingDecision } from "../routing/types";
import { getSystemPrompt, buildMessages } from "./claude";

const DOUBLEWORD_API_URL =
  process.env.DOUBLEWORD_API_URL ?? "https://api.doubleword.ai";
const DOUBLEWORD_API_KEY = process.env.DOUBLEWORD_API_KEY ?? "";

export async function submitBatch(
  payload: CommandPayload,
  decision: RoutingDecision,
): Promise<string> {
  const redis = getRedis();
  const systemPrompt = getSystemPrompt(decision.system_prompt_key);
  const messages = buildMessages(payload);

  const jsonlContent = JSON.stringify({
    custom_id: payload.command_id,
    method: "POST",
    url: "/v1/chat/completions",
    body: {
      model: decision.model,
      messages: [
        { role: "system", content: systemPrompt },
        ...messages.map((m) => ({ role: m.role, content: m.content })),
      ],
      max_tokens: 8192,
    },
  });

  const uploadResponse = await fetch(`${DOUBLEWORD_API_URL}/v1/files`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${DOUBLEWORD_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ content: jsonlContent, purpose: "batch" }),
  });

  if (!uploadResponse.ok) {
    throw new Error(
      `Doubleword file upload failed: ${uploadResponse.status}`,
    );
  }
  const uploadResult = await uploadResponse.json();
  const fileId = uploadResult.id;

  const batchResponse = await fetch(`${DOUBLEWORD_API_URL}/v1/batches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${DOUBLEWORD_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      input_file_id: fileId,
      endpoint: "/v1/chat/completions",
      completion_window: "24h",
    }),
  });

  if (!batchResponse.ok) {
    throw new Error(
      `Doubleword batch creation failed: ${batchResponse.status}`,
    );
  }
  const batchResult = await batchResponse.json();
  const jobId = batchResult.id;

  await redis.set(
    `jobmeta:${jobId}`,
    JSON.stringify({
      command_id: payload.command_id,
      fan_out: decision.fan_out,
      brain: decision.brain,
      submitted_at: new Date().toISOString(),
    }),
    { ex: 86400 },
  );

  return jobId;
}
