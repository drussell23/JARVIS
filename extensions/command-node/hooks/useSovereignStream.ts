'use client';

/**
 * useSovereignStream -- the React SSE client for the Command Node.
 *
 * Wraps the framework-agnostic `StreamConsumer` (lib/stream.ts, mirror
 * of the vscode parser) into React state:
 *   - a bounded in-memory ring buffer of typed event frames (cap from
 *     config; default 500)
 *   - a typed connection state
 *   - the current Last-Event-ID (for operator visibility / debugging)
 *
 * Resilience: if the stream fails `maxFailuresBeforePoll` times in a
 * row, the hook degrades to a poll fallback that periodically GETs
 * /observability/tasks and synthesizes a `task_updated`-shaped frame so
 * the UI keeps refreshing even when SSE is unreachable. When the stream
 * recovers, polling stops.
 *
 * Read-only: this hook never POSTs and exposes no write surface.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { resolveConfig } from '../lib/config';
import { ObservabilityClient } from '../lib/api';
import { StreamConsumer, StreamState } from '../lib/stream';
import { StreamEventFrame } from '../lib/types';

export type ConnectionState = StreamState | 'polling';

export interface UseSovereignStreamResult {
  readonly events: readonly StreamEventFrame[];
  readonly connectionState: ConnectionState;
  readonly lastEventId: string | null;
  /** Manually clear the in-memory event buffer (operator action). */
  readonly clear: () => void;
}

export interface UseSovereignStreamOptions {
  /** Optional op_id filter passed to the stream endpoint. */
  readonly opIdFilter?: string;
  /** Test injection -- defaults to the global fetch. */
  readonly fetchFn?: typeof fetch;
  /** Test injection -- defaults to resolveConfig(). */
  readonly config?: ReturnType<typeof resolveConfig>;
}

/** Append a frame to a ring buffer of at most `cap` entries. Pure. */
export function pushBounded(
  buf: readonly StreamEventFrame[],
  frame: StreamEventFrame,
  cap: number,
): StreamEventFrame[] {
  const next = buf.length >= cap ? buf.slice(buf.length - cap + 1) : buf.slice();
  next.push(frame);
  return next;
}

export function useSovereignStream(
  options: UseSovereignStreamOptions = {},
): UseSovereignStreamResult {
  const cfg = options.config ?? resolveConfig();
  const fetchFn = options.fetchFn;

  const [events, setEvents] = useState<readonly StreamEventFrame[]>([]);
  const [connectionState, setConnectionState] =
    useState<ConnectionState>('disconnected');
  const [lastEventId, setLastEventId] = useState<string | null>(null);

  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollSeqRef = useRef(0);

  const clear = useCallback(() => setEvents([]), []);

  const ingest = useCallback(
    (frame: StreamEventFrame) => {
      setEvents((prev) => pushBounded(prev, frame, cfg.eventBufferCap));
      setLastEventId(frame.event_id);
    },
    [cfg.eventBufferCap],
  );

  useEffect(() => {
    let disposed = false;

    const client = new ObservabilityClient({
      endpoint: cfg.observabilityBase,
      ...(fetchFn ? { fetchFn } : {}),
    });

    const stopPolling = (): void => {
      if (pollTimerRef.current !== null) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };

    const startPolling = (): void => {
      if (pollTimerRef.current !== null || disposed) {
        return;
      }
      setConnectionState('polling');
      const tick = async (): Promise<void> => {
        try {
          const list = await client.taskList();
          if (disposed) {
            return;
          }
          pollSeqRef.current += 1;
          // Synthesize a poll-fallback frame so consumers keep updating.
          ingest({
            schema_version: '1.0',
            event_id: `poll-${pollSeqRef.current}`,
            event_type: 'task_updated',
            op_id: '__poll__',
            timestamp: new Date().toISOString(),
            payload: { op_ids: list.op_ids, count: list.count, poll: true },
          });
        } catch {
          // Keep polling; the next tick may succeed.
        }
      };
      void tick();
      pollTimerRef.current = setInterval(() => void tick(), cfg.pollIntervalMs);
    };

    const consumer = new StreamConsumer({
      endpoint: cfg.observabilityBase,
      autoReconnect: true,
      reconnectMaxBackoffMs: cfg.reconnectMaxBackoffMs,
      ...(fetchFn ? { fetchFn } : {}),
      ...(options.opIdFilter ? { opIdFilter: options.opIdFilter } : {}),
    });

    consumer.onEvent((frame) => {
      // Any live frame means SSE is healthy again -- drop the poll.
      stopPolling();
      ingest(frame);
    });

    consumer.onState((state) => {
      if (disposed) {
        return;
      }
      setConnectionState(state);
      setLastEventId(consumer.getLastEventId());
      if (state === 'connected') {
        stopPolling();
      }
      // Degrade to polling once we've failed enough times in a row.
      if (
        (state === 'error' || state === 'reconnecting') &&
        consumer.getConsecutiveFailures() >= cfg.maxFailuresBeforePoll
      ) {
        startPolling();
      }
    });

    consumer.start();

    return () => {
      disposed = true;
      stopPolling();
      void consumer.stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    cfg.observabilityBase,
    cfg.reconnectMaxBackoffMs,
    cfg.pollIntervalMs,
    cfg.maxFailuresBeforePoll,
    options.opIdFilter,
    fetchFn,
    ingest,
  ]);

  return { events, connectionState, lastEventId, clear };
}
