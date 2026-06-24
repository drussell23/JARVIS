/**
 * Pure graph-building for the blast-radius visualization. Separated
 * from the React component so the node/edge construction + repo color
 * coding is unit-testable without rendering React Flow.
 */

import type { Edge, Node } from 'reactflow';
import { AffectedNode, BlastRadiusResponse } from './types';
import { repoStyle } from './theme';
import { SURFACE, TEXT, BORDER } from './tokens';

export const CENTER_NODE_ID = '__center__';

/** Stable id for an affected node (repo + file + name are unique). */
export function affectedNodeId(n: AffectedNode): string {
  return `${n.repo}::${n.file}::${n.name}`;
}

export interface BlastFlowGraph {
  readonly nodes: Node[];
  readonly edges: Edge[];
}

interface BuildOptions {
  /** Radius of the dependent ring(s) in px. */
  readonly directRadius?: number;
  readonly transitiveRadius?: number;
}

/**
 * Build a radial graph: the mutated symbol at the center, a ring of
 * directly-affected dependents, and an outer ring of transitively-
 * affected dependents. Each node is colored by its repo so a cross-
 * boundary blast (e.g. a reactor mutation with prime/jarvis dependents)
 * is visually obvious.
 */
export function buildBlastGraph(
  report: BlastRadiusResponse,
  opts: BuildOptions = {},
): BlastFlowGraph {
  const directRadius = opts.directRadius ?? 220;
  const transitiveRadius = opts.transitiveRadius ?? 420;

  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // Center: the mutated op/symbol.
  nodes.push({
    id: CENTER_NODE_ID,
    position: { x: 0, y: 0 },
    data: {
      label: `${report.op_id}\n(mutated)`,
      kind: 'center',
    },
    style: {
      background: SURFACE.center,
      color: TEXT.inverse,
      border: `2px solid ${BORDER.bright}`,
      borderRadius: 10,
      fontSize: 12,
      width: 150,
      whiteSpace: 'pre-line' as const,
    },
  });

  const place = (
    list: readonly AffectedNode[],
    radius: number,
    edgeStyle: 'direct' | 'transitive',
  ): void => {
    const n = list.length;
    list.forEach((node, i) => {
      const angle = (2 * Math.PI * i) / Math.max(n, 1);
      const id = affectedNodeId(node);
      const style = repoStyle(node.repo);
      nodes.push({
        id,
        position: {
          x: Math.cos(angle) * radius,
          y: Math.sin(angle) * radius,
        },
        data: {
          label: `${node.name}\n${node.repo}/${node.file}`,
          affected: node,
          kind: edgeStyle,
        },
        style: {
          background: style.color,
          color: TEXT.inverse,
          border: `2px solid ${style.border}`,
          borderRadius: 8,
          fontSize: 10,
          width: 140,
          whiteSpace: 'pre-line' as const,
        },
      });
      edges.push({
        id: `e-${edgeStyle}-${id}`,
        source: CENTER_NODE_ID,
        target: id,
        animated: edgeStyle === 'direct',
        style: {
          stroke: style.border,
          strokeDasharray: edgeStyle === 'transitive' ? '4 3' : undefined,
        },
      });
    });
  };

  place(report.directly_affected, directRadius, 'direct');
  place(report.transitively_affected, transitiveRadius, 'transitive');

  return { nodes, edges };
}

/** Count distinct repos present in a blast report (cross-boundary
 * detection: >1 means the blast crosses a Trinity boundary). */
export function distinctRepos(report: BlastRadiusResponse): string[] {
  const set = new Set<string>();
  for (const n of [
    ...report.directly_affected,
    ...report.transitively_affected,
  ]) {
    set.add(n.repo);
  }
  return Array.from(set);
}
