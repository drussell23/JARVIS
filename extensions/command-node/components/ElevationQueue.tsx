'use client';

/**
 * ElevationQueue -- read-only list of pending CRITICAL_ELEVATION PRs
 * from cross_repo_elevation_pending events.
 *
 * Phase 1 is VIEW ONLY. The "Authorize" affordance is a DISABLED
 * placeholder clearly marked as Phase 2 (biometric) -- there is no
 * write/auth code anywhere in this component. The only active action is
 * "view blast radius", which asks the parent to focus the graph on the
 * op.
 */

import { ElevationEntry } from '../lib/projection';
import { repoStyle } from '../lib/theme';

export interface ElevationQueueProps {
  readonly entries: readonly ElevationEntry[];
  /** Ask the parent to load + focus the blast radius for this op. */
  readonly onViewBlastRadius: (opId: string) => void;
  /** The op currently focused in the blast graph (for highlighting). */
  readonly focusedOpId?: string | null;
}

export function ElevationQueue({
  entries,
  onViewBlastRadius,
  focusedOpId = null,
}: ElevationQueueProps): JSX.Element {
  return (
    <section className="elevation-queue" data-testid="elevation-queue"
      aria-label="pending critical elevations">
      <header className="elevation-head">
        <h2>Critical Elevations</h2>
        <span className="muted">{entries.length} pending</span>
      </header>
      {entries.length === 0 ? (
        <p className="muted" data-testid="elevation-empty">
          No pending elevations.
        </p>
      ) : (
        <ul className="elevation-list">
          {entries.map((e) => {
            const s = repoStyle(e.target_repo);
            const focused = focusedOpId === e.opId;
            return (
              <li
                key={e.pr_id}
                className={`elevation-item${focused ? ' focused' : ''}`}
                data-pr-id={e.pr_id}
                style={{ borderLeftColor: s.border }}
              >
                <div className="elevation-row">
                  <span className="elevation-pr mono">{e.pr_id}</span>
                  <span
                    className="badge badge-repo"
                    style={{ backgroundColor: s.color }}
                  >
                    {s.label} ({e.target_repo})
                  </span>
                </div>
                <p className="elevation-summary">{e.blast_radius_summary}</p>
                <div className="elevation-actions">
                  <button
                    className="btn btn-view"
                    onClick={() => onViewBlastRadius(e.opId)}
                  >
                    View blast radius
                  </button>
                  <button
                    className="btn btn-authorize"
                    disabled
                    title="Disabled in Phase 1 -- biometric authorization arrives in Phase 2"
                    aria-disabled="true"
                  >
                    Authorize (Phase 2: biometric)
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

export default ElevationQueue;
