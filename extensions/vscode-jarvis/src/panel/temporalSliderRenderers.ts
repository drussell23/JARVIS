/**
 * Pure render functions for the Temporal Slider panel.
 *
 * Extracted from ``temporalSliderPanel.ts`` so the renderers can
 * be unit-tested without booting VS Code (mirrors the existing
 * extraction pattern for the worktree topology + op-detail
 * panels).
 *
 * Authority discipline:
 *   * No vscode imports — pure functions over the wire types.
 *   * Webview script uses ``textContent`` + DOM construction
 *     ONLY for dynamic content. ``innerHTML`` is reserved for
 *     fully-static template strings, so no XSS taint flow exists
 *     from data to HTML even though backend data is server-trusted.
 *   * The serialized JSON payload's ``</script>`` sequences are
 *     escaped before injection so a doctored payload can't break
 *     out of the inline script block.
 */

import {
  DagRecordResponse,
  DagSessionResponse,
  ReplayHealthResponse,
  ReplayVerdictsResponse,
  SessionListResponse,
  SessionProjection,
} from '../api/types';

/**
 * Composite state object passed to ``renderHtml`` — assembled by
 * the panel from the four GET endpoints. Optional fields render
 * empty / loading-state UX when absent.
 */
export interface TemporalSliderState {
  readonly sessions: SessionListResponse | null;
  readonly selectedSessionId: string | null;
  readonly dag: DagSessionResponse | null;
  readonly selectedRecordIndex: number;  // 0-based; -1 = none
  readonly record: DagRecordResponse | null;
  readonly replayHealth: ReplayHealthResponse | null;
  readonly replayVerdicts: ReplayVerdictsResponse | null;
}

export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderSessionItem(
  s: SessionProjection, isSelected: boolean,
): string {
  const sid = String(s.session_id);
  const klass = isSelected ? 'session-item selected' : 'session-item';
  const ok = s.ok_outcome === true ? '✓' :
             (s.ok_outcome === false ? '✗' : '');
  const flags: string[] = [];
  if (s.bookmarked === true) flags.push('★');
  if (s.pinned === true) flags.push('📌');
  if (s.has_replay === true) flags.push('⏵');
  if (s.parse_error === true) flags.push('⚠');
  const flagStr = flags.length > 0
    ? ` <span class="dim">${escapeHtml(flags.join(' '))}</span>` : '';
  return (
    `<div class="${klass}" data-session-id="${escapeHtml(sid)}">` +
    `<span class="ok">${escapeHtml(ok)}</span> ` +
    `${escapeHtml(sid)}${flagStr}` +
    `</div>`
  );
}

function renderRecordSummary(record: Record<string, unknown>): string {
  // Render top-level keys + values as a key/value table. Values
  // that are objects/arrays render as JSON (escaped). String
  // values are escaped + truncated. The substrate doesn't pin
  // a strict shape so we render every key.
  const rows: string[] = [];
  const keys = Object.keys(record).sort();
  for (const k of keys) {
    const v = record[k];
    let displayValue: string;
    if (v === null) {
      displayValue = 'null';
    } else if (
      typeof v === 'string' ||
      typeof v === 'number' ||
      typeof v === 'boolean'
    ) {
      const s = String(v);
      displayValue = escapeHtml(s.length > 240 ? s.slice(0, 240) + '…' : s);
    } else {
      try {
        const json = JSON.stringify(v);
        displayValue = escapeHtml(
          json.length > 240 ? json.slice(0, 240) + '…' : json,
        );
      } catch {
        displayValue = '<span class="dim">[unserializable]</span>';
      }
    }
    rows.push(
      `<tr><td class="k">${escapeHtml(k)}</td>` +
      `<td class="v">${displayValue}</td></tr>`,
    );
  }
  return rows.length > 0
    ? `<table class="record-fields">${rows.join('')}</table>`
    : '<div class="dim">no fields</div>';
}

