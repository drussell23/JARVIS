'use client';

/**
 * useSwarmTopology -- reducer-driven swarm-mesh state from the swarm.* SSE
 * event stream (Phase 1d).
 *
 * Consumes the SAME `StreamEventFrame[]` buffer that useSovereignStream
 * already maintains (no new SSE client). It folds only the swarm.* frames
 * (everything else is ignored) into a bounded topology model and runs a
 * lightweight ticker that expires transient message pulses + sentinel
 * blocks (~1.5s / ~2.5s) so the canvas pulses then settles.
 *
 * The fold is fully derived from the (capped) event buffer on every render
 * via `reduceEvents`, so it is order-independent and idempotent -- a
 * re-render with the same events yields the same topology. The ticker only
 * advances `now` to drive transient expiry; it holds no authoritative
 * state.
 *
 * Read-only: this hook never POSTs and exposes no write surface.
 */

import { useEffect, useMemo, useState } from 'react';
import { StreamEventFrame } from '../lib/types';
import {
  PULSE_TTL_MS,
  SwarmTopologyState,
  buildSwarmFlowGraph,
  reduceEvents,
  swarmCounters,
  swarmGraphIds,
} from '../lib/swarmTopology';

export interface UseSwarmTopologyOptions {
  /** Test injection -- defaults to Date.now. */
  readonly nowFn?: () => number;
  /** Test injection -- transient-expiry tick interval (ms). */
  readonly tickMs?: number;
}

export interface UseSwarmTopologyResult {
  readonly state: SwarmTopologyState;
  /** Distinct active graph ids (for the graph selector). */
  readonly graphIds: readonly string[];
}

export function useSwarmTopology(
  events: readonly StreamEventFrame[],
  options: UseSwarmTopologyOptions = {},
): UseSwarmTopologyResult {
  const nowFn = options.nowFn ?? Date.now;
  // Half the pulse TTL keeps expiry visually crisp without a busy loop.
  const tickMs = options.tickMs ?? Math.floor(PULSE_TTL_MS / 2);

  // `now` is bumped by the ticker; recomputing on its change sweeps
  // expired transients. We snapshot the clock into state so renders are
  // deterministic for a given (events, now) pair.
  const [now, setNow] = useState<number>(() => nowFn());

  useEffect(() => {
    const id = setInterval(() => setNow(nowFn()), tickMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tickMs]);

  const state = useMemo(
    () => reduceEvents(events, now),
    [events, now],
  );

  const graphIds = useMemo(() => swarmGraphIds(state), [state]);

  return { state, graphIds };
}

export { buildSwarmFlowGraph, swarmCounters };
