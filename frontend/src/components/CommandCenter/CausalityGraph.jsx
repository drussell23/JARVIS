/**
 * CausalityGraph — Slice 110
 * ==========================
 * Dependency-free, force-directed SVG graph of the AI's branching decisions.
 * Nodes = ops (cyan) + files they touched (amber); edges = decision sequence
 * (solid) + op→file "touches" (dashed). A lightweight Fruchterman-Reingold
 * spring/repulsion simulation runs on requestAnimationFrame — no d3, no
 * react-force-graph, no new npm dependency (keeps the lockfile clean).
 *
 * Props:
 *   graph: { nodes: [{id, type, kind, phase, confidence_aura, label}], edges: [{source, target, type}] }
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';

const W = 640;
const H = 420;
const REPULSION = 5200;
const SPRING = 0.015;
const SPRING_LEN = 78;
const DAMPING = 0.86;
const CENTER_PULL = 0.012;

const AURA_COLOR = {
  high: '#34d399',
  medium: '#fbbf24',
  low: '#f87171',
};

function seedPosition(id, i, n) {
  // Deterministic ring seed (no Math.random at seed time → stable first paint).
  const angle = (i / Math.max(1, n)) * Math.PI * 2;
  const r = 120 + (i % 5) * 22;
  return { x: W / 2 + Math.cos(angle) * r, y: H / 2 + Math.sin(angle) * r, vx: 0, vy: 0 };
}

export default function CausalityGraph({ graph }) {
  const nodes = graph?.nodes || [];
  const edges = graph?.edges || [];
  const posRef = useRef(new Map());
  const [, setTick] = useState(0);
  const rafRef = useRef(null);

  // Maintain a stable position map as nodes arrive/leave.
  useMemo(() => {
    const next = new Map();
    nodes.forEach((nd, i) => {
      next.set(nd.id, posRef.current.get(nd.id) || seedPosition(nd.id, i, nodes.length));
    });
    posRef.current = next;
  }, [nodes]);

  useEffect(() => {
    const adjacency = edges
      .map((e) => [e.source, e.target])
      .filter(([a, b]) => posRef.current.has(a) && posRef.current.has(b));

    const step = () => {
      const pos = posRef.current;
      const ids = Array.from(pos.keys());
      // Repulsion (all pairs — bounded node count keeps this cheap).
      for (let i = 0; i < ids.length; i += 1) {
        const a = pos.get(ids[i]);
        for (let j = i + 1; j < ids.length; j += 1) {
          const b = pos.get(ids[j]);
          let dx = a.x - b.x;
          let dy = a.y - b.y;
          let d2 = dx * dx + dy * dy || 0.01;
          const f = REPULSION / d2;
          const d = Math.sqrt(d2);
          const fx = (dx / d) * f;
          const fy = (dy / d) * f;
          a.vx += fx; a.vy += fy;
          b.vx -= fx; b.vy -= fy;
        }
      }
      // Springs along edges.
      adjacency.forEach(([sa, sb]) => {
        const a = pos.get(sa);
        const b = pos.get(sb);
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const f = SPRING * (d - SPRING_LEN);
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        a.vx += fx; a.vy += fy;
        b.vx -= fx; b.vy -= fy;
      });
      // Center gravity + integrate + damp + clamp.
      pos.forEach((p) => {
        p.vx += (W / 2 - p.x) * CENTER_PULL;
        p.vy += (H / 2 - p.y) * CENTER_PULL;
        p.vx *= DAMPING; p.vy *= DAMPING;
        p.x = Math.max(16, Math.min(W - 16, p.x + p.vx));
        p.y = Math.max(16, Math.min(H - 16, p.y + p.vy));
      });
      setTick((t) => (t + 1) % 1000000);
      rafRef.current = requestAnimationFrame(step);
    };
    rafRef.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(rafRef.current);
  }, [edges, nodes.length]);

  const pos = posRef.current;

  return (
    <div className="cc-panel cc-causality">
      <div className="cc-panel-title">CAUSALITY DAG · {nodes.length} nodes</div>
      <svg viewBox={`0 0 ${W} ${H}`} className="cc-graph-svg" preserveAspectRatio="xMidYMid meet">
        <defs>
          <marker id="cc-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3"
                  orient="auto" markerUnits="strokeWidth">
            <path d="M0,0 L6,3 L0,6 Z" fill="#3b82f6" />
          </marker>
        </defs>
        {edges.map((e, i) => {
          const a = pos.get(e.source);
          const b = pos.get(e.target);
          if (!a || !b) return null;
          const isSeq = e.type === 'sequence';
          return (
            <line key={`e${i}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
              stroke={isSeq ? '#3b82f6' : '#52525b'}
              strokeWidth={isSeq ? 1.6 : 1}
              strokeDasharray={isSeq ? '0' : '4 3'}
              markerEnd={isSeq ? 'url(#cc-arrow)' : undefined}
              opacity={0.7} />
          );
        })}
        {nodes.map((nd) => {
          const p = pos.get(nd.id);
          if (!p) return null;
          const isFile = nd.type === 'file';
          const color = isFile ? '#f59e0b' : (AURA_COLOR[nd.confidence_aura] || '#22d3ee');
          return (
            <g key={nd.id} transform={`translate(${p.x},${p.y})`}>
              {isFile ? (
                <rect x={-5} y={-5} width={10} height={10} rx={2} fill={color} opacity={0.9} />
              ) : (
                <circle r={7} fill={color} opacity={0.95}>
                  <title>{`${nd.kind || ''} ${nd.phase || ''}`}</title>
                </circle>
              )}
              <text x={10} y={4} className="cc-node-label">
                {isFile ? (nd.label || '').split('/').pop() : (nd.id || '').slice(0, 10)}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="cc-legend">
        <span><i className="cc-dot cc-cyan" /> op</span>
        <span><i className="cc-dot cc-amber" /> file</span>
        <span><i className="cc-line cc-blue" /> decision flow</span>
        <span><i className="cc-line cc-dash" /> touches</span>
      </div>
    </div>
  );
}
