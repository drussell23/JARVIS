'use client';

/**
 * ElevationQueue -- the list of pending CRITICAL_ELEVATION PRs from
 * cross_repo_elevation_pending events.
 *
 * Read surface for the blast radius (view-only) + the Phase 2 write
 * affordance: "Authorize" now opens the biometric AuthorizeElevationModal
 * (it does NOT authorize anything itself -- it hands the PR + its mutation
 * metadata up to the parent, which drives the FSM-gated modal; the backend
 * is the sole authority).
 *
 * If the biometric write-path is gated off, the parent passes
 * authDisabled=true and the Authorize button is disabled with a clear
 * "biometric auth disabled" hint (no crash).
 */

import { ElevationEntry } from '../lib/projection';
import { repoStyle } from '../lib/theme';

/** The fields the modal needs to fetch a challenge for an elevation. */
export interface AuthorizeTarget {
  readonly prId: string;
  readonly astMutationId: string;
  readonly blastRadiusHash: string;
  readonly targetRepo: string;
}

export interface ElevationQueueProps {
  readonly entries: readonly ElevationEntry[];
  /** Ask the parent to load + focus the blast radius for this op. */
  readonly onViewBlastRadius: (opId: string) => void;
  /** Open the biometric authorize modal for this elevation. */
  readonly onAuthorize: (target: AuthorizeTarget) => void;
  /** The op currently focused in the blast graph (for highlighting). */
  readonly focusedOpId?: string | null;
  /** True when the write-path backend is gated off (Authorize disabled). */
  readonly authDisabled?: boolean;
}

/**
 * Derive a stable mutation id / blast hash when the server did not stamp
 * them on the SSE payload. The challenge binds to these exact strings;
 * deterministic derivation keeps a retry stable for the same PR.
 */
function resolveAstMutationId(e: ElevationEntry): string {
  return e.ast_mutation_id ?? e.opId;
}

function resolveBlastHash(e: ElevationEntry): string {
  if (e.blast_radius_hash !== undefined && e.blast_radius_hash !== '') {
    return e.blast_radius_hash;
  }
  // Deterministic, dependency-free FNV-1a hash of the summary as a fallback.
  let h = 0x811c9dc5;
  const s = e.blast_radius_summary;
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, '0');
}

export function ElevationQueue({
  entries,
  onViewBlastRadius,
  onAuthorize,
  focusedOpId = null,
  authDisabled = false,
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
                    disabled={authDisabled}
                    aria-disabled={authDisabled ? 'true' : 'false'}
                    title={
                      authDisabled
                        ? 'Biometric auth disabled on this node'
                        : 'Authorize this cross-repo elevation (biometric)'
                    }
                    onClick={() =>
                      onAuthorize({
                        prId: e.pr_id,
                        astMutationId: resolveAstMutationId(e),
                        blastRadiusHash: resolveBlastHash(e),
                        targetRepo: e.target_repo,
                      })
                    }
                  >
                    {authDisabled
                      ? 'Authorize (disabled)'
                      : 'Authorize (biometric)'}
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
