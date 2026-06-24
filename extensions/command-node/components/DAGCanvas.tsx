'use client';

/**
 * DAGCanvas -- the live execution DAG.
 *
 * Renders nodes from the projected DAG view (dag_node_updated + task_*
 * events), colored by state (pending/running/applied/fractured/
 * complete). Uses React Flow for layout + pan/zoom. Read-only.
 *
 * Phase 1 has no edge stream from the backend yet; nodes are laid out
 * in a simple grid keyed by op so the operator sees concurrent ops at a
 * glance. When edge events land in a later phase, wire them here.
 */

import { useMemo } from 'react';
import ReactFlow, {
  Background,
  Controls,
  Edge,
  Node,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { DagNodeView } from '../lib/projection';
import { dagStateColor } from '../lib/theme';
import { TEXT, BORDER } from '../lib/tokens';

export interface DAGCanvasProps {
  readonly nodes: readonly DagNodeView[];
}

const COLS = 4;
const CELL_W = 200;
const CELL_H = 120;

export function buildFlowNodes(views: readonly DagNodeView[]): Node[] {
  return views.map((v, i) => ({
    id: v.nodeId,
    position: {
      x: (i % COLS) * CELL_W,
      y: Math.floor(i / COLS) * CELL_H,
    },
    data: { label: `${v.nodeId}\n${v.state}` },
    style: {
      background: dagStateColor(v.state),
      color: TEXT.inverse,
      border: `1px solid ${BORDER.nodeEdge}`,
      borderRadius: 8,
      fontSize: 11,
      width: CELL_W - 40,
      whiteSpace: 'pre-line' as const,
    },
  }));
}

export function DAGCanvas({ nodes }: DAGCanvasProps): JSX.Element {
  const flowNodes = useMemo(() => buildFlowNodes(nodes), [nodes]);
  const flowEdges = useMemo<Edge[]>(() => [], []);

  return (
    <div className="dag-canvas" data-testid="dag-canvas">
      {nodes.length === 0 ? (
        <div className="dag-empty muted">No DAG nodes yet.</div>
      ) : (
        <ReactFlow
          nodes={flowNodes}
          edges={flowEdges}
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
  );
}

export default DAGCanvas;
