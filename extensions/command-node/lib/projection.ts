/**
 * Pure projections from the raw SSE event buffer into the view models
 * the dashboard components render. Kept pure (no React) so they are
 * trivially testable and reusable.
 */

import {
  CrossRepoElevationPayload,
  DagNodeState,
  DagNodeUpdatedPayload,
  FsmPhaseChangedPayload,
  OuroborosPhase,
  RiskTier,
  StreamEventFrame,
  SovereignYieldPayload,
  isCrossRepoElevationEvent,
  isDagNodeUpdatedEvent,
  isFsmPhaseEvent,
  isSovereignYieldEvent,
  isTaskEvent,
} from './types';

export interface OpFsmState {
  readonly opId: string;
  readonly phase: OuroborosPhase;
  readonly route?: string;
  readonly riskTier?: RiskTier;
  readonly provider?: string;
  readonly updatedAt: string;
}

export interface DagNodeView {
  readonly nodeId: string;
  readonly opId: string;
  readonly state: DagNodeState;
}

export interface ElevationEntry extends CrossRepoElevationPayload {
  readonly opId: string;
  readonly receivedAt: string;
}

export interface YieldAlert {
  readonly opId: string;
  readonly reason: SovereignYieldPayload['reason'];
  readonly at: string;
  readonly key: string;
}

/** Latest FSM phase per active op, ordered by most-recent update. */
export function projectFsmStates(
  events: readonly StreamEventFrame[],
): OpFsmState[] {
  const byOp = new Map<string, OpFsmState>();
  for (const frame of events) {
    if (isFsmPhaseEvent(frame)) {
      const p = frame.payload as FsmPhaseChangedPayload;
      byOp.set(frame.op_id, {
        opId: frame.op_id,
        phase: p.phase,
        ...(p.route ? { route: p.route } : {}),
        ...(p.risk_tier ? { riskTier: p.risk_tier } : {}),
        ...(p.provider ? { provider: p.provider } : {}),
        updatedAt: frame.timestamp,
      });
    } else if (isTaskEvent(frame) && frame.event_type === 'task_completed') {
      // Completed ops drop off the live ribbon list.
      byOp.delete(frame.op_id);
    }
  }
  return Array.from(byOp.values());
}

/** Latest state per DAG node, fed by dag_node_updated + task_* events. */
export function projectDagNodes(
  events: readonly StreamEventFrame[],
): DagNodeView[] {
  const byNode = new Map<string, DagNodeView>();
  for (const frame of events) {
    if (isDagNodeUpdatedEvent(frame)) {
      const p = frame.payload as DagNodeUpdatedPayload;
      byNode.set(p.node_id, {
        nodeId: p.node_id,
        opId: frame.op_id,
        state: p.state,
      });
    } else if (isTaskEvent(frame)) {
      // Fall back to the op_id as a coarse node when no DAG event has
      // been seen for it yet, so the canvas isn't empty pre-DAG.
      const key = `op:${frame.op_id}`;
      const state = taskEventToState(frame.event_type);
      if (state !== null && !byNode.has(frame.op_id)) {
        byNode.set(key, { nodeId: key, opId: frame.op_id, state });
      }
    }
  }
  return Array.from(byNode.values());
}

function taskEventToState(eventType: string): DagNodeState | null {
  switch (eventType) {
    case 'task_created':
      return 'pending';
    case 'task_started':
    case 'task_updated':
      return 'running';
    case 'task_completed':
      return 'complete';
    case 'task_cancelled':
      return 'fractured';
    default:
      return null;
  }
}

/** Pending cross-repo elevations, newest first, de-duped by pr_id. */
export function projectElevationQueue(
  events: readonly StreamEventFrame[],
): ElevationEntry[] {
  const byPr = new Map<string, ElevationEntry>();
  for (const frame of events) {
    if (isCrossRepoElevationEvent(frame)) {
      const p = frame.payload as CrossRepoElevationPayload;
      byPr.set(p.pr_id, {
        ...p,
        opId: frame.op_id,
        receivedAt: frame.timestamp,
      });
    }
  }
  return Array.from(byPr.values()).reverse();
}

/** The most recent sovereign_yield events, newest first (capped). */
export function projectYieldAlerts(
  events: readonly StreamEventFrame[],
  cap = 5,
): YieldAlert[] {
  const out: YieldAlert[] = [];
  for (const frame of events) {
    if (isSovereignYieldEvent(frame)) {
      const p = frame.payload as SovereignYieldPayload;
      out.push({
        opId: frame.op_id,
        reason: p.reason,
        at: frame.timestamp,
        key: frame.event_id,
      });
    }
  }
  return out.reverse().slice(0, cap);
}
