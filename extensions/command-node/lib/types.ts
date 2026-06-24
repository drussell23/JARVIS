/**
 * Wire types for the Sovereign Command Node (Phase 1, read-only).
 *
 * These shapes mirror the Python server's schema_version "1.0"
 * payloads in `backend/core/ouroboros/governance/ide_observability.py`
 * (Slice 1 GETs) and `ide_observability_stream.py` (Slice 2 SSE),
 * extended with the Command-Node-specific Task A event types
 * (fsm_phase_changed / cross_repo_elevation_pending / sovereign_yield /
 * dag_node_updated) and the blast-radius GET.
 *
 * Authority invariant: these are READ-ONLY shapes. The dashboard never
 * constructs a payload and POSTs it back. Phase 1 is a consumer only.
 *
 * Mirrors `extensions/vscode-jarvis/src/api/types.ts` -- kept in sync
 * by hand (the two extensions consume the same backend surface).
 */

export const SUPPORTED_SCHEMA_VERSION = '1.0';

/** Shared envelope for every JSON response from the server. */
export interface Envelope {
  readonly schema_version: string;
}

// --- GET responses --------------------------------------------------------

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

// --- Blast radius (GET /observability/blast-radius/{op_id}) ---------------

/** One of the three Trinity repos. Open vocabulary: render unknowns
 * with a neutral color rather than crashing. */
export type TrinityRepo = 'jarvis' | 'prime' | 'reactor' | string;

export interface AffectedNode {
  readonly repo: TrinityRepo;
  readonly file: string;
  readonly name: string;
  readonly type: string;
}

export type BlastRiskLevel =
  | 'low'
  | 'moderate'
  | 'high'
  | 'critical'
  | string;

export interface BlastRadiusResponse extends Envelope {
  readonly op_id: string;
  readonly directly_affected: readonly AffectedNode[];
  readonly transitively_affected: readonly AffectedNode[];
  readonly total_affected: number;
  readonly risk_level: BlastRiskLevel;
}

// --- Phase 2 biometric write-path -----------------------------------------

/**
 * The single-use, TTL-bounded challenge minted by the backend
 * (`GET /command-node/elevation/{pr_id}/challenge`). The operator must
 * speak THIS phrase; a stale enrollment recording cannot answer it. The
 * nonce is consumed atomically on use -- a fresh challenge MUST be fetched
 * for every retry (no resending a spent nonce).
 */
export interface ElevationChallenge {
  readonly nonce: string;
  readonly phrase: string;
  readonly pr_id: string;
  readonly ast_mutation_id: string;
  readonly blast_radius_hash: string;
  readonly issued_at: number;
  readonly ttl_s: number;
}

export interface ChallengeResponse extends Envelope {
  readonly challenge: ElevationChallenge;
}

export type AuthDecision = 'AUTHORIZED' | 'REJECTED' | string;

/**
 * The backend verdict from `POST /command-node/authorize-elevation`.
 * The frontend reflects this -- it never computes it. `reason` carries
 * the rejection cause, including the load-bearing `immutable_orange`
 * cause (Mind/Nerves PRs are permanently human-merge-only by law, NOT a
 * biometric failure).
 */
export interface ElevationVerdict {
  readonly decision: AuthDecision;
  readonly reason: string;
  readonly ecapa_score: number;
  readonly antispoof_ok: boolean;
  readonly freshness_ok: boolean;
  readonly pr_id: string;
  readonly ast_mutation_id: string;
  readonly target_repo: TrinityRepo;
}

/** The POST body for an authorization attempt. */
export interface AuthorizeRequest {
  readonly pr_id: string;
  readonly nonce: string;
  readonly ast_mutation_id: string;
  readonly audio_b64: string;
  readonly sample_rate: number;
}

/**
 * The canonical "immutable_orange" rejection reason. Detected to render
 * the special copy that explains this is a governance law, not a
 * biometric mismatch.
 */
export const IMMUTABLE_ORANGE_REASON = 'immutable_orange';

