'use client';

/**
 * FSMStateStream -- the 11-phase Ouroboros ribbon.
 *
 * One ribbon per active op (multiple concurrent ops = multiple
 * ribbons), live-highlighting the current phase from fsm_phase_changed,
 * with provider / route / risk-tier badges. Read-only.
 */

import { OUROBOROS_PHASES } from '../lib/types';
import { OpFsmState } from '../lib/projection';
import { STATE } from '../lib/tokens';

export interface FSMStateStreamProps {
  readonly ops: readonly OpFsmState[];
}

function phaseIndex(phase: string): number {
  const idx = (OUROBOROS_PHASES as readonly string[]).indexOf(phase);
  return idx;
}

function riskBadgeColor(tier: string | undefined): string {
  switch (tier) {
    case 'safe_auto':
      return STATE.ok;
    case 'notify_apply':
      return STATE.warn;
    case 'approval_required':
      return STATE.attention;
    case 'critical_elevation':
      return STATE.danger;
    case 'blocked':
      return STATE.dangerDeep;
    default:
      return STATE.pending;
  }
}

export function OpRibbon({ op }: { readonly op: OpFsmState }): JSX.Element {
  const current = phaseIndex(op.phase);
  return (
    <div className="op-ribbon" data-op-id={op.opId} role="group"
      aria-label={`op ${op.opId} phase ${op.phase}`}>
      <div className="op-ribbon-head">
        <span className="op-id" title={op.opId}>{op.opId}</span>
        {op.provider ? (
          <span className="badge badge-provider">{op.provider}</span>
        ) : null}
        {op.route ? (
          <span className="badge badge-route">{op.route}</span>
        ) : null}
        {op.riskTier ? (
          <span
            className="badge badge-risk"
            style={{ backgroundColor: riskBadgeColor(op.riskTier) }}
          >
            {op.riskTier}
          </span>
        ) : null}
      </div>
      <ol className="phase-track">
        {OUROBOROS_PHASES.map((phase, i) => {
          const state =
            current < 0
              ? 'idle'
              : i < current
                ? 'done'
                : i === current
                  ? 'active'
                  : 'future';
          return (
            <li
              key={phase}
              className={`phase phase-${state}`}
              data-phase={phase}
              data-state={state}
              title={phase}
            >
              <span className="phase-dot" aria-hidden="true" />
              <span className="phase-label">{phase}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export function FSMStateStream({ ops }: FSMStateStreamProps): JSX.Element {
  if (ops.length === 0) {
    return (
      <div className="fsm-stream fsm-empty" data-testid="fsm-empty">
        <span className="muted">No active operations.</span>
      </div>
    );
  }
  return (
    <div className="fsm-stream" data-testid="fsm-stream">
      {ops.map((op) => (
        <OpRibbon key={op.opId} op={op} />
      ))}
    </div>
  );
}

export default FSMStateStream;
