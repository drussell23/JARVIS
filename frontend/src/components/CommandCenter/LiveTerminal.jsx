/**
 * LiveTerminal — Slice 110
 * ========================
 * Scrolling, color-coded stream of the gateway frames (the command center's
 * "stdout"). High-severity containment breaches flash red. Bounded buffer so a
 * long-running soak never grows unbounded in the DOM.
 *
 * Props: lines = [{ kind, op_id, ts, payload }]  (newest last)
 */

import React, { useEffect, useRef } from 'react';

const KIND_CLASS = {
  why_snapshot: 'cc-line-why',
  containment_breach: 'cc-line-breach',
  telemetry: 'cc-line-tele',
  causality_update: 'cc-line-causal',
  hello: 'cc-line-hello',
  terminal_line: 'cc-line-term',
};

function render(line) {
  const ts = line.ts ? new Date(line.ts * 1000).toLocaleTimeString() : '';
  const p = line.payload || {};
  switch (line.kind) {
    case 'containment_breach':
      return `⛔ CONTAINMENT BREACH op=${line.op_id} vector=${JSON.stringify(p.vector || {})}`;
    case 'why_snapshot': {
      const s = p.snapshot || {};
      const why = s.why || {};
      return `⏺ ${s.kind || ''} op=${line.op_id} aura=${why.confidence_aura ?? '—'} phase=${s.phase || ''}${p.replay ? ' (replay)' : ''}`;
    }
    case 'telemetry':
      return `📊 entropy=${p.shannon_entropy ?? '—'} conf=${p.confidence_aura ?? '—'} depth=${p.recursion_depth ?? '—'}`;
    case 'causality_update':
      return `🕸 causality nodes=${(p.nodes || []).length} edges=${(p.edges || []).length}`;
    case 'hello':
      return `🔌 gateway connected (${p.schema_version || ''})`;
    default:
      return `${line.kind} ${line.op_id || ''}`;
  }
}

export default function LiveTerminal({ lines }) {
  const endRef = useRef(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [lines]);

  return (
    <div className="cc-panel cc-terminal">
      <div className="cc-panel-title">LIVE STREAM</div>
      <div className="cc-terminal-body">
        {lines.map((line, i) => (
          <div key={i} className={`cc-term-line ${KIND_CLASS[line.kind] || ''}`}>
            <span className="cc-term-ts">{line.ts ? new Date(line.ts * 1000).toLocaleTimeString() : ''}</span>
            <span className="cc-term-msg">{render(line)}</span>
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </div>
  );
}
