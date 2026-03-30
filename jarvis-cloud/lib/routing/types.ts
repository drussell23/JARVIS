export type DeviceType = "watch" | "iphone" | "mac" | "browser";
export type Priority = "realtime" | "background" | "deferred";
export type ResponseMode = "stream" | "notify";
export type BrainId = "claude" | "doubleword_397b" | "doubleword_235b" | "vla_local";
export type RoutingMode = "stream" | "batch" | "local";
export type DeviceRole = "executor" | "observer";
export type FanOutChannel = "redis" | "queue";

export interface CommandContext {
  active_app?: string;
  active_file?: string;
  screen_summary?: string;
  location?: string;
  battery_level?: number;
  /** Base64-encoded JPEG screenshot for VLA (Vision Language Agent). */
  screenshot?: string;
}

export interface CommandPayload {
  command_id: string;
  device_id: string;
  device_type: DeviceType;
  text: string;
  intent_hint?: string;
  context?: CommandContext;
  priority: Priority;
  response_mode: ResponseMode;
  timestamp: string;
  signature: string;
}

export interface DeviceTarget {
  device_id: string;
  channel: FanOutChannel;
  role: DeviceRole;
}

export interface RoutingDecision {
  brain: BrainId;
  mode: RoutingMode;
  model: string;
  fan_out: DeviceTarget[];
  system_prompt_key: string;
  estimated_latency: "realtime" | "minutes" | "hours";
}

export interface DeviceRecord {
  device_id: string;
  device_type: DeviceType;
  device_name: string;
  paired_at: string;
  last_seen: string;
  push_token?: string;
  role: DeviceRole;
  active: boolean;
  hkdf_version: number;
}

export interface RouteRule {
  pattern: RegExp;
  brain: BrainId;
  mode: RoutingMode;
  model: string;
  system_prompt_key: string;
  estimated_latency: "realtime" | "minutes" | "hours";
}

export interface PairingSession {
  code: string;
  created_by_session: string;
  created_at: string;
  attempts_remaining: number;
  device_type_hint: DeviceType;
}
