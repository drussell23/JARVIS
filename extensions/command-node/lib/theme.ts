/**
 * Visual theming constants shared across components.
 *
 * The repo color map is load-bearing for the blast-radius graph: a
 * cross-boundary blast must be visually obvious. Body/jarvis=blue,
 * Mind/prime=amber, Nerves/reactor=violet. Unknown repos render the
 * neutral token (open vocabulary -- never crash on a new repo).
 *
 * NO raw hex: every color here is a `var(--token)` reference resolved
 * against `app/globals.css` `:root`. The Sovereign token block is the
 * single source of truth (see lib/tokens.ts + DESIGN_TOKENS.md).
 */

import { DagNodeState, TrinityRepo } from './types';
import { REPO_TOKENS, STATE } from './tokens';

export interface RepoStyle {
  readonly label: string;
  readonly color: string;
  readonly border: string;
}

const REPO_STYLES: Record<string, RepoStyle> = {
  jarvis: {
    label: 'Body',
    color: REPO_TOKENS.jarvis.fill,
    border: REPO_TOKENS.jarvis.border,
  },
  prime: {
    label: 'Mind',
    color: REPO_TOKENS.prime.fill,
    border: REPO_TOKENS.prime.border,
  },
  reactor: {
    label: 'Nerves',
    color: REPO_TOKENS.reactor.fill,
    border: REPO_TOKENS.reactor.border,
  },
};

const NEUTRAL: RepoStyle = {
  label: 'Unknown',
  color: REPO_TOKENS.unknown.fill,
  border: REPO_TOKENS.unknown.border,
};

export function repoStyle(repo: TrinityRepo): RepoStyle {
  return REPO_STYLES[repo] ?? NEUTRAL;
}

export const DAG_STATE_COLORS: Record<string, string> = {
  pending: STATE.pending,
  running: STATE.running,
  applied: STATE.applied,
  fractured: STATE.danger,
  complete: STATE.ok,
};

export function dagStateColor(state: DagNodeState): string {
  return DAG_STATE_COLORS[state] ?? STATE.pending;
}
