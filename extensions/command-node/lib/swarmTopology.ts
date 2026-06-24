/**
 * Pure swarm-topology state machine + React Flow graph projection.
 *
 * The reducer folds the swarm.* SSE event stream into a bounded topology
 * model, keyed by `graph_id`. Kept pure (no React, no clock side effects)
 * so it is trivially testable and reusable -- the hook (hooks/
 * useSwarmTopology.ts) is a thin React wrapper that supplies `now` and
 * drives the transient-expiry tick.
 *
 * Resilience contract (Phase 1d):
 *   - Out-of-order: a message_sent before the matching spawn implicitly
 *     materializes a placeholder node so the edge can still light up; a
 *     vaporize for an unknown node is a no-op.
 *   - Duplicate: spawning the same worker twice is idempotent (last write
 *     wins on role/read_only); a duplicate vaporize is a no-op.
 *   - Bounded: nodes per graph, pulses, blocks, and graphs are all capped;
 *     the oldest entries are dropped under flood.
 *
 * Read-only: this model only VISUALIZES; it never emits a control action.
 */

import type { Edge, Node } from 'reactflow';
import {
  StreamEventFrame,
  SwarmDeadlockPayload,
  SwarmMessageSentPayload,
  SwarmNodeSpawnedPayload,
  SwarmNodeVaporizedPayload,
  SwarmSentinelBlockPayload,
  isSwarmDeadlockEvent,
  isSwarmMessageSentEvent,
  isSwarmNodeSpawnedEvent,
  isSwarmNodeVaporizedEvent,
  isSwarmSentinelBlockEvent,
} from './types';
import { repoStyle } from './theme';
import { ACCENT, BORDER, STATE, SURFACE, TEXT } from './tokens';

// --- Bounds (env-free; mirror the SSE ring-buffer defaults) ---------------
export const MAX_GRAPHS = 16;
export const MAX_NODES_PER_GRAPH = 64;
export const MAX_PULSES = 128;
export const MAX_BLOCKS = 16;
/** Transient lifetime for a message pulse + a sentinel block (ms). */
export const PULSE_TTL_MS = 1500;
export const BLOCK_TTL_MS = 2500;

export interface SwarmNode {
  readonly workerId: string;
  readonly role: string;
  readonly allowedToolsCount: number;
  readonly readOnly: boolean;
  /** True once a swarm_node_spawned was actually seen (vs a placeholder
   * materialized by an out-of-order message). */
  readonly materialized: boolean;
  readonly spawnedAt: number;
}

export interface SwarmPulse {
  readonly id: string;
  readonly from: string;
  readonly to: string;
  readonly kind: string;
  readonly expiresAt: number;
}

export interface DeadlockPair {
  readonly a: string;
  readonly b: string;
  readonly trigger: string;
}

export interface SentinelBlock {
  readonly key: string;
  readonly opId: string;
  readonly reason: string;
  readonly expiresAt: number;
}

export interface SwarmGraph {
  readonly graphId: string;
  /** worker_id -> node. */
  readonly nodes: ReadonlyMap<string, SwarmNode>;
  readonly pulses: readonly SwarmPulse[];
  readonly deadlocks: readonly DeadlockPair[];
}

export interface SwarmTopologyState {
  /** graph_id -> graph. */
  readonly graphs: ReadonlyMap<string, SwarmGraph>;
  /** Sentinel blocks are op-scoped (not graph-scoped); kept globally. */
  readonly blocks: readonly SentinelBlock[];
  /** Monotonic counter for synthesizing stable keys without a clock. */
  readonly seq: number;
}

export function emptyTopology(): SwarmTopologyState {
  return { graphs: new Map(), blocks: [], seq: 0 };
}

function emptyGraph(graphId: string): SwarmGraph {
  return { graphId, nodes: new Map(), pulses: [], deadlocks: [] };
}

/** Bounded oldest-drop on an array. Pure. */
function capArray<T>(arr: readonly T[], cap: number): T[] {
  return arr.length > cap ? arr.slice(arr.length - cap) : arr.slice();
}

