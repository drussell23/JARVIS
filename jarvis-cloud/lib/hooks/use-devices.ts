"use client";

import { useState, useEffect, useCallback } from "react";
import type { DeviceRecord } from "@/lib/routing/types";

const REFRESH_INTERVAL_MS = 30_000;

export function useDevices() {
  const [devices, setDevices] = useState<DeviceRecord[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/devices");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { devices?: DeviceRecord[] };
      setDevices(data.devices ?? []);
    } catch {
      // Network or parse error — keep stale data
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [refresh]);

  return { devices, loading, refresh };
}
