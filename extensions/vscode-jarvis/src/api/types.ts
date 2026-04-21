/**
 * Wire types for the JARVIS Gap #6 observability surface.
 *
 * Shapes mirror the Python server's schema_version "1.0" payloads in
 * `backend/core/ouroboros/governance/ide_observability.py` (Slice 1)
 * and `ide_observability_stream.py` (Slice 2). Clients MUST feature-
 * detect on `schema_version` and must never assume fields outside
 * this spec.
 *
 * Authority invariant: these are read-only shapes. The extension
 * never constructs a payload and POSTs it back — Slice 3 is a
 * consumer only.
 */

export const SUPPORTED_SCHEMA_VERSION = '1.0';

/** Shared envelope for every JSON response from the server. */
export interface Envelope {
  readonly schema_version: string;
}

// --- Slice 1 GET responses ------------------------------------------------

export interface HealthResponse extends Envelope {
  readonly enabled: boolean;
  readonly api_version: string;
  readonly surface: string;
  readonly now_mono: number;
}

export interface TaskListResponse extends Envelope {
  readonly op_ids: readonly string[];
  readonly count: number;
}

export type TaskState =
  | 'pending'
  | 'in_progress'
  | 'completed'
  | 'cancelled';

export interface TaskProjection {
  readonly task_id: string;
  readonly state: TaskState;
  readonly title: string;
  readonly body: string;
  readonly sequence: number;
  readonly cancel_reason: string;
}

export interface TaskDetailResponse extends Envelope {
  readonly op_id: string;
  readonly closed: boolean;
  readonly active_task_id: string | null;
  readonly tasks: readonly TaskProjection[];
  readonly board_size: number;
}

export interface ErrorResponse extends Envelope {
  readonly error: true;
  readonly reason_code: string;
}

// --- Slice 2 SSE event frames ---------------------------------------------

export type TaskEventType =
  | 'task_created'
  | 'task_started'
  | 'task_updated'
  | 'task_completed'
  | 'task_cancelled'
  | 'board_closed';

export type ControlEventType =
  | 'heartbeat'
  | 'stream_lag'
  | 'replay_start'
  | 'replay_end';

export type StreamEventType = TaskEventType | ControlEventType;

/** Discriminated-union frame matching the server's StreamEvent shape. */
export interface StreamEventFrame extends Envelope {
  readonly event_id: string;
  readonly event_type: StreamEventType;
  readonly op_id: string;
  readonly timestamp: string;
  readonly payload: Record<string, unknown>;
}

// --- Helpers --------------------------------------------------------------

/**
 * Narrow the envelope's schema_version against the supported version.
 * Returns false for payloads we cannot safely render — consumers
 * should fall back to a conservative display.
 */
export function isSupportedSchema(env: Envelope | null | undefined): boolean {
  return (
    env !== null && env !== undefined && env.schema_version === SUPPORTED_SCHEMA_VERSION
  );
}

const TASK_EVENT_TYPES: ReadonlySet<TaskEventType> = new Set([
  'task_created',
  'task_started',
  'task_updated',
  'task_completed',
  'task_cancelled',
  'board_closed',
]);

export function isTaskEvent(
  frame: StreamEventFrame,
): frame is StreamEventFrame & { event_type: TaskEventType } {
  return TASK_EVENT_TYPES.has(frame.event_type as TaskEventType);
}

const CONTROL_EVENT_TYPES: ReadonlySet<ControlEventType> = new Set([
  'heartbeat',
  'stream_lag',
  'replay_start',
  'replay_end',
]);

export function isControlEvent(
  frame: StreamEventFrame,
): frame is StreamEventFrame & { event_type: ControlEventType } {
  return CONTROL_EVENT_TYPES.has(frame.event_type as ControlEventType);
}
