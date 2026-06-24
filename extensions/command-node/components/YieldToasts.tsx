'use client';

/**
 * YieldToasts -- transient alerts for sovereign_yield events
 * (FRACTURE / QUARANTINE / RECOVERED). Read-only surfacing; no action.
 */

import { YieldAlert } from '../lib/projection';
import { STATE } from '../lib/tokens';

const REASON_COLOR: Record<string, string> = {
  FRACTURE: STATE.danger,
  QUARANTINE: STATE.attention,
  RECOVERED: STATE.ok,
};

export interface YieldToastsProps {
  readonly alerts: readonly YieldAlert[];
}

export function YieldToasts({ alerts }: YieldToastsProps): JSX.Element | null {
  if (alerts.length === 0) {
    return null;
  }
  return (
    <div className="yield-toasts" data-testid="yield-toasts"
      role="status" aria-live="polite">
      {alerts.map((a) => (
        <div
          key={a.key}
          className="yield-toast"
          data-reason={a.reason}
          style={{ borderLeftColor: REASON_COLOR[a.reason] ?? STATE.pending }}
        >
          <span
            className="yield-badge"
            style={{ backgroundColor: REASON_COLOR[a.reason] ?? STATE.pending }}
          >
            {a.reason}
          </span>
          <span className="yield-op mono">{a.opId}</span>
        </div>
      ))}
    </div>
  );
}

export default YieldToasts;
