'use client';

/**
 * BlastRadiusGraph -- the operator constraint, made visible.
 *
 * An interactive React Flow graph of GET /observability/blast-radius/
 * {op_id}: the mutated symbol at the center, edges to every directly-
 * and transitively-affected dependent. Nodes are color-coded by repo
 * (Body/jarvis=blue, Mind/prime=amber, Nerves/reactor=violet) so a
 * cross-boundary blast is visually obvious. Click a node -> detail
 * panel (repo / file / symbol / type). Read-only.
 */

import { useEffect, useMemo, useState } from 'react';
import ReactFlow, {
  Background,
  Controls,
  Node,
  NodeMouseHandler,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { AffectedNode, BlastRadiusResponse, TrinityRepo } from '../lib/types';
import { buildBlastGraph, distinctRepos } from '../lib/blastGraph';
import { repoStyle } from '../lib/theme';

export interface BlastRadiusGraphProps {
  /** The loaded report, or null while idle / loading. */
  readonly report: BlastRadiusResponse | null;
  readonly loading?: boolean;
  readonly error?: string | null;
}

function RepoLegend(): JSX.Element {
  const repos: TrinityRepo[] = ['jarvis', 'prime', 'reactor'];
  return (
    <div className="blast-legend" data-testid="blast-legend">
      {repos.map((r) => {
        const s = repoStyle(r);
        return (
          <span className="legend-item" key={r}>
            <span
              className="legend-swatch"
              style={{ backgroundColor: s.color, borderColor: s.border }}
            />
            {s.label} ({r})
          </span>
        );
      })}
    </div>
  );
}

function DetailPanel({
  node,
  onClose,
}: {
  readonly node: AffectedNode;
  readonly onClose: () => void;
}): JSX.Element {
  const s = repoStyle(node.repo);
  return (
    <aside className="blast-detail" data-testid="blast-detail" role="dialog"
      aria-label="affected node detail">
      <div className="blast-detail-head" style={{ borderColor: s.border }}>
        <strong>{node.name}</strong>
        <button className="link" onClick={onClose} aria-label="close detail">
          x
        </button>
      </div>
      <dl>
        <dt>Repo</dt>
        <dd>
          <span
            className="legend-swatch"
            style={{ backgroundColor: s.color, borderColor: s.border }}
          />
          {s.label} ({node.repo})
        </dd>
        <dt>File</dt>
        <dd className="mono">{node.file}</dd>
        <dt>Symbol</dt>
        <dd className="mono">{node.name}</dd>
        <dt>Type</dt>
        <dd>{node.type}</dd>
      </dl>
    </aside>
  );
}

export function BlastRadiusGraph({
  report,
  loading = false,
  error = null,
}: BlastRadiusGraphProps): JSX.Element {
  const [selected, setSelected] = useState<AffectedNode | null>(null);

  // Reset the selected detail whenever the report changes.
  useEffect(() => {
    setSelected(null);
  }, [report?.op_id]);

  const graph = useMemo(
    () => (report ? buildBlastGraph(report) : { nodes: [], edges: [] }),
    [report],
  );

  const repos = useMemo(
    () => (report ? distinctRepos(report) : []),
    [report],
  );
  const crossBoundary = repos.length > 1;

  const onNodeClick: NodeMouseHandler = (_evt, node: Node) => {
    const affected = (node.data as { affected?: AffectedNode } | undefined)
      ?.affected;
    if (affected) {
      setSelected(affected);
    }
  };

  if (error) {
    return (
      <div className="blast-graph blast-error" data-testid="blast-error">
        <span className="muted">Blast radius unavailable: {error}</span>
      </div>
    );
  }
  if (loading) {
    return (
      <div className="blast-graph" data-testid="blast-loading">
        <span className="muted">Loading blast radius...</span>
      </div>
    );
  }
  if (!report) {
    return (
      <div className="blast-graph blast-idle" data-testid="blast-idle">
        <span className="muted">
          Select an elevation to view its blast radius.
        </span>
      </div>
    );
  }

  return (
    <div className="blast-graph" data-testid="blast-graph">
      <div className="blast-summary">
        <span>
          op <span className="mono">{report.op_id}</span>
        </span>
        <span>total affected: {report.total_affected}</span>
        <span>risk: {report.risk_level}</span>
        {crossBoundary ? (
          <span className="badge badge-cross" data-testid="cross-boundary">
            CROSS-BOUNDARY ({repos.join(', ')})
          </span>
        ) : null}
      </div>
      <RepoLegend />
      <div className="blast-flow">
        <ReactFlow
          nodes={graph.nodes}
          edges={graph.edges}
          fitView
          proOptions={{ hideAttribution: true }}
          nodesDraggable={false}
          nodesConnectable={false}
          onNodeClick={onNodeClick}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      {selected ? (
        <DetailPanel node={selected} onClose={() => setSelected(null)} />
      ) : null}
    </div>
  );
}

export default BlastRadiusGraph;
