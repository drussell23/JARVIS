/**
 * Swarm-topology reducer + hook tests (Phase 1d).
 *
 *   - spawn adds a node (keyed by graph_id + worker_id)
 *   - message_sent creates a transient edge pulse that expires (~1.5s)
 *   - vaporize removes the node
 *   - deadlock marks the pair severed
 *   - out-of-order (message before spawn / vaporize unknown node) is
 *     handled gracefully
 *   - duplicate spawn is idempotent; duplicate message de-duped by msg_id
 *   - bounded: a flood is capped (nodes / pulses / graphs)
 */

import { describe, expect, test } from 'vitest';
import {
  MAX_GRAPHS,
  MAX_NODES_PER_GRAPH,
  PULSE_TTL_MS,
  buildSwarmFlowGraph,
  emptyTopology,
  reduceEvents,
  reduceFrame,
  sweepExpired,
} from '../lib/swarmTopology';
import { StreamEventFrame } from '../lib/types';

let counter = 0;
function frame(
  eventType: string,
  payload: Record<string, unknown>,
  id = `e-${(counter += 1)}`,
): StreamEventFrame {
  return {
    schema_version: '1.0',
    event_id: id,
    event_type: eventType as StreamEventFrame['event_type'],
    op_id: (payload.op_id as string) ?? 'op-1',
    timestamp: 't',
    payload,
  };
}

const G = 'graph-1';

function spawn(workerId: string, readOnly = false): StreamEventFrame {
  return frame('swarm_node_spawned', {
    graph_id: G,
    worker_id: workerId,
    role: readOnly ? 'reviewer' : 'coder',
    allowed_tools_count: readOnly ? 3 : 9,
    read_only: readOnly,
  });
}

function message(
  from: string,
  to: string,
  kind = 'PROPOSE',
  msgId = `m-${(counter += 1)}`,
): StreamEventFrame {
  return frame('swarm_message_sent', {
    graph_id: G,
    from_worker: from,
    to_worker: to,
    kind,
    msg_id: msgId,
  });
}

describe('reduceFrame: spawn', () => {
  test('spawn adds a node keyed by graph_id + worker_id', () => {
    const s = reduceFrame(emptyTopology(), spawn('w1'), 0);
    const g = s.graphs.get(G);
    expect(g).toBeDefined();
    expect(g!.nodes.has('w1')).toBe(true);
    expect(g!.nodes.get('w1')!.role).toBe('coder');
    expect(g!.nodes.get('w1')!.materialized).toBe(true);
  });

  test('duplicate spawn is idempotent (last write wins, one node)', () => {
    let s = reduceFrame(emptyTopology(), spawn('w1', false), 0);
    s = reduceFrame(s, spawn('w1', true), 10);
    const g = s.graphs.get(G)!;
    expect(g.nodes.size).toBe(1);
    expect(g.nodes.get('w1')!.readOnly).toBe(true);
  });
});

describe('reduceFrame: message pulse', () => {
  test('message_sent creates a transient edge that expires', () => {
    let s = reduceEvents([spawn('w1'), spawn('w2'), message('w1', 'w2')], 0);
    expect(s.graphs.get(G)!.pulses).toHaveLength(1);

    // Past the TTL it sweeps away.
    s = sweepExpired(s, PULSE_TTL_MS + 1);
    expect(s.graphs.get(G)!.pulses).toHaveLength(0);
  });

  test('the pulse becomes a React Flow edge while live', () => {
    const s = reduceEvents([spawn('w1'), spawn('w2'), message('w1', 'w2', 'CRITIQUE')], 0);
    const flow = buildSwarmFlowGraph(s.graphs.get(G));
    const edge = flow.edges.find((e) => e.data?.kind === 'pulse');
    expect(edge).toBeDefined();
    expect(edge!.source).toBe('w1');
    expect(edge!.target).toBe('w2');
    expect(edge!.animated).toBe(true);
    expect(edge!.label).toBe('CRITIQUE');
  });

  test('duplicate message (same msg_id) is de-duped within the window', () => {
    const s = reduceEvents(
      [spawn('w1'), spawn('w2'), message('w1', 'w2', 'X', 'dup'), message('w1', 'w2', 'X', 'dup')],
      0,
    );
    expect(s.graphs.get(G)!.pulses).toHaveLength(1);
  });
});

