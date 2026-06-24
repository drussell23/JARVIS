/**
 * SwarmTopology component tests (Phase 1d).
 *
 *   - renders nodes from state (React Flow draws the labels)
 *   - an edge pulse appears on a message (animated edge present)
 *   - a node is removed on vaporize
 *   - a deadlock renders a severed-edge (danger) styling
 *   - a sentinel block surfaces a transient marker
 *   - empty state when no workers
 */

import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import SwarmTopology from '../components/SwarmTopology';
import { emptyTopology, reduceEvents } from '../lib/swarmTopology';
import { StreamEventFrame } from '../lib/types';

const G = 'graph-1';
let counter = 0;

function frame(
  eventType: string,
  payload: Record<string, unknown>,
): StreamEventFrame {
  return {
    schema_version: '1.0',
    event_id: `e-${(counter += 1)}`,
    event_type: eventType as StreamEventFrame['event_type'],
    op_id: (payload.op_id as string) ?? 'op-1',
    timestamp: 't',
    payload,
  };
}

function spawn(workerId: string, readOnly = false): StreamEventFrame {
  return frame('swarm_node_spawned', {
    graph_id: G,
    worker_id: workerId,
    role: readOnly ? 'reviewer' : 'coder',
    allowed_tools_count: 5,
    read_only: readOnly,
  });
}

describe('SwarmTopology', () => {
  test('renders worker nodes from state', () => {
    const state = reduceEvents([spawn('alpha'), spawn('beta', true)], 0);
    render(<SwarmTopology state={state} graphId={G} />);
    expect(screen.getByTestId('swarm-topology')).toBeInTheDocument();
    // React Flow renders node labels (role + worker_id).
    expect(screen.getByText(/alpha/)).toBeInTheDocument();
    expect(screen.getByText(/beta/)).toBeInTheDocument();
    // Live worker counter reflects the two spawns.
    expect(screen.getByTestId('swarm-counter')).toHaveTextContent('2');
  });

  test('an edge pulse appears on a message', () => {
    const state = reduceEvents(
      [
        spawn('alpha'),
        spawn('beta'),
        frame('swarm_message_sent', {
          graph_id: G,
          from_worker: 'alpha',
          to_worker: 'beta',
          kind: 'PROPOSE',
          msg_id: 'm1',
        }),
      ],
      0,
    );
    render(<SwarmTopology state={state} graphId={G} />);
    // React Flow does not render edge labels in jsdom (no DOM measurement),
    // so assert on the component-rendered live-pulse counter -- the
    // edge construction itself is proven in useSwarmTopology.test.ts.
    expect(screen.getByTestId('swarm-counter')).toHaveTextContent('live msgs');
    expect(screen.getByTestId('swarm-counter')).toHaveTextContent('1');
  });

  test('a node is removed on vaporize', () => {
    const state = reduceEvents(
      [
        spawn('alpha'),
        spawn('beta'),
        frame('swarm_node_vaporized', {
          graph_id: G,
          worker_id: 'beta',
          turns_cleared: 2,
        }),
      ],
      0,
    );
    render(<SwarmTopology state={state} graphId={G} />);
    expect(screen.getByText(/alpha/)).toBeInTheDocument();
    expect(screen.queryByText(/beta/)).toBeNull();
  });

  test('a deadlock renders a severed (danger) edge label', () => {
    const state = reduceEvents(
      [
        spawn('alpha'),
        spawn('beta'),
        frame('swarm_deadlock', {
          graph_id: G,
          pair: ['alpha', 'beta'],
          trigger: 'mutual_wait',
        }),
      ],
      0,
    );
    render(<SwarmTopology state={state} graphId={G} />);
    // The deadlock severed-edge label is a React Flow edge (not measurable
    // in jsdom); the component surfaces the deadlock via its counter chip.
    expect(screen.getByTestId('swarm-deadlock-count')).toHaveTextContent('1');
  });

  test('a sentinel block surfaces a transient marker', () => {
    const state = reduceEvents(
      [
        spawn('alpha'),
        frame('swarm_sentinel_block', { op_id: 'op-42', reason: 'credential_shape' }),
      ],
      0,
    );
    render(<SwarmTopology state={state} graphId={G} />);
    const blocks = screen.getByTestId('swarm-blocks');
    expect(blocks).toHaveTextContent('op-42');
    expect(blocks).toHaveTextContent('credential_shape');
  });

  test('empty state when no workers', () => {
    render(<SwarmTopology state={emptyTopology()} graphId={null} />);
    expect(screen.getByTestId('swarm-empty')).toBeInTheDocument();
  });
});