/** True when a verdict was rejected because the target repo is Mind/Nerves. */
export function isImmutableOrange(verdict: ElevationVerdict | null): boolean {
  if (verdict === null) {
    return false;
  }
  return verdict.reason.toLowerCase().includes(IMMUTABLE_ORANGE_REASON);
}

// --- SSE event frames -----------------------------------------------------

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
  | 'replay_end'
  | 'posture_changed';

// Task A command-node event types.
export type SovereignEventType =
  | 'fsm_phase_changed'
  | 'cross_repo_elevation_pending'
  | 'sovereign_yield'
  | 'dag_node_updated';

export type StreamEventType =
  | TaskEventType
  | ControlEventType
  | SovereignEventType;

/** The 11-phase Ouroboros governance pipeline. */
export const OUROBOROS_PHASES = [
  'CLASSIFY',
  'ROUTE',
  'CONTEXT_EXPANSION',
  'PLAN',
  'GENERATE',
  'VALIDATE',
  'GATE',
  'APPROVE',
  'APPLY',
  'VERIFY',
  'COMPLETE',
] as const;

export type OuroborosPhase = (typeof OUROBOROS_PHASES)[number] | string;

export type RiskTier =
  | 'safe_auto'
  | 'notify_apply'
  | 'approval_required'
  | 'critical_elevation'
  | 'blocked'
  | string;

export interface FsmPhaseChangedPayload {
  readonly phase: OuroborosPhase;
  readonly route?: string;
  readonly risk_tier?: RiskTier;
  readonly provider?: string;
}

export interface CrossRepoElevationPayload {
  readonly pr_id: string;
  readonly target_repo: TrinityRepo;
  readonly blast_radius_summary: string;
  /**
   * The mutation identity + blast-radius hash the challenge is bound to.
   * OPTIONAL + additive (older servers omit them); the modal needs them to
   * fetch a challenge, so the UI falls back to the op_id / a stable hash of
   * the summary when the server does not stamp them.
   */
  readonly ast_mutation_id?: string;
  readonly blast_radius_hash?: string;
}

export type SovereignYieldReason =
  | 'FRACTURE'
  | 'QUARANTINE'
  | 'RECOVERED'
  | string;

export interface SovereignYieldPayload {
  readonly reason: SovereignYieldReason;
}

export type DagNodeState =
  | 'pending'
  | 'running'
  | 'applied'
  | 'fractured'
  | 'complete'
  | string;

export interface DagNodeUpdatedPayload {
  readonly node_id: string;
  readonly state: DagNodeState;
}

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
 * Returns false for payloads we cannot safely render -- consumers
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
  'posture_changed',
]);

export function isControlEvent(
  frame: StreamEventFrame,
): frame is StreamEventFrame & { event_type: ControlEventType } {
  return CONTROL_EVENT_TYPES.has(frame.event_type as ControlEventType);
}

export function isFsmPhaseEvent(
  frame: StreamEventFrame,
): frame is StreamEventFrame & {
  event_type: 'fsm_phase_changed';
  payload: FsmPhaseChangedPayload;
} {
  return frame.event_type === 'fsm_phase_changed';
}

export function isCrossRepoElevationEvent(
  frame: StreamEventFrame,
): frame is StreamEventFrame & {
  event_type: 'cross_repo_elevation_pending';
  payload: CrossRepoElevationPayload;
} {
  return frame.event_type === 'cross_repo_elevation_pending';
}

export function isSovereignYieldEvent(
  frame: StreamEventFrame,
): frame is StreamEventFrame & {
  event_type: 'sovereign_yield';
  payload: SovereignYieldPayload;
} {
  return frame.event_type === 'sovereign_yield';
}

export function isDagNodeUpdatedEvent(
  frame: StreamEventFrame,
): frame is StreamEventFrame & {
  event_type: 'dag_node_updated';
  payload: DagNodeUpdatedPayload;
} {
  return frame.event_type === 'dag_node_updated';
}
