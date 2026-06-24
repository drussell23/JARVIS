/**
 * Projection tests -- the pure event-stream -> view-model transforms.
 */

import { describe, expect, test } from 'vitest';
import {
  projectDagNodes,
  projectElevationQueue,
  projectFsmStates,
  projectYieldAlerts,
} from '../lib/projection';
import { StreamEventFrame } from '../lib/types';

function frame(
  eventType: string,
  payload: Record<string, unknown>,
  opId = 'op-1',
  id = `e-${Math.random()}`,
): StreamEventFrame {
  return {
    schema_version: '1.0',
    event_id: id,
    event_type: eventType as StreamEventFrame['event_type'],
    op_id: opId,
    timestamp: 't',
    payload,
  };
}

describe('projectFsmStates', () => {
  test('keeps the latest phase per op and drops completed ops', () => {
    const events = [
      frame('fsm_phase_changed', { phase: 'CLASSIFY' }, 'op-1'),
      frame('fsm_phase_changed', { phase: 'GENERATE', route: 'STANDARD', risk_tier: 'notify_apply' }, 'op-1'),
      frame('fsm_phase_changed', { phase: 'PLAN' }, 'op-2'),
      frame('task_completed', {}, 'op-2'),
    ];
    const states = projectFsmStates(events);
    expect(states).toHaveLength(1);
    expect(states[0]?.opId).toBe('op-1');
    expect(states[0]?.phase).toBe('GENERATE');
    expect(states[0]?.route).toBe('STANDARD');
    expect(states[0]?.riskTier).toBe('notify_apply');
  });
});

describe('projectDagNodes', () => {
  test('dag_node_updated wins; task events seed coarse nodes', () => {
    const events = [
      frame('task_created', {}, 'op-7'),
      frame('dag_node_updated', { node_id: 'n9', state: 'fractured' }, 'op-7'),
    ];
    const nodes = projectDagNodes(events);
    const n9 = nodes.find((n) => n.nodeId === 'n9');
    expect(n9?.state).toBe('fractured');
  });
});

describe('projectElevationQueue', () => {
  test('de-dupes by pr_id, newest first', () => {
    const events = [
      frame('cross_repo_elevation_pending', { pr_id: 'pr-1', target_repo: 'prime', blast_radius_summary: 'a' }, 'op-a'),
      frame('cross_repo_elevation_pending', { pr_id: 'pr-2', target_repo: 'reactor', blast_radius_summary: 'b' }, 'op-b'),
      frame('cross_repo_elevation_pending', { pr_id: 'pr-1', target_repo: 'prime', blast_radius_summary: 'a2' }, 'op-a'),
    ];
    const q = projectElevationQueue(events);
    // De-duped to 2 entries. Order is by first-seen, reversed, so the
    // most-recently-first-seen pr (pr-2) leads; pr-1 retains its
    // original slot but its summary is updated in place to 'a2'.
    expect(q).toHaveLength(2);
    expect(q.map((e) => e.pr_id)).toEqual(['pr-2', 'pr-1']);
    const pr1 = q.find((e) => e.pr_id === 'pr-1');
    expect(pr1?.blast_radius_summary).toBe('a2');
  });
});

describe('projectYieldAlerts', () => {
  test('returns newest first, capped', () => {
    const events = [
      frame('sovereign_yield', { reason: 'FRACTURE' }, 'op-1', 'y1'),
      frame('sovereign_yield', { reason: 'RECOVERED' }, 'op-1', 'y2'),
    ];
    const alerts = projectYieldAlerts(events, 1);
    expect(alerts).toHaveLength(1);
    expect(alerts[0]?.reason).toBe('RECOVERED');
  });
});
