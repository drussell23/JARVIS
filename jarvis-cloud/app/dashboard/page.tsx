"use client";

import { useDevices } from "@/lib/hooks/use-devices";
import { useEventSource } from "@/lib/hooks/use-event-source";
import type { SSEEvent } from "@/lib/hooks/use-event-source";
import type { DeviceRecord } from "@/lib/routing/types";

export default function DashboardOverview() {
  const { devices, loading } = useDevices();
  // SSE URL is null until the browser is a paired device (Plan 1.1 phase 2)
  const { events, isConnected } = useEventSource(null);

  const activeDevices = devices.filter((d) => d.active);
  const commandCount = events.filter((e) => e.type === "complete").length;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold text-zinc-100">System Overview</h2>
        <div className="flex items-center gap-2">
          <span
            className={`inline-block w-2 h-2 rounded-full ${
              isConnected ? "bg-green-500" : "bg-zinc-600"
            }`}
          />
          <span className="text-xs text-zinc-500">
            {isConnected ? "Live" : "Polling"}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4">
        <StatCard
          label="Connected Devices"
          value={loading ? "..." : String(activeDevices.length)}
        />
        <StatCard label="Commands (session)" value={String(commandCount)} />
        <StatCard label="Events Received" value={String(events.length)} />
      </div>

      {/* Device list */}
      <div className="mt-8 border border-zinc-800 rounded-lg">
        <div className="p-4 border-b border-zinc-800">
          <h3 className="text-sm font-bold text-zinc-400">Devices</h3>
        </div>
        {activeDevices.length === 0 ? (
          <p className="p-4 text-zinc-600 text-sm">
            {loading ? "Loading devices…" : "No devices connected."}
          </p>
        ) : (
          <div className="divide-y divide-zinc-800">
            {activeDevices.map((device) => (
              <DeviceRow key={device.device_id} device={device} />
            ))}
          </div>
        )}
      </div>

      {/* Live event feed */}
      <div className="mt-6 border border-zinc-800 rounded-lg">
        <div className="p-4 border-b border-zinc-800">
          <h3 className="text-sm font-bold text-zinc-400">Live Event Feed</h3>
        </div>
        <div className="max-h-80 overflow-y-auto">
          {events.length === 0 ? (
            <p className="p-4 text-zinc-600 text-sm">
              Events will appear here as devices send commands.
            </p>
          ) : (
            <div className="divide-y divide-zinc-800/50">
              {[...events]
                .reverse()
                .slice(0, 50)
                .map((event, i) => (
                  <div key={i} className="px-4 py-2 flex items-start gap-3">
                    <EventBadge type={event.type} />
                    <div className="flex-1 min-w-0">
                      <EventContent event={event} />
                    </div>
                  </div>
                ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="border border-zinc-800 rounded-lg p-4">
      <p className="text-xs text-zinc-500 uppercase tracking-wider">{label}</p>
      <p className="text-2xl font-mono text-zinc-100 mt-1">{value}</p>
    </div>
  );
}

function DeviceRow({ device }: { device: DeviceRecord }) {
  return (
    <div className="p-4 flex items-center justify-between">
      <div>
        <p className="text-sm text-zinc-200">{device.device_name}</p>
        <p className="text-xs text-zinc-500 font-mono">{device.device_id}</p>
      </div>
      <div className="text-right">
        <span
          className={`text-xs px-2 py-0.5 rounded-full ${
            device.role === "executor"
              ? "bg-cyan-900/50 text-cyan-400"
              : "bg-zinc-800 text-zinc-400"
          }`}
        >
          {device.role}
        </span>
        <p className="text-xs text-zinc-600 mt-1">{device.device_type}</p>
      </div>
    </div>
  );
}

const BADGE_COLORS: Record<string, string> = {
  token: "bg-blue-900/50 text-blue-400",
  daemon: "bg-purple-900/50 text-purple-400",
  status: "bg-yellow-900/50 text-yellow-400",
  complete: "bg-green-900/50 text-green-400",
  heartbeat: "bg-zinc-800 text-zinc-500",
  action: "bg-orange-900/50 text-orange-400",
  disconnect: "bg-red-900/50 text-red-400",
};

function EventBadge({ type }: { type: string }) {
  return (
    <span
      className={`text-xs px-1.5 py-0.5 rounded font-mono flex-shrink-0 ${
        BADGE_COLORS[type] ?? "bg-zinc-800 text-zinc-400"
      }`}
    >
      {type}
    </span>
  );
}

function EventContent({ event }: { event: SSEEvent }) {
  const d = event.data;
  switch (event.type) {
    case "token":
      return (
        <span className="text-sm text-zinc-300">{String(d.token ?? "")}</span>
      );
    case "daemon":
      return (
        <div>
          <span
            className={`text-sm ${
              d.narration_priority === "urgent"
                ? "text-red-400"
                : "text-purple-300"
            }`}
          >
            {String(d.narration_text ?? "")}
          </span>
          <span className="text-xs text-zinc-600 ml-2">
            {String(d.source_brain ?? "")}
          </span>
        </div>
      );
    case "complete":
      return (
        <span className="text-sm text-green-400">
          {String(d.source_brain ?? "?")} — {String(d.latency_ms ?? "?")}ms,{" "}
          {String(d.token_count ?? "?")} tokens
        </span>
      );
    case "status":
      return (
        <span className="text-sm text-yellow-300">
          [{String(d.phase ?? "")}] {String(d.message ?? "")}
        </span>
      );
    default:
      return (
        <span className="text-xs text-zinc-500 font-mono">
          {JSON.stringify(d).slice(0, 100)}
        </span>
      );
  }
}
