"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import type { SSEEventType } from "@/lib/sse/types";

export interface SSEEvent {
  type: SSEEventType;
  data: Record<string, unknown>;
  id?: string;
}

const WATCHED_TYPES: SSEEventType[] = [
  "token",
  "daemon",
  "status",
  "complete",
  "heartbeat",
  "action",
  "disconnect",
];

export function useEventSource(url: string | null) {
  const [events, setEvents] = useState<SSEEvent[]>([]);
  const [isConnected, setIsConnected] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  const connect = useCallback(() => {
    if (!url) return;

    const source = new EventSource(url);
    sourceRef.current = source;

    source.onopen = () => setIsConnected(true);
    source.onerror = () => {
      setIsConnected(false);
      // EventSource auto-reconnects per the spec
    };

    const handlers: Array<() => void> = [];

    for (const type of WATCHED_TYPES) {
      const handler = (e: MessageEvent) => {
        try {
          const data = JSON.parse(e.data) as Record<string, unknown>;
          setEvents((prev) => [
            ...prev.slice(-100),
            { type, data, id: e.lastEventId || undefined },
          ]);
        } catch {
          // Malformed event — skip
        }
      };
      source.addEventListener(type, handler);
      handlers.push(() => source.removeEventListener(type, handler));
    }

    return () => {
      for (const remove of handlers) remove();
      source.close();
      sourceRef.current = null;
      setIsConnected(false);
    };
  }, [url]);

  useEffect(() => {
    const cleanup = connect();
    return cleanup;
  }, [connect]);

  const clearEvents = useCallback(() => setEvents([]), []);

  return { events, isConnected, clearEvents };
}
