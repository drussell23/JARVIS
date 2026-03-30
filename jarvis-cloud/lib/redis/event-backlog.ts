import { getRedis } from "./client";

const MAX_BACKLOG = 100;
const MAX_REPLAY = 50;

export async function appendToBacklog(
  deviceId: string,
  eventId: string,
  event: { event: string; data: Record<string, unknown> },
): Promise<void> {
  const redis = getRedis();
  const key = `stream:events:${deviceId}`;
  await redis.xadd(key, "*", {
    payload: JSON.stringify({ ...event, id: eventId }),
  });
  await redis.xtrim(key, { strategy: "MAXLEN", threshold: MAX_BACKLOG });
}

export interface ReplayedEvent {
  id: string;
  event: string;
  data: Record<string, unknown>;
}

export async function replayBacklog(
  deviceId: string,
  lastEventId: string,
): Promise<ReplayedEvent[]> {
  const redis = getRedis();
  const key = `stream:events:${deviceId}`;
  const parts = lastEventId.split("-");
  const exclusiveStart =
    parts.length === 2
      ? `${parts[0]}-${parseInt(parts[1], 10) + 1}`
      : `${lastEventId}-1`;
  const entries = await redis.xrange<Record<string, string>>(key, exclusiveStart, "+", MAX_REPLAY);
  return Object.entries(entries).map(([id, fields]) => {
    const parsed = JSON.parse(fields.payload);
    return { id, event: parsed.event, data: parsed.data };
  });
}
