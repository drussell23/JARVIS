// jarvis-cloud/lib/hive/hive-state.ts
// Redis-backed Hive state accumulator.
// Tracks cognitive state, active threads, and resolved threads from SSE events.

import { getRedis } from "@/lib/redis/client";

const HIVE_STATE_KEY = "hive:state";
const HIVE_THREADS_KEY = "hive:threads";
const HIVE_RESOLVED_KEY = "hive:resolved";

export interface HiveThreadSummary {
  title: string;
  state: string;
  resolved_at?: string;
  outcome?: string;
}

export interface HiveSummary {
  cognitive_state: string;
  active_threads: HiveThreadSummary[];
  recent_resolved: HiveThreadSummary[];
  stats: {
    total_threads_today: number;
    tokens_consumed_today: number;
    debates_resolved_today: number;
  };
}

export async function getHiveSummary(): Promise<HiveSummary> {
  const redis = getRedis();

  const [stateRaw, threadsRaw, resolvedRaw] = await Promise.all([
    redis.get<string>(HIVE_STATE_KEY),
    redis.hgetall<Record<string, string>>(HIVE_THREADS_KEY),
    redis.lrange(HIVE_RESOLVED_KEY, 0, 4),
  ]);

  const cognitiveState = stateRaw || "baseline";

  const activeThreads: HiveThreadSummary[] = [];
  let totalToday = 0;
  let tokensToday = 0;

  if (threadsRaw) {
    for (const [, value] of Object.entries(threadsRaw)) {
      try {
        const thread = JSON.parse(value) as HiveThreadSummary & { tokens_consumed?: number };
        if (thread.state && !["resolved", "stale"].includes(thread.state)) {
          activeThreads.push({ title: thread.title, state: thread.state });
        }
        totalToday++;
        tokensToday += thread.tokens_consumed || 0;
      } catch { /* skip corrupt entries */ }
    }
  }

  const recentResolved: HiveThreadSummary[] = [];
  let debatesResolved = 0;
  if (resolvedRaw) {
    for (const item of resolvedRaw) {
      try {
        const thread = (typeof item === "string" ? JSON.parse(item) : item) as HiveThreadSummary;
        recentResolved.push(thread);
        debatesResolved++;
      } catch { /* skip */ }
    }
  }

  return {
    cognitive_state: cognitiveState,
    active_threads: activeThreads,
    recent_resolved: recentResolved,
    stats: {
      total_threads_today: totalToday,
      tokens_consumed_today: tokensToday,
      debates_resolved_today: debatesResolved,
    },
  };
}

export async function accumulateHiveEvent(eventType: string, data: Record<string, unknown>): Promise<void> {
  const redis = getRedis();

  switch (eventType) {
    case "cognitive_transition": {
      const toState = data.to_state as string;
      if (toState) {
        await redis.set(HIVE_STATE_KEY, toState, { ex: 86400 });
      }
      break;
    }
    case "thread_lifecycle": {
      const threadId = data.thread_id as string;
      const state = data.state as string;
      const title = (data.title as string) || `Thread ${threadId?.slice(-6)}`;
      if (threadId && state) {
        const summary = JSON.stringify({ title, state, tokens_consumed: 0 });
        await redis.hset(HIVE_THREADS_KEY, { [threadId]: summary });
        if (state === "resolved" || state === "stale") {
          const resolved = JSON.stringify({
            title,
            state,
            outcome: state === "resolved" ? "pr_opened" : "stale",
            resolved_at: new Date().toISOString(),
          });
          await redis.lpush(HIVE_RESOLVED_KEY, resolved);
          await redis.ltrim(HIVE_RESOLVED_KEY, 0, 19);
          await redis.hdel(HIVE_THREADS_KEY, threadId);
        }
      }
      break;
    }
    case "persona_reasoning": {
      const threadId = data.thread_id as string;
      const tokenCost = (data.token_cost as number) || 0;
      if (threadId && tokenCost > 0) {
        const existing = await redis.hget<string>(HIVE_THREADS_KEY, threadId);
        if (existing) {
          try {
            const parsed = JSON.parse(existing) as { tokens_consumed?: number; [k: string]: unknown };
            parsed.tokens_consumed = (parsed.tokens_consumed || 0) + tokenCost;
            await redis.hset(HIVE_THREADS_KEY, { [threadId]: JSON.stringify(parsed) });
          } catch { /* skip */ }
        }
      }
      break;
    }
  }
}
