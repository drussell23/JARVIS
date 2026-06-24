/**
 * Visual theming constants shared across components.
 *
 * The repo color map is load-bearing for the blast-radius graph: a
 * cross-boundary blast must be visually obvious. Body/jarvis=blue,
 * Mind/prime=amber, Nerves/reactor=violet. Unknown repos render
 * neutral gray (open vocabulary -- never crash on a new repo).
 */

import { DagNodeState, TrinityRepo } from './types';

export interface RepoStyle {
  readonly label: string;
  readonly color: string;
  readonly border: string;
}

const REPO_STYLES: Record<string, RepoStyle> = {
  jarvis: { label: 'Body', color: '#2563eb', border: '#3b82f6' },
  prime: { label: 'Mind', color: '#d97706', border: '#f59e0b' },
  reactor: { label: 'Nerves', color: '#7c3aed', border: '#8b5cf6' },
};

const NEUTRAL: RepoStyle = {
  label: 'Unknown',
  color: '#6b7280',
  border: '#9ca3af',
};

export function repoStyle(repo: TrinityRepo): RepoStyle {
  return REPO_STYLES[repo] ?? NEUTRAL;
}

export const DAG_STATE_COLORS: Record<string, string> = {
  pending: '#6b7280',
  running: '#2563eb',
  applied: '#0891b2',
  fractured: '#dc2626',
  complete: '#16a34a',
};

export function dagStateColor(state: DagNodeState): string {
  return DAG_STATE_COLORS[state] ?? '#6b7280';
}
