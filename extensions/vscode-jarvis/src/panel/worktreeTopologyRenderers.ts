/**
 * Pure render functions for the Worktree Topology panel.
 *
 * Extracted from ``worktreeTopologyPanel.ts`` so the renderers
 * can be unit-tested without booting VS Code (mirrors the existing
 * ``renderers.ts`` pattern for the op-detail panel).
 *
 * Authority discipline:
 *   * No vscode imports — pure functions over the wire types.
 *   * Webview script (inlined into the HTML) uses textContent +
 *     DOM construction ONLY for dynamic content. innerHTML is
 *     reserved for fully-static template strings, so no XSS taint
 *     flow exists from data to HTML even though topology data is
 *     server-trusted.
 */

import { WorktreeTopologyProjection } from '../api/types';

export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderSummaryRow(
  label: string, value: string | number,
): string {
  return (
    `<tr><td class="k">${escapeHtml(label)}</td>` +
    `<td class="v">${escapeHtml(String(value))}</td></tr>`
  );
}

export function renderHtml(
  topology: WorktreeTopologyProjection, nonce: string,
): string {
  const summary = topology.summary;
  const stateCounts = Object.entries(summary.units_by_state)
    .map(([k, v]) =>
      `<span class="pill state-${escapeHtml(k)}">` +
      `${escapeHtml(k)}: ${v}</span>`)
    .join(' ') || '<span class="dim">none</span>';
  const phaseCounts = Object.entries(summary.graphs_by_phase)
    .map(([k, v]) =>
      `<span class="pill phase-${escapeHtml(k)}">` +
      `${escapeHtml(k)}: ${v}</span>`)
    .join(' ') || '<span class="dim">none</span>';

  const orphanBlock = summary.orphan_worktree_count > 0
    ? `<div class="warn">⚠ ${summary.orphan_worktree_count} ` +
      `orphan worktree(s) on disk: ` +
      `<code>${
        summary.orphan_worktree_paths.map(escapeHtml).join('</code>, <code>')
      }</code></div>`
    : '';

  // Inline JSON payload for the webview script — escape </script>
  // sequences so a doctored payload can't break out of the inline
  // <script>...</script> block. The webview script renders this
  // payload using textContent + safe DOM construction (no inner-
  // HTML, no eval) so no XSS taint flow exists.
  const safeJson = JSON.stringify(topology)
    .replace(/<\/script>/gi, '<\\/script>');

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>JARVIS Worktree Topology</title>
<style>
  body { font: 13px/1.45 -apple-system, BlinkMacSystemFont, sans-serif; padding: 14px; }
  h1 { font-size: 14px; margin: 0 0 10px 0; }
  h2 { font-size: 12px; margin: 18px 0 6px 0; color: var(--vscode-descriptionForeground); text-transform: uppercase; letter-spacing: 0.05em; }
  .summary { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-bottom: 14px; }
  .summary table { width: 100%; border-collapse: collapse; }
  .summary td { font-size: 12px; padding: 2px 6px; }
  td.k { color: var(--vscode-descriptionForeground); width: 50%; }
  td.v { font-family: monospace; }
  .dim { color: var(--vscode-descriptionForeground); font-style: italic; }
  .warn { background: var(--vscode-inputValidation-warningBackground); padding: 8px; margin: 8px 0; border: 1px solid var(--vscode-inputValidation-warningBorder); border-radius: 3px; font-size: 12px; }
  .warn code { font-family: monospace; }
  .pill { display: inline-block; padding: 1px 7px; margin-right: 4px; border-radius: 8px; font-size: 11px; }
  .pill.state-pending   { background: #6c757d; color: white; }
  .pill.state-running   { background: var(--vscode-charts-blue); color: white; }
  .pill.state-completed { background: var(--vscode-charts-green); color: black; }
  .pill.state-failed    { background: var(--vscode-errorForeground); color: white; }
  .pill.state-cancelled { background: var(--vscode-charts-yellow); color: black; }
  .pill.phase-created   { background: #6c757d; color: white; }
  .pill.phase-running   { background: var(--vscode-charts-blue); color: white; }
  .pill.phase-completed { background: var(--vscode-charts-green); color: black; }
  .pill.phase-failed    { background: var(--vscode-errorForeground); color: white; }
  .pill.phase-cancelled { background: var(--vscode-charts-yellow); color: black; }
  .graph-card { border: 1px solid var(--vscode-panel-border); padding: 12px; margin-bottom: 14px; }
  .graph-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  .graph-header h3 { margin: 0; font-size: 13px; font-family: monospace; }
  .graph-meta { font-size: 11px; color: var(--vscode-descriptionForeground); }
  .graph-meta a { color: var(--vscode-textLink-foreground); cursor: pointer; }
  svg.dag { width: 100%; background: var(--vscode-editor-background); border: 1px solid var(--vscode-panel-border); display: block; }
  svg.dag .node circle { stroke: var(--vscode-foreground); stroke-width: 1; cursor: pointer; }
  svg.dag .node.state-pending   circle { fill: #6c757d; }
  svg.dag .node.state-running   circle { fill: var(--vscode-charts-blue); }
  svg.dag .node.state-completed circle { fill: var(--vscode-charts-green); }
  svg.dag .node.state-failed    circle { fill: var(--vscode-errorForeground); }
  svg.dag .node.state-cancelled circle { fill: var(--vscode-charts-yellow); }
  svg.dag .node text { font: 11px monospace; fill: var(--vscode-foreground); pointer-events: none; }
  svg.dag .edge.dependency { stroke: var(--vscode-foreground); stroke-width: 1.2; opacity: 0.7; fill: none; }
  svg.dag .edge.barrier    { stroke: var(--vscode-charts-yellow); stroke-width: 1; stroke-dasharray: 3 3; opacity: 0.7; fill: none; }
  svg.dag .has-worktree    { stroke: var(--vscode-charts-green); stroke-width: 2.5; }
  .node-detail { font-size: 11px; padding: 6px; margin-top: 4px; min-height: 20px; font-family: monospace; color: var(--vscode-descriptionForeground); }
  .node-detail strong { color: var(--vscode-foreground); }
  .node-detail em { color: var(--vscode-descriptionForeground); font-style: italic; }
  button { padding: 4px 12px; cursor: pointer; background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; }
  .empty { text-align: center; padding: 30px; color: var(--vscode-descriptionForeground); font-style: italic; }
</style>
</head>
<body>
<h1>JARVIS Worktree Topology
  <button id="refresh-btn" style="float: right;">Refresh</button>
</h1>

<h2>Summary</h2>
<div class="summary">
  <table>
    ${renderSummaryRow('total_graphs', summary.total_graphs)}
    ${renderSummaryRow('total_units', summary.total_units)}
    ${renderSummaryRow('units_with_worktree', summary.units_with_worktree)}
    ${renderSummaryRow('orphan_worktrees', summary.orphan_worktree_count)}
  </table>
  <div>
    <div style="margin-bottom: 4px;"><strong>units by state:</strong> ${stateCounts}</div>
    <div><strong>graphs by phase:</strong> ${phaseCounts}</div>
  </div>
</div>
${orphanBlock}

<h2>Graphs (${topology.graphs.length})</h2>
${topology.graphs.length === 0
    ? `<div class="empty">no active execution graphs (outcome=${escapeHtml(topology.outcome)})</div>`
    : topology.graphs.map((g) => `
  <div class="graph-card" data-graph-id="${escapeHtml(g.graph_id)}">
    <div class="graph-header">
      <h3>${escapeHtml(g.graph_id)} <span class="pill phase-${escapeHtml(g.phase)}">${escapeHtml(g.phase)}</span></h3>
      <div class="graph-meta">
        op_id: <a class="open-op" data-op-id="${escapeHtml(g.op_id)}">${escapeHtml(g.op_id)}</a>
        • planner: ${escapeHtml(g.planner_id)}
        • plan_digest: ${escapeHtml(g.plan_digest.slice(0, 12))}
        • concurrency: ${g.concurrency_limit}
        ${g.last_error ? `<br><span style="color: var(--vscode-errorForeground);">error: ${escapeHtml(g.last_error)}</span>` : ''}
      </div>
    </div>
    <svg class="dag" id="dag-${escapeHtml(g.graph_id)}" viewBox="0 0 600 360" preserveAspectRatio="xMidYMid meet"></svg>
    <div id="node-detail-${escapeHtml(g.graph_id)}" class="node-detail">hover a node for details</div>
  </div>
`).join('')}

<script nonce="${nonce}">
  (function () {
    const vscode = acquireVsCodeApi();
    const TOPOLOGY = ${safeJson};
    const SVG_NS = 'http://www.w3.org/2000/svg';

    function makeSvg(tag, attrs) {
      const el = document.createElementNS(SVG_NS, tag);
      for (const k in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, k)) {
          el.setAttribute(k, String(attrs[k]));
        }
      }
      return el;
    }

    function layoutGraph(graph, width, height) {
      const nodes = graph.nodes.map(function (n, i) {
        return {
          unit_id: n.unit_id, repo: n.repo, goal: n.goal,
          target_files: n.target_files,
          owned_paths: n.owned_paths,
          state: n.state, has_worktree: n.has_worktree,
          worktree_path: n.worktree_path,
          attempt_count: n.attempt_count,
          x: width * 0.5 + Math.cos((i / graph.nodes.length) * Math.PI * 2) * 100,
          y: height * 0.5 + Math.sin((i / graph.nodes.length) * Math.PI * 2) * 100,
          vx: 0, vy: 0,
        };
      });
      const idx = new Map(nodes.map(function (n, i) { return [n.unit_id, i]; }));
      const edges = graph.edges
        .filter(function (e) { return idx.has(e.from_unit_id) && idx.has(e.to_unit_id); })
        .map(function (e) {
          return {
            source: idx.get(e.from_unit_id),
            target: idx.get(e.to_unit_id),
            edge_kind: e.edge_kind,
          };
        });

      const TICKS = 80;
      const REPULSE = 1500;
      const SPRING = 0.04;
      const SPRING_LEN = 80;
      const CENTER = 0.01;
      const DAMP = 0.85;
      for (let t = 0; t < TICKS; t++) {
        for (let i = 0; i < nodes.length; i++) {
          for (let j = i + 1; j < nodes.length; j++) {
            const a = nodes[i], b = nodes[j];
            const dx = b.x - a.x, dy = b.y - a.y;
            const distSq = Math.max(dx * dx + dy * dy, 1);
            const force = REPULSE / distSq;
            const dist = Math.sqrt(distSq);
            const fx = (dx / dist) * force, fy = (dy / dist) * force;
            a.vx -= fx; a.vy -= fy;
            b.vx += fx; b.vy += fy;
          }
        }
        for (let k = 0; k < edges.length; k++) {
          const e = edges[k];
          const a = nodes[e.source], b = nodes[e.target];
          const dx = b.x - a.x, dy = b.y - a.y;
          const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
          const delta = dist - SPRING_LEN;
          const fx = (dx / dist) * delta * SPRING;
          const fy = (dy / dist) * delta * SPRING;
          a.vx += fx; a.vy += fy;
          b.vx -= fx; b.vy -= fy;
        }
        for (let m = 0; m < nodes.length; m++) {
          const n = nodes[m];
          n.vx += (width / 2 - n.x) * CENTER;
          n.vy += (height / 2 - n.y) * CENTER;
        }
        for (let m = 0; m < nodes.length; m++) {
          const n = nodes[m];
          n.vx *= DAMP; n.vy *= DAMP;
          n.x += n.vx; n.y += n.vy;
          n.x = Math.max(20, Math.min(width - 20, n.x));
          n.y = Math.max(20, Math.min(height - 20, n.y));
        }
      }
      return { nodes: nodes, edges: edges };
    }

    function writeNodeDetail(detailEl, n) {
      while (detailEl.firstChild) detailEl.removeChild(detailEl.firstChild);
      const head = document.createElement('strong');
      head.textContent = n.unit_id;
      detailEl.appendChild(head);
      const stateNode = document.createTextNode(
        ' [' + n.state + '] • repo=' + n.repo +
        ' • attempts=' + n.attempt_count +
        ' • worktree=' + (n.has_worktree ? 'yes' : 'no') +
        (n.worktree_path ? ' (' + n.worktree_path + ')' : ''),
      );
      detailEl.appendChild(stateNode);
      detailEl.appendChild(document.createElement('br'));
      const goalLabel = document.createElement('em');
      goalLabel.textContent = 'goal: ';
      detailEl.appendChild(goalLabel);
      detailEl.appendChild(document.createTextNode(n.goal));
      detailEl.appendChild(document.createElement('br'));
      const filesLabel = document.createElement('em');
      filesLabel.textContent = 'files: ';
      detailEl.appendChild(filesLabel);
      detailEl.appendChild(document.createTextNode(n.target_files.join(', ')));
    }

    function clearNodeDetail(detailEl) {
      while (detailEl.firstChild) detailEl.removeChild(detailEl.firstChild);
      detailEl.textContent = 'hover a node for details';
    }

    function renderDag(svg, graph) {
      const w = 600, h = 360;
      const layout = layoutGraph(graph, w, h);
      const nodes = layout.nodes;
      const edges = layout.edges;

      while (svg.firstChild) svg.removeChild(svg.firstChild);

      const defs = makeSvg('defs', {});
      const marker = makeSvg('marker', {
        id: 'arrow', viewBox: '0 -5 10 10', refX: '14', refY: '0',
        markerWidth: '8', markerHeight: '8', orient: 'auto',
      });
      const arrowPath = makeSvg('path', {
        d: 'M0,-4L8,0L0,4', fill: 'currentColor',
      });
      marker.appendChild(arrowPath);
      defs.appendChild(marker);
      svg.appendChild(defs);

      for (let i = 0; i < edges.length; i++) {
        const e = edges[i];
        const a = nodes[e.source], b = nodes[e.target];
        const line = makeSvg('line', {
          'class': 'edge ' + e.edge_kind,
          x1: a.x, y1: a.y, x2: b.x, y2: b.y,
          'marker-end': 'url(#arrow)',
        });
        svg.appendChild(line);
      }

      const detailId = svg.id.replace(/^dag-/, 'node-detail-');
      const detailEl = document.getElementById(detailId);

      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        const g = makeSvg('g', {
          'class': 'node state-' + n.state,
          transform: 'translate(' + n.x + ',' + n.y + ')',
        });
        const c = makeSvg('circle', { r: '12' });
        if (n.has_worktree) c.classList.add('has-worktree');
        g.appendChild(c);
        const label = makeSvg('text', {
          'text-anchor': 'middle', dy: '24',
        });
        label.textContent = n.unit_id;
        g.appendChild(label);
        (function (node) {
          g.addEventListener('mouseenter', function () {
            if (detailEl) writeNodeDetail(detailEl, node);
          });
          g.addEventListener('mouseleave', function () {
            if (detailEl) clearNodeDetail(detailEl);
          });
        })(n);
        svg.appendChild(g);
      }
    }

    for (let gi = 0; gi < TOPOLOGY.graphs.length; gi++) {
      const g = TOPOLOGY.graphs[gi];
      const svg = document.getElementById('dag-' + g.graph_id);
      if (svg) renderDag(svg, g);
    }

    document.getElementById('refresh-btn').addEventListener('click', function () {
      vscode.postMessage({ type: 'refresh' });
    });

    const links = document.querySelectorAll('a.open-op');
    for (let li = 0; li < links.length; li++) {
      (function (a) {
        a.addEventListener('click', function () {
          const opId = a.getAttribute('data-op-id');
          if (opId) vscode.postMessage({ type: 'open_op', payload: { op_id: opId } });
        });
      })(links[li]);
    }
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
  <h2>Failed to load Worktree Topology</h2>
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
