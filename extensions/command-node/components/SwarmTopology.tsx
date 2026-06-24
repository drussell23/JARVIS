'use client';

/**
 * SwarmTopology -- the live swarm mesh (Phase 1d).
 *
 * A React Flow canvas of the AgentMessageBus swarm for one graph_id:
 *   - nodes appear on swarm_node_spawned (labeled role + worker_id;
 *     mutating workers = accent, read-only workers = Nerves/violet token,
 *     pre-spawn placeholders dimmed)
 *   - edges light up in real time on swarm_message_sent (animated pulse
 *     from->to, labeled with the message kind, auto-expiring ~1.5s)
 *   - nodes physically disappear on swarm_node_vaporized
 *   - a deadlocked pair shows a danger-toned severed edge
 *   - a sentinel-block surfaces a transient blocked-message marker
 *
 * Styled entirely with the Sovereign CSS-var tokens (no hardcoded hex):
 * the React Flow node/edge inline styles pull `var(--token)` strings from
 * lib/tokens.ts. Read-only -- this only visualizes the mesh.
 */

import { useMemo } from 'react';
import ReactFlow, { Background, Controls } from 'reactflow';
import 'reactflow/dist/style.css';
import {
  SwarmTopologyState,
  buildSwarmFlowGraph,
  swarmCounters,
} from '../lib/swarmTopology';

export interface SwarmTopologyProps {
  readonly state: SwarmTopologyState;
  /** The graph_id to render; null/unknown -> empty canvas. */
  readonly graphId: string | null;
}

function Legend(): JSX.Element {
  return (
    <div className="swarm-legend" data-testid="swarm-legend">
      <span className="legend-item">
        <span className="legend-swatch swarm-sw-mutate" />
        mutating worker
      </span>
      <span className="legend-item">
        <span className="legend-swatch swarm-sw-readonly" />
        read-only worker
      </span>
      <span className="legend-item">
        <span className="legend-swatch swarm-sw-pulse" />
        message pulse
      </span>
      <span className="legend-item">
        <span className="legend-swatch swarm-sw-deadlock" />
        deadlock
      </span>
    </div>
  );
}

export function SwarmTopology({
  state,
  graphId,
}: SwarmTopologyProps): JSX.Element {
  const graph = graphId !== null ? state.graphs.get(graphId) : undefined;

  const flow = useMemo(() => buildSwarmFlowGraph(graph), [graph]);
  const counters = useMemo(() => swarmCounters(graph), [graph]);

  // Sentinel blocks are op-scoped (global, not per-graph); surface the live
  // ones as transient markers regardless of which graph is focused.
  const blocks = state.blocks;

  const empty = graph === undefined || graph.nodes.size === 0;

  return (
    <div className="swarm-topology" data-testid="swarm-topology">
      <div className="swarm-counter" data-testid="swarm-counter">
        <span>
          workers: <strong>{counters.activeWorkers}</strong>
        </span>
        <span>
          live msgs: <strong>{counters.livePulses}</strong>
        </span>
        {counters.deadlockedPairs > 0 ? (
          <span className="swarm-deadlock-count" data-testid="swarm-deadlock-count">
            deadlocks: <strong>{counters.deadlockedPairs}</strong>
          </span>
        ) : null}
      </div>

      <Legend />

      <div className="swarm-flow">
        {empty ? (
          <div className="swarm-empty muted" data-testid="swarm-empty">
            No swarm workers yet.
          </div>
        ) : (
          <ReactFlow
            nodes={flow.nodes}
            edges={flow.edges}
            fitView
            proOptions={{ hideAttribution: true }}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable={false}
          >
            <Background />
            <Controls showInteractive={false} />
          </ReactFlow>
        )}
      </div>

      {blocks.length > 0 ? (
        <div className="swarm-blocks" data-testid="swarm-blocks">
          {blocks.map((b) => (
            <div className="swarm-block" key={b.key} role="status">
              <span className="swarm-block-icon" aria-hidden="true">
                BLOCKED
              </span>
              <span className="mono">{b.opId}</span>
              <span className="muted">{b.reason}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export default SwarmTopology;
