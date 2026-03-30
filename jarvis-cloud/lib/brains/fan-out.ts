import { ulid } from "ulid";
import { getRedis } from "../redis/client";
import type { DeviceTarget } from "../routing/types";

export async function publishToDevices(
  targets: DeviceTarget[],
  event: { event: string; data: Record<string, unknown> },
): Promise<void> {
  const eventId = `${(event.data.command_id as string) ?? "sys"}:${ulid()}`;
  const redis = getRedis();
  await Promise.all(
    targets.map(async (target) => {
      if (target.channel === "redis") {
        const key = `stream:events:${target.device_id}`;
        const payload = JSON.stringify({ ...event, id: eventId });
        await redis.xadd(key, "*", { payload });
        await redis.xtrim(key, { strategy: "MAXLEN", threshold: 100 });
      } else {
        await enqueueForDevice(target.device_id, { ...event, id: eventId });
      }
    }),
  );
}

async function enqueueForDevice(
  deviceId: string,
  event: Record<string, unknown>,
): Promise<void> {
  const redis = getRedis();
  const key = `queue:durable:${deviceId}`;
  await redis.xadd(key, "*", { payload: JSON.stringify(event) });
  await redis.xtrim(key, { strategy: "MAXLEN", threshold: 500 });
}
