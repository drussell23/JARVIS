export const TOPICS = {
  OUROBOROS_COMPLETE: "ouroboros.complete",
  DOUBLEWORD_RESULT: "doubleword.result",
  DEVICE_NOTIFICATION: "device.notification",
} as const;

export async function enqueueOuroborosResult(payload: {
  job_id: string;
  command_id: string;
  status: string;
  artifacts?: unknown[];
}): Promise<void> {
  const { getRedis } = await import("../redis/client");
  const redis = getRedis();
  await redis.xadd("queue:ouroboros:results", "*", {
    payload: JSON.stringify(payload),
  });
}
