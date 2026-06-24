'use client';

/**
 * The Sovereign Command Node mission-control page (Phase 1, read-only).
 *
 * 3-region layout:
 *   - top:  FSM ribbon (one per active op)
 *   - main: live execution DAG canvas
 *   - side: blast-radius graph + critical-elevation queue
 * plus a connection-status indicator and sovereign_yield toasts.
 *
 * Blast radius loads on demand: when a cross_repo_elevation_pending
 * event arrives (newest first) we auto-focus its op, and the operator
 * can re-focus any pending elevation via "view blast radius".
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSovereignStream } from '../hooks/useSovereignStream';
import { resolveConfig } from '../lib/config';
import { ObservabilityClient } from '../lib/api';
import { BlastRadiusResponse } from '../lib/types';
import {
  projectDagNodes,
  projectElevationQueue,
  projectFsmStates,
  projectYieldAlerts,
} from '../lib/projection';
import FSMStateStream from '../components/FSMStateStream';
import DAGCanvas from '../components/DAGCanvas';
import BlastRadiusGraph from '../components/BlastRadiusGraph';
import ElevationQueue from '../components/ElevationQueue';
import ConnectionStatus from '../components/ConnectionStatus';
import YieldToasts from '../components/YieldToasts';

export default function Page(): JSX.Element {
  const cfg = useMemo(() => resolveConfig(), []);
  const { events, connectionState, lastEventId } = useSovereignStream();

  const fsmOps = useMemo(() => projectFsmStates(events), [events]);
  const dagNodes = useMemo(() => projectDagNodes(events), [events]);
  const elevations = useMemo(() => projectElevationQueue(events), [events]);
  const yieldAlerts = useMemo(() => projectYieldAlerts(events), [events]);

  const [focusedOpId, setFocusedOpId] = useState<string | null>(null);
  const [report, setReport] = useState<BlastRadiusResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const clientRef = useRef<ObservabilityClient | null>(null);
  if (clientRef.current === null) {
    clientRef.current = new ObservabilityClient({
      endpoint: cfg.observabilityBase,
    });
  }

  const loadBlastRadius = useCallback(async (opId: string): Promise<void> => {
    setFocusedOpId(opId);
    setLoading(true);
    setError(null);
    try {
      const r = await clientRef.current!.blastRadius(opId);
      setReport(r);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setReport(null);
    } finally {
      setLoading(false);
    }
  }, []);

  // Auto-focus the newest pending elevation that we haven't loaded yet.
  const newestOpId = elevations.length > 0 ? elevations[0]!.opId : null;
  useEffect(() => {
    if (newestOpId !== null && newestOpId !== focusedOpId) {
      void loadBlastRadius(newestOpId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [newestOpId]);

  return (
    <main className="command-node">
      <div className="cn-topbar">
        <strong>JARVIS Sovereign Command Node</strong>
        <ConnectionStatus state={connectionState} lastEventId={lastEventId} />
      </div>

      <div className="cn-fsm">
        <FSMStateStream ops={fsmOps} />
      </div>

      <div className="cn-dag">
        <DAGCanvas nodes={dagNodes} />
      </div>

      <div className="cn-side">
        <BlastRadiusGraph report={report} loading={loading} error={error} />
        <ElevationQueue
          entries={elevations}
          onViewBlastRadius={(opId) => void loadBlastRadius(opId)}
          focusedOpId={focusedOpId}
        />
      </div>

      <YieldToasts alerts={yieldAlerts} />
    </main>
  );
}
