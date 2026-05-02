/**
 * Wire types for the JARVIS Gap #2 Slice 4 Confidence-policy
 * write authority surface (`/policy/confidence/*`).
 *
 * The router emits ``schema_version: "ide_policy_router.1"`` —
 * deliberately distinct from the read surface's ``"1.0"`` so the
 * two layers can graduate independently. Clients MUST feature-
 * detect on this exact constant.
 *
 * Authority discipline (mirror of the agent-side AST pin):
 *   * No types reach into orchestrator / iron_gate / risk_engine /
 *     policy_engine — write surface stays cage-mediated.
 *   * Every write operation is an HTTP POST against a documented
 *     route; never side-channel mutation.
 */

export const POLICY_ROUTER_SCHEMA_VERSION = 'ide_policy_router.1';

/** Closed taxonomy mirroring agent-side ConfidencePolicyKind. */
export type ConfidencePolicyKind =
  | 'raise_floor'
  | 'shrink_window'
  | 'widen_approaching'
  | 'enable_enforce'
  | 'multi_dim_tighten';

/** The four operator-visible threshold knobs. */
export interface ConfidencePolicy {
  readonly floor: number;
  readonly window_k: number;
  readonly approaching_factor: number;
  readonly enforce: boolean;
}

/** Currently-effective policy snapshot (live monitor accessors). */
export interface CurrentEffective {
  readonly floor: number | null;
  readonly window_k: number | null;
  readonly approaching_factor: number | null;
  readonly enforce: boolean | null;
}

/** Adapted-YAML state surfaced by the loader. */
export interface AdaptedBlock {
  readonly loader_enabled: boolean;
  readonly in_effect: boolean;
  readonly values: Partial<ConfidencePolicy>;
  readonly proposal_id: string;
  readonly approved_at: string;
  readonly approved_by: string;
}

export type ProposalStatus = 'pending' | 'approved' | 'rejected';

export interface ProposalProjection {
  readonly proposal_id: string;
  readonly kind: string;
  readonly status: ProposalStatus;
  readonly proposed_at: string;
  readonly operator_decision_by: string;
  readonly current_state_hash: string;
  readonly proposed_state_hash: string;
}

export interface ProposalsBlock {
  readonly pending: number;
  readonly approved: number;
  readonly rejected: number;
  readonly items: readonly ProposalProjection[];
}

export interface ConfidenceSnapshot {
  readonly schema_version: string;
  readonly current_effective: CurrentEffective;
  readonly adapted: AdaptedBlock;
  readonly proposals: ProposalsBlock;
  readonly policy_substrate_enabled: boolean;
}

// --- Request bodies -------------------------------------------------------

export interface ProposeBody {
  readonly current: ConfidencePolicy;
  readonly proposed: ConfidencePolicy;
  readonly evidence_summary: string;
  readonly observation_count: number;
  readonly operator: string;
  readonly proposal_id?: string;
}

export interface DecisionBody {
  readonly operator: string;
  readonly reason?: string;
}

// --- Response bodies ------------------------------------------------------

export interface ProposeResponse {
  readonly schema_version: string;
  readonly ok: true;
  readonly proposal_id: string;
  readonly kind: ConfidencePolicyKind | string;
  readonly moved_dimensions: readonly string[];
  readonly current_state_hash: string;
  readonly proposed_state_hash: string;
  readonly monotonic_tightening_verdict: string;
}

export interface DecisionAppliedBlock {
  readonly operator: string;
  readonly yaml_path: string;
  readonly write_status: string;
  readonly write_detail: string;
}

export interface DecisionResponse {
  readonly schema_version: string;
  readonly ok: true;
  readonly proposal_id: string;
  readonly operator_decision: 'approved' | 'rejected';
  readonly operator: string;
  readonly applied: DecisionAppliedBlock | null;
}

export interface PolicyErrorResponse {
  readonly schema_version: string;
  readonly error: true;
  readonly reason_code: string;
  readonly detail: string;
}

// --- SSE event types ------------------------------------------------------

export type PolicyEventType =
  | 'confidence_policy_proposed'
  | 'confidence_policy_approved'
  | 'confidence_policy_rejected'
  | 'confidence_policy_applied';

const POLICY_EVENT_TYPES: ReadonlySet<PolicyEventType> = new Set([
  'confidence_policy_proposed',
  'confidence_policy_approved',
  'confidence_policy_rejected',
  'confidence_policy_applied',
]);

export function isPolicyEventType(t: string): t is PolicyEventType {
  return POLICY_EVENT_TYPES.has(t as PolicyEventType);
}

// --- Validation helpers ---------------------------------------------------

export interface SnapshotEnvelope {
  schema_version?: string;
}

/**
 * Narrow against the Slice 4 router schema. Returns false for
 * payloads we cannot safely render — callers should fall back to
 * a conservative display.
 */
export function isSupportedPolicySchema(
  env: SnapshotEnvelope | null | undefined,
): boolean {
  return (
    env !== null &&
    env !== undefined &&
    env.schema_version === POLICY_ROUTER_SCHEMA_VERSION
  );
}

// --- Tighten-direction predicate (mirror of substrate) --------------------
//
// Defense-in-depth: the webview validates the operator's proposed
// delta BEFORE POSTing so the operator gets immediate UX feedback
// (rather than a 400 from the router). The agent-side cage is
// still the structural arbiter — this client check is a UX hint,
// not a gate.

export interface TightenCheck {
  readonly is_tighten: boolean;
  readonly is_no_op: boolean;
  readonly moved: readonly string[];
  readonly reason: string;
}

export function classifyClientSide(
  current: ConfidencePolicy,
  proposed: ConfidencePolicy,
): TightenCheck {
  const moved: string[] = [];
  let reason = '';
  let isNoOp = true;
  let anyLoosen = false;

  // floor↑ = tighten
  if (proposed.floor !== current.floor) {
    isNoOp = false;
    if (proposed.floor > current.floor) {
      moved.push('raise_floor');
    } else {
      anyLoosen = true;
      reason =
        `floor ${current.floor} → ${proposed.floor} (loosen)`;
    }
  }
  // window_k↓ = tighten
  if (proposed.window_k !== current.window_k) {
    isNoOp = false;
    if (proposed.window_k < current.window_k) {
      moved.push('shrink_window');
    } else {
      anyLoosen = true;
      reason =
        `window_k ${current.window_k} → ${proposed.window_k} (loosen)`;
    }
  }
  // approaching_factor↑ = tighten
  if (proposed.approaching_factor !== current.approaching_factor) {
    isNoOp = false;
    if (proposed.approaching_factor > current.approaching_factor) {
      moved.push('widen_approaching');
    } else {
      anyLoosen = true;
      reason =
        `approaching_factor ${current.approaching_factor} → ` +
        `${proposed.approaching_factor} (loosen)`;
    }
  }
  // enforce False→True = tighten
  if (proposed.enforce !== current.enforce) {
    isNoOp = false;
    if (!current.enforce && proposed.enforce) {
      moved.push('enable_enforce');
    } else {
      anyLoosen = true;
      reason = `enforce true → false (loosen)`;
    }
  }

  if (anyLoosen) {
    return {
      is_tighten: false,
      is_no_op: false,
      moved: [],
      reason: `cage rejects: ${reason}`,
    };
  }
  if (isNoOp) {
    return {
      is_tighten: false,
      is_no_op: true,
      moved: [],
      reason: 'no-op proposal',
    };
  }
  return {
    is_tighten: true,
    is_no_op: false,
    moved,
    reason: `tightens: ${moved.join(', ')}`,
  };
}
