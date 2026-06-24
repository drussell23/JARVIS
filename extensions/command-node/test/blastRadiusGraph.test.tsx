/**
 * BlastRadiusGraph tests.
 *
 *   - the pure buildBlastGraph: center + repo-color-coded nodes/edges
 *   - cross-boundary detection (distinctRepos > 1)
 *   - the component renders nodes from a fixture report and a click on
 *     a node opens the detail panel with repo / file / symbol.
 *
 * React Flow renders nodes as DOM; we drive a click via the node label.
 */

import { describe, expect, test } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import BlastRadiusGraph from '../components/BlastRadiusGraph';
import {
  buildBlastGraph,
  distinctRepos,
  CENTER_NODE_ID,
  affectedNodeId,
} from '../lib/blastGraph';
import { repoStyle } from '../lib/theme';
import { BlastRadiusResponse } from '../lib/types';

const FIXTURE: BlastRadiusResponse = {
  schema_version: '1.0',
  op_id: 'op-cross-1',
  directly_affected: [
    { repo: 'reactor', file: 'core/train.py', name: 'train_step', type: 'function' },
    { repo: 'prime', file: 'mind/router.py', name: 'route', type: 'function' },
  ],
  transitively_affected: [
    { repo: 'jarvis', file: 'backend/loop.py', name: 'GovernedLoop', type: 'class' },
  ],
  total_affected: 3,
  risk_level: 'critical',
};

describe('buildBlastGraph (pure)', () => {
  test('center node + one node per affected, colored by repo', () => {
    const { nodes, edges } = buildBlastGraph(FIXTURE);
    // 1 center + 2 direct + 1 transitive = 4 nodes; 3 edges from center.
    expect(nodes).toHaveLength(4);
    expect(edges).toHaveLength(3);
    expect(nodes[0]?.id).toBe(CENTER_NODE_ID);

    const reactorNode = nodes.find(
      (n) => n.id === affectedNodeId(FIXTURE.directly_affected[0]!),
    );
    const primeNode = nodes.find(
      (n) => n.id === affectedNodeId(FIXTURE.directly_affected[1]!),
    );
    const jarvisNode = nodes.find(
      (n) => n.id === affectedNodeId(FIXTURE.transitively_affected[0]!),
    );
    expect(reactorNode?.style?.background).toBe(repoStyle('reactor').color);
    expect(primeNode?.style?.background).toBe(repoStyle('prime').color);
    expect(jarvisNode?.style?.background).toBe(repoStyle('jarvis').color);

    // Every edge originates at the center (radial graph).
    expect(edges.every((e) => e.source === CENTER_NODE_ID)).toBe(true);
  });

  test('distinctRepos detects a cross-boundary blast', () => {
    expect(distinctRepos(FIXTURE).sort()).toEqual([
      'jarvis',
      'prime',
      'reactor',
    ]);
    const singleRepo: BlastRadiusResponse = {
      ...FIXTURE,
      directly_affected: [
        { repo: 'jarvis', file: 'a.py', name: 'x', type: 'function' },
      ],
      transitively_affected: [],
    };
    expect(distinctRepos(singleRepo)).toEqual(['jarvis']);
  });
});

describe('BlastRadiusGraph component', () => {
  test('renders the summary, the cross-boundary badge, and affected nodes', () => {
    render(<BlastRadiusGraph report={FIXTURE} />);
    expect(screen.getByTestId('blast-graph')).toBeInTheDocument();
    expect(screen.getByTestId('cross-boundary')).toHaveTextContent(
      'CROSS-BOUNDARY',
    );
    // Affected node labels are present (React Flow renders them).
    expect(screen.getByText(/train_step/)).toBeInTheDocument();
    expect(screen.getByText(/route/)).toBeInTheDocument();
  });

  test('clicking an affected node opens the detail panel', () => {
    render(<BlastRadiusGraph report={FIXTURE} />);
    // No detail until a node is clicked.
    expect(screen.queryByTestId('blast-detail')).toBeNull();

    fireEvent.click(screen.getByText(/train_step/));
    const detail = screen.getByTestId('blast-detail');
    expect(detail).toBeInTheDocument();
    expect(within(detail).getByText('core/train.py')).toBeInTheDocument();
    expect(within(detail).getByText(/Nerves \(reactor\)/)).toBeInTheDocument();
  });

  test('idle state when no report', () => {
    render(<BlastRadiusGraph report={null} />);
    expect(screen.getByTestId('blast-idle')).toBeInTheDocument();
  });

  test('error state surfaces the message', () => {
    render(<BlastRadiusGraph report={null} error="boom" />);
    expect(screen.getByTestId('blast-error')).toHaveTextContent('boom');
  });
});