function renderEdgeList(
  label: string, ids: readonly string[], emptyText: string,
): string {
  if (ids.length === 0) {
    return (
      `<div class="edge-list"><strong>${escapeHtml(label)}:</strong> ` +
      `<span class="dim">${escapeHtml(emptyText)}</span></div>`
    );
  }
  const items = ids.map((id) =>
    `<a class="edge-link" data-record-id="${escapeHtml(id)}">` +
    `${escapeHtml(id)}</a>`,
  ).join(', ');
  return (
    `<div class="edge-list"><strong>${escapeHtml(label)}:</strong> ${items}</div>`
  );
}

function renderVerdictRow(
  v: Record<string, unknown>, _idx: number,
): string {
  const verdict = String(v.verdict ?? 'unknown');
  const tightening = String(v.tightening ?? '');
  const cluster = String(v.cluster_kind ?? '');
  const klass = `verdict verdict-${escapeHtml(verdict)}`;
  return (
    `<div class="${klass}">` +
    `<span class="verdict-kind">${escapeHtml(verdict)}</span>` +
    (tightening ? ` <span class="dim">${escapeHtml(tightening)}</span>` : '') +
    (cluster ? ` <span class="dim">·</span> <span class="dim">${escapeHtml(cluster)}</span>` : '') +
    `</div>`
  );
}

function renderTickClass(
  recordId: string, idx: number, selectedIdx: number,
): string {
  const klasses: string[] = ['tick'];
  if (idx === selectedIdx) klasses.push('selected');
  // Heuristic: phase derivable from record_id when shaped
  // ``<sid>:<phase>:<seq>`` (causality_dag convention). Falls back
  // to neutral.
  const parts = recordId.split(':');
  if (parts.length >= 2) {
    const phase = parts[parts.length - 2].toLowerCase();
    klasses.push(`phase-${phase.replace(/[^a-z0-9_-]/g, '')}`);
  }
  return klasses.join(' ');
}

