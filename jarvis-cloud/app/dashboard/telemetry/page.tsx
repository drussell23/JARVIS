"use client";

import { useEventSource } from "@/lib/hooks/use-event-source";
import type { SSEEvent } from "@/lib/hooks/use-event-source";
import type { SSEEventType } from "@/lib/sse/types";
import { useState } from "react";

const FILTER_OPTIONS = [
  "all",
  "token",
  "daemon",
  "status",
  "complete",
  "action",
] as const;

type FilterOption = (typeof FILTER_OPTIONS)[number];

const TYPE_COLORS: Record<string, string> = {
  token: "bg-blue-900/50 text-blue-400",
  daemon: "bg-purple-900/50 text-purple-400",
  status: "bg-yellow-900/50 text-yellow-400",
  complete: "bg-green-900/50 text-green-400",
  action: "bg-orange-900/50 text-orange-400",
  disconnect: "bg-red-900/50 text-red-400",
};

function typeColor(type: string): string {
  return TYPE_COLORS[type] ?? "bg-zinc-800 text-zinc-400";
}

function formatEventContent(event: SSEEvent): string {
  const d = event.data;
  switch (event.type) {
    case "token":
      return String(d.token ?? "");
    case "daemon":
      return `[${String(d.narration_priority ?? "")}] ${String(d.narration_text ?? "")}`;
    case "complete":
      return `${String(d.source_brain ?? "")} — ${String(d.latency_ms ?? "")}ms, ${String(d.token_count ?? "")} tokens`;
    case "status":
      return `[${String(d.phase ?? "")}] ${String(d.message ?? "")}`;
    case "action":
      return `${String(d.action_type ?? "")} → ${String(d.target_device ?? "")}`;
    default:
      return JSON.stringify(d).slice(0, 100);
  }
}

export default function TelemetryPage() {
  // SSE URL is null until the browser is a paired device (Plan 1.1 phase 2)
  const { events, isConnected, clearEvents } = useEventSource(null);
  const [filter, setFilter] = useState<FilterOption>("all");

  const filtered =
    filter === "all"
      ? events
      : events.filter((e) => e.type === (filter as SSEEventType));

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold text-zinc-100">Telemetry</h2>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <span
              className={`w-2 h-2 rounded-full ${
                isConnected ? "bg-green-500" : "bg-zinc-600"
              }`}
            />
            <span className="text-xs text-zinc-500">
              {isConnected ? "Live" : "Disconnected"}
            </span>
          </div>
          <button
            onClick={clearEvents}
            className="text-xs text-zinc-500 hover:text-zinc-300 px-2 py-1 border border-zinc-700 rounded transition-colors"
          >
            Clear
          </button>
        </div>
      </div>

      {/* Filter tabs */}
      <div className="flex gap-1 mb-4">
        {FILTER_OPTIONS.map((type) => {
          const count =
            type !== "all"
              ? events.filter((e) => e.type === (type as SSEEventType)).length
              : null;
          return (
            <button
              key={type}
              onClick={() => setFilter(type)}
              className={`text-xs px-3 py-1.5 rounded-md transition-colors ${
                filter === type
                  ? "bg-zinc-700 text-zinc-100"
                  : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50"
              }`}
            >
              {type}
              {count !== null && (
                <span className="ml-1 text-zinc-600">{count}</span>
              )}
            </button>
          );
        })}
      </div>

      {/* Event table */}
      <div className="border border-zinc-800 rounded-lg">
        <div className="max-h-[calc(100vh-240px)] overflow-y-auto">
          {filtered.length === 0 ? (
            <p className="p-8 text-center text-zinc-600 text-sm">
              {events.length === 0
                ? "No events yet. Send a command from a connected device."
                : "No events match this filter."}
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-zinc-950 border-b border-zinc-800">
                <tr className="text-left text-xs text-zinc-500 uppercase">
                  <th className="px-4 py-2 w-24">Type</th>
                  <th className="px-4 py-2 w-32">Command</th>
                  <th className="px-4 py-2">Content</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800/50">
                {[...filtered]
                  .reverse()
                  .map((event, i) => (
                    <tr key={i} className="hover:bg-zinc-900/50">
                      <td className="px-4 py-2">
                        <span
                          className={`text-xs px-1.5 py-0.5 rounded font-mono ${typeColor(event.type)}`}
                        >
                          {event.type}
                        </span>
                      </td>
                      <td className="px-4 py-2 font-mono text-xs text-zinc-600">
                        {String(event.data.command_id ?? "").slice(0, 8) || "—"}
                      </td>
                      <td className="px-4 py-2 text-zinc-300 truncate max-w-0">
                        {formatEventContent(event)}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
