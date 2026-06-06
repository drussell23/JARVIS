/**
 * TelemetryGauges — Slice 110
 * ===========================
 * Real-time radial gauges for the cognitive telemetry: Shannon entropy of the
 * domain distribution (0→1) and the confidence aura band (high/medium/low),
 * plus a compact belief prior-distribution bar. SVG arcs animated with
 * framer-motion (already a frontend dependency).
 *
 * Props: telemetry = { shannon_entropy, confidence_aura, confidence_score,
 *                      recursion_depth, decision_prior_distribution }
 */

import React from 'react';
import { motion } from 'framer-motion';

const R = 54;
const CIRC = 2 * Math.PI * R;

const AURA = {
  high: { v: 0.92, color: '#34d399', label: 'HIGH' },
  medium: { v: 0.6, color: '#fbbf24', label: 'MEDIUM' },
  low: { v: 0.28, color: '#f87171', label: 'LOW' },
};

const PRIOR_COLOR = {
  stable: '#34d399',
  drifting: '#fbbf24',
  falsified: '#f87171',
  disabled: '#52525b',
};

function Gauge({ label, value, display, color }) {
  const v = Math.max(0, Math.min(1, value ?? 0));
  const dash = CIRC * v;
  return (
    <div className="cc-gauge">
      <svg viewBox="0 0 140 140" className="cc-gauge-svg">
        <circle cx="70" cy="70" r={R} className="cc-gauge-track" />
        <motion.circle
          cx="70" cy="70" r={R}
          className="cc-gauge-arc"
          stroke={color}
          strokeDasharray={`${dash} ${CIRC}`}
          transform="rotate(-90 70 70)"
          initial={false}
          animate={{ strokeDasharray: `${dash} ${CIRC}` }}
          transition={{ type: 'spring', stiffness: 80, damping: 18 }}
        />
        <text x="70" y="66" className="cc-gauge-value" fill={color}>{display}</text>
        <text x="70" y="88" className="cc-gauge-label">{label}</text>
      </svg>
    </div>
  );
}

export default function TelemetryGauges({ telemetry }) {
  const t = telemetry || {};
  const entropy = typeof t.shannon_entropy === 'number' ? t.shannon_entropy : null;
  const aura = AURA[t.confidence_aura] || { v: 0, color: '#52525b', label: '—' };
  const prior = t.decision_prior_distribution || {};
  const priorTotal = Object.values(prior).reduce((a, b) => a + (b || 0), 0) || 1;

  return (
    <div className="cc-panel cc-telemetry">
      <div className="cc-panel-title">COGNITIVE TELEMETRY</div>
      <div className="cc-gauge-row">
        <Gauge
          label="ENTROPY"
          value={entropy}
          display={entropy === null ? '—' : entropy.toFixed(2)}
          color="#22d3ee"
        />
        <Gauge
          label="CONFIDENCE"
          value={aura.v}
          display={aura.label}
          color={aura.color}
        />
      </div>
      <div className="cc-telemetry-meta">
        <span>recursion depth: <b>{t.recursion_depth ?? '—'}</b> / 3</span>
        <span>phase: <b>{t.phase || '—'}</b></span>
      </div>
      <div className="cc-prior">
        <div className="cc-prior-title">belief prior distribution</div>
        <div className="cc-prior-bar">
          {Object.keys(PRIOR_COLOR).map((k) => {
            const pct = ((prior[k] || 0) / priorTotal) * 100;
            if (!pct) return null;
            return (
              <div key={k} className="cc-prior-seg"
                   style={{ width: `${pct}%`, background: PRIOR_COLOR[k] }}
                   title={`${k}: ${prior[k]}`} />
            );
          })}
        </div>
        <div className="cc-prior-legend">
          {Object.entries(prior).map(([k, n]) => (
            <span key={k}><i className="cc-dot" style={{ background: PRIOR_COLOR[k] || '#52525b' }} />{k} {n}</span>
          ))}
        </div>
      </div>
    </div>
  );
}