/** Cap a graph map by spawn order (oldest dropped). Pure. */
function capGraphs(
  graphs: Map<string, SwarmGraph>,
  cap: number,
): Map<string, SwarmGraph> {
  if (graphs.size <= cap) {
    return graphs;
  }
  // Map preserves insertion order; drop the oldest inserted graphs.
  const overflow = graphs.size - cap;
  const trimmed = new Map<string, SwarmGraph>();
  let i = 0;
  for (const [k, v] of graphs) {
    if (i >= overflow) {
      trimmed.set(k, v);
    }
    i += 1;
  }
  return trimmed;
}

function placeholderNode(workerId: string, at: number): SwarmNode {
  return {
    workerId,
    role: 'unknown',
    allowedToolsCount: 0,
    readOnly: false,
    materialized: false,
    spawnedAt: at,
  };
}

function withNode(
  graph: SwarmGraph,
  node: SwarmNode,
): SwarmGraph {
  const nodes = new Map(graph.nodes);
  nodes.set(node.workerId, node);
  // Cap nodes by spawn order (oldest dropped) -- but never drop nodes that
  // are referenced by a live pulse implicitly; simplest bounded policy is
  // insertion-order drop, which matches the SSE ring semantics.
  if (nodes.size > MAX_NODES_PER_GRAPH) {
    const overflow = nodes.size - MAX_NODES_PER_GRAPH;
    let i = 0;
    for (const k of nodes.keys()) {
      if (i >= overflow) {
        break;
      }
      nodes.delete(k);
      i += 1;
    }
  }
  return { ...graph, nodes };
}

// --- Reducer --------------------------------------------------------------

/** Fold one frame into the topology at logical time `now` (ms). Pure. */
export function reduceFrame(
  state: SwarmTopologyState,
  frame: StreamEventFrame,
  now: number,
): SwarmTopologyState {
  if (isSwarmNodeSpawnedEvent(frame)) {
    return applySpawn(state, frame.payload, now);
  }
  if (isSwarmMessageSentEvent(frame)) {
    return applyMessage(state, frame.payload, now);
  }
  if (isSwarmNodeVaporizedEvent(frame)) {
    return applyVaporize(state, frame.payload);
  }
  if (isSwarmDeadlockEvent(frame)) {
    return applyDeadlock(state, frame.payload);
  }
  if (isSwarmSentinelBlockEvent(frame)) {
    return applyBlock(state, frame.payload, now);
  }
  return state;
}

/** Fold a batch of frames, then sweep expired transients at `now`. Pure. */
export function reduceEvents(
  events: readonly StreamEventFrame[],
  now: number,
  initial: SwarmTopologyState = emptyTopology(),
): SwarmTopologyState {
  let state = initial;
  for (const frame of events) {
    state = reduceFrame(state, frame, now);
  }
  return sweepExpired(state, now);
}

function applySpawn(
  state: SwarmTopologyState,
  p: SwarmNodeSpawnedPayload,
  now: number,
): SwarmTopologyState {
  const graphs = new Map(state.graphs);
  const graph = graphs.get(p.graph_id) ?? emptyGraph(p.graph_id);
  const existing = graph.nodes.get(p.worker_id);
  const node: SwarmNode = {
    workerId: p.worker_id,
    role: p.role,
    allowedToolsCount: p.allowed_tools_count,
    readOnly: p.read_only,
    materialized: true,
    // Preserve original spawn time on a duplicate (idempotent).
    spawnedAt: existing?.materialized ? existing.spawnedAt : now,
  };
  graphs.set(p.graph_id, withNode(graph, node));
  return { ...state, graphs: capGraphs(graphs, MAX_GRAPHS), seq: state.seq + 1 };
}

