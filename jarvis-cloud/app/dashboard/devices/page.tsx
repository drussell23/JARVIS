"use client";

import { useDevices } from "@/lib/hooks/use-devices";
import type { DeviceRecord } from "@/lib/routing/types";

const DEVICE_ICONS: Record<string, string> = {
  mac: "💻",
  watch: "⌚",
  iphone: "📱",
  browser: "🌐",
};

function timeAgo(dateStr: string): string {
  const seconds = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export default function DevicesPage() {
  const { devices, loading, refresh } = useDevices();

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold text-zinc-100">Devices</h2>
        <div className="flex gap-2">
          <button
            onClick={refresh}
            className="text-sm text-zinc-400 hover:text-zinc-200 px-3 py-1.5 border border-zinc-700 rounded-md hover:border-zinc-600 transition-colors"
          >
            Refresh
          </button>
          <button className="bg-zinc-100 text-zinc-900 text-sm font-medium px-4 py-1.5 rounded-md hover:bg-zinc-200 transition-colors">
            Pair New Device
          </button>
        </div>
      </div>

      {loading ? (
        <div className="border border-zinc-800 rounded-lg p-8 text-center text-zinc-500">
          Loading devices…
        </div>
      ) : devices.length === 0 ? (
        <div className="border border-zinc-800 rounded-lg p-8 text-center text-zinc-500">
          No devices paired. Click &ldquo;Pair New Device&rdquo; to get started.
        </div>
      ) : (
        <div className="border border-zinc-800 rounded-lg divide-y divide-zinc-800">
          {devices.map((device) => (
            <DeviceRow key={device.device_id} device={device} />
          ))}
        </div>
      )}
    </div>
  );
}

function DeviceRow({ device }: { device: DeviceRecord }) {
  return (
    <div className="p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-lg">
            {DEVICE_ICONS[device.device_type] ?? "📟"}
          </span>
          <div>
            <p className="text-sm font-medium text-zinc-200">
              {device.device_name}
            </p>
            <p className="text-xs font-mono text-zinc-500">{device.device_id}</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span
            className={`text-xs px-2 py-0.5 rounded-full ${
              device.active
                ? "bg-green-900/50 text-green-400"
                : "bg-red-900/50 text-red-400"
            }`}
          >
            {device.active ? "Active" : "Revoked"}
          </span>
          <span
            className={`text-xs px-2 py-0.5 rounded-full ${
              device.role === "executor"
                ? "bg-cyan-900/50 text-cyan-400"
                : "bg-zinc-800 text-zinc-400"
            }`}
          >
            {device.role}
          </span>
        </div>
      </div>
      <div className="mt-2 flex gap-4 text-xs text-zinc-600">
        <span>Paired: {new Date(device.paired_at).toLocaleDateString()}</span>
        <span>Last seen: {timeAgo(device.last_seen)}</span>
      </div>
    </div>
  );
}
