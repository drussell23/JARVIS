/**
 * CommandCenter — Slice 110 Native Sovereign Interface
 * ====================================================
 * The top-level dashboard. Owns one ObservabilityClient, fans incoming gateway
 * frames into the four panels (Causality DAG, Telemetry gauges, Live stream,
 * Voice control), and re-fetches the causality graph on a slow cadence so the
 * force layout stays fresh even between cognitive events.
 *
 * Consumes ONLY the read-only gateway (+ the cosmetic voice POST). No authority
 * is reachable from here.
 */

import React, { useEffect, useReducer, useRef, useState } from 'react';
import { motion } from 'framer-motion';
import ObservabilityClient from '../../services/ObservabilityClient';
import CausalityGraph from './CausalityGraph';
import TelemetryGauges from './TelemetryGauges';
import LiveTerminal from './LiveTerminal';
import VoiceToggle from './VoiceToggle';
import './CommandCenter.css';

const MAX_LINES = 200;

function linesReducer(state, action) {
  if (action.type === 'push') {
    const next = state.concat(action.frame);
    return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
  }
  return state;
}

export default function CommandCenter() {
  const clientRef = useRef(null);
  const [connected, setConnected] = useState(false);
  const [telemetry, setTelemetry] = useState({});
  const [graph, setGraph] = useState({ nodes: [], edges: [] });
  const [health, setHealth] = useState(null);
  const [breachPulse, setBreachPulse] = useState(0);
  const [lines, dispatchLines] = useReducer(linesReducer, []);

  useEffect(() => {
    const client = new ObservabilityClient();
    clientRef.current = client;

    const offAny = client.onAny((frame) => {
      if (frame.kind === '__connection__') {
        setConnected(!!frame.payload?.up);
        return;
      }
      dispatchLines({ type: 'push', frame });
    });
    const offTele = client.on('telemetry', (f) => setTelemetry(f.payload || {}));
    const offCausal = client.on('causality_update', (f) => setGraph(f.payload || { nodes: [], edges: [] }));
    const offBreach = client.on('containment_breach', () => setBreachPulse((p) => p + 1));

    client.connect();
    client.fetchHealth().then(setHealth).catch(() => {});

    // Slow causality refresh (the force layout stays alive between events).
    const causalTimer = setInterval(() => {
      client.fetchCausality(30).then((g) => {
        if (g && Array.isArray(g.nodes)) setGraph(g);
      }).catch(() => {});
    }, 8000);

    return () => {
      offAny(); offTele(); offCausal(); offBreach();
      clearInterval(causalTimer);
      client.disconnect();
    };
  }, []);

  return (
    <div className="cc-root">
      <header className="cc-header">
        <div className="cc-brand">
          <span className="cc-brand-mark">⊘</span> O+V · SOVEREIGN COMMAND CENTER
        </div>
        <div className="cc-status">
          <span className={`cc-conn ${connected ? 'cc-conn-up' : 'cc-conn-down'}`}>
            {connected ? '● LIVE' : '○ RECONNECTING'}
          </span>
          {health?.cognitive_bus_enabled
            ? <span className="cc-badge cc-ok">cognitive bus</span>
            : <span className="cc-badge cc-warn">bus off</span>}
        </div>
      </header>

      <motion.div
        key={breachPulse}
        className="cc-grid"
        animate={breachPulse ? { boxShadow: ['0 0 0 rgba(248,113,113,0)', '0 0 40px rgba(248,113,113,0.5)', '0 0 0 rgba(248,113,113,0)'] } : {}}
        transition={{ duration: 1.2 }}
      >
        <CausalityGraph graph={graph} />
        <TelemetryGauges telemetry={telemetry} />
        <LiveTerminal lines={lines} />
        <VoiceToggle client={clientRef.current} health={health} />
      </motion.div>
    </div>
  );
}