function applyMessage(
  state: SwarmTopologyState,
  p: SwarmMessageSentPayload,
  now: number,
): SwarmTopologyState {
  const graphs = new Map(state.graphs);
  let graph = graphs.get(p.graph_id) ?? emptyGraph(p.graph_id);

  // Out-of-order: materialize placeholders for unseen endpoints so the
  // pulse renders. They upgrade in place when their spawn arrives.
  for (const w of [p.from_worker, p.to_worker]) {
    if (!graph.nodes.has(w)) {
      graph = withNode(graph, placeholderNode(w, now));
    }
  }

  const pulse: SwarmPulse = {
    // Stable, de-dupe-friendly key: msg_id + seq guards a duplicate frame.
    id: `${p.msg_id}:${state.seq}`,
    from: p.from_worker,
    to: p.to_worker,
    kind: p.kind,
    expiresAt: now + PULSE_TTL_MS,
  };
  // De-dupe by msg_id within the live window (same msg replayed twice).
  const live = graph.pulses.filter((x) => x.id.split(':')[0] !== p.msg_id);
  const pulses = capArray([...live, pulse], MAX_PULSES);
  graphs.set(p.graph_id, { ...graph, pulses });
  return { ...state, graphs: capGraphs(graphs, MAX_GRAPHS), seq: state.seq + 1 };
}

function applyVaporize(
  state: SwarmTopologyState,
  p: SwarmNodeVaporizedPayload,
): SwarmTopologyState {
  const graph = state.graphs.get(p.graph_id);
  if (graph === undefined || !graph.nodes.has(p.worker_id)) {
    // Unknown node / graph -> no-op (resilient to out-of-order).
    return state;
  }
  const graphs = new Map(state.graphs);
  const nodes = new Map(graph.nodes);
  nodes.delete(p.worker_id);
  // Drop pulses + deadlocks that reference the vaporized worker.
  const pulses = graph.pulses.filter(
    (x) => x.from !== p.worker_id && x.to !== p.worker_id,
  );
  const deadlocks = graph.deadlocks.filter(
    (d) => d.a !== p.worker_id && d.b !== p.worker_id,
  );
  graphs.set(p.graph_id, { ...graph, nodes, pulses, deadlocks });
  return { ...state, graphs, seq: state.seq + 1 };
}

function applyDeadlock(
  state: SwarmTopologyState,
  p: SwarmDeadlockPayload,
): SwarmTopologyState {
  const [a, b] = p.pair;
  const graphs = new Map(state.graphs);
  const graph = graphs.get(p.graph_id) ?? emptyGraph(p.graph_id);
  // De-dupe an existing pair (order-insensitive).
  const exists = graph.deadlocks.some(
    (d) =>
      (d.a === a && d.b === b) || (d.a === b && d.b === a),
  );
  if (exists) {
    return state;
  }
  const deadlocks = [...graph.deadlocks, { a, b, trigger: p.trigger }];
  graphs.set(p.graph_id, { ...graph, deadlocks });
  return { ...state, graphs: capGraphs(graphs, MAX_GRAPHS), seq: state.seq + 1 };
}

function applyBlock(
  state: SwarmTopologyState,
  p: SwarmSentinelBlockPayload,
  now: number,
): SwarmTopologyState {
  const block: SentinelBlock = {
    key: `${p.op_id}:${state.seq}`,
    opId: p.op_id,
    reason: p.reason,
    expiresAt: now + BLOCK_TTL_MS,
  };
  const blocks = capArray([...state.blocks, block], MAX_BLOCKS);
  return { ...state, blocks, seq: state.seq + 1 };
}

/** Drop expired pulses + blocks at `now`. Pure (returns same ref if no-op). */
export function sweepExpired(
  state: SwarmTopologyState,
  now: number,
): SwarmTopologyState {
  let mutated = false;
  const graphs = new Map<string, SwarmGraph>();
  for (const [id, g] of state.graphs) {
    const livePulses = g.pulses.filter((x) => x.expiresAt > now);
    if (livePulses.length !== g.pulses.length) {
      mutated = true;
      graphs.set(id, { ...g, pulses: livePulses });
    } else {
      graphs.set(id, g);
    }
  }
  const liveBlocks = state.blocks.filter((x) => x.expiresAt > now);
  if (liveBlocks.length !== state.blocks.length) {
    mutated = true;
  }
  if (!mutated) {
    return state;
  }
  return { ...state, graphs, blocks: liveBlocks };
}

// --- React Flow projection ------------------------------------------------

