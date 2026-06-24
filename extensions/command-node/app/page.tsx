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
import { useSwarmTopology } from '../hooks/useSwarmTopology';
import { useBiometricAuth } from '../hooks/useBiometricAuth';
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
import SwarmTopology from '../components/SwarmTopology';
import BlastRadiusGraph from '../components/BlastRadiusGraph';
import ElevationQueue from '../components/ElevationQueue';
import type { AuthorizeTarget } from '../components/ElevationQueue';
import AuthorizeElevationModal from '../components/AuthorizeElevationModal';
import ConnectionStatus from '../components/ConnectionStatus';
import YieldToasts from '../components/YieldToasts';

export default function Page(): JSX.Element {
  const cfg = useMemo(() => resolveConfig(), []);
  const { events, connectionState, lastEventId } = useSovereignStream();

  const fsmOps = useMemo(() => projectFsmStates(events), [events]);
  const dagNodes = useMemo(() => projectDagNodes(events), [events]);
  const elevations = useMemo(() => projectElevationQueue(events), [events]);
  const yieldAlerts = useMemo(() => projectYieldAlerts(events), [events]);

  // Phase 1d swarm topology (same SSE buffer, swarm.* frames folded).
  const swarm = useSwarmTopology(events);
  const [mainPanel, setMainPanel] = useState<'dag' | 'swarm'>('dag');
  const [swarmGraphId, setSwarmGraphId] = useState<string | null>(null);

  // Auto-focus the newest swarm graph; keep the operator's pick if still live.
  useEffect(() => {
    if (swarm.graphIds.length === 0) {
      if (swarmGraphId !== null) {
        setSwarmGraphId(null);
      }
      return;
    }
    if (swarmGraphId === null || !swarm.graphIds.includes(swarmGraphId)) {
      setSwarmGraphId(swarm.graphIds[swarm.graphIds.length - 1]!);
    }
  }, [swarm.graphIds, swarmGraphId]);

  const [focusedOpId, setFocusedOpId] = useState<string | null>(null);
  const [report, setReport] = useState<BlastRadiusResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // --- Biometric write-path (Phase 2) -------------------------------------
  // The modal is unreachable until an operator clicks Authorize on a pending
  // elevation. The hook drives the FSM <-> API <-> Web Audio flow; the
  // backend is the sole authority on the verdict.
  const auth = useBiometricAuth({ endpoint: cfg.observabilityBase });
  const [authTarget, setAuthTarget] = useState<AuthorizeTarget | null>(null);

  const onAuthorize = useCallback(
    (target: AuthorizeTarget): void => {
      setAuthTarget(target);
      void auth.open({
        prId: target.prId,
        astMutationId: target.astMutationId,
        blastRadiusHash: target.blastRadiusHash,
      });
    },
    [auth],
  );

  const closeAuth = useCallback((): void => {
    auth.reset();
    setAuthTarget(null);
  }, [auth]);

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
        <div className="cn-tabs" role="tablist" aria-label="topology view">
          <button
            className={`cn-tab${mainPanel === 'dag' ? ' active' : ''}`}
            role="tab"
            aria-selected={mainPanel === 'dag'}
            onClick={() => setMainPanel('dag')}
          >
            Execution DAG
          </button>
          <button
            className={`cn-tab${mainPanel === 'swarm' ? ' active' : ''}`}
            role="tab"
            aria-selected={mainPanel === 'swarm'}
            onClick={() => setMainPanel('swarm')}
          >
            Swarm Topology
            {swarm.graphIds.length > 0 ? (
              <span className="cn-tab-count">{swarm.graphIds.length}</span>
            ) : null}
          </button>
          {mainPanel === 'swarm' && swarm.graphIds.length > 1 ? (
            <select
              className="cn-graph-select"
              data-testid="swarm-graph-select"
              aria-label="swarm graph selector"
              value={swarmGraphId ?? ''}
              onChange={(e) => setSwarmGraphId(e.target.value)}
            >
              {swarm.graphIds.map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </select>
          ) : null}
        </div>
        <div className="cn-panel-body">
          {mainPanel === 'dag' ? (
            <DAGCanvas nodes={dagNodes} />
          ) : (
            <SwarmTopology state={swarm.state} graphId={swarmGraphId} />
          )}
        </div>
      </div>

      <div className="cn-side">
        <BlastRadiusGraph report={report} loading={loading} error={error} />
        <ElevationQueue
          entries={elevations}
          onViewBlastRadius={(opId) => void loadBlastRadius(opId)}
          onAuthorize={onAuthorize}
          focusedOpId={focusedOpId}
          authDisabled={auth.disabled}
        />
      </div>

      <YieldToasts alerts={yieldAlerts} />

      {authTarget !== null ? (
        <AuthorizeElevationModal
          auth={auth}
          prId={authTarget.prId}
          targetRepo={authTarget.targetRepo}
          onClose={closeAuth}
        />
      ) : null}
    </main>
  );
}