export function renderHtml(
  state: TemporalSliderState, nonce: string,
): string {
  const sessions = state.sessions?.sessions ?? [];
  const selectedSession = state.selectedSessionId;
  const dag = state.dag;
  const recordIds = dag?.record_ids ?? [];
  const sel = state.selectedRecordIndex;
  const record = state.record;
  const verdicts = state.replayVerdicts?.verdicts ?? [];
  const health = state.replayHealth;

  const sessionListHtml = sessions.length === 0
    ? '<div class="empty">no sessions found</div>'
    : sessions.map((s) =>
        renderSessionItem(s, s.session_id === selectedSession),
      ).join('');

  const ticksHtml = recordIds.length === 0
    ? '<div class="empty">select a session to load DAG</div>'
    : recordIds.map((rid, i) =>
        `<div class="${renderTickClass(rid, i, sel)}" ` +
        `data-record-index="${i}" data-record-id="${escapeHtml(rid)}" ` +
        `title="${escapeHtml(rid)}"></div>`,
      ).join('');

  const recordPanelHtml = record === null
    ? '<div class="empty">scrub the timeline to inspect a record</div>'
    : `
      <div class="record-header">
        <h3>${escapeHtml(record.record_id)}</h3>
        <div class="dim">subgraph_node_count: ${record.subgraph_node_count}</div>
      </div>
      <h4>Fields</h4>
      ${renderRecordSummary(record.record)}
      <h4>Causality</h4>
      ${renderEdgeList('parents', record.parents, 'root')}
      ${renderEdgeList('children', record.children, 'leaf')}
      ${record.counterfactual_branches.length > 0
        ? `<h4>Counterfactual branches</h4>` +
          `<div class="cf-branches">` +
          record.counterfactual_branches.map((b) =>
            `<pre class="cf">${escapeHtml(JSON.stringify(b, null, 2))}</pre>`,
          ).join('') +
          `</div>`
        : ''}
    `;

  const verdictsHtml = verdicts.length === 0
    ? '<div class="dim">no verdicts on record</div>'
    : verdicts.map(renderVerdictRow).join('');

  const healthHtml = health === null
    ? '<span class="dim">replay surface unreachable</span>'
    : `<span class="dim">replay: ` +
      `engine=${health.engine_enabled ? 'on' : 'off'}, ` +
      `comparator=${health.comparator_enabled ? 'on' : 'off'}, ` +
      `observer=${health.observer_enabled ? 'on' : 'off'}, ` +
      `history_count=${health.history_count}` +
      `</span>`;

  // </script> escape on every JSON payload that's inlined into
  // the script block.
  const safeRecordIds = JSON.stringify(recordIds)
    .replace(/<\/script>/gi, '<\\/script>');

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>JARVIS Temporal Slider</title>
<style>
  body { font: 13px/1.45 -apple-system, BlinkMacSystemFont, sans-serif; padding: 0; margin: 0; height: 100vh; display: flex; flex-direction: column; }
  header { padding: 10px 14px; border-bottom: 1px solid var(--vscode-panel-border); display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 14px; margin: 0; flex: 1; }
  .layout { display: flex; flex: 1; overflow: hidden; }
  aside { width: 240px; border-right: 1px solid var(--vscode-panel-border); overflow-y: auto; padding: 8px; }
  main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
  .timeline { padding: 12px 16px; border-bottom: 1px solid var(--vscode-panel-border); }
  .timeline-meta { font-size: 11px; color: var(--vscode-descriptionForeground); margin-bottom: 6px; }
  .ticks { display: flex; gap: 1px; height: 28px; align-items: stretch; padding: 4px; background: var(--vscode-editor-background); border: 1px solid var(--vscode-panel-border); border-radius: 3px; overflow-x: auto; }
  .tick { flex: 1; min-width: 4px; max-width: 16px; background: var(--vscode-charts-blue); opacity: 0.5; cursor: pointer; transition: opacity 0.1s, transform 0.1s; }
  .tick:hover { opacity: 1; transform: scaleY(1.1); }
  .tick.selected { opacity: 1; outline: 2px solid var(--vscode-charts-yellow); outline-offset: 1px; }
  .tick.phase-classify  { background: #6c757d; }
  .tick.phase-route     { background: var(--vscode-charts-blue); }
  .tick.phase-plan      { background: var(--vscode-charts-purple); }
  .tick.phase-generate  { background: var(--vscode-charts-orange); }
  .tick.phase-validate  { background: var(--vscode-charts-yellow); }
  .tick.phase-gate      { background: var(--vscode-charts-red); }
  .tick.phase-approve   { background: var(--vscode-charts-green); }
  .tick.phase-apply     { background: var(--vscode-charts-green); }
  .tick.phase-verify    { background: var(--vscode-charts-green); }
  .tick.phase-complete  { background: var(--vscode-charts-green); }
  .scrub-controls { display: flex; gap: 8px; align-items: center; margin-top: 8px; font-size: 11px; }
  .scrub-controls input[type="range"] { flex: 1; cursor: pointer; }
  .record-pane { flex: 1; padding: 14px; overflow-y: auto; }
  .verdicts-pane { padding: 8px 14px; border-top: 1px solid var(--vscode-panel-border); max-height: 140px; overflow-y: auto; background: var(--vscode-editor-inactiveSelectionBackground); }
  .session-item { padding: 4px 6px; cursor: pointer; font-family: monospace; font-size: 11px; border-radius: 3px; }
  .session-item:hover { background: var(--vscode-list-hoverBackground); }
  .session-item.selected { background: var(--vscode-list-activeSelectionBackground); color: var(--vscode-list-activeSelectionForeground); }
  .session-item .ok { display: inline-block; width: 14px; }
  .empty { color: var(--vscode-descriptionForeground); font-style: italic; padding: 12px; text-align: center; }
  .dim { color: var(--vscode-descriptionForeground); }
  h3 { margin: 0 0 4px 0; font-size: 13px; font-family: monospace; }
  h4 { margin: 14px 0 6px 0; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--vscode-descriptionForeground); }
  table.record-fields { width: 100%; border-collapse: collapse; }
  table.record-fields td { padding: 2px 8px; vertical-align: top; font-size: 12px; border-bottom: 1px solid var(--vscode-panel-border); }
  table.record-fields td.k { color: var(--vscode-descriptionForeground); width: 30%; max-width: 200px; word-break: break-word; }
  table.record-fields td.v { font-family: monospace; word-break: break-word; }
  .edge-list { font-size: 12px; margin: 6px 0; }
  .edge-link { color: var(--vscode-textLink-foreground); cursor: pointer; font-family: monospace; margin-right: 4px; }
  .edge-link:hover { text-decoration: underline; }
  .cf-branches { max-height: 200px; overflow-y: auto; }
  .cf { font-size: 11px; background: var(--vscode-textBlockQuote-background); padding: 6px; border-left: 3px solid var(--vscode-charts-purple); margin: 4px 0; white-space: pre-wrap; word-break: break-word; }
  .verdict { display: inline-block; padding: 2px 8px; margin: 2px 4px 2px 0; border-radius: 3px; font-size: 11px; font-family: monospace; }
  .verdict-equivalent       { background: #6c757d; color: white; }
  .verdict-diverged_better  { background: var(--vscode-charts-green); color: black; }
  .verdict-diverged_worse   { background: var(--vscode-errorForeground); color: white; }
  .verdict-diverged_neutral { background: var(--vscode-charts-yellow); color: black; }
  .verdict-failed           { background: var(--vscode-errorForeground); color: white; }
  .verdict-kind { font-weight: 600; }
  button { padding: 4px 10px; cursor: pointer; background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  input[type="text"] { font-family: monospace; padding: 2px 4px; background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border); }
</style>
</head>
<body>
<header>
  <h1>JARVIS Temporal Slider</h1>
  ${healthHtml}
  <button id="refresh-btn">Refresh</button>
</header>
<div class="layout">
  <aside>
    <div class="dim" style="margin-bottom: 4px; font-size: 11px;">Sessions</div>
    <input id="prefix-filter" type="text" placeholder="filter prefix…" style="width: 100%; box-sizing: border-box; margin-bottom: 6px;">
    <div id="session-list">${sessionListHtml}</div>
  </aside>
  <main>
    <div class="timeline">
      <div class="timeline-meta">
        ${selectedSession === null
          ? '<span class="dim">no session selected</span>'
          : `session: <code>${escapeHtml(selectedSession)}</code> ` +
            `(${recordIds.length} records, ${dag?.edge_count ?? 0} edges)`}
      </div>
      <div class="ticks" id="timeline-ticks">${ticksHtml}</div>
      ${recordIds.length > 0 ? `
        <div class="scrub-controls">
          <button id="step-back" ${sel <= 0 ? 'disabled' : ''}>◀</button>
          <input type="range" id="scrubber" min="0" max="${recordIds.length - 1}" value="${sel < 0 ? 0 : sel}">
          <button id="step-forward" ${sel >= recordIds.length - 1 ? 'disabled' : ''}>▶</button>
          <span class="dim" id="scrub-label">
            ${sel >= 0 ? `${sel + 1} / ${recordIds.length}` : `– / ${recordIds.length}`}
          </span>
        </div>
      ` : ''}
    </div>
    <div class="record-pane" id="record-pane">${recordPanelHtml}</div>
    <div class="verdicts-pane">
      <div class="dim" style="margin-bottom: 4px; font-size: 11px;">Recent replay verdicts</div>
      ${verdictsHtml}
    </div>
  </main>
</div>

<script nonce="${nonce}">
  (function () {
    const vscode = acquireVsCodeApi();
    const RECORD_IDS = ${safeRecordIds};

    function postSelectSession(sessionId) {
      vscode.postMessage({
        type: 'select_session',
        payload: { session_id: sessionId },
      });
    }

    function postSelectRecord(recordIndex) {
      vscode.postMessage({
        type: 'select_record',
        payload: { record_index: recordIndex },
      });
    }

    document.getElementById('refresh-btn').addEventListener('click', function () {
      vscode.postMessage({ type: 'refresh' });
    });

    const prefixInput = document.getElementById('prefix-filter');
    if (prefixInput) {
      let prefixDebounce = null;
      prefixInput.addEventListener('input', function () {
        if (prefixDebounce !== null) clearTimeout(prefixDebounce);
        prefixDebounce = setTimeout(function () {
          const v = prefixInput.value;
          // Validate: substrate _SESSION_ID_RE wants
          // [A-Za-z0-9_\\-:.] only; reject inputs with anything else
          // before round-tripping.
          if (v === '' || /^[A-Za-z0-9_\\-:.]{1,128}$/.test(v)) {
            vscode.postMessage({
              type: 'set_prefix_filter',
              payload: { prefix: v },
            });
          }
        }, 250);
      });
    }

    const sessionItems = document.querySelectorAll('.session-item');
    for (let i = 0; i < sessionItems.length; i++) {
      (function (el) {
        el.addEventListener('click', function () {
          const sid = el.getAttribute('data-session-id');
          if (sid) postSelectSession(sid);
        });
      })(sessionItems[i]);
    }

    const ticks = document.querySelectorAll('.tick');
    for (let i = 0; i < ticks.length; i++) {
      (function (el) {
        el.addEventListener('click', function () {
          const idxStr = el.getAttribute('data-record-index');
          if (idxStr !== null) postSelectRecord(parseInt(idxStr, 10));
        });
      })(ticks[i]);
    }

    const scrubber = document.getElementById('scrubber');
    const stepBack = document.getElementById('step-back');
    const stepForward = document.getElementById('step-forward');
    const scrubLabel = document.getElementById('scrub-label');
    if (scrubber) {
      // Live update label as user drags; commit on change.
      scrubber.addEventListener('input', function () {
        if (scrubLabel) {
          scrubLabel.textContent =
            (parseInt(scrubber.value, 10) + 1) + ' / ' + RECORD_IDS.length;
        }
      });
      scrubber.addEventListener('change', function () {
        postSelectRecord(parseInt(scrubber.value, 10));
      });
    }
    if (stepBack) {
      stepBack.addEventListener('click', function () {
        if (scrubber && parseInt(scrubber.value, 10) > 0) {
          postSelectRecord(parseInt(scrubber.value, 10) - 1);
        }
      });
    }
    if (stepForward) {
      stepForward.addEventListener('click', function () {
        if (scrubber && parseInt(scrubber.value, 10) < RECORD_IDS.length - 1) {
          postSelectRecord(parseInt(scrubber.value, 10) + 1);
        }
      });
    }

    // Edge-link click delegation
    const edgeLinks = document.querySelectorAll('.edge-link');
    for (let i = 0; i < edgeLinks.length; i++) {
      (function (el) {
        el.addEventListener('click', function () {
          const rid = el.getAttribute('data-record-id');
          if (!rid) return;
          // Find this record_id in the current RECORD_IDS list;
          // if present, scrub to it. If not (cross-session edge),
          // ignore — operator can paste into the prefix filter.
          const idx = RECORD_IDS.indexOf(rid);
          if (idx >= 0) postSelectRecord(idx);
        });
      })(edgeLinks[i]);
    }

    // Keyboard: ←/→ step through records
    document.addEventListener('keydown', function (e) {
      if (!scrubber) return;
      const cur = parseInt(scrubber.value, 10);
      if (e.key === 'ArrowLeft' && cur > 0) {
        e.preventDefault();
        postSelectRecord(cur - 1);
      } else if (e.key === 'ArrowRight' && cur < RECORD_IDS.length - 1) {
        e.preventDefault();
        postSelectRecord(cur + 1);
      }
    });
  })();
</script>
</body>
</html>`;
}

export function renderErrorHtml(error: string, nonce: string): string {
  return `<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  body { font: 13px -apple-system, sans-serif; padding: 16px; }
  .err { background: var(--vscode-inputValidation-errorBackground); padding: 12px; border: 1px solid var(--vscode-errorForeground); }
  pre { font-family: monospace; white-space: pre-wrap; word-break: break-word; }
  button { padding: 4px 10px; margin-top: 8px; cursor: pointer; }
</style>
</head>
<body>
<div class="err">
  <h2>Failed to load Temporal Slider</h2>
  <pre>${escapeHtml(error)}</pre>
  <button id="retry">Retry</button>
</div>
<script nonce="${nonce}">
  (function () {
    const vscode = acquireVsCodeApi();
    document.getElementById('retry').addEventListener('click', function () {
      vscode.postMessage({ type: 'refresh' });
    });
  })();
</script>
</body></html>`;
}