export interface SwarmFlowGraph {
  readonly nodes: Node[];
  readonly edges: Edge[];
}

const COLS = 4;
const CELL_W = 200;
const CELL_H = 130;

/** Distinct, stable graph ids present in the topology (insertion order). */
export function swarmGraphIds(state: SwarmTopologyState): string[] {
  return Array.from(state.graphs.keys());
}

/** Live counts for the on-canvas legend / counter. */
export interface SwarmCounters {
  readonly activeWorkers: number;
  readonly livePulses: number;
  readonly deadlockedPairs: number;
}

export function swarmCounters(graph: SwarmGraph | undefined): SwarmCounters {
  if (graph === undefined) {
    return { activeWorkers: 0, livePulses: 0, deadlockedPairs: 0 };
  }
  return {
    activeWorkers: graph.nodes.size,
    livePulses: graph.pulses.length,
    deadlockedPairs: graph.deadlocks.length,
  };
}

/** Color for a worker node: read-only=neutral repo token, mutating=accent. */
function nodeFill(node: SwarmNode): string {
  if (!node.materialized) {
    // A pulse-materialized placeholder we haven't seen a spawn for.
    return SURFACE.s3;
  }
  return node.readOnly ? repoStyle('reactor').color : ACCENT.deep;
}

function nodeBorder(node: SwarmNode): string {
  if (!node.materialized) {
    return BORDER.subtle;
  }
  return node.readOnly ? repoStyle('reactor').border : ACCENT.base;
}

/**
 * Derive React Flow nodes/edges from one swarm graph. Deterministic
 * grid layout (force-directed layout is delegated to React Flow's fitView
 * + the auto-layout the canvas applies; positions here are stable seeds).
 */
export function buildSwarmFlowGraph(
  graph: SwarmGraph | undefined,
): SwarmFlowGraph {
  if (graph === undefined) {
    return { nodes: [], edges: [] };
  }

  const workerIds = Array.from(graph.nodes.keys());
  const nodes: Node[] = workerIds.map((wid, i) => {
    const n = graph.nodes.get(wid)!;
    const label = n.materialized
      ? `${n.role}\n${n.workerId}${n.readOnly ? '\n(read-only)' : ''}`
      : `${n.workerId}\n(pending spawn)`;
    return {
      id: wid,
      position: {
        x: (i % COLS) * CELL_W,
        y: Math.floor(i / COLS) * CELL_H,
      },
      data: { label, worker: n },
      style: {
        background: nodeFill(n),
        color: TEXT.inverse,
        border: `2px solid ${nodeBorder(n)}`,
        borderRadius: 8,
        fontSize: 11,
        width: CELL_W - 50,
        opacity: n.materialized ? 1 : 0.6,
        whiteSpace: 'pre-line' as const,
      },
    };
  });

  const present = new Set(workerIds);
  const edges: Edge[] = [];

  // Deadlock edges first (severed, danger-toned, undirected look).
  for (const d of graph.deadlocks) {
    if (!present.has(d.a) || !present.has(d.b)) {
      continue;
    }
    edges.push({
      id: `deadlock-${d.a}-${d.b}`,
      source: d.a,
      target: d.b,
      animated: false,
      label: `DEADLOCK (${d.trigger})`,
      data: { kind: 'deadlock' },
      style: {
        stroke: STATE.danger,
        strokeWidth: 3,
        strokeDasharray: '6 4',
      },
      labelStyle: { fill: STATE.danger, fontSize: 9 },
    });
  }

  // Live message pulses (animated, accent-toned).
  for (const pulse of graph.pulses) {
    if (!present.has(pulse.from) || !present.has(pulse.to)) {
      continue;
    }
    edges.push({
      id: `pulse-${pulse.id}`,
      source: pulse.from,
      target: pulse.to,
      animated: true,
      label: pulse.kind,
      data: { kind: 'pulse' },
      style: { stroke: ACCENT.bright, strokeWidth: 2 },
      labelStyle: { fill: ACCENT.bright, fontSize: 9 },
    });
  }

  return { nodes, edges };
}
