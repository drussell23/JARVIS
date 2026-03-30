export interface TokenEvent {
  command_id: string;
  token: string;
  source_brain: "claude";
  sequence: number;
}

export interface ActionEvent {
  command_id: string;
  action_type: "ghost_hands" | "file_edit" | "terminal" | "notification";
  payload: Record<string, unknown>;
  target_device: "mac";
}

export interface DaemonEvent {
  command_id: string;
  narration_text: string;
  narration_priority: "ambient" | "informational" | "urgent";
  source_brain: "claude" | "doubleword_397b" | "doubleword_235b";
}

export interface StatusEvent {
  command_id: string;
  phase: string;
  progress?: number;
  message: string;
}

export interface CompleteEvent {
  command_id: string;
  source_brain: "claude" | "doubleword_397b" | "doubleword_235b";
  token_count?: number;
  latency_ms: number;
  artifacts?: {
    url: string;
    type: "pr" | "diff" | "analysis" | "vision_description";
    expires_at: string;
  }[];
}

export type SSEEventType = "token" | "action" | "daemon" | "status" | "complete" | "heartbeat" | "disconnect";
