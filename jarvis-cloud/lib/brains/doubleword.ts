import { getRedis } from "../redis/client";
import type { CommandPayload, RoutingDecision } from "../routing/types";
import { getSystemPrompt, buildMessages } from "./claude";

// Doubleword base URL uses /v1 suffix (matches existing Python provider)
const DOUBLEWORD_BASE_URL =
  process.env.DOUBLEWORD_BASE_URL ??
  process.env.DOUBLEWORD_API_URL ??
  "https://api.doubleword.ai/v1";
const DOUBLEWORD_API_KEY = process.env.DOUBLEWORD_API_KEY ?? "";
const DOUBLEWORD_MODEL =
  process.env.DOUBLEWORD_MODEL ?? "Qwen/Qwen3.5-397B-A17B-FP8";
const DOUBLEWORD_COMPLETION_WINDOW =
  process.env.DOUBLEWORD_WINDOW ?? "1h";

export async function submitBatch(
  payload: CommandPayload,
  decision: RoutingDecision,
): Promise<string> {
  const redis = getRedis();
  const systemPrompt = getSystemPrompt(decision.system_prompt_key);
  const messages = buildMessages(payload);

  // Build JSONL content (one line per request, matching Doubleword's batch format)
  const jsonlContent = JSON.stringify({
    custom_id: payload.command_id,
    method: "POST",
    url: "/v1/chat/completions",
    body: {
      model: decision.model || DOUBLEWORD_MODEL,
      messages: [
        { role: "system", content: systemPrompt },
        ...messages.map((m) => ({ role: m.role, content: m.content })),
      ],
      max_tokens: 10000,
      temperature: 0.2,
    },
  });

  // Stage 1: Upload JSONL file via multipart form data
  // (matches existing Python DoublewordProvider._upload_file)
  const formData = new FormData();
  formData.append(
    "file",
    new Blob([jsonlContent], { type: "application/jsonl" }),
    "batch_input.jsonl",
  );
  formData.append("purpose", "batch");

  const uploadResponse = await fetch(`${DOUBLEWORD_BASE_URL}/files`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${DOUBLEWORD_API_KEY}`,
    },
    body: formData,
  });

  if (!uploadResponse.ok) {
    const errBody = await uploadResponse.text();
    throw new Error(
      `Doubleword file upload failed: ${uploadResponse.status} ${errBody.slice(0, 200)}`,
    );
  }
  const uploadResult = await uploadResponse.json();
  const fileId = uploadResult.id;

  // Stage 2: Create batch job
  const batchResponse = await fetch(`${DOUBLEWORD_BASE_URL}/batches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${DOUBLEWORD_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      input_file_id: fileId,
      endpoint: "/v1/chat/completions",
      completion_window: DOUBLEWORD_COMPLETION_WINDOW,
    }),
  });

  if (!batchResponse.ok) {
    const errBody = await batchResponse.text();
    throw new Error(
      `Doubleword batch creation failed: ${batchResponse.status} ${errBody.slice(0, 200)}`,
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