describe('reduceFrame: vaporize', () => {
  test('vaporize removes the node and its edges', () => {
    let s = reduceEvents([spawn('w1'), spawn('w2'), message('w1', 'w2')], 0);
    s = reduceFrame(
      s,
      frame('swarm_node_vaporized', { graph_id: G, worker_id: 'w2', turns_cleared: 4 }),
      0,
    );
    const g = s.graphs.get(G)!;
    expect(g.nodes.has('w2')).toBe(false);
    expect(g.nodes.has('w1')).toBe(true);
    // The pulse referencing the vaporized worker is dropped.
    expect(g.pulses).toHaveLength(0);
    // And it is gone from the rendered flow.
    expect(buildSwarmFlowGraph(g).nodes.map((n) => n.id)).toEqual(['w1']);
  });

  test('vaporize of an unknown node is a no-op (out-of-order safe)', () => {
    const base = reduceFrame(emptyTopology(), spawn('w1'), 0);
    const s = reduceFrame(
      base,
      frame('swarm_node_vaporized', { graph_id: G, worker_id: 'ghost', turns_cleared: 1 }),
      0,
    );
    expect(s.graphs.get(G)!.nodes.has('w1')).toBe(true);
  });
});

describe('reduceFrame: deadlock', () => {
  test('deadlock marks the pair with a severed (danger) edge', () => {
    let s = reduceEvents([spawn('w1'), spawn('w2')], 0);
    s = reduceFrame(
      s,
      frame('swarm_deadlock', { graph_id: G, pair: ['w1', 'w2'], trigger: 'mutual_wait' }),
      0,
    );
    const g = s.graphs.get(G)!;
    expect(g.deadlocks).toHaveLength(1);
    const flow = buildSwarmFlowGraph(g);
    const dl = flow.edges.find((e) => e.data?.kind === 'deadlock');
    expect(dl).toBeDefined();
    expect(dl!.style?.strokeDasharray).toBeDefined();
    expect(dl!.label).toContain('DEADLOCK');
  });

  test('duplicate deadlock pair (order-insensitive) is de-duped', () => {
    let s = reduceEvents([spawn('w1'), spawn('w2')], 0);
    s = reduceFrame(s, frame('swarm_deadlock', { graph_id: G, pair: ['w1', 'w2'], trigger: 't' }), 0);
    s = reduceFrame(s, frame('swarm_deadlock', { graph_id: G, pair: ['w2', 'w1'], trigger: 't' }), 0);
    expect(s.graphs.get(G)!.deadlocks).toHaveLength(1);
  });
});

describe('reduceFrame: out-of-order message', () => {
  test('message before spawn materializes dimmed placeholders, then upgrades', () => {
    let s = reduceFrame(emptyTopology(), message('w1', 'w2'), 0);
    const g0 = s.graphs.get(G)!;
    expect(g0.nodes.get('w1')!.materialized).toBe(false);
    expect(g0.pulses).toHaveLength(1);

    // The real spawn upgrades the placeholder in place.
    s = reduceFrame(s, spawn('w1'), 5);
    expect(s.graphs.get(G)!.nodes.get('w1')!.materialized).toBe(true);
  });
});

describe('reduceFrame: sentinel block', () => {
  test('sentinel_block adds a transient block that expires', () => {
    let s = reduceFrame(
      emptyTopology(),
      frame('swarm_sentinel_block', { op_id: 'op-9', reason: 'forbidden_path' }),
      0,
    );
    expect(s.blocks).toHaveLength(1);
    expect(s.blocks[0]!.opId).toBe('op-9');
    s = sweepExpired(s, 10_000);
    expect(s.blocks).toHaveLength(0);
  });
});

describe('bounded under flood', () => {
  test('nodes per graph are capped', () => {
    const events: StreamEventFrame[] = [];
    for (let i = 0; i < MAX_NODES_PER_GRAPH + 25; i += 1) {
      events.push(spawn(`w${i}`));
    }
    const s = reduceEvents(events, 0);
    expect(s.graphs.get(G)!.nodes.size).toBe(MAX_NODES_PER_GRAPH);
  });

  test('graphs are capped', () => {
    const events: StreamEventFrame[] = [];
    for (let i = 0; i < MAX_GRAPHS + 10; i += 1) {
      events.push(
        frame('swarm_node_spawned', {
          graph_id: `g${i}`,
          worker_id: 'w',
          role: 'r',
          allowed_tools_count: 1,
          read_only: false,
        }),
      );
    }
    const s = reduceEvents(events, 0);
    expect(s.graphs.size).toBe(MAX_GRAPHS);
  });
});

describe('non-swarm frames ignored', () => {
  test('a task/fsm frame does not perturb the topology', () => {
    const s = reduceFrame(
      emptyTopology(),
      frame('fsm_phase_changed', { phase: 'GENERATE' }),
      0,
    );
    expect(s.graphs.size).toBe(0);
  });
});
