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

// Gap #3 Slice 3 — L3 worktree topology stream events.
// Translated 1:1 by the agent-side bridge from autonomy
// EventEmitter (EXECUTION_GRAPH_STATE_CHANGED + WORK_UNIT_STATE_CHANGED).
export type WorktreeEventType =
  | 'worktree_topology_updated'
  | 'worktree_unit_state_changed';

export type StreamEventType =
  | TaskEventType
  | ControlEventType
  | WorktreeEventType;

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
export function isSupportedSchema(
  env: { schema_version?: string } | null | undefined,
): boolean {
  return (
    env !== null &&
    env !== undefined &&
    env.schema_version === SUPPORTED_SCHEMA_VERSION
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

const WORKTREE_EVENT_TYPES: ReadonlySet<WorktreeEventType> = new Set([
  'worktree_topology_updated',
  'worktree_unit_state_changed',
]);

export function isWorktreeEvent(
  frame: StreamEventFrame,
): frame is StreamEventFrame & { event_type: WorktreeEventType } {
  return WORKTREE_EVENT_TYPES.has(
    frame.event_type as WorktreeEventType,
  );
}

// --- Gap #3 worktree topology projection wire shapes ----------------------
//
// Mirror of the agent-side WorktreeTopology produced by
// ``verification.worktree_topology.compute_worktree_topology`` and
// surfaced via ``GET /observability/worktrees`` /
// ``GET /observability/worktrees/{graph_id}``. The substrate stamps
// ``schema_version: "worktree_topology.1"`` on the inner topology
// envelope; the outer HTTP envelope uses the read surface's
// canonical ``"1.0"``.

export type WorktreeTopologyOutcome =
  | 'ok'
  | 'empty'
  | 'disabled'
  | 'scheduler_invalid'
  | 'failed';

export type WorktreeUnitState =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

export type WorktreeGraphPhase =
  | 'created'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

export type WorktreeEdgeKind = 'dependency' | 'barrier';

export interface WorktreeNodeProjection {
  readonly unit_id: string;
  readonly repo: string;
  readonly goal: string;
  readonly target_files: readonly string[];
  readonly owned_paths: readonly string[];
  readonly dependency_ids: readonly string[];
  readonly state: WorktreeUnitState;
  readonly barrier_id: string;
  readonly has_worktree: boolean;
  readonly worktree_path: string;
  readonly attempt_count: number;
  readonly schema_version: string;
}

export interface WorktreeEdgeProjection {
  readonly from_unit_id: string;
  readonly to_unit_id: string;
  readonly edge_kind: WorktreeEdgeKind;
}

export interface WorktreeGraphProjection {
  readonly graph_id: string;
  readonly op_id: string;
  readonly planner_id: string;
  readonly plan_digest: string;
  readonly causal_trace_id: string;
  readonly phase: WorktreeGraphPhase;
  readonly concurrency_limit: number;
  readonly nodes: readonly WorktreeNodeProjection[];
  readonly edges: readonly WorktreeEdgeProjection[];
  readonly last_error: string;
  readonly updated_at_ns: number;
  readonly checksum: string;
  readonly schema_version: string;
}

export interface WorktreeTopologySummary {
  readonly total_graphs: number;
  readonly total_units: number;
  readonly units_by_state: Record<string, number>;
  readonly graphs_by_phase: Record<string, number>;
  readonly units_with_worktree: number;
  readonly orphan_worktree_count: number;
  readonly orphan_worktree_paths: readonly string[];
}

export interface WorktreeTopologyProjection {
  readonly outcome: WorktreeTopologyOutcome;
  readonly graphs: readonly WorktreeGraphProjection[];
  readonly summary: WorktreeTopologySummary;
  readonly detail: string;
  readonly captured_at_ns: number;
  readonly schema_version: string;
}

export interface WorktreesListResponse extends Envelope {
  readonly topology: WorktreeTopologyProjection;
}

export interface WorktreeDetailResponse extends Envelope {
  readonly graph: WorktreeGraphProjection;
}

// --- Gap #1 — Sessions / DAG / Replay wire shapes -------------------------
//
// Mirror of the agent-side projections returned by:
//   GET /observability/sessions
//   GET /observability/sessions/{session_id}
//   GET /observability/dag/{session_id}
//   GET /observability/dag/{session_id}/{record_id}
//   GET /observability/replay/{health,baseline,verdicts,history}
//
// Used by the Temporal Slider panel for time-travel debugging
// across the CausalityDAG.

export interface SessionProjection {
  readonly session_id: string;
  readonly ok_outcome?: boolean;
  readonly bookmarked?: boolean;
  readonly pinned?: boolean;
  readonly has_replay?: boolean;
  readonly parse_error?: boolean;
  // Substrate-defined free-form metadata (timestamps, op counts,
  // etc.). The extension renders by key; no field is load-bearing
  // beyond the discriminants above.
  readonly [extra: string]: unknown;
}

export interface SessionListResponse extends Envelope {
  readonly sessions: readonly SessionProjection[];
  readonly count: number;
}

export interface SessionDetailResponse extends Envelope {
  readonly session: SessionProjection;
}

export interface DagSessionResponse extends Envelope {
  readonly session_id: string;
  readonly node_count: number;
  readonly edge_count: number;
  // Capped at 1000 by the substrate (handle_dag_session). Already
  // ordered by the substrate (insertion order = causality order).
  readonly record_ids: readonly string[];
}

export interface DagRecordResponse extends Envelope {
  readonly record_id: string;
  // DecisionRecord.to_dict() — free-form per substrate. The
  // panel renders selected fields if present (phase, op_id,
  // ts_ns, etc.) but never assumes a strict shape.
  readonly record: Record<string, unknown>;
  readonly parents: readonly string[];
  readonly children: readonly string[];
  readonly counterfactual_branches: readonly Record<string, unknown>[];
  readonly subgraph_node_count: number;
}

export interface ReplayHealthResponse extends Envelope {
  readonly enabled: boolean;
  readonly engine_enabled: boolean;
  readonly comparator_enabled: boolean;
  readonly observer_enabled: boolean;
  readonly history_path: string;
  readonly history_count: number;
}

export type ReplayVerdictKind =
  | 'equivalent'
  | 'diverged_better'
  | 'diverged_worse'
  | 'diverged_neutral'
  | 'failed'
  | string;  // open vocabulary — substrate may add new kinds

export interface ReplayVerdict {
  readonly verdict?: ReplayVerdictKind;
  readonly tightening?: string;
  readonly cluster_kind?: string;
  readonly schema_version?: string;
  readonly [extra: string]: unknown;
}

export interface ReplayVerdictsResponse extends Envelope {
  readonly verdicts: readonly ReplayVerdict[];
  readonly count: number;
  readonly limit: number;
}

export type ReplayBaselineOutcome =
  | 'baseline_ok'
  | 'baseline_drift'
  | 'baseline_insufficient'
  | string;  // open vocabulary

export interface ReplayBaselineReport extends Envelope {
  readonly outcome: ReplayBaselineOutcome;
  readonly tightening?: string;
  readonly stats?: Record<string, unknown>;
  readonly detail?: string;
  readonly [extra: string]: unknown;
}
